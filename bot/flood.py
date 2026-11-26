"""How the bot stays under Telegram's limits.

TDLib moves the bytes of a file by itself and cannot be told to slow down, so the bot does it where it can: it lets only a few big
transfers run at once (however many jobs there are), spaces the edits of progress messages, and when Telegram says "too many
requests" it holds back new work until that is over.  A flood that was met by a transfer also lowers how many run at once, and the
limit comes back one step at a time after a quiet minute.  Everything that talks to Telegram shares one ``FloodControl``
(``Client.flood``).

The flood is Telegram's limit for the bot's address, not for one user: a second user's files count against the same limit.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
import weakref
from collections import deque
from typing import Any, AsyncIterator, Callable, Deque, Dict, Optional

from .errors import Cancelled

log = logging.getLogger(__name__)

SLACK = 0.5  # seconds added to what Telegram asks for
MAX_COOL = 30.0  # the longest all new work is held back for one flood wait (a longer one belongs to one request, not to the whole bot)
SERIOUS = 5.0  # a flood wait this long, or one a transfer met, means the connection is overloaded: fewer transfers at once
EPISODE = 10.0  # flood waits closer together than this (seconds) are one flood: together they lower the limits once
QUIET = 60.0  # seconds without a serious flood before the limits come back one step
STAGGER = 2.0  # when a flood is over, the transfers waiting for it start up to this many seconds apart
ROUNDS = 6  # how often the end of a flood is looked at again after waiting for it (another one may have come meanwhile)
EDIT_GAP = 1.1  # seconds between two edits in one chat
PROGRESS_SHARE = 2.5  # seconds of one chat's edits each running transfer may use: three running means a progress message every 7.5 s


class Slots:
    """Places for transfers: at most ``size`` at a time, given out in the order they were asked for.  ``size`` may change while they are in use."""

    def __init__(self, size: int):
        self.size = max(1, size)
        self.used = 0
        self._line: Deque["asyncio.Future[None]"] = deque()

    @property
    def waiting(self) -> int:
        return sum(1 for turn in self._line if not turn.done())

    @property
    def crowded(self) -> bool:
        """Would one more transfer have to wait for its turn?"""
        return self.used >= self.size or self.waiting > 0

    def resize(self, size: int) -> None:
        self.size = max(1, size)
        self._serve()

    def _serve(self) -> None:
        while self._line and self.used < self.size:
            turn = self._line.popleft()
            if not turn.done():
                self.used += 1
                turn.set_result(None)

    def release(self) -> None:
        self.used -= 1
        self._serve()

    def _leave(self, turn: "asyncio.Future[None]") -> None:
        """Give up the turn: out of the line, or, when it had been given a place already, the place goes back."""
        if turn.done() and not turn.cancelled():
            self.release()
            return
        turn.cancel()
        with contextlib.suppress(ValueError):
            self._line.remove(turn)

    async def acquire(self, cancel: Optional[asyncio.Event] = None) -> None:
        """Wait for a place.  ``Cancelled`` when ``cancel`` is set first (the place is not taken then)."""
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        turn: "asyncio.Future[None]" = asyncio.get_running_loop().create_future()
        self._line.append(turn)
        self._serve()
        waiter = asyncio.ensure_future(cancel.wait()) if cancel is not None else None
        try:
            await asyncio.wait({turn, waiter} if waiter is not None else {turn}, return_when=asyncio.FIRST_COMPLETED)
        except BaseException:  # whoever waits was cancelled
            self._leave(turn)
            raise
        finally:
            if waiter is not None:
                waiter.cancel()
        if turn.done() and not turn.cancelled() and not (cancel is not None and cancel.is_set()):
            return
        self._leave(turn)
        raise Cancelled()


class FloodControl:
    """What the bot knows about Telegram's patience.  See the module's text; ``clock``, ``sleep`` and ``jitter`` are there for the tests."""

    def __init__(self, *, uploads: int = 1, downloads: int = 2, edit_gap: float = EDIT_GAP, clock: Optional[Callable[[], float]] = None,
                 sleep: Optional[Callable[[float], Any]] = None, jitter: Optional[Callable[[], float]] = None):
        self.max_uploads = max(1, uploads)
        self.max_downloads = max(1, downloads)
        self.edit_gap = edit_gap
        self._clock = clock or time.monotonic
        self._sleep = sleep
        self._jitter = jitter or random.random
        self.uploads = Slots(self.max_uploads)
        self.downloads = Slots(self.max_downloads)
        self.hits = 0  # flood waits met since the start
        self._until = 0.0  # no new work is started before this time
        self._level = 0  # how many steps the limits are lowered by
        self._episode = -1e9  # when the last flood that lowered the limits began
        self._serious = -1e9  # when the last serious flood was met
        self._next_edit: Dict[int, float] = {}
        self._watching: Dict[int, "weakref.WeakSet[Any]"] = {}

    # ------------------------------------------------------------------ the end of a flood
    def hit(self, seconds: float = 0.0, *, transfer: bool = False) -> None:
        """Telegram asked for ``seconds`` of quiet (``transfer``: a download or an upload met it).  Until that is over nothing new starts."""
        now = self._clock()
        seconds = max(0.0, float(seconds))
        self.hits += 1
        self._until = max(self._until, now + min(seconds + SLACK, MAX_COOL))
        if not (transfer or seconds >= SERIOUS):  # a flood wait on an edit is the chat's limit, not the connection's
            return
        self._serious = now
        if now - self._episode >= EPISODE:  # the first flood wait of a flood lowers the limits; the ones that follow it do not
            self._episode = now
            if self._level < self._steps:
                self._level += 1
                self._fit()
                log.warning("Telegram is rate limiting the bot: holding back new transfers for %.0fs, now %d upload(s) and %d download(s) at once",
                            min(seconds + SLACK, MAX_COOL), self.uploads.size, self.downloads.size)

    @property
    def _steps(self) -> int:
        return max(self.max_uploads, self.max_downloads) - 1

    def _fit(self) -> None:
        self.uploads.resize(self.max_uploads - min(self._level, self.max_uploads - 1))
        self.downloads.resize(self.max_downloads - min(self._level, self.max_downloads - 1))

    def _recover(self) -> None:
        now = self._clock()
        if self._level and now - self._serious >= QUIET:
            self._level -= 1
            self._serious = now
            self._fit()
            log.info("Telegram has been quiet for a while: %d upload(s) and %d download(s) at once", self.uploads.size, self.downloads.size)

    def cooling_left(self) -> float:
        """Seconds until the last flood is over (0 when there is none)."""
        return max(0.0, self._until - self._clock())

    @property
    def cooling(self) -> bool:
        return self.cooling_left() > 0

    async def _wait(self, seconds: float, cancel: Optional[asyncio.Event]) -> None:
        if seconds <= 0:
            return
        sleep = self._sleep or asyncio.sleep
        if cancel is None:
            await sleep(seconds)
            return
        sleeper, waiter = asyncio.ensure_future(sleep(seconds)), asyncio.ensure_future(cancel.wait())
        try:
            await asyncio.wait({sleeper, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            sleeper.cancel()
            waiter.cancel()
        if cancel.is_set():
            raise Cancelled()

    async def calm(self, cancel: Optional[asyncio.Event] = None) -> None:
        """Wait until the last flood is over (at once when there is none).  Several callers leave a moment apart, not all together."""
        waited = False
        for _ in range(ROUNDS):
            left = self.cooling_left()
            if left <= 0:
                break
            waited = True
            await self._wait(left, cancel)
        if waited:
            await self._wait(self._jitter() * STAGGER, cancel)

    # ------------------------------------------------------------------ big transfers
    async def _enter(self, slots: Slots, cancel: Optional[asyncio.Event]) -> None:
        self._recover()
        await slots.acquire(cancel)
        try:
            await self.calm(cancel)
        except BaseException:
            slots.release()
            raise

    @contextlib.asynccontextmanager
    async def download(self, cancel: Optional[asyncio.Event] = None) -> AsyncIterator[None]:
        """A place among the downloads, once no flood is on.  ``Cancelled`` when ``cancel`` is set while waiting."""
        await self._enter(self.downloads, cancel)
        try:
            yield
        finally:
            self.downloads.release()

    @contextlib.asynccontextmanager
    async def upload(self, cancel: Optional[asyncio.Event] = None) -> AsyncIterator[None]:
        await self._enter(self.uploads, cancel)
        try:
            yield
        finally:
            self.uploads.release()

    # ------------------------------------------------------------------ messages
    async def pace(self, chat_id: int, cancel: Optional[asyncio.Event] = None) -> None:
        """Wait for this chat's turn to edit a message: one edit per ``edit_gap`` seconds, in the order they were asked for."""
        now = self._clock()
        turn = max(now, self._next_edit.get(chat_id, 0.0))
        self._next_edit[chat_id] = turn + self.edit_gap
        if len(self._next_edit) > 256:
            self._next_edit = {chat: when for chat, when in self._next_edit.items() if when > now}
        await self._wait(turn - now, cancel)

    def watch(self, chat_id: int, reporter: Any) -> None:
        """``reporter`` edits a progress message in ``chat_id`` until it is let go (by ``unwatch``, or when nothing refers to it any more)."""
        self._watching.setdefault(chat_id, weakref.WeakSet()).add(reporter)

    def unwatch(self, chat_id: int, reporter: Any) -> None:
        watchers = self._watching.get(chat_id)
        if watchers is not None:
            watchers.discard(reporter)
            if not watchers:
                del self._watching[chat_id]

    def progress_interval(self, chat_id: int, base: float) -> float:
        """Seconds between two edits of one progress message: ``base``, or more when several share a chat or Telegram has been busy."""
        self._recover()
        watchers = self._watching.get(chat_id)
        if watchers is not None and not watchers:  # every reporter of the chat was dropped without being let go
            del self._watching[chat_id]
        shared = len(watchers or ()) * PROGRESS_SHARE
        return max(base, shared) * (2 if self._level else 1)

    def describe(self) -> str:
        """One line for /status."""
        state = f"holding back for {self.cooling_left():.0f}s" if self.cooling else "no flood"
        waits = f"{self.hits} flood wait{'' if self.hits == 1 else 's'}"
        return (f"{self.uploads.used}/{self.uploads.size} uploads · {self.downloads.used}/{self.downloads.size} downloads · "
                f"{state} · {waits} since the start")
