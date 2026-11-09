"""Telegram's data centers (DCs): the one a file is stored on, the one the bot is connected to, and where they are."""
from __future__ import annotations

from typing import Any, Optional

PLACES = {1: "Miami", 2: "Amsterdam", 3: "Miami", 4: "Amsterdam", 5: "Singapore"}  # where Telegram keeps its data centers


def _valid(dc_id: Any) -> Optional[int]:
    return dc_id if isinstance(dc_id, int) and dc_id > 0 else None


def of_message(message: Any) -> Optional[int]:
    """The DC the file (or photo) of a message is stored on; ``None`` when it has neither, or Telegram doesn't say."""
    for kind in ("document", "photo"):
        dc_id = _valid(getattr(getattr(message, kind, None), "dc_id", None))
        if dc_id is not None:
            return dc_id
    return None


def home_of(client: Any) -> Optional[int]:
    """The DC the bot itself is connected to: where everything it uploads goes."""
    return _valid(getattr(getattr(client, "session", None), "dc_id", None))


def label(dc_id: int, place: bool = False) -> str:
    """``DC 4``; with ``place``, ``DC 4 · Amsterdam`` (a DC that is not one of the five stays ``DC 9``)."""
    where = PLACES.get(dc_id) if place else None
    return f"DC {dc_id} · {where}" if where else f"DC {dc_id}"
