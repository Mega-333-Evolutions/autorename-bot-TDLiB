"""Error reports for the log channel: what they look like, what is hidden or held back, and how they are delivered."""
import asyncio
import html
import logging
import re
from types import SimpleNamespace

import pytest

from bot import errorlog
from bot.config import load_config
from bot.errorlog import ChannelLogHandler, ErrorReporter, build_report, describe_update, redact
from bot.tg import Message, events

from .tdfakes import td_message, text_content
from .tgfakes import FakeCallback, FakeEvent, FakeInlineQuery, document_message

CODE = '<pre><code class="language-python">'


class Catch(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def make_record(message="Error handling update:", raiser=None, *args):
    """A real log record, made the way the bot makes them: ``log.exception`` inside an ``except``."""
    logger = logging.getLogger("errorlog.test.records")
    logger.propagate = False
    catch = Catch()
    logger.addHandler(catch)
    try:
        try:
            if raiser is not None:
                raiser()
        except Exception:  # noqa: BLE001
            logger.exception(message, *args)
        else:
            logger.error(message, *args)
    finally:
        logger.removeHandler(catch)
    return catch.records[0]


def visible(report):
    """What Telegram shows: the report without its HTML markup."""
    return html.unescape(re.sub(r"</?(?:pre|code)[^>]*>", "", report))


def fail(text="bad"):
    raise ValueError(text)


def fail_elsewhere():
    raise ValueError("bad")


def chained():
    try:
        {}["missing"]
    except KeyError:
        raise RuntimeError("could not read <b>this</b> & that")


def very_deep(depth=200):
    """A traceback with ``depth`` different frames (a recursion would be shortened by Python)."""
    scope = {}
    source = "\n".join(f"def f{i}():\n    return f{i + 1}()" for i in range(depth)) + f"\ndef f{depth}():\n    raise ValueError('bottom')"
    exec(source, scope)  # noqa: S102
    scope["f0"]()


# ---------------------------------------------------------------------------
# describing the update
# ---------------------------------------------------------------------------


def test_a_message_with_a_file_is_described_with_its_text_and_file():
    message = document_message("1146 - One Piece [Dual] 720p @Anime_X_Universe.mkv", 316_000_000, caption="hello")
    line = describe_update(FakeEvent(1000001, message=message))
    assert line == (
        "FakeEvent(sender_id=1000001, chat_id=1000001, message_id=10, text='hello', "
        "file='1146 - One Piece [Dual] 720p @Anime_X_Universe.mkv' (video/x-matroska, 316000000 bytes))"
    )


def test_a_button_press_is_described_with_its_data():
    assert describe_update(FakeCallback(5, "fmt:v:3", msg_id=77)) == "FakeCallback(sender_id=5, chat_id=5, message_id=77, data='fmt:v:3')"


def test_real_events_are_described_too():
    message = Message(td_message(text_content("/mediainfo"), chat_id=77, plain_id=5, sender=77))
    assert describe_update(events.MessageEvent(None, message)) == "NewMessage(sender_id=77, chat_id=77, message_id=5, text='/mediainfo')"
    press = {"id": 1, "sender_user_id": 77, "chat_id": 77, "message_id": 9 << 20, "payload": {"@type": "callbackQueryPayloadData", "data": "Zm10OnY6Mw=="}}
    assert describe_update(events.CallbackEvent(None, press)) == "CallbackQuery(sender_id=77, chat_id=77, message_id=9, data='fmt:v:3')"


async def test_an_inline_query_is_described_with_what_was_typed():
    assert describe_update(FakeInlineQuery(5, "share_12")) == "FakeInlineQuery(sender_id=5, chat_id=5, query='share_12')"
    query = {"id": 1, "sender_user_id": 77, "query": "share_12", "offset": ""}
    assert describe_update(events.InlineEvent(None, query)) == "InlineQuery(sender_id=77, chat_id=77, query='share_12')"


def test_a_long_text_is_cut_and_an_odd_object_never_raises():
    line = describe_update(FakeEvent(1, "x" * 500))
    assert "…" in line and len(line) < 400

    class Odd:
        @property
        def sender_id(self):
            raise RuntimeError("no")

    assert describe_update(Odd()) == "Odd"


# ---------------------------------------------------------------------------
# hiding secrets
# ---------------------------------------------------------------------------


def test_configured_secrets_are_hidden_but_short_values_are_left_alone():
    assert redact("token abc123XYZ789 and abc", ["abc123XYZ789", "abc"]) == "token *** and abc"


def test_long_token_like_strings_are_hidden():
    """The saved TDLib state (thousands of characters of base64) must never reach a report, nor must anything else of that kind."""
    state = "H4sIAAAAAAAC" + "A1b2C3_-" * 40
    assert redact(f"bound {state} here") == "bound *** here"


def test_ordinary_text_survives():
    for text in (
        "File \"/home/user/app/bot/app.py\", line 1021, in _mediainfo",
        "File \"/tmp/claude-0/-home-claude/77cec5a8-65b1-5e4a-87df-15495b60ec66/scratchpad/venv312/lib/python3.12/site-packages/x.py\"",
        "1147 - One Piece [Dual] 720p @Anime_X_Universe.mkv → One Piece - E1147.mkv (276.61 MB)",
        "Naruto_Shippuden_Episode_123_Dual_Audio_English_Japanese_Subbed_1080p_HEVC_x265_10bit.mkv",
        "-" * 150,
    ):
        assert redact(text) == text


def test_the_values_an_sql_error_prints_are_hidden():
    text = "[SQL: INSERT INTO kv VALUES (?, ?)]\n[parameters: ('session', 'short')]\n(Background on this error)"
    assert "short" not in redact(text) and "[SQL: INSERT INTO kv VALUES (?, ?)]" in redact(text)


def test_secrets_come_from_the_configuration():
    cfg = load_config({
        "API_ID": "1", "API_HASH": "0123456789abcdef", "BOT_TOKEN": "123:botsecret", "OWNER_ID": "1",
        "DATABASE_URL": "postgresql://user:pw123456@host/db",
    })
    secrets = errorlog.secrets_from(cfg)
    assert {"0123456789abcdef", "123:botsecret", "pw123456", "postgresql://user:pw123456@host/db"} <= set(secrets)
    assert "" not in secrets  # a value that is not set is not a secret


# ---------------------------------------------------------------------------
# the report itself
# ---------------------------------------------------------------------------


def test_the_report_has_the_message_then_the_whole_traceback_in_a_python_block():
    _signature, body = build_report(make_record("Error handling update:", chained))
    assert body.startswith(CODE + "Error handling update:\n\nTraceback (most recent call last):") and body.endswith("</code></pre>")
    text = visible(body)
    assert "KeyError: 'missing'" in text and "During handling of the above exception, another exception occurred:" in text
    assert text.endswith("RuntimeError: could not read <b>this</b> & that")
    assert "&lt;b&gt;this&lt;/b&gt; &amp; that" in body  # escaped for Telegram's HTML


def test_a_message_without_an_exception_is_just_the_message():
    _signature, body = build_report(make_record("something odd: %s", None, 42))
    assert body == f"{CODE}something odd: 42</code></pre>"


def test_what_the_bot_was_working_on_comes_under_the_message():
    token = errorlog.error_context.set("user 42 · job #7\nold.mkv → new.mkv (1.00 MB)")
    try:
        _signature, body = build_report(make_record("Error processing file:", fail))
    finally:
        errorlog.error_context.reset(token)
    assert body.startswith(CODE + "Error processing file:\nuser 42 · job #7\nold.mkv → new.mkv (1.00 MB)\n\nTraceback")
    assert "user 42" not in build_report(make_record("later", fail))[1]  # only while it is set


def test_the_working_context_can_be_a_function_and_is_not_repeated():
    token = errorlog.error_context.set(lambda: "FakeEvent(sender_id=1)")
    try:
        body = build_report(make_record("Error handling update:", fail))[1]
        assert body.count("FakeEvent(sender_id=1)") == 1
        assert build_report(make_record("FakeEvent(sender_id=1) failed", fail))[1].count("FakeEvent(sender_id=1)") == 1
    finally:
        errorlog.error_context.reset(token)


def test_secrets_are_hidden_in_the_message_and_in_the_traceback():
    body = build_report(make_record("login with TOP-SECRET-VALUE failed", lambda: fail("TOP-SECRET-VALUE")), ["TOP-SECRET-VALUE"])[1]
    assert "TOP-SECRET-VALUE" not in body and body.count("***") >= 2


def test_a_long_traceback_keeps_its_first_frames_and_its_end_and_fits_in_one_message():
    _signature, body = build_report(make_record("Error handling update:", very_deep))
    text = visible(body)
    assert errorlog._u16(text) <= errorlog.MAX_TEXT
    first, last = text.split("\n…\n")  # only the middle is cut
    assert first.startswith("Error handling update:\n\nTraceback (most recent call last):\n  File ")
    assert "in make_record" in first and "in very_deep" in first  # where it came from
    assert re.search(r", in f\d+$", first)  # cut after a whole line
    assert last.startswith("  File ") and last.endswith("ValueError: bottom")  # from the start of a line, up to the error


def test_a_traceback_that_fits_is_left_whole():
    text = visible(build_report(make_record("Error handling update:", chained))[1])
    assert "…" not in text and "in make_record" in text


def library_failure(depth=40):
    """Fail ``depth`` calls deep inside code that looks like it lives in site-packages, the way SQLAlchemy's frames do."""
    scope = {}
    source = "\n".join(f"def g{i}():\n    return g{i + 1}()" for i in range(depth)) + f"\ndef g{depth}():\n    raise ValueError('inside the library')"
    exec(compile(source, "/home/user/.local/lib/python3.12/site-packages/somelib/core.py", "exec"), scope)  # noqa: S102
    scope["g0"]()


def test_library_frames_are_collapsed_before_anything_of_the_bots_own_is_cut():
    text = visible(build_report(make_record("Error handling update:", library_failure))[1])
    assert errorlog._u16(text) <= errorlog.MAX_TEXT
    assert "in make_record" in text and "in library_failure" in text  # the bot's own frames stay
    assert "[… 41 library frames …]" in text and "somelib" not in text
    assert "\n…\n" not in text  # nothing else had to go
    assert text.endswith("ValueError: inside the library")


def test_only_as_much_of_the_library_frames_is_collapsed_as_needed():
    def frame(path, n):
        return f'  File "{path}", line {n}, in f{n}\n    call{n}()'

    lib, mine = "/usr/local/lib/python3.12/site-packages/x.py", "/home/user/app/bot/app.py"
    trace = "\n".join(
        ["Traceback (most recent call last):", frame(mine, 1)] + [frame(lib, n) for n in range(10, 15)]
        + [frame(mine, 2), frame(lib, 30), frame(mine, 3), "ValueError: boom"]
    )
    assert errorlog._elide(trace, 10_000) == trace  # fits: left alone
    some = errorlog._elide(trace, len(trace) - 50)  # the run of five goes, the single library frame stays
    assert "[… 5 library frames …]" in some and 'x.py", line 30' in some
    most = errorlog._elide(trace, 150)  # every run goes
    assert "[… 1 library frame …]" in most and 'x.py' not in most
    assert most.endswith("ValueError: boom") and 'app.py", line 1' in most


def test_a_chained_traceback_keeps_each_exception_line_when_its_library_frames_are_collapsed():
    def frame(path, n):
        return f'  File "{path}", line {n}, in f{n}\n    call{n}()'

    lib = "/usr/local/lib/python3.12/site-packages/asyncpg/protocol.py"
    mine = "/home/user/app/bot/db.py"
    trace = "\n".join(
        ["Traceback (most recent call last):"] + [frame(lib, n) for n in range(5)] + ["asyncpg.UndefinedColumnError: no such column", "",
         "The above exception was the direct cause of the following exception:", "", "Traceback (most recent call last):", frame(mine, 7)]
        + [frame(lib, n) for n in range(20, 30)] + ["sqlalchemy.exc.ProgrammingError: column formats.data does not exist"]
    )
    text = errorlog._elide(trace, 300)
    assert text.count("library frames …]") == 2
    assert "asyncpg.UndefinedColumnError: no such column" in text and "direct cause of the following exception" in text
    assert 'db.py", line 7' in text and text.endswith("column formats.data does not exist")


def test_emoji_count_double_towards_the_limit():
    text = visible(build_report(make_record("Error handling update:", lambda: fail("🔥" * 3000)))[1])
    assert errorlog._u16(text) <= errorlog.MAX_TEXT and text.endswith("🔥")


def test_a_log_call_with_wrong_arguments_still_gives_a_report():
    record = logging.LogRecord("bot.test", logging.ERROR, __file__, 1, "%d items", ("x",), None)  # what such a call makes
    assert build_report(record)[1] == f"{CODE}%d items</code></pre>"


def test_the_same_exception_on_the_same_line_is_the_same_error():
    same = build_report(make_record("one", fail))[0], build_report(make_record("another message", fail))[0]
    assert same[0] == same[1]
    assert build_report(make_record("one", fail_elsewhere))[0] != same[0]
    assert build_report(make_record("one", None))[0] != same[0]


# ---------------------------------------------------------------------------
# keeping repeats and floods out
# ---------------------------------------------------------------------------


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def sink():
    sent = []

    async def send(text):
        sent.append(text)

    return SimpleNamespace(sent=sent, send=send, clock=Clock())


async def test_an_identical_error_is_reported_once_and_counted(sink):
    reporter = ErrorReporter(sink.send, clock=sink.clock)
    for n in (1, 2, 3):
        reporter.submit("same", f"<pre>{n}</pre>")
    await reporter.flush()
    assert sink.sent == ["<pre>1</pre>"]
    sink.clock.now += 121
    reporter.submit("same", "<pre>4</pre>")
    await reporter.flush()
    assert sink.sent[1] == "<pre>4</pre>\n🔁 2 more of this error since the last report."
    sink.clock.now += 121
    reporter.submit("same", "<pre>5</pre>")
    await reporter.flush()
    assert sink.sent[2] == "<pre>5</pre>"  # nothing came back in between: no note


async def test_different_errors_all_go_out_in_the_order_they_happened(sink):
    async def slow_first(text):
        await asyncio.sleep(0.05 if text == "<pre>a</pre>" else 0)
        await sink.send(text)

    reporter = ErrorReporter(slow_first, clock=sink.clock)
    for name in "abc":
        reporter.submit(name, f"<pre>{name}</pre>")
    await reporter.flush()
    assert sink.sent == ["<pre>a</pre>", "<pre>b</pre>", "<pre>c</pre>"]


async def test_a_flood_is_cut_off_and_mentioned_in_the_next_report(sink):
    reporter = ErrorReporter(sink.send, per_minute=3, clock=sink.clock)
    for n in range(5):
        reporter.submit(f"e{n}", f"<pre>{n}</pre>")
    await reporter.flush()
    assert sink.sent == ["<pre>0</pre>", "<pre>1</pre>", "<pre>2</pre>"]
    sink.clock.now += 61
    reporter.submit("later", "<pre>later</pre>")
    await reporter.flush()
    assert sink.sent[3] == "<pre>later</pre>\n⚠️ 2 other error report(s) were left out to keep this chat readable."


async def test_a_failing_send_does_not_stop_the_next_reports(sink, caplog):
    calls = []

    async def flaky(text):
        calls.append(text)
        if len(calls) == 1:
            raise RuntimeError("CHAT_WRITE_FORBIDDEN")
        await sink.send(text)

    reporter = ErrorReporter(flaky, clock=sink.clock)
    reporter.submit("a", "<pre>a</pre>")
    reporter.submit("b", "<pre>b</pre>")
    await reporter.flush()
    assert sink.sent == ["<pre>b</pre>"]
    assert "could not send an error report" in caplog.text and "CHAT_WRITE_FORBIDDEN" in caplog.text


async def test_old_entries_are_forgotten(sink):
    reporter = ErrorReporter(sink.send, per_minute=10_000, clock=sink.clock)
    for n in range(150):
        reporter.submit(f"old{n}", "<pre>x</pre>")
    sink.clock.now += 500
    for n in range(100):
        reporter.submit(f"new{n}", "<pre>x</pre>")
    assert len(reporter._seen) <= 101
    await reporter.flush()


# ---------------------------------------------------------------------------
# the logging handler
# ---------------------------------------------------------------------------


@pytest.fixture
async def channel(sink):
    logger = logging.getLogger("errorlog.test.channel")
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    handler = errorlog.install(sink.send, ["TOP-SECRET-VALUE"], logger=logger)
    yield SimpleNamespace(log=logger, sent=sink.sent, handler=handler)
    handler.detach()


async def test_an_error_with_a_traceback_arrives_as_one_python_block(channel):
    try:
        fail("disk is full")
    except ValueError:
        channel.log.exception("Error processing file:")
    await channel.handler.aclose()
    (report,) = channel.sent
    assert report.startswith(CODE + "Error processing file:\n\nTraceback (most recent call last):")
    assert report.endswith("ValueError: disk is full</code></pre>")


async def test_only_errors_are_reported(channel):
    channel.log.info("fine")
    channel.log.warning("careful")
    channel.log.error("broken")
    channel.log.critical("very broken")
    await channel.handler.aclose()
    assert channel.sent == [f"{CODE}broken</code></pre>", f"{CODE}very broken</code></pre>"]


async def test_errors_logged_from_another_thread_arrive_too(channel):
    await asyncio.to_thread(channel.log.error, "from a worker thread")
    await channel.handler.aclose()
    assert channel.sent == [f"{CODE}from a worker thread</code></pre>"]


async def test_the_working_context_is_added_to_errors_logged_below_it(channel):
    token = errorlog.error_context.set("user 42 · job #7")
    try:
        channel.log.error("helper failed")
    finally:
        errorlog.error_context.reset(token)
    await channel.handler.aclose()
    assert channel.sent == [f"{CODE}helper failed\nuser 42 · job #7</code></pre>"]


async def test_secrets_never_leave_the_bot(channel):
    channel.log.error("the key TOP-SECRET-VALUE was refused")
    await channel.handler.aclose()
    assert channel.sent == [f"{CODE}the key *** was refused</code></pre>"]


async def test_closing_stops_the_reports(channel):
    await channel.handler.aclose()
    channel.log.error("too late")
    await asyncio.sleep(0.01)
    assert channel.sent == []


async def test_the_reporters_own_trouble_is_not_reported(sink):
    handler = errorlog.install(sink.send)  # the root logger, as the bot does
    try:
        logging.getLogger("bot.errorlog").error("this would loop")
        logging.getLogger("bot.other").error("this is reported")
        await handler.aclose()
    finally:
        handler.detach()
    assert sink.sent == [f"{CODE}this is reported</code></pre>"]


def test_a_closed_event_loop_does_not_break_logging(sink):
    loop = asyncio.new_event_loop()
    loop.close()
    logger = logging.getLogger("errorlog.test.closed")
    logger.propagate = False
    handler = ChannelLogHandler(ErrorReporter(sink.send), loop)
    logger.addHandler(handler)
    try:
        logger.error("nobody is listening")  # must not raise
    finally:
        logger.removeHandler(handler)
