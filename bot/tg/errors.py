"""The errors Telegram answers with, as exceptions the bot can catch by name.

TDLib returns an error as ``{"@type": "error", "code": 400, "message": "..."}``; ``from_td`` turns one into the exception that
the handlers expect (``MessageNotModifiedError``, ``FloodWaitError`` ...), and any other error into a plain ``RPCError``.
"""
from __future__ import annotations

import re
from typing import Optional

from ..errors import Cancelled


class RPCError(Exception):
    """An error answered by Telegram (or by TDLib, which checks a request before Telegram sees it)."""

    def __init__(self, message: str = "", code: int = 400, request: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.request = request

    def __str__(self) -> str:
        return f"{self.message} ({self.code})" if self.message else f"RPC error {self.code}"


class MessageNotModifiedError(RPCError):
    """An edit that would leave the message exactly as it is."""


class FloodWaitError(RPCError):
    """Telegram asks to wait ``seconds`` before this request is made again."""

    def __init__(self, seconds: int, message: str = "", code: int = 429, request: Optional[str] = None):
        super().__init__(message or f"A wait of {seconds} seconds is required", code, request)
        self.seconds = int(seconds)


class BotInlineDisabledError(RPCError):
    """The bot has inline mode off, and a message with a switch-inline button was refused."""


class MessageTooLongError(RPCError):
    """The text of a message is longer than Telegram allows."""


class MediaCaptionTooLongError(RPCError):
    """The caption of a file is longer than Telegram allows."""


class EntityBoundsInvalidError(RPCError):
    """A text entity (bold, link ...) does not fit the text it belongs to."""


_RETRY_AFTER = re.compile(r"retry after (\d+)", re.I)
_FLOOD_WAIT = re.compile(r"FLOOD_WAIT_(\d+)", re.I)


def from_td(code: int, message: str, request: Optional[str] = None) -> RPCError:
    """The exception for a TDLib ``error`` object."""
    code = int(code or 0)
    message = str(message or "")
    low = message.lower()
    wait = _RETRY_AFTER.search(message) or _FLOOD_WAIT.search(message)
    if code == 429 or wait:
        return FloodWaitError(int(wait.group(1)) if wait else 1, message, code or 429, request)
    if "message is not modified" in low or "message_not_modified" in low:
        return MessageNotModifiedError(message, code, request)
    if "bot_inline_disabled" in low or "inline mode" in low and "disabled" in low:
        return BotInlineDisabledError(message, code, request)
    if "caption" in low and "too long" in low or "media_caption_too_long" in low:
        return MediaCaptionTooLongError(message, code, request)
    if "message is too long" in low or "message_too_long" in low or "text is too long" in low:
        return MessageTooLongError(message, code, request)
    if "entity_bounds_invalid" in low or "entit" in low and ("bound" in low or "parse" in low or "wrong" in low or "invalid" in low):
        return EntityBoundsInvalidError(message, code, request)
    return RPCError(message, code, request)


TransferCancelled = Cancelled  # a download or an upload the user stopped: the bot's one "cancelled" exception
