"""A stand-in for pytdbot's ``Client`` for the tests, and a check that every request is one TDLib's schema has.

``FakeTd.invoke`` refuses (``SchemaError``) a request whose function, parameters or nested objects pytdbot does not know, or that
puts an object where the schema wants another kind, so a typo in a request fails a test instead of failing in front of Telegram.
Answers are given back as pytdbot's own typed objects (as the real client does), which also tests the conversion back to plain dicts.
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import os
import re
import struct
from typing import Any, Callable, Dict, List, Optional

import pytdbot
from pytdbot import types as td_types
from pytdbot.utils import dict_to_obj

SHIFT = 20  # a message id TDLib gives a message that Telegram has: the plain id << 20


class SchemaError(AssertionError):
    """A request that TDLib's schema (as pytdbot has it) does not allow."""


_PRIMITIVES = {"int": int, "float": (int, float), "str": str, "bool": bool, "bytes": (bytes, str)}  # bytes travel as base64 text
_KEYWORD = (inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)


def _parameters(function: Callable[..., Any]) -> Dict[str, str]:
    return {name: str(p.annotation) for name, p in inspect.signature(function).parameters.items() if name != "self" and p.kind in _KEYWORD}


def _check_value(where: str, annotation: str, value: Any) -> None:
    if value is None:
        return
    allowed = set(re.findall(r"\b([A-Z]\w*)\b", annotation)) - {"None"}  # the classes a field may hold (written with or without ``pytdbot.types.``)
    if isinstance(value, dict) and "@type" in value:
        name = value["@type"]
        cls_name = name[0].upper() + name[1:]
        cls = getattr(td_types, cls_name, None)
        fits = cls_name in allowed or any(
            inspect.isclass(cls) and inspect.isclass(base := getattr(td_types, a, None)) and issubclass(cls, base) for a in allowed)
        if allowed and not fits:
            raise SchemaError(f"{where}: {name} is not one of {sorted(allowed)}")
        check_object(value, where)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _check_value(f"{where}[{index}]", annotation, item)
    else:
        alternatives = {a.strip().strip("'") for a in annotation.strip("'").split("|")}
        primitive = {_PRIMITIVES[a] for a in alternatives if a in _PRIMITIVES}
        if primitive and alternatives <= set(_PRIMITIVES) | {"None"}:
            kinds = tuple(k for entry in primitive for k in (entry if isinstance(entry, tuple) else (entry,)))
            if isinstance(value, bool) and bool not in kinds:
                raise SchemaError(f"{where}: {value!r} is a bool, the schema wants {annotation}")
            if not isinstance(value, kinds):
                raise SchemaError(f"{where}: {value!r} is not {annotation}")


def check_object(obj: Dict[str, Any], where: str = "") -> None:
    name = obj.get("@type")
    cls = getattr(td_types, (name or " ")[0].upper() + (name or " ")[1:], None)
    if not inspect.isclass(cls):
        raise SchemaError(f"{where or name}: pytdbot has no type {name!r}")
    known = _parameters(cls.__init__)
    for key, value in obj.items():
        if key == "@type":
            continue
        if key not in known:
            raise SchemaError(f"{where}/{name}: {name} has no field {key!r} (it has {sorted(known)})")
        _check_value(f"{where}/{name}.{key}", known[key], value)


def check_request(request: Dict[str, Any]) -> None:
    """Raise ``SchemaError`` unless ``request`` is a call TDLib knows, with fields it has, holding objects of the right kinds."""
    name = request.get("@type")
    function = getattr(pytdbot.Client, name or "", None)
    if not callable(function) or name.startswith("_"):
        raise SchemaError(f"TDLib has no function {name!r}")
    known = _parameters(function)
    for key, value in request.items():
        if key == "@type":
            continue
        if key not in known:
            raise SchemaError(f"{name} has no parameter {key!r} (it has {sorted(known)})")
        _check_value(f"{name}.{key}", known[key], value)


# --------------------------------------------------------------------------- TDLib objects the tests build
def error(code: int, message: str) -> Dict[str, Any]:
    return {"@type": "error", "code": code, "message": message}


def td_file(file_id: int = 1, size: int = 0, *, remote_id: str = "", path: str = "", completed: bool = False, uploaded: int = 0,
            downloaded: int = 0) -> Dict[str, Any]:
    return {
        "@type": "file", "id": file_id, "size": size, "expected_size": size,
        "local": {"@type": "localFile", "path": path, "can_be_downloaded": True, "can_be_deleted": False, "is_downloading_active": False,
                  "is_downloading_completed": completed, "download_offset": 0, "downloaded_prefix_size": downloaded if not completed else size,
                  "downloaded_size": downloaded if not completed else size},
        "remote": {"@type": "remoteFile", "id": remote_id, "unique_id": "u" + remote_id, "is_uploading_active": False,
                   "is_uploading_completed": bool(remote_id), "uploaded_size": uploaded},
    }


def remote_id(dc: int, kind: int = 5) -> str:
    """A persistent file id like Telegram's: URL-safe base64 of a record with runs of zeros shortened, that starts with the file type
    (5 document, 4 video, 2 photo, 9 audio ...) and the data center, then holds the rest of the file's address."""
    raw = struct.pack("<ii", kind | 0x02000000, dc) + bytes(range(1, 9)) + b"\x2b"
    out, zeros = bytearray(), 0
    for byte in raw:
        if byte == 0:
            zeros += 1
            continue
        if zeros:
            out += b"\0" + bytes([zeros])
            zeros = 0
        out.append(byte)
    if zeros:
        out += b"\0" + bytes([zeros])
    return base64.urlsafe_b64encode(bytes(out)).decode().rstrip("=")


def formatted(text: str = "", entities: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    return {"@type": "formattedText", "text": text, "entities": entities or []}


def td_message(content: Dict[str, Any], *, chat_id: int = 1, plain_id: int = 10, sender: int = 1, out: bool = False,
               reply_to: int = 0, pending: bool = False) -> Dict[str, Any]:
    """A TDLib ``message`` (``plain_id`` is the id the bot works with; TDLib's own is that shifted left by 20 bits)."""
    message: Dict[str, Any] = {
        "@type": "message", "id": (plain_id << SHIFT) + (1 if pending else 0), "chat_id": chat_id, "date": 1_700_000_000,
        "sender_id": {"@type": "messageSenderUser", "user_id": sender}, "is_outgoing": out, "content": content,
    }
    if reply_to:
        message["reply_to"] = {"@type": "messageReplyToMessage", "chat_id": chat_id, "message_id": reply_to << SHIFT, "origin_send_date": 0}
    if pending:
        message["sending_state"] = {"@type": "messageSendingStatePending", "sending_id": 0}
    return message


def text_content(text: str = "", entities: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    return {"@type": "messageText", "text": formatted(text, entities)}


def document_content(name: str = "Movie.mkv", size: int = 5 * 1024 * 1024, *, mime: str = "video/x-matroska", caption: str = "",
                     file_id: int = 7, remote_id: str = "BQACAgEAAxkBAAIB", path: str = "", completed: bool = False,
                     thumbnail: Optional[Dict[str, Any]] = None, minithumbnail: Optional[str] = None) -> Dict[str, Any]:
    return {
        "@type": "messageDocument", "caption": formatted(caption),
        "document": {"@type": "document", "file_name": name, "mime_type": mime, "thumbnail": thumbnail,
                     "minithumbnail": {"@type": "minithumbnail", "width": 8, "height": 8, "data": minithumbnail} if minithumbnail else None,
                     "document": td_file(file_id, size, remote_id=remote_id, path=path, completed=completed)},
    }


def audio_content(name: str = "Song.mp3", size: int = 4 * 1024 * 1024, *, duration: int = 215, title: str = "Song", performer: str = "Me",
                  caption: str = "", file_id: int = 7, remote_id: str = "CQACAgEAAxkBAAIB", mime: str = "audio/mpeg") -> Dict[str, Any]:
    return {
        "@type": "messageAudio", "caption": formatted(caption),
        "audio": {"@type": "audio", "duration": duration, "title": title, "performer": performer, "file_name": name, "mime_type": mime,
                  "audio": td_file(file_id, size, remote_id=remote_id)},
    }


def thumbnail_of(size: int = 900, *, width: int = 320, height: int = 180, file_id: int = 55) -> Dict[str, Any]:
    """The ``thumbnail`` TDLib gives a document or a video that has a real preview picture (not just the tiny blurred one)."""
    return {"@type": "thumbnail", "format": {"@type": "thumbnailFormatJpeg"}, "width": width, "height": height,
            "file": td_file(file_id, size, remote_id="thumb" + str(file_id))}


def video_content(name: str = "Movie.mkv", size: int = 5 * 1024 * 1024, *, duration: int = 60, width: int = 640, height: int = 360,
                  caption: str = "", file_id: int = 7, remote_id: str = "BAACAgEAAxkBAAIB", mime: str = "video/x-matroska",
                  thumbnail: Optional[Dict[str, Any]] = None, minithumbnail: Optional[str] = None) -> Dict[str, Any]:
    return {
        "@type": "messageVideo", "caption": formatted(caption),
        "video": {"@type": "video", "duration": duration, "width": width, "height": height, "file_name": name, "mime_type": mime,
                  "supports_streaming": True, "thumbnail": thumbnail,
                  "minithumbnail": {"@type": "minithumbnail", "width": 8, "height": 8, "data": minithumbnail} if minithumbnail else None,
                  "video": td_file(file_id, size, remote_id=remote_id)},
    }


def photo_content(*, caption: str = "", file_id: int = 9, remote_id: str = "AgACAgEAAxkBAAIB", size: int = 4000) -> Dict[str, Any]:
    return {
        "@type": "messagePhoto", "caption": formatted(caption),
        "photo": {"@type": "photo", "has_stickers": False, "sizes": [
            {"@type": "photoSize", "type": "m", "width": 90, "height": 60, "photo": td_file(file_id - 1, 500, remote_id="small")},
            {"@type": "photoSize", "type": "x", "width": 800, "height": 600, "photo": td_file(file_id, size, remote_id=remote_id)}]},
    }


def mirror(content: Dict[str, Any], file_id: int) -> Dict[str, Any]:
    """The ``message`` content Telegram would report for an ``input_message_content`` that was sent."""
    kind = content["@type"]
    if kind == "inputMessageText":
        return text_content(content["text"]["text"], content["text"].get("entities"))
    caption = content.get("caption") or formatted()

    def name_of(input_file: Dict[str, Any], default: str) -> str:
        return os.path.basename(input_file.get("path") or input_file.get("original_path") or "") or default

    def file_of(input_file: Dict[str, Any]) -> Dict[str, Any]:
        if input_file["@type"] == "inputFileGenerated":  # not made yet: it is on its way to Telegram while the bot writes it
            return td_file(file_id, int(input_file.get("expected_size") or 0))
        local = input_file.get("path", "")
        size = os.path.getsize(local) if local and os.path.exists(local) else 0
        return td_file(file_id, size, remote_id="BQACAgEAAxkBAAI" + str(file_id), path=local, completed=True)

    if kind == "inputMessageDocument":
        inner = content["document"]
        name = name_of(inner["document"], "file")
        message = document_content(name, 0, caption="", mime="application/octet-stream")
        message["document"]["document"] = file_of(inner["document"])
    elif kind == "inputMessageVideo":
        inner = content["video"]
        name = name_of(inner["video"], "video.mp4")
        message = video_content(name, 0, duration=inner.get("duration", 0), width=inner.get("width", 0), height=inner.get("height", 0))
        message["video"]["video"] = file_of(inner["video"])
    elif kind == "inputMessageAudio":
        inner = content["audio"]
        name = name_of(inner["audio"], "audio.mp3")
        message = audio_content(name, 0, duration=inner.get("duration", 0), title=inner.get("title", ""), performer=inner.get("performer", ""))
        message["audio"]["audio"] = file_of(inner["audio"])
    elif kind == "inputMessagePhoto":
        message = photo_content()
        message["photo"]["sizes"][-1]["photo"] = file_of(content["photo"]["photo"])
    elif kind == "inputMessageForwarded":
        message = document_content(remote_id="BQACAgEAAxkBAAICOPY")
    else:
        return {"@type": "messageUnsupported"}
    message["caption"] = caption
    return message


# --------------------------------------------------------------------------- the client
class FakeTd:
    """Looks like ``pytdbot.Client`` to the bot: ``add_handler``, ``invoke``, ``start``, ``idle``, ``stop``.

    ``replies[function]`` holds the answers to a call (one per call, the last one stays; a callable gets the request, an exception is raised).
    A ``sendMessage`` is answered with the pending message TDLib returns, and ``updateMessageSendSucceeded`` follows by itself
    (``hold_sends = True`` keeps the message pending, ``fail_sends`` makes it fail).
    """

    def __init__(self, *, typed: bool = True, chat_id: int = 1):
        self.typed = typed
        self.handlers: Dict[str, List[Callable[..., Any]]] = {}
        self.requests: List[Dict[str, Any]] = []
        self.replies: Dict[str, List[Any]] = {}
        self.hold_sends = False
        self.fail_sends: List[Dict[str, Any]] = []
        self.started = self.stopped = False
        self._stop: Optional[asyncio.Event] = None
        self._counter = 100
        self.sent: List[Dict[str, Any]] = []  # the messages that went out (pending ones, as TDLib first answered)
        self.pending: List[Dict[str, Any]] = []  # the sends still waiting while ``hold_sends`` is on
        self._tasks: List["asyncio.Future[Any]"] = []

    # ---- what pytdbot's Client does
    def add_handler(self, update_type: str, func: Callable[..., Any], *_args: Any, **_kwargs: Any) -> None:
        self.handlers.setdefault(update_type, []).append(func)

    async def start(self, wait_login: bool = True) -> None:
        self.started = True

    async def idle(self) -> None:
        self._stop = self._stop or asyncio.Event()
        await self._stop.wait()

    async def stop(self) -> None:
        self.stopped = True
        (self._stop or asyncio.Event()).set()
        if self._stop is None:
            self._stop = asyncio.Event()
            self._stop.set()

    async def invoke(self, request: Dict[str, Any]) -> Any:
        check_request(request)
        self.requests.append(request)
        queue = self.replies.get(request["@type"])
        if queue:
            answer = queue[0] if len(queue) == 1 else queue.pop(0)
        else:
            answer = self._default(request)
        if callable(answer):
            answer = answer(request)
        if inspect.isawaitable(answer):
            answer = await answer
        if isinstance(answer, BaseException):
            raise answer
        return dict_to_obj(answer) if self.typed and isinstance(answer, dict) else answer

    # ---- for the tests
    def calls(self, function: str) -> List[Dict[str, Any]]:
        return [r for r in self.requests if r["@type"] == function]

    def reply(self, function: str, *answers: Any) -> None:
        self.replies[function] = list(answers)

    async def emit(self, update: Dict[str, Any]) -> None:
        data = dict_to_obj(update) if self.typed else update
        for handler in list(self.handlers.get(update["@type"], [])):
            await handler(self, data)

    async def settle(self, rounds: int = 20) -> None:
        """Let the tasks the bot started (each update is handled in its own) run."""
        for _ in range(rounds):
            await asyncio.sleep(0)

    async def confirm(self, pending: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Telegram accepts a message that was held; the message as it is now."""
        pending = pending or self.pending.pop(0)
        final = self._final(pending)
        await self.emit({"@type": "updateMessageSendSucceeded", "message": final, "old_message_id": pending["id"]})
        return final

    async def fail(self, pending: Optional[Dict[str, Any]] = None, code: int = 400, message: str = "Bad Request") -> None:
        pending = pending or self.pending.pop(0)
        await self.emit({"@type": "updateMessageSendFailed", "message": pending, "old_message_id": pending["id"],
                         "error": error(code, message)})

    # ---- default answers
    def _next(self) -> int:
        self._counter += 1
        return self._counter

    def _final(self, pending: Dict[str, Any]) -> Dict[str, Any]:
        final = dict(pending)
        n = self._next()
        final["id"] = n << SHIFT
        final["sending_state"] = None
        return final

    def _pending(self, request: Dict[str, Any], content: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        n = self._next()
        reply = request.get("reply_to") or {}
        content = content if content is not None else mirror(request["input_message_content"], n)
        message = td_message(content, chat_id=request["chat_id"], plain_id=5000 + n, sender=999, out=True,
                             reply_to=(reply.get("message_id", 0) >> SHIFT), pending=True)
        self.sent.append(message)
        return message

    def _schedule(self, pending: Dict[str, Any]) -> None:
        if self.hold_sends:
            self.pending.append(pending)
            return

        async def later() -> None:
            await asyncio.sleep(0)
            if self.fail_sends:
                failure = self.fail_sends.pop(0)
                await self.fail(pending, failure["code"], failure["message"])
            else:
                await self.confirm(pending)

        self._tasks.append(asyncio.ensure_future(later()))

    def _default(self, request: Dict[str, Any]) -> Dict[str, Any]:
        kind = request["@type"]
        if kind == "sendMessage":
            pending = self._pending(request)
            self._schedule(pending)
            return pending
        if kind == "forwardMessages":
            messages = []
            for message_id in request["message_ids"]:
                pending = self._pending({"chat_id": request["chat_id"]}, document_content(remote_id="BQACAgEAAxkBAAICOPY"))
                self._schedule(pending)
                messages.append(pending)
            return {"@type": "messages", "total_count": len(messages), "messages": messages}
        if kind in ("editMessageText", "editMessageCaption", "editMessageReplyMarkup"):
            return td_message(text_content("edited"), chat_id=request["chat_id"], plain_id=request["message_id"] >> SHIFT, out=True)
        if kind == "getMe":
            return {"@type": "user", "id": 999, "first_name": "Rename Bot", "usernames": {"@type": "usernames", "editable_username": "RenameBot",
                    "active_usernames": ["RenameBot"], "disabled_usernames": []}, "type": {"@type": "userTypeBot", "is_inline": True,
                    "inline_query_placeholder": "Pick a format", "can_join_groups": True, "can_read_all_group_messages": False,
                    "has_main_web_app": False, "is_business": False, "can_be_edited": False, "can_connect_to_business": False,
                    "can_be_added_to_attachment_menu": False, "need_location": False, "active_user_count": 0}}
        if kind == "getMessages":
            return {"@type": "messages", "total_count": 0, "messages": [None] * len(request["message_ids"])}
        if kind == "downloadFile":
            return td_file(request["file_id"], 100, path="/nonexistent", completed=True)
        return {"@type": "ok"}
