"""/formats (the manager of auto and manual formats) and /rename (pick a manual format, send one file)."""
import asyncio
import time

import pytest

from bot import app as appmod
from bot import panels
from bot.db import MAX_FORMATS
from bot.formats import KIND_LABELS
from bot.parser import DEFAULT_TEMPLATE

from .appkit import ADMIN_CFG, EPISODE, OWNER, autofmt, click, data_of, jpeg, say, send_doc
from .tgfakes import FakeEvent, FakeMessage, dispatch, document_message, flat_buttons

ONE_PIECE = "1147 - One Piece [Dual] 720p @Anime_X_Universe.mkv"
KINDS = ["auto", "manual"]


async def make_format(ctx, name="One Piece", uid=OWNER, kind="manual", **changes):
    fmt = await ctx.db.create_format(uid, name, template="One Piece - E{episode}", kind=kind)
    if changes:
        fmt = await ctx.db.update_format(uid, fmt["id"], **changes)
    return fmt


def labels_of(buttons):
    return [label for label, _data in flat_buttons(buttons)]


# ---------------------------------------------------------------------------
# the manager: /formats shows the two kinds, each kind has its own list
# ---------------------------------------------------------------------------


async def test_formats_command_offers_the_two_kinds_with_their_counts(ctx):
    for command in ("/formats", "/format", "/FORMAT@RenameBot"):
        ev = await say(ctx, OWNER, command)
        text, buttons, _kw = ev.out[0]
        assert "FORMATS" in text and "Auto rename" in text and "Manual rename" in text, command
        assert flat_buttons(buttons)[:2] == [("🤖 Auto rename (0/12)", "fm:l:auto"), ("✋ Manual rename (0/12)", "fm:l:manual")], command
    await make_format(ctx, "Auto One", kind="auto")
    await make_format(ctx, "One Piece")
    await make_format(ctx, "Dragon Ball")
    ev = await say(ctx, OWNER, "/format")
    assert flat_buttons(ev.out[0][1])[:2] == [("🤖 Auto rename (1/12)", "fm:l:auto"), ("✋ Manual rename (2/12)", "fm:l:manual")]


async def test_each_kind_has_its_own_list_and_its_own_new_button(ctx):
    cb = await click(ctx, OWNER, "fm:l:manual")
    text, buttons = cb.edits[-1]
    assert "MANUAL FORMATS" in text and "(0/12)" in text and "no manual formats yet" in text and cb.answers
    assert "fm:new:manual" in data_of(buttons) and "fm:new:auto" not in data_of(buttons)
    cb = await click(ctx, OWNER, "fm:l:auto")
    text, buttons = cb.edits[-1]
    assert "AUTO FORMATS" in text and "no auto formats yet" in text and "fm:new:auto" in data_of(buttons)

    auto = await make_format(ctx, "Auto One", kind="auto")
    a, b = await make_format(ctx, "One Piece"), await make_format(ctx, "Dragon Ball")
    text, buttons = (await click(ctx, OWNER, "fm:l:manual")).edits[-1]
    data = data_of(buttons)
    assert "(2/12)" in text and f"fm:v:{a['id']}" in data and f"fm:v:{b['id']}" in data and f"fm:v:{auto['id']}" not in data
    assert "One Piece" in labels_of(buttons) and "Dragon Ball" in labels_of(buttons) and "Auto One" not in labels_of(buttons)
    assert "fm:l:manual" not in data and "m:formats" in data  # the way back is the chooser

    text, buttons = (await click(ctx, OWNER, "fm:l:auto")).edits[-1]
    data = data_of(buttons)
    assert "(1/12)" in text and f"fm:v:{auto['id']}" in data and f"fm:v:{a['id']}" not in data
    assert "Auto One ✅" in labels_of(buttons)  # the one in use is ticked


@pytest.mark.parametrize("data", ["fm:l", "fm:l:nope", "fm:new", "fm:new:nope"])
async def test_a_button_without_a_known_kind_opens_the_choice(ctx, data):
    """``fm:new`` is what the button of a message from before the two kinds sends: nothing is created, the user chooses a kind."""
    cb = await click(ctx, OWNER, data)
    text, buttons = cb.edits[-1]
    assert "FORMATS" in text and data_of(buttons)[:2] == ["fm:l:auto", "fm:l:manual"] and cb.answers == [(None, False)]
    assert OWNER not in ctx.app.pending and await ctx.db.count_formats(OWNER) == 0


async def test_formats_are_reachable_from_the_home_menu(ctx):
    ev = await say(ctx, OWNER, "/start")
    assert "m:formats" in data_of(ev.out[0][1])
    cb = await click(ctx, OWNER, "m:formats")
    assert "FORMATS" in cb.edits[-1][0] and cb.answers and "fm:l:auto" in data_of(cb.edits[-1][1])


@pytest.mark.parametrize("kind", KINDS)
async def test_creating_a_format_asks_for_a_name_and_opens_it(ctx, kind):
    cb = await click(ctx, OWNER, f"fm:new:{kind}", msg_id=500)
    p = ctx.app.pending[OWNER]
    assert p.kind == "fmt:new" and p.fid == 0 and p.msg_id == 500 and p.fmt_kind == kind
    assert cb.edits[-1][0] == panels.FORMAT_PROMPTS["new"] and data_of(cb.edits[-1][1]) == [f"fm:l:{kind}"]

    ev = await say(ctx, OWNER, "  One   Piece ", mid=60)
    assert ev.deleted and OWNER not in ctx.app.pending
    (fmt,) = await ctx.db.list_formats(OWNER)
    assert fmt["name"] == "One Piece" and fmt["kind"] == kind and fmt["template"] == DEFAULT_TEMPLATE  # it starts from the defaults
    (chat, text, kw), = ctx.client.edited
    assert kw["message"] == 500 and "created" in text and "🗂 One Piece" in text and KIND_LABELS[kind] in text
    assert f"fm:in:{fmt['id']}:template" in data_of(kw["buttons"]) and f"fm:dm:{fmt['id']}" in data_of(kw["buttons"])


async def test_a_bad_or_taken_name_keeps_the_prompt_open(ctx):
    await make_format(ctx, "One Piece")
    await click(ctx, OWNER, "fm:new:manual")
    for text, expected in (("x" * 31, "too long"), ("one piece", "already have a format")):
        ev = await say(ctx, OWNER, text)
        assert expected in ev.texts[0] and "Try again" in ev.texts[0]
        assert OWNER in ctx.app.pending and await ctx.db.count_formats(OWNER) == 1
    cb = await click(ctx, OWNER, "fm:l:manual")  # Cancel
    assert OWNER not in ctx.app.pending and "MANUAL FORMATS" in cb.edits[-1][0]


async def test_a_name_is_taken_only_within_its_own_kind(ctx):
    await make_format(ctx, "One Piece", kind="auto")
    await click(ctx, OWNER, "fm:new:manual")
    await say(ctx, OWNER, "one piece")
    assert OWNER not in ctx.app.pending
    assert [f["kind"] for f in await ctx.db.list_formats(OWNER)] == ["auto", "manual"]
    await click(ctx, OWNER, "fm:new:manual")
    ev = await say(ctx, OWNER, "ONE PIECE")
    assert "already have a format" in ev.texts[0] and OWNER in ctx.app.pending  # but not twice in the same kind


@pytest.mark.parametrize("kind", KINDS)
async def test_the_twelve_format_limit_of_each_kind(ctx, kind):
    other = "manual" if kind == "auto" else "auto"
    for i in range(MAX_FORMATS):
        await make_format(ctx, f"F{i}", kind=kind)
    cb = await click(ctx, OWNER, f"fm:l:{kind}")
    assert f"all 12 {kind} formats" in cb.edits[-1][0] and f"fm:new:{kind}" not in data_of(cb.edits[-1][1])
    cb = await click(ctx, OWNER, f"fm:new:{kind}")
    assert f"already have 12 {kind} formats" in cb.alerts[0] and OWNER not in ctx.app.pending

    # the other kind has its own twelve
    cb = await click(ctx, OWNER, f"fm:new:{other}")
    assert not cb.alerts and ctx.app.pending[OWNER].fmt_kind == other
    await click(ctx, OWNER, f"fm:l:{other}")

    # the limit is also enforced when it is reached while the name prompt is open
    await ctx.db.delete_format(OWNER, (await ctx.db.list_formats(OWNER, kind))[0]["id"])
    await click(ctx, OWNER, f"fm:new:{kind}")
    await make_format(ctx, "Sneaked in", kind=kind)
    ev = await say(ctx, OWNER, "Late one")
    assert f"already have 12 {kind} formats" in ev.texts[0] and OWNER not in ctx.app.pending


async def test_the_format_screen_shows_every_field_and_has_a_button_for_each(ctx):
    fmt = await make_format(ctx, meta={"title": "One Piece", "video": "HD"}, keep_audio=["japanese", "2"])
    cb = await click(ctx, OWNER, f"fm:v:{fmt['id']}")
    text, buttons = cb.edits[-1]
    for label in ("Template", "Caption", "Thumbnail", "Title", "Author", "Artist", "Audio", "Subtitle", "Video", "Keep Audio", "Keep Subtitle"):
        assert label in text, label
    assert "One Piece - E{episode}" in text and "Japanese, 2" in text and panels.THUMB_NONE in text
    assert "<b>Title</b> ▸ <code>One Piece</code>" in text and "<b>Video</b> ▸ <code>HD</code>" in text  # values land on the right line
    assert "<b>Author</b> ▸ —" in text
    assert "✋ Manual" in text.splitlines()[0]
    data = data_of(buttons)
    i = fmt["id"]
    for field in ("name", "template", "caption", "title", "author", "artist", "audio", "subtitle", "video", "keep_audio", "keep_subtitle"):
        assert f"fm:in:{i}:{field}" in data, field
    assert {f"fm:th:{i}", f"fm:del:{i}", f"fm:dm:{i}", f"fm:cp:{i}", "fm:l:manual", "m:home", "x:close"} <= set(data)
    assert f"fm:thd:{i}" not in data  # nothing to delete yet
    assert f"fm:use:{i}" not in data  # only an auto format is used for auto rename
    assert f"fm:cr:{i}" not in data and "<b>Caption</b> ▸ <b>{filename}</b> · default" in text  # nor to reset


async def test_editing_each_field_goes_through_the_same_checks_as_the_settings(ctx):
    fmt = await make_format(ctx)
    i = fmt["id"]
    edits = {
        "template": ("[S{season}E{episode}] {title}", lambda f: f["template"] == "[S{season}E{episode}] {title}"),
        "caption": ("<i>{filename}</i> · {size}", lambda f: f["caption"] == "<i>{filename}</i> · {size}"),
        "title": ("One Piece", lambda f: f["meta"]["title"] == "One Piece"),
        "author": ("@Me", lambda f: f["meta"]["author"] == "@Me"),
        "artist": ("@Me", lambda f: f["meta"]["artist"] == "@Me"),
        "audio": ("{lang} Audio", lambda f: f["meta"]["audio"] == "{lang} Audio"),
        "subtitle": ("{lang}", lambda f: f["meta"]["subtitle"] == "{lang}"),
        "video": ("HD", lambda f: f["meta"]["video"] == "HD"),
        "keep_audio": ("Japanese English", lambda f: f["keep_audio"] == ["japanese", "english"]),
        "keep_subtitle": ("2 1", lambda f: f["keep_subtitle"] == ["2", "1"]),
    }
    for n, (field, (value, check)) in enumerate(edits.items()):
        cb = await click(ctx, OWNER, f"fm:in:{i}:{field}", msg_id=700 + n)
        p = ctx.app.pending[OWNER]
        assert p.kind == f"fmt:{field}" and p.fid == i and p.msg_id == 700 + n
        assert cb.edits[-1][0] == panels.FORMAT_PROMPTS[field] and data_of(cb.edits[-1][1]) == [f"fm:cx:{i}"]
        ev = await say(ctx, OWNER, value)
        assert ev.deleted and OWNER not in ctx.app.pending, field
        assert check(await ctx.db.get_format(OWNER, i)), field
        chat, text, kw = ctx.client.edited[-1]
        assert kw["message"] == 700 + n and "🗂 One Piece" in text, field  # the format screen is back in the same message
    # none of this touched the default metadata or the auto formats
    mine = await ctx.db.get_settings(OWNER)
    assert mine["meta"]["title"] == "" and mine["keep_audio"] == [] and mine["metadata_on"] is False and await autofmt(ctx) is None


async def test_an_auto_format_is_edited_in_the_same_way(ctx):
    fmt = await make_format(ctx, "Auto One", kind="auto")
    i = fmt["id"]
    await click(ctx, OWNER, f"fm:in:{i}:template", msg_id=701)
    await say(ctx, OWNER, "{title} - {2episode}")
    await click(ctx, OWNER, f"fm:in:{i}:title", msg_id=702)
    await say(ctx, OWNER, "One Piece")
    got = await ctx.db.get_format(OWNER, i)
    assert got["template"] == "{title} - {2episode}" and got["meta"]["title"] == "One Piece" and got["kind"] == "auto"
    chat, text, kw = ctx.client.edited[-1]
    assert kw["message"] == 702 and "🗂 Auto One" in text and "🤖 Auto" in text


async def test_invalid_values_keep_the_prompt_open_and_cancel_returns_to_the_format(ctx):
    fmt = await make_format(ctx)
    i = fmt["id"]
    await click(ctx, OWNER, f"fm:in:{i}:keep_audio")
    ev = await say(ctx, OWNER, "bad!word")
    assert "❌" in ev.texts[0] and "Try again" in ev.texts[0] and OWNER in ctx.app.pending
    cb = await click(ctx, OWNER, f"fm:cx:{i}")
    assert OWNER not in ctx.app.pending and "🗂 One Piece" in cb.edits[-1][0]


async def test_clearing_a_field_with_off(ctx):
    fmt = await make_format(ctx, meta={"title": "X"}, keep_audio=["english"])
    await click(ctx, OWNER, f"fm:in:{fmt['id']}:title")
    await say(ctx, OWNER, "off")
    await click(ctx, OWNER, f"fm:in:{fmt['id']}:keep_audio")
    await say(ctx, OWNER, "off")
    got = await ctx.db.get_format(OWNER, fmt["id"])
    assert got["meta"]["title"] == "" and got["keep_audio"] == []


async def test_a_formats_caption_is_its_own_and_can_be_reset(ctx):
    auto = await make_format(ctx, "Auto One", kind="auto", caption="<i>MY OWN {filename}</i>")
    fmt = await make_format(ctx)
    i = fmt["id"]
    cb = await click(ctx, OWNER, f"fm:in:{i}:caption", msg_id=710)
    assert cb.edits[-1][0] == panels.FORMAT_PROMPTS["caption"] and data_of(cb.edits[-1][1]) == [f"fm:cx:{i}"]
    assert "reset" in cb.edits[-1][0]

    ev = await say(ctx, OWNER, "x" * 801)  # the same checks as the Set Caption prompt: too long keeps the prompt open
    assert "too long" in ev.texts[0] and "Try again" in ev.texts[0] and OWNER in ctx.app.pending
    assert (await ctx.db.get_format(OWNER, i))["caption"] == "<b>{filename}</b>"
    ev = await say(ctx, OWNER, "<b>{filename}</b> · {size} · {nonsense}")
    assert ev.deleted and OWNER not in ctx.app.pending
    chat, text, kw = ctx.client.edited[-1]
    assert kw["message"] == 710 and "Caption template saved" in text and "Unknown variables" in text and "{nonsense}" in text
    assert "<b>Caption</b> ▸ <b>{filename}</b> · {size} · {nonsense}" in text and "· default" not in text
    assert f"fm:cr:{i}" in data_of(kw["buttons"])  # a custom caption can be reset with one tap

    cb = await click(ctx, OWNER, f"fm:cr:{i}")
    assert cb.toasts == ["Caption reset."] and (await ctx.db.get_format(OWNER, i))["caption"] == "<b>{filename}</b>"
    assert "· default" in cb.edits[-1][0] and f"fm:cr:{i}" not in data_of(cb.edits[-1][1])
    assert (await autofmt(ctx))["caption"] == "<i>MY OWN {filename}</i>" and (await autofmt(ctx))["id"] == auto["id"]  # never touched

    await ctx.db.update_format(OWNER, i, caption="<u>{filename}</u>")  # sending reset in the prompt does the same
    await click(ctx, OWNER, f"fm:in:{i}:caption")
    await say(ctx, OWNER, "reset")
    assert (await ctx.db.get_format(OWNER, i))["caption"] == "<b>{filename}</b>" and OWNER not in ctx.app.pending


async def test_the_reset_caption_button_of_a_deleted_format_says_so(ctx):
    fmt = await make_format(ctx, caption="<u>{filename}</u>")
    await ctx.db.delete_format(OWNER, fmt["id"])
    cb = await click(ctx, OWNER, f"fm:cr:{fmt['id']}")
    assert cb.alerts == ["That format no longer exists."] and "FORMATS" in cb.edits[-1][0]


async def test_renaming_a_format(ctx):
    fmt = await make_format(ctx, "One Piece")
    await make_format(ctx, "Dragon Ball")
    await click(ctx, OWNER, f"fm:in:{fmt['id']}:name")
    ev = await say(ctx, OWNER, "dragon ball")
    assert "already have a format" in ev.texts[0] and OWNER in ctx.app.pending
    await say(ctx, OWNER, "Straw Hats")
    assert (await ctx.db.get_format(OWNER, fmt["id"]))["name"] == "Straw Hats"
    assert "🗂 Straw Hats" in ctx.client.edited[-1][1]


async def test_a_format_thumbnail_is_saved_shown_and_deleted(ctx):
    fmt = await make_format(ctx)
    i = fmt["id"]
    cb = await click(ctx, OWNER, f"fm:th:{i}", msg_id=800)
    assert ctx.app.pending[OWNER].kind == "fmt:thumb"
    assert cb.edits[-1][0] == panels.FORMAT_PROMPTS["thumb"]
    ev = await say(ctx, OWNER, "not a photo")  # text while a photo is expected
    assert "Send a photo" in ev.texts[0] and OWNER in ctx.app.pending

    ev = FakeEvent(OWNER, message=FakeMessage("", mid=801, photo=True), photo_bytes=jpeg((900, 600)))
    await dispatch(ctx.client, ev)
    assert OWNER not in ctx.app.pending and ev.out == []  # the format's panel is shown again, no "saved for your auto format"
    stored = await ctx.db.get_format_thumb(OWNER, i)
    assert stored[:2] == b"\xff\xd8" and await autofmt(ctx) is None  # the photo went to this format, no "Default" auto format was made
    chat, text, kw = ctx.client.edited[-1]
    assert kw["message"] == 800 and "Thumbnail saved" in text and "✅ saved" in text and f"fm:thd:{i}" in data_of(kw["buttons"])

    cb = await click(ctx, OWNER, f"fm:thd:{i}")
    assert cb.toasts == ["Thumbnail deleted."] and await ctx.db.get_format_thumb(OWNER, i) is None
    assert panels.THUMB_NONE in cb.edits[-1][0]


async def test_a_photo_after_cancelling_the_thumbnail_prompt_is_not_a_thumbnail(ctx):
    manual = await make_format(ctx)
    await click(ctx, OWNER, f"fm:th:{manual['id']}", msg_id=800)
    await click(ctx, OWNER, f"fm:cx:{manual['id']}")  # Cancel
    assert OWNER not in ctx.app.pending
    ev = FakeEvent(OWNER, message=FakeMessage("", mid=900, photo=True), photo_bytes=jpeg())
    await dispatch(ctx.client, ev)
    assert ev.out == [] and await ctx.db.get_format_thumb(OWNER, manual["id"]) is None and await autofmt(ctx) is None


async def test_deleting_a_format_asks_first(ctx):
    fmt = await make_format(ctx)
    i = fmt["id"]
    cb = await click(ctx, OWNER, f"fm:del:{i}")
    assert "Delete the format" in cb.edits[-1][0] and set(data_of(cb.edits[-1][1])) == {f"fm:ok:{i}", f"fm:v:{i}"}
    assert await ctx.db.get_format(OWNER, i) is not None
    cb = await click(ctx, OWNER, f"fm:v:{i}")  # "Keep it"
    assert "🗂 One Piece" in cb.edits[-1][0]
    cb = await click(ctx, OWNER, f"fm:ok:{i}")
    assert await ctx.db.get_format(OWNER, i) is None
    assert "MANUAL FORMATS" in cb.edits[-1][0] and cb.toasts == ["Deleted One Piece."]  # back to the list it was in


async def test_buttons_of_a_deleted_format_say_so_and_show_the_chooser(ctx):
    fmt = await make_format(ctx)
    await ctx.db.delete_format(OWNER, fmt["id"])
    for data in (
        f"fm:v:{fmt['id']}", f"fm:in:{fmt['id']}:title", f"fm:th:{fmt['id']}", f"fm:del:{fmt['id']}", f"fm:ok:{fmt['id']}",
        f"fm:dm:{fmt['id']}", f"fm:cp:{fmt['id']}", f"fm:use:{fmt['id']}",
    ):
        cb = await click(ctx, OWNER, data)
        assert cb.alerts == ["That format no longer exists."] and "FORMATS" in cb.edits[-1][0], data
    assert OWNER not in ctx.app.pending


async def test_typing_into_a_prompt_for_a_deleted_format_is_handled(ctx):
    fmt = await make_format(ctx)
    await click(ctx, OWNER, f"fm:in:{fmt['id']}:title")
    await ctx.db.delete_format(OWNER, fmt["id"])
    ev = await say(ctx, OWNER, "Title")
    assert "no longer exists" in ev.texts[0] and OWNER not in ctx.app.pending


async def test_formats_belong_to_one_user(ctx):
    mine = await make_format(ctx, "Mine")
    auto = await make_format(ctx, "Auto", kind="auto")
    for fid in (mine["id"], auto["id"]):
        cb = await click(ctx, ADMIN_CFG, f"fm:v:{fid}")
        assert cb.alerts == ["That format no longer exists."]
        for action in ("ok", "dm", "cp", "use"):
            await click(ctx, ADMIN_CFG, f"fm:{action}:{fid}")
        assert await ctx.db.get_format(OWNER, fid) is not None
    assert await ctx.db.count_formats(OWNER) == 2 and await ctx.db.count_formats(ADMIN_CFG) == 0
    ev = await say(ctx, ADMIN_CFG, "/formats")
    assert flat_buttons(ev.out[0][1])[:2] == [("🤖 Auto rename (0/12)", "fm:l:auto"), ("✋ Manual rename (0/12)", "fm:l:manual")]
    ev = await say(ctx, ADMIN_CFG, "/rename")
    assert "no manual formats yet" in ev.texts[0]


async def test_any_other_tap_abandons_an_open_prompt(ctx):
    fmt = await make_format(ctx)
    await click(ctx, OWNER, f"fm:in:{fmt['id']}:title")
    await click(ctx, OWNER, "fm:new:manual")
    assert ctx.app.pending[OWNER].kind == "fmt:new"
    await click(ctx, OWNER, f"fm:v:{fmt['id']}")
    assert OWNER not in ctx.app.pending


# ---------------------------------------------------------------------------
# /rename: pick a manual format, send one file
# ---------------------------------------------------------------------------


async def test_rename_without_manual_formats_points_to_creating_one(ctx):
    ev = await say(ctx, OWNER, "/rename")
    assert "no manual formats yet" in ev.texts[0] and "fm:new:manual" in data_of(ev.out[0][1])
    ev = await say(ctx, OWNER, "/rename New Name", reply=FakeMessage("x", mid=3))  # the old reply-with-a-name form is gone
    assert "no manual formats yet" in ev.texts[0] and not ctx.app.queue.jobs
    await make_format(ctx, "Auto One", kind="auto")  # an auto format is not for /rename
    ev = await say(ctx, OWNER, "/rename")
    assert "no manual formats yet" in ev.texts[0]


async def test_rename_offers_the_saved_manual_formats_by_name(ctx):
    a, b = await make_format(ctx, "One Piece"), await make_format(ctx, "Dragon Ball")
    auto = await make_format(ctx, "Auto One", kind="auto")
    ev = await say(ctx, OWNER, "/rename")
    assert "Choose the manual format" in ev.texts[0]
    data = data_of(ev.out[0][1])
    assert f"rn:p:{a['id']}" in data and f"rn:p:{b['id']}" in data and "fm:l:manual" in data
    assert f"rn:p:{auto['id']}" not in data and "Auto One" not in labels_of(ev.out[0][1])  # that one is chosen in the /start menu
    assert "One Piece" in labels_of(ev.out[0][1]) and "Dragon Ball" in labels_of(ev.out[0][1])
    assert not ctx.app.queue.jobs and ctx.app._mode(OWNER) is None  # nothing is armed until a format is chosen


async def test_choosing_a_format_then_sending_a_file_renames_it_with_that_format(ctx):
    await make_format(ctx, "Auto One", kind="auto", template="DEFAULT {title}")
    fmt = await make_format(ctx, meta={"title": "One Piece"})
    cb = await click(ctx, OWNER, f"rn:p:{fmt['id']}")
    assert cb.toasts == ["Format: One Piece"] and ctx.app._mode(OWNER, "rename") is not None
    text, buttons = cb.edits[-1]
    assert "One Piece" in text and "one file at a time" in text and "/rename again" in text
    assert set(data_of(buttons)) == {"rn:menu", "rn:stop"}

    ev = await send_doc(ctx, OWNER, ONE_PIECE, mid=50)
    (job,) = ctx.app.queue.jobs.values()
    assert job.new_name == "One Piece - E1147.mkv"  # the manual format's template, not the auto format's
    assert job.fmt["id"] == fmt["id"] and job.fmt["meta"]["title"] == "One Piece" and job.order == 50
    assert "Queued" in ev.texts[0] and "🗂 Format: <b>One Piece</b>" in ev.texts[0] and "One Piece - E1147.mkv" in ev.texts[0]
    assert ctx.app._mode(OWNER) is None  # it was for one file


async def test_other_settings_still_come_from_the_users_own_settings(ctx):
    await ctx.db.update_settings(OWNER, mode="caption", remove_words=["@Junk"])
    fmt = await make_format(ctx)
    await click(ctx, OWNER, f"rn:p:{fmt['id']}")
    await send_doc(ctx, OWNER, "xyz.mkv", caption="1148 - One Piece 720p @Junk", mid=51)
    (job,) = ctx.app.queue.jobs.values()
    assert job.new_name == "One Piece - E1148.mkv"  # read from the caption (the user's mode)


async def test_the_next_file_needs_rename_again(ctx):
    auto = await make_format(ctx, "Auto One", kind="auto", template="Auto - E{episode}")
    fmt = await make_format(ctx)
    await click(ctx, OWNER, f"rn:p:{fmt['id']}")
    await send_doc(ctx, OWNER, ONE_PIECE, mid=50)
    # a second file straight after is not auto-renamed behind the user's back: one hint, then silence
    ev = await send_doc(ctx, OWNER, "1148 - One Piece [Dual] 720p.mkv", mid=51)
    assert "one file at a time" in ev.texts[0] and "/rename again" in ev.texts[0]
    ev = await send_doc(ctx, OWNER, "1149 - One Piece [Dual] 720p.mkv", mid=52)
    assert ev.out == []
    assert [j.file_name for j in ctx.app.queue.jobs.values()] == [ONE_PIECE]

    # once the short grace period is over, files are renamed with the auto format as usual
    ctx.app._recent[OWNER][1] -= appmod.ONE_SHOT_GRACE + 1
    ev = await send_doc(ctx, OWNER, "1150 - One Piece [Dual] 720p.mkv", mid=53)
    assert "Queued" in ev.texts[0] and "🗂 Auto format: <b>Auto One</b>" in ev.texts[0] and "Auto - E1150.mkv" in ev.texts[0]
    assert len(ctx.app.queue.jobs) == 2

    # and choosing a format again renames the next one with it
    await click(ctx, OWNER, f"rn:p:{fmt['id']}")
    ev = await send_doc(ctx, OWNER, "1151 - One Piece [Dual] 720p.mkv", mid=54)
    assert "🗂 Format: <b>One Piece</b>" in ev.texts[0] and len(ctx.app.queue.jobs) == 3
    assert auto["id"] != fmt["id"]


async def test_a_forwarded_batch_gives_the_format_to_exactly_one_file(ctx):
    fmt = await make_format(ctx)
    await click(ctx, OWNER, f"rn:p:{fmt['id']}")
    events = [FakeEvent(OWNER, message=document_message(f"114{n} - One Piece 720p.mkv", 1024, mid=60 + n)) for n in range(5, 9)]
    await asyncio.gather(*(dispatch(ctx.client, ev) for ev in events))
    jobs = list(ctx.app.queue.jobs.values())
    assert len(jobs) == 1 and jobs[0].fmt["id"] == fmt["id"]
    hints = [t for ev in events for t in ev.texts if "one file at a time" in t]
    assert len(hints) == 1  # one hint for the whole batch, not one per file


async def test_the_chosen_format_is_forgotten_after_the_wait(ctx):
    fmt = await make_format(ctx)
    await click(ctx, OWNER, f"rn:p:{fmt['id']}")
    ctx.app.modes[OWNER].expires = time.time() - 1
    ev = await send_doc(ctx, OWNER, ONE_PIECE, mid=50)
    (job,) = ctx.app.queue.jobs.values()
    assert job.fmt is None and "Format" not in ev.texts[0]  # a normal auto-rename (the user has no auto format)
    assert ctx.app._mode(OWNER) is None


async def test_a_format_deleted_after_choosing_it_is_reported_and_nothing_is_queued(ctx):
    fmt = await make_format(ctx)
    await click(ctx, OWNER, f"rn:p:{fmt['id']}")
    await ctx.db.delete_format(OWNER, fmt["id"])
    ev = await send_doc(ctx, OWNER, ONE_PIECE, mid=50)
    assert "format was deleted" in ev.texts[0] and not ctx.app.queue.jobs and ctx.app._mode(OWNER) is None


async def test_a_template_variable_missing_from_the_file_is_flagged_for_the_format_too(ctx):
    fmt = await make_format(ctx)
    await click(ctx, OWNER, f"rn:p:{fmt['id']}")
    ev = await send_doc(ctx, OWNER, "Plain Movie.mkv", mid=50)
    (job,) = ctx.app.queue.jobs.values()
    assert job.new_name == "One Piece - E.mkv" and "⚠️" in ev.texts[0] and "{episode}" in ev.texts[0]


async def test_choosing_a_format_that_is_gone_or_not_yours(ctx):
    fmt = await make_format(ctx)
    cb = await click(ctx, ADMIN_CFG, f"rn:p:{fmt['id']}")  # somebody else's
    assert cb.alerts == ["That format no longer exists."] and ctx.app._mode(ADMIN_CFG) is None
    await ctx.db.delete_format(OWNER, fmt["id"])
    cb = await click(ctx, OWNER, f"rn:p:{fmt['id']}")
    assert cb.alerts == ["That format no longer exists."] and "no manual formats yet" in cb.edits[-1][0]


async def test_an_auto_format_cannot_be_chosen_for_rename(ctx):
    auto = await make_format(ctx, "Auto One", kind="auto")
    cb = await click(ctx, OWNER, f"rn:p:{auto['id']}")  # a stale or made-up button: the picker never offers it
    assert cb.alerts == ["That format no longer exists."] and ctx.app._mode(OWNER) is None
    await send_doc(ctx, OWNER, ONE_PIECE, mid=50)
    (job,) = ctx.app.queue.jobs.values()
    assert job.fmt["id"] == auto["id"]  # the file goes through the auto format, as any file does


async def test_one_users_chosen_format_does_not_affect_another(ctx):
    fmt = await make_format(ctx)
    await click(ctx, OWNER, f"rn:p:{fmt['id']}")
    await send_doc(ctx, ADMIN_CFG, EPISODE, mid=70)
    (job,) = ctx.app.queue.jobs.values()
    assert job.user_id == ADMIN_CFG and job.fmt is None
    assert ctx.app._mode(OWNER, "rename") is not None  # still waiting for the owner's file


async def test_change_format_and_stop_buttons(ctx):
    a, b = await make_format(ctx, "One Piece"), await make_format(ctx, "Dragon Ball")
    await click(ctx, OWNER, f"rn:p:{a['id']}")
    cb = await click(ctx, OWNER, "rn:menu")
    assert "Choose the manual format" in cb.edits[-1][0] and ctx.app._mode(OWNER) is None
    await click(ctx, OWNER, f"rn:p:{b['id']}")
    assert ctx.app.modes[OWNER].data["fid"] == b["id"]
    cb = await click(ctx, OWNER, "rn:stop")
    assert "Stopped" in cb.edits[-1][0] and ctx.app._mode(OWNER) is None
    ev = await send_doc(ctx, OWNER, ONE_PIECE, mid=50)
    (job,) = ctx.app.queue.jobs.values()
    assert job.fmt is None and "Format" not in ev.texts[0]
    assert not ctx.app._recent  # stopping is not a finished one-file command: no hint for later files


async def test_cancel_ends_the_chosen_format_before_it_touches_queued_files(ctx):
    fmt = await make_format(ctx)
    await send_doc(ctx, OWNER, EPISODE, mid=40)  # something is already queued
    await click(ctx, OWNER, f"rn:p:{fmt['id']}")
    ev = await say(ctx, OWNER, "/cancel")
    assert "Cancelled" in ev.texts[0] and "/rename again" in ev.texts[0] and "/cancel again" in ev.texts[0]
    assert ctx.app._mode(OWNER) is None and len(ctx.app.queue.jobs) == 1
    ev = await say(ctx, OWNER, "/cancel")
    assert "Cancelling 1 job" in ev.texts[0] and not ctx.app.queue.jobs


async def test_cancel_with_nothing_armed_still_cancels_jobs(ctx):
    await send_doc(ctx, OWNER, EPISODE, mid=40)
    ev = await say(ctx, OWNER, "/cancel")
    assert "Cancelling 1 job" in ev.texts[0]
    ev = await say(ctx, OWNER, "/cancel")
    assert "no active jobs" in ev.texts[0]


async def test_start_and_the_rename_picker_close_an_open_text_prompt(ctx):
    fmt = await make_format(ctx)
    await click(ctx, OWNER, f"fm:in:{fmt['id']}:title")
    await say(ctx, OWNER, "/rename")
    assert OWNER not in ctx.app.pending
    await click(ctx, OWNER, f"fm:in:{fmt['id']}:title")
    await say(ctx, OWNER, "/formats")
    assert OWNER not in ctx.app.pending


@pytest.mark.parametrize("kind", KINDS)
def test_the_format_screens_fit_in_a_caption_even_with_the_longest_values(kind):
    from .test_panels import CAPTION_LIMIT, visible

    longest = ["klingon-language-tag-x"] * 12
    fmt = {
        "id": 12, "name": "N" * 30, "kind": kind, "has_thumb": True, "template": "T" * 200, "caption": "C" * 800,
        "meta": {k: "M" * 200 for k in ("title", "author", "artist", "audio", "subtitle", "video")},
        "keep_audio": longest, "keep_subtitle": longest,
    }
    formats = [dict(fmt, id=i, name="F" * 30) for i in range(12)]
    other = dict(fmt, kind="manual" if kind == "auto" else "auto", id=13)
    for name, (text, rows) in {
        "view": panels.format_view(fmt), "view_active": panels.format_view(fmt, active=True),
        "view_inline": panels.format_view(fmt, share_inline=True),
        "menu": panels.formats_menu(formats, 12, kind, 3), "delete": panels.format_delete_confirm(fmt),
        "chooser": panels.formats_home(12, 12, 12), "auto_menu": panels.auto_menu(formats, 3, 12),
        "ready": panels.rename_ready(fmt, 10), "picker": panels.rename_picker(formats),
        "copied": panels.format_copied(fmt, other), "metadata_applied": (panels.format_metadata_applied(fmt), []),
        "shared": panels.shared_format(fmt, "T" * 16), "shared_mine": panels.shared_format(fmt, "T" * 16, mine=True),
        "added": panels.shared_added(fmt), "card": (panels.share_card(fmt)[2], []),
        **{f"prompt_{k}": panels.format_prompt(k, 12) for k in panels.FORMAT_PROMPTS if k != "new"},
        "prompt_new": panels.format_prompt("new", 0, kind),
    }.items():
        assert len(visible(text)) <= CAPTION_LIMIT, (name, len(visible(text)))
        assert text.count("<b>") == text.count("</b>") and text.count("<code>") == text.count("</code>"), name
        for row in rows:
            assert 1 <= len(row) <= 4, name
            for label, data in row:
                assert label.strip() and len(label) <= 32 and 0 < len(data.encode()) <= 64, (name, label, data)
