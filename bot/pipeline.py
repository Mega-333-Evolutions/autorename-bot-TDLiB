"""The rename pipeline: download -> (ffmpeg) -> upload, or (when STREAM_RENAME is on) for a plain rename of a big file the upload starts while the file is still downloading."""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from . import datacenter, errorlog, media, transfer
from .config import Config
from .db import Database
from .errors import Cancelled, MediaError
from .formats import apply_format
from .parser import build_values, parse_info, render_caption, split_ext
from .progress import ProgressReporter
from .queue import Job
from .tg import errors, types
from .utils import clip, esc, human_duration, human_size

log = logging.getLogger(__name__)

MIME_OVERRIDES = {
    ".mkv": "video/x-matroska", ".mka": "audio/x-matroska", ".webm": "video/webm", ".mp4": "video/mp4",
    ".m4v": "video/x-m4v", ".mov": "video/quicktime", ".avi": "video/x-msvideo", ".ts": "video/mp2t",
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".flac": "audio/flac", ".ogg": "audio/ogg", ".opus": "audio/ogg",
    ".wav": "audio/x-wav", ".srt": "application/x-subrip", ".ass": "text/x-ssa", ".zip": "application/zip",
}
# containers worth running ffprobe on
PROBE_EXTS = media.VIDEO_CONTAINERS | media.AUDIO_TAG_EXTS | {".avi", ".ts", ".flv", ".wmv", ".mpg", ".mpeg", ".3gp", ".wav", ".aac", ".wma"}


def guess_mime(name: str, fallback: Optional[str]) -> str:
    ext = split_ext(name)[1].lower()
    return MIME_OVERRIDES.get(ext) or mimetypes.guess_type(name)[0] or fallback or "application/octet-stream"


def decide_send_type(setting: str, message: Any) -> str:
    """How the renamed file is sent: ``document`` | ``video`` | ``audio``."""
    if setting in ("document", "video", "audio"):
        return setting
    if getattr(message, "video", None) is not None:
        return "video"
    if getattr(message, "audio", None) is not None:
        return "audio"
    return "document"


def source_ext(message: Any, original_name: str) -> str:
    ext = split_ext(original_name)[1]
    if not ext:
        ext = getattr(message.file, "ext", None) or ""
    return ext


def original_name_of(message: Any) -> str:
    f = message.file
    if f is not None and f.name:
        return f.name
    kind = "video" if getattr(message, "video", None) is not None else "audio" if getattr(message, "audio", None) is not None else "file"
    return f"{kind}_{message.id}{(f.ext if f is not None else '') or ''}"


def make_caption(template: str, *, new_name: str, orig_name: str, ext: str, size: int, duration: float) -> Optional[str]:
    """The caption of a renamed file: the user's (or the format's) template with its variables filled in."""
    parsed = parse_info(split_ext(orig_name)[0])
    values = build_values(parsed, split_ext(orig_name)[0], ext)
    values.update(
        filename=split_ext(new_name)[0], original=orig_name, ext=ext.lstrip("."),
        size=human_size(size), duration=human_duration(duration) if duration else "",
    )
    return render_caption(template, values)[:1000] or None


def source_attributes(message: Any) -> Tuple[Optional[Any], Optional[Any]]:
    """The video and the audio details Telegram keeps with a file: ``(DocumentAttributeVideo, DocumentAttributeAudio)``, ``None`` for a missing one."""
    attributes = getattr(getattr(message, "document", None), "attributes", None) or []
    video = next((a for a in attributes if isinstance(a, types.DocumentAttributeVideo) and not a.round_message), None)
    audio = next((a for a in attributes if isinstance(a, types.DocumentAttributeAudio) and not a.voice), None)
    return video, audio


async def embedded_thumb(client: Any, msg: Any, allow_stripped: bool = False) -> Optional[bytes]:
    """The preview Telegram keeps with a file.  The tiny blurred one (a "stripped" size) only counts when ``allow_stripped``."""
    thumbs = getattr(getattr(msg, "document", None), "thumbs", None) or []
    if not any(not isinstance(t, types.PhotoStrippedSize) for t in thumbs) and not (allow_stripped and thumbs):
        return None
    try:
        return await client.download_media(msg, file=bytes, thumb=-1) or None
    except Exception as exc:  # noqa: BLE001
        log.debug("could not download the embedded thumbnail: %s", exc)
        return None


@dataclass
class StreamPlan:
    """What a streamed rename sends along with the file, worked out without ever reading the file."""

    attrs: List[Any]
    as_document: bool
    thumb: Optional[bytes]
    duration: float


class Renamer:
    def __init__(self, client: Any, cfg: Config, db: Database):
        self.client = client
        self.cfg = cfg
        self.db = db
        # called as ``await on_delivered(job, chat, sent)`` once a file renamed with a manual format is in its chat (the offer
        # to upload it to the Database Channel, see dbflow); ``None``: nothing to do
        self.on_delivered: Optional[Callable[[Job, int, Any], Awaitable[None]]] = None

    # ------------------------------------------------------------------
    async def _fresh_message(self, job: Job) -> Any:
        """Re-fetch the message so its file reference is valid even after a long queue wait."""
        msg = await self.client.get_messages(job.chat_id, ids=job.msg_id)
        if msg is None or msg.file is None:
            raise RuntimeError("the original message was deleted")
        return msg

    def _dc_note(self, msg: Any, upload: bool = False) -> str:
        """Where the file comes from, for the progress message: ``DC 4``.  With ``upload`` also where it goes (the bot's own DC): ``DC 4 → DC 1``."""
        source = datacenter.of_message(msg)
        if source is None:
            return ""
        home = datacenter.home_of(self.client) if upload else None
        return f"{datacenter.label(source)} → {datacenter.label(home)}" if home else datacenter.label(source)

    def _check_space(self, work: Path, size: int, will_remux: bool) -> None:
        need = int(size * (2.2 if will_remux else 1.1)) + 64 * 1024 * 1024
        free = shutil.disk_usage(work).free
        if free < need:
            raise RuntimeError(f"not enough free disk space ({human_size(free)} free, about {human_size(need)} needed)")

    def _room_for_stream(self, size: int) -> bool:
        """A streamed rename keeps two copies of the file for a while: TDLib's download, and the file TDLib uploads from."""
        try:
            return shutil.disk_usage(self.cfg.tdlib_dir).free >= int(size * 2.2) + 64 * 1024 * 1024
        except OSError:  # no folder to look at yet: TDLib will make one
            return True

    # ------------------------------------------------------------------
    async def process(self, job: Job) -> None:
        s = apply_format(await self.db.get_settings(job.user_id), job.fmt)  # no format: the built-in template and caption
        work = Path(self.cfg.temp_dir) / f"job{job.id}"
        work.mkdir(parents=True, exist_ok=True)
        reporter = ProgressReporter(self.client, job.status_msg, job.id, job.new_name, self.cfg.progress_interval)
        started = time.monotonic()
        context = errorlog.error_context.set(  # the log channel report says which file this was
            f"user {job.user_id} · job #{job.id}\n{job.file_name} → {job.new_name} ({human_size(job.size)})"
        )
        try:
            await self._run(job, s, work, reporter, started)
        except Cancelled:
            job.state = "cancelled"
            await reporter.finish("❌ <b>Cancelled</b>\n<code>" + esc(clip(job.new_name, 120)) + "</code>")
        except Exception as exc:  # noqa: BLE001
            job.state = "failed"
            log.exception("Error processing file:")
            reason = clip(f"{type(exc).__name__}: {exc}", 300)
            await reporter.finish(f"❌ <b>Failed</b>\n<code>{esc(clip(job.new_name, 120))}</code>\n\n{esc(reason)}")
        finally:
            errorlog.error_context.reset(context)
            shutil.rmtree(work, ignore_errors=True)

    async def _custom_thumb(self, job: Job) -> Optional[bytes]:
        """The thumbnail the user chose: the one of the format being used (``None``: a frame of the video is used)."""
        return await self.db.get_format_thumb(job.user_id, job.fmt["id"]) if job.fmt else None

    async def _stream_plan(
        self, job: Job, s: Dict[str, Any], msg: Any, ext: str, send_type: str, size: int
    ) -> Optional[StreamPlan]:
        """What a streamed rename would send, or ``None`` when the file has to be looked at first (it is then saved and uploaded as usual).

        Streaming never saves the file, so nothing can read it: the video and audio details come from the ones Telegram keeps
        with the original, the thumbnail from the user's setting or the original's own preview.  A rename that changes the
        file itself (metadata and tracks) cannot be streamed either.
        """
        if not self.cfg.stream_transfer:
            return None
        video, audio = source_attributes(msg)
        source = video if send_type == "video" else audio if send_type == "audio" else (video or audio)
        duration = float(getattr(source, "duration", 0) or 0)
        reason = None
        if getattr(msg, "document", None) is None:
            reason = "it is not a document"
        elif size <= transfer.BIG_FILE_THRESHOLD:
            reason = "it is small, so there is little to gain"
        elif not self._room_for_stream(size):
            reason = "the disk has no room for the two copies streaming keeps"
        elif s["metadata_on"]:
            reason = "metadata and tracks change the file"
        elif send_type == "video" and video is None:
            reason = "Telegram has no video details to reuse"
        elif send_type == "audio" and audio is None:
            reason = "Telegram has no audio details to reuse"
        elif "{duration}" in (s["caption"] or "") and not duration:
            reason = "the caption wants the duration and only the file can tell"
        thumb = None
        if reason is None:
            thumb = await self._custom_thumb(job)
            if thumb is None and ext.lower() in media.VIDEO_CONTAINERS:  # the usual way makes one from a frame of the file
                thumb = await embedded_thumb(self.client, msg)
                if thumb is None:
                    reason = "there is no thumbnail to reuse"
        if reason is not None:
            log.info("%s is not streamed: %s", job.new_name, reason)
            return None
        attrs: List[Any] = [types.DocumentAttributeFilename(job.new_name)]
        if send_type == "video":
            attrs.append(types.DocumentAttributeVideo(duration=video.duration or 0, w=video.w or 1, h=video.h or 1, supports_streaming=True))
        elif send_type == "audio":
            attrs.append(
                types.DocumentAttributeAudio(
                    duration=int(audio.duration or 0), title=audio.title or None, performer=audio.performer or None
                )
            )
        return StreamPlan(attrs=attrs, as_document=(send_type == "document"), thumb=thumb, duration=duration)

    async def _stream(
        self, job: Job, s: Dict[str, Any], msg: Any, orig_name: str, ext: str, send_type: str, size: int,
        reporter: ProgressReporter, started: float,
    ) -> bool:
        """Rename by uploading the file while it is still downloading: nothing is saved by the bot, so the whole takes about as long as
        the slower of the two.  ``True`` when the file was sent; ``False`` when it cannot be streamed or streaming failed (the usual way follows)."""
        plan = await self._stream_plan(job, s, msg, ext, send_type, size)
        if plan is None:
            return False
        reporter.set_stage("⬇️⬆️ Streaming", size, dc=self._dc_note(msg, upload=True))
        began = time.monotonic()
        caption = make_caption(s["caption"], new_name=job.new_name, orig_name=orig_name, ext=ext, size=size, duration=plan.duration)
        warnings = list(job.notes)
        relay = transfer.Relay(self.client, msg, job.new_name, cancel=job.cancel)
        try:
            async with relay:
                target, sent = await self._send(
                    job, s, msg, relay.source, caption=caption, attrs=plan.attrs, as_document=plan.as_document, thumb=plan.thumb,
                    warnings=warnings, reporter=reporter, cancel=relay.cancel,
                )
        except Exception as exc:  # noqa: BLE001
            if job.cancel.is_set():
                raise Cancelled() from exc
            if relay.error is None:  # Telegram refused the message itself (a flood wait, say): not a streaming problem
                raise
            self._stream_failed(job, relay.error)
            return False
        seconds = time.monotonic() - began
        rates = f" · ⬇⬆ {human_size(size / seconds)}/s streamed" if seconds >= 1 else ""
        transfer.stats.streamed += 1
        await self._finish(job, target, sent, final_size=size, warnings=warnings, rates=rates, started=started, reporter=reporter)
        return True

    def _stream_failed(self, job: Job, exc: BaseException) -> None:
        log.error("streaming %s failed (%s: %s); doing it the usual way", job.new_name, type(exc).__name__, exc, exc_info=exc)
        transfer.stats.stream_failed(exc)
        job.notes.append("Streaming didn't work for this file, so it was saved first and then uploaded.")

    async def _send(
        self, job: Job, s: Dict[str, Any], msg: Any, source: Any, *, caption: Optional[str], attrs: List[Any],
        as_document: bool, thumb: Optional[bytes], warnings: List[str], reporter: ProgressReporter,
        cancel: Optional[asyncio.Event] = None,
    ) -> Tuple[int, Any]:
        """Upload ``source`` (a path, or a file being relayed) to the chat it goes to; ``(that chat, the message that was sent)``.
        ``reporter`` hears of the upload, and ``cancel`` (``job.cancel`` unless another event is given) stops it."""
        target = s.get("target_channel") or job.chat_id
        kwargs: Dict[str, Any] = dict(
            caption=caption, attributes=attrs, mime_type=guess_mime(job.new_name, getattr(msg.file, "mime_type", None)),
            force_document=as_document, thumb=thumb, parse_mode="html", progress=reporter,
            cancel=cancel if cancel is not None else job.cancel,
        )
        if target == job.chat_id:
            kwargs["reply_to"] = job.msg_id
        try:
            sent = await self.client.send_file(target, source, **kwargs)
        except (errors.EntityBoundsInvalidError, errors.MediaCaptionTooLongError, errors.MessageTooLongError):
            # the caption template produced something Telegram rejects: send the file without a caption
            kwargs.update(caption=None)
            sent = await self.client.send_file(target, source, **kwargs)
            warnings.append("The caption was rejected by Telegram (check your caption template), so it was left out.")
        transfer.stats.uploads += 1
        return target, sent

    async def _finish(
        self, job: Job, target: int, sent: Any, *, final_size: int, warnings: List[str], rates: str, started: float,
        reporter: ProgressReporter,
    ) -> None:
        """Count a file that was sent and say it is done."""
        await self.db.record_job(job.user_id, final_size)
        job.state = "done"
        took = time.monotonic() - started
        note = "".join(f"\n⚠️ {esc(w)}" for w in warnings)
        where = "" if target == job.chat_id else "\n📣 Sent to your channel."
        await reporter.finish(
            f"✅ <b>Done</b> in {human_duration(took)}\n<code>{esc(clip(job.new_name, 120))}</code>\n{human_size(final_size)}{rates}{where}{note}"
        )
        await self._after_delivery(job, target, sent)

    async def _after_delivery(self, job: Job, target: int, sent: Any) -> None:
        """A file renamed with a manual format may go on to the Database Channel: the offer comes after the file itself."""
        if self.on_delivered is None or job.fmt is None or job.fmt.get("kind") != "manual":
            return
        try:
            await self.on_delivered(job, target, sent)
        except Exception:  # noqa: BLE001 - the rename is done whatever happens here
            log.exception("could not offer the upload to the Database Channel")

    async def _run(self, job: Job, s: Dict[str, Any], work: Path, reporter: ProgressReporter, started: float) -> None:
        client = self.client
        msg = await self._fresh_message(job)
        orig_name = original_name_of(msg)
        ext = source_ext(msg, orig_name)
        send_type = decide_send_type(s["file_type"], msg)
        size = int(msg.file.size or job.size or 0)
        src = work / f"src{ext}"

        if await self._stream(job, s, msg, orig_name, ext, send_type, size, reporter, started):
            return
        self._check_space(work, size, will_remux=bool(s["metadata_on"]))

        # ---- download ------------------------------------------------------------
        reporter.set_stage("⬇️ Downloading", size, dc=self._dc_note(msg))
        began = time.monotonic()
        await client.download_to(msg, src, progress=reporter, cancel=job.cancel)
        transfer.stats.downloads += 1
        down_seconds = time.monotonic() - began
        if job.cancel.is_set():
            raise Cancelled()

        # ---- probe / ffmpeg -----------------------------------------------------------
        info: Optional[media.MediaInfo] = None
        current = src
        if ext.lower() in PROBE_EXTS or send_type in ("video", "audio"):
            try:
                info = await media.probe(src, job.cancel)
            except MediaError as exc:
                log.info("ffprobe could not read %s: %s", orig_name, exc)
        warnings = list(job.notes)
        plan = media.build_plan(info, s, ext) if (info is not None and s["metadata_on"]) else None
        if s["metadata_on"] and plan is None and info is not None and ext.lower() not in (media.VIDEO_CONTAINERS | media.AUDIO_TAG_EXTS):
            warnings.append(f"Metadata is not supported for {ext or 'this file type'}, so it was skipped.")
        if plan is not None:
            warnings.extend(plan.warnings)
            reporter.set_stage("⚙️ Applying metadata & tracks", 0)
            out = work / f"out{ext}"
            await media.apply_plan(src, out, plan, job.cancel)
            src.unlink(missing_ok=True)
            current = out
            info = await media.probe(current, job.cancel)

        final_size = current.stat().st_size

        # ---- how it will be sent -------------------------------------------------------
        if send_type == "video" and (info is None or not info.of("video")):
            send_type = "document"
        if send_type == "audio" and (info is None or not info.of("audio")):
            send_type = "document"
        meta = s["meta"] if s["metadata_on"] else {}
        attrs = [types.DocumentAttributeFilename(job.new_name)]
        if send_type == "video":
            attrs.append(
                types.DocumentAttributeVideo(
                    duration=info.duration or 0, w=info.width or 1, h=info.height or 1, supports_streaming=True
                )
            )
        elif send_type == "audio":
            attrs.append(
                types.DocumentAttributeAudio(
                    duration=int(info.duration or 0),
                    title=(meta.get("title") or info.tags.get("title") or None),
                    performer=(meta.get("artist") or meta.get("author") or info.tags.get("artist") or None),
                )
            )

        thumb = await self._custom_thumb(job)
        if thumb is None and ext.lower() in media.VIDEO_CONTAINERS and info is not None and info.of("video"):
            thumb_path = work / "thumb.jpg"
            if await media.make_video_thumbnail(current, thumb_path, info.duration, job.cancel):
                thumb = thumb_path.read_bytes()

        # ---- caption ---------------------------------------------------------------------
        caption = make_caption(
            s["caption"], new_name=job.new_name, orig_name=orig_name, ext=ext, size=final_size,
            duration=(info.duration if info else 0) or 0,
        )

        # ---- upload ---------------------------------------------------------------------------
        reporter.set_stage("⬆️ Uploading", final_size)
        began = time.monotonic()
        target, sent = await self._send(
            job, s, msg, current, caption=caption, attrs=attrs, as_document=(send_type == "document"), thumb=thumb,
            warnings=warnings, reporter=reporter,
        )
        up_seconds = time.monotonic() - began
        rates = "".join(  # how fast each way went; a transfer under a second says nothing
            f" · {arrow} {human_size(amount / seconds)}/s"
            for arrow, amount, seconds in (("⬇", size, down_seconds), ("⬆", final_size, up_seconds))
            if seconds >= 1 and amount > 0
        )
        await self._finish(job, target, sent, final_size=final_size, warnings=warnings, rates=rates, started=started, reporter=reporter)
