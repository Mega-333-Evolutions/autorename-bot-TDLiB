import pytest

from bot.db import DEFAULT_CAPTION, DEFAULT_FORMAT, DEFAULT_SETTINGS, MAX_FORMATS, Database
from bot.errors import FormatError
from bot.formats import (
    NAME_MAX, FormatSettings, apply_format, default_auto_format, default_metadata_changes, has_default_metadata,
    has_metadata, other_kind, validate_name,
)
from bot.parser import DEFAULT_TEMPLATE
from bot.settings_ops import apply_setting

U, V = 111, 222


@pytest.fixture
async def db(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/f.db")
    await database.init()
    yield database
    await database.close()


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------


async def test_a_new_format_has_defaults_and_shows_up_in_the_list(db):
    fmt = await db.create_format(U, "One Piece")
    assert fmt["name"] == "One Piece" and fmt["id"] > 0 and fmt["has_thumb"] is False
    assert {k: fmt[k] for k in DEFAULT_FORMAT} == DEFAULT_FORMAT
    await db.create_format(U, "Dragon Ball", template="DB - E{episode}")
    listed = await db.list_formats(U)
    assert [f["name"] for f in listed] == ["One Piece", "Dragon Ball"]  # creation order
    assert listed[1]["template"] == "DB - E{episode}"
    assert await db.count_formats(U) == 2 and await db.count_formats(V) == 0
    assert (await db.get_format(U, fmt["id"]))["name"] == "One Piece"


async def test_names_are_unique_per_user_ignoring_case(db):
    await db.create_format(U, "One Piece")
    with pytest.raises(FormatError) as exc:
        await db.create_format(U, "one piece")
    assert exc.value.code == "taken"
    await db.create_format(V, "One Piece")  # someone else may use the same name


async def test_a_user_can_save_twelve_formats_and_no_more(db):
    for i in range(MAX_FORMATS):
        await db.create_format(U, f"Format {i}")
    with pytest.raises(FormatError) as exc:
        await db.create_format(U, "One too many")
    assert exc.value.code == "limit"
    first = (await db.list_formats(U))[0]
    assert await db.delete_format(U, first["id"])
    await db.create_format(U, "One too many")  # a slot is free again
    assert await db.count_formats(U) == MAX_FORMATS


async def test_update_changes_fields_and_merges_meta(db):
    fmt = await db.create_format(U, "One Piece")
    out = await db.update_format(U, fmt["id"], template="One Piece - E{episode}", keep_audio=["japanese", "english"])
    assert out["template"] == "One Piece - E{episode}" and out["keep_audio"] == ["japanese", "english"]
    await db.update_format(U, fmt["id"], meta={"title": "One Piece"})
    out = await db.update_format(U, fmt["id"], meta={"author": "@Me"}, keep_subtitle=["english"])
    assert out["meta"]["title"] == "One Piece" and out["meta"]["author"] == "@Me" and out["meta"]["artist"] == ""
    assert out["keep_subtitle"] == ["english"]
    with pytest.raises(KeyError):
        await db.update_format(U, fmt["id"], bogus=1)


async def test_formats_persist_across_database_instances(db):
    fmt = await db.create_format(U, "One Piece", template="X {title}")
    await db.update_format(U, fmt["id"], meta={"video": "V"}, keep_audio=["2", "1"])
    await db.set_format_thumb(U, fmt["id"], b"jpegbytes")
    other = Database(db.url)
    got = await other.get_format(U, fmt["id"])
    assert got["template"] == "X {title}" and got["meta"]["video"] == "V" and got["keep_audio"] == ["2", "1"] and got["has_thumb"]
    assert await other.get_format_thumb(U, fmt["id"]) == b"jpegbytes"
    await other.close()


async def test_renaming_a_format_checks_the_other_names_only(db):
    a = await db.create_format(U, "One Piece")
    await db.create_format(U, "Dragon Ball")
    assert (await db.update_format(U, a["id"], name="ONE PIECE"))["name"] == "ONE PIECE"  # its own name, new case
    with pytest.raises(FormatError):
        await db.update_format(U, a["id"], name="dragon ball")
    assert (await db.get_format(U, a["id"]))["name"] == "ONE PIECE"


async def test_thumbnails_belong_to_one_format(db):
    a = await db.create_format(U, "A")
    b = await db.create_format(U, "B")
    assert await db.get_format_thumb(U, a["id"]) is None
    assert await db.set_format_thumb(U, a["id"], b"A-thumb")
    assert [f["has_thumb"] for f in await db.list_formats(U)] == [True, False]
    assert await db.get_format_thumb(U, b["id"]) is None
    assert await db.set_format_thumb(U, a["id"], None)
    assert (await db.get_format(U, a["id"]))["has_thumb"] is False


async def test_nobody_can_touch_another_users_format(db):
    fmt = await db.create_format(U, "Mine")
    assert await db.get_format(V, fmt["id"]) is None
    assert await db.update_format(V, fmt["id"], template="x") is None
    assert await db.set_format_thumb(V, fmt["id"], b"x") is False
    assert await db.get_format_thumb(V, fmt["id"]) is None
    assert await db.delete_format(V, fmt["id"]) is False
    assert (await db.get_format(U, fmt["id"]))["name"] == "Mine"


async def test_delete_and_missing_ids(db):
    fmt = await db.create_format(U, "Gone")
    assert await db.delete_format(U, fmt["id"]) is True
    assert await db.delete_format(U, fmt["id"]) is False
    assert await db.get_format(U, fmt["id"]) is None and await db.update_format(U, fmt["id"], template="x") is None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def test_validate_name():
    assert validate_name("  One   Piece  ") == (True, "One Piece")
    assert validate_name("")[0] is False and validate_name("   ")[0] is False and validate_name(None)[0] is False
    ok, msg = validate_name("x" * (NAME_MAX + 1))
    assert not ok and str(NAME_MAX) in msg
    assert validate_name("x" * NAME_MAX)[0] is True
    assert validate_name("one\npiece") == (True, "one piece")


def fmt_with(**kw):
    base = {"id": 1, "name": "F", "kind": "manual", "has_thumb": False, "template": "T {title}", "caption": DEFAULT_FORMAT["caption"],
            "meta": dict(DEFAULT_FORMAT["meta"]), "keep_audio": [], "keep_subtitle": []}
    base.update(kw)
    return base


def test_apply_format_lays_the_format_over_the_settings_without_mutating_them():
    settings = dict(DEFAULT_SETTINGS, mode="caption", caption="<i>{original}</i>", file_type="document", remove_words=["x"],
                    target_channel=-100123, metadata_on=False, keep_audio=["french"])
    settings["meta"] = dict(DEFAULT_SETTINGS["meta"], title="DEFAULT TITLE")
    fmt = fmt_with(template="One Piece - E{episode}", meta=dict(DEFAULT_FORMAT["meta"], author="@Me"), keep_subtitle=["english"])
    out = apply_format(settings, fmt)
    assert out["template"] == "One Piece - E{episode}"
    assert out["meta"]["author"] == "@Me" and out["meta"]["title"] == ""  # the format's own metadata, not the default metadata
    assert out["keep_audio"] == [] and out["keep_subtitle"] == ["english"]
    assert out["metadata_on"] is True
    assert out["caption"] == "<b>{filename}</b>"  # the format's own, not the user's <i>{original}</i>
    # everything else is still the user's
    assert (out["mode"], out["file_type"], out["remove_words"], out["target_channel"]) == ("caption", "document", ["x"], -100123)
    assert "template" not in settings and settings["meta"]["title"] == "DEFAULT TITLE"
    assert settings["caption"] == "<i>{original}</i>"


def test_a_format_brings_its_own_caption_and_never_borrows_the_users():
    mine = dict(DEFAULT_SETTINGS, caption="<i>{original}</i> by me")
    assert apply_format(mine, fmt_with(caption="<b>{title}</b> · {size}"))["caption"] == "<b>{title}</b> · {size}"
    assert apply_format(mine, fmt_with())["caption"] == "<b>{filename}</b>"  # none of its own: the new file name in bold
    odd = fmt_with()
    del odd["caption"]  # a snapshot without the field
    assert apply_format(mine, odd)["caption"] == "<b>{filename}</b>"
    assert apply_format(mine, fmt_with(caption=""))["caption"] == "<b>{filename}</b>"
    assert mine["caption"] == "<i>{original}</i> by me"


def test_a_format_without_metadata_leaves_the_file_alone():
    assert not has_metadata(fmt_with())
    settings = dict(DEFAULT_SETTINGS, metadata_on=True)  # default metadata on, but the format sets nothing
    assert apply_format(settings, fmt_with())["metadata_on"] is False
    assert has_metadata(fmt_with(keep_audio=["1"])) and has_metadata(fmt_with(meta=dict(DEFAULT_FORMAT["meta"], video="v")))


# ---------------------------------------------------------------------------
# editing a format through the shared validation
# ---------------------------------------------------------------------------


async def test_settings_ops_edit_a_format_with_the_same_rules_as_the_users_own_settings(db):
    fmt = await db.create_format(U, "One Piece")
    store = FormatSettings(db, fmt["id"])

    ok, msg = await apply_setting(store, U, "template", "One Piece - E{episode}")
    assert ok and "Template saved" in msg and "One Piece - E" in msg
    ok, msg = await apply_setting(store, U, "mt_title", "One Piece")
    assert ok and "Metadata is Off" not in msg
    ok, _ = await apply_setting(store, U, "mt_audio", "{lang} Audio")
    assert ok
    ok, msg = await apply_setting(store, U, "keep_audio", "Japanese English")
    assert ok and "turned it On" not in msg
    ok, _ = await apply_setting(store, U, "keep_subtitle", "2 1")
    assert ok

    got = await db.get_format(U, fmt["id"])
    assert got["template"] == "One Piece - E{episode}"
    assert got["meta"]["title"] == "One Piece" and got["meta"]["audio"] == "{lang} Audio"
    assert got["keep_audio"] == ["japanese", "english"] and got["keep_subtitle"] == ["2", "1"]
    # the user's own settings were not touched
    assert await db.get_settings(U) == DEFAULT_SETTINGS

    ok, msg = await apply_setting(store, U, "keep_audio", "off")
    assert ok and "cleared" in msg and (await db.get_format(U, fmt["id"]))["keep_audio"] == []
    ok, msg = await apply_setting(store, U, "mt_title", "off")
    assert ok and (await db.get_format(U, fmt["id"]))["meta"]["title"] == ""
    ok, msg = await apply_setting(store, U, "keep_audio", "bad!word")
    assert not ok and "❌" in msg


async def test_editing_a_deleted_format_raises_lookup_error(db):
    fmt = await db.create_format(U, "Gone")
    store = FormatSettings(db, fmt["id"])
    await db.delete_format(U, fmt["id"])
    with pytest.raises(LookupError):
        await apply_setting(store, U, "template", "x {title}")
    with pytest.raises(LookupError):
        await store.get_settings(U)


# ---------------------------------------------------------------------------
# a format's own caption
# ---------------------------------------------------------------------------


async def test_a_format_starts_with_the_file_name_in_bold_and_keeps_a_caption_of_its_own(db):
    fmt = await db.create_format(U, "One Piece")
    assert fmt["caption"] == "<b>{filename}</b>" == DEFAULT_CAPTION
    out = await db.update_format(U, fmt["id"], caption="<i>{filename}</i> · {size}")
    assert out["caption"] == "<i>{filename}</i> · {size}"
    await db.update_format(U, fmt["id"], template="X {title}", meta={"title": "T"})  # other edits leave it alone
    other = Database(db.url)
    assert (await other.get_format(U, fmt["id"]))["caption"] == "<i>{filename}</i> · {size}"
    await other.close()
    assert (await db.list_formats(U))[0]["caption"] == "<i>{filename}</i> · {size}"
    assert await db.get_settings(U) == DEFAULT_SETTINGS  # formats never touch the user's own settings


async def test_formats_saved_before_captions_existed_get_the_default_one(db):
    import json

    from bot.db import Format

    async with db._sessions() as session:
        session.add(Format(user_id=U, name="Old", data=json.dumps({"template": "Old - {title}", "keep_audio": ["english"]})))
        session.add(Format(user_id=U, name="Broken", data=json.dumps({"caption": 7})))
        session.add(Format(user_id=U, name="Blank", data=json.dumps({"caption": "   "})))
        await session.commit()
    old, broken, blank = await db.list_formats(U)
    assert old["template"] == "Old - {title}" and old["keep_audio"] == ["english"] and old["caption"] == "<b>{filename}</b>"
    assert broken["caption"] == "<b>{filename}</b>" and blank["caption"] == "<b>{filename}</b>"


async def test_a_copy_of_a_format_has_its_caption(db):
    fmt = await db.create_format(U, "One Piece")
    await db.update_format(U, fmt["id"], caption="<code>{filename}</code>")
    copy = await db.copy_format(V, U, fmt["id"])
    assert copy["caption"] == "<code>{filename}</code>"
    await db.update_format(V, copy["id"], caption="<b>{title}</b>")
    assert (await db.get_format(U, fmt["id"]))["caption"] == "<code>{filename}</code>"


async def test_settings_ops_edit_a_formats_caption_with_the_checks_of_the_auto_format_caption(db):
    fmt = await db.create_format(U, "One Piece")
    store = FormatSettings(db, fmt["id"])
    ok, msg = await apply_setting(store, U, "caption", "<b>{filename}</b> · {size} · {duration}")
    assert ok and "Caption template saved" in msg and "Unknown" not in msg
    assert (await db.get_format(U, fmt["id"]))["caption"] == "<b>{filename}</b> · {size} · {duration}"
    ok, msg = await apply_setting(store, U, "caption", "<i>{nonsense}</i>")
    assert ok and "Unknown variables" in msg and "{nonsense}" in msg  # kept as plain text, as for the auto format's caption
    ok, msg = await apply_setting(store, U, "caption", "x" * 801)
    assert not ok and "too long" in msg and (await db.get_format(U, fmt["id"]))["caption"] == "<i>{nonsense}</i>"
    ok, msg = await apply_setting(store, U, "caption", "")
    assert not ok and "can't be empty" in msg
    for word in ("reset", "off", "none", "clear"):
        await apply_setting(store, U, "caption", "<u>{filename}</u>")
        ok, _ = await apply_setting(store, U, "caption", word)
        assert ok and (await db.get_format(U, fmt["id"]))["caption"] == "<b>{filename}</b>", word  # the file name in bold again
    assert (await store.get_settings(U))["caption"] == "<b>{filename}</b>"
    assert await db.get_settings(U) == DEFAULT_SETTINGS


# ---------------------------------------------------------------------------
# two kinds: 12 auto formats and 12 manual ones
# ---------------------------------------------------------------------------


async def test_a_format_is_manual_unless_it_is_made_as_an_auto_one(db):
    assert (await db.create_format(U, "A"))["kind"] == "manual"
    assert (await db.create_format(U, "B", kind="manual"))["kind"] == "manual"
    auto = await db.create_format(U, "C", kind="auto")
    assert auto["kind"] == "auto" and (await db.get_format(U, auto["id"]))["kind"] == "auto"
    with pytest.raises(KeyError):
        await db.create_format(U, "D", kind="both")
    assert other_kind("auto") == "manual" and other_kind("manual") == "auto"


async def test_each_kind_lists_and_counts_its_own_formats(db):
    a1, m1, a2 = await db.create_format(U, "A1", kind="auto"), await db.create_format(U, "M1"), await db.create_format(U, "A2", kind="auto")
    await db.create_format(V, "Theirs", kind="auto")
    assert [f["id"] for f in await db.list_formats(U)] == [a1["id"], m1["id"], a2["id"]]  # no kind: all of them, oldest first
    assert [f["id"] for f in await db.list_formats(U, "auto")] == [a1["id"], a2["id"]]
    assert [f["id"] for f in await db.list_formats(U, "manual")] == [m1["id"]]
    assert (await db.count_formats(U), await db.count_formats(U, "auto"), await db.count_formats(U, "manual")) == (3, 2, 1)
    assert await db.count_formats(V, "manual") == 0 and await db.count_formats(V, "auto") == 1


async def test_a_user_can_save_twelve_formats_of_each_kind_and_no_more(db):
    for kind in ("auto", "manual"):
        for i in range(MAX_FORMATS):
            await db.create_format(U, f"{kind} {i}", kind=kind)
        with pytest.raises(FormatError) as exc:
            await db.create_format(U, "One too many", kind=kind)
        assert exc.value.code == "limit"
    assert await db.count_formats(U) == 2 * MAX_FORMATS
    first = (await db.list_formats(U, "auto"))[0]
    assert await db.delete_format(U, first["id"])
    await db.create_format(U, "One too many", kind="auto")  # a slot of that kind is free again
    with pytest.raises(FormatError):
        await db.create_format(U, "Still too many", kind="manual")  # the manual ones are still full


async def test_a_name_only_has_to_be_free_within_its_kind(db):
    auto = await db.create_format(U, "One Piece", kind="auto")
    manual = await db.create_format(U, "ONE PIECE")  # the same name, the other kind
    with pytest.raises(FormatError) as exc:
        await db.create_format(U, "one piece", kind="auto")
    assert exc.value.code == "taken"
    other = await db.create_format(U, "Dragon Ball", kind="auto")
    with pytest.raises(FormatError):
        await db.update_format(U, other["id"], name="one piece")  # taken by an auto format
    await db.update_format(U, other["id"], name="Dragon Ball Z")
    await db.update_format(U, manual["id"], name="Dragon Ball Z")  # but free among the manual ones
    assert (await db.get_format(U, auto["id"]))["name"] == "One Piece"


async def test_formats_stored_before_there_were_kinds_are_manual(db):
    import json

    from bot.db import Format

    async with db._sessions() as session:
        session.add(Format(user_id=U, name="Old", data=json.dumps({"template": "Old - {title}"})))
        session.add(Format(user_id=U, name="Odd", data=json.dumps({"kind": "sideways"})))
        session.add(Format(user_id=U, name="Auto", data=json.dumps({"kind": "auto"})))
        await session.commit()
    assert [(f["name"], f["kind"]) for f in await db.list_formats(U)] == [("Old", "manual"), ("Odd", "manual"), ("Auto", "auto")]
    old = (await db.list_formats(U, "manual"))[0]
    out = await db.update_format(U, old["id"], template="New - {title}")  # editing a format keeps its kind
    assert out["kind"] == "manual" and (await db.get_format(U, old["id"]))["template"] == "New - {title}"
    auto = (await db.list_formats(U, "auto"))[0]
    assert (await db.update_format(U, auto["id"], meta={"title": "T"}))["kind"] == "auto"


async def test_copy_format_to_the_other_kind(db):
    src = await db.create_format(U, "One Piece", "E{episode}", kind="auto")
    await db.update_format(U, src["id"], meta={"title": "OP"}, keep_audio=["japanese"], caption="<i>{filename}</i>")
    await db.set_format_thumb(U, src["id"], b"\xff\xd8thumb")

    same = await db.copy_format(V, U, src["id"])  # sharing: the copy keeps the kind
    assert same["kind"] == "auto"
    mine = await db.copy_format(U, U, src["id"], kind="manual")
    assert mine["kind"] == "manual" and mine["name"] == "One Piece" and mine["id"] != src["id"]  # free in the manual names
    assert mine["template"] == "E{episode}" and mine["meta"]["title"] == "OP" and mine["keep_audio"] == ["japanese"]
    assert mine["caption"] == "<i>{filename}</i>" and mine["has_thumb"] and await db.get_format_thumb(U, mine["id"]) == b"\xff\xd8thumb"
    assert [(await db.copy_format(U, U, src["id"], kind="manual"))["name"] for _ in range(2)] == ["One Piece (2)", "One Piece (3)"]
    back = await db.copy_format(U, U, mine["id"], kind="auto")  # and the other way round: "One Piece" is taken among the auto ones
    assert back["kind"] == "auto" and back["name"] == "One Piece (2)"
    await db.update_format(U, mine["id"], template="Changed")  # the copy is independent
    assert (await db.get_format(U, src["id"]))["template"] == "E{episode}"
    with pytest.raises(KeyError):
        await db.copy_format(U, U, src["id"], kind="both")


async def test_copying_to_the_other_kind_stops_at_the_limit_of_that_kind(db):
    src = await db.create_format(U, "One Piece", kind="auto")
    for i in range(MAX_FORMATS):
        await db.create_format(U, f"M{i}")
    with pytest.raises(FormatError) as exc:
        await db.copy_format(U, U, src["id"], kind="manual")
    assert exc.value.code == "limit" and await db.count_formats(U, "manual") == MAX_FORMATS
    assert (await db.copy_format(U, U, src["id"]))["kind"] == "auto"  # the auto ones have room


# ---------------------------------------------------------------------------
# the auto format in use
# ---------------------------------------------------------------------------


async def test_without_auto_formats_there_is_none_in_use(db):
    assert await db.get_auto_format(U) is None
    await db.create_format(U, "Manual only")  # a manual format never renames files by itself
    assert await db.get_auto_format(U) is None and (await db.get_settings(U))["auto_format"] == 0


async def test_the_first_auto_format_is_in_use_and_it_is_remembered(db):
    first = await db.create_format(U, "First", kind="auto")
    await db.create_format(U, "Second", kind="auto")
    assert (await db.get_auto_format(U))["id"] == first["id"]
    assert (await db.get_settings(U))["auto_format"] == first["id"]
    assert (await db.get_auto_format(V)) is None  # per user


async def test_selecting_an_auto_format(db):
    a, b = await db.create_format(U, "A", kind="auto"), await db.create_format(U, "B", kind="auto")
    manual = await db.create_format(U, "M")
    theirs = await db.create_format(V, "Theirs", kind="auto")
    assert (await db.select_auto_format(U, b["id"]))["id"] == b["id"]
    assert (await db.get_auto_format(U))["id"] == b["id"] and (await db.get_settings(U))["auto_format"] == b["id"]
    for fid in (manual["id"], theirs["id"], 99999):  # not an auto format of theirs
        assert await db.select_auto_format(U, fid) is None
    assert (await db.get_auto_format(U))["id"] == b["id"]  # unchanged by the refusals
    await db.select_auto_format(U, a["id"])
    assert (await db.get_auto_format(U))["id"] == a["id"]


async def test_deleting_the_auto_format_in_use_hands_over_to_another(db):
    a = await db.create_format(U, "A", kind="auto")
    b = await db.create_format(U, "B", kind="auto")
    c = await db.create_format(U, "C", kind="auto")
    await db.select_auto_format(U, b["id"])
    await db.delete_format(U, b["id"])
    assert (await db.get_auto_format(U))["id"] == a["id"]  # the oldest left takes over
    assert (await db.get_settings(U))["auto_format"] == a["id"]
    await db.delete_format(U, c["id"])  # deleting one that is not in use changes nothing
    assert (await db.get_auto_format(U))["id"] == a["id"]
    await db.delete_format(U, a["id"])
    assert await db.get_auto_format(U) is None and (await db.get_settings(U))["auto_format"] == 0


# ---------------------------------------------------------------------------
# a format over the user's settings, and the default metadata
# ---------------------------------------------------------------------------


def with_default_metadata(**over):
    settings = dict(DEFAULT_SETTINGS, **over)
    settings["meta"] = dict(DEFAULT_SETTINGS["meta"], title="DEFAULT TITLE", audio="{lang} Audio")
    return settings


def test_an_auto_format_without_metadata_leaves_the_default_metadata_in_charge():
    mine = with_default_metadata(metadata_on=True, keep_audio=["english"])
    out = apply_format(mine, fmt_with(kind="auto"))
    assert out["template"] == "T {title}" and out["metadata_on"] is True  # its switch decides, as it always did
    assert out["meta"]["title"] == "DEFAULT TITLE" and out["keep_audio"] == ["english"]
    off = apply_format(with_default_metadata(metadata_on=False), fmt_with(kind="auto"))
    assert off["metadata_on"] is False and off["meta"]["title"] == "DEFAULT TITLE"  # off: nothing is written


def test_an_auto_format_with_metadata_of_its_own_ignores_the_default_metadata():
    fmt = fmt_with(kind="auto", meta=dict(DEFAULT_FORMAT["meta"], author="@Me"))
    out = apply_format(with_default_metadata(metadata_on=False), fmt)
    assert out["metadata_on"] is True and out["meta"]["author"] == "@Me" and out["meta"]["title"] == "" and out["keep_audio"] == []
    assert apply_format(with_default_metadata(metadata_on=True), fmt)["meta"]["title"] == ""


def test_a_manual_format_never_uses_the_default_metadata():
    out = apply_format(with_default_metadata(metadata_on=True, keep_audio=["english"]), fmt_with(kind="manual"))
    assert out["metadata_on"] is False and out["meta"]["title"] == "" and out["keep_audio"] == []


def test_no_auto_format_means_the_built_in_defaults_and_the_default_metadata():
    built_in = default_auto_format()
    assert built_in["kind"] == "auto" and built_in["template"] == DEFAULT_TEMPLATE and built_in["caption"] == DEFAULT_CAPTION
    assert not built_in["has_thumb"] and not has_metadata(built_in)
    mine = with_default_metadata(metadata_on=True, template="ignored")
    out = apply_format(mine, None)
    assert out["template"] == DEFAULT_TEMPLATE and out["caption"] == DEFAULT_CAPTION
    assert out["metadata_on"] is True and out["meta"]["title"] == "DEFAULT TITLE"
    assert built_in is not default_auto_format() and default_auto_format()["meta"] == DEFAULT_FORMAT["meta"]  # a fresh copy each time


def test_default_metadata_is_what_the_user_set_in_the_metadata_menu():
    assert not has_default_metadata(DEFAULT_SETTINGS)
    assert not has_default_metadata(dict(DEFAULT_SETTINGS, metadata_on=True))  # the switch alone is not metadata
    assert has_default_metadata(with_default_metadata())
    assert has_default_metadata(dict(DEFAULT_SETTINGS, keep_audio=["1"])) and has_default_metadata(dict(DEFAULT_SETTINGS, keep_subtitle=["english"]))
    changes = default_metadata_changes(with_default_metadata(keep_audio=["2", "1"], keep_subtitle=["english"]))
    assert set(changes) == {"meta", "keep_audio", "keep_subtitle"}
    assert changes["meta"]["title"] == "DEFAULT TITLE" and changes["keep_audio"] == ["2", "1"] and changes["keep_subtitle"] == ["english"]
    source = with_default_metadata(keep_audio=["2"])
    changes = default_metadata_changes(source)
    changes["keep_audio"].append("3")
    changes["meta"]["title"] = "other"
    assert source["keep_audio"] == ["2"] and source["meta"]["title"] == "DEFAULT TITLE"  # copies, not the user's own lists


async def test_default_metadata_can_be_copied_into_a_format_of_either_kind(db):
    settings = with_default_metadata(keep_audio=["japanese"], keep_subtitle=["english"])
    for kind in ("auto", "manual"):
        fmt = await db.create_format(U, f"{kind} one", kind=kind)
        await db.update_format(U, fmt["id"], meta={"video": "OLD"}, keep_audio=["french", "2"])
        got = await db.update_format(U, fmt["id"], **default_metadata_changes(settings))
        assert got["meta"]["title"] == "DEFAULT TITLE" and got["meta"]["audio"] == "{lang} Audio"
        assert got["keep_audio"] == ["japanese"] and got["keep_subtitle"] == ["english"] and got["kind"] == kind
        assert got["meta"]["video"] == "" and got["meta"]["author"] == ""  # all six fields are sent: it replaces what the format had
