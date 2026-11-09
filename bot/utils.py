"""Small formatting helpers."""
from __future__ import annotations

import html
from typing import Iterable


def esc(value: object) -> str:
    return html.escape(str(value), quote=False)


def code_each(items: Iterable[object]) -> str:
    """``<code>a</code> <code>b</code>``: one block per item.  Telegram copies a whole ``<code>`` block when it is tapped,
    so a list inside a single block can't be copied item by item."""
    return " ".join(f"<code>{esc(item)}</code>" for item in items)


def human_size(num: float) -> str:
    num = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024.0 or unit == "TB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.2f} {unit}"
        num /= 1024.0
    return f"{num:.2f} TB"


def human_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def human_uptime(seconds: float) -> str:
    seconds = int(max(0, seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if d or h:
        parts.append(f"{h}h")
    parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)


def progress_bar(fraction: float, width: int = 14) -> str:
    fraction = min(1.0, max(0.0, fraction))
    filled = int(round(fraction * width))
    return "█" * filled + "░" * (width - filled)


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"
