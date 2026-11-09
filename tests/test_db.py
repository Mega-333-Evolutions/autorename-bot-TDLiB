import json

import pytest

from bot.db import AUTO_FORMATS_MIGRATION, DEFAULT_CAPTION, DEFAULT_FORMAT, DEFAULT_SETTINGS, Database, Format, User, normalize_database_url
from bot.parser import DEFAULT_TEMPLATE


@pytest.fixture
async def db(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    await database.init()
    yield database
    await database.close()


def test_normalize_postgres_urls():
    url, args = normalize_database_url("postgres://u:p@host/db?sslmode=require&channel_binding=require")
    assert url == "postgresql+asyncpg://u:p@host/db"
    assert args == {"ssl": "require"}
    url, args = normalize_database_url("postgresql://u:p@host/db")
    assert url == "postgresql+asyncpg://u:p@host/db" and args == {}
    url, args = normalize_database_url("sqlite+aiosqlite:///x.db")
    assert url == "sqlite+aiosqlite:///x.db" and args == {}


async def test_defaults_and_update_roundtrip(db):
    s = await db.get_settings(1)
    assert s == DEFAULT_SETTINGS
    s["mode"] = "tampered"  # returned copy must not leak into the cache
    assert (await db.get_settings(1))["mode"] == "filename"

    await db.update_settings(1, mode="caption", keep_audio=["2", "1"], meta={"title": "Hello"})
    await db.update_settings(1, meta={"author": "me"})
    # a fresh Database instance proves the values were persisted, not just cached
    other = Database(db.url)
    s2 = await other.get_settings(1)
    assert s2["mode"] == "caption"
    assert s2["keep_audio"] == ["2", "1"]
    assert s2["meta"]["title"] == "Hello" and s2["meta"]["author"] == "me" and s2["meta"]["artist"] == ""
    await other.close()


async def test_unknown_setting_rejected(db):
    with pytest.raises(KeyError):
        await db.update_settings(1, nope=1)


async def test_stats(db):
    await db.record_job(5, 1000)
    await db.record_job(5, 24)
    await db.get_settings(6)
    assert await db.count_users() == 2
    assert await db.totals() == (2, 1024)


async def test_admins_and_kv(db):
    assert await db.add_admin(10, 1) is True
    assert await db.add_admin(10, 1) is False
    assert await db.add_admin(11, 1) is True
    assert sorted(await db.list_admins()) == [10, 11]
    assert await db.del_admin(10) is True
    assert await db.del_admin(10) is False
    assert await db.list_admins() == [11]

    assert await db.kv_get("session") is None
    await db.kv_set("session", "abc")
    await db.kv_set("session", "def")
    assert await db.kv_get("session") == "def"
    await db.kv_delete("session")
    assert await db.kv_get("session") is None


async def test_a_foreign_table_called_formats_in_a_shared_database_is_left_alone(tmp_path):
    """The Postgres can be shared with another app whose "formats" table has other columns (that broke /rename once)."""
    import sqlite3

    path = tmp_path / "shared.db"
    other_app = sqlite3.connect(path)
    other_app.execute("CREATE TABLE formats (id INTEGER PRIMARY KEY, name TEXT, body TEXT)")
    other_app.execute("INSERT INTO formats (name, body) VALUES ('their format', 'their body')")
    other_app.commit()
    other_app.close()

    database = Database(f"sqlite+aiosqlite:///{path}")
    await database.init()
    try:
        created = await database.create_format(1, "One Piece", "{title} E{episode}")
        assert [f["name"] for f in await database.list_formats(1)] == ["One Piece"]
        assert (await database.get_format(1, created["id"]))["template"] == "{title} E{episode}"
        assert await database.share_token(1, created["id"])  # the sharing table is ours too
        assert await database.delete_format(1, created["id"]) is True
    finally:
        await database.close()

    other_app = sqlite3.connect(path)
    assert other_app.execute("SELECT name, body FROM formats").fetchall() == [("their format", "their body")]
    assert [r[1] for r in other_app.execute("PRAGMA table_info(formats)")] == ["id", "name", "body"]
    other_app.close()


async def test_first_use_by_many_concurrent_handlers_does_not_collide(db):
    """A forwarded batch is handled by several tasks at once; all of them look the new user up together."""
    import asyncio

    results = await asyncio.gather(*(db.get_settings(77) for _ in range(8)))
    assert all(r == DEFAULT_SETTINGS for r in results)
    # the same goes for the other writers that create the row on first use
    await asyncio.gather(*(db.record_job(78, 10) for _ in range(5)), *(db.update_settings(79, mode="caption") for _ in range(5)))
    assert await db.count_users() == 3


# ---------------------------------------------------------------------------
# bold captions
# ---------------------------------------------------------------------------


async def test_captions_are_bold_by_default(db):
    assert DEFAULT_CAPTION == DEFAULT_FORMAT["caption"] == "<b>{filename}</b>"
    assert (await db.create_format(1, "New", kind="auto"))["caption"] == "<b>{filename}</b>"
    assert "caption" not in DEFAULT_SETTINGS and "template" not in DEFAULT_SETTINGS  # they live in the formats now


async def test_the_plain_default_caption_of_earlier_versions_becomes_bold_once(db):
    import json

    from bot.db import AUTO_FORMATS_MIGRATION, BOLD_CAPTION_MIGRATION, User

    # what an older version left behind: no markers yet, and the settings stored in full
    await db.kv_delete(BOLD_CAPTION_MIGRATION)
    await db.kv_delete(AUTO_FORMATS_MIGRATION)
    stored = {
        1: {"caption": "{filename}", "mode": "caption", "template": "T {title}"},  # still the old default
        2: {"caption": "{filename} · {size}"},  # their own caption
        3: {"mode": "caption"},  # no caption stored: gets the default
        4: "not json",
        5: [1, 2],
    }
    async with db._sessions() as session:
        for uid, value in stored.items():
            session.add(User(id=uid, settings=value if isinstance(value, str) else json.dumps(value)))
        await session.commit()

    await db.init()  # runs the migrations
    fresh = Database(db.url)  # a new instance has no cache
    try:
        # the old default became the bold one, and the old template and caption became an auto format
        s1 = await fresh.get_settings(1)
        assert s1["mode"] == "caption"
        (one,) = await fresh.list_formats(1, "auto")
        assert one["name"] == "Default" and one["caption"] == "<b>{filename}</b>" and one["template"] == "T {title}"
        assert s1["auto_format"] == one["id"]
        (two,) = await fresh.list_formats(2, "auto")
        assert two["caption"] == "{filename} · {size}" and two["template"] == DEFAULT_FORMAT["template"]
        assert await fresh.list_formats(3) == [] and (await fresh.get_settings(3))["mode"] == "caption"  # nothing of theirs to keep
        assert await fresh.get_settings(4) == DEFAULT_SETTINGS and await fresh.get_settings(5) == DEFAULT_SETTINGS
        await fresh.update_format(1, one["id"], caption="{filename}")  # going back to a plain caption afterwards...
    finally:
        await fresh.close()
    await db.init()
    again = Database(db.url)
    try:
        assert (await again.get_format(1, one["id"]))["caption"] == "{filename}"  # ...sticks, also across restarts
        assert len(await again.list_formats(1)) == 1  # and the migrations ran only once
    finally:
        await again.close()


# ---------------------------------------------------------------------------
# sharing formats
# ---------------------------------------------------------------------------


async def test_share_token_is_stable_private_to_the_owner_and_dies_with_the_format(db):
    fmt = await db.create_format(1, "One Piece", "E{episode}")
    token = await db.share_token(1, fmt["id"])
    assert token and len(token) >= 16 and token == await db.share_token(1, fmt["id"])
    other = await db.create_format(1, "Dragon Ball")
    assert await db.share_token(1, other["id"]) != token
    assert await db.share_token(2, fmt["id"]) is None and await db.share_token(1, 999) is None

    owner, shared = await db.get_shared_format(token)
    assert owner == 1 and shared["name"] == "One Piece" and shared["template"] == "E{episode}"
    await db.update_format(1, fmt["id"], template="New {title}")
    assert (await db.get_shared_format(token))[1]["template"] == "New {title}"  # the format as it is now
    assert await db.get_shared_format("nope") is None

    assert await db.delete_format(1, fmt["id"]) is True
    assert await db.get_shared_format(token) is None
    await db.create_format(1, "Another")  # it may get the deleted format's id: the old link must not lead to it
    assert await db.get_shared_format(token) is None
    assert await db.get_shared_format(await db.share_token(1, other["id"])) is not None  # the other link is untouched


async def test_copy_format_copies_everything_and_finds_a_free_name(db):
    src = await db.create_format(1, "One Piece", "E{episode}")
    await db.update_format(1, src["id"], meta={"title": "OP"}, keep_audio=["japanese"], keep_subtitle=["2"])
    await db.set_format_thumb(1, src["id"], b"\xff\xd8thumb")

    mine = await db.copy_format(2, 1, src["id"])
    assert mine["name"] == "One Piece" and mine["template"] == "E{episode}" and mine["meta"]["title"] == "OP"
    assert mine["keep_audio"] == ["japanese"] and mine["keep_subtitle"] == ["2"] and mine["has_thumb"] is True
    assert mine["id"] != src["id"] and await db.get_format_thumb(2, mine["id"]) == b"\xff\xd8thumb"
    assert [(await db.copy_format(2, 1, src["id"]))["name"] for _ in range(2)] == ["One Piece (2)", "One Piece (3)"]

    await db.update_format(2, mine["id"], template="Changed")  # the copy is independent of the original
    assert (await db.get_format(1, src["id"]))["template"] == "E{episode}"
    assert await db.copy_format(2, 3, src["id"]) is None and await db.copy_format(2, 1, 999) is None  # not theirs / gone

    big = await db.create_format(1, "N" * 30)
    names = [(await db.copy_format(3, 1, big["id"], name_max=30))["name"] for _ in range(2)]
    assert names == ["N" * 30, "N" * 26 + " (2)"]  # the number fits inside the length limit


async def test_copy_format_stops_at_the_limit(db):
    from bot.db import MAX_FORMATS
    from bot.errors import FormatError

    src = await db.create_format(1, "One Piece")
    for i in range(MAX_FORMATS):
        await db.create_format(2, f"F{i}")
    with pytest.raises(FormatError) as raised:
        await db.copy_format(2, 1, src["id"])
    assert raised.value.code == "limit" and await db.count_formats(2) == MAX_FORMATS


# ---------------------------------------------------------------------------
# from the global template, caption and thumbnail to auto formats
# ---------------------------------------------------------------------------

META = {"title": "My Title", "author": "", "artist": "", "audio": "", "subtitle": "", "video": ""}


async def start_after_an_old_version(db, users, formats=()):
    """Store what a version without auto formats left behind (no marker yet), start the bot on it and return a fresh Database.

    ``users``: ``{id: (stored settings, old thumbnail)}``; ``formats``: ``[(user id, name, stored data, thumbnail)]`` - formats of
    that version had no kind.
    """
    await db.kv_delete(AUTO_FORMATS_MIGRATION)
    async with db._sessions() as session:
        for uid, (settings, thumb) in users.items():
            session.add(User(id=uid, settings=json.dumps(settings), thumb=thumb))
        for uid, name, data, thumb in formats:
            session.add(Format(user_id=uid, name=name, data=json.dumps(data), thumb=thumb))
        await session.commit()
    await db.init()
    return Database(db.url)  # a new instance has no cache


async def test_the_old_template_caption_and_thumbnail_become_the_auto_format_default(db):
    fresh = await start_after_an_old_version(
        db, {1: ({"template": "T {title}", "caption": "<i>{filename}</i>", "mode": "caption"}, b"\xff\xd8old")}
    )
    try:
        (auto,) = await fresh.list_formats(1)
        assert (auto["name"], auto["kind"]) == ("Default", "auto")
        assert auto["template"] == "T {title}" and auto["caption"] == "<i>{filename}</i>" and auto["has_thumb"]
        assert await fresh.get_format_thumb(1, auto["id"]) == b"\xff\xd8old"
        settings = await fresh.get_settings(1)
        assert settings["auto_format"] == auto["id"] and settings["mode"] == "caption"  # in use, the rest of the settings is untouched
        assert (await fresh.get_auto_format(1))["id"] == auto["id"]
        async with fresh._sessions() as session:  # and nothing is kept in two places
            row = await session.get(User, 1)
            assert row.thumb is None and {"template", "caption"}.isdisjoint(json.loads(row.settings))
    finally:
        await fresh.close()


async def test_the_old_metadata_stays_as_the_default_metadata_and_is_not_copied_into_the_auto_format(db):
    users = {
        1: ({"template": "A {title}", "metadata_on": True, "meta": META, "keep_audio": ["english"]}, None),
        2: ({"template": "B {title}", "metadata_on": False, "meta": META, "keep_audio": ["english"]}, None),
        3: ({"metadata_on": True, "meta": META}, None),  # nothing else of its own
    }
    fresh = await start_after_an_old_version(db, users)
    try:
        for uid, template in ((1, "A {title}"), (2, "B {title}")):
            (auto,) = await fresh.list_formats(uid)
            # no snapshot: it has none of its own, so the default metadata applies to it while it is On, edits included
            assert auto["template"] == template and not any(auto["meta"].values()) and auto["keep_audio"] == [] and auto["keep_subtitle"] == []
        assert await fresh.list_formats(3) == []  # nothing to keep: the built-in defaults are used
        for uid in (1, 2, 3):  # the metadata is where it always was
            settings = await fresh.get_settings(uid)
            assert settings["meta"]["title"] == "My Title" and settings["metadata_on"] is (uid in (1, 3))
        assert (await fresh.get_settings(2))["keep_audio"] == ["english"] and (await fresh.get_settings(1))["keep_audio"] == ["english"]
    finally:
        await fresh.close()


async def test_formats_made_before_the_kinds_are_manual_and_the_ones_without_a_thumbnail_keep_the_old_one(db):
    fresh = await start_after_an_old_version(
        db,
        {1: ({"template": "T"}, b"old-1"), 2: ({}, b"old-2"), 3: ({}, None)},
        formats=[
            (1, "Own thumb", {"template": "X"}, b"own"), (1, "Borrowing", {"template": "Y"}, None),
            (2, "Other", {}, None), (3, "Plain", {}, None),
        ],
    )
    try:
        own, borrowing = await fresh.list_formats(1, "manual")
        assert (own["name"], borrowing["name"]) == ("Own thumb", "Borrowing") and own["template"] == "X"
        assert await fresh.get_format_thumb(1, own["id"]) == b"own"  # it had one of its own
        assert await fresh.get_format_thumb(1, borrowing["id"]) == b"old-1"  # it used the old global one: it still does
        (auto,) = await fresh.list_formats(1, "auto")
        assert await fresh.get_format_thumb(1, auto["id"]) == b"old-1"

        (other,) = await fresh.list_formats(2, "manual")
        assert await fresh.get_format_thumb(2, other["id"]) == b"old-2"
        (their_auto,) = await fresh.list_formats(2, "auto")  # a thumbnail alone is something to keep
        assert their_auto["template"] == DEFAULT_TEMPLATE and their_auto["has_thumb"]

        (plain,) = await fresh.list_formats(3)
        assert plain["kind"] == "manual" and not plain["has_thumb"] and await fresh.get_auto_format(3) is None
    finally:
        await fresh.close()


async def test_someone_who_never_changed_anything_gets_no_auto_format(db):
    fresh = await start_after_an_old_version(db, {1: ({}, None), 2: ({"template": DEFAULT_TEMPLATE, "caption": DEFAULT_CAPTION}, None)})
    try:
        for uid in (1, 2):
            assert await fresh.list_formats(uid) == [] and (await fresh.get_settings(uid))["auto_format"] == 0
            assert await fresh.get_auto_format(uid) is None
    finally:
        await fresh.close()


async def test_the_migration_makes_each_users_auto_format_once(db):
    fresh = await start_after_an_old_version(db, {1: ({"template": "T"}, b"t")})
    await fresh.close()
    await db.kv_delete(AUTO_FORMATS_MIGRATION)  # as if the marker was lost: the user's own mark is what counts
    await db.init()
    again = Database(db.url)
    try:
        assert len(await again.list_formats(1)) == 1
        assert await again.kv_get(AUTO_FORMATS_MIGRATION) == "done"
    finally:
        await again.close()


async def test_settings_that_cannot_be_read_are_left_as_they_are(db):
    await db.kv_delete(AUTO_FORMATS_MIGRATION)
    async with db._sessions() as session:
        session.add(User(id=1, settings="not json", thumb=b"x"))
        session.add(User(id=2, settings="[1, 2]"))
        await session.commit()
    await db.init()
    async with db._sessions() as session:
        assert (await session.get(User, 1)).settings == "not json" and (await session.get(User, 2)).settings == "[1, 2]"
    assert await db.list_formats(1) == [] and await db.list_formats(2) == []


async def test_a_new_installation_just_notes_that_nothing_needs_changing(tmp_path):
    fresh = Database(f"sqlite+aiosqlite:///{tmp_path}/new.db")
    await fresh.init()
    try:
        assert await fresh.kv_get(AUTO_FORMATS_MIGRATION) == "done" and await fresh.count_users() == 0
    finally:
        await fresh.close()
