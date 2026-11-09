"""Text entities and file attributes, in the shapes the bot's code was written against.

An entity says "this part of the text is bold / a link / a mention ...": ``offset`` and ``length`` count UTF-16 code units, which is
how Telegram counts, so an emoji outside the basic plane takes two.  The classes are named ``MessageEntityBold`` ...,
and TDLib's ``textEntity`` objects are converted to and from them at the edge (``from_td`` / ``to_td``).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional


# --------------------------------------------------------------------------- surrogate pairs
def add_surrogate(text: str) -> str:
    """The text with every character outside the basic plane written as the two UTF-16 halves it is counted as."""
    return "".join(
        "".join(chr(y) for y in struct.unpack("<HH", x.encode("utf-16le"))) if 0x10000 <= ord(x) <= 0x10FFFF else x for x in text
    )


def del_surrogate(text: str) -> str:
    return text.encode("utf-16", "surrogatepass").decode("utf-16")


def within_surrogate(text: str, index: int) -> bool:
    """Is ``index`` in the middle of a surrogate pair?"""
    return 1 < index < len(text) and "\ud800" <= text[index - 1] <= "\udbff" and "\ud800" <= text[index] <= "\udfff"


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


# --------------------------------------------------------------------------- entities
@dataclass
class MessageEntity:
    offset: int = 0
    length: int = 0


class MessageEntityBold(MessageEntity):
    pass


class MessageEntityItalic(MessageEntity):
    pass


class MessageEntityUnderline(MessageEntity):
    pass


class MessageEntityStrike(MessageEntity):
    pass


class MessageEntityCode(MessageEntity):
    pass


class MessageEntitySpoiler(MessageEntity):
    pass


@dataclass
class MessageEntityPre(MessageEntity):
    language: str = ""


@dataclass
class MessageEntityBlockquote(MessageEntity):
    collapsed: bool = False


@dataclass
class MessageEntityTextUrl(MessageEntity):
    url: str = ""


@dataclass
class MessageEntityMentionName(MessageEntity):
    user_id: int = 0


@dataclass
class MessageEntityCustomEmoji(MessageEntity):
    document_id: int = 0


# what Telegram finds in a text by itself
class MessageEntityMention(MessageEntity):
    pass


class MessageEntityHashtag(MessageEntity):
    pass


class MessageEntityCashtag(MessageEntity):
    pass


class MessageEntityBotCommand(MessageEntity):
    pass


class MessageEntityUrl(MessageEntity):
    pass


class MessageEntityEmail(MessageEntity):
    pass


class MessageEntityPhone(MessageEntity):
    pass


class MessageEntityBankCard(MessageEntity):
    pass


_TD_TO_ENTITY: Dict[str, Any] = {
    "textEntityTypeMention": MessageEntityMention,
    "textEntityTypeHashtag": MessageEntityHashtag,
    "textEntityTypeCashtag": MessageEntityCashtag,
    "textEntityTypeBotCommand": MessageEntityBotCommand,
    "textEntityTypeUrl": MessageEntityUrl,
    "textEntityTypeEmailAddress": MessageEntityEmail,
    "textEntityTypePhoneNumber": MessageEntityPhone,
    "textEntityTypeBankCardNumber": MessageEntityBankCard,
    "textEntityTypeBold": MessageEntityBold,
    "textEntityTypeItalic": MessageEntityItalic,
    "textEntityTypeUnderline": MessageEntityUnderline,
    "textEntityTypeStrikethrough": MessageEntityStrike,
    "textEntityTypeSpoiler": MessageEntitySpoiler,
    "textEntityTypeCode": MessageEntityCode,
}

_ENTITY_TO_TD: Dict[Any, str] = {cls: name for name, cls in _TD_TO_ENTITY.items()}


def from_td(entities: Optional[Iterable[Dict[str, Any]]]) -> List[MessageEntity]:
    """The entities of a TDLib ``formattedText`` (its ``entities`` list of ``textEntity`` dicts).  Kinds the bot has no use for
    (media timestamps, ...) are left out."""
    out: List[MessageEntity] = []
    for item in entities or []:
        kind = item.get("type") or {}
        name = kind.get("@type")
        at = {"offset": int(item.get("offset") or 0), "length": int(item.get("length") or 0)}
        if name in _TD_TO_ENTITY:
            out.append(_TD_TO_ENTITY[name](**at))
        elif name == "textEntityTypePre":
            out.append(MessageEntityPre(**at))
        elif name == "textEntityTypePreCode":
            out.append(MessageEntityPre(language=str(kind.get("language") or ""), **at))
        elif name in ("textEntityTypeBlockQuote", "textEntityTypeExpandableBlockQuote"):
            out.append(MessageEntityBlockquote(collapsed=name == "textEntityTypeExpandableBlockQuote", **at))
        elif name == "textEntityTypeTextUrl":
            out.append(MessageEntityTextUrl(url=str(kind.get("url") or ""), **at))
        elif name == "textEntityTypeMentionName":
            out.append(MessageEntityMentionName(user_id=int(kind.get("user_id") or 0), **at))
        elif name == "textEntityTypeCustomEmoji":
            out.append(MessageEntityCustomEmoji(document_id=int(kind.get("custom_emoji_id") or 0), **at))
    return out


def to_td(entities: Optional[Iterable[MessageEntity]]) -> List[Dict[str, Any]]:
    """TDLib ``textEntity`` dicts for entities; kinds that cannot be sent (a custom emoji, an unknown one) are left out."""
    out: List[Dict[str, Any]] = []
    for e in entities or []:
        kind = type(e)
        if kind in _ENTITY_TO_TD:
            td_type: Dict[str, Any] = {"@type": _ENTITY_TO_TD[kind]}
        elif kind is MessageEntityPre:
            td_type = {"@type": "textEntityTypePreCode", "language": e.language} if e.language else {"@type": "textEntityTypePre"}
        elif kind is MessageEntityBlockquote:
            td_type = {"@type": "textEntityTypeExpandableBlockQuote" if e.collapsed else "textEntityTypeBlockQuote"}
        elif kind is MessageEntityTextUrl:
            td_type = {"@type": "textEntityTypeTextUrl", "url": e.url}
        elif kind is MessageEntityMentionName:
            td_type = {"@type": "textEntityTypeMentionName", "user_id": e.user_id}
        else:
            continue
        out.append({"@type": "textEntity", "offset": e.offset, "length": e.length, "type": td_type})
    return out


def formatted_text(text: str, entities: Optional[Iterable[MessageEntity]] = None) -> Dict[str, Any]:
    """A TDLib ``formattedText`` for ``text`` and its entities."""
    return {"@type": "formattedText", "text": text or "", "entities": to_td(entities)}


# --------------------------------------------------------------------------- file attributes
@dataclass
class DocumentAttributeFilename:
    file_name: str = ""


@dataclass
class DocumentAttributeVideo:
    duration: float = 0
    w: int = 0
    h: int = 0
    round_message: bool = False
    supports_streaming: bool = False


@dataclass
class DocumentAttributeAudio:
    duration: int = 0
    voice: bool = False
    title: Optional[str] = None
    performer: Optional[str] = None


class DocumentAttributeAnimated:
    pass


@dataclass
class PhotoSize:
    """A real preview of a file."""

    type: str = ""
    w: int = 0
    h: int = 0


class PhotoStrippedSize(PhotoSize):
    """The tiny blurred preview that comes with every file (a few hundred bytes)."""


@dataclass
class BotCommand:
    command: str = ""
    description: str = ""


# --------------------------------------------------------------------------- inline keyboards
@dataclass
class InlineButtonTypeCallback:
    """A button that sends its ``data`` back to the bot when it is tapped."""

    data: bytes = b""


@dataclass
class InlineButtonTypeUrl:
    url: str = ""


@dataclass
class InlineButtonTypeSwitchInline:
    """A button that opens Telegram's chat picker and types ``@bot query`` in the chosen chat."""

    query: str = ""
    same_peer: bool = False


@dataclass
class KeyboardInlineButton:
    text: str = ""
    type: Any = None


@dataclass
class KeyboardInlineButtonRow:
    buttons: List[KeyboardInlineButton]


@dataclass
class ReplyInlineMarkup:
    rows: List[KeyboardInlineButtonRow]
