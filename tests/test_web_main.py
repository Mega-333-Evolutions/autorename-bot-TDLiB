import asyncio
import dataclasses
import logging
import signal
import socket
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

import bot.main as bot_main
from bot import __version__, tdstate
from bot.config import load_config
from bot.db import Database
from bot.errorlog import ChannelLogHandler
from bot.tg import Client
from bot.web import make_app, start_web

from .tdfakes import FakeTd
from .tgfakes import FakeClient


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# health server
# ---------------------------------------------------------------------------


async def test_health_endpoints_answer_get_and_head():
    async with TestClient(TestServer(make_app())) as client:
        r = await client.get("/")
        assert r.status == 200 and "running" in await r.text()
        assert (await client.head("/")).status == 200  # cron pingers often use HEAD
        r = await client.get("/health")
        body = await r.json()
        assert r.status == 200 and body["status"] == "ok" and body["version"] == __version__
        assert body["uptime_seconds"] >= 0
        assert (await client.get("/nope")).status == 404


async def test_start_web_listens_on_the_given_port():
    port = free_port()
    runner = await start_web(port, host="127.0.0.1")
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(f"http://127.0.0.1:{port}/health") as r:
                assert r.status == 200
    finally:
        await runner.cleanup()


# ---------------------------------------------------------------------------
# start-up helpers
# ---------------------------------------------------------------------------


def test_clean_temp_dir_removes_only_job_leftovers(tmp_path):
    root = tmp_path / "work"
    for leftover in ("job1", "tracks5_6", "ethumb7", "minfo3", "send"):
        (root / leftover).mkdir(parents=True)
        (root / leftover / "movie.mkv").write_bytes(b"1")
    (root / "other").mkdir()
    (root / "keep.txt").write_text("k")
    bot_main.clean_temp_dir(str(root))
    assert sorted(p.name for p in root.iterdir()) == ["keep.txt", "other"]


def test_clean_temp_dir_creates_a_missing_directory(tmp_path):
    bot_main.clean_temp_dir(str(tmp_path / "a" / "b"))
    assert (tmp_path / "a" / "b").is_dir()


def test_main_exits_with_status_2_when_configuration_is_missing(monkeypatch, capsys):
    for key in ("API_ID", "API_HASH", "BOT_TOKEN", "OWNER_ID"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(SystemExit) as exc:
        bot_main.main()
    assert exc.value.code == 2
    assert "Missing required environment variables" in capsys.readouterr().err


def test_main_exits_with_the_code_the_bot_ended_with(monkeypatch):
    for code in (0, 1):
        monkeypatch.setattr(bot_main, "_run", lambda _main, code=code: code)
        with pytest.raises(SystemExit) as exc:
            bot_main.main()
        assert exc.value.code == code

    def interrupted(_main):
        raise KeyboardInterrupt

    monkeypatch.setattr(bot_main, "_run", interrupted)
    with pytest.raises(SystemExit) as exc:
        bot_main.main()
    assert exc.value.code == 0  # Ctrl+C is a normal way to stop


def test_run_gives_back_what_the_bot_returns():
    async def main():
        return 7

    assert bot_main._run(main) == 7


# ---------------------------------------------------------------------------
# TDLib: making it, logging in
# ---------------------------------------------------------------------------


def test_the_tdlib_client_is_made_with_the_settings_and_keeps_no_messages(tmp_path):
    cfg = load_config({"API_ID": "11", "API_HASH": "hash", "BOT_TOKEN": "1234:abc", "OWNER_ID": "1", "TDLIB_DIR": str(tmp_path / "td"),
                       "TDLIB_LOG_LEVEL": "2"})
    td = bot_main.make_td(cfg)
    assert (td._Client__api_id, td._Client__api_hash, td._Client__token) == (11, "hash", "1234:abc")
    assert td.files_directory == str(tmp_path / "td") and td.td_verbosity == 2
    assert (td.use_file_database, td.use_message_database, td.use_chat_info_database) == (False, False, True)  # chats are all it keeps


class LoginTd(FakeTd):
    """TDLib as the login sees it.  ``how``: ``ok`` (``start()`` returns once the bot is in), ``refused`` (it closes itself and ``start()``
    waits for ever, as pytdbot's does), ``hang`` (no answer at all) or ``error`` (``start()`` raises)."""

    def __init__(self, how="ok", stop_fails=False, dc=None):
        super().__init__()
        self.how, self.stop_fails = how, stop_fails
        if dc is not None:  # TDLib says which data center the bot is on
            self.reply("getOption", {"@type": "optionValueInteger", "value": dc})
        self.authorization_state = "authorizationStateWaitPhoneNumber"
        self.start_cancelled = False

    async def start(self, wait_login=True):
        self.started = True
        if self.how == "ok":
            self.authorization_state = "authorizationStateReady"
            return
        if self.how == "error":
            raise RuntimeError("API_ID_INVALID")
        if self.how == "refused":
            self.authorization_state = "authorizationStateClosing"
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.start_cancelled = True
            raise

    async def stop(self):
        await super().stop()
        if self.stop_fails:
            raise RuntimeError("already closed")


async def let_tasks_run(rounds=10):
    for _ in range(rounds):
        await asyncio.sleep(0)


@pytest.fixture(autouse=True)
def quick_login(monkeypatch):
    monkeypatch.setattr(bot_main, "LOGIN_POLL", 0.01)


async def test_login_returns_once_tdlib_has_logged_the_bot_in():
    td = LoginTd("ok")
    await bot_main.login(td)
    assert td.started


async def test_a_refused_login_is_named_instead_of_waiting_for_ever():
    td = LoginTd("refused")
    with pytest.raises(bot_main.LoginError, match="did not accept the login"):
        await bot_main.login(td, timeout=30)
    await let_tasks_run()
    assert td.start_cancelled


async def test_a_login_that_takes_too_long_is_given_up():
    td = LoginTd("hang")
    with pytest.raises(bot_main.LoginError, match="within 0.2 seconds"):
        await bot_main.login(td, timeout=0.2)
    await let_tasks_run()
    assert td.start_cancelled


async def test_what_start_raises_while_logging_in_is_passed_on():
    with pytest.raises(RuntimeError, match="API_ID_INVALID"):
        await bot_main.login(LoginTd("error"))


@pytest.fixture
async def boot(tmp_path, monkeypatch):
    """``build_client`` with a fake TDLib: ``plan.hows`` says how each TDLib that is made behaves (``ok`` when the list is used up), and
    ``made`` has them all; ``state_seen`` of each is what was in TDLib's database folder when it was made."""
    cfg = load_config({"API_ID": "11", "API_HASH": "hash", "BOT_TOKEN": "1234:abc", "OWNER_ID": "1", "TDLIB_DIR": str(tmp_path / "td"),
                       "TEMP_DIR": str(tmp_path / "work")})
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/boot.db")
    await db.init()
    made, plan = [], SimpleNamespace(hows=[], stop_fails=False, dc=None)

    def make_td(config):
        td = LoginTd(plan.hows.pop(0) if plan.hows else "ok", plan.stop_fails, plan.dc)
        td.state_seen = sorted(p.name for p in (Path(config.tdlib_dir) / "database").glob("*"))
        made.append(td)
        return td

    monkeypatch.setattr(bot_main, "make_td", make_td)
    yield SimpleNamespace(cfg=cfg, db=db, made=made, plan=plan, tmp=tmp_path)
    await db.close()


async def save_state(boot, token_id="1234", content=b"LOGIN"):
    """What an earlier run left in the bot's database."""
    folder = boot.tmp / "earlier"
    folder.mkdir(exist_ok=True)
    (folder / "td.binlog").write_bytes(content)
    saver = tdstate.Saver(boot.db, folder, token_id)
    assert await saver.save()


async def test_the_first_login_starts_tdlib_and_makes_the_client(boot):
    client = await bot_main.build_client(boot.cfg, boot.db)
    (td,) = boot.made
    assert isinstance(client, Client) and td.started and td.state_seen == []  # nothing was saved: TDLib starts empty
    assert client.session.dc_id is None  # not told, so the first file will tell
    assert client.tmp_dir == Path(boot.cfg.temp_dir) / "send"


async def test_the_data_center_setting_reaches_the_client(boot):
    client = await bot_main.build_client(dataclasses.replace(boot.cfg, bot_dc=4), boot.db)
    assert client.session.dc_id == 4


async def test_the_data_center_tdlib_reports_is_taken_when_there_is_no_setting(boot):
    boot.plan.dc = 5
    client = await bot_main.build_client(boot.cfg, boot.db)
    assert client.session.dc_id == 5


async def test_a_state_saved_by_an_earlier_run_is_back_before_tdlib_starts(boot):
    await save_state(boot)
    client = await bot_main.build_client(boot.cfg, boot.db)
    (td,) = boot.made
    assert isinstance(client, Client) and td.state_seen == ["td.binlog"]
    assert (Path(boot.cfg.tdlib_dir) / "database" / "td.binlog").read_bytes() == b"LOGIN"


async def test_a_state_saved_by_another_bot_is_not_used(boot):
    await save_state(boot, token_id="9999")
    await bot_main.build_client(boot.cfg, boot.db)
    assert boot.made[0].state_seen == []


async def test_a_saved_state_that_does_not_work_is_dropped_and_the_login_made_afresh(boot, caplog):
    await save_state(boot)
    boot.plan.hows = ["error", "ok"]
    with caplog.at_level(logging.WARNING, logger="bot"):
        client = await bot_main.build_client(boot.cfg, boot.db)
    first, second = boot.made
    assert first.state_seen == ["td.binlog"] and second.state_seen == []  # what failed was wiped before the second try
    assert first.stopped and not second.stopped and isinstance(client, Client)
    assert "logging in afresh" in caplog.text and "API_ID_INVALID" in caplog.text


async def test_without_a_saved_state_a_failed_login_is_not_repeated(boot):
    boot.plan.hows = ["error", "ok"]
    with pytest.raises(RuntimeError, match="API_ID_INVALID"):
        await bot_main.build_client(boot.cfg, boot.db)
    assert len(boot.made) == 1 and boot.made[0].stopped  # and TDLib was closed, not left running


async def test_a_refused_login_with_a_saved_state_is_tried_twice_and_then_reported(boot):
    await save_state(boot)
    boot.plan.hows = ["refused", "refused"]
    with pytest.raises(bot_main.LoginError, match="did not accept the login"):
        await bot_main.build_client(boot.cfg, boot.db)
    assert len(boot.made) == 2 and all(td.stopped for td in boot.made)


async def test_a_tdlib_that_cannot_be_closed_does_not_hide_why_the_login_failed(boot):
    boot.plan.hows, boot.plan.stop_fails = ["error"], True
    with pytest.raises(RuntimeError, match="API_ID_INVALID"):
        await bot_main.build_client(boot.cfg, boot.db)


# ---------------------------------------------------------------------------
# whole start-up sequence with a fake Telegram client
# ---------------------------------------------------------------------------


async def start_the_bot(tmp_path, monkeypatch, closing=None):
    """Run ``amain`` against a fake client until the bot has said it started.  The signal handlers it installs are caught in
    ``handlers`` (a test calls one to stop the bot, as the signal would); ``gate`` ends the fake connection to Telegram."""
    port = free_port()
    env = {
        "API_ID": "1", "API_HASH": "h", "BOT_TOKEN": "1:t", "OWNER_ID": "1000001", "LOG_CHANNEL": "-1001111111111",
        "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path}/main.db", "TEMP_DIR": str(tmp_path / "work"), "TDLIB_DIR": str(tmp_path / "td"),
        "PORT": str(port),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    (tmp_path / "work" / "job9").mkdir(parents=True)  # leftover of a "crashed" run
    (tmp_path / "td" / "database").mkdir(parents=True)
    (tmp_path / "td" / "database" / "td.binlog").write_bytes(b"LOGIN")  # what TDLib has learnt: it stays
    (tmp_path / "td" / "documents").mkdir()
    (tmp_path / "td" / "documents" / "half.bin").write_bytes(b"x")  # a half-downloaded file of a crashed run: it goes

    gate, handlers = asyncio.Event(), {}
    client = FakeClient()
    client.disconnected = False

    async def get_me():
        return SimpleNamespace(username="rename_bot")

    async def run_until_disconnected():
        await gate.wait()

    async def disconnect():
        client.disconnected = True
        if closing is not None:
            closing()  # TDLib writes what it still holds while it closes

    client.get_me, client.run_until_disconnected, client.disconnect = get_me, run_until_disconnected, disconnect

    async def fake_build_client(cfg, db):
        return client

    monkeypatch.setattr(bot_main, "build_client", fake_build_client)
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", lambda sig, callback, *args: handlers.__setitem__(sig, callback))

    task = asyncio.ensure_future(bot_main.amain())
    for _ in range(200):  # wait for the "started" notice: everything is wired by then
        if any("Bot started" in text for _chat, text, _kw in client.sent) and handlers:
            break
        await asyncio.sleep(0.05)
    else:
        task.cancel()
        pytest.fail("the bot never finished starting")
    return SimpleNamespace(task=task, client=client, gate=gate, handlers=handlers, port=port, tmp=tmp_path)


async def test_amain_wires_everything_and_exits_nonzero_if_telegram_disconnects(tmp_path, monkeypatch):
    bot = await start_the_bot(tmp_path, monkeypatch)
    client, task = bot.client, bot.task
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(f"http://127.0.0.1:{bot.port}/health") as r:
                assert r.status == 200
        assert len(client.handlers) > 10
        (menu,) = client.commands
        assert menu  # the command menu was set
        assert not (tmp_path / "work" / "job9").exists()
        assert not (tmp_path / "td" / "documents").exists() and (tmp_path / "td" / "database" / "td.binlog").read_bytes() == b"LOGIN"
        assert client.sent[0][0] == -1001111111111
        started = client.sent[0][1]  # the notice also says what the speed depends on
        assert "Bot started" in started and f"v{__version__}" in started and "DC 1" in started
        assert "TDLib ✅" in started and "no streaming ✅" in started
        assert any(isinstance(h, ChannelLogHandler) for h in logging.getLogger().handlers)
        logging.getLogger("bot.something").error("an error somewhere in the bot")  # errors go to the log channel as well
        for _ in range(200):
            if any("an error somewhere in the bot" in text for _chat, text, _kw in client.sent):
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("the error never reached the log channel")
        bot.gate.set()  # Telegram connection lost -> exit non-zero so the container restarts
        assert await asyncio.wait_for(task, 10) == 1
    finally:
        bot.gate.set()
        if not task.done():
            task.cancel()
    assert client.disconnected
    assert not any(isinstance(h, ChannelLogHandler) for h in logging.getLogger().handlers)  # detached again on the way out
    with pytest.raises(aiohttp.ClientError):
        async with aiohttp.ClientSession() as http:
            async with http.get(f"http://127.0.0.1:{bot.port}/health"):
                pass


async def test_amain_exits_with_zero_when_it_is_told_to_stop(tmp_path, monkeypatch):
    bot = await start_the_bot(tmp_path, monkeypatch)
    try:
        assert {signal.SIGINT, signal.SIGTERM} <= set(bot.handlers)
        bot.handlers[signal.SIGTERM]()  # what the platform sends when it stops the container
        assert await asyncio.wait_for(bot.task, 10) == 0
    finally:
        bot.gate.set()
        if not bot.task.done():
            bot.task.cancel()
    assert bot.client.disconnected


async def test_the_tdlib_state_is_kept_in_the_database_and_saved_again_on_the_way_out(tmp_path, monkeypatch):
    bot = await start_the_bot(tmp_path, monkeypatch, closing=lambda: (tmp_path / "td" / "database" / "td.binlog").write_bytes(b"LOGIN AND MORE"))
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/main.db")
    await db.init()
    try:
        assert await db.kv_get(tdstate.KEY_BOT) == "1"  # the id of the bot: the number before the colon of the token
        first = await db.kv_get(tdstate.KEY_DATA)
        assert first
        bot.handlers[signal.SIGTERM]()
        assert await asyncio.wait_for(bot.task, 10) == 0
        assert await db.kv_get(tdstate.KEY_DATA) != first  # the final save came after TDLib had closed, with what it wrote while closing
        restored = tmp_path / "restored"
        await tdstate.restore(db, restored, "1")
        assert (restored / "td.binlog").read_bytes() == b"LOGIN AND MORE"
    finally:
        bot.gate.set()
        if not bot.task.done():
            bot.task.cancel()
        await db.close()


async def test_the_tdlib_state_is_saved_again_while_the_bot_runs(tmp_path, monkeypatch):
    monkeypatch.setitem(tdstate.Saver.__init__.__kwdefaults__, "interval", 0.02)  # instead of every ten minutes
    bot = await start_the_bot(tmp_path, monkeypatch)
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/main.db")
    await db.init()
    try:
        first = await db.kv_get(tdstate.KEY_DATA)
        (tmp_path / "td" / "database" / "td.binlog").write_bytes(b"LOGIN, and what TDLib learnt while the bot ran")
        for _ in range(200):
            if await db.kv_get(tdstate.KEY_DATA) != first:
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("the changed state was never saved")
    finally:
        bot.gate.set()
        await asyncio.wait_for(bot.task, 10)
        await db.close()


async def test_settings_of_the_telethon_version_are_reported_as_unused_without_showing_their_values(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("SESSION_STRING", "1BVtsOKEB-the-secret-session")
    monkeypatch.setenv("FAST_TRANSFER", "0")
    monkeypatch.setenv("TRANSFER_CONNECTIONS", "  ")  # nothing in it: as good as not set
    with caplog.at_level(logging.WARNING, logger="bot"):
        bot = await start_the_bot(tmp_path, monkeypatch)
        bot.handlers[signal.SIGTERM]()
        assert await asyncio.wait_for(bot.task, 10) == 0
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert "SESSION_STRING is set, but this version does not use it any more: remove it" in warnings
    assert "FAST_TRANSFER is set, but this version does not use it any more: remove it" in warnings
    assert not any("TRANSFER_CONNECTIONS" in w for w in warnings) and "the-secret-session" not in caplog.text


async def test_nothing_is_said_about_settings_that_are_not_set(tmp_path, monkeypatch, caplog):
    for name in bot_main.LEFTOVER_SETTINGS:
        monkeypatch.delenv(name, raising=False)
    with caplog.at_level(logging.WARNING, logger="bot"):
        bot = await start_the_bot(tmp_path, monkeypatch)
        bot.handlers[signal.SIGTERM]()
        await asyncio.wait_for(bot.task, 10)
    assert "does not use it any more" not in caplog.text
