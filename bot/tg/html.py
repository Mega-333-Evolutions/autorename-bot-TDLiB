"""Telegram HTML -> text and entities.

The dialect is the one captions have always been stored in: ``<b>/<strong>``, ``<i>/<em>``, ``<u>``, ``<s>/<del>``, ``<code>``, ``<pre>``
(with ``<code class="language-x">`` inside for the language), ``<a href>``, ``<blockquote>`` (``expandable``), ``<tg-emoji>``.  Anything
else is ignored and only its text is kept.  The parser is a port of the one in Telethon (MIT licence), working with this package's
entities, so a stored caption is read exactly as before.
"""
from __future__ import annotations

from collections import deque
from html.parser import HTMLParser
from typing import List, Tuple

from . import entities as E


def strip_text(text: str, entities: List[E.MessageEntity]) -> str:
    """The text without the whitespace around it, with the entities moved along and the empty ones dropped."""
    if not entities:
        return text.strip()

    len_ori = len(text)
    text = text.lstrip()
    left_offset = len_ori - len(text)
    text = text.rstrip()
    len_final = len(text)

    for i in reversed(range(len(entities))):
        e = entities[i]
        if e.length == 0:
            del entities[i]
            continue
        if e.offset + e.length > left_offset:
            if e.offset >= left_offset:
                e.offset -= left_offset
            else:
                e.length = e.offset + e.length - left_offset
                e.offset = 0
        else:
            del entities[i]
            continue
        if e.offset + e.length <= len_final:
            continue
        if e.offset >= len_final:
            del entities[i]
        else:
            e.length = len_final - e.offset
    return text


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.text = ""
        self.entities: List[E.MessageEntity] = []
        self._building: dict = {}
        self._open_tags: deque = deque()
        self._open_tags_meta: deque = deque()

    def handle_starttag(self, tag, attrs):
        self._open_tags.appendleft(tag)
        self._open_tags_meta.appendleft(None)
        attrs = dict(attrs)
        kind = None
        args: dict = {}
        if tag in ("strong", "b"):
            kind = E.MessageEntityBold
        elif tag in ("em", "i"):
            kind = E.MessageEntityItalic
        elif tag == "u":
            kind = E.MessageEntityUnderline
        elif tag in ("del", "s"):
            kind = E.MessageEntityStrike
        elif tag == "blockquote":
            kind = E.MessageEntityBlockquote
            if "expandable" in attrs:
                args["collapsed"] = True
        elif tag == "code":
            pre = self._building.get("pre")
            if pre is None:
                kind = E.MessageEntityCode
            elif "class" in attrs:  # <pre><code class="language-x">: the language of the block
                pre.language = attrs["class"][len("language-"):]
        elif tag == "pre":
            kind = E.MessageEntityPre
            args["language"] = ""
        elif tag == "a":
            url = attrs.get("href")
            if url is None:
                return
            if url.startswith("mailto:"):
                url = url[len("mailto:"):]
                kind = E.MessageEntityEmail
            elif self.get_starttag_text() == url:
                kind = E.MessageEntityUrl
            else:
                kind = E.MessageEntityTextUrl
                args["url"] = E.del_surrogate(url)
                url = None
            self._open_tags_meta.popleft()
            self._open_tags_meta.appendleft(url)
        elif tag == "tg-emoji":
            try:
                emoji_id = int(attrs["emoji-id"])
            except (KeyError, ValueError):
                return
            kind = E.MessageEntityCustomEmoji
            args["document_id"] = emoji_id
        if kind and tag not in self._building:
            self._building[tag] = kind(offset=len(self.text), length=0, **args)

    def handle_data(self, text):
        if self._open_tags and self._open_tags[0] == "a":
            url = self._open_tags_meta[0]
            if url:
                text = url
        for entity in self._building.values():
            entity.length += len(text)
        self.text += text

    def handle_endtag(self, tag):
        try:
            self._open_tags.popleft()
            self._open_tags_meta.popleft()
        except IndexError:
            pass
        entity = self._building.pop(tag, None)
        if entity:
            self.entities.append(entity)


def parse(html: str) -> Tuple[str, List[E.MessageEntity]]:
    """``(text, entities)`` of a piece of Telegram HTML: the text with the tags taken out and what they said as entities."""
    if not html:
        return html, []
    parser = _Parser()
    parser.feed(E.add_surrogate(html))
    text = strip_text(parser.text, parser.entities)
    parser.entities.reverse()
    parser.entities.sort(key=lambda entity: entity.offset)
    return E.del_surrogate(text), parser.entities
