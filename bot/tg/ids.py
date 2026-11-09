"""Ids: the message ids the bot works with and TDLib's, and the data center a file lives on.

TDLib numbers a message ``server_id << 20`` (the low bits tell a sent message from a local or a scheduled one).  The bot keeps the
plain server id everywhere - in its database, in the notices it exchanges with the channel post bot, in the links it builds - so
they are the numbers Telegram's own apps show (and the ones a database written by an earlier version of the bot holds).  Chat ids need no change: a user is its id, a group ``-id`` and a channel
``-100id`` in both.
"""
from __future__ import annotations

import base64
import struct
from typing import Optional

_SHIFT = 20
_LOW = (1 << _SHIFT) - 1


def to_td_message_id(message_id: int) -> int:
    """The id TDLib uses for message ``message_id`` (the plain id the bot works with)."""
    return int(message_id) << _SHIFT


def from_td_message_id(td_id: int) -> int:
    """The plain id of a message TDLib calls ``td_id``.  An id without the server pattern (a message that is still being
    sent) is returned unchanged: it has no plain id yet."""
    td_id = int(td_id)
    return td_id >> _SHIFT if td_id and td_id & _LOW == 0 else td_id


def _unrle(data: bytes) -> bytes:
    out = bytearray()
    zeros = False
    for byte in data:
        if byte == 0:
            zeros = True
            continue
        if zeros:
            out.extend(b"\0" * byte)
            zeros = False
        else:
            out.append(byte)
    return bytes(out)


def dc_of_remote_id(remote_id: str) -> Optional[int]:
    """The data center a file is stored on, read from its persistent id (the ``file_id`` a bot sees: TDLib's ``remote.id``).

    The id is URL-safe base64 of a zero-run-length-coded record that starts with two little-endian ints, the file type with some flag
    bits and the data center, and ends with the version of the format.  ``None`` when the id is empty or is not one.
    """
    if not remote_id or not isinstance(remote_id, str):
        return None
    try:
        raw = _unrle(base64.urlsafe_b64decode(remote_id + "=" * (-len(remote_id) % 4)))
    except (ValueError, TypeError):
        return None
    if len(raw) < 10:
        return None
    _kind, dc = struct.unpack("<ii", raw[:8])
    return dc if 0 < dc < 1000 else None
