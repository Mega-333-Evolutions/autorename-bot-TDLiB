"""Auto formats in the app: the /start menu's Auto Format screen, Use for auto rename, copying a format to the other kind,
Apply default metadata, and what the files are renamed with."""
import pytest

from bot.db import MAX_FORMATS
from bot.formats import apply_format

from .appkit import ADMIN_CFG, EPISODE_NEW, OWNER, autofmt, click, data_of, jpeg, prefs, say, send_doc
from .test_formats_app import ONE_PIECE, labels_of, make_format

DEFAULT_META = {"title": "My Channel", "author": "@MyChannel", "artist": "", "audio": "{lang} Audio", "subtitle": "{lang}", "video": "HD"}


async def set_default_metadata(ctx, **over):
    await ctx.db.update_settings(OWNER, meta=dict(DEFAULT_META), keep_audio=["japanese", "english"], keep_subtitle=["english"], **over)


def latest_job(ctx):
    return list(ctx.app.queue.jobs.values())[-1]


# ---------------------------------------------------------------------------
# the Auto Format screen of the /start menu
# ---------------------------------------------------------------------------


async def test_the_start_menu_opens_the_auto_format_screen(ctx):
    ev = await say(ctx, OWNER, "/start")
    text, buttons, _kw = ev.out[0]
    assert "<b>Auto format:</b> none saved" in text and "m:auto" in data_of(buttons) and "Auto Format" in labels_of(buttons)
    cb = await click(ctx, OWNER, "m:auto")
    text, buttons = cb.edits[-1]
    assert "AUTO RENAME FORMAT" in text and "no auto formats yet" in text and cb.answers
    data = data_of(buttons)
    assert {"fm:new:auto", "fm:l:auto", "m:home", "x:close"} <= set(data) and not any(d.startswith("au:") for d in data)


async def test_the_screen_lists_the_auto_formats_and_marks_the_one_in_use(ctx):
    a = await make_format(ctx, "One Piece", kind="auto", template="OP - E{episode}")
    b = await make_format(ctx, "Dragon Ball", kind="auto", template="DB - E{episode}")
    manual = await make_format(ctx, "Manual one")
    cb = await click(ctx, OWNER, "m:auto")
    text, buttons = cb.edits[-1]
    data = data_of(buttons)
    assert f"au:{a['id']}" in data and f"au:{b['id']}" in data and f"au:{manual['id']}" not in data
    assert labels_of(buttons)[:2] == ["One Piece ✅", "Dragon Ball"]  # the first one is in use from the start
    assert f"fm:v:{a['id']}" in data  # "Edit the one in use"

    cb = await click(ctx, OWNER, f"au:{b['id']}")
    assert cb.toasts == ["Auto format: Dragon Ball"] and labels_of(cb.edits[-1][1])[:2] == ["One Piece", "Dragon Ball ✅"]
    assert (await prefs(ctx))["auto_format"] == b["id"] and (await autofmt(ctx))["id"] == b["id"]
    data = data_of(cb.edits[-1][1])
    assert f"fm:v:{b['id']}" in data and f"fm:v:{a['id']}" not in data  # the edit button follows the choice

    ev = await say(ctx, OWNER, "/start")  # and the menu shows what is in use
    assert "<b>Auto format:</b> <b>Dragon Ball</b>" in ev.texts[0] and "DB - E{episode}" in ev.texts[0]


async def test_a_stale_or_foreign_choice_is_refused(ctx):
    manual = await make_format(ctx, "Manual one")
    theirs = await make_format(ctx, "Theirs", uid=ADMIN_CFG, kind="auto")
    await make_format(ctx, "Mine", kind="auto")
    for data in (f"au:{manual['id']}", f"au:{theirs['id']}", "au:999999", "au:x", "au"):
        cb = await click(ctx, OWNER, data)
        assert cb.alerts == ["That format no longer exists."] and "AUTO RENAME FORMAT" in cb.edits[-1][0], data
        assert (await autofmt(ctx))["name"] == "Mine"


async def test_each_user_has_their_own_auto_format(ctx):
    mine = await make_format(ctx, "Mine", kind="auto", template="Mine - {title}")
    theirs = await make_format(ctx, "Theirs", uid=ADMIN_CFG, kind="auto", template="Theirs - {title}")
    await send_doc(ctx, OWNER, ONE_PIECE, mid=50)
    await send_doc(ctx, ADMIN_CFG, ONE_PIECE, mid=51)
    jobs = {j.user_id: j for j in ctx.app.queue.jobs.values()}
    assert jobs[OWNER].new_name == "Mine - One Piece.mkv" and jobs[OWNER].fmt["id"] == mine["id"]
    assert jobs[ADMIN_CFG].new_name == "Theirs - One Piece.mkv" and jobs[ADMIN_CFG].fmt["id"] == theirs["id"]


# ---------------------------------------------------------------------------
# what the files are renamed with
# ---------------------------------------------------------------------------


async def test_the_auto_format_in_use_renames_every_file_sent(ctx):
    a = await make_format(ctx, "One Piece", kind="auto", template="OP - E{episode}")
    b = await make_format(ctx, "Dragon Ball", kind="auto", template="DB - E{episode}")
    ev = await send_doc(ctx, OWNER, ONE_PIECE, mid=50)
    job = latest_job(ctx)
    assert job.new_name == "OP - E1147.mkv" and job.fmt["id"] == a["id"] and job.fmt["kind"] == "auto"
    assert "🗂 Auto format: <b>One Piece</b>" in ev.texts[0]
    await click(ctx, OWNER, f"au:{b['id']}")
    ev = await send_doc(ctx, OWNER, ONE_PIECE, mid=51)
    job = latest_job(ctx)
    assert job.new_name == "DB - E1147.mkv" and job.fmt["id"] == b["id"] and "🗂 Auto format: <b>Dragon Ball</b>" in ev.texts[0]


async def test_without_an_auto_format_files_get_the_built_in_template(ctx):
    await make_format(ctx, "Manual one")  # a manual format is never used by itself
    ev = await send_doc(ctx, OWNER)
    (job,) = ctx.app.queue.jobs.values()
    assert job.fmt is None and job.new_name == EPISODE_NEW and "Auto format" not in ev.texts[0]


async def test_the_padded_episode_variables_in_an_auto_format(ctx):
    await make_format(ctx, "Padded", kind="auto", template="{title} - {1episode} {2episode} {3episode}")
    for mid, name, expected in (
        (50, "One Piece - 4 [1080p].mkv", "One Piece - 04 004 0004.mkv"),
        (51, "One Piece - 321 [1080p].mkv", "One Piece - 321 321 0321.mkv"),
        (52, "One Piece - 1321 [1080p].mkv", "One Piece - 1321 1321 1321.mkv"),
    ):
        await send_doc(ctx, OWNER, name, mid=mid)
        assert latest_job(ctx).new_name == expected


async def test_the_first_auto_format_is_used_at_once_and_deleting_the_one_in_use_hands_over(ctx):
    await click(ctx, OWNER, "fm:new:auto", msg_id=500)
    await say(ctx, OWNER, "One Piece")
    (a,) = await ctx.db.list_formats(OWNER, "auto")
    _chat, text, kw = ctx.client.edited[-1]
    assert "✅ In use" in text and f"fm:use:{a['id']}" not in data_of(kw["buttons"])
    assert (await autofmt(ctx))["id"] == a["id"]
    await make_format(ctx, "Dragon Ball", kind="auto")

    await click(ctx, OWNER, f"fm:del:{a['id']}")
    cb = await click(ctx, OWNER, f"fm:ok:{a['id']}")  # the one in use goes: the other takes over
    assert "AUTO FORMATS" in cb.edits[-1][0] and "Dragon Ball ✅" in labels_of(cb.edits[-1][1])
    last = await autofmt(ctx)
    assert last["name"] == "Dragon Ball"
    await click(ctx, OWNER, f"fm:ok:{last['id']}")  # the last one goes: back to the built-in defaults
    assert await autofmt(ctx) is None
    await send_doc(ctx, OWNER)
    (job,) = ctx.app.queue.jobs.values()
    assert job.fmt is None and job.new_name == EPISODE_NEW


async def test_use_for_auto_rename_button(ctx):
    a = await make_format(ctx, "One Piece", kind="auto")
    b = await make_format(ctx, "Dragon Ball", kind="auto")
    cb = await click(ctx, OWNER, f"fm:v:{a['id']}")
    assert "✅ In use" in cb.edits[-1][0] and f"fm:use:{a['id']}" not in data_of(cb.edits[-1][1])
    cb = await click(ctx, OWNER, f"fm:v:{b['id']}")
    text, buttons = cb.edits[-1]
    assert "Not in use" in text and f"fm:use:{b['id']}" in data_of(buttons) and "✅ Use for auto rename" in labels_of(buttons)

    cb = await click(ctx, OWNER, f"fm:use:{b['id']}")
    text, buttons = cb.edits[-1]
    assert cb.toasts == ["Auto format: Dragon Ball"] and "now renames your files" in text and "✅ In use" in text
    assert f"fm:use:{b['id']}" not in data_of(buttons) and (await autofmt(ctx))["id"] == b["id"]
    cb = await click(ctx, OWNER, f"fm:v:{a['id']}")  # the other one can be used again
    assert f"fm:use:{a['id']}" in data_of(cb.edits[-1][1])


async def test_a_manual_format_cannot_be_used_for_auto_rename(ctx):
    auto = await make_format(ctx, "Auto one", kind="auto")
    manual = await make_format(ctx, "Manual one")
    cb = await click(ctx, OWNER, f"fm:v:{manual['id']}")
    assert f"fm:use:{manual['id']}" not in data_of(cb.edits[-1][1]) and "Use it with /rename" in cb.edits[-1][0]
    cb = await click(ctx, OWNER, f"fm:use:{manual['id']}")  # a stale button
    assert cb.alerts == ["Only an auto format can be used for auto rename."] and cb.edits == []
    assert (await autofmt(ctx))["id"] == auto["id"]


# ---------------------------------------------------------------------------
# copying a format to the other kind
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source, target", [("auto", "manual"), ("manual", "auto")])
async def test_a_format_can_be_copied_to_the_other_kind(ctx, source, target):
    fmt = await make_format(ctx, "One Piece", kind=source, meta={"title": "OP"}, keep_audio=["japanese"], caption="<i>{filename}</i>")
    await ctx.db.set_format_thumb(OWNER, fmt["id"], jpeg())
    cb = await click(ctx, OWNER, f"fm:v:{fmt['id']}")
    assert f"fm:cp:{fmt['id']}" in data_of(cb.edits[-1][1]) and f"📋 Copy to {target} formats" in labels_of(cb.edits[-1][1])

    cb = await click(ctx, OWNER, f"fm:cp:{fmt['id']}")
    (copy,) = await ctx.db.list_formats(OWNER, target)
    assert copy["id"] != fmt["id"] and copy["kind"] == target and copy["name"] == "One Piece"
    assert copy["template"] == "One Piece - E{episode}" and copy["meta"]["title"] == "OP" and copy["keep_audio"] == ["japanese"]
    assert copy["caption"] == "<i>{filename}</i>" and await ctx.db.get_format_thumb(OWNER, copy["id"]) == jpeg()
    text, buttons = cb.edits[-1]
    assert cb.toasts == [f"Copied to your {target} formats."] and f"is copied to your {target} formats" in text
    assert data_of(buttons) == [f"fm:v:{copy['id']}", f"fm:v:{fmt['id']}", "x:close"]
    assert (await ctx.db.get_format(OWNER, fmt["id"]))["kind"] == source and await ctx.db.count_formats(OWNER, source) == 1

    cb = await click(ctx, OWNER, f"fm:v:{copy['id']}")  # "Open the copy": it is a format of the other kind
    assert f"{'🤖 Auto' if target == 'auto' else '✋ Manual'}" in cb.edits[-1][0].splitlines()[0]


async def test_copies_are_independent_and_get_a_free_name(ctx):
    fmt = await make_format(ctx, "One Piece", kind="auto")
    await click(ctx, OWNER, f"fm:cp:{fmt['id']}")
    await click(ctx, OWNER, f"fm:cp:{fmt['id']}")
    first, second = await ctx.db.list_formats(OWNER, "manual")
    assert [first["name"], second["name"]] == ["One Piece", "One Piece (2)"]
    await click(ctx, OWNER, f"fm:cp:{first['id']}")  # and back again: "One Piece" is taken among the auto formats
    back = (await ctx.db.list_formats(OWNER, "auto"))[-1]
    assert back["name"] == "One Piece (2)" and back["kind"] == "auto" and await ctx.db.count_formats(OWNER, "auto") == 2

    await click(ctx, OWNER, f"fm:in:{first['id']}:template")
    await say(ctx, OWNER, "Changed {title}")
    assert (await ctx.db.get_format(OWNER, first["id"]))["template"] == "Changed {title}"
    assert (await ctx.db.get_format(OWNER, fmt["id"]))["template"] == "One Piece - E{episode}"
    assert (await ctx.db.get_format(OWNER, second["id"]))["template"] == "One Piece - E{episode}"


@pytest.mark.parametrize("kind", ["auto", "manual"])
async def test_copying_to_a_full_kind_is_refused_and_the_kinds_count_apart(ctx, kind):
    other = "manual" if kind == "auto" else "auto"
    fmt = await make_format(ctx, "One Piece", kind=kind)
    for i in range(MAX_FORMATS):
        await make_format(ctx, f"F{i}", kind=other)
    cb = await click(ctx, OWNER, f"fm:cp:{fmt['id']}")
    assert cb.alerts == [f"You already have {MAX_FORMATS} {other} formats. Delete one first."] and cb.edits == []
    assert await ctx.db.count_formats(OWNER, other) == MAX_FORMATS and await ctx.db.count_formats(OWNER, kind) == 1


async def test_a_copy_shows_up_where_its_kind_is_used(ctx):
    auto = await make_format(ctx, "From auto", kind="auto")
    manual = await make_format(ctx, "From manual")
    await click(ctx, OWNER, f"fm:cp:{auto['id']}")  # a manual copy of the auto format
    await click(ctx, OWNER, f"fm:cp:{manual['id']}")  # an auto copy of the manual one
    assert {"From manual", "From auto"} <= set(labels_of((await say(ctx, OWNER, "/rename")).out[0][1]))
    labels = labels_of((await click(ctx, OWNER, "m:auto")).edits[-1][1])
    assert "From auto ✅" in labels and "From manual" in labels


async def test_an_auto_format_made_by_copying_is_the_first_one_and_renames_the_files(ctx):
    manual = await make_format(ctx, "One Piece")
    assert await autofmt(ctx) is None
    await click(ctx, OWNER, f"fm:cp:{manual['id']}")
    auto = await autofmt(ctx)
    assert auto["kind"] == "auto" and auto["name"] == "One Piece"
    ev = await send_doc(ctx, OWNER, ONE_PIECE, mid=50)
    assert "🗂 Auto format: <b>One Piece</b>" in ev.texts[0] and latest_job(ctx).new_name == "One Piece - E1147.mkv"


# ---------------------------------------------------------------------------
# Apply default metadata
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["auto", "manual"])
async def test_apply_default_metadata_copies_all_of_it_into_the_format(ctx, kind):
    await set_default_metadata(ctx)  # the metadata switch is Off: copying does not depend on it
    fmt = await make_format(
        ctx, "One Piece", kind=kind, meta={"title": "Old title", "artist": "Old artist"}, keep_audio=["french"], keep_subtitle=["2"]
    )
    cb = await click(ctx, OWNER, f"fm:dm:{fmt['id']}")
    got = await ctx.db.get_format(OWNER, fmt["id"])
    assert got["meta"] == DEFAULT_META and got["keep_audio"] == ["japanese", "english"] and got["keep_subtitle"] == ["english"]
    assert got["template"] == "One Piece - E{episode}" and got["kind"] == kind  # nothing else changed
    text, buttons = cb.edits[-1]
    assert cb.toasts == ["Default metadata applied."] and "Your default metadata is now in <b>One Piece</b>" in text
    assert "<b>Title</b> ▸ <code>My Channel</code>" in text and "Japanese, English" in text and f"fm:dm:{fmt['id']}" in data_of(buttons)

    s = await prefs(ctx)  # the default metadata itself is untouched, and the format is not linked to it afterwards
    assert s["meta"] == DEFAULT_META and s["keep_audio"] == ["japanese", "english"] and s["metadata_on"] is False
    await ctx.db.update_settings(OWNER, meta={"title": "Changed later"})
    assert (await ctx.db.get_format(OWNER, fmt["id"]))["meta"]["title"] == "My Channel"


@pytest.mark.parametrize("kind", ["auto", "manual"])
async def test_a_new_format_can_take_the_default_metadata_straight_away(ctx, kind):
    await set_default_metadata(ctx)
    await click(ctx, OWNER, f"fm:new:{kind}", msg_id=500)
    await say(ctx, OWNER, "Fresh")
    (fmt,) = await ctx.db.list_formats(OWNER)
    _chat, text, kw = ctx.client.edited[-1]
    assert "🏷 Apply default metadata" in labels_of(kw["buttons"]) and "Apply default metadata" in text
    assert fmt["meta"]["title"] == ""
    await click(ctx, OWNER, f"fm:dm:{fmt['id']}")
    assert (await ctx.db.get_format(OWNER, fmt["id"]))["meta"] == DEFAULT_META


@pytest.mark.parametrize("kind", ["auto", "manual"])
async def test_apply_default_metadata_says_so_when_there_is_none(ctx, kind):
    fmt = await make_format(ctx, "One Piece", kind=kind, meta={"title": "Mine"})
    await ctx.db.update_settings(OWNER, metadata_on=True)  # the switch alone is not metadata
    cb = await click(ctx, OWNER, f"fm:dm:{fmt['id']}")
    assert cb.alerts == ["Your default metadata is empty. Set it in /metadata first, then apply it."] and cb.edits == []
    assert (await ctx.db.get_format(OWNER, fmt["id"]))["meta"]["title"] == "Mine"  # nothing was wiped


async def test_track_lists_alone_count_as_default_metadata(ctx):
    await ctx.db.update_settings(OWNER, keep_audio=["english"])
    fmt = await make_format(ctx, kind="auto")
    cb = await click(ctx, OWNER, f"fm:dm:{fmt['id']}")
    assert cb.toasts == ["Default metadata applied."] and (await ctx.db.get_format(OWNER, fmt["id"]))["keep_audio"] == ["english"]


async def test_a_file_renamed_with_a_format_that_took_the_default_metadata_gets_it(ctx):
    await set_default_metadata(ctx)
    fmt = await make_format(ctx)
    await click(ctx, OWNER, f"fm:dm:{fmt['id']}")
    await click(ctx, OWNER, f"rn:p:{fmt['id']}")
    await send_doc(ctx, OWNER, ONE_PIECE, mid=50)
    job = latest_job(ctx)
    assert job.fmt["meta"]["title"] == "My Channel" and job.fmt["keep_audio"] == ["japanese", "english"]
    out = apply_format(await prefs(ctx), job.fmt)
    assert out["metadata_on"] is True and out["meta"]["video"] == "HD"  # written, although the default switch is Off


async def test_an_auto_format_says_whether_the_default_metadata_applies_to_it(ctx):
    auto = await make_format(ctx, "Auto one", kind="auto")
    manual = await make_format(ctx, "Manual one")
    cb = await click(ctx, OWNER, f"fm:v:{auto['id']}")
    assert "No metadata of its own: your default metadata is used while it is On" in cb.edits[-1][0]
    assert "your default metadata is used" not in (await click(ctx, OWNER, f"fm:v:{manual['id']}")).edits[-1][0]  # a manual one never uses it
    await ctx.db.update_format(OWNER, auto["id"], meta={"title": "Own"})
    assert "your default metadata is used" not in (await click(ctx, OWNER, f"fm:v:{auto['id']}")).edits[-1][0]


async def test_the_metadata_screen_calls_it_default_metadata_and_points_to_the_button(ctx):
    cb = await click(ctx, OWNER, "m:meta")
    text = cb.edits[-1][0]
    assert "Your default metadata is currently: Off" in text and "Apply default metadata" in text and "global" not in text.lower()
    await click(ctx, OWNER, "set:meta:on")
    assert (await prefs(ctx))["metadata_on"] is True  # the On/Off switch and the /metadata commands are unchanged
    assert "currently: On" in (await say(ctx, OWNER, "/metadata")).texts[0]
