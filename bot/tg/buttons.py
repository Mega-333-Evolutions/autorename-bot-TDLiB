"""Inline keyboards: ``Button.inline`` / ``url`` / ``switch_inline`` make the buttons, ``markup`` turns rows of them into the
``replyMarkupInlineKeyboard`` TDLib wants, and ``read_markup`` turns the keyboard of a message back into the same kind of objects."""
from __future__ import annotations

import base64
from typing import Any, Dict, Iterable, List, Optional, Union

from .entities import (
    InlineButtonTypeCallback, InlineButtonTypeSwitchInline, InlineButtonTypeUrl, KeyboardInlineButton, KeyboardInlineButtonRow,
    ReplyInlineMarkup,
)


class Button:
    @staticmethod
    def inline(text: str, data: Union[bytes, str, None] = None) -> KeyboardInlineButton:
        raw = data if isinstance(data, bytes) else (data or text).encode()
        return KeyboardInlineButton(text, InlineButtonTypeCallback(raw))

    @staticmethod
    def url(text: str, url: str) -> KeyboardInlineButton:
        return KeyboardInlineButton(text, InlineButtonTypeUrl(url))

    @staticmethod
    def switch_inline(text: str, query: str = "", same_peer: bool = False) -> KeyboardInlineButton:
        return KeyboardInlineButton(text, InlineButtonTypeSwitchInline(query, same_peer))


def _button(b: Any) -> Dict[str, Any]:
    kind = getattr(b, "type", None)
    if isinstance(kind, InlineButtonTypeCallback):
        td_type: Dict[str, Any] = {"@type": "inlineKeyboardButtonTypeCallback", "data": base64.b64encode(kind.data).decode()}
    elif isinstance(kind, InlineButtonTypeUrl):
        td_type = {"@type": "inlineKeyboardButtonTypeUrl", "url": kind.url}
    elif isinstance(kind, InlineButtonTypeSwitchInline):
        target: Dict[str, Any] = (
            {"@type": "targetChatCurrent"} if kind.same_peer else
            {"@type": "targetChatChosen", "types": {
                "@type": "targetChatTypes", "allow_user_chats": True, "allow_bot_chats": True,
                "allow_group_chats": True, "allow_channel_chats": True}}
        )
        td_type = {"@type": "inlineKeyboardButtonTypeSwitchInline", "query": kind.query, "target_chat": target}
    else:
        raise TypeError(f"not a button: {b!r}")
    return {"@type": "inlineKeyboardButton", "text": b.text, "type": td_type}


def markup(buttons: Optional[Iterable[Iterable[Any]]]) -> Optional[Dict[str, Any]]:
    """The reply markup for rows of buttons; ``None`` when there is no button."""
    rows: List[List[Dict[str, Any]]] = [[_button(b) for b in row] for row in (buttons or [])]
    rows = [r for r in rows if r]
    return {"@type": "replyMarkupInlineKeyboard", "rows": rows} if rows else None


def read_markup(td_markup: Optional[Dict[str, Any]]) -> Optional[ReplyInlineMarkup]:
    """The inline keyboard of a TDLib message as ``ReplyInlineMarkup`` (``None`` when it has none).  Buttons of kinds the bot never makes
    keep their text and have no type."""
    if not td_markup or td_markup.get("@type") != "replyMarkupInlineKeyboard":
        return None
    rows = []
    for td_row in td_markup.get("rows") or []:
        buttons = []
        for item in td_row or []:
            kind = (item or {}).get("type") or {}
            name = kind.get("@type")
            if name == "inlineKeyboardButtonTypeUrl":
                typed: Any = InlineButtonTypeUrl(str(kind.get("url") or ""))
            elif name == "inlineKeyboardButtonTypeCallback":
                data = kind.get("data")
                typed = InlineButtonTypeCallback(base64.b64decode(data) if isinstance(data, str) else bytes(data or b""))
            elif name == "inlineKeyboardButtonTypeSwitchInline":
                typed = InlineButtonTypeSwitchInline(str(kind.get("query") or ""), (kind.get("target_chat") or {}).get("@type") == "targetChatCurrent")
            else:
                typed = None
            buttons.append(KeyboardInlineButton(str(item.get("text") or ""), typed))
        rows.append(KeyboardInlineButtonRow(buttons))
    return ReplyInlineMarkup(rows)
