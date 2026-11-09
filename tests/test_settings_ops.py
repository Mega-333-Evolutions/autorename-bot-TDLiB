import pytest

from bot.db import DEFAULT_CAPTION, Database
from bot.formats import FormatSettings
from bot.parser import DEFAULT_TEMPLATE
from bot.settings_ops import META_COMMANDS, META_KINDS, apply_setting, parse_channel, preview_template

UID = 7


@pytest.fixture
async def db(tmp_path):
    d = Database(f"sqlite+aiosqlite:///{tmp_path}/s.db")
    await d.init()
    yield d
    await d.close()


async def get(db):
    return await db.get_settings(UID)


async def auto(db):
    """The auto format in use: the template and the caption are saved there, not in the user's settings."""
    return await db.get_auto_format(UID)


@pytest.fixture
async def store(db):
    """Where the template and the caption are saved: an auto format (they live in formats, not in the user's settings)."""
    fmt = await db.create_format(UID, "Default", kind="auto")
    return FormatSettings(db, fmt["id"])


async def test_template_saved_with_preview(db, store):
    ok, msg = await apply_setting(store, UID, "template", "[S{season}E{episode}] {title} [{quality}]")
    assert ok and "Template saved" in msg and "Preview" in msg
    assert (await auto(db))["template"] == "[S{season}E{episode}] {title} [{quality}]"
    assert "template" not in await get(db)  # the user's own settings don't hold one


async def test_template_validation_and_reset(db, store):
    assert (await apply_setting(store, UID, "template", "   "))[0] is False
    ok, msg = await apply_setting(store, UID, "template", "x" * 201)
    assert not ok and "too long" in msg
    assert (await auto(db))["template"] == DEFAULT_TEMPLATE  # nothing was saved
    await apply_setting(store, UID, "template", "{title}")
    assert (await auto(db))["template"] == "{title}"
    ok, _ = await apply_setting(store, UID, "template", "reset")
    assert ok and (await auto(db))["template"] == DEFAULT_TEMPLATE


async def test_template_unknown_variables_are_reported_but_saved(db, store):
    ok, msg = await apply_setting(store, UID, "template", "{title} {nope}")
    assert ok and "{nope}" in msg and "Unknown variables" in msg
    assert (await auto(db))["template"] == "{title} {nope}"


async def test_the_padded_episode_variables_are_known_ones(db, store):
    ok, msg = await apply_setting(store, UID, "template", "{title} E{1episode} {2episode} {3episode}")
    assert ok and "Unknown" not in msg
    assert "Tokyo Revengers E05 005 0005" in msg  # the preview shows the padding
    ok, msg = await apply_setting(store, UID, "caption", "Episode {2episode} of {title}")
    assert ok and "Unknown" not in msg


async def test_caption_set_validate_reset(db, store):
    ok, msg = await apply_setting(store, UID, "caption", "<b>{filename}</b> · {size}")
    assert ok and (await auto(db))["caption"] == "<b>{filename}</b> · {size}"
    assert "Unknown" not in msg
    assert (await apply_setting(store, UID, "caption", "x" * 801))[0] is False
    assert (await apply_setting(store, UID, "caption", ""))[0] is False
    ok, msg = await apply_setting(store, UID, "caption", "{bogus}")
    assert ok and "{bogus}" in msg
    ok, _ = await apply_setting(store, UID, "caption", "reset")
    assert ok and (await auto(db))["caption"] == DEFAULT_CAPTION == "<b>{filename}</b>"


async def test_remove_words(db):
    ok, msg = await apply_setting(db, UID, "remove", "@Chan, [Group]\n www.site.com ,, ")
    assert ok
    assert (await get(db))["remove_words"] == ["@Chan", "[Group]", "www.site.com"]
    assert (await apply_setting(db, UID, "remove", " , ,"))[0] is False
    assert (await apply_setting(db, UID, "remove", ", ".join(f"w{i}" for i in range(51))))[0] is False
    assert (await apply_setting(db, UID, "remove", "x" * 61))[0] is False
    ok, _ = await apply_setting(db, UID, "remove", "off")
    assert ok and (await get(db))["remove_words"] == []


@pytest.mark.parametrize("kind", sorted(META_KINDS))
async def test_every_metadata_field_can_be_set_and_cleared(db, kind):
    field = META_KINDS[kind]
    ok, msg = await apply_setting(db, UID, kind, f"{field} value")
    assert ok and (await get(db))["meta"][field] == f"{field} value"
    assert "Metadata is Off" in msg  # reminder that nothing applies until it is switched on
    await db.update_settings(UID, metadata_on=True)
    ok, msg = await apply_setting(db, UID, kind, "another")
    assert ok and "Metadata is Off" not in msg
    ok, _ = await apply_setting(db, UID, kind, "off")
    assert ok and (await get(db))["meta"][field] == ""


async def test_metadata_value_validation(db):
    assert (await apply_setting(db, UID, "mt_title", ""))[0] is False
    ok, msg = await apply_setting(db, UID, "mt_title", "x" * 201)
    assert not ok and "too long" in msg
    assert (await get(db))["meta"]["title"] == ""


async def test_meta_commands_map_to_known_kinds():
    assert set(META_COMMANDS.values()) == set(META_KINDS)


async def test_metadata_values_are_html_escaped_in_the_reply(db):
    ok, msg = await apply_setting(db, UID, "mt_title", "<script>")
    assert ok and "<script>" not in msg and "&lt;script&gt;" in msg


@pytest.mark.parametrize("kind", ["keep_audio", "keep_subtitle"])
async def test_keep_tracks_by_language_names_keeps_order_and_enables_metadata(db, kind):
    assert (await get(db))["metadata_on"] is False
    ok, msg = await apply_setting(db, UID, kind, "English, Japanese")
    assert ok
    s = await get(db)
    assert s[kind] == ["english", "japanese"]
    assert s["metadata_on"] is True and "turned it On" in msg
    ok, msg = await apply_setting(db, UID, kind, "2 1")
    assert ok and (await get(db))[kind] == ["2", "1"] and "turned it On" not in msg
    ok, msg = await apply_setting(db, UID, kind, "off")
    assert ok and (await get(db))[kind] == [] and "all tracks are kept" in msg


async def test_keep_tracks_rejects_bad_input_without_changing_anything(db):
    await apply_setting(db, UID, "keep_audio", "english")
    for bad in ("0", "1 english 0", "***", "   ", "a" * 30, "1 2 3 4 5 6 7 8 9 10 11 12 13"):
        ok, msg = await apply_setting(db, UID, "keep_audio", bad)
        assert not ok and msg.startswith("❌"), bad
        assert (await get(db))["keep_audio"] == ["english"]


async def test_keep_tracks_duplicates_are_collapsed(db):
    await apply_setting(db, UID, "keep_audio", "english eng English 2 2")
    assert (await get(db))["keep_audio"] == ["english", "eng", "2"]


async def test_unknown_kind_raises(db):
    with pytest.raises(ValueError):
        await apply_setting(db, UID, "nonsense", "x")


def test_parse_channel():
    assert parse_channel("-1001234567890") == (True, -1001234567890, "")
    assert parse_channel("12345") == (True, 12345, "")
    assert parse_channel("off") == (True, None, "")
    ok, cid, err = parse_channel("@mychannel")
    assert not ok and cid is None and "numeric" in err
    assert parse_channel("12")[0] is False


def test_preview_template_renders_sample():
    out = preview_template("{title} [{quality}]")
    assert "Preview" in out and "<code>" in out and "[" in out
