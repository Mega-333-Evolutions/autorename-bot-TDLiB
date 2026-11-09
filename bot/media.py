"""ffprobe / ffmpeg helpers: probing, track selection, metadata, thumbnails."""
from __future__ import annotations

import asyncio
import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .errors import Cancelled, MediaError
from .utils import esc

VIDEO_CONTAINERS = {".mkv", ".mp4", ".m4v", ".mov", ".webm", ".mka"}
AUDIO_TAG_EXTS = {".mp3", ".m4a", ".flac", ".ogg", ".opus"}

# canonical name -> accepted aliases (ISO 639-1, 639-2/B, 639-2/T, common names)
LANGUAGES: Dict[str, Tuple[str, ...]] = {
    "english": ("en", "eng"),
    "japanese": ("ja", "jpn", "jap", "jp"),
    "hindi": ("hi", "hin"),
    "tamil": ("ta", "tam"),
    "telugu": ("te", "tel"),
    "malayalam": ("ml", "mal"),
    "kannada": ("kn", "kan"),
    "bengali": ("bn", "ben", "bangla"),
    "marathi": ("mr", "mar"),
    "punjabi": ("pa", "pan"),
    "gujarati": ("gu", "guj"),
    "urdu": ("ur", "urd"),
    "korean": ("ko", "kor"),
    "chinese": ("zh", "zho", "chi", "mandarin", "cantonese", "cmn", "yue"),
    "spanish": ("es", "spa", "esp"),
    "portuguese": ("pt", "por", "ptbr"),
    "french": ("fr", "fra", "fre"),
    "german": ("de", "deu", "ger"),
    "italian": ("it", "ita"),
    "russian": ("ru", "rus"),
    "arabic": ("ar", "ara"),
    "turkish": ("tr", "tur"),
    "indonesian": ("id", "ind"),
    "thai": ("th", "tha"),
    "vietnamese": ("vi", "vie"),
    "malay": ("ms", "msa", "may"),
    "filipino": ("fil", "tl", "tgl", "tagalog"),
    "polish": ("pl", "pol"),
    "dutch": ("nl", "nld", "dut"),
    "swedish": ("sv", "swe"),
}
_ALIAS: Dict[str, str] = {}
for _name, _aliases in LANGUAGES.items():
    _ALIAS[_name] = _name
    for _a in _aliases:
        _ALIAS[_a] = _name
_UNDETERMINED = {"", "und", "unk", "zxx", "mul", "mis"}


def canonical_lang(code: str) -> Optional[str]:
    code = (code or "").strip().lower()
    code = re.split(r"[-_]", code)[0]
    if code in _UNDETERMINED:
        return None
    return _ALIAS.get(code)


# ---------------------------------------------------------------------------
# probing
# ---------------------------------------------------------------------------


@dataclass
class Stream:
    index: int
    kind: str  # video | audio | subtitle | attachment | data
    codec: str = ""
    lang: str = ""
    title: str = ""
    default: bool = False
    forced: bool = False
    attached_pic: bool = False
    channels: int = 0
    width: int = 0
    height: int = 0


@dataclass
class MediaInfo:
    duration: float = 0.0
    width: int = 0
    height: int = 0
    format_name: str = ""
    tags: Dict[str, str] = field(default_factory=dict)
    streams: List[Stream] = field(default_factory=list)

    def of(self, kind: str) -> List[Stream]:
        return [s for s in self.streams if s.kind == kind]

    @property
    def audio(self) -> List[Stream]:
        return self.of("audio")

    @property
    def subtitles(self) -> List[Stream]:
        return self.of("subtitle")

    @property
    def video(self) -> List[Stream]:
        return [s for s in self.of("video") if not s.attached_pic]


def _parse_hms(value: str) -> float:
    m = re.fullmatch(r"(\d+):(\d+):(\d+(?:\.\d+)?)", value or "")
    if not m:
        return 0.0
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def parse_probe(data: Dict[str, Any]) -> MediaInfo:
    fmt = data.get("format", {}) or {}
    info = MediaInfo(
        format_name=fmt.get("format_name", ""),
        tags={k.lower(): str(v) for k, v in (fmt.get("tags") or {}).items()},
    )
    stream_durations: List[float] = []
    for raw in data.get("streams", []) or []:
        tags = {k.lower(): str(v) for k, v in (raw.get("tags") or {}).items()}
        disp = raw.get("disposition") or {}
        kind = raw.get("codec_type") or "data"
        stream = Stream(
            index=int(raw.get("index", len(info.streams))),
            kind=kind if kind in {"video", "audio", "subtitle", "attachment", "data"} else "data",
            codec=raw.get("codec_name", ""),
            lang=tags.get("language", ""),
            title=tags.get("title", ""),
            default=bool(disp.get("default")),
            forced=bool(disp.get("forced")),
            attached_pic=bool(disp.get("attached_pic")),
            channels=int(raw.get("channels") or 0),
            width=int(raw.get("width") or 0),
            height=int(raw.get("height") or 0),
        )
        info.streams.append(stream)
        for cand in (raw.get("duration"), tags.get("duration")):
            try:
                value = float(cand) if cand and ":" not in str(cand) else _parse_hms(str(cand))
            except ValueError:
                value = 0.0
            if value:
                stream_durations.append(value)
                break
    try:
        info.duration = float(fmt.get("duration") or 0)
    except ValueError:
        info.duration = 0.0
    if not info.duration and stream_durations:
        info.duration = max(stream_durations)
    for s in info.video:
        info.width, info.height = s.width, s.height
        break
    return info


async def _run(cmd: Sequence[str], cancel: Optional[asyncio.Event] = None) -> Tuple[int, bytes, bytes]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except FileNotFoundError as exc:
        raise MediaError(f"{cmd[0]} is not installed") from exc
    comm = asyncio.ensure_future(proc.communicate())
    waiters = [comm]
    cancel_task = None
    if cancel is not None:
        cancel_task = asyncio.ensure_future(cancel.wait())
        waiters.append(cancel_task)
    try:
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        if not comm.done():
            proc.kill()
            await proc.wait()
            comm.cancel()
            raise Cancelled()
        out, err = comm.result()
        return proc.returncode or 0, out, err
    finally:
        if cancel_task is not None:
            cancel_task.cancel()
        if not comm.done():
            comm.cancel()
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass


async def probe(path: Path | str, cancel: Optional[asyncio.Event] = None) -> MediaInfo:
    code, out, err = await _run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)], cancel
    )
    try:
        data = json.loads(out.decode("utf-8", "replace") or "{}")
    except ValueError:
        data = {}
    if not data.get("streams") and code != 0:
        raise MediaError("ffprobe failed: " + err.decode("utf-8", "replace").strip()[-300:])
    return parse_probe(data)


async def probe_raw(path: Path | str, cancel: Optional[asyncio.Event] = None) -> Dict[str, Any]:
    """ffprobe's own JSON (format, streams, chapters) for /mediainfo; an empty dict when it found nothing."""
    code, out, err = await _run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", "-show_chapters", str(path)], cancel
    )
    try:
        data = json.loads(out.decode("utf-8", "replace") or "{}")
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if not data.get("streams") and not data.get("format") and code != 0:
        raise MediaError("ffprobe failed: " + err.decode("utf-8", "replace").strip()[-300:])
    return data


# ---------------------------------------------------------------------------
# track selection
# ---------------------------------------------------------------------------


def parse_track_tokens(text: str) -> List[str]:
    """``"English, Japanese"`` / ``"2 1"`` -> ``["english", "japanese"]`` / ``["2", "1"]``."""
    tokens: List[str] = []
    for raw in re.split(r"[\s,;>]+", (text or "").strip().lower()):
        if raw and raw not in tokens:
            tokens.append(raw)
    return tokens


def validate_track_tokens(tokens: Sequence[str]) -> Optional[str]:
    """Return an error message when a token is not a number or a language word."""
    for token in tokens:
        if token.isdigit():
            if int(token) < 1:
                return f"Track numbers start at 1 (got {token})."
        elif not re.fullmatch(r"[a-z][a-z\-_]{0,23}", token):
            return f"“{token}” is not a track number or language name."
    return None


def _matches_language(stream: Stream, token: str) -> bool:
    wanted = _ALIAS.get(token)
    have = canonical_lang(stream.lang)
    if wanted is None:  # a language we do not know by name: compare raw codes / title words
        raw = re.split(r"[-_]", stream.lang.lower())[0]
        return raw == token or bool(re.search(rf"\b{re.escape(token)}\b", stream.title.lower()))
    if have is not None:
        return have == wanted
    # untagged track: fall back to words in its title ("English [Dub]")
    names = (wanted,) + tuple(a for a in LANGUAGES[wanted] if len(a) > 3)
    title = stream.title.lower()
    return any(re.search(rf"\b{re.escape(n)}\b", title) for n in names)


def resolve_tokens(streams: Sequence[Stream], tokens: Sequence[str]) -> List[Stream]:
    """Pick streams for ``tokens`` in token order.

    Numbers are 1-based positions among ``streams``; language words select every
    matching stream (file order).  A stream is only returned once.
    """
    chosen: List[Stream] = []
    for token in tokens:
        if token.isdigit():
            pos = int(token)
            matches = [streams[pos - 1]] if 1 <= pos <= len(streams) else []
        else:
            matches = [s for s in streams if _matches_language(s, token)]
        for s in matches:
            if s not in chosen:
                chosen.append(s)
    return chosen


def lang_label(stream: Stream, position: int) -> str:
    canon = canonical_lang(stream.lang)
    if canon:
        return canon.title()
    return stream.lang.upper() if stream.lang and stream.lang.lower() not in _UNDETERMINED else f"Track {position}"


# ---------------------------------------------------------------------------
# ffmpeg command planning
# ---------------------------------------------------------------------------


@dataclass
class Plan:
    args: List[str]
    kept_audio: List[Stream] = field(default_factory=list)
    kept_subtitles: List[Stream] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def _stream_title(template: str, stream: Stream, position: int) -> str:
    return template.replace("{lang}", lang_label(stream, position))


def build_plan(info: MediaInfo, settings: Dict[str, Any], ext: str) -> Optional[Plan]:
    """Work out the ffmpeg options for a file.  ``None`` means "nothing to do"."""
    if not settings.get("metadata_on"):
        return None
    ext = (ext or "").lower()
    meta = settings.get("meta") or {}
    keep_audio = list(settings.get("keep_audio") or [])
    keep_subs = list(settings.get("keep_subtitle") or [])

    if ext in AUDIO_TAG_EXTS:
        args = ["-map", "0", "-c", "copy", "-map_metadata", "0"]
        if meta.get("title"):
            args += ["-metadata", f"title={meta['title']}"]
        artist = meta.get("artist") or meta.get("author")
        if artist:
            args += ["-metadata", f"artist={artist}"]
        return Plan(args) if len(args) > 6 else None

    if ext not in VIDEO_CONTAINERS:
        return None

    audio, subs = info.of("audio"), info.of("subtitle")
    warnings: List[str] = []

    if keep_audio:
        sel_a = resolve_tokens(audio, keep_audio)
        if not sel_a and audio:
            warnings.append("No audio track matched your Keep Audio list, so the original audio was kept.")
            sel_a = list(audio)
    else:
        sel_a = list(audio)
    sel_s = resolve_tokens(subs, keep_subs) if keep_subs else list(subs)

    filtering = bool(keep_audio or keep_subs)
    has_edits = filtering or any(meta.get(k) for k in ("title", "author", "artist", "audio", "subtitle", "video"))
    if not has_edits:
        return None

    out_video = info.of("video")
    maps = out_video + sel_a + sel_s + info.of("attachment")
    if not filtering:
        maps += info.of("data")

    args: List[str] = ["-map_metadata", "0", "-map_chapters", "0"]
    for s in maps:
        args += ["-map", f"0:{s.index}"]
    args += ["-c", "copy"]

    for key in ("title", "author", "artist"):
        if meta.get(key):
            args += ["-metadata", f"{key}={meta[key]}"]
    if meta.get("video"):
        for j, s in enumerate(out_video):
            if not s.attached_pic:
                args += [f"-metadata:s:v:{j}", f"title={meta['video']}"]
    if meta.get("audio"):
        for j, s in enumerate(sel_a):
            args += [f"-metadata:s:a:{j}", f"title={_stream_title(meta['audio'], s, j + 1)}"]
    if meta.get("subtitle"):
        for j, s in enumerate(sel_s):
            args += [f"-metadata:s:s:{j}", f"title={_stream_title(meta['subtitle'], s, j + 1)}"]

    if keep_audio and sel_a:
        for j in range(len(sel_a)):
            args += [f"-disposition:a:{j}", "+default" if j == 0 else "-default"]
    if keep_subs and sel_s:
        for j in range(len(sel_s)):
            args += [f"-disposition:s:{j}", "+default" if j == 0 else "-default"]

    return Plan(args, kept_audio=sel_a, kept_subtitles=sel_s, warnings=warnings)


async def apply_plan(src: Path, dst: Path, plan: Plan, cancel: Optional[asyncio.Event] = None) -> None:
    cmd = [
        "ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
        "-i", str(src), *plan.args, "-max_muxing_queue_size", "9999", str(dst),
    ]
    code, _out, err = await _run(cmd, cancel)
    if code != 0 or not dst.exists() or dst.stat().st_size == 0:
        raise MediaError("ffmpeg failed: " + err.decode("utf-8", "replace").strip()[-500:])


# ---------------------------------------------------------------------------
# thumbnails
# ---------------------------------------------------------------------------


async def make_video_thumbnail(src: Path, dst: Path, duration: float, cancel: Optional[asyncio.Event] = None) -> bool:
    """Grab a frame (~10% in) as a small JPEG.  Failure is non-fatal."""
    seek = 1.0 if duration <= 0 else min(max(duration * 0.1, 0.0), 60.0)
    cmd = [
        "ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-ss", f"{seek:.2f}", "-i", str(src),
        "-frames:v", "1", "-vf", "scale='min(320,iw)':-2", "-q:v", "4", str(dst),
    ]
    try:
        code, _o, _e = await _run(cmd, cancel)
    except MediaError:
        return False
    return code == 0 and dst.exists() and dst.stat().st_size > 0


def frame_seeks(duration: float) -> List[float]:
    """Positions (seconds) to try for a picture, best first: ~10% in, then earlier ones for a file that is cut off."""
    first = 1.0 if duration <= 0 else min(max(duration * 0.1, 0.0), 60.0)
    return [round(first, 2)] + [seek for seek in (20.0, 5.0, 0.0) if seek < first]


async def extract_frame(
    src: Path, dst: Path, seeks: Sequence[float], width: int = 1280, cancel: Optional[asyncio.Event] = None
) -> bool:
    """Save one frame of ``src`` as a JPEG (at most ``width`` px wide), trying each seek position until one gives a picture."""
    for seek in seeks:
        dst.unlink(missing_ok=True)
        cmd = [
            "ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-ss", f"{seek:.2f}", "-i", str(src),
            "-frames:v", "1", "-vf", f"scale='min({width},iw)':-2", "-q:v", "3", str(dst),
        ]
        try:
            code, _o, _e = await _run(cmd, cancel)
        except MediaError:
            return False
        if code == 0 and dst.exists() and dst.stat().st_size > 0:
            return True
    return False


def _flatten(img: Any) -> Any:
    """``img`` as RGB; transparent parts become white."""
    from PIL import Image

    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        flat = Image.new("RGB", rgba.size, (255, 255, 255))
        flat.paste(rgba, mask=rgba.getchannel("A"))
        return flat
    return img.convert("RGB")


def fit_photo(data: bytes, box: int = 1280) -> bytes:
    """A picture as a JPEG that fits ``box`` x ``box`` (a smaller one is not enlarged): what Telegram takes as a photo."""
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(data)) as img:
        img = _flatten(ImageOps.exif_transpose(img))
        img.thumbnail((box, box), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=90, optimize=True)
        return buf.getvalue()


RESIZE_MAX_PIXELS = 80_000_000  # a picture with more pixels than this is not resized (it would take gigabytes of memory)
_PNG_FRIENDLY_MODES = ("L", "LA", "RGB", "RGBA")  # the rest (palettes, CMYK ...) is converted so it resizes smoothly and saves as PNG


class ImageTooBig(ValueError):
    """A picture has more than ``RESIZE_MAX_PIXELS`` pixels."""


def image_size(data: bytes) -> Tuple[int, int]:
    """``(width, height)`` of a picture, the way it is meant to be seen (a photo stored sideways is counted as turned upright)."""
    from PIL import Image

    with Image.open(io.BytesIO(data)) as img:
        width, height = img.size
        if img.getexif().get(0x0112, 1) in (5, 6, 7, 8):  # EXIF orientation: turned a quarter
            width, height = height, width
        return width, height


def resize_exact(data: bytes, width: int, height: int) -> bytes:
    """The picture scaled to exactly ``width`` x ``height`` pixels, whatever its shape, as an optimised PNG."""
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(data)) as img:
        if img.width * img.height > RESIZE_MAX_PIXELS:
            raise ImageTooBig(f"{img.width}x{img.height} pixels")
        img = ImageOps.exif_transpose(img)
        if img.mode not in _PNG_FRIENDLY_MODES:
            img = img.convert("RGBA" if "A" in img.getbands() or "transparency" in img.info else "RGB")
        out = img.resize((width, height))
        buf = io.BytesIO()
        out.save(buf, format="PNG", optimize=True)
        return buf.getvalue()


def prepare_thumb(data: bytes) -> bytes:
    """Resize/re-encode an image to Telegram's thumbnail limits (<=320px, <=200 KB JPEG)."""
    from PIL import Image

    with Image.open(io.BytesIO(data)) as img:
        img = img.convert("RGB")
        img.thumbnail((320, 320))
        for quality in (88, 80, 70, 60, 50, 40):
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=quality, optimize=True)
            if buf.tell() <= 200 * 1024:
                break
        return buf.getvalue()


# ---------------------------------------------------------------------------
# /tracks output
# ---------------------------------------------------------------------------


def format_tracks(info: MediaInfo) -> str:
    lines: List[str] = []
    vids = info.video
    if vids:
        v = vids[0]
        res = f" {v.width}x{v.height}" if v.width and v.height else ""
        lines.append(f"🎞 <b>Video</b>: {esc(v.codec)}{res}")
    for label, icon, streams in (("Audio", "🔊", info.audio), ("Subtitles", "💬", info.subtitles)):
        lines.append("")
        if not streams:
            lines.append(f"{icon} <b>{label}</b>: none")
            continue
        lines.append(f"{icon} <b>{label}</b> (use these numbers)")
        for pos, s in enumerate(streams, start=1):
            bits = [lang_label(s, pos)]
            if s.lang and s.lang.lower() not in _UNDETERMINED:
                bits[0] += f" ({esc(s.lang)})"
            if s.codec:
                bits.append(esc(s.codec))
            if s.kind == "audio" and s.channels:
                bits.append(f"{s.channels}ch")
            if s.title:
                bits.append(f"“{esc(s.title)}”")
            if s.default:
                bits.append("default")
            if s.forced:
                bits.append("forced")
            lines.append(f"{pos}. " + " · ".join(bits))
    return "\n".join(lines).strip()
