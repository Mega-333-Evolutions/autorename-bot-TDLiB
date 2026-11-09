"""Entry point: ``python -m bot``."""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import sys
from pathlib import Path
from typing import Awaitable, Callable

from . import __version__, errorlog, sysinfo, tdstate
from .config import Config, ConfigError, load_config
from .db import Database
from .tg import Client

log = logging.getLogger("bot")

TEMP_PREFIXES = ("job", "tracks", "ethumb", "minfo", "send")  # the folders the bot makes in TEMP_DIR
LOGIN_TIMEOUT = 90.0  # seconds TDLib gets to log the bot in
LOGIN_POLL = 0.5  # seconds between two looks at how the login is going
LEFTOVER_SETTINGS = ("SESSION_STRING", "TRANSFER_CONNECTIONS", "FAST_TRANSFER")  # settings of the Telethon version: they do nothing now


def clean_temp_dir(path: str) -> None:
    """Remove leftovers of a crashed run; only touches our own job folders."""
    root = Path(path)
    root.mkdir(parents=True, exist_ok=True)
    for child in root.iterdir():
        if child.is_dir() and child.name.startswith(TEMP_PREFIXES):
            shutil.rmtree(child, ignore_errors=True)


class LoginError(RuntimeError):
    """Telegram did not let the bot in."""


def make_td(cfg: Config):
    """The pytdbot client (TDLib) for this configuration.  It keeps no messages and no file list: the bot's own database has what it needs."""
    import pytdbot

    return pytdbot.Client(
        api_id=cfg.api_id, api_hash=cfg.api_hash, token=cfg.bot_token, files_directory=str(Path(cfg.tdlib_dir)),
        database_encryption_key="", use_file_database=False, use_chat_info_database=True, use_message_database=False,
        td_verbosity=cfg.td_log_level,
    )


async def login(td, timeout: float = LOGIN_TIMEOUT) -> None:
    """Start TDLib and wait until the bot is logged in.  pytdbot waits for ever when the login is refused (a wrong token, say) after
    closing TDLib, so that is looked for as well."""
    task = asyncio.ensure_future(td.start())
    deadline = asyncio.get_running_loop().time() + timeout
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=LOGIN_POLL)
            if getattr(td, "authorization_state", "") in ("authorizationStateClosing", "authorizationStateClosed"):
                raise LoginError("Telegram did not accept the login: check BOT_TOKEN, API_ID and API_HASH")
            if asyncio.get_running_loop().time() > deadline and not task.done():
                raise LoginError(f"the bot was not logged in within {timeout:g} seconds: check the connection, BOT_TOKEN, API_ID and API_HASH")
        task.result()
    finally:
        if not task.done():
            task.cancel()


async def _stop_quietly(td) -> None:
    try:
        await asyncio.wait_for(td.stop(), 15)
    except Exception:  # noqa: BLE001 - it may never have started, or may be closed already
        pass


async def start_client(cfg: Config) -> Client:
    td = make_td(cfg)
    client = Client(td, tmp_dir=Path(cfg.temp_dir) / "send", bot_dc=cfg.bot_dc)
    try:
        await login(td)
    except BaseException:
        await _stop_quietly(td)
        raise
    await client.detect_dc()
    return client


async def build_client(cfg: Config, db: Database) -> Client:
    """Log the bot in.  The login and what TDLib knows about the chats come back from the database when the Space restarted; when that saved
    state turns out to be unusable the bot logs in afresh."""
    folder = Path(cfg.tdlib_dir) / "database"
    restored = await tdstate.restore(db, folder, cfg.bot_token.split(":")[0])
    try:
        return await start_client(cfg)
    except Exception as exc:  # noqa: BLE001
        if not restored:
            raise
        log.warning("TDLib could not start with the saved state (%s: %s); logging in afresh", type(exc).__name__, exc)
        tdstate.wipe(folder)
        return await start_client(cfg)


async def amain() -> int:
    from .app import App
    from .web import start_web

    cfg = load_config()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("pytdbot").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    log.info("Auto Rename Bot v%s starting", __version__)
    for name in LEFTOVER_SETTINGS:
        if os.environ.get(name, "").strip():
            log.warning("%s is set, but this version does not use it any more: remove it", name)

    clean_temp_dir(cfg.temp_dir)
    tdstate.clean_files(Path(cfg.tdlib_dir))
    db = Database(cfg.database_url)
    await db.init()
    if db.url.startswith("sqlite"):
        log.warning("DATABASE_URL is not set: using local SQLite, so settings are lost when the Space restarts")

    runner = await start_web(cfg.port)  # up first: the Space must answer its health check while we log in
    client = await build_client(cfg, db)
    saver = tdstate.Saver(db, Path(cfg.tdlib_dir) / "database", cfg.bot_token.split(":")[0])
    await saver.save()
    saver.start()
    me = await client.get_me()
    app = App(client, cfg, db)
    username = getattr(me, "username", "") or ""
    app.telegraph.set_author(getattr(me, "first_name", "") or username, f"https://t.me/{username}" if username else "")
    await app.setup()
    channel_log = errorlog.install(app._log_send, errorlog.secrets_from(cfg)) if cfg.log_channel else None  # errors -> LOG_CHANNEL
    dc = client.session.dc_id
    log.info("@%s is online (%s%s)", me.username, sysinfo.library_version(), f", DC {dc}" if dc else "")
    speed = sysinfo.speed_summary(cfg.stream_transfer)
    log.info("speed: %s", speed)
    for _short, detail, fine in sysinfo.speed_checks(cfg.stream_transfer):
        if not fine:
            log.warning("speed: %s", detail)
    if cfg.log_channel:
        try:
            await app._log_send(f"✅ <b>Bot started</b> · v{__version__}{f' · DC {dc}' if dc else ''}\n⚡ {speed}")
        except Exception as exc:  # noqa: BLE001
            log.warning("could not post to LOG_CHANNEL: %s", exc)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass
    disconnected = asyncio.ensure_future(client.run_until_disconnected())
    stopper = asyncio.ensure_future(stop.wait())
    await asyncio.wait({disconnected, stopper}, return_when=asyncio.FIRST_COMPLETED)
    crashed = disconnected.done() and not stop.is_set()

    log.info("shutting down")
    await app.shutdown()
    if channel_log is not None:
        await channel_log.aclose()  # sends the reports still on their way, while the client is connected
    await runner.cleanup()
    try:
        await client.disconnect()
    except Exception:  # noqa: BLE001
        pass
    await saver.stop()
    await saver.save()  # TDLib has closed: its files are complete now
    await db.close()
    return 1 if crashed else 0


def _run(main: Callable[[], Awaitable[int]]) -> int:
    try:
        import uvloop
    except ImportError:
        return asyncio.run(main())
    if sys.version_info >= (3, 12):
        return asyncio.run(main(), loop_factory=uvloop.new_event_loop)
    uvloop.install()
    return asyncio.run(main())


def main() -> None:
    try:
        code = _run(amain)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        code = 0
    sys.exit(code)


if __name__ == "__main__":
    main()
