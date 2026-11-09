"""Telegram formatting <-> the HTML a caption is stored as.

People format a caption like any message: select words and make them bold, italic, a link...  Telegram delivers that
as *entities* (kind, offset, length) next to the plain text.  Captions are kept as Telegram HTML (the variables are
filled in and the HTML is sent with ``parse_mode="html"``), so ``from_entities`` turns the entities into that HTML and
nobody has to type a tag, and ``preview`` turns stored HTML back into something that is safe to show in a panel.

Entity offsets count UTF-16 code units, so the text is handled with an emoji outside the basic
plane as two "surrogate" characters while the work is going on.
"""
from __future__ import annotations

import html
import re
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Tuple

from .parser import CAPTION_VARS
from .tg import html as telegram_html
from .tg import types
from .tg.entities import add_surrogate, del_surrogate
from .utils import esc

_VARIABLE = re.compile(r"\{(?:%s)\}" % "|".join(map(re.escape, CAPTION_VARS)))

_SIMPLE = {
    types.MessageEntityBold: ("<b>", "</b>"),
    types.MessageEntityItalic: ("<i>", "</i>"),
    types.MessageEntityUnderline: ("<u>", "</u>"),
    types.MessageEntityStrike: ("<s>", "</s>"),
    types.MessageEntityCode: ("<code>", "</code>"),
}


class Span(NamedTuple):
    start: int
    end: int
    open: str
    close: str


def _tags(entity: Any) -> Optional[Tuple[str, str]]:
    """The HTML tags for one entity.  ``None`` for the ones that are not formatting the bot keeps: what Telegram finds by
    itself (links in the text, @mentions, #hashtags, /commands) comes back by itself when the caption is sent.  Spoilers and
    custom emoji are not kept either: the HTML the bot stores has no spoiler, and a bot may not send custom emoji."""
    kind = type(entity)
    if kind in _SIMPLE:
        return _SIMPLE[kind]
    if kind is types.MessageEntityTextUrl:
        return f'<a href="{html.escape(entity.url, quote=True)}">', "</a>"
    if kind is types.MessageEntityMentionName:
        return f'<a href="tg://user?id={entity.user_id}">', "</a>"
    if kind is types.MessageEntityPre:
        language = html.escape(getattr(entity, "language", "") or "", quote=True)
        return (f'<pre><code class="language-{language}">', "</code></pre>") if language else ("<pre>", "</pre>")
    if kind is types.MessageEntityBlockquote:
        return ("<blockquote expandable>" if getattr(entity, "collapsed", False) else "<blockquote>"), "</blockquote>"
    return None


def _whole(body: str, at: int) -> int:
    """``at``, moved off the middle of a surrogate pair."""
    if 0 < at < len(body) and "\udc00" <= body[at] <= "\udfff" and "\ud800" <= body[at - 1] <= "\udbff":
        return at + 1
    return at


def _tidy(spans: Iterable[Span]) -> List[Span]:
    """Spans of one kind never overlap afterwards.  The HTML parser can't nest a tag in itself (``<b>a<b>b</b>c</b>`` loses
    its outer one), so identical spans that overlap or touch are joined, and of two links (or other tags with different
    details) that overlap, the one that starts later only gets what the first leaves."""
    groups: Dict[str, List[Span]] = {}
    for s in spans:
        groups.setdefault(re.match(r"<(\w+)", s.open).group(1), []).append(s)
    out: List[Span] = []
    for group in groups.values():
        kept: List[Span] = []
        for s in sorted(group, key=lambda s: (s.start, -s.end)):
            if kept and s.start <= kept[-1].end:
                last = kept[-1]
                if s.open == last.open:
                    kept[-1] = last._replace(end=max(last.end, s.end))
                    continue
                if s.start < last.end:
                    s = s._replace(start=last.end)
                    if s.end <= s.start:
                        continue
            kept.append(s)
        out += kept
    return out


def _render(body: str, spans: Iterable[Span]) -> str:
    """``body`` as HTML with the spans' tags in it.  Spans that overlap without one containing the other are split, so
    that the tags always nest."""
    spans = [s._replace(start=_whole(body, s.start), end=_whole(body, s.end)) for s in spans]
    spans = _tidy(s for s in spans if s.end > s.start)
    points = sorted({0, len(body)} | {p for s in spans for p in (s.start, s.end)})
    out: List[str] = []
    running: List[Span] = []  # the spans that are open right now, outermost first
    for index, at in enumerate(points):
        ending = [s for s in running if s.end == at]
        if ending:
            depth = min(running.index(s) for s in ending)
            inner, running = running[depth:], running[:depth]
            out += [s.close for s in reversed(inner)]
            for s in inner:
                if s.end != at:  # it goes on past this point: close it around the one that ended and open it again
                    out.append(s.open)
                    running.append(s)
        for s in sorted((s for s in spans if s.start == at), key=lambda s: -s.end):  # on a tie the earlier one stays outside
            out.append(s.open)
            running.append(s)
        if index + 1 < len(points):
            out.append(esc(body[at:points[index + 1]]))
    return "".join(out)


def from_entities(text: str, entities: Optional[Iterable[Any]], start: int = 0) -> Optional[str]:
    """``text[start:]`` as HTML that carries the formatting in ``entities``, or ``None`` when there is none to keep.

    ``start`` skips what comes before the caption (a command word, say).  A ``{variable}`` is never split: a format that
    covers only part of one (double-tapping the word inside the braces does that) is widened to the whole variable, because
    the variable is replaced as a whole.
    """
    if not text or not entities:
        return None
    first = len(add_surrogate(text[:start]))
    body = add_surrogate(text)[first:]
    first += len(body) - len(body.lstrip())
    body = body.strip()
    variables = [(m.start(), m.end()) for m in _VARIABLE.finditer(body)]
    spans = []
    for entity in entities:
        tags = _tags(entity)
        if tags is None:
            continue
        a, b = max(entity.offset - first, 0), min(entity.offset + entity.length - first, len(body))
        if b <= a:
            continue
        for lo, hi in variables:
            if a < hi and b > lo:
                a, b = min(a, lo), max(b, hi)
        spans.append(Span(a, b, *tags))
    return del_surrogate(_render(body, spans)) if spans else None


def preview(template: str, limit: int) -> str:
    """A stored caption template as HTML that is safe to show in a panel: its formatting is shown, its tags are balanced
    whatever was typed, and it is cut to ``limit`` characters."""
    text, entities = telegram_html.parse(template or "")
    if not text:
        return "—"
    body = add_surrogate(text)
    cut = len(body) if len(body) <= limit else _whole(body, limit - 1)
    spans = []
    for entity in entities:
        tags = _tags(entity)
        if tags is not None:
            spans.append(Span(entity.offset, min(entity.offset + entity.length, cut), *tags))
    return del_surrogate(_render(body[:cut], spans)) + ("…" if cut < len(body) else "")
