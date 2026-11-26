"""Throttled progress messages (speed, ETA, cancel button)."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from .tg import Button, errors
from .utils import clip, esc, human_duration, human_size, progress_bar

log = logging.getLogger(__name__)


class ProgressReporter:
    """Callable ``(current, total)`` that edits a status message at most every ``interval`` seconds.

    With a ``client.flood`` (see ``bot.flood``) the edits spare Telegram: the interval grows when several files share a chat or Telegram
    has been busy, no tick is sent while a flood is on, and the edits wait for their turn in the chat."""

    def __init__(self, client: Any, message: Any, job_id: int, name: str, interval: float = 4.0):
        self.client = client
        self.message = message
        self.job_id = job_id
        self.name = name
        self.interval = interval
        self.stage = ""
        self.dc = ""  # where the file comes from ("DC 4"), shown beside the stage while it downloads
        self.note = ""  # shown instead of "Working…" by a stage without a total
        self.flood = getattr(client, "flood", None)
        self.chat_id = getattr(message, "chat_id", None)
        self._shown = ""  # the text of the last edit asked for
        if self.flood is not None and self.chat_id is not None:
            self.flood.watch(self.chat_id, self)
        self._last_edit = 0.0
        self._muted_until = 0.0
        self._inflight: Optional[asyncio.Future] = None
        self._speed = 0.0
        self._last_t = 0.0
        self._last_bytes = 0

    # ---- helpers -------------------------------------------------------------
    def _buttons(self):
        return [[Button.inline("✖ Cancel", f"job:x:{self.job_id}".encode())]]

    def render(self, current: int, total: int) -> str:
        where = f" · {esc(self.dc)}" if self.dc else ""
        head = f"<b>{self.stage}</b>{where}\n<code>{esc(clip(self.name, 120))}</code>\n"
        if total <= 0:
            return head + "\n" + (esc(self.note) or "Working…")
        frac = current / total
        eta = (total - current) / self._speed if self._speed > 0 else 0
        speed = f"{human_size(self._speed)}/s" if self._speed > 0 else "…"
        eta_text = human_duration(eta) if self._speed > 0 else "…"
        return (
            f"{head}\n{progress_bar(frac)} {frac * 100:.1f}%\n"
            f"{human_size(current)} / {human_size(total)} · {speed} · ETA {eta_text}"
        )

    def _interval(self) -> float:
        if self.flood is None or self.chat_id is None:
            return self.interval
        return self.flood.progress_interval(self.chat_id, self.interval)

    async def _edit(self, text: str, buttons: Any, after: Optional[asyncio.Future] = None, quiet: bool = False) -> None:
        """One edit.  ``quiet``: a tick of the progress, which is left out when Telegram says to slow down; any other edit has to get through."""
        if after is not None:  # keep edits in order: wait for the one still in flight
            try:
                await after
            except Exception:  # noqa: BLE001
                pass
        extra = {} if self.flood is None else {"quiet": True} if quiet else {"paced": True}
        try:
            await self.client.edit_message(self.message, text, buttons=buttons, parse_mode="html", **extra)
        except errors.MessageNotModifiedError:
            pass
        except errors.FloodWaitError as exc:
            self._muted_until = time.monotonic() + exc.seconds + 1
        except Exception as exc:  # noqa: BLE001 - progress is best effort
            log.debug("progress edit failed: %s", exc)

    # ---- public API -----------------------------------------------------------
    def set_stage(self, stage: str, total: int = 0, dc: str = "", note: str = "") -> None:
        """Switch stage and show it right away.  ``dc`` (``DC 4``) is shown beside the stage name, for this stage only; ``note`` takes the
        place of "Working…" in a stage that counts nothing."""
        self.stage = stage
        self.dc = dc
        self.note = note
        self._speed = 0.0
        self._last_t = time.monotonic()
        self._last_bytes = 0
        self._last_edit = time.monotonic()
        self._schedule(self.render(0, total), force=True)

    def waiting(self, stage: str, note: str = "") -> None:
        """Show that the file is waiting (for its turn, for a flood to be over, for a new try): the stage and a note, no progress."""
        self.set_stage(stage, 0, note=note)

    def _schedule(self, text: str, force: bool = False) -> None:
        """Queue an edit; ordinary progress ticks are dropped while one is in flight (and when they would not change the message), a stage
        change is not."""
        busy = self._inflight is not None and not self._inflight.done()
        if (busy or text == self._shown) and not force or time.monotonic() < self._muted_until:
            return
        self._shown = text
        self._inflight = asyncio.ensure_future(self._edit(text, self._buttons(), after=self._inflight if busy else None, quiet=not force))

    def __call__(self, current: int, total: int) -> None:
        now = time.monotonic()
        dt = now - self._last_t
        if dt >= 1.0:
            inst = max(0, current - self._last_bytes) / dt
            self._speed = inst if self._speed == 0 else self._speed * 0.6 + inst * 0.4
            self._last_t, self._last_bytes = now, current
        if now - self._last_edit < self._interval() and current < total:
            return
        if self.flood is not None and self.flood.cooling:  # Telegram asked for quiet: this tick is not worth another flood wait
            return
        self._last_edit = now
        self._schedule(self.render(current, total))

    async def flush(self) -> None:
        """Wait until the edit that was asked for last has been made."""
        if self._inflight is not None:
            try:
                await self._inflight
            except Exception:  # noqa: BLE001
                pass

    async def finish(self, text: str) -> None:
        """Final text, buttons removed."""
        await self.flush()
        self._muted_until = 0.0
        if self.flood is not None and self.chat_id is not None:
            self.flood.unwatch(self.chat_id, self)
        await self._edit(text, None)
