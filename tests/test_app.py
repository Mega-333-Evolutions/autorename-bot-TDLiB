"""Handler-level tests: the real event builders and handlers of the bot, with fake events and a fake client."""
import asyncio
import logging
import shutil
import time
from pathlib import Path

import pytest

from bot import app as appmod
from bot import errorlog, panels, transfer
from bot.app import COMMANDS, OWNER_COMMANDS, PANEL_NAMES, App, Pending, cmd_pattern, is_renameable
from bot.config import load_config
from bot.db import Database
from bot.queue import JobQueue

from .conftest import needs_ffmpeg
from .appkit import (
    ADMIN_CFG, ADMIN_DB, EPISODE, EPISODE_NEW, LOG, OWNER, STRANGER, auto_thumb, autofmt, click, data_of, edit_auto, env_for, jpeg,
    prefs, say, send_doc, set_auto_thumb,
)
from .tgfakes import (
    FakeCallback, FakeClient, FakeEvent, FakeMessage, dispatch, document_message, flat_buttons, make_file, variables_line,
)

# ---------------------------------------------------------------------------
# access control & routing
# ---------------------------------------------------------------------------


async def test_strangers_are_ignored_and_reported_once(ctx):
    for text in ("/start", "hello", "/rename x", "/cancel"):
        assert (await say(ctx, STRANGER, text)).out == []
    assert (await send_doc(ctx, STRANGER)).out == []
    cb = await click(ctx, STRANGER, "m:home")
    assert cb.edits == [] and not ctx.app.queue.jobs
    reports = [t for chat, t, _kw in ctx.client.sent if chat == LOG]
    assert len(reports) == 1 and str(STRANGER) in reports[0] and f"/addadmin {STRANGER}" in reports[0]


async def test_a_plain_message_from_a_stranger_is_also_reported(ctx):
    await say(ctx, STRANGER, "hi there")
    assert len([1 for chat, _t, _k in ctx.client.sent if chat == LOG]) == 1


async def test_owner_and_admins_from_config_and_db_are_allowed(ctx):
    await ctx.db.add_admin(ADMIN_DB, OWNER)
    ctx.app.admins |= set(await ctx.db.list_admins())
    for uid in (OWNER, ADMIN_CFG, ADMIN_DB):
        assert (await say(ctx, uid, "/help")).out, uid
    assert ctx.client.sent == []  # nobody was reported


async def test_group_chats_are_ignored(ctx):
    assert (await say(ctx, OWNER, "/start", private=False)).out == []
    assert (await say(ctx, STRANGER, "/start", private=False)).out == []
    assert ctx.client.sent == []


async def test_owner_only_commands_are_refused_for_admins(ctx):
    for text in ("/addadmin 123456", "/deladmin 123456", "/admins"):
        ev = await say(ctx, ADMIN_CFG, text)
        assert ev.texts == ["⛔ Only the owner can do that."], text
    assert 123456 not in ctx.app.admins
    assert (await click(ctx, ADMIN_CFG, "m:admin")).alerts == ["Owner only."]
    assert (await click(ctx, ADMIN_CFG, "adm:stats")).alerts == ["Owner only."]
    assert (await click(ctx, ADMIN_CFG, "adm:clear")).alerts == ["Owner only."]
    assert ctx.client.sent == []


def test_cmd_pattern_accepts_args_bot_suffix_and_any_case():
    p = cmd_pattern("rename")
    assert p.match("/rename New Name").group(1) == "New Name"
    assert p.match("/rename@MyBot New Name").group(1) == "New Name"
    assert p.match("/RENAME").group(1) is None
    assert p.match("/rename\nline one\nline two").group(1) == "line one\nline two"
    assert not p.match("/renamex") and not p.match("rename") and not p.match("/re")


def test_every_menu_command_has_a_handler_and_telegram_compatible_text(ctx):
    table = ctx.app._command_table()
    for name, description in COMMANDS:
        assert name in table
        assert name.islower() and 1 <= len(name) <= 32
        assert 3 <= len(description) <= 256
    assert OWNER_COMMANDS <= set(table)
    assert {"settitle", "setauthor", "setartist", "setaudio", "setsubtitle", "setvideo", "removewords", "setchannel"} <= set(table)
    assert dict(COMMANDS)["status"] == "Show basic bot uptime, ping, and version information."
    assert dict(COMMANDS)["stats"] == "Show detailed system stats such as users, CPU, RAM, and disk usage."


async def test_setup_registers_command_menu_loads_admins_and_survives_log_channel_errors(tmp_path):
    cfg = load_config(env_for(tmp_path))
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/setup.db")
    await db.init()
    await db.add_admin(ADMIN_DB, OWNER)
    client = FakeClient()
    client.bad_entities.add(LOG)
    app = App(client, cfg, db)
    await app.setup()
    try:
        assert client.parse_mode == "html"
        assert ADMIN_DB in app.admins and ADMIN_CFG in app.admins
        (menu,) = client.commands  # the command menu was set once
        assert [c.command for c in menu] == [c for c, _ in COMMANDS]
        assert len(client.handlers) > 10
    finally:
        await app.shutdown()
        await db.close()


async def test_setup_tells_in_the_log_which_chat_the_bot_cannot_use_and_what_to_do(tmp_path, caplog):
    cfg = load_config(env_for(tmp_path, DB_CHANNEL="-1001000000001", COMMUNICATION_CHAT="-1001000000002"))
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/chats.db")
    await db.init()
    client = FakeClient()
    client.bad_entities.update({LOG, -1001000000001})  # the log channel and the Database Channel are not known to TDLib; the communication chat is
    app = App(client, cfg, db)
    try:
        with caplog.at_level(logging.INFO, logger="bot"):
            await app.setup()
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 2
        assert f"cannot access LOG_CHANNEL {LOG}" in warnings[0] and "cannot access DB_CHANNEL -1001000000001" in warnings[1]
        assert all("post any message in that chat" in w and "admin" in w for w in warnings)
        assert "uploading to the Database Channel is on" in caplog.text
    finally:
        await app.shutdown()
        await db.close()


async def test_setup_says_when_only_one_of_the_two_upload_chats_is_set(tmp_path, caplog):
    cfg = load_config(env_for(tmp_path, DB_CHANNEL="-1001000000001"))
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/chats.db")
    await db.init()
    app = App(FakeClient(), cfg, db)
    try:
        with caplog.at_level(logging.INFO, logger="bot"):
            await app.setup()
        assert "uploading to the Database Channel is off: it needs both DB_CHANNEL (-1001000000001) and COMMUNICATION_CHAT (missing)" in caplog.text
    finally:
        await app.shutdown()
        await db.close()


# ---------------------------------------------------------------------------
# /start, /help and the one-line settings commands
# ---------------------------------------------------------------------------


async def test_start_shows_the_home_panel_and_hides_admin_from_admins(ctx):
    ev = await say(ctx, OWNER, "/start")
    text, buttons, kw = ev.out[0]
    assert "Auto Rename Bot" in text and kw["parse_mode"] == "html"
    assert {"m:auto", "m:rename", "m:meta", "m:ftype", "m:formats", "m:help", "m:admin"} <= set(data_of(buttons))
    assert "m:cap" not in data_of(buttons)  # the caption and the thumbnail are edited in /formats
    assert "m:admin" not in data_of((await say(ctx, ADMIN_CFG, "/start")).out[0][1])


@pytest.mark.parametrize("text", ["/start payload", "/start@SomeBot", "/START"])
async def test_start_variants(ctx, text):
    assert (await say(ctx, OWNER, text)).out


async def test_start_and_help(ctx):
    ctx.app.pending[OWNER] = Pending("remove", OWNER, 1, "rename", False, time.time() + 100)
    await say(ctx, OWNER, "/start")
    assert OWNER not in ctx.app.pending
    ev = await say(ctx, OWNER, "/help")
    assert "How to use" in ev.texts[0] and "x:close" in data_of(ev.out[0][1])


async def test_mode_command(ctx):
    ev = await say(ctx, OWNER, "/mode caption")
    assert (await prefs(ctx))["mode"] == "caption" and "CAPTION" in ev.texts[0]
    for text in ("/mode", "/mode nonsense"):
        ev = await say(ctx, OWNER, text)
        assert "RENAME MODE" in ev.texts[0] and "set:mode:filename" in data_of(ev.out[0][1])
    assert (await prefs(ctx))["mode"] == "caption"
    await say(ctx, OWNER, "/mode FILENAME")
    assert (await prefs(ctx))["mode"] == "filename"


async def test_filetype_command_and_aliases(ctx):
    for arg, expected in (("video", "video"), ("audio", "audio"), ("document", "document"), ("original", "same"),
                          ("doc", "document"), ("file", "document"), ("same", "same")):
        await say(ctx, OWNER, f"/filetype {arg}")
        assert (await prefs(ctx))["file_type"] == expected, arg
    ev = await say(ctx, OWNER, "/filetype")
    assert "FILE TYPE" in ev.texts[0]
    ev = await say(ctx, OWNER, "/filetype bogus")
    assert "FILE TYPE" in ev.texts[0] and (await prefs(ctx))["file_type"] == "same"


async def test_metadata_command(ctx):
    await say(ctx, OWNER, "/metadata on")
    assert (await prefs(ctx))["metadata_on"] is True
    await say(ctx, OWNER, "/metadata off")
    assert (await prefs(ctx))["metadata_on"] is False
    ev = await say(ctx, OWNER, "/metadata")
    assert "currently: Off" in ev.texts[0]


async def test_the_template_and_caption_of_the_auto_format_are_edited_in_its_formats_screen(ctx):
    auto = await ctx.db.create_format(OWNER, "Mine", kind="auto")
    fid = auto["id"]
    await click(ctx, OWNER, f"fm:in:{fid}:template", msg_id=500)
    await say(ctx, OWNER, "[S{season}E{episode}] {title} [{quality}]", mid=60)
    assert "Template saved" in ctx.client.edited[-1][1]
    assert (await autofmt(ctx))["template"] == "[S{season}E{episode}] {title} [{quality}]"
    assert "template" not in await prefs(ctx) and "caption" not in await prefs(ctx)
    assert "[S{season}E{episode}] {title} [{quality}]" in (await click(ctx, OWNER, "m:rename")).edits[-1][0]  # shown there, not edited

    await click(ctx, OWNER, f"fm:in:{fid}:caption", msg_id=501)
    await say(ctx, OWNER, "<b>{filename}</b> {size}", mid=61)
    assert (await autofmt(ctx))["caption"] == "<b>{filename}</b> {size}"
    assert await ctx.db.count_formats(OWNER) == 1  # both went into the same format


@pytest.mark.parametrize("command", ["template", "caption", "thumb", "delthumb"])
async def test_the_template_caption_and_thumbnail_commands_are_gone(ctx, command):
    assert command not in [name for name, _description in COMMANDS] and command not in ctx.app._command_table()
    await set_auto_thumb(ctx, b"\xff\xd8thumb")
    before = await autofmt(ctx)
    ev = await say(ctx, OWNER, f"/{command} something {{title}}")
    assert ev.out == []  # not a command any more: nothing answers it
    assert await autofmt(ctx) == before and await auto_thumb(ctx) == b"\xff\xd8thumb"


async def test_removewords_command(ctx):
    await say(ctx, OWNER, "/removewords @Chan, [Group]")
    assert (await prefs(ctx))["remove_words"] == ["@Chan", "[Group]"]
    assert "@Chan, [Group]" in (await say(ctx, OWNER, "/removewords")).texts[0]
    await say(ctx, OWNER, "/removewords off")
    assert (await prefs(ctx))["remove_words"] == []


async def test_template_and_caption_prompts_list_each_variable_as_its_own_copyable_block(ctx):
    fid = (await ctx.db.create_format(OWNER, "Mine", kind="auto"))["id"]
    line, blocks = variables_line((await click(ctx, OWNER, f"fm:in:{fid}:template")).edits[-1][0])
    assert blocks == line.split(": ", 1)[1].split() and "{quality}" in blocks and "{3episode}" in blocks and "{4episode}" not in blocks and len(blocks) == 11
    line, blocks = variables_line((await click(ctx, OWNER, f"fm:in:{fid}:caption")).edits[-1][0])
    assert blocks == line.split(": ", 1)[1].split() and "{size}" in blocks and "{3episode}" in blocks and "{4episode}" not in blocks and len(blocks) == 13


async def test_settings_are_per_user(ctx):
    await say(ctx, OWNER, "/mode caption")
    await say(ctx, ADMIN_CFG, "/filetype audio")
    assert (await prefs(ctx, OWNER))["mode"] == "caption" and (await prefs(ctx, OWNER))["file_type"] == "same"
    assert (await prefs(ctx, ADMIN_CFG))["mode"] == "filename" and (await prefs(ctx, ADMIN_CFG))["file_type"] == "audio"


@pytest.mark.parametrize("command,field", [
    ("settitle", "title"), ("setauthor", "author"), ("setartist", "artist"),
    ("setaudio", "audio"), ("setsubtitle", "subtitle"), ("setvideo", "video"),
])
async def test_metadata_field_commands(ctx, command, field):
    ev = await say(ctx, OWNER, f"/{command} {{lang}} Value")
    assert (await prefs(ctx))["meta"][field] == "{lang} Value" and "✅" in ev.texts[0]
    assert "{lang} Value" in (await say(ctx, OWNER, f"/{command}")).texts[0]
    await say(ctx, OWNER, f"/{command} off")
    assert (await prefs(ctx))["meta"][field] == ""


async def test_keepaudio_by_language_and_by_number(ctx):
    ev = await say(ctx, OWNER, "/keepaudio English Japanese")
    s = await prefs(ctx)
    assert s["keep_audio"] == ["english", "japanese"] and s["metadata_on"] is True and "turned it On" in ev.texts[0]
    await say(ctx, OWNER, "/keepaudio 2 1")
    assert (await prefs(ctx))["keep_audio"] == ["2", "1"]
    assert "2, 1" in (await say(ctx, OWNER, "/keepaudio")).texts[0]
    await say(ctx, OWNER, "/keepaudio off")
    assert (await prefs(ctx))["keep_audio"] == []
    ev = await say(ctx, OWNER, "/keepaudio 0")
    assert ev.texts[0].startswith("❌")


async def test_keepsubtitle_is_independent_of_keepaudio(ctx):
    await say(ctx, OWNER, "/keepaudio 2 1")
    await say(ctx, OWNER, "/keepsubtitle english")
    s = await prefs(ctx)
    assert s["keep_audio"] == ["2", "1"] and s["keep_subtitle"] == ["english"]
    await say(ctx, OWNER, "/keepsubtitle off")
    s = await prefs(ctx)
    assert s["keep_audio"] == ["2", "1"] and s["keep_subtitle"] == []


async def test_setchannel(ctx):
    assert "not set" in (await say(ctx, OWNER, "/setchannel")).texts[0]
    ev = await say(ctx, OWNER, "/setchannel -1002223334445")
    assert (await prefs(ctx))["target_channel"] == -1002223334445 and "✅" in ev.texts[0]
    ev = await say(ctx, OWNER, "/setchannel @name")
    assert ev.texts[0].startswith("❌") and (await prefs(ctx))["target_channel"] == -1002223334445
    ctx.client.bad_entities.add(-1005556667778)
    ev = await say(ctx, OWNER, "/setchannel -1005556667778")
    assert "can't access" in ev.texts[0] and "send any message there" in ev.texts[0] and (await prefs(ctx))["target_channel"] == -1002223334445
    await say(ctx, OWNER, "/setchannel off")
    assert (await prefs(ctx))["target_channel"] is None


# ---------------------------------------------------------------------------
# thumbnails
# ---------------------------------------------------------------------------


async def send_photo(ctx, mid=20, sender=OWNER, data=None, **kw):
    ev = FakeEvent(sender, message=FakeMessage("", mid=mid, photo=True, **kw), photo_bytes=jpeg() if data is None else data)
    await dispatch(ctx.client, ev)
    return ev


async def test_a_photo_sent_or_forwarded_to_the_bot_is_not_made_a_thumbnail(ctx):
    ev = await send_photo(ctx)
    assert ev.out == [] and ctx.client.files == [] and ctx.client.edited == []  # no answer, nothing saved
    assert await ctx.db.count_formats(OWNER) == 0  # not even a format was made for it

    auto = await ctx.db.create_format(OWNER, "Auto", kind="auto")
    manual = await ctx.db.create_format(OWNER, "Manual", kind="manual")
    assert (await send_photo(ctx, 21)).out == []
    assert (await send_photo(ctx, 22, fwd=True)).out == []  # a forwarded one is no different
    assert await ctx.db.get_format_thumb(OWNER, auto["id"]) is None and await ctx.db.get_format_thumb(OWNER, manual["id"]) is None
    assert not (await autofmt(ctx))["has_thumb"] and ctx.client.files == []


async def test_only_the_thumbnail_prompt_of_a_format_takes_a_photo(ctx):
    auto = await ctx.db.create_format(OWNER, "Auto", kind="auto")
    for kind in ("remove", "mt_title", "keep_audio"):  # no other prompt takes it
        ctx.app.pending[OWNER] = Pending(kind, OWNER, 1, "rename", False, time.time() + 100)
        assert (await send_photo(ctx)).out == [] and ctx.app.pending[OWNER].kind == kind
    ctx.app.pending[OWNER] = Pending("fmt:template", OWNER, 1, "formats", False, time.time() + 100, fid=auto["id"])
    assert (await send_photo(ctx)).out == [] and ctx.app.pending[OWNER].kind == "fmt:template"
    ctx.app.pending[OWNER] = Pending("fmt:thumb", OWNER, 1, "formats", False, time.time() - 1, fid=auto["id"])  # one that ran out
    assert (await send_photo(ctx)).out == []
    assert await ctx.db.get_format_thumb(OWNER, auto["id"]) is None

    ctx.app.pending[OWNER] = Pending("fmt:thumb", OWNER, 500, "formats", False, time.time() + 100, fid=auto["id"])
    await send_photo(ctx, 30)  # the one that is waiting for it does
    assert (await ctx.db.get_format_thumb(OWNER, auto["id"]))[:2] == b"\xff\xd8" and OWNER not in ctx.app.pending


async def test_unreadable_photos_are_reported_while_a_format_waits_for_its_thumbnail(ctx):
    auto = await ctx.db.create_format(OWNER, "Auto", kind="auto")
    for data, said in ((b"not an image", "couldn't read"), (None, "couldn't download")):
        ctx.app.pending[OWNER] = Pending("fmt:thumb", OWNER, 500, "formats", False, time.time() + 100, fid=auto["id"])
        ev = FakeEvent(OWNER, message=FakeMessage("", photo=True), photo_bytes=data)
        await dispatch(ctx.client, ev)
        assert said in ev.texts[0] and OWNER in ctx.app.pending  # and the prompt stays open for another try
    assert await ctx.db.get_format_thumb(OWNER, auto["id"]) is None
    stranger = await send_photo(ctx, sender=STRANGER)
    assert stranger.out == [] and await autofmt(ctx, STRANGER) is None


async def test_the_buttons_of_the_old_caption_template_and_thumbnail_screens_do_nothing(ctx):
    auto = await set_auto_thumb(ctx, b"\xff\xd8keep")
    await edit_auto(ctx, template="{title}", caption="<i>{filename}</i>")
    before = await autofmt(ctx)
    old = ["m:cap", "in:template", "in:caption", "clr:caption"] + [f"th:{a}{b}" for a in ("view", "del", "rm") for b in ("", f":{auto['id']}")]
    for data in old:
        cb = await click(ctx, OWNER, data)
        assert cb.edits == [] and cb.alerts == [] and not cb.deleted, data
    assert not ctx.app.pending and ctx.client.files == []
    assert await autofmt(ctx) == before and await auto_thumb(ctx) == b"\xff\xd8keep"  # nothing was changed


# ---------------------------------------------------------------------------
# renaming
# ---------------------------------------------------------------------------


async def test_a_file_is_queued_with_the_template_name_and_a_cancel_button(ctx):
    ev = await send_doc(ctx, OWNER, mid=10)
    (job,) = ctx.app.queue.jobs.values()
    assert job.new_name == EPISODE_NEW and (job.user_id, job.chat_id, job.msg_id) == (OWNER, OWNER, 10)
    assert job.file_name == EPISODE and job.size == 50 * 1024 * 1024 and job.fmt is None and job.state == "queued"
    text, buttons, kw = ev.out[0]
    assert "Queued" in text and "starting soon" in text and EPISODE_NEW in text
    assert flat_buttons(buttons) == [("✖ Cancel", f"job:x:{job.id}")]
    assert job.status_msg is not None

    ev = await send_doc(ctx, OWNER, "Another.File.mp4", mid=11)
    assert "1 ahead of it" in ev.texts[0]


async def test_caption_mode_reads_the_caption(ctx):
    await say(ctx, OWNER, "/mode caption")
    await send_doc(ctx, OWNER, "xyz123.mkv", caption="Tokyo Revengers S02E07 720p", mid=12)
    (job,) = ctx.app.queue.jobs.values()
    assert job.new_name.endswith(".mkv") and "Tokyo Revengers" in job.new_name and "S02E07" in job.new_name


async def test_remove_words_are_applied_to_the_new_name(ctx):
    await say(ctx, OWNER, "/removewords @SomeChannel")
    await send_doc(ctx, OWNER, "[@SomeChannel] Tokyo Revengers - 05 (1080p).mkv")
    (job,) = ctx.app.queue.jobs.values()
    assert "SomeChannel" not in job.new_name and "Tokyo Revengers" in job.new_name


async def test_the_one_piece_style_name_gets_its_episode_number(ctx):
    await edit_auto(ctx, template="One Piece - E{episode}")
    ev = await send_doc(ctx, OWNER, "1147 - One Piece [Dual] 720p @Anime_X_Universe.mkv", mid=21)
    (job,) = ctx.app.queue.jobs.values()
    assert job.new_name == "One Piece - E1147.mkv" and job.notes == []
    assert "One Piece - E1147.mkv" in ev.texts[0] and "⚠️" not in ev.texts[0]


async def test_a_template_variable_that_cannot_be_filled_is_flagged(ctx):
    await edit_auto(ctx, template="One Piece - E{episode}")
    ev = await send_doc(ctx, OWNER, "Plain Movie.mkv")
    (job,) = ctx.app.queue.jobs.values()
    assert job.new_name == "One Piece - E.mkv"
    assert "⚠️" in ev.texts[0] and "{episode}" in ev.texts[0] and "file name" in ev.texts[0] and "change the template" in ev.texts[0]
    assert job.notes and "{episode}" in job.notes[0]

    await say(ctx, OWNER, "/mode caption")
    ev = await send_doc(ctx, OWNER, "Plain Movie.mkv", mid=22)
    assert "in the caption" in ev.texts[0]


async def test_the_default_template_never_warns(ctx):
    ev = await send_doc(ctx, OWNER, "Plain Movie.mkv")
    assert "⚠️" not in ev.texts[0]  # bracket groups vanish on purpose


class SlowEvent(FakeEvent):
    """A message whose reply takes a while, like a slow round trip to Telegram."""

    def __init__(self, *a, delay=0.0, **kw):
        super().__init__(*a, **kw)
        self.delay = delay

    async def reply(self, text=None, **kw):
        await asyncio.sleep(self.delay)
        return await super().respond(text, **kw)


async def test_jobs_are_ordered_by_the_message_that_triggered_them(ctx):
    await send_doc(ctx, OWNER, "B.mkv", mid=31)
    await send_doc(ctx, OWNER, "A.mkv", mid=30)
    await send_doc(ctx, OWNER, "old.mkv", 1024, mid=34)
    assert sorted(j.order for j in ctx.app.queue.jobs.values()) == [30, 31, 34]
    assert [j.file_name for j in ctx.app.queue.for_user(OWNER)] == ["A.mkv", "B.mkv", "old.mkv"]


async def test_a_forwarded_batch_comes_out_in_the_order_it_was_sent(ctx):
    """Messages that arrive together are handled concurrently; the results must still be in order."""
    ran = []

    async def runner(job):
        ran.append(job.file_name)

    ctx.app.queue = JobQueue(runner, workers=1, settle=0.3)
    ctx.app.queue.start()
    await asyncio.sleep(0.02)  # the worker is idle, waiting for files
    names = {
        1145: "1145 - One Piece [Dual] 720p @Anime_X_Universe.mkv",
        1146: "1146 - One Piece [Dual] 720p @Anime_X_Universe.mkv",
        1147: "1147 - One Piece [Dual] 720p @Anime_X_Universe.mkv",
    }
    delays = {1145: 0.10, 1146: 0.00, 1147: 0.05}  # the replies finish in a different order than the files arrived
    events = [
        SlowEvent(OWNER, message=document_message(name, 1024, mid=mid), delay=delays[mid]) for mid, name in names.items()
    ]
    await asyncio.gather(*(dispatch(ctx.client, ev) for ev in reversed(events)))
    await asyncio.sleep(0.7)
    assert ran == [names[1145], names[1146], names[1147]]


async def test_oversized_files_are_refused(ctx):
    ev = await send_doc(ctx, OWNER, size=101 * 1024 * 1024)
    assert "limit is 100.00 MB" in ev.texts[0] and not ctx.app.queue.jobs
    ev = await send_doc(ctx, OWNER, size=100 * 1024 * 1024)  # exactly at the limit is fine
    assert "Queued" in ev.texts[0]


async def test_stickers_voice_notes_and_gifs_are_not_renamed(ctx):
    for flags in ({"sticker": True}, {"voice": True}, {"gif": True}, {"video_note": True}):
        ev = FakeEvent(OWNER, message=FakeMessage("", file=make_file("x.webm"), **flags))
        await dispatch(ctx.client, ev)
        assert ev.out == [], flags
    assert not ctx.app.queue.jobs
    assert is_renameable(document_message()) and not is_renameable(FakeMessage("text"))


async def test_a_file_without_a_name_gets_a_generated_one(ctx):
    ev = FakeEvent(OWNER, message=FakeMessage("", mid=77, file=make_file(None, 1024, "video/mp4", ext=".mp4"), video=True))
    await dispatch(ctx.client, ev)
    (job,) = ctx.app.queue.jobs.values()
    assert job.file_name == "video_77.mp4" and job.new_name.endswith(".mp4")


# ---------------------------------------------------------------------------
# queue and cancel
# ---------------------------------------------------------------------------


async def test_queue_listing_is_scoped_to_the_user_unless_owner(ctx):
    assert "Nothing in the queue" in (await say(ctx, OWNER, "/queue")).texts[0]
    await send_doc(ctx, OWNER, "Owner.File.mkv")
    await send_doc(ctx, ADMIN_CFG, "Admin.File.mkv")
    admin_view = (await say(ctx, ADMIN_CFG, "/queue")).texts[0]
    assert "Admin File" in admin_view and "Owner File" not in admin_view
    owner_view = (await say(ctx, OWNER, "/queue")).texts[0]
    assert "Admin File" in owner_view and "Owner File" in owner_view and str(ADMIN_CFG) in owner_view


async def test_cancel_commands_respect_ownership(ctx):
    await send_doc(ctx, OWNER, "Owner.File.mkv")      # job 1
    await send_doc(ctx, ADMIN_CFG, "Admin.File.mkv")  # job 2
    await send_doc(ctx, ADMIN_CFG, "Admin.Two.mkv")   # job 3

    assert "couldn't find" in (await say(ctx, ADMIN_CFG, "/cancel 1")).texts[0]  # not theirs
    assert ctx.app.queue.jobs[1].state == "queued"
    assert "Cancelling 2 job(s)" in (await say(ctx, ADMIN_CFG, "/cancel all")).texts[0]  # only their own
    assert set(ctx.app.queue.jobs) == {1}
    assert "no active jobs" in (await say(ctx, ADMIN_CFG, "/cancel")).texts[0]

    await send_doc(ctx, ADMIN_CFG, "Admin.Three.mkv")  # job 4
    assert "Cancelling 2 job(s)" in (await say(ctx, OWNER, "/cancel all")).texts[0]  # owner: jobs 1 and 4
    assert not ctx.app.queue.jobs


async def test_cancelled_queued_jobs_update_their_status_message(ctx):
    await send_doc(ctx, OWNER)
    await say(ctx, OWNER, "/cancel 1")
    for _ in range(5):
        await asyncio.sleep(0)
    (entity, text, kw), = ctx.client.edited
    assert "Cancelled" in text and kw["buttons"] is None


async def test_cancel_button_on_a_queued_file(ctx):
    await send_doc(ctx, OWNER)
    await send_doc(ctx, OWNER, "Second.mkv")
    cb = await click(ctx, ADMIN_CFG, "job:x:1")  # somebody else's job
    assert cb.toasts == ["That job already finished."] and 1 in ctx.app.queue.jobs
    cb = await click(ctx, OWNER, "job:x:1")
    assert cb.toasts == ["Cancelling…"] and 1 not in ctx.app.queue.jobs
    assert (await click(ctx, OWNER, "job:x:1")).toasts == ["That job already finished."]
    admin_doc = await send_doc(ctx, ADMIN_CFG, "Admin.mkv")
    assert admin_doc.out
    assert (await click(ctx, OWNER, "job:x:3")).toasts == ["Cancelling…"]  # the owner may cancel anything


async def test_cancel_clears_a_pending_prompt_first(ctx):
    ctx.app.pending[OWNER] = Pending("template", OWNER, 1, "rename", False, time.time() + 100)
    await send_doc(ctx, OWNER)
    ev = await say(ctx, OWNER, "/cancel")
    assert "nothing was changed" in ev.texts[0] and OWNER not in ctx.app.pending
    assert ctx.app.queue.jobs  # the job is untouched
    assert "Cancelling 1 job(s)" in (await say(ctx, OWNER, "/cancel")).texts[0]


# ---------------------------------------------------------------------------
# admin management, status and stats
# ---------------------------------------------------------------------------


async def test_owner_manages_admins(ctx):
    ev = await say(ctx, OWNER, "/addadmin 555555")
    assert "can now use the bot" in ev.texts[0] and 555555 in ctx.app.admins
    assert 555555 in await ctx.db.list_admins()
    assert (await say(ctx, 555555, "/help")).out  # works immediately
    assert "already have access" in (await say(ctx, OWNER, "/addadmin 555555")).texts[0]
    assert "already have access" in (await say(ctx, OWNER, f"/addadmin {OWNER}")).texts[0]
    assert "already have access" in (await say(ctx, OWNER, f"/addadmin {ADMIN_CFG}")).texts[0]
    for bad in ("/addadmin", "/addadmin abc", "/addadmin 12", "/addadmin @user"):
        assert "Usage" in (await say(ctx, OWNER, bad)).texts[0], bad

    listing = (await say(ctx, OWNER, "/admins")).texts[0]
    assert str(OWNER) in listing and "555555" in listing and str(ADMIN_CFG) in listing

    assert "was removed" in (await say(ctx, OWNER, "/deladmin 555555")).texts[0]
    assert 555555 not in ctx.app.admins and 555555 not in await ctx.db.list_admins()
    assert (await say(ctx, 555555, "/help")).out == []  # access gone
    assert "wasn't in the list" in (await say(ctx, OWNER, "/deladmin 555555")).texts[0]
    assert "ADMIN_IDS" in (await say(ctx, OWNER, f"/deladmin {ADMIN_CFG}")).texts[0]
    assert ADMIN_CFG in ctx.app.admins
    assert "Usage" in (await say(ctx, OWNER, "/deladmin")).texts[0]


async def test_status_pings_telegram_and_reports_version_info(ctx):
    ev = await say(ctx, OWNER, "/status")
    text = ev.texts[0]
    assert "Bot status" in text and "Uptime" in text and "Ping" in text and "12 ms" in text and "Version" in text
    assert "TDLib" in text and "pytdbot" in text and "Telethon" not in text
    assert ctx.client.pings == 1
    assert (await say(ctx, ADMIN_CFG, "/status")).out  # admins may use it too


async def test_status_shows_what_makes_transfers_fast_and_how_they_went(ctx, monkeypatch):
    monkeypatch.setattr(transfer, "stats", transfer.TransferStats(downloads=4, uploads=3, streamed=1))
    text = (await say(ctx, OWNER, "/status")).texts[0]
    assert "<b>Speed</b>" in text and "Transfers: TDLib" in text and "Event loop:" in text
    assert "<b>Downloads:</b> 4 · ⬆ <b>Uploads:</b> 3" in text and "<b>Streamed renames:</b> 1" in text
    assert "no streaming" not in text and "Streaming failed" not in text  # streaming is off by default: nothing to say about it failing
    transfer.stats.stream_failed(RuntimeError("boom"))
    text = (await say(ctx, OWNER, "/status")).texts[0]
    assert "Streaming failed 1×" in text and "RuntimeError: boom" in text


async def test_status_survives_a_failed_ping(ctx, monkeypatch, caplog):
    async def broken():
        raise ConnectionError("offline")

    monkeypatch.setattr(ctx.client, "ping", broken)
    assert "n/a" in (await say(ctx, OWNER, "/status")).texts[0]
    assert "ping failed: ConnectionError: offline" in caplog.text  # the reason is in the Space log, not only "n/a" on screen


async def test_stats_reports_users_files_and_system_usage(ctx):
    await say(ctx, ADMIN_CFG, "/help")
    await ctx.db.record_job(OWNER, 3 * 1024 ** 3)
    text = (await say(ctx, OWNER, "/stats")).texts[0]
    for expected in ("System stats", "Users", "Files renamed", "3.00 GB", "Queue", "CPU", "RAM", "Disk"):
        assert expected in text, expected
    assert (await say(ctx, ADMIN_CFG, "/stats")).out
    assert (await say(ctx, STRANGER, "/stats")).out == []


# ---------------------------------------------------------------------------
# /tracks
# ---------------------------------------------------------------------------


async def test_tracks_needs_a_reply_to_a_file(ctx):
    for reply in (None, FakeMessage("text", mid=3)):
        assert "Reply to a video or audio file" in (await say(ctx, OWNER, "/tracks", reply=reply)).texts[0]


@needs_ffmpeg
async def test_tracks_lists_numbered_audio_and_subtitles_and_cleans_up(ctx, media_dir):
    sample = media_dir / "sample.mkv"
    ctx.client.file_data[33] = sample.read_bytes()
    reply = document_message("Show.mkv", sample.stat().st_size, mid=33)
    ev = await say(ctx, OWNER, "/tracks", reply=reply, mid=34)
    assert ev.texts == ["🔎 Reading the tracks…"]
    (status, text, kw), = ctx.client.edited
    assert status.id == 1001 and "Show.mkv" in text
    assert "Audio" in text and "Subtitles" in text and "Japanese" in text and "English" in text and "use these numbers" in text
    (downloaded,) = ctx.client.downloaded_to  # only the start of the file was asked for
    assert downloaded[0] == 33 and downloaded[2] == appmod.TRACKS_PROBE_BYTES
    assert not list(Path(ctx.cfg.temp_dir).glob("tracks*"))


@needs_ffmpeg
async def test_tracks_downloads_the_whole_file_when_the_start_is_not_enough(ctx, media_dir, monkeypatch):
    calls = []

    async def fake_download(message, dest, *, progress=None, cancel=None, max_bytes=None):
        calls.append(max_bytes)
        if max_bytes is not None:
            Path(dest).write_bytes(b"\x00" * 4096)  # e.g. an mp4 whose index is at the end
        else:
            shutil.copy(media_dir / "sample.mp4", dest)
        return Path(dest).stat().st_size

    monkeypatch.setattr(ctx.client, "download_to", fake_download)
    reply = document_message("Show.mp4", 40 * 1024 * 1024, mid=33)
    await say(ctx, OWNER, "/tracks", reply=reply, mid=34)
    assert calls == [appmod.TRACKS_PROBE_BYTES, None]
    assert "Audio" in ctx.client.edited[-1][1] and "needs the whole file" in ctx.client.edited[0][1]


@needs_ffmpeg
async def test_tracks_gives_up_gracefully(ctx):
    ctx.client.file_data[33] = b"\x00" * 4096  # whatever is downloaded, it is no media
    for size in (2 * 1024 * 1024, 900 * 1024 * 1024):  # small file / too big for a full download
        ctx.client.edited.clear()
        reply = document_message("Bad.mkv", size, mid=33)
        await say(ctx, OWNER, "/tracks", reply=reply, mid=34)
        assert "couldn't read any tracks" in ctx.client.edited[-1][1], size
    assert not list(Path(ctx.cfg.temp_dir).glob("tracks*"))


# ---------------------------------------------------------------------------
# menus (callbacks)
# ---------------------------------------------------------------------------


async def test_menu_navigation_edits_the_message_in_place(ctx):
    cb = await click(ctx, OWNER, "m:meta")
    text, buttons = cb.edits[-1]
    assert "metadata is currently" in text and "m:tool" in data_of(buttons)
    assert cb.answers == [(None, False)]
    cb = await click(ctx, OWNER, "m:tool")
    assert "TOOL" in cb.edits[-1][0] and "m:meta" in data_of(cb.edits[-1][1])
    cb = await click(ctx, OWNER, "m:home")
    assert "Auto Rename Bot" in cb.edits[-1][0]
    cb = await click(ctx, OWNER, "m:admin")
    assert "ADMIN" in cb.edits[-1][0]
    cb = await click(ctx, OWNER, "m:bogus")
    assert cb.edits == [] and cb.answers == [(None, False)]


async def test_toggles_update_settings_and_refresh_the_panel(ctx):
    cb = await click(ctx, OWNER, "set:mode:caption")
    assert (await prefs(ctx))["mode"] == "caption" and cb.toasts == ["Mode: Caption"]
    assert "Caption ✅" in [label for label, _d in flat_buttons(cb.edits[-1][1])]
    cb = await click(ctx, OWNER, "set:ftype:video")
    assert (await prefs(ctx))["file_type"] == "video" and cb.toasts == ["File type: Video"]
    cb = await click(ctx, OWNER, "set:ftype:same")
    assert cb.toasts == ["File type: Same as original"]
    cb = await click(ctx, OWNER, "set:meta:on")
    assert (await prefs(ctx))["metadata_on"] is True and cb.toasts == ["Metadata ON"]
    await click(ctx, OWNER, "set:meta:off")
    assert (await prefs(ctx))["metadata_on"] is False

    before = await prefs(ctx)
    for junk in ("set:mode:bogus", "set:nothing:x", "set:ftype:zip", "set:meta:maybe", "set:mode"):
        cb = await click(ctx, OWNER, junk)
        assert cb.edits == [] and cb.answers == [(None, False)], junk
    assert await prefs(ctx) == before


async def test_clear_buttons(ctx):
    await say(ctx, OWNER, "/keepaudio 2 1")
    await say(ctx, OWNER, "/keepsubtitle 1")
    cb = await click(ctx, OWNER, "clr:keep_audio")
    s = await prefs(ctx)
    assert s["keep_audio"] == [] and s["keep_subtitle"] == ["1"] and "TOOL" in cb.edits[-1][0]
    await click(ctx, OWNER, "clr:keep_subtitle")
    assert (await prefs(ctx))["keep_subtitle"] == []
    cb = await click(ctx, OWNER, "clr:whatever")
    assert cb.edits == []


async def test_close_deletes_the_message_and_falls_back_to_editing(ctx):
    ctx.app.pending[OWNER] = Pending("remove", OWNER, 1, "rename", False, time.time() + 100)
    cb = await click(ctx, OWNER, "x:close")
    assert cb.deleted and OWNER not in ctx.app.pending
    old = FakeCallback(OWNER, "x:close")
    old.delete_error = RuntimeError("MESSAGE_DELETE_FORBIDDEN")
    await dispatch(ctx.client, old)
    assert old.edits == [("Closed.", None)] and not old.deleted


async def test_metadata_help_and_unknown_callbacks(ctx):
    cb = await click(ctx, OWNER, "help:meta")
    assert "How to set metadata" in cb.edits[-1][0]
    for junk in ("", "zzz", "adm", "th:what", "job:x:abc", "in:nope"):
        cb = await click(ctx, OWNER, junk)
        assert cb.answers and not cb.alerts, junk


async def test_admin_panel_buttons(ctx):
    await send_doc(ctx, OWNER)
    await send_doc(ctx, OWNER, "Two.mkv")
    cb = await click(ctx, OWNER, "adm:stats", chat=OWNER)
    (chat, text, kw), = ctx.client.sent
    assert chat == OWNER and "System stats" in text and "2 waiting" in text
    await click(ctx, OWNER, "adm:status")
    assert "Bot status" in ctx.client.sent[-1][1] and "<b>Speed</b>" in ctx.client.sent[-1][1]
    cb = await click(ctx, OWNER, "adm:clear")
    assert cb.alerts == ["Cancelling 2 job(s)."] and not ctx.app.queue.jobs


@pytest.mark.parametrize("panel", sorted(PANEL_NAMES))
async def test_every_button_of_every_panel_is_handled(ctx, panel):
    await set_auto_thumb(ctx, b"\xff\xd8thumb")
    await ctx.db.create_format(OWNER, "Other auto", kind="auto")
    await ctx.db.create_format(OWNER, "Manual one", kind="manual")
    _text, rows = await ctx.app.panel(OWNER, panel)
    clicked = 0
    for row in rows:
        for _label, data in row:
            cb = await click(ctx, OWNER, data)
            assert cb.answers or cb.deleted, (panel, data)
            assert not any("went wrong" in a for a in cb.alerts), (panel, data)
            clicked += 1
    assert clicked >= 1


@pytest.mark.parametrize("kind", sorted(panels.PROMPTS))
async def test_every_prompt_opens_and_cancels(ctx, kind):
    cb = await click(ctx, OWNER, f"in:{kind}")
    p = ctx.app.pending[OWNER]
    assert p.kind == kind and p.back in PANEL_NAMES and not p.is_photo
    text, buttons = cb.edits[-1]
    assert text == panels.PROMPTS[kind] and data_of(buttons) == [f"in:cancel:{p.back}"]
    cb = await click(ctx, OWNER, f"in:cancel:{p.back}")
    assert OWNER not in ctx.app.pending and cb.toasts == ["Cancelled."] and cb.edits


# ---------------------------------------------------------------------------
# "tap a button, then send the value"
# ---------------------------------------------------------------------------


async def test_remove_words_prompt_flow(ctx):
    await click(ctx, OWNER, "in:remove", msg_id=500)
    ev = await say(ctx, OWNER, "@Chan, [Group]", mid=60)
    assert ev.deleted and OWNER not in ctx.app.pending  # the typed value is removed to keep the chat tidy
    assert (await prefs(ctx))["remove_words"] == ["@Chan", "[Group]"]
    (chat, text, kw), = ctx.client.edited
    assert chat == OWNER and kw["message"] == 500 and "I'll strip" in text and "RENAME MODE" in text
    assert "set:mode:filename" in data_of(kw["buttons"]) and "in:template" not in data_of(kw["buttons"])


async def test_keep_audio_prompt_flow_enables_metadata_and_returns_to_the_tool_panel(ctx):
    await click(ctx, OWNER, "in:keep_audio")
    await say(ctx, OWNER, "English Japanese")
    s = await prefs(ctx)
    assert s["keep_audio"] == ["english", "japanese"] and s["metadata_on"] is True
    (chat, text, kw), = ctx.client.edited
    assert "TOOL" in text and "English, Japanese" in text and "in:keep_subtitle" in data_of(kw["buttons"])


async def test_invalid_input_keeps_the_prompt_open(ctx):
    await click(ctx, OWNER, "in:mt_title")
    ev = await say(ctx, OWNER, "x" * 300)
    assert "too long" in ev.texts[0] and "Try again" in ev.texts[0] and not ev.deleted
    assert OWNER in ctx.app.pending and ctx.client.edited == []
    ev = await say(ctx, OWNER, "My Title")
    assert OWNER not in ctx.app.pending and (await prefs(ctx))["meta"]["title"] == "My Title"


async def test_expired_prompts_and_other_users_text_are_ignored(ctx):
    await click(ctx, OWNER, "in:mt_title")
    assert (await say(ctx, ADMIN_CFG, "Title")).out == []  # not their prompt
    assert OWNER in ctx.app.pending
    ctx.app.pending[OWNER].expires = time.time() - 1
    ev = await say(ctx, OWNER, "Title")
    assert ev.out == [] and not ev.deleted and OWNER not in ctx.app.pending
    assert (await prefs(ctx))["meta"]["title"] == "" and (await prefs(ctx, ADMIN_CFG))["meta"]["title"] == ""  # nothing was saved


async def test_plain_text_without_a_prompt_is_ignored(ctx):
    ev = await say(ctx, OWNER, "hello bot")
    assert ev.out == [] and not ev.deleted


async def test_prompt_on_a_photo_banner_message_is_flagged_and_long_updates_fall_back(ctx, monkeypatch):
    await click(ctx, OWNER, "in:mt_title", photo=True)
    assert ctx.app.pending[OWNER].is_photo and ctx.app.pending[OWNER].back == "meta"
    monkeypatch.setattr(appmod, "visible_len", lambda _t: 5000)  # pretend it would not fit a caption
    await say(ctx, OWNER, "My Title")
    (_chat, text, _kw), = ctx.client.edited
    assert "Open the menu to see the update" in text and "My Title" in text
    assert (await prefs(ctx))["meta"]["title"] == "My Title"


async def test_when_the_menu_message_cannot_be_edited_a_new_one_is_sent(ctx):
    async def refuse(*a, **k):
        raise RuntimeError("MESSAGE_ID_INVALID")

    await click(ctx, OWNER, "in:remove")
    ctx.client.edit_message = refuse
    ev = await say(ctx, OWNER, "@Chan, [Group]")
    assert (await prefs(ctx))["remove_words"] == ["@Chan", "[Group]"]
    assert "I'll strip" in ev.texts[0] and ev.out[0][1]  # sent as a fresh message with buttons


# ---------------------------------------------------------------------------
# robustness
# ---------------------------------------------------------------------------


async def test_handler_failures_are_reported_to_the_user(ctx, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("database is down")

    monkeypatch.setattr(ctx.db, "get_settings", boom)
    ev = await say(ctx, OWNER, "/start")
    assert ev.texts == ["⚠️ Something went wrong. Please try again."]
    cb = await click(ctx, OWNER, "m:meta")
    assert cb.alerts == ["⚠️ Something went wrong."]
    # and the bot keeps working afterwards
    monkeypatch.undo()
    assert "Auto Rename Bot" in (await say(ctx, OWNER, "/start")).texts[0]


async def test_handler_failures_reach_the_log_channel_with_the_update_and_the_traceback(ctx, monkeypatch):
    channel_log = errorlog.install(ctx.app._log_send, errorlog.secrets_from(ctx.cfg))  # the root logger, as in main.py

    async def boom(*a, **k):
        raise RuntimeError("database is down")

    monkeypatch.setattr(ctx.db, "get_settings", boom)
    ev = await say(ctx, OWNER, "/start")
    await channel_log.aclose()
    assert ev.texts == ["⚠️ Something went wrong. Please try again."]
    (_chat, text, kw), = [s for s in ctx.client.sent if s[0] == LOG]
    assert kw["parse_mode"] == "html"
    assert text.startswith(
        '<pre><code class="language-python">Error handling update:\n'
        f"FakeEvent(sender_id={OWNER}, chat_id={OWNER}, message_id=1, text='/start')\n\nTraceback (most recent call last):"
    )
    assert text.endswith("RuntimeError: database is down</code></pre>")


async def test_an_error_inside_a_helper_still_names_the_update_that_started_it(ctx, monkeypatch):
    channel_log = errorlog.install(ctx.app._log_send)

    async def boom(*a, **k):
        raise RuntimeError("telegraph exploded")

    monkeypatch.setattr(ctx.app, "_mediainfo", boom)
    reply = document_message("Movie.mkv", 5 * 1024 * 1024)
    await say(ctx, OWNER, "/mediainfo", reply=reply)
    await channel_log.aclose()
    (_chat, text, _kw), = [s for s in ctx.client.sent if s[0] == LOG]
    assert text.startswith('<pre><code class="language-python">/mediainfo failed\nFakeEvent(sender_id=')
    assert "text='/mediainfo'" in text and text.endswith("RuntimeError: telegraph exploded</code></pre>")


async def test_a_file_is_handled_by_exactly_one_handler(ctx):
    ctx.app.pending[OWNER] = Pending("template", OWNER, 1, "rename", False, time.time() + 100)
    ev = await send_doc(ctx, OWNER)
    assert len(ev.out) == 1 and "Queued" in ev.texts[0]
    assert OWNER in ctx.app.pending  # a file does not feed the text prompt
