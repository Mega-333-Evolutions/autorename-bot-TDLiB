"""Stand-ins for Telegram events and the client, used by the handler tests.

``dispatch`` pushes an event through the handlers registered on ``FakeClient`` the
same way the real client does (every matching builder runs, in registration order), so
the real event builders, patterns and ``func`` filters are exercised too.
"""
from __future__ import annotations

import inspect
import re
from types import SimpleNamespace
from typing import Any, List, Optional, Tuple

from bot.tg import errors, events, types
from bot.tg import html as telegram_html
from bot.tg.entities import MessageEntityCode, add_surrogate, del_surrogate
from bot.tg.events import InlineBuilder


_ENTITY_TAGS = {
    "b": types.MessageEntityBold, "i": types.MessageEntityItalic, "u": types.MessageEntityUnderline,
    "s": types.MessageEntityStrike, "code": types.MessageEntityCode, "quote": types.MessageEntityBlockquote,
    "spoiler": types.MessageEntitySpoiler, "tag": types.MessageEntityHashtag, "at": types.MessageEntityMention,
}


def telegram_text(markup: str) -> Tuple[str, List[Any]]:
    """A message the way Telegram delivers it: the plain text and the formatting as entities.

    ``"/mode [b]Hi[/b] [a=https://x.io]a link[/a]"`` -> ``("/mode Hi a link", [command, bold, link])``.  Tags: b i u s code
    quote spoiler tag at, ``[pre=python]``, ``[a=url]``; tags may overlap.  Offsets count UTF-16 units, like Telegram's.
    """
    text, entities, open_tags, position = "", [], [], 0
    for m in re.finditer(r"\[(/?)(\w+)(?:=([^\]]*))?\]", markup):
        text += markup[position:m.start()]
        position = m.end()
        closing, tag, argument = m.groups()
        here = len(add_surrogate(text))
        if not closing:
            open_tags.append((tag, argument, here))
            continue
        index = max(i for i, t in enumerate(open_tags) if t[0] == tag)
        _tag, argument, offset = open_tags.pop(index)
        length = here - offset
        if tag == "a":
            entities.append(types.MessageEntityTextUrl(offset, length, argument))
        elif tag == "pre":
            entities.append(types.MessageEntityPre(offset, length, argument or ""))
        else:
            entities.append(_ENTITY_TAGS[tag](offset, length))
    text += markup[position:]
    command = re.match(r"/\w+(?:@\w+)?", text)
    if command:  # Telegram marks the command of a message as an entity of its own
        entities.insert(0, types.MessageEntityBotCommand(0, command.end()))
    return text, sorted(entities, key=lambda e: e.offset)


def make_file(name: Optional[str] = "Movie.mkv", size: int = 5 * 1024 * 1024, mime: str = "video/x-matroska",
              ext: Optional[str] = None, *, width: Optional[int] = None, height: Optional[int] = None,
              duration: Optional[float] = None, file_id: str = "BQACAgEAAxkBAAIBXXXX") -> SimpleNamespace:
    if ext is None:
        ext = name[name.rfind("."):] if name and "." in name else ""
    return SimpleNamespace(name=name, size=size, mime_type=mime, ext=ext, width=width, height=height, duration=duration, id=file_id)


def has_share_button(buttons: Any) -> bool:
    """Is there a share (switch inline) button among the buttons?"""
    return any(isinstance(b.type, types.InlineButtonTypeSwitchInline) for row in buttons or [] for b in row)


def refuse_share_button(client: Any, buttons: Any) -> None:
    """Telegram refuses a message with a share button while inline mode is off for the bot (``client.server_inline``)."""
    if client is not None and not client.server_inline and has_share_button(buttons):
        raise errors.BotInlineDisabledError("BOT_INLINE_DISABLED")


class FakeMessage:
    def __init__(self, text: str = "", *, mid: int = 1, sender: int = 1, file: Any = None, video: bool = False,
                 audio: bool = False, photo: bool = False, sticker: bool = False, gif: bool = False,
                 voice: bool = False, video_note: bool = False, fwd: bool = False, out: bool = False,
                 entities: Any = None, thumbs: Any = None, reply_to_id: Optional[int] = None):
        self.id = mid
        self.reply_to_msg_id = reply_to_id
        self.message = text
        self.raw_text = text
        self.entities = entities
        self.out = out
        self.fwd_from = object() if fwd else None
        self.sender_id = sender
        self.file = file
        self.document = SimpleNamespace(thumbs=thumbs) if file is not None else None
        self.video = object() if video else None
        self.audio = object() if audio else None
        self.photo = object() if photo else None
        self.sticker = object() if sticker else None
        self.gif = object() if gif else None
        self.voice = object() if voice else None
        self.video_note = object() if video_note else None
        self.media = object() if (file is not None or photo) else None


def document_message(name: str = "Movie.mkv", size: int = 5 * 1024 * 1024, *, mid: int = 10, caption: str = "",
                     video: bool = True, mime: str = "video/x-matroska", thumbs: Any = None) -> FakeMessage:
    return FakeMessage(caption, mid=mid, file=make_file(name, size, mime), video=video, thumbs=thumbs)


class FakeEvent:
    """A ``NewMessage`` event."""

    def __init__(self, sender: int, text: str = "", *, message: Optional[FakeMessage] = None, reply: Any = None,
                 mid: int = 1, chat: Optional[int] = None, private: bool = True, photo_bytes: Optional[bytes] = None,
                 entities: Any = None, reply_to_id: Optional[int] = None):
        self.message = message if message is not None else FakeMessage(
            text, mid=mid, sender=sender, entities=entities, reply_to_id=reply_to_id
        )
        self.message.sender_id = sender
        self.sender_id = sender
        self.chat_id = sender if chat is None else chat
        self.is_private = private
        self.id = self.message.id
        self.raw_text = self.message.raw_text
        self.pattern_match = None
        self._reply = reply
        self.out: List[tuple] = []  # every respond()/reply(): (text, buttons, kwargs)
        self.deleted = False
        self.photo_bytes = photo_bytes

    async def respond(self, text: Optional[str] = None, **kw: Any) -> SimpleNamespace:
        refuse_share_button(getattr(self, "client", None), kw.get("buttons"))
        self.out.append((text, kw.get("buttons"), kw))
        return SimpleNamespace(id=1000 + len(self.out), text=text)

    reply = respond

    async def get_reply_message(self) -> Any:
        return self._reply

    async def delete(self) -> None:
        self.deleted = True

    async def download_media(self, file: Any = None) -> Optional[bytes]:
        return self.photo_bytes

    @property
    def texts(self) -> List[str]:
        return [t or "" for t, _b, _k in self.out]


class FakeCallback:
    """A ``CallbackQuery`` event."""

    def __init__(self, sender: int, data: str, *, msg_id: int = 500, chat: Optional[int] = None, photo: bool = False):
        self.sender_id = sender
        self.chat_id = sender if chat is None else chat
        self.data = data.encode()
        self.message_id = msg_id
        self.id = 9
        self.query = SimpleNamespace(data=self.data, chat_instance=1, msg_id=msg_id)
        self.edits: List[tuple] = []
        self.edit_kwargs: List[dict] = []  # the other arguments of every edit(), e.g. link_preview
        self.answers: List[tuple] = []
        self.deleted = False
        self._photo = photo
        self.delete_error: Optional[Exception] = None

    async def edit(self, text: Optional[str] = None, **kw: Any) -> None:
        refuse_share_button(getattr(self, "client", None), kw.get("buttons"))
        self.edits.append((text, kw.get("buttons")))
        self.edit_kwargs.append(kw)

    async def answer(self, message: Optional[str] = None, *, alert: bool = False, **kw: Any) -> None:
        self.answers.append((message, alert))

    async def delete(self) -> None:
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted = True

    async def get_message(self) -> SimpleNamespace:
        return SimpleNamespace(photo=object() if self._photo else None)

    @property
    def alerts(self) -> List[str]:
        return [m or "" for m, a in self.answers if a]

    @property
    def toasts(self) -> List[str]:
        return [m or "" for m, a in self.answers if m and not a]


class FakeInlineQuery:
    """An ``InlineQuery`` event.  Its builder is the real one (it works offline), so the cards it makes are real."""

    def __init__(self, sender: int, text: str = ""):
        self.sender_id = sender
        self.chat_id = sender
        self.text = text
        self.id = 77
        from bot.tg import Client

        from .tdfakes import FakeTd

        self.builder = InlineBuilder(Client(FakeTd()))
        self.answers: List[tuple] = []  # (results, kwargs) of every answer()

    async def answer(self, results: Any = None, **kw: Any) -> None:
        self.answers.append((list(results or []), kw))

    @property
    def results(self) -> List[Any]:
        return self.answers[-1][0] if self.answers else []


class FakeClient:
    def __init__(self) -> None:
        self.handlers: List[tuple] = []
        self.parse_mode = None
        self.session = SimpleNamespace(dc_id=1)
        self.sent: List[tuple] = []     # send_message(entity, text, **kw)
        self.files: List[tuple] = []    # send_file(entity, file, **kw)
        self.edited: List[tuple] = []   # edit_message(entity, text, **kw)
        self.requests: List[Any] = []   # raw requests (none: the bot uses the methods below)
        self.commands: List[Any] = []   # the bot command menus set_bot_commands() was given
        self.downloaded_to: List[tuple] = []  # download_to(message, dest, max_bytes)
        self.file_data: dict = {}       # message id -> bytes that download_to() writes
        self.pings = 0
        self.bad_entities: set = set()
        self.fail_send_file = False
        self.messages: dict = {}        # id -> message that get_messages() can find (a missing id comes back as None)
        self.deleted: List[tuple] = []  # delete_messages(entity, ids)
        self.file_errors: dict = {}     # message id -> exceptions send_file() raises for it, one per call
        self.get_calls: List[tuple] = []
        self.media: dict = {}           # message id -> bytes that download_media() returns
        self.thumb_data: dict = {}      # message id -> bytes that download_media(thumb=...) returns
        self.downloads: List[tuple] = []
        self.username = "RenameBot"
        self.inline_placeholder: Optional[str] = "Pick a format"  # what the bot's profile says; None = inline mode is off
        self.server_inline = True  # False: Telegram refuses messages that carry a share button
        self.me_calls = 0
        self.forwarded: List[tuple] = []  # forward_messages(entity, messages, from_peer, **kw)
        self.forward_errors: List[Exception] = []  # raised by forward_messages(), one per call
        self.message_errors: dict = {}  # entity -> exception that send_message() raises for it
        self.delete_errors: List[Exception] = []  # raised by delete_messages(), one per call

    async def get_me(self) -> SimpleNamespace:
        self.me_calls += 1
        return SimpleNamespace(id=1, username=self.username, bot_inline_placeholder=self.inline_placeholder)

    def add_event_handler(self, callback: Any, event: Any = None) -> None:
        self.handlers.append((callback, event))

    async def set_bot_commands(self, commands: Any) -> None:
        self.commands.append(list(commands))

    async def ping(self) -> float:
        self.pings += 1
        return 12.0

    async def download_to(self, message: Any, dest: Any, *, progress: Any = None, cancel: Any = None, max_bytes: Optional[int] = None) -> int:
        data = self.file_data.get(getattr(message, "id", None), b"")
        data = data if max_bytes is None else data[:max_bytes]
        self.downloaded_to.append((getattr(message, "id", None), dest, max_bytes))
        with open(dest, "wb") as out:
            out.write(data)
        return len(data)

    async def get_input_entity(self, entity: Any) -> Any:
        if entity in self.bad_entities:
            raise ValueError(f"Could not find the input entity for {entity!r}")
        return SimpleNamespace(id=entity)

    async def forward_messages(self, entity: Any, messages: Any, from_peer: Any = None, **kw: Any) -> SimpleNamespace:
        if self.forward_errors:
            raise self.forward_errors.pop(0)
        self.forwarded.append((entity, messages, from_peer, kw))
        return SimpleNamespace(id=4000 + len(self.forwarded))

    async def send_message(self, entity: Any, text: str = "", **kw: Any) -> SimpleNamespace:
        refuse_share_button(self, kw.get("buttons"))
        if entity in self.message_errors:
            raise self.message_errors[entity]
        self.sent.append((entity, text, kw))
        return SimpleNamespace(id=2000 + len(self.sent))

    async def get_messages(self, entity: Any, ids: Any = None, **kw: Any) -> Any:
        self.get_calls.append((entity, ids))
        if isinstance(ids, (list, tuple)):
            return [self.messages.get(i) for i in ids]
        return self.messages.get(ids)

    async def download_media(self, message: Any, file: Any = None, thumb: Any = None, **kw: Any) -> Any:
        self.downloads.append((getattr(message, "id", None), file, thumb))
        source = self.thumb_data if thumb is not None else self.media
        return source.get(getattr(message, "id", None))

    async def delete_messages(self, entity: Any, ids: Any, **kw: Any) -> None:
        if self.delete_errors:
            raise self.delete_errors.pop(0)
        self.deleted.append((entity, list(ids) if isinstance(ids, (list, tuple)) else [ids]))

    async def send_file(self, entity: Any, file: Any, **kw: Any) -> SimpleNamespace:
        refuse_share_button(self, kw.get("buttons"))
        if self.fail_send_file:
            raise RuntimeError("cannot send file")
        errors_for = self.file_errors.get(getattr(file, "id", None))
        if errors_for:
            raise errors_for.pop(0)
        self.files.append((entity, file, kw))
        return SimpleNamespace(id=3000 + len(self.files))

    async def edit_message(self, entity: Any, message: Any = None, text: Optional[str] = None, **kw: Any) -> None:
        if text is None and isinstance(message, str):
            text, message = message, None
        refuse_share_button(self, kw.get("buttons"))
        self.edited.append((entity, text, {**kw, "message": message}))


def _family(obj: Any) -> str:
    """Each kind of event goes to the matching builders: callback queries, inline queries, new messages."""
    if isinstance(obj, (events.CallbackQuery, FakeCallback)):
        return "callback"
    if isinstance(obj, (events.InlineQuery, FakeInlineQuery)):
        return "inline"
    return "message"


async def dispatch(client: FakeClient, event: Any) -> None:
    """Run ``event`` through the registered handlers like the real dispatcher."""
    event.client = client
    for callback, builder in list(client.handlers):
        if _family(builder) != _family(event):
            continue
        passed = builder.accepts(event)
        if inspect.isawaitable(passed):
            passed = await passed
        if not passed:
            continue
        try:
            await callback(event)
        except events.StopPropagation:
            break


def button_data(button: Any) -> str:
    """The callback data of a button; ``inline:<query>`` for a share button and ``url:<link>`` for a link."""
    kind = button.type
    if isinstance(kind, types.InlineButtonTypeSwitchInline):
        return "inline:" + kind.query
    if isinstance(kind, types.InlineButtonTypeUrl):
        return "url:" + kind.url
    return kind.data.decode()


def button_rows(buttons: Any) -> List[List[tuple]]:
    """Buttons -> ``[[(text, data_str), ...], ...]``."""
    out = []
    for row in buttons or []:
        out.append([(b.text, button_data(b)) for b in row])
    return out


def flat_buttons(buttons: Any) -> List[tuple]:
    return [b for row in button_rows(buttons) for b in row]


def variables_line(html_text: str) -> Tuple[str, List[str]]:
    """The "Variables: …" line of a message as Telegram receives it (parsed by the real HTML parser): its text, and
    the ``<code>`` blocks on it.  Tapping a block copies exactly its text, so these are the things a user can copy."""
    plain, entities = telegram_html.parse(html_text)
    units, start = add_surrogate(plain), 0  # entity offsets count UTF-16 units
    for line in units.split("\n"):
        end = start + len(line)
        if re.match(r"(?:Template v|V)ariables: ", line):
            blocks = [del_surrogate(units[e.offset:e.offset + e.length]) for e in entities
                      if isinstance(e, MessageEntityCode) and start <= e.offset < end]
            return del_surrogate(line), blocks
        start = end + 1
    raise AssertionError("no Variables line in: " + plain)
