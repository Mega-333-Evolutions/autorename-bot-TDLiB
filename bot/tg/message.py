"""A message as the bot sees it, built from a TDLib ``message`` dict.

The attributes are the ones the handlers read (``raw_text``, ``file``, ``document``, ``photo``, ``video``, ``gif`` ...):
``document`` is set for anything that is a file (a video, a voice message and a sticker are files too), and
``photo`` only for a picture sent as a photo.  The ids are the plain ones (see ``ids``).
"""
from __future__ import annotations

import mimetypes
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import entities as E
from .buttons import read_markup
from .ids import dc_of_remote_id, from_td_message_id

# the message contents that carry a file, with the key of the object that describes it and the key of its file inside that object
_FILE_CONTENT = {
    "messageDocument": ("document", "document"),
    "messageVideo": ("video", "video"),
    "messageAudio": ("audio", "audio"),
    "messageAnimation": ("animation", "animation"),
    "messageVoiceNote": ("voice_note", "voice"),
    "messageVideoNote": ("video_note", "video"),
    "messageSticker": ("sticker", "sticker"),
}


def _file_of(obj: Optional[Dict[str, Any]], key: str) -> Optional[Dict[str, Any]]:
    f = (obj or {}).get(key)
    return f if isinstance(f, dict) else None


def _size_of(file: Optional[Dict[str, Any]]) -> int:
    if not file:
        return 0
    return int(file.get("size") or file.get("expected_size") or 0)


class FileInfo:
    """What is known about the file of a message."""

    def __init__(self, *, name: Optional[str], mime_type: Optional[str], size: int, width: Optional[int] = None,
                 height: Optional[int] = None, duration: Optional[float] = None, title: Optional[str] = None,
                 performer: Optional[str] = None, td_file: Optional[Dict[str, Any]] = None):
        self.name = name or None
        self.mime_type = mime_type or None
        self.size = int(size or 0)
        self.width = width
        self.height = height
        self.duration = duration
        self.title = title or None
        self.performer = performer or None
        self.td_file = td_file or {}

    @property
    def ext(self) -> str:
        ext = os.path.splitext(self.name)[1] if self.name else ""
        return ext or (mimetypes.guess_extension(self.mime_type) if self.mime_type else None) or ""

    @property
    def id(self) -> str:
        """The persistent id of the file (what a bot can send it by)."""
        return str((self.td_file.get("remote") or {}).get("id") or "")


class Document:
    """The file of a message that has one (a video, a voice message and a sticker included): its attributes and its previews."""

    def __init__(self, *, td_file: Optional[Dict[str, Any]], mime_type: Optional[str], size: int, attributes: List[Any], thumbs: List[Any]):
        self.mime_type = mime_type
        self.size = size
        self.attributes = attributes
        self.thumbs = thumbs
        self.td_file = td_file or {}
        self.dc_id = dc_of_remote_id((self.td_file.get("remote") or {}).get("id") or "")

    @property
    def id(self) -> str:
        return str((self.td_file.get("remote") or {}).get("id") or "")


class Photo:
    """The picture of a message sent as a photo."""

    def __init__(self, *, td_file: Optional[Dict[str, Any]], width: int, height: int, size: int):
        self.td_file = td_file or {}
        self.width, self.height, self.size = width, height, size
        self.dc_id = dc_of_remote_id((self.td_file.get("remote") or {}).get("id") or "")

    @property
    def id(self) -> str:
        return str((self.td_file.get("remote") or {}).get("id") or "")


def _text_of(formatted: Optional[Dict[str, Any]]) -> tuple:
    f = formatted or {}
    return str(f.get("text") or ""), E.from_td(f.get("entities"))


def _thumbs(obj: Dict[str, Any]) -> List[Any]:
    if obj.get("thumbnail"):
        return [E.PhotoSize(type="m", w=int(obj["thumbnail"].get("width") or 0), h=int(obj["thumbnail"].get("height") or 0))]
    if obj.get("minithumbnail"):
        return [E.PhotoStrippedSize(type="i")]
    return []


class Message:
    """One Telegram message: ``Message(td_message)``.  ``td`` is kept for the code that has to talk to TDLib about it."""

    def __init__(self, td: Dict[str, Any]):
        self.td = td
        self.id: int = from_td_message_id(td.get("id") or 0)
        self.td_id: int = int(td.get("id") or 0)
        self.chat_id: int = int(td.get("chat_id") or 0)
        sender = td.get("sender_id") or {}
        self.sender_id: Optional[int] = (
            int(sender["user_id"]) if "user_id" in sender else int(sender["chat_id"]) if "chat_id" in sender else None
        )
        self.out: bool = bool(td.get("is_outgoing"))
        self.date: Optional[datetime] = datetime.fromtimestamp(td["date"], tz=timezone.utc) if td.get("date") else None
        reply = td.get("reply_to") or {}
        self.reply_to_msg_id: Optional[int] = (
            from_td_message_id(reply["message_id"]) if reply.get("@type") == "messageReplyToMessage" and reply.get("message_id") else None
        )
        self.grouped_id: Optional[int] = int(td.get("media_album_id") or 0) or None
        self.is_private: bool = self.chat_id > 0
        self.reply_markup: Optional[E.ReplyInlineMarkup] = read_markup(td.get("reply_markup"))

        content = td.get("content") or {}
        self.content_type: str = content.get("@type") or ""
        self.raw_text, self.entities = _text_of(content.get("text") if self.content_type == "messageText" else content.get("caption"))
        self.text = self.raw_text
        self.message = self.raw_text

        self.document: Optional[Document] = None
        self.photo: Optional[Photo] = None
        self.video: Optional[Document] = None
        self.audio: Optional[Document] = None
        self.voice: Optional[Document] = None
        self.gif: Optional[Document] = None
        self.sticker: Optional[Document] = None
        self.video_note: Optional[Document] = None
        self.file: Optional[FileInfo] = None
        self.thumb_file: Optional[Dict[str, Any]] = None  # the TDLib file of the preview, when there is one
        self.minithumbnail: Optional[str] = None  # the tiny preview as base64, when that is all there is
        self._read_media(content)

    # ------------------------------------------------------------------ media
    @property
    def media(self) -> Optional[Any]:
        """Something (truthy) when the message carries anything but text."""
        return None if self.content_type in ("", "messageText") else (self.document or self.photo or self.content_type)

    def _read_media(self, content: Dict[str, Any]) -> None:
        kind = self.content_type
        if kind == "messagePhoto":
            photo = content.get("photo") or {}
            sizes = [s for s in photo.get("sizes") or [] if isinstance(s, dict) and s.get("photo")]
            best = max(sizes, key=lambda s: (int(s.get("width") or 0) * int(s.get("height") or 0), _size_of(s["photo"])), default=None)
            if best is not None:
                f = best["photo"]
                self.photo = Photo(td_file=f, width=int(best.get("width") or 0), height=int(best.get("height") or 0), size=_size_of(f))
                self.file = FileInfo(name=None, mime_type="image/jpeg", size=_size_of(f), width=self.photo.width, height=self.photo.height, td_file=f)
            return
        if kind not in _FILE_CONTENT:
            return
        key, file_key = _FILE_CONTENT[kind]
        obj = content.get(key) or {}
        f = _file_of(obj, file_key)
        name = str(obj.get("file_name") or "")
        mime = str(obj.get("mime_type") or "") or None
        attributes: List[Any] = [E.DocumentAttributeFilename(name)] if name else []
        width, height, duration = obj.get("width"), obj.get("height"), obj.get("duration")
        title = performer = None
        if kind == "messageVideo":
            attributes.append(E.DocumentAttributeVideo(duration=duration or 0, w=width or 0, h=height or 0, supports_streaming=bool(obj.get("supports_streaming"))))
        elif kind == "messageAudio":
            title, performer = obj.get("title") or None, obj.get("performer") or None
            attributes.append(E.DocumentAttributeAudio(duration=int(duration or 0), title=title, performer=performer))
        elif kind == "messageAnimation":
            attributes += [E.DocumentAttributeAnimated(), E.DocumentAttributeVideo(duration=duration or 0, w=width or 0, h=height or 0)]
        elif kind == "messageVoiceNote":
            attributes.append(E.DocumentAttributeAudio(duration=int(duration or 0), voice=True))
        elif kind == "messageVideoNote":
            side = int(obj.get("length") or 0)
            width = height = side
            attributes.append(E.DocumentAttributeVideo(duration=duration or 0, w=side, h=side, round_message=True))
        elif kind == "messageSticker":
            width, height = obj.get("width"), obj.get("height")
            mime = mime or {"stickerFormatWebp": "image/webp", "stickerFormatTgs": "application/x-tgsticker", "stickerFormatWebm": "video/webm"}.get(
                (obj.get("format") or {}).get("@type"), None
            )
        doc = Document(td_file=f, mime_type=mime, size=_size_of(f), attributes=attributes, thumbs=_thumbs(obj))
        self.document = doc
        thumb = obj.get("thumbnail") or {}
        self.thumb_file = thumb.get("file") if isinstance(thumb.get("file"), dict) else None
        mini = obj.get("minithumbnail") or {}
        self.minithumbnail = mini.get("data") if isinstance(mini.get("data"), str) else None
        if kind == "messageVideo":
            self.video = doc
        elif kind == "messageAudio":
            self.audio = doc
        elif kind == "messageVoiceNote":
            self.voice = doc
        elif kind == "messageAnimation":
            self.gif = doc
        elif kind == "messageSticker":
            self.sticker = doc
        elif kind == "messageVideoNote":
            self.video_note = doc
        self.file = FileInfo(
            name=name, mime_type=mime, size=doc.size, width=int(width) if width else None, height=int(height) if height else None,
            duration=float(duration) if duration else None, title=title, performer=performer, td_file=f,
        )

    def __repr__(self) -> str:
        return f"<Message {self.chat_id}:{self.id} {self.content_type}>"


class User:
    """The part of a TDLib ``user`` the bot reads: ``id``, ``username``, ``first_name``, and ``bot_inline_placeholder`` (``None``
    unless the bot has inline mode on)."""

    def __init__(self, td: Dict[str, Any]):
        self.td = td
        self.id = int(td.get("id") or 0)
        self.first_name = str(td.get("first_name") or "")
        self.username: Optional[str] = (td.get("usernames") or {}).get("editable_username") or None
        kind = td.get("type") or {}
        self.bot = kind.get("@type") == "userTypeBot"
        self.bot_inline_placeholder: Optional[str] = str(kind.get("inline_query_placeholder") or "") if kind.get("is_inline") else None
