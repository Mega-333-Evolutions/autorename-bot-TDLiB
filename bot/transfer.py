"""What the bot counts about its transfers, and the one place where it moves the bytes of a file itself: the streamed rename.

TDLib does the transferring (it splits a file into parts and moves them in parallel); the pipeline only tells it what to download and
what to send, and counts how it went for /status.  The exception is ``Relay`` (experimental, off unless ``STREAM_RENAME`` is on): it
makes TDLib upload a file while it is still being downloaded.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .errors import Cancelled
from .tg import Client, Generated, errors

log = logging.getLogger(__name__)

BIG_FILE_THRESHOLD = 10 * 1024 * 1024  # a streamed rename is only worth trying above this: a smaller file is quick the usual way
STALL_TIMEOUT = 120.0  # seconds the download may go without a single new byte before a streamed rename is given up
LOOK_EVERY = 2.0  # seconds between two looks at the download when TDLib has said nothing
REPORT_EVERY = 0.25  # seconds between two reports of how much of the file is ready
COPY_CHUNK = 4 * 1024 * 1024
DOWNLOAD_PRIORITY = 16  # TDLib's scale is 1 to 32; this is what an ordinary download of the bot uses too


@dataclass
class TransferStats:
    """The transfers since the start (shown by /status)."""

    downloads: int = 0  # files downloaded to be renamed
    uploads: int = 0  # renamed files uploaded
    streamed: int = 0  # renames that went from the download into the upload without waiting for the whole file
    stream_fallbacks: int = 0  # streamed renames that failed, so the file was saved first and then uploaded
    last_stream_fallback: str = ""  # what failed last, e.g. "RuntimeError: ..."

    def stream_failed(self, exc: BaseException) -> None:
        self.stream_fallbacks += 1
        self.last_stream_fallback = f"{type(exc).__name__}: {exc}"


stats = TransferStats()


def copy_part(source: str, out_fd: int, start: int, end: int) -> int:
    """Copy the bytes ``start`` to ``end`` of the file at ``source`` onto the end of the open file ``out_fd``.  The offset reached: less
    than ``end`` when the file is shorter than that."""
    at = start
    with open(source, "rb") as src:
        src.seek(start)
        while at < end:
            chunk = src.read(min(COPY_CHUNK, end - at))
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                view = view[os.write(out_fd, view):]
            at += len(chunk)
    return at


class Relay:
    """A file that Telegram uploads under its new name while the bot downloads it.

    ``source`` goes to ``Client.send_file`` in place of a path.  TDLib then asks the bot to generate that file
    (``updateFileGenerationStart``): the relay has TDLib download the original, and as the start of it arrives it appends it to the file
    TDLib is uploading from, telling TDLib how much of it is ready.  The upload follows the download, and nothing is waited for twice.

    ``cancel`` (the job's) stops everything.  ``self.cancel`` is the event to give ``send_file`` instead: it is also set when the
    download side fails, which stops the upload; ``self.error`` then says why, so that the caller can do the rename the usual way.
    """

    def __init__(self, client: Client, message: Any, new_name: str, *, cancel: Optional[asyncio.Event] = None,
                 stall_timeout: float = STALL_TIMEOUT, look_every: float = LOOK_EVERY):
        td_file = (message.file.td_file if getattr(message, "file", None) else None) or {}
        if not td_file.get("id"):
            raise ValueError("there is no file to relay")
        self.client, self.message, self.name, self.td_file = client, message, new_name, td_file
        self.size = int(td_file.get("size") or td_file.get("expected_size") or 0)
        self.conversion = "relay:" + uuid.uuid4().hex
        self.source = Generated(new_name, self.conversion, self.size)
        self.cancel = asyncio.Event()
        self.error: Optional[BaseException] = None
        self.complete = False  # the whole file was downloaded
        self._outer = cancel
        self._stall, self._look = stall_timeout, look_every
        self._task: Optional["asyncio.Future[Any]"] = None  # what is moving the bytes
        self._follower: Optional["asyncio.Future[Any]"] = None  # sets ``cancel`` when the job's is set
        self._stopped = asyncio.Event()  # TDLib said it has no more use for the file
        self._woke = asyncio.Event()  # something happened that the mover of the bytes should look at: news of the download, a cancel, a stop
        self._destination = ""
        self._generation = 0

    # ------------------------------------------------------------------ the scope of one streamed rename
    async def __aenter__(self) -> "Relay":
        self.client.generators[self.conversion] = self._start
        if self._outer is not None:
            self._follower = asyncio.ensure_future(self._follow())
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        self.client.generators.pop(self.conversion, None)
        self.client.generators.pop(f"stop:{self._generation}", None)
        for task in (self._follower, self._task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        file_id = int(self.td_file["id"])
        self.client.unwatch_file(file_id)
        with contextlib.suppress(errors.RPCError):  # what was downloaded stays for the usual way to carry on from; nothing else needs it
            if not self.complete:
                await self.client.call({"@type": "cancelDownloadFile", "file_id": file_id, "only_if_pending": False})
        if self.complete:  # a finished download is TDLib's copy of the original: the disk gets it back
            await self.client.forget_file(self.td_file)
        if self._destination:
            with contextlib.suppress(OSError):
                os.unlink(self._destination)
        return False

    async def _follow(self) -> None:
        assert self._outer is not None
        await self._outer.wait()
        self.cancel.set()
        self._woke.set()

    def _stop(self, _update: Dict[str, Any]) -> None:
        self._stopped.set()
        self._woke.set()

    # ------------------------------------------------------------------ TDLib asks for the file
    async def _start(self, update: Dict[str, Any]) -> None:
        """Called when TDLib wants the file generated: moves the bytes until the file is all there, and tells TDLib how it went."""
        previous, self._task = self._task, asyncio.current_task()
        if previous is not None and previous is not self._task and not previous.done():  # TDLib asked again: the old request is void
            previous.cancel()
            await asyncio.gather(previous, return_exceptions=True)
        generation = self._generation = int(update.get("generation_id") or 0)
        old_destination, self._destination = self._destination, str(update.get("destination_path") or "")
        if old_destination and old_destination != self._destination:
            with contextlib.suppress(OSError):
                os.unlink(old_destination)
        self._stopped.clear()
        self.client.generators[f"stop:{generation}"] = self._stop
        problem: Optional[BaseException] = None
        try:
            finished = await self._move(generation, self._destination)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            finished, problem = False, exc
        finally:
            self.client.generators.pop(f"stop:{generation}", None)
        if self._stopped.is_set() and problem is None and not finished:
            return  # TDLib itself has given up on the file: there is no one to tell
        if problem is not None and not isinstance(problem, Cancelled):
            self.error = problem
            log.warning("could not relay %s to its upload: %s: %s", self.name, type(problem).__name__, problem)
        if finished:
            reply: Dict[str, Any] = {"@type": "finishFileGeneration", "generation_id": generation}
        else:
            what = "cancelled" if problem is None or isinstance(problem, Cancelled) else f"{type(problem).__name__}: {problem}"
            reply = {"@type": "finishFileGeneration", "generation_id": generation, "error": {"@type": "error", "code": 500, "message": what[:200]}}
        with contextlib.suppress(errors.RPCError):
            await self.client.call(reply)
        if not finished:
            self.cancel.set()  # nothing is coming: the upload must not wait for it
            self._woke.set()

    def _watch(self, latest: Dict[str, Any]) -> None:
        def seen(file: Dict[str, Any]) -> None:
            latest["file"] = file
            self._woke.set()

        self.client.watch_file(int(self.td_file["id"]), seen)

    async def _move(self, generation: int, destination: str) -> bool:
        """Download the original and append what arrives to ``destination``.  ``True`` when all of it is there."""
        client, file_id = self.client, int(self.td_file["id"])
        os.makedirs(os.path.dirname(destination) or ".", exist_ok=True)
        woke = self._woke
        latest: Dict[str, Any] = {"file": self.td_file}
        self._watch(latest)
        out_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # there from the start: TDLib reads it as it grows
        try:
            latest["file"] = await client.call({"@type": "downloadFile", "file_id": file_id, "priority": DOWNLOAD_PRIORITY, "offset": 0,
                                                "limit": 0, "synchronous": False})
            copied, reported = 0, -1
            reported_at, alive_at = 0.0, time.monotonic()
            while True:
                if self.cancel.is_set() or (self._outer is not None and self._outer.is_set()):
                    raise Cancelled()
                if self._stopped.is_set():
                    return False
                file = latest["file"]
                local = file.get("local") or {}
                path = local.get("path") or ""
                done = bool(local.get("is_downloading_completed"))
                total = int(file.get("size") or file.get("expected_size") or self.size or 0)
                if done and not total and path and os.path.exists(path):
                    total = os.path.getsize(path)
                ready = total if done and total else int(local.get("downloaded_prefix_size") or 0)
                if path and ready > copied:
                    reached = await asyncio.to_thread(copy_part, path, out_fd, copied, ready)
                    if reached > copied:
                        copied, alive_at = reached, time.monotonic()
                done = done and copied >= total  # all of it is in the destination
                now = time.monotonic()
                if copied != reported and (done or now - reported_at >= REPORT_EVERY):
                    await client.call({"@type": "setFileGenerationProgress", "generation_id": generation,
                                       "expected_size": total or self.size, "local_prefix_size": copied})
                    reported, reported_at = copied, now
                if done:
                    self.complete = True
                    return True
                if not local.get("can_be_downloaded", True) and not local.get("is_downloading_active") and not path:
                    raise RuntimeError("Telegram does not let the bot download this file")
                if now - alive_at > self._stall:
                    raise TimeoutError(f"the download has not moved for {int(self._stall)} seconds")
                wait = self._look
                if copied != reported:  # what was just copied is reported as soon as the pause between two reports is over
                    wait = min(wait, max(0.01, REPORT_EVERY - (now - reported_at)))
                woke.clear()
                try:
                    await asyncio.wait_for(woke.wait(), wait)
                except asyncio.TimeoutError:
                    if copied == reported:  # nothing was said for a while: look, in case an update was missed
                        latest["file"] = await client.call({"@type": "getFile", "file_id": file_id})
        finally:
            os.close(out_fd)
            client.unwatch_file(file_id)
