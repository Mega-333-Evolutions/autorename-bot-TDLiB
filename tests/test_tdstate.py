"""TDLib's folder kept in the bot's database: what is packed, what is refused on the way back, and when it is saved."""
import asyncio
import base64
import gzip
import io
import logging
import os
import sqlite3
import tarfile
from pathlib import Path

import pytest

from bot import tdstate
from bot.db import Database


def make_tree(root: Path, files: dict) -> Path:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return root


def read_tree(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(Path(root).rglob("*")) if p.is_file()}


def crafted(*entries) -> bytes:
    """A packed folder built by hand from ``(tarfile.TarInfo, data)`` pairs: what a damaged or hostile copy might hold."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for info, data in entries:
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    return gzip.compress(raw.getvalue())


def member(name, data=b"x", kind=tarfile.REGTYPE, linkname=""):
    info = tarfile.TarInfo(name)
    info.type, info.linkname = kind, linkname
    info.size = len(data) if kind == tarfile.REGTYPE else 0
    return info, (data if kind == tarfile.REGTYPE else None)


FILES = {"td.binlog": b"\x00login keys\xff" * 50, "db/db.sqlite.bak": b"not a database", "files/a/b/c.dat": b"\x01\x02\x03", "empty": b""}


# ---------------------------------------------------------------------------
# pack and unpack
# ---------------------------------------------------------------------------


def test_what_is_packed_comes_back_the_same(tmp_path):
    source = make_tree(tmp_path / "source", FILES)
    tdstate.unpack(tdstate.pack(source), tmp_path / "back")
    assert read_tree(tmp_path / "back") == FILES


def test_the_same_files_pack_to_the_same_bytes_whenever_they_were_made(tmp_path):
    first = make_tree(tmp_path / "first", FILES)
    second = tmp_path / "second"
    for name in reversed(list(FILES)):  # another order, another time
        make_tree(second, {name: FILES[name]})
        os.utime(second / name, (1_000_000_000, 1_000_000_000))
    assert tdstate.pack(first) == tdstate.pack(second) == tdstate.pack(first)  # so an unchanged folder is not saved again
    (second / "td.binlog").write_bytes(b"changed")
    assert tdstate.pack(first) != tdstate.pack(second)


def test_the_order_the_folder_is_read_in_does_not_change_what_is_packed(tmp_path, monkeypatch):
    source = make_tree(tmp_path / "td", {**FILES, "z/last": b"1", "a/first": b"2", "m/deep/er/file": b"3"})
    expected = tdstate.pack(source)
    real_walk = os.walk
    monkeypatch.setattr(tdstate.os, "walk", lambda top: iter(reversed(list(real_walk(top)))))  # a filesystem may list its folders in any order
    assert tdstate.pack(source) == expected


def test_an_empty_or_missing_folder_packs_to_a_state_with_no_files(tmp_path):
    (tmp_path / "empty").mkdir()
    for folder in (tmp_path / "empty", tmp_path / "missing"):
        tdstate.unpack(tdstate.pack(folder), tmp_path / "out")
        assert read_tree(tmp_path / "out") == {}


def test_sqlite_databases_are_packed_as_a_consistent_copy_while_they_are_in_use(tmp_path):
    folder = tmp_path / "td"
    (folder / "db").mkdir(parents=True)
    conn = sqlite3.connect(folder / "db" / "db.sqlite")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE chats (id INTEGER, title TEXT)")
        conn.executemany("INSERT INTO chats VALUES (?, ?)", [(i, f"chat {i}") for i in range(200)])
        conn.commit()
        assert (folder / "db" / "db.sqlite-wal").stat().st_size > 0  # the rows are in the -wal file next to the database, not yet in it
        packed = tdstate.pack(folder)
    finally:
        conn.close()
    tdstate.unpack(packed, tmp_path / "back")
    assert sorted(read_tree(tmp_path / "back")) == ["db/db.sqlite"]  # the side files are not kept: the copy holds what they had
    copy = sqlite3.connect(tmp_path / "back" / "db" / "db.sqlite")
    try:
        assert copy.execute("SELECT count(*), max(title) FROM chats").fetchone() == (200, "chat 99")
        assert copy.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        copy.close()


def test_a_file_that_only_looks_like_a_database_is_packed_as_it_is(tmp_path):
    source = make_tree(tmp_path / "td", {"odd.sqlite": b"this is not a database", "also.db": b"\x00" * 5000})
    tdstate.unpack(tdstate.pack(source), tmp_path / "back")
    assert read_tree(tmp_path / "back") == read_tree(source)


def test_links_and_side_files_of_a_database_are_left_out(tmp_path):
    source = make_tree(tmp_path / "td", {"keep.bin": b"1", "x.sqlite-wal": b"w", "x.sqlite-shm": b"s", "x.db-journal": b"j", "notes-wal": b"stays"})
    (source / "link.bin").symlink_to(source / "keep.bin")
    tdstate.unpack(tdstate.pack(source), tmp_path / "back")
    assert sorted(read_tree(tmp_path / "back")) == ["keep.bin", "notes-wal"]


def test_a_file_that_goes_away_while_the_folder_is_packed_is_skipped(tmp_path, monkeypatch):
    source = make_tree(tmp_path / "td", {"keep.bin": b"1", "gone.bin": b"2"})
    real = Path.read_bytes

    def flaky(self):
        if self.name == "gone.bin":
            raise FileNotFoundError(str(self))  # TDLib deleted it just now
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", flaky)
    tdstate.unpack(tdstate.pack(source), tmp_path / "back")
    assert read_tree(tmp_path / "back") == {"keep.bin": b"1"}


@pytest.mark.parametrize("bad", [
    member("/etc/cron.d/evil"),
    member("../evil"),
    member("a/../../evil"),
    member("link", kind=tarfile.SYMTYPE, linkname="/etc/passwd"),
    member("hard", kind=tarfile.LNKTYPE, linkname="td.binlog"),
    member("folder", kind=tarfile.DIRTYPE),
], ids=["absolute", "parent", "buried-parent", "symlink", "hardlink", "directory"])
def test_a_packed_folder_with_anything_but_plain_files_is_refused_before_anything_is_written(tmp_path, bad):
    blob = crafted(member("td.binlog", b"fine"), bad)
    with pytest.raises(ValueError, match="unexpected entry"):
        tdstate.unpack(blob, tmp_path / "td")
    assert not (tmp_path / "td").exists() and not (tmp_path / "evil").exists()


def test_a_member_far_bigger_than_any_state_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(tdstate, "MAX_PACKED_BYTES", 10)
    with pytest.raises(ValueError, match="unexpected entry"):
        tdstate.unpack(crafted(member("big.bin", b"x" * 100)), tmp_path / "td")
    tdstate.unpack(crafted(member("ok.bin", b"x" * 40)), tmp_path / "td")  # up to four times what is kept
    assert (tmp_path / "td" / "ok.bin").exists()


# ---------------------------------------------------------------------------
# wipe, clean_files
# ---------------------------------------------------------------------------


def test_wipe_removes_the_folder_and_does_not_mind_a_missing_one(tmp_path):
    make_tree(tmp_path / "td", FILES)
    tdstate.wipe(tmp_path / "td")
    tdstate.wipe(tmp_path / "td")
    assert not (tmp_path / "td").exists()


def test_clean_files_keeps_only_the_database(tmp_path):
    root = make_tree(tmp_path / "td", {"database/td.binlog": b"login", "documents/half.bin": b"x", "temp/t": b"y", "stray.bin": b"z"})
    outside = make_tree(tmp_path / "outside", {"precious.txt": b"mine"})
    (root / "shortcut").symlink_to(outside, target_is_directory=True)
    tdstate.clean_files(root)
    assert sorted(p.name for p in root.iterdir()) == ["database"] and (root / "database" / "td.binlog").read_bytes() == b"login"
    assert (outside / "precious.txt").read_bytes() == b"mine"  # a link is removed, not followed


def test_clean_files_makes_a_missing_folder_and_can_keep_another_name(tmp_path):
    tdstate.clean_files(tmp_path / "new" / "td")
    assert (tmp_path / "new" / "td").is_dir() and list((tmp_path / "new" / "td").iterdir()) == []
    root = make_tree(tmp_path / "td", {"database/a": b"1", "other/b": b"2"})
    tdstate.clean_files(root, keep="other")
    assert sorted(p.name for p in root.iterdir()) == ["other"]


# ---------------------------------------------------------------------------
# restore and Saver (with a database)
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/state.db")
    await database.init()
    yield database
    await database.close()


async def saved(db, tmp_path, files=None, token_id="1234", name="earlier"):
    folder = make_tree(tmp_path / name, files or FILES)
    assert await tdstate.Saver(db, folder, token_id).save()
    return folder


async def test_a_saved_state_is_restored_into_a_clean_folder(db, tmp_path):
    await saved(db, tmp_path)
    folder = make_tree(tmp_path / "fresh", {"leftover.bin": b"from an earlier try"})  # no binlog: it is not a login
    assert await tdstate.restore(db, folder, "1234") is True
    assert read_tree(folder) == FILES  # exactly what was saved: the leftovers are gone


async def test_there_is_nothing_to_restore_before_the_first_save(db, tmp_path):
    assert await tdstate.restore(db, tmp_path / "td", "1234") is False
    assert not (tmp_path / "td").exists()


@pytest.mark.parametrize("data", [None, ""])
async def test_a_saved_bot_id_without_a_state_restores_nothing(db, tmp_path, data):
    await db.kv_set(tdstate.KEY_BOT, "1234")
    if data is not None:
        await db.kv_set(tdstate.KEY_DATA, data)
    assert await tdstate.restore(db, tmp_path / "td", "1234") is False
    assert not (tmp_path / "td").exists()


async def test_the_state_of_another_bot_is_not_restored(db, tmp_path):
    await saved(db, tmp_path, token_id="1234")
    assert await tdstate.restore(db, tmp_path / "td", "5678") is False
    assert not (tmp_path / "td").exists()


async def test_a_database_folder_that_already_exists_is_newer_than_anything_saved(db, tmp_path):
    await saved(db, tmp_path)
    folder = make_tree(tmp_path / "td", {"td.binlog": b"newer login", "extra": b"1"})
    assert await tdstate.restore(db, folder, "1234") is False
    assert read_tree(folder) == {"td.binlog": b"newer login", "extra": b"1"}


@pytest.mark.parametrize("damaged", [
    "!!! not base64 !!!",
    base64.b64encode(b"base64 of something that is not gzip").decode(),
    base64.b64encode(gzip.compress(b"gzip of something that is not a tar")).decode(),
    base64.b64encode(crafted(member("td.binlog", b"fine"), member("../evil"))).decode(),
], ids=["not-base64", "not-gzip", "not-tar", "refused-entry"])
async def test_a_saved_state_that_cannot_be_used_is_dropped_and_the_login_made_afresh(db, tmp_path, caplog, damaged):
    await db.kv_set(tdstate.KEY_BOT, "1234")
    await db.kv_set(tdstate.KEY_DATA, damaged)
    folder = make_tree(tmp_path / "td", {"leftover.bin": b"x"})
    with caplog.at_level(logging.WARNING, logger="bot.tdstate"):
        assert await tdstate.restore(db, folder, "1234") is False
    assert not folder.exists() and not (tmp_path / "evil").exists()  # nothing half-restored is left for TDLib to find
    assert "could not be used" in caplog.text


async def test_a_restore_that_fails_half_way_leaves_nothing_for_tdlib_to_find(db, tmp_path, monkeypatch):
    await saved(db, tmp_path)

    def out_of_space(blob, folder):
        make_tree(Path(folder), {"td.binlog": b"half of a login"})
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(tdstate, "unpack", out_of_space)
    assert await tdstate.restore(db, tmp_path / "td", "1234") is False
    assert not (tmp_path / "td").exists()  # a half-written login would be taken for a whole one


async def test_the_first_save_writes_the_state_and_the_bot_it_belongs_to(db, tmp_path):
    folder = make_tree(tmp_path / "td", FILES)
    saver = tdstate.Saver(db, folder, "1234")
    assert await saver.save() is True
    assert await db.kv_get(tdstate.KEY_BOT) == "1234"
    assert base64.b64decode(await db.kv_get(tdstate.KEY_DATA)) == tdstate.pack(folder)


async def test_an_unchanged_state_is_not_written_again_and_a_changed_one_is(db, tmp_path, monkeypatch):
    writes = []
    real = db.kv_set

    async def counting(key, value):
        writes.append(key)
        await real(key, value)

    monkeypatch.setattr(db, "kv_set", counting)
    folder = make_tree(tmp_path / "td", FILES)
    saver = tdstate.Saver(db, folder, "1234")
    assert await saver.save() is True and len(writes) == 2
    assert await saver.save() is False and await saver.save() is False and len(writes) == 2  # nothing changed: no write
    (folder / "td.binlog").write_bytes(b"a new login key")
    assert await saver.save() is True and len(writes) == 4


async def test_a_state_too_big_for_the_database_is_not_saved_and_is_reported_once(db, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(tdstate, "MAX_PACKED_BYTES", 200)
    folder = make_tree(tmp_path / "td", {"big.bin": os.urandom(2000)})  # random bytes do not shrink
    saver = tdstate.Saver(db, folder, "1234")
    with caplog.at_level(logging.WARNING, logger="bot.tdstate"):
        assert await saver.save() is False and await saver.save() is False
    assert caplog.text.count("too big") == 1 and await db.kv_get(tdstate.KEY_DATA) is None
    (folder / "big.bin").write_bytes(b"small now")
    assert await saver.save() is True


async def test_a_failed_write_is_reported_and_tried_again_next_time(db, tmp_path, monkeypatch, caplog):
    folder = make_tree(tmp_path / "td", FILES)
    saver = tdstate.Saver(db, folder, "1234")
    real = db.kv_set

    async def refusing(key, value):
        raise ConnectionError("database is away")

    monkeypatch.setattr(db, "kv_set", refusing)
    with caplog.at_level(logging.WARNING, logger="bot.tdstate"):
        assert await saver.save() is False
    assert "could not save" in caplog.text and "database is away" in caplog.text
    monkeypatch.setattr(db, "kv_set", real)
    assert await saver.save() is True  # the same state: it was never written, so it is written now


async def test_a_folder_that_cannot_be_packed_is_reported(db, tmp_path, monkeypatch, caplog):
    def broken(_folder):
        raise PermissionError("no access")

    monkeypatch.setattr(tdstate, "pack", broken)
    with caplog.at_level(logging.WARNING, logger="bot.tdstate"):
        assert await tdstate.Saver(db, tmp_path / "td", "1234").save() is False
    assert "could not pack" in caplog.text and "no access" in caplog.text


async def test_the_saver_looks_at_the_folder_on_its_own_until_it_is_stopped(db, tmp_path):
    folder = make_tree(tmp_path / "td", FILES)
    saver = tdstate.Saver(db, folder, "1234", interval=0.01)
    assert await saver.save()
    saver.start()
    task = saver._task
    saver.start()
    assert saver._task is task  # started once
    (folder / "td.binlog").write_bytes(b"learnt something")
    for _ in range(300):
        if base64.b64decode(await db.kv_get(tdstate.KEY_DATA)) == tdstate.pack(folder):
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("the change was never saved")
    await saver.stop()
    await saver.stop()  # stopping twice does no harm
    assert saver._task is None and task.cancelled()
    (folder / "td.binlog").write_bytes(b"learnt more")
    await asyncio.sleep(0.05)
    assert base64.b64decode(await db.kv_get(tdstate.KEY_DATA)) != tdstate.pack(folder)  # nobody is looking any more


async def test_a_saved_login_with_its_database_works_after_a_restart(db, tmp_path):
    """The whole way: TDLib's folder (with a database that is in use) -> the bot's database -> an empty disk -> TDLib's folder again."""
    folder = tmp_path / "td" / "database"
    folder.mkdir(parents=True)
    (folder / "td.binlog").write_bytes(b"login keys")
    conn = sqlite3.connect(folder / "db.sqlite")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE chats (id INTEGER PRIMARY KEY, title TEXT)")
        conn.execute("INSERT INTO chats VALUES (-1001234567890, 'the log channel')")
        conn.commit()
        assert await tdstate.Saver(db, folder, "1234").save()
    finally:
        conn.close()
    tdstate.wipe(tmp_path / "td")  # the Space restarted: the disk is empty
    assert await tdstate.restore(db, folder, "1234") is True
    assert (folder / "td.binlog").read_bytes() == b"login keys"
    restored = sqlite3.connect(folder / "db.sqlite")
    try:
        assert restored.execute("SELECT title FROM chats WHERE id = -1001234567890").fetchone() == ("the log channel",)
    finally:
        restored.close()
