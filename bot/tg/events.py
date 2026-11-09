"""Events: what a handler is given, and the builders that say which updates a handler wants.

``NewMessage`` / ``CallbackQuery`` / ``InlineQuery`` are registered with ``client.add_event_handler(callback, builder)``.  Every
handler whose builder accepts an update is called, in the order they were added, until one raises ``StopPropagation``.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Iterable, List, Optional, Sequence

from .message import Message


class StopPropagation(Exception):
    """Raised by a handler to keep the handlers after it from seeing the same update."""


# --------------------------------------------------------------------------- builders
class NewMessage:
    kind = "message"

    def __init__(self, incoming: Optional[bool] = None, outgoing: Optional[bool] = None, *, pattern: Any = None,
                 func: Optional[Callable[[Any], Any]] = None, chats: Optional[Sequence[int]] = None,
                 from_users: Optional[Sequence[int]] = None):
        self.incoming, self.outgoing = incoming, outgoing
        self.pattern = re.compile(pattern).match if isinstance(pattern, str) else pattern.match if hasattr(pattern, "match") else pattern
        self.func = func
        self.chats = {int(c) for c in chats} if chats else None
        self.from_users = {int(u) for u in from_users} if from_users else None

    def accepts(self, event: Any) -> bool:
        """Does the handler want this message?  (A pattern that matches is kept in ``event.pattern_match``.)"""
        if self.incoming and event.message.out or self.outgoing and not event.message.out:
            return False
        if self.chats is not None and event.chat_id not in self.chats:
            return False
        if self.from_users is not None and event.sender_id not in self.from_users:
            return False
        if self.pattern is not None:
            event.pattern_match = self.pattern(event.raw_text or "")
            if not event.pattern_match:
                return False
        return self.func is None or bool(self.func(event))

    def build(self, client: Any, td_message: dict) -> Optional["MessageEvent"]:
        event = MessageEvent(client, Message(td_message))
        return event if self.accepts(event) else None


class CallbackQuery:
    kind = "callback"

    def __init__(self, *, func: Optional[Callable[[Any], Any]] = None, data: Optional[bytes] = None):
        self.func, self.data = func, data

    def accepts(self, event: Any) -> bool:
        return (self.data is None or event.data == self.data) and (self.func is None or bool(self.func(event)))

    def build(self, client: Any, update: dict) -> Optional["CallbackEvent"]:
        event = CallbackEvent(client, update)
        return event if self.accepts(event) else None


class InlineQuery:
    kind = "inline"

    def __init__(self, *, func: Optional[Callable[[Any], Any]] = None):
        self.func = func

    def accepts(self, event: Any) -> bool:
        return self.func is None or bool(self.func(event))

    def build(self, client: Any, update: dict) -> Optional["InlineEvent"]:
        event = InlineEvent(client, update)
        return event if self.accepts(event) else None


# --------------------------------------------------------------------------- events
class _Base:
    def __init__(self, client: Any):
        self.client = client
        self.pattern_match: Optional["re.Match[str]"] = None


class MessageEvent(_Base):
    def __init__(self, client: Any, message: Message):
        super().__init__(client)
        self.message = message
        self.chat_id = message.chat_id
        self.sender_id = message.sender_id
        self.id = self.message_id = message.id
        self.raw_text = message.raw_text
        self.text = message.text
        self.is_private = message.is_private

    async def respond(self, message: Optional[str] = None, **kwargs: Any) -> Message:
        """A new message in the same chat."""
        return await self._send(message, kwargs)

    async def reply(self, message: Optional[str] = None, **kwargs: Any) -> Message:
        """A new message in the same chat that replies to this one."""
        kwargs.setdefault("reply_to", self.message.id)
        return await self._send(message, kwargs)

    async def _send(self, message: Optional[str], kwargs: dict) -> Message:
        file = kwargs.pop("file", None)
        if file is not None:
            return await self.client.send_file(self.chat_id, file, caption=message, **kwargs)
        return await self.client.send_message(self.chat_id, message or "", **kwargs)

    async def edit(self, text: Optional[str] = None, **kwargs: Any) -> Message:
        return await self.client.edit_message(self.chat_id, self.message.id, text, **kwargs)

    async def delete(self) -> None:
        await self.client.delete_messages(self.chat_id, [self.message.id])

    async def get_reply_message(self) -> Optional[Message]:
        if not self.message.reply_to_msg_id:
            return None
        return await self.client.get_replied_message(self.chat_id, self.message.id)

    async def get_message(self) -> Message:
        return self.message

    async def download_media(self, file: Any = None, **kwargs: Any) -> Any:
        return await self.client.download_media(self.message, file=file, **kwargs)


class CallbackEvent(_Base):
    def __init__(self, client: Any, update: dict):
        super().__init__(client)
        self.query_id = int(update.get("id") or 0)
        self.sender_id = int(update.get("sender_user_id") or 0)
        self.chat_id = int(update.get("chat_id") or 0)
        from .ids import from_td_message_id
        self.message_id = self.id = from_td_message_id(update.get("message_id") or 0)
        payload = update.get("payload") or {}
        raw = payload.get("data")
        if isinstance(raw, str):
            import base64
            raw = base64.b64decode(raw)
        self.data: bytes = bytes(raw or b"")
        self.is_private = self.chat_id > 0
        self._message: Optional[Message] = None

    async def answer(self, message: Optional[str] = None, *, alert: bool = False, url: Optional[str] = None, cache_time: int = 0) -> None:
        await self.client.answer_callback(self.query_id, message, alert=alert, url=url, cache_time=cache_time)

    async def get_message(self) -> Optional[Message]:
        if self._message is None:
            self._message = await self.client.get_callback_message(self.chat_id, self.message_id, self.query_id)
        return self._message

    async def edit(self, text: Optional[str] = None, **kwargs: Any) -> Message:
        return await self.client.edit_message(self.chat_id, self.message_id, text, **kwargs)

    async def delete(self) -> None:
        await self.client.delete_messages(self.chat_id, [self.message_id])

    async def respond(self, message: Optional[str] = None, **kwargs: Any) -> Message:
        return await self.client.send_message(self.chat_id, message or "", **kwargs)

    async def reply(self, message: Optional[str] = None, **kwargs: Any) -> Message:
        kwargs.setdefault("reply_to", self.message_id)
        return await self.client.send_message(self.chat_id, message or "", **kwargs)

    async def get_reply_message(self) -> Optional[Message]:
        message = await self.get_message()
        if message is None or not message.reply_to_msg_id:
            return None
        return await self.client.get_replied_message(self.chat_id, self.message_id)


class InlineBuilder:
    """``event.builder``: makes the results an inline query is answered with."""

    def __init__(self, client: Any):
        self._client = client

    async def article(self, title: str, description: Optional[str] = None, *, text: Optional[str] = None, parse_mode: Any = "client",
                      link_preview: bool = True, buttons: Optional[Iterable[Iterable[Any]]] = None, id: Optional[str] = None) -> dict:
        import uuid
        from .buttons import markup
        content = self._client.text_content(text or title, parse_mode=parse_mode, link_preview=link_preview)
        return {
            "@type": "inputInlineQueryResultArticle", "id": id or uuid.uuid4().hex, "title": title, "description": description or "",
            "reply_markup": markup(buttons), "input_message_content": content,
        }


class InlineEvent(_Base):
    def __init__(self, client: Any, update: dict):
        super().__init__(client)
        self.query_id = int(update.get("id") or 0)
        self.sender_id = self.chat_id = int(update.get("sender_user_id") or 0)  # an inline query has no chat: it is the sender's own
        self.text = str(update.get("query") or "")
        self.offset = str(update.get("offset") or "")
        self.builder = InlineBuilder(client)

    async def answer(self, results: Optional[List[dict]] = None, *, cache_time: int = 0, private: bool = False) -> None:
        await self.client.answer_inline(self.query_id, results or [], cache_time=cache_time, private=private)
