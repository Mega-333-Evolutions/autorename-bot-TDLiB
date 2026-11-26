"""/sequence + /sort: put files in episode order and report which episodes are missing or repeated."""
from __future__ import annotations

import asyncio
import bisect
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from .parser import parse_caption, parse_info, split_ext
from .utils import clip, esc

MAX_LISTED = 12  # names shown per section of the report
NOTE_BURST = 5.0  # seconds: a text sent this close before the first file, or after the last one, was sent along with the files


@dataclass
class SeqItem:
    msg_id: int
    name: str
    caption: str = ""
    season: Optional[int] = None
    episode: Optional[int] = None
    quality: Optional[int] = None  # picture height: 480, 720, 1080, 1440 (2K), 2160 (4K)
    source_id: int = 0  # the message the file was renamed from, in the chat with the user (0: not known): where it was among the texts sent
    sent_at: float = 0.0  # when that message was sent (seconds since 1970; 0: not known)


@dataclass
class Note:
    """A text message the user sent (no file, no command): the sort can put it among the files, where it was sent."""

    id: int  # the message in the chat with the user
    text: str
    entities: List[Any] = field(default_factory=list)  # its formatting, as Telegram delivered it
    date: float = 0.0  # when it was sent (seconds since 1970)


@dataclass
class SequenceState:
    """What /sequence has collected so far for one user."""

    items: List[SeqItem] = field(default_factory=list)
    status_id: Optional[int] = None  # the "N files collected" message, replaced after each burst of files
    task: Optional["asyncio.Future[None]"] = None  # the debounced status update that is waiting for the burst to end
    version: int = 0  # bumped for every file, so the status task can tell whether more are still arriving
    closed: bool = False  # /sort or /cancel ended the collection
    full_noted: bool = False  # the "limit reached" notice was shown


@dataclass
class SortBatch:
    """The files one user had renamed into one chat since the last answer to "sort these now?" (what /sequence collects by hand)."""

    home: int  # the user's chat with the bot: the question and the report go there
    items: List[SeqItem] = field(default_factory=list)
    token: int = 0  # the number of the question that waits for an answer (its buttons carry it); 0: none
    prompt_id: Optional[int] = None  # that question's message
    version: int = 0  # bumped by whatever makes a question that is just being sent out of date
    texts: List[Note] = field(default_factory=list)  # the text messages the open question asks about including (after Yes to sorting)


def detect(name: str, caption: str, mode: str) -> Tuple[Optional[int], Optional[int]]:
    """``(season, episode)`` read from the file name and the caption, the one the rename mode names first.

    The season is 1 when only an episode number is found; both are ``None`` when there is no episode number.
    """
    from_name = lambda: parse_info(split_ext(name)[0])  # noqa: E731
    from_caption = lambda: parse_caption(caption)  # noqa: E731
    for read in ((from_caption, from_name) if mode == "caption" else (from_name, from_caption)):
        info = read()
        if info.episode.isdigit():
            return (int(info.season) if info.season.isdigit() else 1), int(info.episode)
    return None, None


_TWO_K = re.compile(r"(?<![A-Za-z0-9])2k(?![A-Za-z0-9])", re.I)  # another name for 1440p, which the parser does not know (it knows 4K)


def detect_quality(name: str, caption: str, mode: str) -> Optional[int]:
    """The picture height read from the file name and the caption, the one the rename mode names first: ``1080`` for 1080p,
    ``1440`` for 2K, ``2160`` for 4K.  ``None`` when neither says."""
    sources = [(split_ext(name)[0], parse_info), (caption, parse_caption)]
    if mode == "caption":
        sources.reverse()
    for text, read in sources:
        quality = read(text).quality
        if quality:
            return int(quality[:-1])
        if _TWO_K.search(text):
            return 1440
    return None


@dataclass
class Analysis:
    ordered: List[SeqItem]  # numbered files by season / episode / quality, then the others in the order they were sent
    unknown: List[SeqItem]  # no episode number found
    missing: Dict[int, List[int]]  # season -> episode numbers absent between its first and last
    repeated: Dict[Tuple[int, int], List[SeqItem]]  # (season, episode) -> the files of it that have the same quality as another
    first: Optional[Tuple[int, int]] = None
    last: Optional[Tuple[int, int]] = None


def analyse(items: Sequence[SeqItem], mode: str) -> Analysis:
    for item in items:
        item.season, item.episode = detect(item.name, item.caption, mode)
        item.quality = detect_quality(item.name, item.caption, mode)
    # one episode in several seasons: season 1 first; in several qualities: the smallest picture first, one without a quality last
    numbered = sorted(
        (i for i in items if i.episode is not None),
        key=lambda i: (i.season, i.episode, i.quality is None, i.quality or 0, i.msg_id),
    )
    unknown = sorted((i for i in items if i.episode is None), key=lambda i: i.msg_id)

    by_season: Dict[int, List[int]] = {}
    groups: Dict[Tuple[int, int], List[SeqItem]] = {}
    for item in numbered:
        by_season.setdefault(item.season, []).append(item.episode)
        groups.setdefault((item.season, item.episode), []).append(item)
    missing: Dict[int, List[int]] = {}
    for season, episodes in by_season.items():
        have = set(episodes)
        gaps = [n for n in range(min(have), max(have) + 1) if n not in have]
        if gaps:
            missing[season] = gaps
    repeated: Dict[Tuple[int, int], List[SeqItem]] = {}
    for key, files in groups.items():
        count: Dict[Optional[int], int] = {}
        for f in files:
            count[f.quality] = count.get(f.quality, 0) + 1
        shared = [f for f in files if count[f.quality] > 1]  # the same episode in another quality is a version of it, not a repeat
        if shared:
            repeated[key] = shared
    return Analysis(
        ordered=numbered + unknown,
        unknown=unknown,
        missing=missing,
        repeated=repeated,
        first=(numbered[0].season, numbered[0].episode) if numbered else None,
        last=(numbered[-1].season, numbered[-1].episode) if numbered else None,
    )


def notes_for(items: Sequence[SeqItem], notes: Sequence[Note]) -> List[Note]:
    """The text messages that go with these files, oldest first: the ones sent between the first and the last of the files (in the order
    the files were sent in), and the ones sent in the same moment just before the first or just after the last (a heading forwarded
    together with the episodes)."""
    sent = sorted((i for i in items if i.source_id), key=lambda i: i.source_id)
    if not sent:
        return []
    first, last = sent[0], sent[-1]
    chosen: List[Note] = []
    for note in sorted(notes, key=lambda n: n.id):
        if first.source_id < note.id < last.source_id:
            chosen.append(note)
        elif note.id < first.source_id and first.sent_at and note.date >= first.sent_at - NOTE_BURST:
            chosen.append(note)
        elif note.id > last.source_id and last.sent_at and note.date <= last.sent_at + NOTE_BURST:
            chosen.append(note)
    return chosen


def place_notes(ordered: Sequence[SeqItem], notes: Sequence[Note]) -> List[Union[SeqItem, Note]]:
    """``ordered`` (the files in their new order) with the text messages put in: each one right after the file that was sent before it,
    so it stays between the same two episodes; one sent before every file comes first, one sent after every file last."""
    by_sent = sorted((i for i in ordered if i.source_id), key=lambda i: i.source_id)
    ids = [i.source_id for i in by_sent]
    lead: List[Note] = []
    tail: List[Note] = []
    after: Dict[int, List[Note]] = {}
    for note in sorted(notes, key=lambda n: n.id):
        before = bisect.bisect_left(ids, note.id)  # how many of the files were sent before this text
        if before == 0:
            lead.append(note)
        elif before == len(ids):
            tail.append(note)
        else:
            after.setdefault(by_sent[before - 1].msg_id, []).append(note)
    placed: List[Union[SeqItem, Note]] = list(lead)
    for item in ordered:
        placed.append(item)
        placed.extend(after.get(item.msg_id, ()))
    placed.extend(tail)
    return placed


def _ep(n: int) -> str:
    return f"E{n:02d}"


def ranges(numbers: Sequence[int], limit: int = 0) -> str:
    """``[3, 7, 8, 9]`` -> ``E03, E07–E09``; with ``limit`` the text is cut after that many characters."""
    spans: List[Tuple[int, int]] = []
    for n in sorted(set(numbers)):
        if spans and n == spans[-1][1] + 1:
            spans[-1] = (spans[-1][0], n)
        else:
            spans.append((n, n))
    texts = [_ep(a) if a == b else f"{_ep(a)}–{_ep(b)}" for a, b in spans]
    if limit and len(", ".join(texts)) > limit:
        kept: List[str] = []
        used = 0
        for i, text in enumerate(texts):
            if used + len(text) + 2 > limit - 24:
                kept.append(f"… and {sum(b - a + 1 for a, b in spans[i:])} more")
                break
            kept.append(text)
            used += len(text) + 2
        return ", ".join(kept)
    return ", ".join(texts)


def _label(season: int, episode: int, multi: bool) -> str:
    return f"S{season:02d}{_ep(episode)}" if multi else _ep(episode)


def _bullets(lines: Sequence[str]) -> List[str]:
    out = [f"• {line}" for line in lines[:MAX_LISTED]]
    if len(lines) > MAX_LISTED:
        out.append(f"… and {len(lines) - MAX_LISTED} more")
    return out


def report_sections(
    a: Analysis, total: int, sent: int, failed: Sequence[SeqItem] = (), note: str = "", texts: Optional[Tuple[int, int]] = None,
) -> List[str]:
    """The pieces of the message shown after the files were sent (HTML), each short enough on its own.  ``texts``: how many of the text
    messages that were to be put in were sent, and how many there were (``None``: none were to be)."""
    seasons = {i.season for i in a.ordered if i.season is not None}
    multi = len(seasons) > 1
    head = f"✅ <b>Sorted {sent} of {total} files</b>" if sent != total else f"✅ <b>Sorted {total} file{'s' if total != 1 else ''}</b>"
    if a.first and a.last:
        head += f" · {_label(*a.first, multi)} → {_label(*a.last, multi)}"
    if texts and texts[1]:
        placed, asked = texts
        head += f" · 📝 {placed} of {asked} text messages" if placed != asked else f" · 📝 {asked} text message{'s' if asked != 1 else ''}"
    parts = [head]
    if note:
        parts.append(note)

    if a.missing:
        lines = []
        for season in sorted(a.missing)[:MAX_LISTED]:
            text = ranges(a.missing[season], limit=300)
            lines.append(f"S{season:02d}: {text}" if multi else text)
        if len(a.missing) > MAX_LISTED:
            lines.append(f"… and {len(a.missing) - MAX_LISTED} more seasons")
        parts.append("⚠️ <b>Missing episodes</b>\n" + "\n".join(lines))
    if a.repeated:
        lines = []
        for (season, episode), files in sorted(a.repeated.items()):
            names = ", ".join(f"<code>{esc(clip(f.name, 40))}</code>" for f in files[:3])
            more = f" +{len(files) - 3} more" if len(files) > 3 else ""
            lines.append(f"{_label(season, episode, multi)} ×{len(files)}: {names}{more}")
        parts.append("🔁 <b>Repeated episodes</b>\n" + "\n".join(_bullets(lines)))
    if a.unknown:
        lines = [f"<code>{esc(clip(i.name, 50))}</code>" for i in a.unknown]
        parts.append("❓ <b>No episode number found</b> (sent last)\n" + "\n".join(_bullets(lines)))
    if failed:
        lines = [f"<code>{esc(clip(i.name, 50))}</code>" for i in failed]
        parts.append("🚫 <b>Couldn't be sent</b>\n" + "\n".join(_bullets(lines)))
    if a.first and not (a.missing or a.repeated):
        parts.append("No missing or repeated episodes.")
    return parts


def format_report(
    a: Analysis, total: int, sent: int, failed: Sequence[SeqItem] = (), note: str = "", limit: int = 3600,
    texts: Optional[Tuple[int, int]] = None,
) -> List[str]:
    """The report as one message, or a few when the lists are long (Telegram allows about 4000 characters)."""
    messages: List[str] = []
    current = ""
    for part in report_sections(a, total, sent, failed, note, texts):
        if current and len(current) + 2 + len(part) > limit:
            messages.append(current)
            current = part
        else:
            current = f"{current}\n\n{part}" if current else part
    messages.append(current)
    return messages
