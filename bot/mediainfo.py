"""/mediainfo: everything we can tell about a file, laid out like MediaInfo and ready for a Telegraph page.

MediaInfo (the ``pymediainfo`` package) is used when it is available, because it reports the most.  Without it the
report is built from ffprobe, which is always there because the renamer needs it.
"""
from __future__ import annotations

import asyncio
import logging
import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import media
from .errors import MediaError
from .telegraph import CONTENT_LIMIT, Node, content_size
from .utils import clip

log = logging.getLogger(__name__)

WRAP = 64  # columns of the monospace blocks on the page (Telegraph does not wrap them)
LABEL_ALIGN = 26  # labels longer than this are not used to line up the values
MIN_VALUE = 40  # columns a wrapped value always gets, even next to a long label
STREAM_KINDS = {"Video", "Audio", "Text", "Image", "Other"}
# lines MediaInfo adds when it is given only the start of a file; they describe our download, not the file
PARTIAL_ARTIFACTS = {"CRC_Error_Pos", "IsTruncated"}
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


@dataclass
class Section:
    title: str
    lines: List[Tuple[str, str]] = field(default_factory=list)  # (label, value); an empty label is a plain line

    @property
    def kind(self) -> str:
        return self.title.split(" #")[0]


@dataclass
class Report:
    sections: List[Section]
    engine: str  # "MediaInfo", "ffprobe" or "" when nothing could read the file
    truncated: bool = False  # only the start of the file was read
    notes: List[str] = field(default_factory=list)  # shown under the sections

    @property
    def has_streams(self) -> bool:
        return any(s.kind in STREAM_KINDS for s in self.sections)

    def section(self, title: str) -> Optional[Section]:
        return next((s for s in self.sections if s.title == title), None)

    def summary(self) -> str:
        """``Matroska · 24 min 1 s · 1 video · 2 audio · 3 subtitle``"""
        parts: List[str] = []
        general = self.section("General")
        for label in ("Format", "Duration"):
            value = next((v for lab, v in general.lines if lab == label), "") if general is not None else ""
            if value:
                parts.append(value)
        for kind, word in (("Video", "video"), ("Audio", "audio"), ("Text", "subtitle")):
            count = sum(1 for s in self.sections if s.kind == kind)
            if count:
                parts.append(f"{count} {word}")
        return " · ".join(parts)


# ---------------------------------------------------------------------------
# MediaInfo's number formats
# ---------------------------------------------------------------------------


def mi_size(num: float) -> str:
    """``71.0 KiB``, ``1.46 GiB``: three significant digits, binary units."""
    value = float(num)
    unit = "Bytes"
    for unit in ("Bytes", "KiB", "MiB", "GiB", "TiB"):
        if value < 1023.5 or unit == "TiB":
            break
        value /= 1024
    if unit == "Bytes":
        return f"{int(round(value))} Bytes"
    digits = 0 if value >= 99.95 else 1 if value >= 9.995 else 2
    return f"{value:.{digits}f} {unit}"


def mi_rate(bits_per_second: float) -> str:
    """``62 b/s``, ``370 kb/s`` below 10 Mb/s, ``12.3 Mb/s`` above."""
    if bits_per_second < 1000:
        return f"{int(round(bits_per_second))} b/s"
    kbps = bits_per_second / 1000
    if kbps < 10000:
        return f"{kbps:,.0f}".replace(",", " ") + " kb/s"
    mbps = kbps / 1000
    return f"{mbps:.0f} Mb/s" if mbps >= 100 else f"{mbps:.1f} Mb/s"


def mi_duration(milliseconds: float) -> str:
    """``1 h 24 min``, ``24 min 1 s``, ``2 s 23 ms``."""
    ms = int(round(milliseconds))
    hours, rest = divmod(ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1000)
    if hours:
        return f"{hours} h {minutes} min"
    if minutes:
        return f"{minutes} min {seconds} s"
    return f"{seconds} s {millis} ms"


def clean(value: Any) -> str:
    return _CONTROL_RE.sub("", str(value)).strip()


# ---------------------------------------------------------------------------
# reading MediaInfo's text
# ---------------------------------------------------------------------------


def parse_text(text: str) -> List[Section]:
    """Split MediaInfo's text output into sections of ``label : value`` lines (nested lines keep their indent)."""
    sections: List[Section] = []
    current: Optional[Section] = None
    for raw in text.splitlines():
        line = _CONTROL_RE.sub("", raw).rstrip()
        if not line.strip():
            current = None
            continue
        if current is None:
            current = Section(title=line.strip())
            sections.append(current)
            continue
        label, sep, value = line.partition(" : ")
        if sep:
            current.lines.append((label.rstrip(), value.strip()))
        elif line.endswith(" :"):
            current.lines.append((line[:-2].rstrip(), ""))
        else:
            current.lines.append(("", line.strip()))
    return sections


def _set(section: Section, label: str, value: str, *, first: bool = False) -> None:
    for i, (existing, _old) in enumerate(section.lines):
        if existing == label:
            section.lines[i] = (label, value)
            return
    section.lines.insert(0 if first else len(section.lines), (label, value))


def _drop(section: Section, label: str) -> None:
    section.lines = [(lab, val) for lab, val in section.lines if lab != label]


def _drop_partial_artifacts(section: Section) -> None:
    """Remove the conformance errors and the like that MediaInfo reports about a file that was cut short on purpose."""
    kept: List[Tuple[str, str]] = []
    skipping = False
    for label, value in section.lines:
        if skipping and label[:1].isspace():  # a line nested under the one being dropped
            continue
        skipping = False
        bare = label.strip()
        if bare.startswith("Conformance"):
            skipping = True
            continue
        if bare in PARTIAL_ARTIFACTS:
            continue
        kept.append((label, value))
    section.lines = kept


def fix_general(report: Report, name: str, true_size: int, duration_ms: Optional[float]) -> None:
    """Show the file's own name instead of the temporary path, and its real size when only the start was read."""
    general = report.section("General")
    if general is None:
        general = Section("General")
        report.sections.insert(0, general)
    _set(general, "Complete name", name, first=True)
    if true_size:
        _set(general, "File size", mi_size(true_size))
    if report.truncated:
        _drop_partial_artifacts(general)
        if true_size and duration_ms:
            _set(general, "Overall bit rate", mi_rate(true_size * 8 / (duration_ms / 1000)))
        else:
            _drop(general, "Overall bit rate")  # it was worked out from the part we have, so it would be wrong


def telegram_section(name: str, size: int, *, mime: str = "", sent_as: str = "", sent_on: str = "", dc: str = "", caption: str = "") -> Section:
    """What Telegram itself says about the file."""
    lines = [("File name", name)]
    if size:
        lines.append(("File size", f"{mi_size(size)} ({size:,} bytes)".replace(",", " ")))
    for label, value in (("MIME type", mime), ("Sent as", sent_as), ("Sent on", sent_on), ("Data center", dc), ("Caption", caption)):
        if value:
            lines.append((label, value))
    return Section("Telegram", lines)


# ---------------------------------------------------------------------------
# the ffprobe fallback
# ---------------------------------------------------------------------------

_TAG_LABELS = {
    "title": "Title", "encoder": "Writing application", "creation_time": "Encoded date", "artist": "Performer",
    "album": "Album", "date": "Recorded date", "comment": "Comment", "genre": "Genre", "copyright": "Copyright",
}


def _language(code: str) -> str:
    name = media.canonical_lang(code)
    return name.title() if name else code.strip()


def _fraction(text: Any) -> float:
    try:
        num, _sep, den = str(text).partition("/")
        return float(num) / float(den) if den else float(num)
    except (ValueError, ZeroDivisionError):
        return 0.0


def _seconds(*candidates: Any) -> float:
    """The first usable duration among ``candidates``: plain seconds or ``HH:MM:SS.fff``."""
    for cand in candidates:
        if not cand:
            continue
        text = str(cand)
        try:
            value = media._parse_hms(text) if ":" in text else float(text)
        except ValueError:
            continue
        if value > 0:
            return value
    return 0.0


def _add(section: Section, label: str, value: Any) -> None:
    text = clean(value) if value not in (None, "") else ""
    if text:
        section.lines.append((label, text))


def _flag(value: Any) -> str:
    return "Yes" if int(value or 0) else "No"


def _level(stream: Dict[str, Any]) -> str:
    try:
        level = int(stream.get("level"))
    except (TypeError, ValueError):
        return ""
    if level <= 0:
        return ""
    codec = stream.get("codec_name")
    if codec == "h264":
        return f"L{level / 10:g}"
    if codec == "hevc":
        return f"L{level / 30:g}"
    return ""


def _stream_section(stream: Dict[str, Any], title: str) -> Section:
    sec = Section(title)
    kind = stream.get("codec_type")
    tags = {k.lower(): v for k, v in (stream.get("tags") or {}).items()}
    disp = stream.get("disposition") or {}
    _add(sec, "ID", int(stream.get("index", 0)) + 1)
    _add(sec, "Format", stream.get("codec_long_name") or stream.get("codec_name"))
    if kind == "video":
        _add(sec, "Format profile", "@".join(p for p in (stream.get("profile"), _level(stream)) if p))
    else:
        _add(sec, "Format profile", stream.get("profile"))
    tag = stream.get("codec_tag_string")
    if tag and not tag.startswith("[0]"):
        _add(sec, "Codec ID", tag)
    seconds = _seconds(stream.get("duration"), tags.get("duration"))
    if seconds:
        _add(sec, "Duration", mi_duration(seconds * 1000))
    try:
        rate = float(stream.get("bit_rate") or tags.get("bps") or tags.get("bps-eng") or 0)
    except ValueError:
        rate = 0.0
    if rate:
        _add(sec, "Bit rate", mi_rate(rate))
    if kind == "video":
        if stream.get("width") and stream.get("height"):
            _add(sec, "Width", f"{stream['width']} pixels")
            _add(sec, "Height", f"{stream['height']} pixels")
        _add(sec, "Display aspect ratio", stream.get("display_aspect_ratio"))
        fps = _fraction(stream.get("avg_frame_rate") or "0") or _fraction(stream.get("r_frame_rate") or "0")
        if fps:
            _add(sec, "Frame rate", f"{fps:.3f} FPS")
        _add(sec, "Pixel format", stream.get("pix_fmt"))
        if stream.get("bits_per_raw_sample"):
            _add(sec, "Bit depth", f"{stream['bits_per_raw_sample']} bits")
        for label, key in (("Color space", "color_space"), ("Color range", "color_range"), ("Transfer characteristics", "color_transfer"),
                           ("Color primaries", "color_primaries"), ("Scan type", "field_order")):
            _add(sec, label, stream.get(key))
    elif kind == "audio":
        channels = stream.get("channels")
        if channels:
            _add(sec, "Channel(s)", f"{channels} channel" + ("s" if channels != 1 else ""))
        _add(sec, "Channel layout", stream.get("channel_layout"))
        try:
            if stream.get("sample_rate"):
                _add(sec, "Sampling rate", f"{float(stream['sample_rate']) / 1000:g} kHz")
        except ValueError:
            pass
        if stream.get("bits_per_raw_sample"):
            _add(sec, "Bit depth", f"{stream['bits_per_raw_sample']} bits")
    _add(sec, "Title", tags.get("title"))
    if tags.get("language") not in (None, "", "und"):
        _add(sec, "Language", _language(str(tags["language"])))
    sec.lines.append(("Default", _flag(disp.get("default"))))
    sec.lines.append(("Forced", _flag(disp.get("forced"))))
    return sec


def ffprobe_sections(data: Dict[str, Any], name: str, true_size: int) -> List[Section]:
    """MediaInfo-style sections from ffprobe's JSON (``-show_format -show_streams -show_chapters``)."""
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    general = Section("General")
    _add(general, "Complete name", name)
    _add(general, "Format", fmt.get("format_long_name") or fmt.get("format_name"))
    if true_size:
        _add(general, "File size", mi_size(true_size))
    seconds = _seconds(fmt.get("duration"))
    if seconds:
        _add(general, "Duration", mi_duration(seconds * 1000))
        if true_size:
            _add(general, "Overall bit rate", mi_rate(true_size * 8 / seconds))
    for key, value in (fmt.get("tags") or {}).items():
        _add(general, _TAG_LABELS.get(key.lower(), key.replace("_", " ").title()), value)
    attached = [
        (s.get("tags") or {}).get("filename") or (s.get("tags") or {}).get("FILENAME")
        for s in streams if s.get("codec_type") == "attachment"
    ]
    _add(general, "Attachments", " / ".join(a for a in attached if a))
    sections = [general]

    titles = {"video": "Video", "audio": "Audio", "subtitle": "Text"}
    counts = {kind: sum(1 for s in streams if s.get("codec_type") == kind) for kind in titles}
    seen: Dict[str, int] = {}
    for stream in streams:
        kind = stream.get("codec_type")
        if kind not in titles:
            continue
        seen[kind] = seen.get(kind, 0) + 1
        title = titles[kind] if counts[kind] == 1 else f"{titles[kind]} #{seen[kind]}"
        sections.append(_stream_section(stream, title))

    chapters = data.get("chapters") or []
    if chapters:
        menu = Section("Menu")
        for chapter in chapters:
            millis = int(_seconds(chapter.get("start_time")) * 1000)
            hours, rest = divmod(millis, 3_600_000)
            minutes, rest = divmod(rest, 60_000)
            stamp = f"{hours:02d}:{minutes:02d}:{rest // 1000:02d}.{rest % 1000:03d}"
            menu.lines.append((stamp, clean((chapter.get("tags") or {}).get("title", ""))))
        sections.append(menu)
    return sections


# ---------------------------------------------------------------------------
# reading a file
# ---------------------------------------------------------------------------


def _mediainfo(path: Path) -> Optional[Tuple[str, Optional[float]]]:
    """Blocking: MediaInfo's text and the duration (ms) of the file, or None when MediaInfo is not usable."""
    try:
        from pymediainfo import MediaInfo
    except ImportError:
        return None
    if not MediaInfo.can_parse():
        return None
    text = MediaInfo.parse(str(path), output="text", full=False)
    duration: Optional[float] = None
    try:
        first = MediaInfo.parse(str(path)).tracks[0]
        duration = float(first.duration) if first.duration else None
    except (IndexError, TypeError, ValueError, AttributeError):
        duration = None
    return str(text), duration


async def read_file(path: Path, name: str, true_size: int, *, extra: Optional[Section] = None) -> Report:
    """The report for the file at ``path``, which is all of the file or only its start (``true_size`` is the real size).

    The report may have no streams (``has_streams``): a video whose index is at the end of the file, for one.
    """
    sections: List[Section] = []
    engine = ""
    duration_ms: Optional[float] = None
    try:
        got = await asyncio.to_thread(_mediainfo, path)
    except Exception as exc:  # noqa: BLE001 - the library is native code; whatever goes wrong, ffprobe is the fallback
        log.warning("MediaInfo failed on %s: %s", name, exc)
        got = None
    if got is not None:
        sections, duration_ms = parse_text(got[0]), got[1]
        engine = "MediaInfo" if sections else ""
    if not sections:
        try:
            data = await media.probe_raw(path)
        except MediaError:
            data = {}
        if data.get("format") or data.get("streams"):
            sections, engine = ffprobe_sections(data, name, true_size), "ffprobe"
            duration_ms = _seconds((data.get("format") or {}).get("duration")) * 1000 or None
    report = Report(sections=sections, engine=engine, truncated=bool(true_size and path.stat().st_size < true_size))
    fix_general(report, name, true_size, duration_ms)
    if extra is not None:
        report.sections.append(extra)
    return report


# ---------------------------------------------------------------------------
# the Telegraph page
# ---------------------------------------------------------------------------


def render_section(section: Section, width: int = WRAP, max_value: int = 0) -> str:
    """The section as aligned ``label : value`` lines, long values wrapped under the first one."""
    label_w = max((len(lab) for lab, _ in section.lines if lab and len(lab) <= LABEL_ALIGN), default=0)
    indent = " " * (label_w + 3)
    room = max(MIN_VALUE, width - len(indent))
    out: List[str] = []
    for label, value in section.lines:
        if max_value:
            value = clip(value, max_value)
        if not label:
            out.extend(textwrap.wrap(value, width=width, break_long_words=True, break_on_hyphens=False) or [""])
            continue
        head = f"{clip(label, 60):<{label_w}} : "
        parts = textwrap.wrap(value, width=room, break_long_words=True, break_on_hyphens=False) or [""]
        out.append(head + parts[0])
        out.extend(indent + part for part in parts[1:])
    return "\n".join(line.rstrip() for line in out)


def render_text(report: Report, **kw: Any) -> str:
    """The whole report as plain text (what is sent as a file when Telegraph cannot be reached)."""
    blocks = [f"{s.title}\n{render_section(s, **kw)}" for s in report.sections]
    blocks.extend(report_notes(report))
    return "\n\n".join(blocks) + "\n"


def report_notes(report: Report) -> List[str]:
    notes = list(report.notes)
    if report.truncated:
        notes.append("Only the start of the file was read; its size and overall bit rate are worked out from the real file size.")
    notes.append(f"Made by the bot with {report.engine}." if report.engine else "Made by the bot.")
    return notes


def _nodes(report: Report, max_value: int, keep: int, notes: List[str]) -> List[Node]:
    nodes: List[Node] = []
    for index, section in enumerate(report.sections):
        if index >= keep and section.title != "Telegram":
            continue
        nodes.append({"tag": "h4", "children": [section.title]})
        nodes.append({"tag": "pre", "children": [render_section(section, max_value=max_value)]})
    for note in notes:
        nodes.append({"tag": "p", "children": [{"tag": "i", "children": [note]}]})
    return nodes


def to_nodes(report: Report, limit: int = CONTENT_LIMIT) -> List[Node]:
    """Telegraph nodes for the report, shortened step by step until the page fits Telegraph's size limit."""
    notes = report_notes(report)
    total = len(report.sections)
    for max_value in (0, 300, 150, 80):
        nodes = _nodes(report, max_value, total, notes)
        if content_size(nodes) <= limit:
            return nodes
    keep = total
    while keep > 1:  # still too big (a file with hundreds of tracks): drop the last sections, keeping General and Telegram
        keep -= 1
        left_out = [f"The page size limit was reached, so {total - keep} more section(s) were left out."]
        nodes = _nodes(report, 80, keep, notes + left_out)
        if content_size(nodes) <= limit:
            return nodes
    return _nodes(report, 80, 1, notes)
