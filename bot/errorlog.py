"""Error reports for the log channel.

Every ERROR-level log record becomes one message: what the bot was doing, then the full traceback, inside a Python code
block (Telegram shows it with a "Python" label and a copy button).  An identical error is reported once per couple of
minutes, no more than ``PER_MINUTE`` reports go out in a minute, and secrets are masked before anything is sent.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import re
import time
from collections import deque
from dataclasses import dataclass
from itertools import groupby
from typing import Any, Awaitable, Callable, Deque, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urlsplit

from .utils import clip, esc

log = logging.getLogger(__name__)

Send = Callable[[str], Awaitable[None]]

MAX_TEXT = 3800  # UTF-16 units of the code block: Telegram takes 4096 per message
HEAD_LIMIT = 700  # characters of "what was happening" kept when a traceback shares the message
REPEAT_WINDOW = 120.0  # seconds during which an identical error is reported only once
PER_MINUTE = 10  # reports sent per minute at most

# What the bot is working on right now: text, or a function that returns it.  Errors logged anywhere below it mention
# it, so a failure deep inside a helper still says which message or file it belongs to.
error_context: "contextvars.ContextVar[Any]" = contextvars.ContextVar("error_context", default=None)


_EVENT_NAMES = {"MessageEvent": "NewMessage", "CallbackEvent": "CallbackQuery", "InlineEvent": "InlineQuery"}


def describe_update(event: Any) -> str:
    """One line about an update (a message, a button, an inline query): who sent what, where.  Never raises."""
    try:
        kind = _EVENT_NAMES.get(type(event).__name__, type(event).__name__)  # NewMessage / CallbackQuery / InlineQuery
        parts = [f"sender_id={getattr(event, 'sender_id', None)}", f"chat_id={getattr(event, 'chat_id', None)}"]
        data = getattr(event, "data", None)
        if data is not None:
            parts.append(f"message_id={getattr(event, 'message_id', None)}")
            parts.append("data=" + repr(bytes(data).decode("utf-8", "replace")))
        elif hasattr(event, "builder"):  # an inline query: what was typed after the bot's name
            parts.append("query=" + repr(clip(getattr(event, "text", "") or "", 100)))
        else:
            parts.append(f"message_id={getattr(event, 'id', None)}")
            text = getattr(event, "raw_text", None)
            if text:
                parts.append("text=" + repr(clip(text, 200)))
            file = getattr(getattr(event, "message", None), "file", None)
            if file is not None:
                parts.append(f"file={getattr(file, 'name', None)!r} ({getattr(file, 'mime_type', None)}, {getattr(file, 'size', None)} bytes)")
        return f"{kind}({', '.join(parts)})"
    except Exception:  # noqa: BLE001
        return type(event).__name__


_SQL_PARAMS = re.compile(r"\[parameters: [^\n]*\]")  # SQLAlchemy prints the values it bound, e.g. the saved TDLib state
_TOKENISH = re.compile(r"[A-Za-z0-9_\-+=]{100,}")  # the saved TDLib state is thousands of characters; no path or file name has 100 in a row


def _mask_token(match: "re.Match[str]") -> str:
    text = match.group(0)
    return "***" if re.search(r"[A-Za-z0-9]", text) else text  # a line of dashes is not a secret


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Hide the configured secrets and anything that looks like a session string."""
    for secret in secrets:
        if len(secret) >= 6:
            text = text.replace(secret, "***")
    return _TOKENISH.sub(_mask_token, _SQL_PARAMS.sub("[parameters: …]", text))


def secrets_from(cfg: Any) -> List[str]:
    """The values of the configuration that must never appear in a report."""
    values = [cfg.bot_token, cfg.api_hash, cfg.database_url]
    try:
        values.append(urlsplit(cfg.database_url).password or "")
    except ValueError:
        pass
    return [v for v in values if v]


def _u16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _tail(text: str, units: int) -> str:
    """The end of ``text`` that fits in ``units`` UTF-16 units, starting at the beginning of a line."""
    used, start = 0, len(text)
    for i in range(len(text) - 1, -1, -1):
        used += 2 if ord(text[i]) > 0xFFFF else 1
        if used > units:
            break
        start = i
    tail = text[start:]
    if start > 0:
        newline = tail.find("\n")
        tail = tail[newline + 1:] if newline != -1 else tail
    return tail


def _head(text: str, units: int) -> str:
    """The beginning of ``text`` that fits in ``units`` UTF-16 units, ending at the end of a line."""
    used, end = 0, len(text)
    for i, char in enumerate(text):
        used += 2 if ord(char) > 0xFFFF else 1
        if used > units:
            end = i
            break
    if end == len(text):
        return text
    head = text[:end]
    newline = head.rfind("\n")
    return head[:newline] if newline != -1 else head


_LIBRARY_FRAME = re.compile(r'  File ".*[\\/](?:site-packages|dist-packages|lib[\\/]python[\d.]+)[\\/]')
_CUT = "\n…\n"


def _elide(trace: str, room: int) -> str:
    """Make a traceback fit ``room`` by replacing runs of frames from libraries (SQLAlchemy, pytdbot, asyncio...) with
    one line each: the bot's own frames and the error stay.  Runs of three or more go first, then every run."""
    if _u16(trace) <= room:
        return trace
    blocks: List[List[str]] = []  # a frame is its "File" line and the source lines under it; any other line is a block of its own
    for line in trace.split("\n"):
        if blocks and line.startswith("    ") and blocks[-1][0].startswith("  File "):
            blocks[-1].append(line)
        else:
            blocks.append([line])
    text = trace
    for shortest_run in (3, 1):
        lines: List[str] = []
        for library, group in groupby(blocks, key=lambda block: bool(_LIBRARY_FRAME.match(block[0]))):
            frames = list(group)
            if library and len(frames) >= shortest_run:
                lines.append(f"  [… {len(frames)} library frame{'s' if len(frames) != 1 else ''} …]")
            else:
                lines.extend(line for frame in frames for line in frame)
        text = "\n".join(lines)
        if _u16(text) <= room:
            break
    return text


def _fit(head: str, trace: str) -> str:
    """``head`` + the traceback in at most ``MAX_TEXT``.  What does not fit goes in this order: frames from libraries,
    then the middle of the traceback: its first frames (where it came from) and its end (the error) are what matter."""
    if not trace:
        return clip(head, MAX_TEXT // 2)
    head = clip(head, HEAD_LIMIT)
    room = MAX_TEXT - _u16(head) - 2
    trace = _elide(trace, room)
    if _u16(trace) > room:
        room -= len(_CUT)
        first = room * 2 // 5
        trace = _head(trace, first) + _CUT + _tail(trace, room - first)
    return head + "\n\n" + trace


def _signature(exc_info: Any) -> str:
    """Same exception type raised on the same line = the same error."""
    etype, _value, tb = exc_info
    while tb is not None and tb.tb_next is not None:
        tb = tb.tb_next
    where = f"{os.path.basename(tb.tb_frame.f_code.co_filename)}:{tb.tb_lineno}" if tb is not None else ""
    return f"{getattr(etype, '__name__', etype)}@{where}"


def build_report(record: logging.LogRecord, secrets: Iterable[str] = ()) -> Tuple[str, str]:
    """``(signature, html)``: the record's message, what the bot was working on, and the traceback in a Python block."""
    try:
        head = record.getMessage()
    except Exception:  # noqa: BLE001
        head = str(record.msg)
    context = error_context.get()
    if callable(context):
        context = context()
    if context and str(context) not in head:
        head += "\n" + str(context)
    trace, signature = "", f"{record.name}|{head[:100]}"
    if record.exc_info and record.exc_info[0] is not None:
        trace = logging.Formatter().formatException(record.exc_info)
        signature = _signature(record.exc_info)
    text = _fit(redact(head, secrets), redact(trace, secrets))
    return signature, f'<pre><code class="language-python">{esc(text)}</code></pre>'


@dataclass
class _Seen:
    at: float
    repeats: int = 0


class ErrorReporter:
    """Sends the reports to the log channel one after another and keeps repeats and floods out of it."""

    def __init__(self, send: Send, *, repeat_window: float = REPEAT_WINDOW, per_minute: int = PER_MINUTE,
                 clock: Callable[[], float] = time.monotonic):
        self._send = send
        self._repeat_window = repeat_window
        self._per_minute = per_minute
        self._clock = clock
        self._seen: Dict[str, _Seen] = {}
        self._sent: Deque[float] = deque()  # when the reports of the last minute went out
        self._skipped = 0
        self._lock = asyncio.Lock()
        self._tasks: Set["asyncio.Future[None]"] = set()

    def submit(self, signature: str, body: str) -> None:
        """Queue one report (inside the event loop).  A repeat or a flood is counted and mentioned in the next report."""
        now = self._clock()
        seen = self._seen.get(signature)
        if seen is not None and now - seen.at < self._repeat_window:
            seen.repeats += 1
            return
        while self._sent and now - self._sent[0] >= 60:
            self._sent.popleft()
        if len(self._sent) >= self._per_minute:
            self._skipped += 1
            return
        notes = []
        if seen is not None and seen.repeats:
            notes.append(f"🔁 {seen.repeats} more of this error since the last report.")
        if self._skipped:
            notes.append(f"⚠️ {self._skipped} other error report(s) were left out to keep this chat readable.")
        self._skipped = 0
        self._seen[signature] = _Seen(now)
        if len(self._seen) > 200:
            self._seen = {k: v for k, v in self._seen.items() if now - v.at < self._repeat_window}
        self._sent.append(now)
        task = asyncio.ensure_future(self._deliver(body + "".join("\n" + note for note in notes)))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _deliver(self, text: str) -> None:
        async with self._lock:  # in the order they happened
            try:
                await self._send(text)
            except Exception as exc:  # noqa: BLE001
                log.warning("could not send an error report to the log channel: %s", exc)

    async def flush(self, timeout: float = 5.0) -> None:
        """Wait (a little) for the reports that are still on their way."""
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=timeout)


class ChannelLogHandler(logging.Handler):
    """Turns every ERROR record, from any thread, into a report for the log channel."""

    def __init__(self, reporter: ErrorReporter, loop: asyncio.AbstractEventLoop, secrets: Iterable[str] = ()):
        super().__init__(level=logging.ERROR)
        self._reporter = reporter
        self._loop = loop
        self._secrets = [s for s in secrets if s]
        self._logger: Optional[logging.Logger] = None

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == __name__:  # a problem with the reports themselves must not become a report
            return
        try:
            signature, body = build_report(record, self._secrets)
            self._loop.call_soon_threadsafe(self._reporter.submit, signature, body)
        except Exception:  # noqa: BLE001 - logging must never break the code that logs
            pass

    def attach(self, logger: logging.Logger) -> None:
        logger.addHandler(self)
        self._logger = logger

    def detach(self) -> None:
        if self._logger is not None:
            self._logger.removeHandler(self)
            self._logger = None

    async def aclose(self, timeout: float = 5.0) -> None:
        """Stop listening, then give the reports that are on their way a moment to arrive."""
        self.detach()
        await asyncio.sleep(0)  # reports handed over from another thread land in the loop first
        await self._reporter.flush(timeout)


def install(send: Send, secrets: Iterable[str] = (), *, logger: Optional[logging.Logger] = None) -> ChannelLogHandler:
    """Send the errors logged on ``logger`` (the root logger by default) through ``send``.  Call it inside the event loop."""
    handler = ChannelLogHandler(ErrorReporter(send), asyncio.get_running_loop(), secrets)
    handler.attach(logger or logging.getLogger())
    return handler
