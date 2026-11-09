"""Async persistence (SQLAlchemy 2.x).

Hugging Face Spaces have no persistent disk, so point ``DATABASE_URL`` at an
external Postgres (Neon, Supabase, ...).  SQLite works for local testing.
"""
from __future__ import annotations

import copy
import json
import os
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import BigInteger, DateTime, Integer, LargeBinary, String, Text, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .errors import FormatError
from .parser import DEFAULT_TEMPLATE

DEFAULT_CAPTION = "<b>{filename}</b>"  # the new file name in bold
# The user's own settings.  What names, captions and thumbnails a file gets is not here: it is in the saved formats (below).
# The default metadata (``metadata_on``, ``meta``, ``keep_*``) is a preset: it applies to auto renames whose format has no
# metadata of its own, and a format can copy it ("Apply default metadata").
DEFAULT_SETTINGS: Dict[str, Any] = {
    "mode": "filename",  # filename | caption
    "file_type": "same",  # same | document | video | audio
    "remove_words": [],
    "metadata_on": False,
    "meta": {"title": "", "author": "", "artist": "", "audio": "", "subtitle": "", "video": ""},
    "keep_audio": [],
    "keep_subtitle": [],
    "target_channel": None,
    "auto_format": 0,  # id of the auto format that renames the files sent without /rename (0 = none saved: the built-in defaults)
}


# Captions used to be plain.  Whoever still has that default stored gets the bold one, once (see Database._migrate).
OLD_DEFAULT_CAPTION = "{filename}"
BOLD_CAPTION_MIGRATION = "migrated:bold_caption"
# The global template, caption and thumbnail used to be the settings of auto renames.  They are now an auto format of their own.
AUTO_FORMATS_MIGRATION = "migrated:auto_formats"
MIGRATED_FORMAT_NAME = "Default"

MAX_FORMATS = 12  # per kind: 12 auto formats and 12 manual ones
# Two kinds of format: ``auto`` renames every file the user sends (the one in use is picked from the /start menu), ``manual``
# is picked with /rename for one file.  Formats saved before there were two kinds are manual.
FORMAT_KINDS = ("auto", "manual")
DEFAULT_KIND = "manual"
# what a saved format holds; everything else (mode, file type, ...) comes from the user's settings.  A format never
# borrows a caption from anywhere else: without a caption of its own it sends the new file name in bold.
DEFAULT_FORMAT: Dict[str, Any] = {
    "template": DEFAULT_TEMPLATE,
    "caption": DEFAULT_CAPTION,
    "meta": {"title": "", "author": "", "artist": "", "audio": "", "subtitle": "", "video": ""},
    "keep_audio": [],
    "keep_subtitle": [],
    # a manual format can name the channel its episodes are published in (see dbflow); None: no post is made
    "channel": None,
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    settings: Mapped[str] = mapped_column(Text, default="{}")
    # The old global thumbnail.  Thumbnails belong to the formats now; this column is only read once, by the migration
    # (create_all() never alters a table, so the column stays).
    thumb: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    files_renamed: Mapped[int] = mapped_column(Integer, default=0)
    bytes_processed: Mapped[int] = mapped_column(BigInteger, default=0)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Admin(Base):
    __tablename__ = "admins"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    added_by: Mapped[int] = mapped_column(BigInteger, default=0)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Format(Base):
    # Not just "formats": the Postgres may be shared with another app that already has a table of that name, and
    # create_all() leaves an existing table alone, so ours would never be created.
    __tablename__ = "rename_formats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    name: Mapped[str] = mapped_column(String(64))
    data: Mapped[str] = mapped_column(Text, default="{}")
    thumb: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    created: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class FormatShare(Base):
    """What a "Share format" card links to: one of a user's formats, opened with a hard-to-guess token.

    A table of its own (not a column on ``rename_formats``): create_all() creates missing tables but never alters existing
    ones, so a new column would be missing from every database that already has the table.
    """

    __tablename__ = "rename_format_shares"

    token: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)  # whose format it is
    fmt_id: Mapped[int] = mapped_column(Integer, index=True)
    created: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class KV(Base):
    __tablename__ = "kv"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


def normalize_database_url(url: str) -> Tuple[str, Dict[str, Any]]:
    """Turn a provider URL into something SQLAlchemy's async drivers accept."""
    connect_args: Dict[str, Any] = {}
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]
    if url.startswith("postgresql+asyncpg://"):
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query))
        sslmode = query.pop("sslmode", None)
        query.pop("channel_binding", None)  # Neon adds this; asyncpg rejects it
        if sslmode and sslmode != "disable":
            connect_args["ssl"] = "require"
        url = urlunsplit(parts._replace(query=urlencode(query)))
    return url, connect_args


def _merge_settings(stored: Dict[str, Any]) -> Dict[str, Any]:
    merged = copy.deepcopy(DEFAULT_SETTINGS)
    for key, value in stored.items():
        if key == "meta" and isinstance(value, dict):
            merged["meta"].update({k: v for k, v in value.items() if k in merged["meta"]})
        elif key in merged:
            merged[key] = value
    return merged


def _merge_format(stored: Dict[str, Any]) -> Dict[str, Any]:
    merged = copy.deepcopy(DEFAULT_FORMAT)
    merged["kind"] = stored["kind"] if stored.get("kind") in FORMAT_KINDS else DEFAULT_KIND
    for key in ("template", "caption"):
        if isinstance(stored.get(key), str) and stored[key].strip():
            merged[key] = stored[key]
    if isinstance(stored.get("meta"), dict):
        merged["meta"].update({k: str(v) for k, v in stored["meta"].items() if k in merged["meta"]})
    for key in ("keep_audio", "keep_subtitle"):
        if isinstance(stored.get(key), list):
            merged[key] = [str(t) for t in stored[key]]
    channel = stored.get("channel")
    if merged["kind"] == "manual" and isinstance(channel, int) and not isinstance(channel, bool):
        merged["channel"] = channel  # only a manual format has one: an auto format renames files that nobody uploads on
    return merged


def _stored(raw: Optional[str]) -> Dict[str, Any]:
    try:
        stored = json.loads(raw or "{}")
    except ValueError:
        return {}
    return stored if isinstance(stored, dict) else {}


def _kind_of(raw: Optional[str]) -> str:
    """The kind of the format a row holds (``auto`` | ``manual``)."""
    return _merge_format(_stored(raw))["kind"]


def _format_dict(fid: int, name: str, raw: Optional[str], has_thumb: bool) -> Dict[str, Any]:
    out = _merge_format(_stored(raw))
    out.update(id=int(fid), name=name, has_thumb=bool(has_thumb))
    return out


def _format_json(fmt: Dict[str, Any]) -> str:
    """What is stored for a format: its fields and its kind."""
    return json.dumps({**{k: fmt[k] for k in DEFAULT_FORMAT}, "kind": fmt["kind"]}, ensure_ascii=False)


def _free_name(name: str, taken: Iterable[str], name_max: int) -> str:
    """``name``, or ``name (2)``, ``name (3)`` ... when the user already has a format called that (case aside)."""
    used = {n.casefold() for n in taken}
    if name.casefold() not in used:
        return name
    number = 2
    while True:  # at most MAX_FORMATS names exist, so a free one turns up quickly
        suffix = f" ({number})"
        candidate = name[: max(1, name_max - len(suffix))].rstrip() + suffix
        if candidate.casefold() not in used:
            return candidate
        number += 1


class Database:
    def __init__(self, url: str):
        self.url, connect_args = normalize_database_url(url)
        if self.url.startswith("sqlite"):
            path = self.url.split("///", 1)[-1]
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
        self.engine: AsyncEngine = create_async_engine(self.url, pool_pre_ping=True, connect_args=connect_args)
        self._sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self._cache: Dict[int, Dict[str, Any]] = {}

    async def init(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await self._migrate()

    async def _migrate(self) -> None:
        """Changes to what earlier versions stored, each made once: a marker in the kv table says it was done."""
        if await self.kv_get(BOLD_CAPTION_MIGRATION) is None:
            async with self._sessions() as session:
                for row in (await session.execute(select(User))).scalars().all():
                    stored = _stored(row.settings)
                    if stored.get("caption") == OLD_DEFAULT_CAPTION:
                        stored["caption"] = DEFAULT_CAPTION
                        row.settings = json.dumps(stored, ensure_ascii=False)
                await session.commit()
            await self.kv_set(BOLD_CAPTION_MIGRATION, "done")
        if await self.kv_get(AUTO_FORMATS_MIGRATION) is None:
            await self._migrate_auto_formats()
            await self.kv_set(AUTO_FORMATS_MIGRATION, "done")

    async def _migrate_auto_formats(self) -> None:
        """The global template, caption and thumbnail become an auto format called "Default", which is the one in use.

        Whoever never changed them has nothing to keep and gets no format (auto renames then use the built-in defaults).  The
        metadata is not copied: it stays where it is as the default metadata, and an auto format with no metadata of its own uses
        it while it is On, which is how it always applied to auto renames (and edits to it still count).  Formats that had no
        thumbnail of their own used the global one: they keep using it.
        A user is migrated once: the ``auto_format`` key marks it (the rows are rewritten in one transaction).
        """
        async with self._sessions() as session:
            for row in (await session.execute(select(User))).scalars().all():
                try:
                    stored = json.loads(row.settings or "{}")
                except ValueError:
                    continue
                if not isinstance(stored, dict) or "auto_format" in stored:
                    continue
                old = _merge_format({"template": stored.get("template"), "caption": stored.get("caption")})
                thumb = bytes(row.thumb) if row.thumb else None
                keeps_something = old["template"] != DEFAULT_FORMAT["template"] or old["caption"] != DEFAULT_FORMAT["caption"] or thumb
                fid = 0
                if keeps_something:
                    made = Format(user_id=row.id, name=MIGRATED_FORMAT_NAME, data=_format_json({**old, "kind": "auto"}), thumb=thumb)
                    session.add(made)
                    await session.flush()
                    fid = made.id
                if thumb:
                    for fmt in (await session.execute(select(Format).where(Format.user_id == row.id, Format.thumb.is_(None)))).scalars():
                        if _kind_of(fmt.data) == "manual":
                            fmt.thumb = thumb
                row.thumb = None
                stored.pop("template", None)
                stored.pop("caption", None)
                stored["auto_format"] = fid
                row.settings = json.dumps(stored, ensure_ascii=False)
            await session.commit()
        self._cache.clear()

    async def close(self) -> None:
        await self.engine.dispose()

    # ----- users / settings ---------------------------------------------------
    async def _get_or_create(self, session, uid: int) -> User:
        row = await session.get(User, uid)
        if row is None:
            # A forwarded batch is handled by several tasks at once (and two instances can overlap briefly while the
            # Space restarts), so let the database settle who creates the row instead of failing on the unique key.
            insert = pg_insert if self.engine.dialect.name == "postgresql" else sqlite_insert
            await session.execute(insert(User).values(id=uid, settings="{}").on_conflict_do_nothing(index_elements=["id"]))
            row = await session.get(User, uid)
        return row

    async def get_settings(self, uid: int) -> Dict[str, Any]:
        if uid not in self._cache:
            async with self._sessions() as session:
                row = await self._get_or_create(session, uid)
                await session.commit()
                try:
                    stored = json.loads(row.settings or "{}")
                except ValueError:
                    stored = {}
            self._cache[uid] = _merge_settings(stored if isinstance(stored, dict) else {})
        return copy.deepcopy(self._cache[uid])

    async def update_settings(self, uid: int, **changes: Any) -> Dict[str, Any]:
        """Update top-level keys; a ``meta`` dict is merged field by field."""
        current = await self.get_settings(uid)
        for key, value in changes.items():
            if key not in DEFAULT_SETTINGS:
                raise KeyError(key)
            if key == "meta":
                current["meta"].update(value)
            else:
                current[key] = value
        async with self._sessions() as session:
            row = await self._get_or_create(session, uid)
            row.settings = json.dumps(current, ensure_ascii=False)
            await session.commit()
        self._cache[uid] = current
        return copy.deepcopy(current)

    async def record_job(self, uid: int, size: int) -> None:
        async with self._sessions() as session:
            row = await self._get_or_create(session, uid)
            row.files_renamed = (row.files_renamed or 0) + 1
            row.bytes_processed = (row.bytes_processed or 0) + int(size)
            await session.commit()

    async def count_users(self) -> int:
        async with self._sessions() as session:
            return int((await session.execute(select(func.count()).select_from(User))).scalar() or 0)

    async def totals(self) -> Tuple[int, int]:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(func.coalesce(func.sum(User.files_renamed), 0), func.coalesce(func.sum(User.bytes_processed), 0))
                )
            ).one()
            return int(row[0]), int(row[1])

    # ----- saved formats (auto and manual) ----------------------------------------------
    async def list_formats(self, uid: int, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        """The user's formats, oldest first: all of them, or only those of one ``kind``."""
        async with self._sessions() as session:
            result = await session.execute(
                select(Format.id, Format.name, Format.data, Format.thumb.isnot(None)).where(Format.user_id == uid).order_by(Format.id)
            )
            formats = [_format_dict(r[0], r[1], r[2], r[3]) for r in result.all()]
        return [f for f in formats if kind is None or f["kind"] == kind]

    async def count_formats(self, uid: int, kind: Optional[str] = None) -> int:
        return len(await self.list_formats(uid, kind))

    async def get_format(self, uid: int, fid: int) -> Optional[Dict[str, Any]]:
        async with self._sessions() as session:
            result = await session.execute(
                select(Format.id, Format.name, Format.data, Format.thumb.isnot(None)).where(Format.id == fid, Format.user_id == uid)
            )
            r = result.first()
            return _format_dict(r[0], r[1], r[2], r[3]) if r else None

    @staticmethod
    async def _names(session: Any, uid: int, kind: str, *, except_id: int = 0) -> List[str]:
        """The names of the user's formats of one kind (a name only has to be free among its own kind)."""
        rows = await session.execute(select(Format.id, Format.name, Format.data).where(Format.user_id == uid))
        return [r[1] for r in rows.all() if r[0] != except_id and _kind_of(r[2]) == kind]

    async def create_format(
        self, uid: int, name: str, template: Optional[str] = None, kind: str = DEFAULT_KIND
    ) -> Dict[str, Any]:
        """Add a format of ``kind``; raises ``FormatError`` when the user already has ``MAX_FORMATS`` of that kind or the name is taken."""
        if kind not in FORMAT_KINDS:
            raise KeyError(kind)
        async with self._sessions() as session:
            names = await self._names(session, uid, kind)
            if len(names) >= MAX_FORMATS:
                raise FormatError("limit")
            if name.casefold() in {n.casefold() for n in names}:
                raise FormatError("taken")
            data = {**copy.deepcopy(DEFAULT_FORMAT), "kind": kind}
            if template:
                data["template"] = template
            row = Format(user_id=uid, name=name, data=_format_json(data))
            session.add(row)
            await session.flush()
            fid, raw = row.id, row.data
            await session.commit()
        return _format_dict(fid, name, raw, False)

    async def update_format(self, uid: int, fid: int, **changes: Any) -> Optional[Dict[str, Any]]:
        """Change ``name``, ``template``, ``caption``, ``keep_audio``, ``keep_subtitle``, ``channel`` or merge a ``meta`` dict; ``None`` if it is gone."""
        async with self._sessions() as session:
            row = await session.get(Format, fid)
            if row is None or row.user_id != uid:
                return None
            current = _format_dict(row.id, row.name, row.data, False)
            for key, value in changes.items():
                if key == "name":
                    if value.casefold() in {n.casefold() for n in await self._names(session, uid, current["kind"], except_id=fid)}:
                        raise FormatError("taken")
                    row.name = value
                elif key == "meta":
                    current["meta"].update(value)
                elif key == "channel":
                    current["channel"] = value if current["kind"] == "manual" else None
                elif key in DEFAULT_FORMAT:
                    current[key] = value
                else:
                    raise KeyError(key)
            row.data = _format_json(current)
            has_thumb = row.thumb is not None
            await session.commit()
            return _format_dict(row.id, row.name, row.data, has_thumb)

    async def set_format_thumb(self, uid: int, fid: int, data: Optional[bytes]) -> bool:
        async with self._sessions() as session:
            row = await session.get(Format, fid)
            if row is None or row.user_id != uid:
                return False
            row.thumb = data
            await session.commit()
            return True

    async def get_format_thumb(self, uid: int, fid: int) -> Optional[bytes]:
        async with self._sessions() as session:
            row = await session.get(Format, fid)
            return bytes(row.thumb) if row is not None and row.user_id == uid and row.thumb else None

    async def delete_format(self, uid: int, fid: int) -> bool:
        async with self._sessions() as session:
            row = await session.get(Format, fid)
            if row is None or row.user_id != uid:
                return False
            await session.delete(row)
            await session.execute(delete(FormatShare).where(FormatShare.user_id == uid, FormatShare.fmt_id == fid))  # its links die with it
            await session.commit()
            return True

    # ----- sharing a format ------------------------------------------------------------
    async def share_token(self, uid: int, fid: int) -> Optional[str]:
        """The token that opens the user's format for others (made the first time, the same afterwards); ``None`` if there is no such format."""
        async with self._sessions() as session:
            if (await session.execute(select(Format.id).where(Format.id == fid, Format.user_id == uid))).first() is None:
                return None
            existing = (
                await session.execute(
                    select(FormatShare.token)
                    .where(FormatShare.user_id == uid, FormatShare.fmt_id == fid)
                    .order_by(FormatShare.created)
                    .limit(1)
                )
            ).scalar()
            if existing:
                return str(existing)
            token = secrets.token_urlsafe(12)
            session.add(FormatShare(token=token, user_id=uid, fmt_id=fid))
            await session.commit()
            return token

    async def get_shared_format(self, token: str) -> Optional[Tuple[int, Dict[str, Any]]]:
        """``(owner's user id, the format as it is now)``; ``None`` for an unknown token or a format that was deleted."""
        async with self._sessions() as session:
            share = await session.get(FormatShare, token)
            if share is None:
                return None
            r = (
                await session.execute(
                    select(Format.id, Format.name, Format.data, Format.thumb.isnot(None)).where(
                        Format.id == share.fmt_id, Format.user_id == share.user_id
                    )
                )
            ).first()
            return (int(share.user_id), _format_dict(r[0], r[1], r[2], r[3])) if r else None

    async def copy_format(
        self, uid: int, owner_id: int, fid: int, name_max: int = 64, kind: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Give ``uid`` a copy of ``owner_id``'s format (thumbnail included), under a free name.

        The copy is of the same kind as the original unless ``kind`` says otherwise: that is how a user copies an auto format
        to the manual ones and the other way round (``owner_id`` is then the user themselves).  ``None`` when that format is
        gone; ``FormatError("limit")`` when ``uid`` already has ``MAX_FORMATS`` of that kind.  The Channel of a manual format is
        kept only by its owner's own manual copy.
        """
        if kind is not None and kind not in FORMAT_KINDS:
            raise KeyError(kind)
        async with self._sessions() as session:
            source = await session.get(Format, fid)
            if source is None or source.user_id != owner_id:
                return None
            target = kind or _kind_of(source.data)
            names = await self._names(session, uid, target)
            if len(names) >= MAX_FORMATS:
                raise FormatError("limit")
            name = _free_name(source.name, names, name_max)
            copied = _merge_format(_stored(source.data))
            if uid != owner_id or target != "manual":
                copied["channel"] = None  # somebody else's copy (or an auto one) does not publish in the original's channel
            data = _format_json({**copied, "kind": target})
            row = Format(user_id=uid, name=name, data=data, thumb=source.thumb)
            session.add(row)
            await session.flush()
            new_id, has_thumb = row.id, source.thumb is not None
            await session.commit()
        return _format_dict(new_id, name, data, has_thumb)

    # ----- the auto format in use ----------------------------------------------------------
    async def get_auto_format(self, uid: int) -> Optional[Dict[str, Any]]:
        """The auto format that renames the user's files, or ``None`` when they have none (the built-in defaults are used then).

        Having auto formats always means having one in use: when the chosen one is gone (deleted) or was never chosen, the oldest
        takes over and is remembered.
        """
        chosen = (await self.get_settings(uid))["auto_format"]
        formats = await self.list_formats(uid, "auto")
        if not formats:
            if chosen:
                await self.update_settings(uid, auto_format=0)
            return None
        current = next((f for f in formats if f["id"] == chosen), formats[0])
        if current["id"] != chosen:
            await self.update_settings(uid, auto_format=current["id"])
        return current

    async def select_auto_format(self, uid: int, fid: int) -> Optional[Dict[str, Any]]:
        """Use this auto format for the user's files from now on; ``None`` when there is no such auto format."""
        fmt = await self.get_format(uid, fid)
        if fmt is None or fmt["kind"] != "auto":
            return None
        await self.update_settings(uid, auto_format=fid)
        return fmt

    # ----- admins ------------------------------------------------------------------
    async def list_admins(self) -> List[int]:
        async with self._sessions() as session:
            return [int(r) for r in (await session.execute(select(Admin.id).order_by(Admin.added_at))).scalars()]

    async def add_admin(self, uid: int, added_by: int = 0) -> bool:
        async with self._sessions() as session:
            if await session.get(Admin, uid) is not None:
                return False
            session.add(Admin(id=uid, added_by=added_by))
            await session.commit()
            return True

    async def del_admin(self, uid: int) -> bool:
        async with self._sessions() as session:
            row = await session.get(Admin, uid)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    # ----- key/value (session string etc.) ---------------------------------------------
    async def kv_get(self, key: str) -> Optional[str]:
        async with self._sessions() as session:
            row = await session.get(KV, key)
            return row.value if row is not None else None

    async def kv_set(self, key: str, value: str) -> None:
        async with self._sessions() as session:
            row = await session.get(KV, key)
            if row is None:
                session.add(KV(key=key, value=value))
            else:
                row.value = value
            await session.commit()

    async def kv_delete(self, key: str) -> None:
        async with self._sessions() as session:
            row = await session.get(KV, key)
            if row is not None:
                await session.delete(row)
                await session.commit()
