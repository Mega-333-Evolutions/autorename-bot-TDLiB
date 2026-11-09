"""Uploading a manually renamed episode to the Database Channel and having the channel post bot publish it:
the two chats in the settings, the Channel option of a manual format, the offer, the upload, the notice in the
communication chat and the answers of the channel post bot."""
import asyncio
import re
import time
from types import SimpleNamespace

import pytest

from bot import panels
from bot.app import App
from bot.config import ConfigError, load_config
from bot.db import Database, Format
from bot.dbflow import link_notice_text, share_link, classify, delete_button, delete_notice_text, episode_of, line_of, notice_text, post_message_id, reply_target
from bot.formats import default_auto_format
from bot.pipeline import Renamer
from bot.queue import Job
from bot.settings_ops import parse_format_channel
from bot.tg import errors, types

from .appkit import ADMIN_CFG, OWNER, STRANGER, click, data_of, env_for, say
from .tgfakes import FakeCallback, FakeClient, FakeEvent, dispatch, document_message, flat_buttons

DB = -1002000000001  # the Database Channel
COMM = -1003000000002  # the communication chat
POST_BOT = 8950016484
SHOW = -1004000000003  # the channel a format publishes in
NAME = "[S01E05] Tokyo Revengers [1080p].mkv"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@pytest.fixture
async def up(tmp_path):
    """Like ``ctx``, with uploading to the Database Channel switched on (and short waits)."""
    cfg = load_config(env_for(tmp_path, DB_Channel=str(DB), Communication_chat=str(COMM)))
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/app.db")
    await db.init()
    client = FakeClient()
    app = App(client, cfg, db)
    app.dbflow.ack_wait, app.dbflow.result_wait, app.dbflow.delete_wait = 2.0, 4.0, 4.0
    app._register()
    yield SimpleNamespace(app=app, client=client, db=db, cfg=cfg, tmp=tmp_path, flow=app.dbflow)
    await app.shutdown()
    await db.close()


def notices(up):
    """What was written in the communication chat: ``[(message id, text, kwargs)]``."""
    return [(2000 + n, text, kw) for n, (entity, text, kw) in enumerate(up.client.sent, 1) if entity == COMM]


async def wait_for(predicate, timeout=3.0):
    end = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < end, "timed out"
        await asyncio.sleep(0.005)


async def the_notice(up, nth=1):
    await wait_for(lambda: len(notices(up)) >= nth)
    return notices(up)[nth - 1]


def ref_of(text):
    return re.search(r"(?m)^Ref: (\w+)$", text).group(1)


def last_edit(up):
    return up.client.edited[-1][1]


async def post_bot_says(up, text, *, reply_to=None, sender=POST_BOT, chat=COMM):
    ev = FakeEvent(sender, text, chat=chat, private=False, reply_to_id=reply_to)
    await dispatch(up.client, ev)
    return ev


async def start_upload(up, *, channel=SHOW, name=NAME, mid=555, offer_id=77, caption=""):
    """The renamed episode is in the user's chat, and the offer is answered with Yes."""
    up.client.messages[mid] = document_message(name, 10 * 1024 * 1024, mid=mid, caption=caption)
    return await click(up, OWNER, f"dbu:y:{OWNER}:{mid}:{channel or 0}", msg_id=offer_id)


def manual_job(name=NAME, channel=SHOW, kind="manual", chat=OWNER):
    fmt = {"id": 5, "name": "One Piece", "kind": kind, "channel": channel}
    return Job(
        id=1, user_id=OWNER, chat_id=chat, msg_id=40, file_name="original.mkv", size=1, new_name=name, fmt=fmt,
        status_msg=SimpleNamespace(id=99),
    )


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------


def test_both_chats_switch_uploading_on(tmp_path):
    cfg = load_config(env_for(tmp_path, DB_CHANNEL=str(DB), COMMUNICATION_CHAT=str(COMM)))
    assert (cfg.db_channel, cfg.comm_chat, cfg.upload_enabled, cfg.post_bot_id) == (DB, COMM, True, 8950016484)


@pytest.mark.parametrize("db_name, comm_name", [("DB_CHANNEL", "COMMUNICATION_CHAT"), ("db_channel", "communication_chat"), ("DB_Channel", "Communication_chat")])
def test_the_names_may_be_written_in_any_case(tmp_path, db_name, comm_name):
    cfg = load_config(env_for(tmp_path, **{db_name: str(DB), comm_name: str(COMM)}))
    assert (cfg.db_channel, cfg.comm_chat, cfg.upload_enabled) == (DB, COMM, True)


@pytest.mark.parametrize("extra", [{"DB_CHANNEL": str(DB)}, {"COMMUNICATION_CHAT": str(COMM)}, {}])
def test_one_chat_alone_does_not_switch_it_on(tmp_path, extra):
    assert load_config(env_for(tmp_path, **extra)).upload_enabled is False


def test_a_bare_channel_id_gets_its_minus_100(tmp_path):
    cfg = load_config(env_for(tmp_path, DB_CHANNEL="2000000001", COMMUNICATION_CHAT="3000000002"))
    assert (cfg.db_channel, cfg.comm_chat) == (-1002000000001, -1003000000002)


@pytest.mark.parametrize("name", ["DB_CHANNEL", "COMMUNICATION_CHAT", "CHANNEL_POST_BOT_ID"])
def test_an_id_that_is_not_a_number_is_a_configuration_error(tmp_path, name):
    with pytest.raises(ConfigError, match=name):
        load_config(env_for(tmp_path, **{name: "@somewhere"}))


@pytest.mark.parametrize("value", ["", "0", "off", "OFF", "none"])
def test_empty_or_off_means_not_set(tmp_path, value):
    cfg = load_config(env_for(tmp_path, DB_CHANNEL=value, COMMUNICATION_CHAT=value))
    assert cfg.db_channel is None and cfg.comm_chat is None and cfg.upload_enabled is False


def test_the_post_bot_id_can_be_changed(tmp_path):
    assert load_config(env_for(tmp_path, CHANNEL_POST_BOT_ID="12345")).post_bot_id == 12345


async def test_without_both_chats_nothing_is_offered_and_the_buttons_say_so(ctx):
    assert ctx.app.dbflow is None and ctx.app.renamer.on_delivered is None
    cb = await click(ctx, OWNER, f"dbu:y:{OWNER}:555:{SHOW}")
    assert cb.alerts and "switched off" in cb.alerts[0] and not ctx.client.forwarded and not ctx.client.sent


async def test_with_both_chats_the_offer_is_hooked_into_the_pipeline(up):
    assert up.app.renamer.on_delivered == up.flow.offer


# ---------------------------------------------------------------------------
# the Channel option of a manual format
# ---------------------------------------------------------------------------


async def test_a_manual_format_keeps_its_channel(ctx):
    fmt = await ctx.db.create_format(OWNER, "One Piece")
    assert fmt["channel"] is None
    assert (await ctx.db.update_format(OWNER, fmt["id"], channel=SHOW))["channel"] == SHOW
    assert (await ctx.db.get_format(OWNER, fmt["id"]))["channel"] == SHOW
    assert [f["channel"] for f in await ctx.db.list_formats(OWNER, "manual")] == [SHOW]
    assert (await ctx.db.update_format(OWNER, fmt["id"], channel=None))["channel"] is None


async def test_an_auto_format_has_no_channel(ctx):
    fmt = await ctx.db.create_format(OWNER, "Auto One", kind="auto")
    assert (await ctx.db.update_format(OWNER, fmt["id"], channel=SHOW))["channel"] is None


async def test_formats_saved_before_the_option_have_no_channel(ctx):
    async with ctx.db._sessions() as session:
        session.add(Format(user_id=OWNER, name="Old", data='{"template": "{title}", "kind": "manual"}'))
        session.add(Format(user_id=OWNER, name="Odd", data='{"kind": "manual", "channel": "-100123"}'))  # not a number: ignored
        await session.commit()
    assert [f["channel"] for f in await ctx.db.list_formats(OWNER)] == [None, None]


async def test_a_copy_keeps_the_channel_only_for_the_owners_own_manual_copy(ctx):
    fmt = await ctx.db.create_format(OWNER, "One Piece")
    await ctx.db.update_format(OWNER, fmt["id"], channel=SHOW)
    auto = await ctx.db.copy_format(OWNER, OWNER, fmt["id"], kind="auto")
    assert auto["kind"] == "auto" and auto["channel"] is None
    again = await ctx.db.copy_format(OWNER, OWNER, fmt["id"])
    assert again["kind"] == "manual" and again["channel"] == SHOW
    theirs = await ctx.db.copy_format(ADMIN_CFG, OWNER, fmt["id"])  # somebody else adds it from a share card
    assert theirs["kind"] == "manual" and theirs["channel"] is None
    back = await ctx.db.copy_format(OWNER, OWNER, auto["id"], kind="manual")
    assert back["channel"] is None
    assert (await ctx.db.get_format(OWNER, fmt["id"]))["channel"] == SHOW  # the original is untouched


@pytest.mark.parametrize(
    "text, expected",
    [
        ("-1001234567890", -1001234567890),
        ("  -1001234567890 ", -1001234567890),
        ("1234567890", -1001234567890),
        ("https://t.me/c/1234567890/5", -1001234567890),
        ("t.me/c/1234567890", -1001234567890),
        ("off", None), ("None", None), ("-", None), ("clear", None),
    ],
)
def test_a_channel_id_is_read(text, expected):
    assert parse_format_channel(text) == (True, expected, "")


@pytest.mark.parametrize("text", ["", "abc", "@mychannel", "https://t.me/mychannel", "-12345", "123", "-1001234567890x", "12 34567890"])
def test_anything_else_is_refused_with_the_way_to_write_it(text):
    ok, value, message = parse_format_channel(text)
    assert not ok and value is None and "-1001234567890" in message


async def test_without_the_two_chats_the_format_has_no_channel_option(ctx):
    fmt = await ctx.db.create_format(OWNER, "One Piece")
    text, buttons = (await click(ctx, OWNER, f"fm:v:{fmt['id']}")).edits[-1]
    assert "Channel" not in text and not [d for d in data_of(buttons) if "channel" in d]
    cb = await click(ctx, OWNER, f"fm:in:{fmt['id']}:channel")
    assert cb.alerts and OWNER not in ctx.app.pending


async def test_a_manual_format_shows_the_channel_option_and_an_auto_format_does_not(up):
    fmt = await up.db.create_format(OWNER, "One Piece")
    i = fmt["id"]
    text, buttons = (await click(up, OWNER, f"fm:v:{i}")).edits[-1]
    assert "<b>Channel</b> ▸ — (optional)" in text
    assert f"fm:in:{i}:channel" in data_of(buttons) and f"fm:chc:{i}" not in data_of(buttons)
    auto = await up.db.create_format(OWNER, "Auto One", kind="auto")
    text, buttons = (await click(up, OWNER, f"fm:v:{auto['id']}")).edits[-1]
    assert "Channel" not in text and f"fm:in:{auto['id']}:channel" not in data_of(buttons)
    cb = await click(up, OWNER, f"fm:in:{auto['id']}:channel")
    assert cb.alerts and OWNER not in up.app.pending


async def test_the_channel_is_typed_in_checked_and_cleared(up):
    fmt = await up.db.create_format(OWNER, "One Piece")
    i = fmt["id"]
    cb = await click(up, OWNER, f"fm:in:{i}:channel", msg_id=710)
    assert cb.edits[-1][0] == panels.FORMAT_PROMPTS["channel"] and up.app.pending[OWNER].kind == "fmt:channel"
    assert data_of(cb.edits[-1][1]) == [f"fm:cx:{i}"]
    ev = await say(up, OWNER, "@not_an_id")
    assert "❌" in ev.texts[0] and "Try again" in ev.texts[0] and OWNER in up.app.pending
    ev = await say(up, OWNER, "1234567890")  # a bare channel id
    assert ev.deleted and OWNER not in up.app.pending
    assert (await up.db.get_format(OWNER, i))["channel"] == -1001234567890
    chat, text, kw = up.client.edited[-1]
    assert kw["message"] == 710 and "Channel saved" in text and "<code>-1001234567890</code>" in text
    text, buttons = (await click(up, OWNER, f"fm:v:{i}")).edits[-1]
    assert "<b>Channel</b> ▸ <code>-1001234567890</code>" in text and f"fm:chc:{i}" in data_of(buttons)
    cb = await click(up, OWNER, f"fm:chc:{i}")  # the button clears it
    assert (await up.db.get_format(OWNER, i))["channel"] is None and "— (optional)" in cb.edits[-1][0]
    await up.db.update_format(OWNER, i, channel=SHOW)
    await click(up, OWNER, f"fm:in:{i}:channel", msg_id=711)
    await say(up, OWNER, "off")  # and so does typing off
    assert (await up.db.get_format(OWNER, i))["channel"] is None


async def test_the_channel_of_a_deleted_format_is_handled(up):
    fmt = await up.db.create_format(OWNER, "One Piece")
    await click(up, OWNER, f"fm:in:{fmt['id']}:channel")
    await up.db.delete_format(OWNER, fmt["id"])
    ev = await say(up, OWNER, "-1001234567890")
    assert "no longer exists" in ev.texts[0] and OWNER not in up.app.pending


async def test_the_format_screen_with_a_channel_still_fits_a_caption(up):
    fmt = await up.db.create_format(OWNER, "N" * 30, template="T" * 200)
    await up.db.update_format(OWNER, fmt["id"], channel=-1009999999999, caption="C" * 700)
    text, _rows = panels.format_view(await up.db.get_format(OWNER, fmt["id"]), upload_on=True)
    assert len(re.sub(r"<[^>]+>", "", text)) < 1024


def test_a_shared_format_card_does_not_show_the_channel():
    fmt = {**default_auto_format(), "kind": "manual", "name": "One Piece", "id": 5, "channel": SHOW}
    text, _rows = panels.shared_format(fmt, "token")
    assert str(SHOW) not in text and "Channel" not in text


# ---------------------------------------------------------------------------
# the messages
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, episode",
    [
        (NAME, "05"), ("One Piece - E1147.mkv", "1147"), ("Black Clover - S2 E05.mkv", "05"),
        ("Tokyo Revengers Movie.mkv", ""), ("", ""),
    ],
)
def test_the_episode_number_is_read_from_the_renamed_file(name, episode):
    assert episode_of(name) == episode


def test_the_notice_has_what_the_post_bot_needs():
    text = notice_text(episode="05", channel=SHOW, file=NAME, post_id=4001, ref="ab12cd")
    assert text.splitlines() == [
        "File uploaded to the Database Channel", "Episode: 05", f"Channel: {SHOW}", f"File: {NAME}", "Post: 4001", "Ref: ab12cd"
    ]
    bare = notice_text(episode="", channel=SHOW, file=NAME, post_id=None, ref="ab12cd")
    assert "Episode:" not in bare and "Post:" not in bare and f"Channel: {SHOW}" in bare


def test_the_label_lines_of_a_message_are_found():
    text = "✅ Post published successfully\nChannel: Black Clover English\nPost: https://t.me/c/1/2\nRef: ab12cd"
    assert line_of("channel", text) == "Black Clover English" and line_of("post", text) == "https://t.me/c/1/2"
    assert line_of("ref", text) == "ab12cd" and line_of("episode", text) == "" and line_of("ref", None) == ""


@pytest.mark.parametrize(
    "text, expected",
    [
        ("📥 Message received - checking the Database Channel…\nRef: ab12", ("ack", "")),
        ("message received", ("ack", "")),
        ("✅ Post published successfully\nChannel: X", ("ok", "")),
        ("POST PUBLISHED SUCCESSFULLY", ("ok", "")),
        ("❌ Publication failed: no button found\nFile: a.mkv\nRef: ab12", ("fail", "no button found")),
        ("publication failed:   many   spaces  here", ("fail", "many spaces here")),
        ("Publication failed", ("fail", "")),
        ("❌ Publication failed: no post published successfully", ("fail", "no post published successfully")),  # a failure wins
        ("hello", ("other", "")), ("", ("other", "")), (None, ("other", "")),
    ],
)
def test_what_the_post_bot_says_is_told_apart(text, expected):
    assert classify(text) == expected


def test_the_reply_target_is_read_from_the_message():
    assert reply_target(FakeEvent(POST_BOT, "x", reply_to_id=7)) == 7
    assert reply_target(FakeEvent(POST_BOT, "x")) is None


# ---------------------------------------------------------------------------
# the offer after /rename
# ---------------------------------------------------------------------------


async def test_the_offer_asks_yes_or_no_after_the_episode(up):
    await up.app.dbflow.offer(manual_job(), OWNER, SimpleNamespace(id=555))
    (entity, text, kw), = up.client.sent
    assert entity == OWNER and kw["reply_to"] == 555 and kw["parse_mode"] == "html"
    assert "Upload this episode to the Database Channel?" in text and NAME in text
    assert "Episode: <b>05</b>" in text and f"<code>{SHOW}</code>" in text
    assert flat_buttons(kw["buttons"]) == [("✅ Yes", f"dbu:y:{OWNER}:555:{SHOW}"), ("❌ No", f"dbu:n:{OWNER}:555")]


def test_the_buttons_fit_telegrams_limit_even_for_the_biggest_ids():
    data = f"dbu:y:{-1009999999999}:{2147483647}:{-1009999999999}"
    assert len(data.encode()) <= 64


async def test_the_offer_for_a_format_without_a_channel_says_it_is_only_uploaded(up):
    await up.app.dbflow.offer(manual_job(channel=None), OWNER, SimpleNamespace(id=555))
    (_entity, text, kw), = up.client.sent
    assert "has no Channel" in text and "no post is made" in text and "Episode:" not in text
    assert data_of(kw["buttons"])[0] == f"dbu:y:{OWNER}:555:0"


async def test_the_offer_warns_when_the_file_name_has_no_episode_number(up):
    await up.app.dbflow.offer(manual_job(name="Tokyo Revengers Movie.mkv"), OWNER, SimpleNamespace(id=555))
    assert "No episode number found" in up.client.sent[0][1]


async def test_a_file_sent_to_a_channel_is_still_offered_in_the_chat_of_the_user(up):
    await up.app.dbflow.offer(manual_job(), SHOW, SimpleNamespace(id=555))
    (entity, _text, kw), = up.client.sent
    assert entity == OWNER and kw["reply_to"] == 40  # the file the user sent, not the one in the channel
    assert f"dbu:y:{SHOW}:555:{SHOW}" in data_of(kw["buttons"])


async def test_nothing_is_offered_without_a_message_to_point_at(up):
    await up.app.dbflow.offer(manual_job(), OWNER, None)
    assert not up.client.sent


class Reporter:
    def __init__(self, log):
        self.log = log

    async def finish(self, text):
        self.log.append("finished")


async def deliver(up, job, hook, target_channel=None, log=None):
    """Run the delivery step of a rename; ``log`` says in what order the file, the Done message and the hook came."""
    log = [] if log is None else log
    renamer = Renamer(up.client, up.cfg, up.db)
    renamer.on_delivered = hook
    if target_channel:
        await up.db.update_settings(OWNER, target_channel=target_channel)
    original_send = up.client.send_file

    async def send_file(*args, **kw):
        log.append("sent")
        return await original_send(*args, **kw)

    up.client.send_file = send_file
    reporter = Reporter(log)
    target, sent = await renamer._send(
        job, await up.db.get_settings(OWNER), document_message(NAME, 10), "input", caption=None, attrs=[], as_document=True,
        thumb=None, warnings=[], reporter=reporter,
    )
    await renamer._finish(job, target, sent, final_size=10, warnings=[], rates="", started=time.monotonic(), reporter=reporter)
    return log


async def test_a_manual_rename_is_offered_after_the_file_and_the_done_message(up):
    log, calls = [], []

    async def hook(job, target, sent):
        log.append("offer")
        calls.append((job.id, target, sent.id))

    job = manual_job()
    await deliver(up, job, hook, log=log)
    assert job.state == "done" and calls == [(1, OWNER, 3001)] and log == ["sent", "finished", "offer"]


@pytest.mark.parametrize("kind", ["auto", None])
async def test_auto_renames_are_not_offered(up, kind):
    called = []

    async def hook(*args):
        called.append(args)

    job = manual_job(kind=kind or "auto")
    if kind is None:
        job.fmt = None  # an auto rename of someone who has no auto format
    await deliver(up, job, hook)
    assert called == [] and job.state == "done"


async def test_a_failing_offer_does_not_spoil_the_rename(up):
    async def hook(*args):
        raise RuntimeError("telegram said no")

    job = manual_job()
    log = await deliver(up, job, hook)
    assert job.state == "done" and log == ["sent", "finished"]


async def test_without_the_hook_nothing_happens(up):
    job = manual_job()
    assert await deliver(up, job, None) == ["sent", "finished"] and job.state == "done"


async def test_a_file_sent_to_the_target_channel_is_offered_with_that_channel_as_its_chat(up):
    seen = []

    async def hook(job, target, sent):
        seen.append(target)

    await deliver(up, manual_job(), hook, target_channel=SHOW)
    assert seen == [SHOW]


# ---------------------------------------------------------------------------
# Yes: upload, tell the communication chat, read the answers
# ---------------------------------------------------------------------------


async def test_yes_copies_the_episode_and_tells_the_communication_chat(up):
    cb = await start_upload(up, caption="<b>x</b>")
    assert cb.edits[-1][0].startswith("⏳ <b>Processing…</b>") and cb.edits[-1][1] is None and cb.answers
    nid, text, kw = await the_notice(up)
    # copied inside Telegram: nothing was downloaded, nothing was uploaded again
    assert up.client.forwarded == [(DB, 555, OWNER, {"drop_author": True})]
    assert up.client.downloads == [] and up.client.files == []
    ref = ref_of(text)
    assert text.splitlines() == [
        "File uploaded to the Database Channel", "Episode: 05", f"Channel: {SHOW}", f"File: {NAME}", "Post: 4001", f"Ref: {ref}"
    ]
    assert kw["parse_mode"] is None  # plain text: nothing in a file name can be taken for formatting


async def test_the_whole_story_from_yes_to_published(up):
    await start_upload(up)
    nid, text, _ = await the_notice(up)
    ref = ref_of(text)
    await post_bot_says(up, f"📥 Message received - checking the Database Channel…\nRef: {ref}", reply_to=nid)
    await wait_for(lambda: len(up.client.edited) >= 2)
    assert "channel post bot got it" in last_edit(up) and "Waiting" not in last_edit(up)
    done = (
        "✅ Post published successfully\nChannel: Black Clover English\nPost: https://t.me/c/1/2\n"
        f"Button: Download Episode 05 → https://t.me/x\nEpisode: 05\nFile: {NAME}\nRef: {ref}"
    )
    await post_bot_says(up, done, reply_to=nid)
    await up.flow.idle()
    texts = [t for _e, t, _k in up.client.edited]
    assert len(texts) == 3
    assert "Waiting for the channel post bot" in texts[0] and "Uploaded to the Database Channel" in texts[0]
    assert "Post published successfully" in texts[2] and "Black Clover English" in texts[2] and "https://t.me/c/1/2" in texts[2]
    assert NAME in texts[2]
    assert {kw["message"] for _e, _t, kw in up.client.edited} == {77} and {e for e, _t, _k in up.client.edited} == {OWNER}
    buttons = [kw["buttons"] for _e, _t, kw in up.client.edited]
    assert buttons[0] is None and buttons[1] is None  # while it works there is nothing to press ...
    assert flat_buttons(buttons[2]) == [("🗑 Delete post", f"dbu:d:4001:{SHOW}:2")]  # ... and the published post can be deleted again
    assert up.flow.flows == {} and up.flow._busy == set()


async def test_a_failure_of_the_post_bot_is_shown_with_its_reason(up):
    await start_upload(up)
    nid, text, _ = await the_notice(up)
    await post_bot_says(up, f"📥 Message received - checking the Database Channel…\nRef: {ref_of(text)}", reply_to=nid)
    reason = "The latest post of the Database Channel is the file “Other.mkv”, not the one that was reported - nothing was published."
    await post_bot_says(up, f"❌ Publication failed: {reason}\nFile: {NAME}\nRef: {ref_of(text)}", reply_to=nid)
    await up.flow.idle()
    final = last_edit(up)
    assert final.startswith("❌ <b>Publication failed</b>") and reason in final and NAME in final


async def test_names_and_reasons_are_escaped_in_the_edits_but_not_in_the_notice(up):
    await start_upload(up, name="[S01E05] Tom & Jerry <HD>.mkv")
    nid, text, _ = await the_notice(up)
    assert "File: [S01E05] Tom & Jerry <HD>.mkv" in text
    await post_bot_says(up, "❌ Publication failed: bad <b>thing</b> & more\nRef: " + ref_of(text), reply_to=nid)
    await up.flow.idle()
    final = last_edit(up)
    assert "Tom &amp; Jerry &lt;HD&gt;" in final and "bad &lt;b&gt;thing&lt;/b&gt; &amp; more" in final


async def test_no_answer_at_all_means_the_channel_post_bot_is_down(up):
    up.flow.ack_wait = 0.1
    await start_upload(up)
    await up.flow.idle()
    final = last_edit(up)
    assert "down or not responding" in final and "The episode is in the Database Channel" in final and "nothing was published" in final
    assert up.flow.flows == {} and up.flow._busy == set()


async def test_an_acknowledged_notice_that_is_never_answered_is_not_called_down(up):
    up.flow.ack_wait, up.flow.result_wait = 2.0, 0.15
    await start_upload(up)
    nid, text, _ = await the_notice(up)
    await post_bot_says(up, f"📥 Message received - checking the Database Channel…\nRef: {ref_of(text)}", reply_to=nid)
    await up.flow.idle()
    final = last_edit(up)
    assert "No word from the channel post bot" in final and "down" not in final


async def test_an_answer_without_the_acknowledgement_still_counts(up):
    await start_upload(up)
    nid, text, _ = await the_notice(up)
    await post_bot_says(up, f"✅ Post published successfully\nRef: {ref_of(text)}", reply_to=nid)
    await up.flow.idle()
    assert "Post published successfully" in last_edit(up)


async def test_answers_are_matched_by_the_ref_alone_or_by_the_reply_alone(up):
    await start_upload(up, offer_id=71, mid=601)
    nid, text, _ = await the_notice(up)
    await post_bot_says(up, f"✅ Post published successfully\nRef: {ref_of(text)}")  # not a reply: the Ref says whose it is
    await up.flow.idle()
    assert "Post published successfully" in last_edit(up)

    await start_upload(up, offer_id=72, mid=602)
    nid, text, _ = await the_notice(up, 2)
    await post_bot_says(up, "❌ Publication failed: nope", reply_to=nid)  # no Ref: the reply says whose it is
    await up.flow.idle()
    assert "Publication failed" in last_edit(up) and "nope" in last_edit(up)


async def test_with_two_uploads_waiting_the_ref_tells_whose_answer_it_is(up):
    up.client.messages[601] = document_message("[S01E05] A [1080p].mkv", 10, mid=601)
    up.client.messages[602] = document_message("[S01E06] A [1080p].mkv", 10, mid=602)
    await click(up, OWNER, f"dbu:y:{OWNER}:601:{SHOW}", msg_id=71)
    await click(up, OWNER, f"dbu:y:{OWNER}:602:{SHOW}", msg_id=72)
    first, second = await the_notice(up, 1), await the_notice(up, 2)
    await post_bot_says(up, f"✅ Post published successfully\nRef: {ref_of(second[1])}")  # not a reply, and two are waiting
    await wait_for(lambda: any(kw["message"] == 72 and "published successfully" in t for _e, t, kw in up.client.edited))
    assert not any(kw["message"] == 71 and "published successfully" in t for _e, t, kw in up.client.edited)
    assert len(up.flow.flows) == 1 and ref_of(first[1]) in up.flow.flows  # the first is still waiting
    await post_bot_says(up, f"✅ Post published successfully\nRef: {ref_of(first[1])}")
    await up.flow.idle()


async def test_a_plain_message_counts_when_only_one_upload_is_waiting(up):
    await start_upload(up)
    await the_notice(up)
    await post_bot_says(up, "✅ Post published successfully")
    await up.flow.idle()
    assert "Post published successfully" in last_edit(up)


async def test_other_senders_other_chats_other_replies_and_other_words_are_ignored(up):
    await start_upload(up)
    nid, text, _ = await the_notice(up)
    ref = ref_of(text)
    ok = f"✅ Post published successfully\nRef: {ref}"
    await post_bot_says(up, ok, reply_to=nid, sender=STRANGER)  # somebody else
    await post_bot_says(up, ok, reply_to=nid, chat=-1005000000005)  # another chat
    await post_bot_says(up, f"hello there\nRef: {ref}", reply_to=nid)  # not an answer
    await post_bot_says(up, "✅ Post published successfully", reply_to=1)  # a reply to something else
    await asyncio.sleep(0.05)
    assert up.flow.flows[ref].inbox.empty() and len(up.client.edited) == 1
    await post_bot_says(up, ok, reply_to=nid)
    await up.flow.idle()
    assert "Post published successfully" in last_edit(up)


async def test_two_uploads_at_once_each_get_their_own_answer(up):
    up.client.messages[601] = document_message("[S01E05] A [1080p].mkv", 10, mid=601)
    up.client.messages[602] = document_message("[S01E06] A [1080p].mkv", 10, mid=602)
    await click(up, OWNER, f"dbu:y:{OWNER}:601:{SHOW}", msg_id=71)
    await click(up, OWNER, f"dbu:y:{OWNER}:602:{SHOW}", msg_id=72)
    first, second = await the_notice(up, 1), await the_notice(up, 2)
    assert "Episode: 05" in first[1] and "Episode: 06" in second[1] and ref_of(first[1]) != ref_of(second[1])
    await post_bot_says(up, f"❌ Publication failed: nope\nRef: {ref_of(second[1])}", reply_to=second[0])  # out of order
    await post_bot_says(up, f"✅ Post published successfully\nRef: {ref_of(first[1])}", reply_to=first[0])
    await up.flow.idle()
    last = {kw["message"]: text for _e, text, kw in up.client.edited}
    assert "Post published successfully" in last[71] and "Publication failed" in last[72]


async def test_a_format_without_a_channel_is_only_uploaded(up):
    await start_upload(up, channel=None)
    await up.flow.idle()
    assert len(up.client.forwarded) == 1 and not notices(up)
    final = last_edit(up)
    assert final.startswith("✅ <b>Uploaded to the Database Channel</b>") and "has no Channel" in final and NAME in final
    assert up.flow.flows == {}


async def test_when_forwarding_is_refused_the_file_is_sent_again_by_its_id(up):
    up.client.forward_errors.append(errors.RPCError("CHAT_FORWARDS_RESTRICTED", 400))
    await start_upload(up, caption="Some caption")
    nid, text, _ = await the_notice(up)
    assert up.client.forwarded == []
    (entity, file, kw), = up.client.files
    assert entity == DB and file == up.client.messages[555].file.id  # the file itself is not sent again, only its id
    assert kw["caption"] == "Some caption" and kw["formatting_entities"] is None and kw["parse_mode"] is None
    assert "Post: 3001" in text and up.client.downloads == []


async def test_a_failed_upload_can_be_tried_again(up):
    up.client.forward_errors.append(RuntimeError("no rights"))
    up.client.fail_send_file = True
    await start_upload(up)
    await up.flow.idle()
    chat, text, kw = up.client.edited[-1]
    assert "The upload to the Database Channel failed" in text and "RuntimeError: cannot send file" in text and "admin" in text
    assert flat_buttons(kw["buttons"]) == [("🔁 Try again", f"dbu:y:{OWNER}:555:{SHOW}"), ("✖ Close", "x:close")]
    assert not notices(up) and up.flow._busy == set()  # nothing was announced
    up.client.fail_send_file = False
    await click(up, OWNER, f"dbu:y:{OWNER}:555:{SHOW}", msg_id=77)
    await the_notice(up)
    assert len(up.client.forwarded) == 1


async def test_an_episode_that_was_deleted_is_reported(up):
    await click(up, OWNER, f"dbu:y:{OWNER}:999:{SHOW}", msg_id=77)
    await up.flow.idle()
    assert "not there anymore" in last_edit(up) and not up.client.forwarded and not notices(up)


async def test_a_second_tap_while_it_is_working_does_nothing(up):
    await start_upload(up)
    again = await click(up, OWNER, f"dbu:y:{OWNER}:555:{SHOW}", msg_id=77)
    assert again.toasts == ["I'm already on it…"] and again.edits == []
    await the_notice(up)
    await asyncio.sleep(0.05)
    assert len(up.client.forwarded) == 1 and len(notices(up)) == 1


async def test_no_does_not_upload(up):
    cb = await click(up, OWNER, "dbu:n", msg_id=77)
    assert "not uploaded" in cb.edits[-1][0] and cb.edits[-1][1] is None and cb.answers
    assert not up.client.forwarded and not up.client.sent


async def test_a_notice_that_cannot_be_sent_is_reported(up):
    up.client.message_errors[COMM] = RuntimeError("chat not found")
    await start_upload(up)
    await up.flow.idle()
    final = last_edit(up)
    assert "could not write in the communication chat" in final and "chat not found" in final and "Uploaded" in final
    assert len(up.client.forwarded) == 1 and up.flow.flows == {} and up.flow._busy == set()


async def test_an_unexpected_error_is_shown_instead_of_leaving_processing_on_the_screen(up):
    async def broken(*args, **kw):
        raise RuntimeError("boom")

    up.client.get_messages = broken
    await start_upload(up)
    await up.flow.idle()
    assert "Something went wrong" in last_edit(up) and "RuntimeError: boom" in last_edit(up) and up.flow._busy == set()


async def test_a_message_that_was_deleted_meanwhile_does_not_break_the_flow(up):
    async def gone(*args, **kw):
        raise errors.RPCError("Message to edit not found", 400)

    up.client.edit_message = gone
    await start_upload(up)
    nid, text, _ = await the_notice(up)
    await post_bot_says(up, f"✅ Post published successfully\nRef: {ref_of(text)}", reply_to=nid)
    await up.flow.idle()
    assert up.flow.flows == {} and up.flow._busy == set()


async def test_shutdown_stops_the_waiting(up):
    await start_upload(up)
    await the_notice(up)
    await up.app.shutdown()
    assert up.flow._tasks == set() and up.flow.flows == {} and up.flow._busy == set()


async def test_strangers_cannot_press_the_buttons(up):
    up.client.messages[555] = document_message(NAME, 10, mid=555)
    await click(up, STRANGER, f"dbu:y:{STRANGER}:555:{SHOW}")
    assert not up.client.forwarded and not up.flow._tasks


@pytest.mark.parametrize("data", ["dbu", "dbu:y", "dbu:y:1:2", "dbu:y:a:b:c", "dbu:z", "dbu:y:1:2:3:4"])
async def test_a_broken_button_does_nothing(up, data):
    cb = await click(up, OWNER, data)
    assert cb.answers and not up.client.forwarded and up.flow._busy == set() and not up.flow._tasks


# ---------------------------------------------------------------------------
# 🗑 Delete post: the button, the question, the channel post bot, the copy in the Database Channel
# ---------------------------------------------------------------------------

DELETE = f"dbu:d:4001:{SHOW}:57"  # the button of the published post (copy 4001 in the Database Channel, post 57 in the channel)
YES = f"dbu:dy:4001:{SHOW}:57:77"  # ... and the Yes of the question about it (77: the message of the published post)


async def published(up, *, extra=""):
    """The whole upload, answered with "Post published successfully": its message (77) now has the Delete button."""
    await start_upload(up)
    nid, text, _ = await the_notice(up)
    ref = ref_of(text)
    await post_bot_says(up, f"📥 Message received - checking the Database Channel…\nRef: {ref}", reply_to=nid)
    done = f"✅ Post published successfully\nChannel: Black Clover English\nPost: https://t.me/c/1/57\nMessage: 57\n{extra}File: {NAME}\nRef: {ref}"
    await post_bot_says(up, done, reply_to=nid)
    await up.flow.idle()


def to_owner(up):
    return [(2000 + n, text, kw) for n, (entity, text, kw) in enumerate(up.client.sent, 1) if entity == OWNER]


async def ask(up):
    """Press Delete post, then Yes; the id of the question's message."""
    await click(up, OWNER, DELETE, msg_id=77)
    qid = to_owner(up)[-1][0]
    await click(up, OWNER, YES, msg_id=qid)
    return qid


async def post_bot_deletes(up, nth, *answer):
    """What the channel post bot says to the nth notice: each of ``answer`` (with REF in it) as a reply."""
    nid, text, _ = await the_notice(up, nth)
    ref = ref_of(text)
    for line in answer:
        await post_bot_says(up, line.replace("REF", ref), reply_to=nid)
    return ref


OK = "🗑 Post deleted successfully\nChannel: Black Clover English\nMessage: 57\nRef: REF"
GOT = "📥 Message received - deleting the post…\nRef: REF"


async def test_the_published_post_gets_a_delete_button_only_when_its_id_is_known(up):
    await published(up)
    assert flat_buttons(up.client.edited[-1][2]["buttons"]) == [("🗑 Delete post", DELETE)]
    assert up.client.edited[0][2]["buttons"] is None and up.client.edited[1][2]["buttons"] is None


async def test_without_a_post_id_there_is_no_button(up):
    await start_upload(up)
    nid, text, _ = await the_notice(up)
    await post_bot_says(up, f"✅ Post published successfully\nChannel: X\nRef: {ref_of(text)}", reply_to=nid)
    await up.flow.idle()
    assert up.client.edited[-1][2]["buttons"] is None


@pytest.mark.parametrize(
    "reply, expected",
    [
        ("Post: https://t.me/x/58\nMessage: 57", 57),
        ("Post: https://t.me/blackclover/58", 58),
        ("Post: https://t.me/c/123/59", 59),
        ("Message: abc\nPost: https://t.me/c/123/60", 60),
        ("Channel: X", None),
        ("Post: https://t.me/c/123/5?single", None),
    ],
)
def test_the_post_id_comes_from_the_message_line_or_the_link(reply, expected):
    assert post_message_id(reply) == expected


def test_the_delete_notice_and_the_button_limit():
    assert delete_notice_text(channel=SHOW, message=57, ref="ab12").splitlines() == [
        "Delete the published post", f"Channel: {SHOW}", "Message: 57", "Ref: ab12"
    ]
    assert delete_button(10**40, SHOW, 57) is None


@pytest.mark.parametrize(
    "text, expected",
    [
        ("🗑 Post deleted successfully\nRef: x", ("ok", "")),
        ("❌ Deletion failed: no right\nRef: x", ("fail", "no right")),
        ("📥 Message received - deleting the post…", ("ack", "")),
        ("❌ Publication failed: nope", ("fail", "nope")),
    ],
)
def test_the_deletion_answers_are_understood(text, expected):
    assert classify(text) == expected


async def test_pressing_the_button_only_asks(up):
    await published(up)
    cb = await click(up, OWNER, DELETE, msg_id=77)
    (qid, text, kw), = to_owner(up)[-1:]
    assert cb.answers and "Are you sure you want to delete this post?" in text and str(SHOW) in text
    assert kw["reply_to"] == 77 and kw["parse_mode"] == "html"
    assert flat_buttons(kw["buttons"]) == [("✅ Yes, delete it", YES), ("❌ No", "dbu:dn")]
    assert all(len(d.encode()) <= 64 for _l, d in flat_buttons(kw["buttons"]))
    assert up.client.deleted == [] and len(notices(up)) == 1 and len(up.client.edited) == 3  # nothing happened yet


async def test_no_closes_the_question(up):
    await published(up)
    cb = await click(up, OWNER, "dbu:dn", msg_id=500)
    assert cb.deleted and cb.toasts == ["Okay, nothing is deleted."] and up.client.deleted == [] and len(notices(up)) == 1
    stuck = FakeCallback(OWNER, "dbu:dn", msg_id=500)
    stuck.delete_error = RuntimeError("too old")
    await dispatch(up.client, stuck)
    assert stuck.edits[-1][0] == "Closed."


async def test_yes_deletes_the_copy_only_after_the_channel_post_bot_says_the_post_is_gone(up):
    await published(up)
    await click(up, OWNER, DELETE, msg_id=77)
    qid = to_owner(up)[-1][0]
    cb = await click(up, OWNER, YES, msg_id=qid)
    assert cb.edits[-1][0].startswith("⏳ <b>Deleting the post…</b>") and cb.edits[-1][1] is None
    nid, text, kw = await the_notice(up, 2)
    ref = ref_of(text)
    assert text.splitlines() == ["Delete the published post", f"Channel: {SHOW}", "Message: 57", f"Ref: {ref}"] and kw["parse_mode"] is None
    await post_bot_says(up, GOT.replace("REF", ref), reply_to=nid)
    await wait_for(lambda: "got it" in str(up.client.edited[-1][1]))
    assert up.client.deleted == []  # not before the channel post bot has said so
    await post_bot_says(up, OK.replace("REF", ref) + "\nℹ️ The post was already gone from the channel and from My posts.", reply_to=nid)
    await up.flow.idle()
    assert up.client.deleted == [(DB, [4001])]
    chat, final, kw = up.client.edited[-1]
    assert kw["message"] == qid and kw["buttons"] is None
    assert "Post deleted" in final and "Black Clover English" in final and "already gone" in final and "Deleted from the Database Channel" in final
    mark = up.client.edited[-2]
    assert mark[1] is None and mark[2]["message"] == 77 and mark[2]["buttons"] is None  # the post's message loses its button, its text is not touched
    assert up.flow.flows == {} and up.flow._busy == set()


async def test_the_note_an_older_version_left_on_a_deleted_post_removes_itself_when_pressed(up):
    left = await click(up, OWNER, "dbu:x", msg_id=77)
    assert left.toasts == ["This post is already deleted."]
    (chat, text, kw), = up.client.edited
    assert chat == OWNER and text is None and kw["message"] == 77 and kw["buttons"] is None


async def test_a_failure_leaves_everything_and_can_be_tried_again(up):
    await published(up)
    qid = await ask(up)
    await post_bot_deletes(up, 2, GOT, "❌ Deletion failed: Telegram did not delete it & more\nRef: REF")
    await up.flow.idle()
    final = up.client.edited[-1]
    assert "The post was not deleted" in final[1] and "did not delete it &amp; more" in final[1] and "was not touched" in final[1]
    assert flat_buttons(final[2]["buttons"]) == [("🔁 Try again", YES), ("✖ Close", "dbu:dn")]
    assert up.client.deleted == [] and up.flow._busy == set()
    await click(up, OWNER, YES, msg_id=qid)
    ref2 = await post_bot_deletes(up, 3, GOT, OK)
    await up.flow.idle()
    assert ref2 != ref_of(notices(up)[1][1]) and up.client.deleted == [(DB, [4001])]


async def test_a_channel_post_bot_that_does_not_answer_is_reported(up):
    await published(up)
    up.flow.ack_wait = 0.1
    await ask(up)
    await up.flow.idle()
    final = up.client.edited[-1]
    assert "down or not responding" in final[1] and "post is not deleted" in final[1] and up.client.deleted == []
    assert flat_buttons(final[2]["buttons"])[0][0] == "🔁 Try again"


async def test_an_acknowledged_deletion_that_is_never_answered_is_not_called_down(up):
    await published(up)
    up.flow.delete_wait = 0.15
    await ask(up)
    await post_bot_deletes(up, 2, GOT)
    await up.flow.idle()
    final = up.client.edited[-1][1]
    assert "No word from the channel post bot" in final and "down" not in final and up.client.deleted == []


async def test_a_copy_that_cannot_be_deleted_is_reported_and_retried(up):
    await published(up)
    up.client.delete_errors.append(RuntimeError("no right"))
    qid = await ask(up)
    await post_bot_deletes(up, 2, GOT, OK)
    await up.flow.idle()
    chat, final, kw = up.client.edited[-1]
    assert "deleted, but not its file in the Database Channel" in final and "RuntimeError: no right" in final
    assert flat_buttons(kw["buttons"])[0] == ("🔁 Try again", YES) and up.client.deleted == []
    assert [e for e in up.client.edited if e[1] is None] == []  # the post's own button is left alone until everything is done
    await click(up, OWNER, YES, msg_id=qid)
    await post_bot_deletes(up, 3, GOT, OK)
    await up.flow.idle()
    assert up.client.deleted == [(DB, [4001])] and "Post deleted" in up.client.edited[-1][1]


async def test_a_second_tap_on_yes_does_nothing_while_it_works(up):
    await published(up)
    await click(up, OWNER, DELETE, msg_id=77)
    qid = to_owner(up)[-1][0]
    await click(up, OWNER, YES, msg_id=qid)
    again = await click(up, OWNER, YES, msg_id=qid)
    assert again.toasts == ["I'm already on it…"] and again.edits == []
    await the_notice(up, 2)
    await asyncio.sleep(0.05)
    assert len(notices(up)) == 2


async def test_a_notice_that_cannot_be_written_is_reported(up):
    await published(up)
    up.client.message_errors[COMM] = RuntimeError("chat not found")
    await ask(up)
    await up.flow.idle()
    final = up.client.edited[-1][1]
    assert "could not write in the communication chat" in final and "chat not found" in final and "Nothing was deleted" in final
    assert up.client.deleted == [] and up.flow.flows == {} and up.flow._busy == set()


async def test_an_unexpected_error_is_shown(up):
    await published(up)

    async def broken(*args, **kw):
        raise RuntimeError("boom")

    up.flow._answers = broken
    await ask(up)
    await up.flow.idle()
    assert "Something went wrong" in up.client.edited[-1][1] and "RuntimeError: boom" in up.client.edited[-1][1]
    assert up.flow._busy == set() and up.flow.flows == {} and up.client.deleted == []


async def test_deleting_needs_nothing_but_the_buttons_so_it_survives_a_restart(up):
    await click(up, OWNER, DELETE, msg_id=77)  # no upload in this run of the bot
    qid = to_owner(up)[-1][0]
    await click(up, OWNER, YES, msg_id=qid)
    await post_bot_deletes(up, 1, GOT, OK)
    await up.flow.idle()
    assert up.client.deleted == [(DB, [4001])]


async def test_a_post_without_a_known_copy_is_deleted_without_touching_the_database_channel(up):
    await click(up, OWNER, f"dbu:d:0:{SHOW}:57", msg_id=77)
    await click(up, OWNER, f"dbu:dy:0:{SHOW}:57:77", msg_id=to_owner(up)[-1][0])
    await post_bot_deletes(up, 1, OK)
    await up.flow.idle()
    assert up.client.deleted == [] and "no copy in the Database Channel" in up.client.edited[-1][1]


async def test_the_question_message_deleted_meanwhile_does_not_stop_the_deletion(up):
    await published(up)
    await ask(up)

    async def gone(*args, **kw):
        raise errors.RPCError("Message to edit not found", 400)

    up.client.edit_message = gone
    await post_bot_deletes(up, 2, GOT, OK)
    await up.flow.idle()
    assert up.client.deleted == [(DB, [4001])] and up.flow._busy == set()


async def test_shutdown_stops_a_deletion_that_is_waiting(up):
    await published(up)
    await ask(up)
    await the_notice(up, 2)
    await up.app.shutdown()
    assert up.flow._tasks == set() and up.flow.flows == {} and up.flow._busy == set()


async def test_strangers_cannot_press_the_delete_buttons(up):
    await click(up, STRANGER, DELETE)
    await click(up, STRANGER, f"dbu:dy:4001:{SHOW}:57:77")
    assert not [t for _e, t, _k in up.client.sent if "Are you sure" in t] and not notices(up) and not up.flow._tasks


@pytest.mark.parametrize(
    "data",
    [
        "dbu:d", "dbu:d:1:2", "dbu:d:a:b:c", f"dbu:d:-1:{SHOW}:5", f"dbu:d:1:{SHOW}:0", "dbu:d:1:0:5",
        f"dbu:dy:1:{SHOW}:5", f"dbu:d:1:{SHOW}:5:9", f"dbu:dy:1:{SHOW}:5:x",
    ],
)
async def test_a_broken_delete_button_does_nothing(up, data):
    cb = await click(up, OWNER, data)
    assert cb.answers and not up.client.sent and not up.client.deleted and up.flow._busy == set() and not up.flow._tasks


async def test_without_the_chats_the_delete_button_says_it_is_switched_off(ctx):
    cb = await click(ctx, OWNER, DELETE)
    assert cb.alerts and "switched off" in cb.alerts[0] and not ctx.client.sent


# ---------------------------------------------------------------------------
# No -> "upload it and get the Share URL?" (nothing is published)
# ---------------------------------------------------------------------------

FILE_LINK = "https://t.me/filestore_bot?start=Zm9v"
SHEET = "https://t.me/share/url?url=https%3A%2F%2Ft.me%2Ffilestore_bot%3Fstart%3DZm9v"
ASK = f"dbu:n:{OWNER}:555"
LINK_YES = f"dbu:l:{OWNER}:555"


def with_button(link=SHEET, text="🔁 Share URL"):
    row = types.KeyboardInlineButtonRow([types.KeyboardInlineButton(text, types.InlineButtonTypeUrl(link))])
    return SimpleNamespace(id=4001, reply_markup=types.ReplyInlineMarkup([row]))


def test_the_share_button_is_found_and_the_share_sheet_taken_off():
    assert share_link(with_button()) == FILE_LINK and share_link(with_button(FILE_LINK)) == FILE_LINK
    assert share_link(with_button(text="Other")) == "" and share_link(SimpleNamespace(reply_markup=None)) == "" and share_link(None) == ""
    assert share_link(with_button("https://t.me/share/url?text=hi")) == ""


async def test_no_asks_about_the_link_and_uploads_nothing(up):
    cb = await click(up, OWNER, ASK, msg_id=77)
    text, buttons = cb.edits[-1]
    assert "Share URL" in text and "Nothing is published" in text
    assert flat_buttons(buttons) == [("✅ Yes", LINK_YES), ("❌ No", "dbu:z")]
    assert not up.client.forwarded and not notices(up)
    cb = await click(up, OWNER, "dbu:z", msg_id=77)
    assert "not uploaded" in cb.edits[-1][0] and not up.client.forwarded


async def test_yes_uploads_and_shows_the_link_in_monospace(up):
    up.client.messages[555] = document_message(NAME, 10, mid=555)
    up.client.messages[4001] = with_button()
    up.flow.link_poll = 0.01
    await click(up, OWNER, LINK_YES, msg_id=77)
    await up.flow.idle()
    assert up.client.forwarded == [(DB, 555, OWNER, {"drop_author": True})] and not notices(up)  # nothing for the channel post bot
    final = last_edit(up)
    assert "Uploaded to the Database Channel" in final and f"<code>{FILE_LINK}</code>" in final and up.flow._busy == set()


async def test_the_button_that_shows_up_later_is_waited_for(up):
    up.client.messages[555] = document_message(NAME, 10, mid=555)
    up.flow.link_poll, up.flow.link_wait = 0.01, 3.0
    await click(up, OWNER, LINK_YES, msg_id=77)
    await asyncio.sleep(0.1)
    up.client.messages[4001] = with_button()
    await up.flow.idle()
    assert f"<code>{FILE_LINK}</code>" in last_edit(up) and not notices(up)


async def test_when_this_bot_cannot_see_it_the_channel_post_bot_is_asked(up):
    up.client.messages[555] = document_message(NAME, 10, mid=555)
    up.flow.link_poll, up.flow.link_wait = 0.01, 0.05
    await click(up, OWNER, LINK_YES, msg_id=77)
    nid, text, kw = await the_notice(up)
    ref = ref_of(text)
    assert text.splitlines() == ["Get the share URL", "Post: 4001", f"Ref: {ref}"] and kw["parse_mode"] is None
    await post_bot_says(up, f"📥 Message received - looking for the Share URL…\nRef: {ref}", reply_to=nid)
    await post_bot_says(up, f"🔗 Share URL ready\nURL: {FILE_LINK}\nRef: {ref}", reply_to=nid)
    await up.flow.idle()
    assert f"<code>{FILE_LINK}</code>" in last_edit(up) and up.flow.flows == {} and up.flow._busy == set()


async def test_a_failure_of_the_channel_post_bot_offers_to_look_again(up):
    up.client.messages[555] = document_message(NAME, 10, mid=555)
    up.flow.link_poll, up.flow.link_wait = 0.01, 0.05
    await click(up, OWNER, LINK_YES, msg_id=77)
    nid, text, _ = await the_notice(up)
    await post_bot_says(up, f"❌ Share URL failed: No button after 45 seconds\nRef: {ref_of(text)}", reply_to=nid)
    await up.flow.idle()
    chat, final, kw = up.client.edited[-1]
    assert "no Share URL" in final and "No button after 45 seconds" in final
    assert flat_buttons(kw["buttons"]) == [("🔁 Look for the URL again", "dbu:g:4001"), ("✖ Close", "x:close")]
    assert len(up.client.forwarded) == 1  # looking again does not upload again
    up.client.messages[4001] = with_button()
    await click(up, OWNER, "dbu:g:4001", msg_id=77)
    await up.flow.idle()
    assert f"<code>{FILE_LINK}</code>" in last_edit(up) and len(up.client.forwarded) == 1


async def test_a_channel_post_bot_that_is_down_is_reported(up):
    up.client.messages[555] = document_message(NAME, 10, mid=555)
    up.flow.link_poll, up.flow.link_wait, up.flow.ack_wait = 0.01, 0.03, 0.1
    await click(up, OWNER, LINK_YES, msg_id=77)
    await up.flow.idle()
    assert "no Share URL" in last_edit(up) and "did not answer" in last_edit(up)


async def test_an_episode_that_was_deleted_or_cannot_be_uploaded_is_reported(up):
    await click(up, OWNER, f"dbu:l:{OWNER}:999", msg_id=77)
    await up.flow.idle()
    assert "not there anymore" in last_edit(up)
    up.client.messages[555] = document_message(NAME, 10, mid=555)
    up.client.forward_errors.append(RuntimeError("no rights"))
    up.client.fail_send_file = True
    await click(up, OWNER, LINK_YES, msg_id=77)
    await up.flow.idle()
    chat, text, kw = up.client.edited[-1]
    assert "upload to the Database Channel failed" in text and flat_buttons(kw["buttons"])[0] == ("🔁 Try again", LINK_YES)
    assert up.flow._busy == set()


async def test_a_second_tap_does_nothing_and_broken_buttons_are_ignored(up):
    up.client.messages[555] = document_message(NAME, 10, mid=555)
    up.flow.link_poll, up.flow.link_wait = 0.01, 1.0
    await click(up, OWNER, LINK_YES, msg_id=77)
    again = await click(up, OWNER, LINK_YES, msg_id=77)
    assert again.toasts == ["I'm already on it…"]
    up.client.messages[4001] = with_button()
    await up.flow.idle()
    assert len(up.client.forwarded) == 1
    for data in ("dbu:l", "dbu:l:1", "dbu:l:a:b", f"dbu:l:{OWNER}:0", "dbu:g", "dbu:g:x", "dbu:n:1"):
        cb = await click(up, OWNER, data)
        assert cb.answers and up.flow._busy == set() and len(up.client.forwarded) == 1


def test_the_new_answers_are_understood():
    assert classify("🔗 Share URL ready\nURL: x") == ("ok", "") and classify("❌ Share URL failed: why") == ("fail", "why")
    assert link_notice_text(post_id=5, ref="r").splitlines() == ["Get the share URL", "Post: 5", "Ref: r"]
