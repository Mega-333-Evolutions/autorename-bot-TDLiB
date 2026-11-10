"""The bot's Telegram client: the calls the handlers make (``send_message``, ``edit_message``, ``send_file`` ...), answered by TDLib.

``Client`` wraps a pytdbot client (anything with ``invoke``, ``add_handler``, ``start``, ``stop`` and ``idle``).  Requests are
plain TDLib JSON, and so are the answers: they are turned into ``Message`` / ``User`` objects here, and a TDLib ``error`` becomes the
exception the handlers expect (see ``errors``).  Message ids are the plain ones (see ``ids``).
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import shutil
import tempfile
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

from . import entities as E
from . import errors
from . import html as tg_html
from .buttons import markup
from .events import StopPropagation
from .ids import from_td_message_id, to_td_message_id
from .message import Message, User

log = logging.getLogger(__name__)

IMAGE_EXTS = (".jpg", ".jpeg", ".png")  # sent as a picture unless the caller says "as a file"
_FILE_TYPE_OF_ID = {2: "photo", 3: "voice", 4: "video", 5: "document", 9: "audio", 10: "animation", 13: "video_note"}
_KIND_CACHE = 512


def plain(value: Any) -> Any:
    """A pytdbot object as the dict TDLib would have sent; dicts, lists and plain values are returned as they are."""
    if value is None or isinstance(value, (dict, list, str, int, float, bool, bytes)):
        return value
    from pytdbot.utils import obj_to_dict

    return obj_to_dict(value)


def file_type_of_id(remote_id: str) -> Optional[str]:
    """``photo``, ``video``, ``document`` ... for a persistent file id, ``None`` when it is not one."""
    import struct

    from .ids import _unrle

    try:
        raw = _unrle(base64.urlsafe_b64decode(remote_id + "=" * (-len(remote_id) % 4)))
        kind = struct.unpack("<i", raw[:4])[0] & 0xFF
    except Exception:  # noqa: BLE001
        return None
    return _FILE_TYPE_OF_ID.get(kind)


def _safe_name(name: str) -> str:
    name = (name or "file").replace("/", "_").replace("\0", "").strip() or "file"
    raw = name.encode()
    if len(raw) > 240:  # keep the extension when a name has to be cut
        stem, ext = os.path.splitext(name)
        name = stem.encode()[: 240 - len(ext.encode())].decode(errors="ignore") + ext
    return name


@dataclass
class Generated:
    """A file to send that does not exist yet: TDLib asks the bot for it while it uploads (``Client.generators[conversion]`` is called
    and writes it, see ``transfer.Relay``).  ``name`` is what it is called on Telegram, ``size`` how big it will be."""

    name: str
    conversion: str
    size: int = 0


def _jpeg_size(data: bytes) -> Tuple[int, int]:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            return img.size
    except Exception:  # noqa: BLE001
        return 0, 0


class Client:
    def __init__(self, td: Any, *, tmp_dir: Optional[Union[str, Path]] = None, parse_mode: Optional[str] = "html",
                 flood_sleep_threshold: int = 60, bot_dc: Optional[int] = None):
        self._td = td
        self.parse_mode = parse_mode
        self.flood_sleep_threshold = flood_sleep_threshold
        self.session = SimpleNamespace(dc_id=bot_dc)  # the data center the bot is on: from the setting, else from the first file it sends
        self.tmp_dir = Path(tmp_dir) if tmp_dir else Path(tempfile.gettempdir()) / "tg-send"
        self._handlers: List[Tuple[Callable[..., Any], Any]] = []
        self._tasks: Set["asyncio.Future[Any]"] = set()
        self._sending: Dict[Tuple[int, int], "asyncio.Future[Dict[str, Any]]"] = {}
        self._kinds: "OrderedDict[Tuple[int, int], str]" = OrderedDict()  # (chat, td message id) -> "text" | "caption"
        self._watchers: Dict[int, Callable[[Dict[str, Any]], None]] = {}  # file id -> called with each updateFile of it
        self.generators: Dict[str, Callable[[Dict[str, Any]], Any]] = {}  # conversion -> called when TDLib wants a file generated
        self._me: Optional[User] = None
        for update, handler in (
            ("updateNewMessage", self._on_new_message), ("updateNewCallbackQuery", self._on_callback),
            ("updateNewInlineQuery", self._on_inline), ("updateFile", self._on_file),
            ("updateMessageSendSucceeded", self._on_send_succeeded), ("updateMessageSendFailed", self._on_send_failed),
            ("updateFileGenerationStart", self._on_generation_start), ("updateFileGenerationStop", self._on_generation_stop),
        ):
            td.add_handler(update, handler)

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> "Client":
        await self._td.start()
        return self

    async def run_until_disconnected(self) -> None:
        await self._td.idle()

    async def disconnect(self) -> None:
        try:
            await self._td.stop()
        finally:
            for task in list(self._tasks):
                task.cancel()

    # ------------------------------------------------------------------ requests
    async def _call(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """One TDLib request.  A flood wait that is short enough is waited out and the request repeated."""
        for attempt in range(4):
            answer = plain(await self._td.invoke(request))
            if not isinstance(answer, dict) or answer.get("@type") != "error":
                return answer if isinstance(answer, dict) else {}
            error = errors.from_td(answer.get("code") or 0, answer.get("message") or "", request.get("@type"))
            if isinstance(error, errors.FloodWaitError) and error.seconds <= self.flood_sleep_threshold and attempt < 3:
                log.info("flood wait of %ss on %s", error.seconds, request.get("@type"))
                await asyncio.sleep(error.seconds + 0.5)
                continue
            raise error
        raise AssertionError("unreachable")  # pragma: no cover

    async def call(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Any TDLib request (a JSON object with its ``@type``), for the few places that need one this class has no method for."""
        return await self._call(request)

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._spawned_done)

    def _spawned_done(self, task: "asyncio.Future[Any]") -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:  # whatever was spawned handles its own errors; this is the net under it
            log.error("a background task failed", exc_info=task.exception())

    @staticmethod
    def _chat_id(entity: Any) -> int:
        return int(entity.chat_id if isinstance(entity, Message) else entity)

    @staticmethod
    def _msg_id(message: Any) -> int:
        return int(message.id if isinstance(message, Message) else message)

    # ------------------------------------------------------------------ text
    def formatted(self, text: Optional[str], parse_mode: Any = "client", entities: Optional[Iterable[E.MessageEntity]] = None) -> Dict[str, Any]:
        """A TDLib ``formattedText``: from ready entities, or by reading HTML, or as plain text."""
        if entities is not None:
            return E.formatted_text(text or "", entities)
        mode = self.parse_mode if parse_mode == "client" else parse_mode
        if isinstance(mode, str) and mode.lower() == "html":
            clean, found = tg_html.parse(text or "")
            return E.formatted_text(clean or "", found)
        return E.formatted_text(text or "")

    def text_content(self, text: Optional[str], *, parse_mode: Any = "client", link_preview: bool = True,
                     entities: Optional[Iterable[E.MessageEntity]] = None) -> Dict[str, Any]:
        content: Dict[str, Any] = {"@type": "inputMessageText", "text": self.formatted(text, parse_mode, entities)}
        if not link_preview:
            content["link_preview_options"] = {"@type": "linkPreviewOptions", "is_disabled": True}
        return content

    # ------------------------------------------------------------------ sending
    def _future(self, key: Tuple[int, int]) -> "asyncio.Future[Dict[str, Any]]":
        fut = self._sending.get(key)
        if fut is None:
            fut = asyncio.get_running_loop().create_future()
            self._sending[key] = fut
        return fut

    async def _on_send_succeeded(self, _client: Any, update: Any) -> None:
        d = plain(update)
        fut = self._future((int((d.get("message") or {}).get("chat_id") or 0), int(d.get("old_message_id") or 0)))
        if not fut.done():
            fut.set_result(d["message"])
        self._trim_sending()

    async def _on_send_failed(self, _client: Any, update: Any) -> None:
        d = plain(update)
        fut = self._future((int((d.get("message") or {}).get("chat_id") or 0), int(d.get("old_message_id") or 0)))
        if not fut.done():
            err = d.get("error") or {}
            fut.set_exception(errors.from_td(err.get("code") or 0, err.get("message") or "message was not sent"))
        self._trim_sending()

    def _trim_sending(self) -> None:
        """Answers nobody waited for (the waiter gave up) are not kept for ever."""
        if len(self._sending) > 256:
            for key in [k for k, f in self._sending.items() if f.done()][:128]:
                self._sending.pop(key, None)

    async def _await_sent(self, pending: Dict[str, Any], *, progress: Optional[Callable[[int, int], None]] = None,
                          cancel: Optional[asyncio.Event] = None) -> Dict[str, Any]:
        """The message once Telegram has it (TDLib answers a send with the message still waiting to be sent).  ``progress`` hears of the
        upload of its file; when ``cancel`` is set the message is deleted, which stops the upload."""
        state = (pending.get("sending_state") or {}).get("@type")
        if state != "messageSendingStatePending":
            return pending
        key = (int(pending["chat_id"]), int(pending["id"]))
        fut = self._future(key)
        info = Message(pending).file
        file_id = int((info.td_file if info else {}).get("id") or 0)
        if progress is not None and file_id:
            self._watchers[file_id] = lambda f: progress(int((f.get("remote") or {}).get("uploaded_size") or 0), int(f.get("size") or f.get("expected_size") or 0))
        try:
            if cancel is None:
                return await fut
            waiter = asyncio.ensure_future(cancel.wait())
            try:
                await asyncio.wait({fut, waiter}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                waiter.cancel()
            if fut.done():
                failure = fut.exception()
                if failure is not None and cancel.is_set():  # it failed because it was being stopped: that is the cancel, not a failure
                    raise errors.TransferCancelled() from failure
                return fut.result()
            await self.delete_messages(key[0], [from_td_message_id(key[1])], _td_ids=[key[1]])
            raise errors.TransferCancelled()
        finally:
            self._sending.pop(key, None)
            if file_id:
                self._watchers.pop(file_id, None)

    async def _send(self, chat: Any, content: Dict[str, Any], *, reply_to: Any = None, buttons: Any = None,
                    progress: Optional[Callable[[int, int], None]] = None, cancel: Optional[asyncio.Event] = None) -> Message:
        request: Dict[str, Any] = {"@type": "sendMessage", "chat_id": self._chat_id(chat), "input_message_content": content}
        if reply_to:
            request["reply_to"] = {"@type": "inputMessageReplyToMessage", "message_id": to_td_message_id(self._msg_id(reply_to))}
        keyboard = markup(buttons)
        if keyboard:
            request["reply_markup"] = keyboard
        pending = await self._call(request)
        final = await self._await_sent(pending, progress=progress, cancel=cancel)
        return self._seen(Message(final))

    def _seen(self, message: Message) -> Message:
        """What is learnt from a message that went out: how to edit it later, and which data center the bot's files go to."""
        self._kinds[(message.chat_id, message.td_id)] = "text" if message.content_type == "messageText" else "caption"
        while len(self._kinds) > _KIND_CACHE:
            self._kinds.popitem(last=False)
        if self.session.dc_id is None and message.document is not None and message.document.dc_id and message.out:
            self.session.dc_id = message.document.dc_id
        return message

    async def send_message(self, entity: Any, message: Optional[str] = "", *, buttons: Any = None, parse_mode: Any = "client",
                           link_preview: bool = True, reply_to: Any = None, formatting_entities: Optional[Iterable[E.MessageEntity]] = None,
                           **_: Any) -> Message:
        content = self.text_content(message or "", parse_mode=parse_mode, link_preview=link_preview, entities=formatting_entities)
        return await self._send(entity, content, reply_to=reply_to, buttons=buttons)

    # ------------------------------------------------------------------ files out
    def _stage(self, file: Any) -> Tuple[Dict[str, Any], str, Optional[Path]]:
        """``(InputFile, name, temp)``: where a file to send comes from.  ``temp`` is a copy this call made and has to remove."""
        if isinstance(file, Generated):  # TDLib uses the name in the path and nothing else: the file is made when it asks
            name = _safe_name(file.name)
            return {"@type": "inputFileGenerated", "original_path": str(self.tmp_dir / uuid.uuid4().hex / name),
                    "conversion": file.conversion, "expected_size": int(file.size or 0)}, name, None
        if isinstance(file, (bytes, bytearray)) or hasattr(file, "read"):
            data = bytes(file) if isinstance(file, (bytes, bytearray)) else file.read()
            name = _safe_name(getattr(file, "name", None) or "file")
            folder = self.tmp_dir / uuid.uuid4().hex
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / name
            path.write_bytes(data)
            return {"@type": "inputFileLocal", "path": str(path)}, name, folder
        text = os.fspath(file)
        if os.path.exists(text):
            return {"@type": "inputFileLocal", "path": os.path.abspath(text)}, os.path.basename(text), None
        return {"@type": "inputFileRemote", "id": text}, "", None

    def _named(self, input_file: Dict[str, Any], want: str, temp: Optional[Path]) -> Tuple[Dict[str, Any], Optional[Path]]:
        """TDLib names a file after its path, so a file that has to go out under another name is linked to it under that name."""
        if input_file.get("@type") != "inputFileLocal" or not want:
            return input_file, temp
        want = _safe_name(want)
        source = input_file["path"]
        if os.path.basename(source) == want:
            return input_file, temp
        folder = temp or (self.tmp_dir / uuid.uuid4().hex)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / want
        try:
            os.link(source, target)
        except OSError:
            try:
                os.symlink(source, target)
            except OSError:
                shutil.copyfile(source, target)
        return {"@type": "inputFileLocal", "path": str(target)}, folder

    def _thumbnail(self, thumb: Optional[bytes], folder: Path) -> Optional[Dict[str, Any]]:
        if not thumb:
            return None
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "thumbnail.jpg"
        path.write_bytes(thumb)
        width, height = _jpeg_size(thumb)
        return {"@type": "inputThumbnail", "thumbnail": {"@type": "inputFileLocal", "path": str(path)}, "width": width, "height": height}

    def _content(self, input_file: Dict[str, Any], name: str, caption: Dict[str, Any], *, force_document: bool, attributes: Sequence[Any],
                 thumb: Optional[Dict[str, Any]], remote_type: Optional[str] = None) -> Dict[str, Any]:
        video = next((a for a in attributes if isinstance(a, E.DocumentAttributeVideo) and not a.round_message), None)
        audio = next((a for a in attributes if isinstance(a, E.DocumentAttributeAudio) and not a.voice), None)
        animated = any(isinstance(a, E.DocumentAttributeAnimated) for a in attributes)
        kind = remote_type
        if kind is None:
            if force_document:
                kind = "document"
            elif os.path.splitext(name)[1].lower() in IMAGE_EXTS:
                kind = "photo"
            elif animated:  # a GIF: a silent clip that plays by itself, not a video
                kind = "animation"
            elif video is not None:
                kind = "video"
            elif audio is not None:
                kind = "audio"
            else:
                kind = "document"
        if kind == "photo":
            return {"@type": "inputMessagePhoto", "caption": caption, "photo": {"@type": "inputPhoto", "photo": input_file, "thumbnail": thumb}}
        if kind == "video":
            return {"@type": "inputMessageVideo", "caption": caption, "video": {
                "@type": "inputVideo", "video": input_file, "thumbnail": thumb, "duration": int(getattr(video, "duration", 0) or 0),
                "width": int(getattr(video, "w", 0) or 0), "height": int(getattr(video, "h", 0) or 0),
                "supports_streaming": bool(getattr(video, "supports_streaming", True))}}
        if kind == "audio":
            return {"@type": "inputMessageAudio", "caption": caption, "audio": {
                "@type": "inputAudio", "audio": input_file, "album_cover_thumbnail": thumb, "duration": int(getattr(audio, "duration", 0) or 0),
                "title": getattr(audio, "title", None) or "", "performer": getattr(audio, "performer", None) or ""}}
        if kind == "animation":
            return {"@type": "inputMessageAnimation", "caption": caption, "animation": {
                "@type": "inputAnimation", "animation": input_file, "thumbnail": thumb, "duration": int(getattr(video, "duration", 0) or 0),
                "width": int(getattr(video, "w", 0) or 0), "height": int(getattr(video, "h", 0) or 0)}}
        return {"@type": "inputMessageDocument", "caption": caption, "document": {
            "@type": "inputDocument", "document": input_file, "thumbnail": thumb, "disable_content_type_detection": True}}

    async def send_file(self, entity: Any, file: Any, *, caption: Optional[str] = None, buttons: Any = None, parse_mode: Any = "client",
                        force_document: bool = False, attributes: Optional[Sequence[Any]] = None, mime_type: Optional[str] = None,
                        thumb: Optional[bytes] = None, reply_to: Any = None, formatting_entities: Optional[Iterable[E.MessageEntity]] = None,
                        progress: Optional[Callable[[int, int], None]] = None, cancel: Optional[asyncio.Event] = None, **_: Any) -> Message:
        """Send a file: a path, bytes or a file object, a message whose file is sent again (copied), or a persistent file id.

        ``attributes`` (``DocumentAttributeFilename`` / ``Video`` / ``Audio``) name the file and tell what it is.  ``progress(done, total)``
        hears of the upload and ``cancel`` (an ``asyncio.Event``) stops it.
        """
        cap = self.formatted(caption or "", parse_mode, formatting_entities)
        attributes = list(attributes or [])
        if isinstance(file, Message):  # the same file again, with this caption: a copy, nothing is downloaded
            content = {"@type": "inputMessageForwarded", "from_chat_id": file.chat_id, "message_id": file.td_id, "in_game_share": False,
                       "copy_options": {"@type": "messageCopyOptions", "send_copy": True, "replace_caption": True, "new_caption": cap}}
            return await self._send(entity, content, reply_to=reply_to, buttons=buttons)
        input_file, name, temp = self._stage(file)
        try:
            want = next((a.file_name for a in attributes if isinstance(a, E.DocumentAttributeFilename) and a.file_name), "")
            input_file, temp = self._named(input_file, want, temp)
            if input_file["@type"] == "inputFileLocal":
                name = os.path.basename(input_file["path"])
            folder = temp or (self.tmp_dir / uuid.uuid4().hex)
            temp = folder
            content = self._content(
                input_file, name, cap, force_document=force_document, attributes=attributes,
                thumb=self._thumbnail(thumb, folder / "t") if thumb else None,
                remote_type=file_type_of_id(input_file.get("id", "")) if input_file["@type"] == "inputFileRemote" else None,
            )
            return await self._send(entity, content, reply_to=reply_to, buttons=buttons, progress=progress, cancel=cancel)
        finally:
            if temp is not None:
                shutil.rmtree(temp, ignore_errors=True)

    async def forward_messages(self, entity: Any, messages: Any, from_peer: Any = None, *, drop_author: bool = False, **_: Any) -> Any:
        """Forward (or, with ``drop_author``, copy) messages; the answer holds the new messages once they are sent."""
        single = isinstance(messages, (int, Message))
        items = [messages] if single else list(messages)
        source = self._chat_id(from_peer) if from_peer is not None else self._chat_id(items[0])
        answer = await self._call({
            "@type": "forwardMessages", "chat_id": self._chat_id(entity), "from_chat_id": source,
            "message_ids": [to_td_message_id(self._msg_id(m)) for m in items], "send_copy": bool(drop_author),
        })
        sent = [self._seen(Message(await self._await_sent(m))) for m in answer.get("messages") or [] if m]
        return (sent[0] if sent else None) if single else sent

    # ------------------------------------------------------------------ editing and deleting
    async def edit_message(self, entity: Any, message: Any = None, text: Optional[str] = None, *, buttons: Any = None,
                           parse_mode: Any = "client", link_preview: bool = True,
                           formatting_entities: Optional[Iterable[E.MessageEntity]] = None, **_: Any) -> Message:
        """Edit a message: its text (or caption), and its buttons (none given: none left).  ``entity`` may be the message itself."""
        if isinstance(entity, Message):
            chat_id, message_id = entity.chat_id, entity.id
            if text is None:
                text = message
        else:
            chat_id, message_id = self._chat_id(entity), self._msg_id(message)
        td_id = to_td_message_id(message_id)
        keyboard = markup(buttons)
        base: Dict[str, Any] = {"chat_id": chat_id, "message_id": td_id, "reply_markup": keyboard}
        if text is None:
            return Message(await self._call({"@type": "editMessageReplyMarkup", **base}))
        key = (chat_id, td_id)
        if self._kinds.get(key) != "caption":
            try:
                done = await self._call({"@type": "editMessageText", **base, "input_message_content": self.text_content(
                    text, parse_mode=parse_mode, link_preview=link_preview, entities=formatting_entities)})
                self._kinds[key] = "text"
                return Message(done)
            except errors.RPCError as exc:
                if "no text" not in exc.message.lower():
                    raise
                self._kinds[key] = "caption"  # a file with a caption: its text is the caption
        return Message(await self._call({"@type": "editMessageCaption", **base, "caption": self.formatted(text, parse_mode, formatting_entities)}))

    async def delete_messages(self, entity: Any, message_ids: Any, *, revoke: bool = True, _td_ids: Optional[List[int]] = None) -> None:
        items = [message_ids] if isinstance(message_ids, (int, Message)) else list(message_ids)
        ids = _td_ids if _td_ids is not None else [to_td_message_id(self._msg_id(i)) for i in items]
        await self._call({"@type": "deleteMessages", "chat_id": self._chat_id(entity), "message_ids": ids, "revoke": revoke})

    # ------------------------------------------------------------------ reading
    async def get_messages(self, entity: Any, ids: Any = None, **_: Any) -> Any:
        """Messages by id: a list (``None`` for one that is gone), or the message itself when one id was asked for."""
        single = isinstance(ids, (int, Message))
        wanted = [ids] if single else list(ids or [])
        answer = await self._call({"@type": "getMessages", "chat_id": self._chat_id(entity), "message_ids": [to_td_message_id(self._msg_id(i)) for i in wanted]})
        found = [Message(m) if isinstance(m, dict) and m else None for m in answer.get("messages") or []]
        found += [None] * (len(wanted) - len(found))
        return found[0] if single else found

    async def get_replied_message(self, chat_id: int, message_id: int) -> Optional[Message]:
        try:
            return Message(await self._call({"@type": "getRepliedMessage", "chat_id": int(chat_id), "message_id": to_td_message_id(message_id)}))
        except errors.RPCError as exc:
            if exc.code in (400, 404):
                return None
            raise

    async def get_callback_message(self, chat_id: int, message_id: int, query_id: int) -> Optional[Message]:
        try:
            return Message(await self._call({"@type": "getCallbackQueryMessage", "chat_id": int(chat_id),
                                             "message_id": to_td_message_id(message_id), "callback_query_id": int(query_id)}))
        except errors.RPCError as exc:
            if exc.code in (400, 404):
                return None
            raise

    async def get_input_entity(self, chat_id: Any) -> Dict[str, Any]:
        """The chat, when the bot can reach it (raises when TDLib does not know it)."""
        return await self._call({"@type": "getChat", "chat_id": int(chat_id)})

    async def detect_dc(self) -> Optional[int]:
        """The data center of the bot's account, when TDLib tells it (through an option it may not have); kept in ``session.dc_id``."""
        if self.session.dc_id is not None:
            return self.session.dc_id
        for name in ("dc_id", "main_dc_id"):
            try:
                answer = await self._call({"@type": "getOption", "name": name})
            except errors.RPCError:
                continue
            value = answer.get("value")
            if answer.get("@type") == "optionValueInteger" and str(value).isdigit() and 1 <= int(value) <= 5:
                self.session.dc_id = int(value)
                return self.session.dc_id
        return None

    async def get_me(self) -> User:
        if self._me is None:
            self._me = User(await self._call({"@type": "getMe"}))
        return self._me

    # ------------------------------------------------------------------ answering
    async def answer_callback(self, query_id: int, text: Optional[str] = None, *, alert: bool = False, url: Optional[str] = None, cache_time: int = 0) -> None:
        await self._call({"@type": "answerCallbackQuery", "callback_query_id": int(query_id), "text": text or "", "show_alert": bool(alert),
                          "url": url or "", "cache_time": int(cache_time)})

    async def answer_inline(self, query_id: int, results: List[Dict[str, Any]], *, cache_time: int = 0, private: bool = False) -> None:
        await self._call({"@type": "answerInlineQuery", "inline_query_id": int(query_id), "is_personal": bool(private), "results": results,
                          "cache_time": int(cache_time), "next_offset": ""})

    async def set_bot_commands(self, commands: Iterable[E.BotCommand]) -> None:
        await self._call({"@type": "setCommands", "scope": {"@type": "botCommandScopeDefault"}, "language_code": "",
                          "commands": [{"@type": "botCommand", "command": c.command, "description": c.description} for c in commands]})

    async def ping(self) -> float:
        """Milliseconds for a round trip to Telegram.  TDLib's ``testNetwork`` is no use: Telegram refuses the request behind it for a
        bot (``BOT_METHOD_INVALID``).  What is timed is a request a bot may make that TDLib never answers from its own cache: its own
        command list."""
        started = time.perf_counter()
        await self._call({"@type": "getCommands", "scope": {"@type": "botCommandScopeDefault"}, "language_code": ""})
        return (time.perf_counter() - started) * 1000

    # ------------------------------------------------------------------ files in
    async def _on_file(self, _client: Any, update: Any) -> None:
        file = (plain(update) or {}).get("file") or {}
        watcher = self._watchers.get(int(file.get("id") or 0))
        if watcher is not None:
            try:
                watcher(file)
            except Exception:  # noqa: BLE001 - a progress callback must not break the update
                log.debug("file watcher failed", exc_info=True)

    async def _download(self, td_file: Dict[str, Any], *, progress: Optional[Callable[[int, int], None]], cancel: Optional[asyncio.Event],
                        priority: int, limit: int = 0) -> Dict[str, Any]:
        """Ask TDLib for a file (or its first ``limit`` bytes) and wait until it is there; TDLib's description of the file after that."""
        file_id = int(td_file["id"])
        total = int(td_file.get("size") or td_file.get("expected_size") or 0)
        if progress is not None:
            self._watchers[file_id] = lambda f: progress(
                int((f.get("local") or {}).get("downloaded_size") or 0), int(f.get("size") or f.get("expected_size") or total))
        job = asyncio.ensure_future(self._call({"@type": "downloadFile", "file_id": file_id, "priority": priority, "offset": 0,
                                                "limit": int(limit), "synchronous": True}))
        try:
            if cancel is not None:
                waiter = asyncio.ensure_future(cancel.wait())
                try:
                    await asyncio.wait({job, waiter}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    waiter.cancel()
                if not job.done():
                    job.cancel()
                    await self._call({"@type": "cancelDownloadFile", "file_id": file_id, "only_if_pending": False})
                    raise errors.TransferCancelled()
            return await job
        finally:
            self._watchers.pop(file_id, None)
            if not job.done():
                job.cancel()

    async def fetch_file(self, td_file: Dict[str, Any], *, progress: Optional[Callable[[int, int], None]] = None,
                         cancel: Optional[asyncio.Event] = None, priority: int = 16) -> str:
        """Download a TDLib file; the path TDLib keeps it at.  ``progress(done, total)`` hears of the bytes that came, ``cancel``
        stops the download (``TransferCancelled``)."""
        local = td_file.get("local") or {}
        if local.get("is_downloading_completed") and local.get("path") and os.path.exists(local["path"]):
            return local["path"]
        done = await self._download(td_file, progress=progress, cancel=cancel, priority=priority)
        done_local = done.get("local") or {}
        if not done_local.get("is_downloading_completed") or not done_local.get("path"):
            raise errors.RPCError("the file could not be downloaded", 500, "downloadFile")
        return done_local["path"]

    async def download_to(self, message: Message, dest: Union[str, Path], *, progress: Optional[Callable[[int, int], None]] = None,
                          cancel: Optional[asyncio.Event] = None, max_bytes: Optional[int] = None) -> int:
        """Download the file of ``message`` to ``dest`` (only its first ``max_bytes`` bytes when that is given); the bytes written.

        TDLib keeps its own copy of what it downloads: a whole file is moved to ``dest`` (not copied), the start of one is copied and
        TDLib's copy is deleted.
        """
        if message.file is None or not message.file.td_file:
            raise errors.RPCError("there is no file in that message", 400, "downloadFile")
        td_file = message.file.td_file
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        total = int(td_file.get("size") or td_file.get("expected_size") or 0)
        if max_bytes is not None and (not total or max_bytes < total):
            done = await self._download(td_file, progress=progress, cancel=cancel, priority=16, limit=int(max_bytes))
            local = done.get("local") or {}
            path = local.get("path") or ""
            have = int(local.get("downloaded_prefix_size") or 0)
            if local.get("is_downloading_completed") and path and os.path.exists(path):
                have = max(have, os.path.getsize(path))
            if not path or have < min(int(max_bytes), total or int(max_bytes)):
                raise errors.RPCError("the start of the file could not be downloaded", 500, "downloadFile")
            written = 0
            try:
                with open(path, "rb") as source, open(dest, "wb") as out:
                    while written < max_bytes:
                        chunk = source.read(min(1 << 20, max_bytes - written))
                        if not chunk:
                            break
                        out.write(chunk)
                        written += len(chunk)
            finally:
                await self.forget_file(td_file)
            return written
        path = await self.fetch_file(td_file, progress=progress, cancel=cancel)
        try:
            os.replace(path, dest)
        except OSError:  # another disk
            shutil.move(path, dest)
        await self.forget_file(td_file)
        return dest.stat().st_size

    async def download_media(self, message: Message, file: Any = None, *, thumb: Any = None, **_: Any) -> Any:
        """The file of a message (or, with ``thumb``, its preview) as ``bytes`` (``file=bytes``) or saved at a path; ``None`` when
        there is nothing to give."""
        if thumb is not None:
            if message.thumb_file:
                td_file = message.thumb_file
            elif message.minithumbnail:
                data = base64.b64decode(message.minithumbnail)
                return data if file is bytes else self._save(data, file)
            else:
                return None
        else:
            td_file = message.file.td_file if message.file else None
        if not td_file:
            return None
        path = await self.fetch_file(td_file, priority=32)
        if file is bytes:
            try:
                return Path(path).read_bytes()
            finally:
                await self.forget_file(td_file)
        return self._save(Path(path).read_bytes(), file) if file else path

    @staticmethod
    def _save(data: bytes, file: Any) -> Any:
        if hasattr(file, "write"):
            file.write(data)
            return file
        Path(file).write_bytes(data)
        return str(file)

    async def forget_file(self, td_file: Dict[str, Any]) -> None:
        """Delete TDLib's own copy of a file (what it downloaded or generated), to give the disk space back."""
        try:
            await self._call({"@type": "deleteFile", "file_id": int(td_file["id"])})
        except errors.RPCError:
            pass

    def watch_file(self, file_id: int, callback: Callable[[Dict[str, Any]], None]) -> None:
        """``callback(file)`` is called with TDLib's description of the file each time it changes (one callback for a file)."""
        self._watchers[int(file_id)] = callback

    def unwatch_file(self, file_id: int) -> None:
        self._watchers.pop(int(file_id), None)

    # ------------------------------------------------------------------ files generated here (see relay)
    async def _on_generation_start(self, _client: Any, update: Any) -> None:
        d = plain(update) or {}
        starter = self.generators.get(str(d.get("conversion") or ""))
        if starter is None:
            await self._call({"@type": "finishFileGeneration", "generation_id": d.get("generation_id"),
                              "error": {"@type": "error", "code": 400, "message": "unknown conversion"}})
            return
        self._spawn(starter(d))

    async def _on_generation_stop(self, _client: Any, update: Any) -> None:
        d = plain(update) or {}
        stopper = self.generators.get("stop:" + str(d.get("generation_id")))
        if stopper is not None:
            stopper(d)

    # ------------------------------------------------------------------ events
    def add_event_handler(self, callback: Callable[..., Any], builder: Any) -> None:
        self._handlers.append((callback, builder))

    async def _on_new_message(self, _client: Any, update: Any) -> None:
        self._spawn(self._dispatch("message", (plain(update) or {}).get("message") or {}))

    async def _on_callback(self, _client: Any, update: Any) -> None:
        self._spawn(self._dispatch("callback", plain(update) or {}))

    async def _on_inline(self, _client: Any, update: Any) -> None:
        self._spawn(self._dispatch("inline", plain(update) or {}))

    async def _dispatch(self, kind: str, payload: Dict[str, Any]) -> None:
        for callback, builder in list(self._handlers):
            if getattr(builder, "kind", None) != kind:
                continue
            try:
                event = builder.build(self, payload)
            except Exception:  # noqa: BLE001
                log.exception("A handler filter failed:")
                continue
            if event is None:
                continue
            try:
                await callback(event)
            except StopPropagation:
                break
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Unhandled exception in %s:", getattr(callback, "__name__", callback))
