"""TDLib's own state, kept in the bot's database.

TDLib keeps its login (the keys it shares with Telegram) and what it has learnt about the chats the bot is in (a bot can only use a
channel or group id once it has seen the chat) in a small folder.  A Space's disk is empty after every restart, so that folder is also
kept in the bot's database (the ``kv`` table): ``restore`` puts it back before TDLib starts, ``Saver`` saves it every few minutes when it
changed, and once more after TDLib has closed.

The folder is packed as a ``tar.gz`` (a few tens of kilobytes).  SQLite files are copied through SQLite's backup, which gives a
consistent copy while TDLib is writing to them (it keeps recent changes in a ``-wal`` file next to the database).
"""
from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import io
import logging
import os
import shutil
import sqlite3
import tarfile
import tempfile
from pathlib import Path
from typing import Any, List, Optional, Tuple

log = logging.getLogger(__name__)

KEY_DATA = "tdlib_state"  # the packed folder, base64
KEY_BOT = "tdlib_state_bot"  # the id of the bot (the number before the colon of its token) the state belongs to
MAX_PACKED_BYTES = 8 * 1024 * 1024  # a state bigger than this is not kept (the database holds it as text)
SAVE_INTERVAL = 600.0  # seconds between looks at whether the state changed
_SIDE_FILES = ("-wal", "-shm", "-journal")  # SQLite's companions of a database file: the backup takes what they hold
_SQLITE = (".sqlite", ".sqlite3", ".db")


def _is_side_file(name: str) -> bool:
    return any(name.endswith(ext + side) for ext in _SQLITE for side in _SIDE_FILES)


def _copy_sqlite(path: Path, into: Path) -> Path:
    """A consistent copy of a SQLite database that may be in use; the file itself when it turns out not to be one."""
    target = into / path.name
    try:
        source = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            copy = sqlite3.connect(target)
            try:
                source.backup(copy)
            finally:
                copy.close()
        finally:
            source.close()
        return target
    except sqlite3.Error:
        shutil.copyfile(path, target)
        return target


def pack(folder: Path) -> bytes:
    """The folder as the bytes of a ``tar.gz`` that is the same whenever the files hold the same (no dates in it)."""
    folder = Path(folder)
    members: List[Tuple[str, Path]] = []
    with tempfile.TemporaryDirectory(prefix="tdstate-") as scratch:
        scratch_dir = Path(scratch)
        for root, _dirs, files in os.walk(folder):
            for name in sorted(files):
                path = Path(root) / name
                relative = path.relative_to(folder).as_posix()
                if _is_side_file(name) or path.is_symlink():
                    continue
                if name.endswith(_SQLITE):
                    holder = scratch_dir / hashlib.sha1(relative.encode()).hexdigest()
                    holder.mkdir()
                    members.append((relative, _copy_sqlite(path, holder)))
                else:
                    members.append((relative, path))
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w") as tar:
            for relative, path in sorted(members):
                try:
                    data = path.read_bytes()  # the binlog only grows: a half-written end is something TDLib's own recovery skips
                except FileNotFoundError:
                    continue
                info = tarfile.TarInfo(relative)
                info.size, info.mtime, info.mode, info.uid, info.gid, info.uname, info.gname = len(data), 0, 0o600, 0, 0, "", ""
                tar.addfile(info, io.BytesIO(data))
    packed = io.BytesIO()
    with gzip.GzipFile(fileobj=packed, mode="wb", mtime=0, compresslevel=6) as gz:
        gz.write(raw.getvalue())
    return packed.getvalue()


def unpack(blob: bytes, folder: Path) -> None:
    """Put the files of a packed folder into ``folder`` (made if it is missing).  Anything but plain files with a plain relative name is
    refused, whatever the blob says."""
    folder = Path(folder)
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(blob)), mode="r:") as tar:
        entries = []
        for member in tar.getmembers():
            parts = Path(member.name).parts
            if not member.isfile() or member.name.startswith("/") or ".." in parts or not parts or member.size > 4 * MAX_PACKED_BYTES:
                raise ValueError(f"unexpected entry {member.name!r} in the saved TDLib state")
            entries.append(member)
        folder.mkdir(parents=True, exist_ok=True)
        for member in entries:
            target = folder / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            source = tar.extractfile(member)
            assert source is not None
            target.write_bytes(source.read())


def wipe(folder: Path) -> None:
    shutil.rmtree(folder, ignore_errors=True)


def clean_files(root: Path, keep: str = "database") -> None:
    """Remove what a crashed run left in TDLib's folder (half-downloaded files); the database stays."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    for child in root.iterdir():
        if child.name == keep:
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            try:
                child.unlink()
            except OSError:
                pass


async def restore(db: Any, folder: Path, token_id: str) -> bool:
    """Put the saved state back where TDLib will look for it.  ``False`` when there is nothing to put back: a state of another bot is not
    used, and neither is one when a database folder already exists (it is newer than anything saved)."""
    folder = Path(folder)
    if (folder / "td.binlog").exists():
        return False
    if await db.kv_get(KEY_BOT) != token_id:
        return False
    data = await db.kv_get(KEY_DATA)
    if not data:
        return False
    try:
        wipe(folder)
        unpack(base64.b64decode(data, validate=True), folder)
    except Exception as exc:  # noqa: BLE001
        log.warning("the saved TDLib state could not be used (%s: %s); the bot logs in afresh", type(exc).__name__, exc)
        wipe(folder)
        return False
    log.info("TDLib state restored from the database")
    return True


class Saver:
    """Saves the folder when it changed: ``save()`` now, or every ``interval`` seconds after ``start()``."""

    def __init__(self, db: Any, folder: Path, token_id: str, *, interval: float = SAVE_INTERVAL):
        self.db, self.folder, self.token_id, self.interval = db, Path(folder), token_id, interval
        self._digest: Optional[str] = None
        self._task: Optional["asyncio.Future[None]"] = None
        self._too_big = False

    async def save(self) -> bool:
        """``True`` when something was written to the database."""
        try:
            blob = await asyncio.to_thread(pack, self.folder)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not pack the TDLib state (%s: %s)", type(exc).__name__, exc)
            return False
        if len(blob) > MAX_PACKED_BYTES:
            if not self._too_big:
                log.warning("the TDLib state is %d bytes: too big to keep in the database, so it is not saved", len(blob))
            self._too_big = True
            return False
        digest = hashlib.sha256(blob).hexdigest()
        if digest == self._digest:
            return False
        try:
            await self.db.kv_set(KEY_DATA, base64.b64encode(blob).decode())
            await self.db.kv_set(KEY_BOT, self.token_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not save the TDLib state (%s: %s)", type(exc).__name__, exc)
            return False
        self._digest = digest
        return True

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self._loop())

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            await self.save()

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
