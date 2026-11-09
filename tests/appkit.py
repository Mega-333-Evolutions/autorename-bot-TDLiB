"""Shared fixture and helpers for the handler-level tests (real Telethon event builders, fake events and client)."""
import io
from types import SimpleNamespace

import pytest
from PIL import Image

from bot.app import App
from bot.config import load_config
from bot.db import Database

from .tgfakes import FakeCallback, FakeClient, FakeEvent, dispatch, document_message, flat_buttons, telegram_text

OWNER, ADMIN_CFG, ADMIN_DB, STRANGER = 1000001, 1000002, 1000003, 1999999
LOG = -1001111111111
EPISODE = "[SubsPlease] Tokyo Revengers - 05 (1080p) [ABCD1234].mkv"
EPISODE_NEW = "[S01E05] Tokyo Revengers [1080p].mkv"


def env_for(tmp_path, **extra):
    return {
        "API_ID": "1", "API_HASH": "h", "BOT_TOKEN": "1:t", "OWNER_ID": str(OWNER), "ADMIN_IDS": str(ADMIN_CFG),
        "LOG_CHANNEL": str(LOG), "TEMP_DIR": str(tmp_path / "work"), "MAX_FILE_MB": "100",
        **extra,
    }


@pytest.fixture
async def ctx(tmp_path):
    cfg = load_config(env_for(tmp_path))
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/app.db")
    await db.init()
    client = FakeClient()
    app = App(client, cfg, db)
    app._register()  # setup() minus the queue workers: jobs stay queued so they can be inspected
    yield SimpleNamespace(app=app, client=client, db=db, cfg=cfg, tmp=tmp_path)
    await app.shutdown()
    await db.close()


async def say(ctx, sender, text="", **kw):
    ev = FakeEvent(sender, text, **kw)
    await dispatch(ctx.client, ev)
    return ev


async def say_formatted(ctx, sender, markup, **kw):
    """Send a message with formatting, written like ``"[b]bold[/b] [i]italic[/i] [a=https://x.io]link[/a]"`` (see ``telegram_text``)."""
    text, entities = telegram_text(markup)
    return await say(ctx, sender, text, entities=entities, **kw)


async def send_doc(ctx, sender, name=EPISODE, size=50 * 1024 * 1024, **kw):
    message = document_message(name, size, **kw)
    ctx.client.messages[message.id] = message  # so get_messages() can find it again
    ev = FakeEvent(sender, message=message)
    await dispatch(ctx.client, ev)
    return ev


async def click(ctx, sender, data, **kw):
    cb = FakeCallback(sender, data, **kw)
    await dispatch(ctx.client, cb)
    return cb


async def prefs(ctx, uid=OWNER):
    return await ctx.db.get_settings(uid)


async def autofmt(ctx, uid=OWNER):
    """The auto format that renames the user's files (``None`` when they have none)."""
    return await ctx.db.get_auto_format(uid)


async def auto_thumb(ctx, uid=OWNER):
    """The thumbnail of the user's auto format (``None`` when there is none)."""
    fmt = await autofmt(ctx, uid)
    return await ctx.db.get_format_thumb(uid, fmt["id"]) if fmt is not None and fmt["has_thumb"] else None


async def ensure_auto(db, uid=OWNER):
    """The user's auto format, made first (a format called "Default") when they have none: what the tests build on."""
    return await db.get_auto_format(uid) or await db.create_format(uid, "Default", kind="auto")


async def set_auto_thumb(ctx, data, uid=OWNER):
    """Give the user's auto format a thumbnail, making the "Default" auto format first if they have none."""
    fmt = await ensure_auto(ctx.db, uid)
    await ctx.db.set_format_thumb(uid, fmt["id"], data)
    return fmt


async def edit_auto(ctx, uid=OWNER, **changes):
    """Change the user's auto format (``template=...``, ``caption=...``), making the "Default" one first if they have none."""
    fmt = await ensure_auto(ctx.db, uid)
    return await ctx.db.update_format(uid, fmt["id"], **changes)


def jpeg(size=(640, 480)):
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buf, "JPEG")
    return buf.getvalue()


def data_of(buttons):
    return [d for _l, d in flat_buttons(buttons)]
