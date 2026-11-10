"""Telegram handlers: commands, inline menus and the job entry points."""
from __future__ import annotations

import asyncio
import base64
import html
import io
import json
import logging
import re
import shutil
import time
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

from . import datacenter, errorlog, media, mediainfo, panels, richtext, speed, sysinfo, transfer
from .config import Config
from .db import MAX_FORMATS, Database
from .dbflow import PREFIX as DBFLOW_PREFIX
from .dbflow import DbFlow
from .errors import FormatError
from .formats import (
    FIELD_KINDS,
    KIND_LABELS,
    NAME_MAX,
    FormatSettings,
    default_auto_format,
    default_metadata_changes,
    has_default_metadata,
    other_kind,
    validate_name,
)
from .parser import compute_new_name, missing_variables, parse_caption, parse_info, split_ext
from .pipeline import PROBE_EXTS, Renamer, embedded_thumb, original_name_of, source_ext
from .queue import Job, JobQueue
from .sequence import SeqItem, SequenceState, analyse, format_report
from .settings_ops import CLEAR_WORDS, META_COMMANDS, apply_setting, parse_channel, parse_format_channel
from .telegraph import Telegraph, TelegraphError
from .tg import Button, errors, events, types
from .utils import clip, esc, human_size, human_uptime

log = logging.getLogger(__name__)

COMMANDS = [
    ("start", "Open the main menu"),
    ("help", "How to use the bot"),
    ("rename", "Rename one file with a manual format"),
    ("formats", "Auto and manual rename formats"),
    ("sequence", "Collect files to sort by episode"),
    ("sort", "Sort the collected files by episode"),
    ("mode", "Choose Filename or Caption mode"),
    ("filetype", "Choose how files are sent"),
    ("metadata", "Metadata and track settings"),
    ("keepaudio", "Keep audio tracks by language or number"),
    ("keepsubtitle", "Keep subtitle tracks by language or number"),
    ("tracks", "List the tracks of a file (reply to it)"),
    ("check", "See what I can read from a file (reply to it)"),
    ("mediainfo", "Full media details of a file on Telegraph (reply to it)"),
    ("dc", "Get the data center (DC) of a file or picture (reply to it)"),
    ("ethumb", "Extract the thumbnail of a file or video"),
    ("size", "Get the dimensions of a picture (reply to it)"),
    ("resize", "Resize a picture to an exact size (reply to it)"),
    ("queue", "Show your queued files"),
    ("cancel", "Cancel your current job"),
    ("speed", "Test the bot's internet speed (Ookla)"),
    ("status", "Show basic bot uptime, ping, and version information."),
    ("stats", "Show detailed system stats such as users, CPU, RAM, and disk usage."),
]
OWNER_COMMANDS = {"addadmin", "deladmin", "admins", "smedia"}
BACK = {
    "remove": "rename",
    "mt_title": "meta", "mt_author": "meta", "mt_artist": "meta",
    "mt_audio": "meta", "mt_subtitle": "meta", "mt_video": "meta",
    "keep_audio": "tool", "keep_subtitle": "tool",
}
PANEL_NAMES = {"home", "rename", "meta", "tool", "ftype", "help", "admin", "formats", "auto"}
QUEUE_SETTLE = 1.5  # seconds an idle worker waits so files sent together are queued in message order
ONE_FILE_WAIT = 600  # seconds the bot waits for the file after /rename (or /ethumb ...) before forgetting about it
ONE_SHOT_GRACE = 8  # seconds after a one-file command during which further files get a hint, not an auto-rename
ONE_SHOT_TAKES = {  # what each one-file command consumes; further files of that kind sent right after it are skipped
    "rename": {"doc"}, "ethumb": {"doc"}, "smedia": {"doc", "photo", "gif"},
}
ETHUMB_FRAME_WIDTH = 1280
START_MEDIA_MAX_BYTES = 10 * 1024 * 1024  # biggest /start picture or GIF; a copy of it is kept in the database
START_IMAGE_BOX = 1280  # pictures are shrunk to fit this box, as Telegram does with photos
SEE_THE_CHAT = "add the bot there as an admin and post any message in that chat: TDLib lets a bot use a chat only after it has seen something from it"
RESIZE_MAX_BYTES = 20 * 1024 * 1024  # biggest picture /size and /resize read
RESIZE_MAX_SIDE = 4096  # the biggest width or height /resize makes
PHOTO_MAX_SIDE = 1280  # Telegram shrinks a photo to this; /resize sends anything bigger as a file so its size stays exact
PHOTO_MAX_BYTES = 10 * 1024 * 1024  # biggest photo Telegram takes
PHOTO_MAX_RATIO = 20  # ... and the longer side of a photo can't be more than this many times the shorter one
HINT_SECONDS = 5  # how long the hint of /resize stays before it is removed
SEQUENCE_WAIT = 1800  # seconds /sequence keeps the collected files without a new one arriving
SEQUENCE_MAX = 1000  # files one /sequence collects
SEQUENCE_STATUS_DELAY = 1.2  # seconds without a new file before "N files collected" is sent
SORT_GAP = 0.3  # seconds between the files /sort sends back
SORT_FAST_FILES = 200  # /sort sends this many files at that pace; the rest of a long batch goes at SORT_SLOW_GAP
SORT_SLOW_GAP = 0.8  # about one file a second, what Telegram lets a bot keep sending into one chat without flood waits
SORT_LOOKUP = 100  # files /sort looks up per request (Telegram returns at most 100 messages at a time)
SORT_MAX_WAIT = 120  # a Telegram flood wait longer than this (seconds) stops /sort instead of being waited out
SORT_PROGRESS_EVERY = 10  # files between two updates of the "Sorting…" message
TRACKS_PROBE_BYTES = 12 * 1024 * 1024
TRACKS_FULL_LIMIT = 700 * 1024 * 1024
SHARE_PREFIX = "fmt_"  # /start fmt_<token> opens a shared format: it is the link on the card the Share format button makes
SHARE_QUERY = re.compile(r"share_(\d{1,9})")  # what that button types into the chosen chat: share_<format id>
SHARE_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,32}")
INLINE_RECHECK = 60  # seconds before the bot looks again whether inline mode is on, while it was off


@dataclass
class Pending:
    kind: str
    chat_id: int
    msg_id: int
    back: str
    is_photo: bool
    expires: float
    fid: int = 0  # the saved format a "fmt:*" prompt is about (0 = one that is being created)
    fmt_kind: str = ""  # the kind (auto | manual) of the format that is being created


@dataclass
class Mode:
    """What a user's next files are for: a /rename format session, /sequence, /ethumb or /smedia."""

    kind: str
    ttl: float
    expires: float
    data: Any = None
    dropped: str = ""  # a note about what starting this mode threw away, for the reply that announces it


def cmd_pattern(name: str) -> "re.Pattern[str]":
    return re.compile(rf"(?is)^/{name}(?:@\w+)?(?:\s+(.*))?$")


MEDIAINFO_CAPTION = cmd_pattern("mediainfo")  # a file sent with this as its caption is for /mediainfo, not for renaming


def visible_len(html_text: str) -> int:
    return len(html.unescape(re.sub(r"<[^>]+>", "", html_text)))


def plain(html_text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", html_text))


def is_renameable(message: Any) -> bool:
    return (
        getattr(message, "document", None) is not None
        and not (message.sticker or message.gif or message.voice or message.video_note)
    )


def is_image(message: Any) -> bool:
    """A photo, or a file that is a still picture (not a GIF animation)."""
    if getattr(message, "photo", None) is not None:
        return True
    f = getattr(message, "file", None)
    return bool(
        getattr(message, "document", None) is not None and not getattr(message, "gif", None)
        and f is not None and (f.mime_type or "").startswith("image/")
    )


def fits_as_photo(width: int, height: int, size: int) -> bool:
    """Does Telegram keep a picture of this many pixels and bytes as a photo, exactly as it is?  (It shrinks big ones.)"""
    longest, shortest = max(width, height), min(width, height)
    return longest <= PHOTO_MAX_SIDE and longest <= PHOTO_MAX_RATIO * shortest and size <= PHOTO_MAX_BYTES


def sent_as(message: Any) -> str:
    """How Telegram labels the file: a video, an audio file, a voice message ..., or a plain document."""
    for attr, label in (
        ("voice", "Voice message"), ("video_note", "Video message"), ("gif", "GIF"), ("sticker", "Sticker"),
        ("video", "Video"), ("audio", "Audio"),
    ):
        if getattr(message, attr, None) is not None:
            return label
    return "Document"


def is_playable(message: Any, name: str) -> bool:
    """A file that should hold audio or video streams, going by its MIME type and extension."""
    mime = (getattr(message.file, "mime_type", "") or "").lower()
    return mime.startswith(("video/", "audio/")) or source_ext(message, name).lower() in PROBE_EXTS


def telegram_facts(message: Any, caption: bool = True) -> Dict[str, str]:
    """What Telegram knows about the file, for the "Telegram" part of the /mediainfo page."""
    date = getattr(message, "date", None)
    stamp = ""
    if date is not None:
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        stamp = date.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return {
        "mime": getattr(message.file, "mime_type", "") or "",
        "sent_as": sent_as(message),
        "sent_on": stamp,
        "dc": str(getattr(message.document, "dc_id", "") or ""),
        "caption": clip((message.raw_text or "").strip(), 500) if caption else "",
    }


class App:
    PENDING_TTL = 300

    def __init__(self, client: Any, cfg: Config, db: Database):
        self.client = client
        self.cfg = cfg
        self.db = db
        self.admins: Set[int] = set(cfg.admin_ids)
        self.pending: Dict[int, Pending] = {}
        self.modes: Dict[int, Mode] = {}
        self._recent: Dict[int, List[Any]] = {}  # uid -> [command, finished_at, hint_sent] of the last one-file command
        self._sorting: Dict[int, asyncio.Event] = {}  # users whose /sort is sending files; set to stop it
        self._speed_lock = asyncio.Lock()  # one speed test at a time
        self.telegraph = Telegraph(self._load_telegraph_token, self._save_telegraph_token)
        self._tasks: Set["asyncio.Future[Any]"] = set()
        self.renamer = Renamer(client, cfg, db)
        self.queue = JobQueue(self.renamer.process, workers=cfg.max_jobs, on_cancelled=self._on_job_cancelled, settle=QUEUE_SETTLE)
        # uploading a manually renamed episode to the Database Channel (and having it published) needs both chats
        self.dbflow: Optional[DbFlow] = DbFlow(client, cfg) if cfg.upload_enabled else None
        if self.dbflow is not None:
            self.renamer.on_delivered = self.dbflow.offer
        self._warned: Set[int] = set()
        self._username: Optional[str] = None  # the bot's own @username, for the links on shared-format cards
        self._inline_on = False  # is inline mode on for the bot?  See _check_inline
        self._inline_checked: Optional[float] = None

    # ------------------------------------------------------------------ auth
    def is_owner(self, uid: Optional[int]) -> bool:
        return uid is not None and uid == self.cfg.owner_id

    def is_allowed(self, uid: Optional[int]) -> bool:
        return uid is not None and (uid == self.cfg.owner_id or uid in self.admins)

    # ----------------------------------------------------------------- setup
    async def setup(self) -> None:
        self.admins |= set(await self.db.list_admins())
        self.client.parse_mode = "html"
        self._register()
        await self._set_commands()
        await self._check_inline(force=True)
        if self.cfg.log_channel:
            try:
                await self.client.get_input_entity(self.cfg.log_channel)
            except Exception as exc:  # noqa: BLE001
                log.warning("cannot access LOG_CHANNEL %s: %s (%s)", self.cfg.log_channel, exc, SEE_THE_CHAT)
        await self._check_upload_chats()
        self.queue.start()

    async def _check_upload_chats(self) -> None:
        """Say in the log what the upload to the Database Channel cannot reach (or that it is switched off)."""
        cfg = self.cfg
        if self.dbflow is None:
            if cfg.db_channel or cfg.comm_chat:
                log.warning(
                    "uploading to the Database Channel is off: it needs both DB_CHANNEL (%s) and COMMUNICATION_CHAT (%s)",
                    cfg.db_channel or "missing", cfg.comm_chat or "missing",
                )
            return
        log.info(
            "uploading to the Database Channel is on: Database Channel %s, communication chat %s, channel post bot %s",
            cfg.db_channel, cfg.comm_chat, cfg.post_bot_id,
        )
        for name, ref in (("DB_CHANNEL", cfg.db_channel), ("COMMUNICATION_CHAT", cfg.comm_chat)):
            try:
                await self.client.get_input_entity(ref)
            except Exception as exc:  # noqa: BLE001
                log.warning("cannot access %s %s: %s (%s)", name, ref, exc, SEE_THE_CHAT)

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self.dbflow is not None:
            await self.dbflow.shutdown()
        await self.queue.stop()

    async def _set_commands(self) -> None:
        try:
            await self.client.set_bot_commands([types.BotCommand(command=c, description=d) for c, d in COMMANDS])
        except Exception as exc:  # noqa: BLE001
            log.warning("could not set the bot command menu: %s", exc)

    async def _log_send(self, text: str) -> None:
        if self.cfg.log_channel:
            await self.client.send_message(self.cfg.log_channel, text, parse_mode="html", link_preview=False)

    # --------------------------------------------------------------- plumbing
    def _guard(self, fn: Callable[[Any], Awaitable[None]], *, owner_only: bool = False):
        async def wrapper(event: Any) -> None:
            uid = event.sender_id
            allowed = self.is_owner(uid) if owner_only else self.is_allowed(uid)
            if not allowed:
                if owner_only and self.is_allowed(uid):  # a known user trying an owner command
                    try:
                        if hasattr(event, "data"):
                            await event.answer("Owner only.", alert=True)
                        else:
                            await event.respond("⛔ Only the owner can do that.")
                    except Exception:  # noqa: BLE001
                        pass
                else:
                    await self._stranger(uid)
                return
            context = errorlog.error_context.set(lambda: errorlog.describe_update(event))  # for the log channel report
            try:
                await fn(event)
            except events.StopPropagation:
                raise
            except errors.MessageNotModifiedError:
                pass
            except Exception:  # noqa: BLE001
                log.exception("Error handling update:")
                try:
                    if hasattr(event, "data"):
                        await event.answer("⚠️ Something went wrong.", alert=True)
                    else:
                        await event.respond("⚠️ Something went wrong. Please try again.")
                except Exception:  # noqa: BLE001
                    pass
            finally:
                errorlog.error_context.reset(context)

        return wrapper

    def _guard_inline(self, fn: Callable[[Any], Awaitable[None]]):
        guarded = self._guard(fn)

        async def wrapper(event: Any) -> None:
            if self.is_allowed(event.sender_id):
                await guarded(event)
            else:  # anyone can type the bot's name in a chat: strangers get no results, and no notice in the log
                await self._answer_inline(event, [])

        return wrapper

    async def _stranger(self, uid: Optional[int]) -> None:
        if uid is None or uid in self._warned:
            return
        self._warned.add(uid)
        log.info("ignored unauthorised user %s", uid)
        try:
            await self._log_send(f"🚫 Ignored a message from <code>{uid}</code>. Use /addadmin {uid} to allow them.")
        except Exception:  # noqa: BLE001
            pass

    def _register(self) -> None:
        add = self.client.add_event_handler
        for name, handler in self._command_table().items():
            add(
                self._guard(handler, owner_only=name in OWNER_COMMANDS),
                events.NewMessage(incoming=True, pattern=cmd_pattern(name), func=lambda e: e.is_private),
            )
        add(self._guard(self.on_media), events.NewMessage(incoming=True, func=lambda e: e.is_private and is_renameable(e.message)))
        add(self._guard(self.on_photo), events.NewMessage(incoming=True, func=lambda e: e.is_private and e.message.photo is not None))
        add(self._guard(self.on_gif), events.NewMessage(incoming=True, func=lambda e: e.is_private and e.message.gif is not None))
        add(self._guard(self.on_text), events.NewMessage(incoming=True, func=self._wants_text))
        add(self._guard(self.on_callback), events.CallbackQuery())
        add(self._guard_inline(self.on_inline), events.InlineQuery())
        if self.dbflow is not None:  # the channel post bot's answers in the communication chat (not a user of this bot: no guard)
            add(
                self.dbflow.on_chat_message,
                events.NewMessage(incoming=True, chats=[self.cfg.comm_chat], from_users=[self.cfg.post_bot_id]),
            )
        add(self._on_stranger, events.NewMessage(incoming=True, func=lambda e: e.is_private and not self.is_allowed(e.sender_id)))

    async def _on_stranger(self, event: Any) -> None:
        await self._stranger(event.sender_id)

    def _wants_text(self, e: Any) -> bool:
        text = e.raw_text or ""
        return bool(e.is_private and text and not text.startswith("/") and e.sender_id in self.pending and not e.message.media)

    def _command_table(self) -> Dict[str, Callable[[Any], Awaitable[None]]]:
        table: Dict[str, Callable[[Any], Awaitable[None]]] = {
            "start": self.cmd_start, "help": self.cmd_help, "rename": self.cmd_rename, "formats": self.cmd_formats, "format": self.cmd_formats,
            "sequence": self.cmd_sequence, "sort": self.cmd_sort, "mode": self.cmd_mode, "filetype": self.cmd_filetype,
            "metadata": self.cmd_metadata, "keepaudio": self._cmd_keep("keep_audio", "Keep Audio", "/keepaudio English Japanese"),
            "keepsubtitle": self._cmd_keep("keep_subtitle", "Keep Subtitle", "/keepsubtitle English"),
            "tracks": self.cmd_tracks, "check": self.cmd_check, "mediainfo": self.cmd_mediainfo, "dc": self.cmd_dc,
            "ethumb": self.cmd_ethumb, "size": self.cmd_size, "resize": self.cmd_resize,
            "removewords": self.cmd_remove, "setchannel": self.cmd_setchannel, "queue": self.cmd_queue,
            "cancel": self.cmd_cancel, "speed": self.cmd_speed, "status": self.cmd_status, "stats": self.cmd_stats,
            "addadmin": self.cmd_addadmin, "deladmin": self.cmd_deladmin, "admins": self.cmd_admins, "smedia": self.cmd_smedia,
        }
        for command, kind in META_COMMANDS.items():
            table[command] = self._cmd_meta_field(command, kind)
        return table

    @staticmethod
    def _args(event: Any) -> str:
        return (event.pattern_match.group(1) or "").strip() if event.pattern_match else ""

    @staticmethod
    def _caption_input(event: Any) -> str:
        """The caption the person sent.  Formatting they put on it in Telegram (bold, italic, links...) becomes the HTML that is
        stored, so nobody has to type tags; without any formatting it is taken as typed."""
        typed = event.raw_text or ""
        if typed.strip().lower() in CLEAR_WORDS:  # "reset" stays a reset even if it was made bold
            return typed
        entities = getattr(getattr(event, "message", None), "entities", None)
        return richtext.from_entities(typed, entities) or typed

    async def _reply(self, event: Any, text: str, rows: Optional[List[panels.Row]] = None) -> Any:
        buttons = panels.to_buttons(rows) if rows else None
        try:
            return await event.respond(text, buttons=buttons, parse_mode="html", link_preview=False)
        except errors.BotInlineDisabledError:  # inline mode was switched off in @BotFather: the Share button can't be shown
            plain_rows = panels.without_inline(rows or [])
            if plain_rows == rows:
                raise
            self._inline_refused()
            return await self._reply(event, text, plain_rows)

    # ----------------------------------------------------------------- panels
    async def panel(self, uid: int, name: str) -> panels.Panel:
        s = await self.db.get_settings(uid)
        if name == "home":
            return panels.home(s, owner=self.is_owner(uid), auto=await self.db.get_auto_format(uid))
        if name == "rename":
            return panels.rename(s, await self.db.get_auto_format(uid))
        if name == "meta":
            return panels.metadata(s)
        if name == "tool":
            return panels.tool(s)
        if name == "ftype":
            return panels.filetype(s)
        if name == "help":
            return panels.help_panel()
        if name == "formats":
            return panels.formats_home(
                await self.db.count_formats(uid, "auto"), await self.db.count_formats(uid, "manual"), MAX_FORMATS
            )
        if name == "auto":
            active = await self.db.get_auto_format(uid)
            return panels.auto_menu(await self.db.list_formats(uid, "auto"), active["id"] if active else 0, MAX_FORMATS)
        if name == "admin":
            return panels.admin(
                self.cfg.owner_id, sorted(self.admins), self.queue.running, self.queue.waiting, self.cfg.log_channel
            )
        raise KeyError(name)

    @staticmethod
    async def _remove_message(event: Any) -> None:
        try:
            await event.delete()
        except Exception:  # noqa: BLE001 - Telegram refuses to delete messages older than 48 hours
            try:
                await event.edit("Closed.", buttons=None)
            except Exception:  # noqa: BLE001
                pass

    async def _edit_panel(self, event: Any, text: str, rows: List[panels.Row]) -> None:
        try:
            await event.edit(text, buttons=panels.to_buttons(rows), parse_mode="html", link_preview=False)
        except errors.MessageNotModifiedError:
            pass
        except errors.BotInlineDisabledError:  # inline mode was switched off in @BotFather: the Share button can't be shown
            plain_rows = panels.without_inline(rows)
            if plain_rows == rows:
                raise
            self._inline_refused()
            await self._edit_panel(event, text, plain_rows)

    # ------------------------------------------------------------------ modes
    def _mode(self, uid: int, *kinds: str) -> Optional[Mode]:
        """The user's active mode (of one of ``kinds`` if given); an expired one is dropped."""
        mode = self.modes.get(uid)
        if mode is not None and mode.expires < time.time():
            self._end_mode(uid)
            return None
        if mode is None or (kinds and mode.kind not in kinds):
            return None
        return mode

    def _start_mode(self, uid: int, kind: str, ttl: float, data: Any = None) -> Mode:
        previous = self._end_mode(uid)
        mode = Mode(kind=kind, ttl=ttl, expires=time.time() + ttl, data=data)
        if previous is not None and previous.kind == "sequence" and previous.expires >= time.time() and previous.data.items:
            mode.dropped = f"⚠️ Your /sequence with {len(previous.data.items)} file(s) was discarded."
        self.modes[uid] = mode
        return mode

    def _end_mode(self, uid: int) -> Optional[Mode]:
        mode = self.modes.pop(uid, None)
        if mode is not None and isinstance(mode.data, SequenceState):
            mode.data.closed = True
        return mode

    def _spawn(self, coro: Awaitable[Any]) -> "asyncio.Future[Any]":
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _consume(self, uid: int, kind: str) -> Optional[Mode]:
        """Take the user's one-file mode.  Nothing awaits in here, so of several files that arrive together only one gets it."""
        mode = self._mode(uid, kind)
        if mode is None:
            return None
        del self.modes[uid]
        self._recent[uid] = [kind, time.time(), False]
        return mode

    def _extra_file_hint(self, uid: int, via: str = "doc") -> Optional[str]:
        """After a one-file command: ``""`` for a further file to skip silently, a hint for the first one, ``None`` once the grace is over.

        ``via`` is how the file arrived (``doc``, ``photo`` or ``gif``); only files the command itself would have taken are skipped.
        """
        rec = self._recent.get(uid)
        if rec is None:
            return None
        if time.time() - rec[1] > ONE_SHOT_GRACE:
            del self._recent[uid]
            return None
        if via not in ONE_SHOT_TAKES.get(rec[0], {"doc"}):
            return None
        if rec[2]:
            return ""
        rec[2] = True
        return f"ℹ️ /{rec[0]} handles one file at a time, so I skipped this one. Send /{rec[0]} again for the next file."

    # --------------------------------------------------------------- commands
    async def cmd_start(self, event: Any) -> None:
        self.pending.pop(event.sender_id, None)
        arg = self._args(event)
        if arg.startswith(SHARE_PREFIX):  # somebody opened the link on a shared format
            await self._open_shared(event, arg[len(SHARE_PREFIX):])
            return
        text, rows = await self.panel(event.sender_id, "home")
        if not await self._send_start_media(event.chat_id, text, rows):
            await self._reply(event, text, rows)

    async def cmd_help(self, event: Any) -> None:
        text, _ = panels.help_panel()
        await self._reply(event, text, [[("✖ Close", "x:close")]])

    async def cmd_mode(self, event: Any) -> None:
        arg = self._args(event).lower()
        uid = event.sender_id
        if arg in ("filename", "caption"):
            await self.db.update_settings(uid, mode=arg)
            await self._reply(event, f"✅ Rename mode: <code>{arg.upper()}</code>")
            return
        text, rows = await self.panel(uid, "rename")
        await self._reply(event, text, rows)

    async def cmd_filetype(self, event: Any) -> None:
        arg = self._args(event).lower()
        uid = event.sender_id
        aliases = {"original": "same", "file": "document", "doc": "document"}
        arg = aliases.get(arg, arg)
        if arg in ("same", "document", "video", "audio"):
            await self.db.update_settings(uid, file_type=arg)
            label = "same as original" if arg == "same" else arg
            await self._reply(event, f"✅ Files will be sent as: <code>{label}</code>")
            return
        text, rows = await self.panel(uid, "ftype")
        await self._reply(event, text, rows)

    async def cmd_metadata(self, event: Any) -> None:
        arg = self._args(event).lower()
        uid = event.sender_id
        if arg in ("on", "off"):
            await self.db.update_settings(uid, metadata_on=(arg == "on"))
            await self._reply(event, f"✅ Metadata is now <b>{arg.upper()}</b>")
            return
        text, rows = await self.panel(uid, "meta")
        await self._reply(event, text, rows)

    async def cmd_remove(self, event: Any) -> None:
        arg = self._args(event)
        uid = event.sender_id
        if arg:
            _ok, msg = await apply_setting(self.db, uid, "remove", arg)
            await self._reply(event, msg)
            return
        s = await self.db.get_settings(uid)
        words = ", ".join(s["remove_words"]) or "none"
        await self._reply(event, f"<b>Words stripped from names:</b> {esc(words)}\n\nSet: <code>/removewords @Channel, [Group]</code> · clear: <code>/removewords off</code>")

    def _cmd_meta_field(self, command: str, kind: str) -> Callable[[Any], Awaitable[None]]:
        async def handler(event: Any) -> None:
            arg = self._args(event)
            if not arg:
                s = await self.db.get_settings(event.sender_id)
                field = kind[3:]
                current = s["meta"].get(field) or "—"
                await self._reply(event, f"<b>{field.title()}:</b> <code>{esc(current)}</code>\n\nSet it: <code>/{command} value</code> · clear: <code>/{command} off</code>")
                return
            _ok, msg = await apply_setting(self.db, event.sender_id, kind, arg)
            await self._reply(event, msg)

        handler.__name__ = f"cmd_{command}"
        return handler

    def _cmd_keep(self, kind: str, label: str, example: str) -> Callable[[Any], Awaitable[None]]:
        async def handler(event: Any) -> None:
            arg = self._args(event)
            uid = event.sender_id
            if not arg:
                s = await self.db.get_settings(uid)
                await self._reply(
                    event,
                    f"<b>{label}:</b> {esc(panels.fmt_tokens(s[kind]))}\n\n"
                    f"Set it with language names or track numbers:\n<code>{example}</code>\n<code>{example.split()[0]} 2 1</code>\n"
                    f"Only the listed tracks are kept, in that order. <code>{example.split()[0]} off</code> keeps everything.\n"
                    "Reply to a file with /tracks to see its track numbers.",
                )
                return
            _ok, msg = await apply_setting(self.db, uid, kind, arg)
            await self._reply(event, msg)

        handler.__name__ = f"cmd_{kind}"
        return handler

    async def cmd_setchannel(self, event: Any) -> None:
        arg = self._args(event)
        uid = event.sender_id
        if not arg:
            s = await self.db.get_settings(uid)
            current = f"<code>{s['target_channel']}</code>" if s["target_channel"] else "not set (files come back to this chat)"
            await self._reply(event, f"<b>Send renamed files to:</b> {current}\n\nSet: <code>/setchannel -1001234567890</code> (add me to the channel as an admin first) · clear: <code>/setchannel off</code>")
            return
        ok, cid, err = parse_channel(arg)
        if not ok:
            await self._reply(event, err)
            return
        if cid is not None:
            try:
                await self.client.get_input_entity(cid)
            except Exception:  # noqa: BLE001
                await self._reply(event, "❌ I can't access that chat. Add me to the channel as an admin that can post and send any message there (I can use a chat only after I have seen something from it), then send the id again.")
                return
        await self.db.update_settings(uid, target_channel=cid)
        await self._reply(event, f"✅ Renamed files will go to <code>{cid}</code>." if cid else "✅ Renamed files will come back to this chat.")

    async def on_photo(self, event: Any) -> None:
        """A photo is taken only when a format is waiting for its thumbnail (or /smedia for its picture); any other one is ignored."""
        uid = event.sender_id
        p = self.pending.get(uid)
        if not (p is not None and p.kind == "fmt:thumb" and p.expires >= time.time()):
            if await self._image_mode(event):
                return
            hint = self._extra_file_hint(uid, "photo")
            if hint:
                await self._reply(event, hint)
            return
        raw = await event.download_media(bytes)
        if not raw:
            await self._reply(event, "❌ I couldn't download that photo.")
            return
        try:
            thumb = await asyncio.to_thread(media.prepare_thumb, raw)
        except Exception:  # noqa: BLE001
            await self._reply(event, "❌ I couldn't read that image.")
            return
        await self._format_thumb_saved(event, p, thumb)

    async def on_gif(self, event: Any) -> None:
        if await self._image_mode(event):
            return
        hint = self._extra_file_hint(event.sender_id, "gif")
        if hint:
            await self._reply(event, hint)

    # ------------------------------------------------------------ rename flow
    async def cmd_formats(self, event: Any) -> None:
        self.pending.pop(event.sender_id, None)
        text, rows = await self.panel(event.sender_id, "formats")
        await self._reply(event, text, rows)

    async def cmd_rename(self, event: Any) -> None:
        uid = event.sender_id
        self.pending.pop(uid, None)
        text, rows = panels.rename_picker(await self.db.list_formats(uid, "manual"))
        await self._reply(event, text, rows)

    async def on_media(self, event: Any) -> None:
        uid = event.sender_id
        if MEDIAINFO_CAPTION.match(event.raw_text or ""):
            return  # cmd_mediainfo has it
        mode = self._consume(uid, "rename")
        if mode is not None:
            await self._media_rename(event, mode)
            return
        mode = self._consume(uid, "ethumb")
        if mode is not None:
            await self._send_thumbnail(event, event.message)
            return
        if await self._image_mode(event):
            return
        mode = self._mode(uid, "sequence")
        if mode is not None:
            notice = self._collect(event, mode)
            if notice:
                await self._reply(event, notice)
            return
        hint = self._extra_file_hint(uid)
        if hint is not None:
            if hint:
                await self._reply(event, hint)
            return
        await self.enqueue(event, event.message, fmt=await self.db.get_auto_format(uid))  # None: no auto format, the defaults

    async def _media_rename(self, event: Any, mode: Mode) -> None:
        fmt = await self.db.get_format(event.sender_id, mode.data["fid"])
        if fmt is None or fmt["kind"] != "manual":
            await self._reply(event, "⚠️ That format was deleted. Use /rename to pick another one, then send the file again.")
            return
        await self.enqueue(event, event.message, fmt=fmt)

    # --------------------------------------------------------- /sequence, /sort
    async def cmd_sequence(self, event: Any) -> None:
        uid = event.sender_id
        self.pending.pop(uid, None)
        mode = self._mode(uid, "sequence")
        if mode is not None and mode.data.items:  # a second /sequence must not throw the collected files away
            mode.expires = time.time() + mode.ttl
            text, rows = panels.sequence_status(len(mode.data.items), SEQUENCE_MAX)
            await self._reply(event, "You're already collecting files for a sequence.\n\n" + text, rows)
            return
        mode = self._start_mode(uid, "sequence", SEQUENCE_WAIT, SequenceState())
        text, rows = panels.sequence_intro(SEQUENCE_MAX)
        await self._reply(event, text + (f"\n\n{mode.dropped}" if mode.dropped else ""), rows)

    def _collect(self, event: Any, mode: Mode) -> Optional[str]:
        """Add a file to the user's sequence.  Nothing awaits in here, so files that arrive together are all kept."""
        state: SequenceState = mode.data
        mode.expires = time.time() + mode.ttl
        if len(state.items) >= SEQUENCE_MAX:
            if state.full_noted:
                return None
            state.full_noted = True
            return f"⚠️ That's the limit: {SEQUENCE_MAX} files. Send /sort now, or /cancel to start over."
        msg = event.message
        state.items.append(SeqItem(msg_id=msg.id, name=original_name_of(msg), caption=msg.raw_text or ""))
        state.version += 1
        if state.task is None or state.task.done():
            state.task = self._spawn(self._sequence_status(event.chat_id, state))
        return None

    async def _sequence_status(self, chat_id: int, state: SequenceState) -> None:
        """Send "N files collected" once the files stop arriving, so it is the newest message in the chat."""
        try:
            while not state.closed:
                seen = state.version
                await asyncio.sleep(SEQUENCE_STATUS_DELAY)
                if state.closed:
                    return
                if state.version != seen:
                    continue
                text, rows = panels.sequence_status(len(state.items), SEQUENCE_MAX)
                previous = state.status_id
                sent = await self.client.send_message(chat_id, text, buttons=panels.to_buttons(rows), parse_mode="html")
                if state.closed:  # /sort or /cancel came while it was being sent
                    await self._delete_quietly(chat_id, sent.id)
                    return
                state.status_id = sent.id
                if previous is not None:
                    await self._delete_quietly(chat_id, previous)
                if state.version == seen:
                    return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("could not send the sequence status")

    async def _delete_quietly(self, chat_id: int, *ids: int) -> None:
        try:
            await self.client.delete_messages(chat_id, list(ids))
        except Exception as exc:  # noqa: BLE001
            log.debug("could not delete messages %s: %s", ids, exc)

    async def cmd_sort(self, event: Any) -> None:
        await self._run_sort(event.sender_id, event.chat_id)

    @staticmethod
    async def _pause(seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def _send_back(self, chat_id: int, msg: Any) -> None:
        """Send one collected file again (no re-upload), keeping its caption and formatting."""
        for attempt in range(3):
            try:
                await self.client.send_file(chat_id, msg, caption=msg.raw_text or "", formatting_entities=msg.entities, parse_mode=None)
                return
            except errors.FloodWaitError as exc:
                if attempt == 2 or exc.seconds > SORT_MAX_WAIT:
                    raise
                await self._pause(exc.seconds + 1)

    async def _look_up(self, chat_id: int, items: List[SeqItem]) -> List[Any]:
        """The messages behind ``items``, in that order (``None`` for one that is gone or could not be looked up)."""
        try:
            found = list(await self.client.get_messages(chat_id, ids=[i.msg_id for i in items]))
        except Exception:  # noqa: BLE001
            log.exception("could not look up %d files to sort", len(items))
            found = []
        return found + [None] * (len(items) - len(found))

    async def _run_sort(self, uid: int, chat_id: int) -> None:
        say = lambda text, **kw: self.client.send_message(chat_id, text, parse_mode="html", link_preview=False, **kw)  # noqa: E731
        if uid in self._sorting:
            await say("⏳ I'm still sorting your last batch. /cancel to stop it.")
            return
        mode = self._mode(uid, "sequence")
        state: Optional[SequenceState] = mode.data if mode is not None else None
        if state is None or not state.items:
            await say("Nothing to sort yet. Send /sequence, then the files, then /sort.")
            return
        self._end_mode(uid)
        cancel = self._sorting[uid] = asyncio.Event()
        total = len(state.items)
        status = None
        try:
            if state.status_id is not None:
                await self._delete_quietly(chat_id, state.status_id)
            s = await self.db.get_settings(uid)
            plan = analyse(state.items, s["mode"])
            status = await say(f"🔢 <b>Sorting {total} file{'s' if total != 1 else ''}…</b>")

            sent = 0
            failed: List[SeqItem] = []
            note = ""
            stopped = False
            found: List[Any] = []  # the messages of the files being sent, looked up SORT_LOOKUP at a time as the sort goes
            for index, item in enumerate(plan.ordered):
                if cancel.is_set():
                    stopped = True
                    break
                if index % SORT_LOOKUP == 0:
                    found = await self._look_up(chat_id, plan.ordered[index:index + SORT_LOOKUP])
                msg = found[index % SORT_LOOKUP]
                if msg is None or getattr(msg, "file", None) is None:  # deleted since it was sent
                    failed.append(item)
                    continue
                try:
                    await self._send_back(chat_id, msg)
                except errors.FloodWaitError as exc:
                    note = f"⚠️ Telegram asked me to wait {human_uptime(exc.seconds)} before sending more, so I stopped here."
                    failed.extend(plan.ordered[index:])
                    break
                except Exception as exc:  # noqa: BLE001
                    log.warning("could not send %s back: %s", item.name, exc)
                    failed.append(item)
                    continue
                sent += 1
                if sent % SORT_PROGRESS_EVERY == 0 and sent < total:
                    try:
                        await self.client.edit_message(status, f"🔢 <b>Sorting…</b> {sent} of {total} sent", parse_mode="html")
                    except Exception:  # noqa: BLE001
                        pass
                gap = SORT_GAP if sent < SORT_FAST_FILES else SORT_SLOW_GAP
                if gap:
                    await self._pause(gap)
            if status is not None:
                await self._delete_quietly(chat_id, status.id)
            if stopped:
                await say(f"⏹ Stopped after {sent} of {total} file{'s' if total != 1 else ''}.")
                return
            for chunk in format_report(plan, total, sent, failed, note):
                await say(chunk)
        finally:
            self._sorting.pop(uid, None)

    async def enqueue(self, event: Any, msg: Any, fmt: Optional[Dict[str, Any]] = None) -> None:
        uid = event.sender_id
        size = int(msg.file.size or 0) if msg.file is not None else 0
        if size > self.cfg.max_file_bytes:
            await self._reply(event, f"❌ That file is {human_size(size)}; the limit is {human_size(self.cfg.max_file_bytes)}.")
            return
        s = await self.db.get_settings(uid)
        orig = original_name_of(msg)
        ext = source_ext(msg, orig)
        template = fmt["template"] if fmt else default_auto_format()["template"]
        new_name, info = compute_new_name(
            template=template, mode=s["mode"], original_name=orig, original_ext=ext,
            caption=msg.raw_text or "", remove_words=s["remove_words"],
        )
        notes: List[str] = []
        gaps = missing_variables(template, info)
        if gaps:
            where_from = "caption" if s["mode"] == "caption" else "file name"
            notes.append(
                "Couldn't find " + ", ".join("{%s}" % g for g in gaps) + f" in the {where_from}, so it is left empty. "
                "Cancel and change the template if you don't want that."
            )
        job = Job(
            id=self.queue.new_id(), user_id=uid, chat_id=event.chat_id, msg_id=msg.id,
            file_name=orig, size=size, new_name=new_name, fmt=fmt, notes=notes, order=event.id,
        )
        ahead = self.queue.running + self.queue.waiting
        where = "starting soon" if ahead == 0 else f"{ahead} ahead of it"
        used = f"\n🗂 {'Auto format' if fmt['kind'] == 'auto' else 'Format'}: <b>{esc(clip(fmt['name'], 30))}</b>" if fmt else ""
        job.status_msg = await event.reply(
            f"📥 <b>Queued</b> ({where}){used}\n<code>{esc(clip(new_name, 120))}</code>" + "".join(f"\n⚠️ {esc(n)}" for n in notes),
            buttons=panels.to_buttons([[("✖ Cancel", f"job:x:{job.id}")]]),
            parse_mode="html",
        )
        self.queue.add(job)

    async def _on_job_cancelled(self, job: Job) -> None:
        if job.status_msg is not None:
            await self.client.edit_message(
                job.status_msg, f"❌ <b>Cancelled</b>\n<code>{esc(clip(job.new_name, 120))}</code>", buttons=None, parse_mode="html"
            )

    async def cmd_queue(self, event: Any) -> None:
        uid = event.sender_id
        jobs = sorted(self.queue.jobs.values(), key=lambda j: j.id) if self.is_owner(uid) else self.queue.for_user(uid)
        if not jobs:
            await self._reply(event, "Nothing in the queue.")
            return
        lines = ["<b>Queue</b>"]
        for j in jobs[:20]:
            icon = "⚙️" if j.state == "running" else "⏳"
            who = f" · <code>{j.user_id}</code>" if self.is_owner(uid) else ""
            lines.append(f"{icon} #{j.id}{who} · <code>{esc(clip(j.new_name, 60))}</code>")
        if len(jobs) > 20:
            lines.append(f"… and {len(jobs) - 20} more")
        lines.append("\n/cancel — cancel yours · <code>/cancel 3</code> — cancel job #3")
        await self._reply(event, "\n".join(lines))

    async def cmd_cancel(self, event: Any) -> None:
        uid = event.sender_id
        arg = self._args(event).lower()
        if not arg and self.pending.pop(uid, None) is not None:
            await self._reply(event, "Cancelled — nothing was changed.")
            return
        if not arg and uid in self._sorting:
            self._sorting[uid].set()
            await self._reply(event, "⏹ Stopping the sort…")
            return
        if not arg:
            mode = self._end_mode(uid)
            if mode is not None and mode.expires >= time.time():
                if isinstance(mode.data, SequenceState) and mode.data.status_id is not None:
                    await self._delete_quietly(event.chat_id, mode.data.status_id)  # its buttons are of no use any more
                busy = " Send /cancel again to cancel your queued files." if self.queue.for_user(uid) else ""
                await self._reply(event, self._mode_cancelled_text(mode) + busy)
                return
        owner_scope = None if self.is_owner(uid) else uid
        if arg.isdigit():
            ok = self.queue.cancel(int(arg), user_id=owner_scope)
            await self._reply(event, "🛑 Cancelling…" if ok else "I couldn't find an active job with that number.")
            return
        count = self.queue.cancel_all() if (arg == "all" and self.is_owner(uid)) else self.queue.cancel_user(uid)
        await self._reply(event, f"🛑 Cancelling {count} job(s)…" if count else "You have no active jobs.")

    @staticmethod
    def _mode_cancelled_text(mode: Mode) -> str:
        if mode.kind == "sequence":
            count = len(mode.data.items)
            return f"⏹ Sequence cancelled. {count} file{'s' if count != 1 else ''} discarded." if count else "⏹ Sequence cancelled."
        return {
            "rename": "⏹ Cancelled. Send /rename again to pick a format.",
            "smedia": "⏹ Cancelled. Send /smedia again to change the start media.",
        }.get(mode.kind, "Cancelled.")

    # ------------------------------------------------------------------ check
    async def cmd_check(self, event: Any) -> None:
        reply = await event.get_reply_message()
        if reply is None or not is_renameable(reply):
            await self._reply(event, "Reply to a file with /check to see the details I can read from its name and caption.")
            return
        s = await self.db.get_settings(event.sender_id)
        auto = await self.db.get_auto_format(event.sender_id) or default_auto_format()
        name = original_name_of(reply)
        caption = reply.raw_text or ""
        new_name, _info = compute_new_name(
            template=auto["template"], mode=s["mode"], original_name=name, original_ext=source_ext(reply, name),
            caption=caption, remove_words=s["remove_words"],
        )
        text = panels.check_text(s["mode"], parse_info(split_ext(name)[0]), parse_caption(caption), new_name)
        await event.reply(text, parse_mode="html", link_preview=False)

    async def cmd_dc(self, event: Any) -> None:
        """Reply to a file, video or picture with /dc: the data center it is stored on, and the one the bot is connected to."""
        reply = await event.get_reply_message()
        if reply is None or not reply.media:
            await self._reply(event, "Reply to a file, video or picture with /dc to see which data center it is stored on.")
            return
        dc_id = datacenter.of_message(reply)
        if dc_id is None:
            await self._reply(event, "❌ There's no file in that.")
            return
        lines = ["<b>📡 Data center</b>", f"◈ <b>File</b> ▸ {datacenter.label(dc_id, place=True)}"]
        home = datacenter.home_of(self.client)
        if home is not None:
            lines.append(f"◈ <b>Bot</b> ▸ {datacenter.label(home, place=True)}")
        await self._reply(event, "\n".join(lines))

    # ------------------------------------------------------------------ tracks
    async def _with_file_start(
        self, event: Any, msg: Any, tag: str,
        attempt: Callable[[Path], Awaitable[Any]], on_whole: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> Any:
        """Run ``attempt(path)`` on the first part of the file; when it gives nothing (an index at the end of an mp4, say) on the whole file.

        The files live in a temporary folder that is removed afterwards.  ``on_whole`` runs before the big download starts.
        """
        name = original_name_of(msg)
        size = int(msg.file.size or 0)
        tmp_dir = Path(self.cfg.temp_dir) / f"{tag}{event.chat_id}_{event.id}"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        try:
            path = tmp_dir / f"probe{source_ext(msg, name)}"
            await self.client.download_to(msg, path, max_bytes=TRACKS_PROBE_BYTES)
            result = await attempt(path)
            if result is None and TRACKS_PROBE_BYTES < size <= TRACKS_FULL_LIMIT:
                if on_whole is not None:
                    await on_whole()
                await self.client.download_to(msg, path)
                result = await attempt(path)
            return result
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    async def cmd_tracks(self, event: Any) -> None:
        reply = await event.get_reply_message()
        if reply is None or not is_renameable(reply):
            await self._reply(event, "Reply to a video or audio file with /tracks to list its audio and subtitle tracks.")
            return
        status = await self._reply(event, "🔎 Reading the tracks…")

        async def read(path: Path) -> Optional[media.MediaInfo]:
            try:
                info = await media.probe(path)
            except media.MediaError:
                return None
            return info if info.streams else None

        async def whole() -> None:
            await self.client.edit_message(status, "🔎 Reading the tracks (this one needs the whole file)…", parse_mode="html")

        info = await self._with_file_start(event, reply, "tracks", read, whole)
        if info is None:
            await self.client.edit_message(status, "❌ I couldn't read any tracks from that file.", parse_mode="html")
            return
        text = f"<b>{esc(clip(original_name_of(reply), 80))}</b>\n\n{media.format_tracks(info)}"
        await self.client.edit_message(status, text[:3900], parse_mode="html")

    # --------------------------------------------------------------- /mediainfo
    async def _load_telegraph_token(self) -> Optional[str]:
        return await self.db.kv_get("telegraph_token")

    async def _save_telegraph_token(self, token: str) -> None:
        if token:
            await self.db.kv_set("telegraph_token", token)
        else:
            await self.db.kv_delete("telegraph_token")

    async def cmd_mediainfo(self, event: Any) -> None:
        target, own = await event.get_reply_message(), False
        if target is None or getattr(target, "document", None) is None:
            target, own = event.message, True  # a file sent with /mediainfo as its caption
        if getattr(target, "document", None) is None:
            await self._reply(event, "Reply to a file or video with /mediainfo and I'll put everything MediaInfo can read from it on a Telegraph page.")
            return
        status = await self._reply(event, "🔎 Reading the file…")
        try:
            await self._mediainfo(event, target, own, status)
        except Exception:  # noqa: BLE001
            log.exception("/mediainfo failed")
            try:
                await self.client.edit_message(status, "⚠️ Something went wrong while reading that file.", parse_mode="html")
            except Exception:  # noqa: BLE001
                pass

    async def _mediainfo(self, event: Any, msg: Any, own: bool, status: Any) -> None:
        name = original_name_of(msg)
        size = int(msg.file.size or 0)
        playable = is_playable(msg, name)
        extra = mediainfo.telegram_section(name, size, **telegram_facts(msg, caption=not own))
        last: List[mediainfo.Report] = []

        async def read(path: Path) -> Optional[mediainfo.Report]:
            report = await mediainfo.read_file(path, name, size, extra=extra)
            last[:] = [report]
            return report if report.has_streams or not playable else None

        async def whole() -> None:
            await self.client.edit_message(status, "🔎 Reading the file (this one needs the whole file)…", parse_mode="html")

        try:
            report = await self._with_file_start(event, msg, "minfo", read, whole) or (last[0] if last else None)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read %s for /mediainfo: %s", name, exc)
            report = None
        if report is None:
            await self.client.edit_message(status, "❌ I couldn't read that file.", parse_mode="html")
            return
        if playable and not report.has_streams:
            report.notes.append(
                "No tracks could be read from this file, and it is too big for me to download completely."
                if size > TRACKS_FULL_LIMIT else "No tracks could be read from this file. It may be damaged."
            )
        try:
            url = await self.telegraph.create_page(clip(f"MediaInfo – {name}", 256), mediainfo.to_nodes(report))
        except Exception as exc:  # noqa: BLE001
            log.warning("Telegraph did not take the /mediainfo page: %s", exc)
            why = str(exc) if isinstance(exc, TelegraphError) else "Telegraph is not available"
            await self._mediainfo_file(event, status, name, report, why)
            return
        summary = report.summary()
        text = f"🔎 <b>MediaInfo</b> · {esc(clip(name, 80))}\n" + (f"<i>{esc(summary)}</i>\n" if summary else "") + f"\n{esc(url)}"
        await self.client.edit_message(status, text, buttons=[[Button.url("📄 Open MediaInfo", url)]], parse_mode="html", link_preview=False)

    async def _mediainfo_file(self, event: Any, status: Any, name: str, report: mediainfo.Report, why: str) -> None:
        """Telegraph did not work: send the same report as a text file instead."""
        bio = io.BytesIO(mediainfo.render_text(report).encode("utf-8"))
        bio.name = re.sub(r'[\\/:*?"<>|]+', "_", clip(split_ext(name)[0], 60)) + " - MediaInfo.txt"
        caption = f"🔎 <b>MediaInfo</b> · {esc(clip(name, 80))}\n<i>{esc(why)}, so here it is as a text file.</i>"
        await self.client.send_file(event.chat_id, bio, caption=caption, parse_mode="html", force_document=True)
        await self._delete_quietly(event.chat_id, status.id)

    # ----------------------------------------------------------- /ethumb, /resize
    async def cmd_ethumb(self, event: Any) -> None:
        uid = event.sender_id
        self.pending.pop(uid, None)
        reply = await event.get_reply_message()
        if reply is not None and is_renameable(reply):  # sent as a reply to the file: no need to ask for it
            await self._send_thumbnail(event, reply)
            return
        mode = self._start_mode(uid, "ethumb", ONE_FILE_WAIT)
        text, rows = panels.ethumb_prompt(ONE_FILE_WAIT // 60)
        await self._reply(event, text + (f"\n\n{mode.dropped}" if mode.dropped else ""), rows)

    async def _embedded_thumb(self, msg: Any, allow_stripped: bool = False) -> Optional[bytes]:
        return await embedded_thumb(self.client, msg, allow_stripped)

    async def _extract_thumbnail(self, event: Any, msg: Any) -> Optional[bytes]:
        data = await self._embedded_thumb(msg)
        if data:
            return data
        name = original_name_of(msg)
        playable = getattr(msg, "video", None) is not None or getattr(msg, "audio", None) is not None
        if playable or source_ext(msg, name).lower() in PROBE_EXTS:
            async def frame(path: Path) -> Optional[bytes]:
                out = path.with_suffix(".jpg")
                try:
                    duration = (await media.probe(path)).duration
                except media.MediaError:
                    duration = 0.0  # cut off before the index: ffmpeg may still find a picture
                ok = await media.extract_frame(path, out, media.frame_seeks(duration), width=ETHUMB_FRAME_WIDTH)
                return out.read_bytes() if ok else None

            data = await self._with_file_start(event, msg, "ethumb", frame)
            if data:
                return data
        return await self._embedded_thumb(msg, allow_stripped=True)

    async def _send_thumbnail(self, event: Any, msg: Any) -> None:
        status = await self._reply(event, "🖼 Extracting the thumbnail…")
        name = original_name_of(msg)
        try:
            data = await self._extract_thumbnail(event, msg)
        except Exception as exc:  # noqa: BLE001
            log.warning("thumbnail extraction failed for %s: %s", name, exc)
            data = None
        if not data:
            await self.client.edit_message(status, "❌ I couldn't find a thumbnail in that file or grab a frame from it.", parse_mode="html")
            return
        bio = io.BytesIO(data)
        bio.name = "thumbnail.jpg"
        await self.client.send_file(event.chat_id, bio, caption=f"🖼 Thumbnail of <b>{esc(clip(name, 80))}</b>", parse_mode="html")
        await self._delete_quietly(event.chat_id, status.id)

    async def _image_mode(self, event: Any) -> bool:
        """Give an incoming picture or GIF to /smedia when it is waiting for one.  True when the message was taken care of."""
        mode = self._mode(event.sender_id, "smedia")
        if mode is None:
            return False
        await self._take_start_media(event, mode)
        return True

    # ----------------------------------------------------------------- /size, /resize
    async def _picture_of(self, msg: Any) -> Tuple[Optional[bytes], str]:
        """``(the picture, "")`` of a message, or ``(None, why there is none)``.

        A photo, and a file that is a picture, are taken whole.  Anything else (a video, a GIF, a sticker ...) gives the largest
        preview Telegram keeps with it.
        """
        if getattr(msg, "photo", None) is not None or is_image(msg):
            size = int(msg.file.size or 0) if getattr(msg, "file", None) is not None else 0
            if size > RESIZE_MAX_BYTES:
                return None, f"❌ That picture is {human_size(size)}; I can read pictures up to {human_size(RESIZE_MAX_BYTES)}."
            raw = await self.client.download_media(msg, file=bytes)
        elif getattr(msg, "document", None) is not None:
            raw = await self.client.download_media(msg, file=bytes, thumb=-1)
        else:
            return None, "❌ There's no picture in that."
        return (raw, "") if raw else (None, "❌ I couldn't get a picture from that.")

    async def _remove_later(self, chat_id: int, msg_id: int, seconds: float) -> None:
        await asyncio.sleep(seconds)
        await self._delete_quietly(chat_id, msg_id)

    async def cmd_size(self, event: Any) -> None:
        """Reply to a picture (or to anything with a preview) with /size: how many pixels wide and high it is."""
        reply = await event.get_reply_message()
        if reply is None or not reply.media:
            await self._reply(event, "Reply to a picture or other media with /size to get its dimensions.")
            return
        status = await self._reply(event, "⏳ Processing…")
        raw, why = await self._picture_of(reply)
        if raw is None:
            await self.client.edit_message(status, why, parse_mode="html")
            return
        try:
            width, height = await asyncio.to_thread(media.image_size, raw)
        except Exception:  # noqa: BLE001
            await self.client.edit_message(status, "❌ I couldn't read that image.", parse_mode="html")
            return
        await self.client.edit_message(status, f"Dimension Of This Image Is\n<code>{width} x {height}</code>", parse_mode="html")

    async def cmd_resize(self, event: Any) -> None:
        """Reply to a picture with ``/resize 720 1080``: it comes back at exactly that many pixels wide and high (its shape is not kept)."""
        reply = await event.get_reply_message()
        if reply is None or not reply.media:
            await self._reply(event, "Reply to a picture or other media with /resize and a size, like <code>/resize 720 1080</code>.")
            return
        sides = self._args(event).split()
        if len(sides) != 2 or not all(re.fullmatch(r"[0-9]{1,6}", side) for side in sides):
            hint = await self._reply(event, "Give Some Size To Resize, Like <code>/resize 720 1080</code>")
            self._spawn(self._remove_later(event.chat_id, hint.id, HINT_SECONDS))  # it goes away by itself
            return
        width, height = int(sides[0]), int(sides[1])
        if not (1 <= width <= RESIZE_MAX_SIDE and 1 <= height <= RESIZE_MAX_SIDE):
            await self._reply(event, f"❌ Each side can be 1 to {RESIZE_MAX_SIDE} pixels, like <code>/resize 720 1080</code>.")
            return
        status = await self._reply(event, "⏳ Processing…")
        raw, why = await self._picture_of(reply)
        if raw is None:
            await self.client.edit_message(status, why, parse_mode="html")
            return
        try:
            data = await asyncio.to_thread(media.resize_exact, raw, width, height)
        except media.ImageTooBig:
            await self.client.edit_message(status, "❌ That picture has too many pixels for me to resize.", parse_mode="html")
            return
        except Exception:  # noqa: BLE001
            await self.client.edit_message(status, "❌ I couldn't read that image.", parse_mode="html")
            return
        bio = io.BytesIO(data)
        bio.name = "resized.png"
        try:  # as a photo when Telegram keeps it as it is, otherwise as a file, so the size stays exact
            await event.reply(file=bio, force_document=not fits_as_photo(width, height, len(data)))
        except Exception:  # noqa: BLE001
            log.exception("could not send the resized picture")
            await self.client.edit_message(status, "❌ Telegram wouldn't take the resized picture.", parse_mode="html")
            return
        await self._delete_quietly(event.chat_id, status.id)

    # ------------------------------------------------------- /smedia, the /start picture
    async def _start_media_meta(self) -> Optional[Dict[str, Any]]:
        """What /start shows above the menu: ``{"kind": "image" | "gif", "file_id": ..., ...}``, or None."""
        raw = await self.db.kv_get("start_media")
        if not raw:
            return None
        try:
            meta = json.loads(raw)
        except ValueError:
            return None
        return meta if isinstance(meta, dict) and meta.get("kind") in ("image", "gif") else None

    async def _store_start_media(self, meta: str, data: str) -> None:
        """Keep the /start media (its description and a base64 copy of the file); empty strings forget it."""
        for key, value in (("start_media", meta), ("start_media_data", data)):
            if value:
                await self.db.kv_set(key, value)
            else:
                await self.db.kv_delete(key)

    async def _send_start_media(self, chat_id: int, text: str, rows: List[panels.Row]) -> bool:
        """Send the menu with the owner's picture or GIF above it.  False when there is none or it can't be sent."""
        meta = await self._start_media_meta()
        if meta is None:
            return False
        kw: Dict[str, Any] = dict(caption=text, buttons=panels.to_buttons(rows), parse_mode="html")
        sent = None
        if meta.get("file_id"):
            try:
                sent = await self.client.send_file(chat_id, meta["file_id"], **kw)
            except Exception as exc:  # noqa: BLE001 - a file reference that went stale: the stored copy is used instead
                log.warning("the start media could not be sent by its file id (%s); sending the stored copy", exc)
        if sent is None:
            try:
                data = base64.b64decode(await self.db.kv_get("start_media_data") or "", validate=True)
                if not data:
                    return False
                sent = await self._upload_start_media(chat_id, meta, data, **kw)
            except Exception as exc:  # noqa: BLE001
                log.warning("could not send the start media (%s); /start shows the menu only", exc)
                return False
            fresh = getattr(getattr(sent, "file", None), "id", None)
            if fresh and fresh != meta.get("file_id"):  # the next /start can reuse what was just uploaded
                await self.db.kv_set("start_media", json.dumps({**meta, "file_id": fresh}))
        return True

    async def _upload_start_media(self, chat_id: int, meta: Dict[str, Any], data: bytes, **kw: Any) -> Any:
        bio = io.BytesIO(data)
        if meta["kind"] == "gif":
            bio.name = "start.mp4"
            kw.update(
                mime_type=meta.get("mime") or "video/mp4",
                attributes=[
                    types.DocumentAttributeVideo(
                        duration=float(meta.get("duration") or 0), w=int(meta.get("width") or 0), h=int(meta.get("height") or 0),
                        supports_streaming=True,
                    ),
                    types.DocumentAttributeAnimated(),
                ],
            )
        else:
            bio.name = "start.jpg"
        return await self.client.send_file(chat_id, bio, **kw)

    async def cmd_smedia(self, event: Any) -> None:
        self.pending.pop(event.sender_id, None)
        meta = await self._start_media_meta()
        text, rows = panels.smedia_menu(meta["kind"] if meta else "")
        await self._reply(event, text, rows)

    async def _cb_smedia(self, event: Any, uid: int, parts: List[str]) -> None:
        if not self.is_owner(uid):
            await event.answer("Owner only.", alert=True)
            return
        self.pending.pop(uid, None)
        action = parts[1] if len(parts) > 1 else ""
        if action in ("image", "gif"):
            mode = self._start_mode(uid, "smedia", ONE_FILE_WAIT, {"kind": action})
            text, rows = panels.smedia_prompt(action, ONE_FILE_WAIT // 60)
            await self._edit_panel(event, text + (f"\n\n{mode.dropped}" if mode.dropped else ""), rows)
            await event.answer()
        elif action == "menu":
            if self._mode(uid, "smedia") is not None:
                self._end_mode(uid)
            meta = await self._start_media_meta()
            text, rows = panels.smedia_menu(meta["kind"] if meta else "")
            await self._edit_panel(event, text, rows)
            await event.answer()
        elif action == "rm":
            await self._store_start_media("", "")
            text, rows = panels.smedia_menu("")
            await self._edit_panel(event, text, rows)
            await event.answer("Start media removed.")
        elif action == "stop":
            if self._mode(uid, "smedia") is not None:
                self._end_mode(uid)
            await self._edit_panel(event, "⏹ Stopped. Send /smedia again to change the start media.", [[("✖ Close", "x:close")]])
            await event.answer()
        else:
            await event.answer()

    async def _take_start_media(self, event: Any, mode: Mode) -> None:
        """The owner sent the picture or GIF /smedia asked for: keep it, then show /start with it."""
        uid, msg, want = event.sender_id, event.message, mode.data["kind"]
        is_gif = getattr(msg, "gif", None) is not None
        if want == "gif" and not is_gif:
            await self._reply(event, "❌ That isn't a GIF. Send a GIF (an animation), or /cancel to stop.")
            return
        if want == "image" and (is_gif or not is_image(msg)):
            await self._reply(event, "❌ That isn't an image. Send a photo or an image file, or /cancel to stop.")
            return
        size = int(msg.file.size or 0) if getattr(msg, "file", None) is not None else 0
        if size > START_MEDIA_MAX_BYTES:
            await self._reply(
                event, f"❌ That's {human_size(size)}; the biggest I can keep is {human_size(START_MEDIA_MAX_BYTES)}. Send a smaller one, or /cancel to stop."
            )
            return
        self._consume(uid, "smedia")
        raw = await self.client.download_media(msg, file=bytes)
        if not raw:
            await self._reply(event, "❌ I couldn't download that. Send /smedia to try again.")
            return
        try:
            if want == "gif":
                data, meta = raw, {
                    "kind": "gif", "mime": msg.file.mime_type or "video/mp4", "width": int(msg.file.width or 0),
                    "height": int(msg.file.height or 0), "duration": float(msg.file.duration or 0),
                }
            elif getattr(msg, "photo", None) is not None:  # Telegram already made it a JPEG that fits
                data, meta = raw, {"kind": "image"}
            else:
                data, meta = await asyncio.to_thread(media.fit_photo, raw, START_IMAGE_BOX), {"kind": "image"}
        except Exception:  # noqa: BLE001
            await self._reply(event, "❌ I couldn't read that image. Send /smedia to try again.")
            return
        if len(data) > START_MEDIA_MAX_BYTES:
            await self._reply(event, f"❌ That's {human_size(len(data))}; the biggest I can keep is {human_size(START_MEDIA_MAX_BYTES)}. Send /smedia to try another.")
            return
        before = (await self.db.kv_get("start_media") or "", await self.db.kv_get("start_media_data") or "")
        await self._store_start_media(json.dumps({**meta, "file_id": ""}), base64.b64encode(data).decode("ascii"))
        text, rows = await self.panel(uid, "home")
        if not await self._send_start_media(event.chat_id, text, rows):  # the preview is the real /start: if it fails, so would /start
            await self._store_start_media(*before)
            await self._reply(event, "❌ Telegram wouldn't take that as the start media, so nothing was changed. Send /smedia to try another.")
            return
        await self._reply(event, f"✅ Done: /start shows this {'image' if want == 'image' else 'GIF'} from now on.")

    # ------------------------------------------------------------------- speed
    async def cmd_speed(self, event: Any) -> None:
        if self._speed_lock.locked():
            await self._reply(event, "⏳ A speed test is already running. Try again in a minute.")
            return
        async with self._speed_lock:
            busy = self.queue.running > 0
            status = await self._reply(event, "⚡ Running the speed test… this takes about half a minute.")
            preview = False  # only the figures get Telegram's preview of the result page, never an error message
            try:
                result = await speed.run_speedtest()
                text, preview = speed.format_result(result), bool(speed.result_link(result))
                if busy:
                    text += "\nℹ️ A rename job was running during the test, so the numbers may be lower than usual."
            except speed.SpeedError as exc:
                text = f"❌ I couldn't run the speed test: {esc(exc)}"
            except Exception:  # noqa: BLE001
                log.exception("speed test failed")
                text = "❌ The speed test failed unexpectedly."
            await self.client.edit_message(status, text, parse_mode="html", link_preview=preview)

    # ----------------------------------------------------------- status/stats
    async def cmd_status(self, event: Any) -> None:
        ping_ms: Optional[float] = None
        try:
            ping_ms = await self.client.ping()
        except Exception as exc:  # noqa: BLE001
            log.warning("ping failed: %s: %s", type(exc).__name__, exc)
        await self._reply(
            event, sysinfo.format_status(ping_ms, getattr(self.client.session, "dc_id", None), sysinfo.library_version(), self._speed_text())
        )

    def _speed_text(self) -> str:
        return sysinfo.format_speed(transfer.stats, self.cfg.stream_transfer)

    async def cmd_stats(self, event: Any) -> None:
        await self._reply(event, await self._stats_text())

    async def _stats_text(self) -> str:
        users = await self.db.count_users()
        files, total = await self.db.totals()
        return await sysinfo.format_stats(users, files, total, self.queue.running, self.queue.waiting, self.cfg.temp_dir)

    # ------------------------------------------------------------------ admins
    async def cmd_addadmin(self, event: Any) -> None:
        arg = self._args(event)
        if not re.fullmatch(r"\d{4,15}", arg):
            await self._reply(event, "Usage: <code>/addadmin 123456789</code> (a numeric user id; they can find theirs with @userinfobot).")
            return
        uid = int(arg)
        if uid == self.cfg.owner_id or uid in self.admins:
            await self._reply(event, "They already have access.")
            return
        await self.db.add_admin(uid, event.sender_id)
        self.admins.add(uid)
        await self._reply(event, f"✅ <code>{uid}</code> can now use the bot.")

    async def cmd_deladmin(self, event: Any) -> None:
        arg = self._args(event)
        if not re.fullmatch(r"\d{4,15}", arg):
            await self._reply(event, "Usage: <code>/deladmin 123456789</code>")
            return
        uid = int(arg)
        removed = await self.db.del_admin(uid)
        if uid in self.cfg.admin_ids:
            await self._reply(event, "That id is listed in the ADMIN_IDS setting; remove it there.")
            return
        self.admins.discard(uid)
        await self._reply(event, f"✅ <code>{uid}</code> was removed." if removed else "That id wasn't in the list.")

    async def cmd_admins(self, event: Any) -> None:
        ids = ", ".join(f"<code>{a}</code>" for a in sorted(self.admins)) or "none"
        await self._reply(event, f"<b>Owner:</b> <code>{self.cfg.owner_id}</code>\n<b>Allowed users:</b> {ids}")

    # --------------------------------------------------------------- text input
    async def on_text(self, event: Any) -> None:
        uid = event.sender_id
        p = self.pending.get(uid)
        if p is None or p.expires < time.time():
            self.pending.pop(uid, None)
            return
        if p.kind.startswith("fmt:"):
            await self._on_format_text(event, p)
            return
        ok, msg = await apply_setting(self.db, uid, p.kind, event.raw_text)
        if not ok:
            await self._reply(event, msg + "\n\nTry again, or tap Cancel.")
            return
        self.pending.pop(uid, None)
        try:
            await event.delete()
        except Exception:  # noqa: BLE001
            pass
        text, rows = await self.panel(uid, p.back)
        limit = 1000 if p.is_photo else 3900
        full = f"{msg}\n\n{text}" if visible_len(msg) + visible_len(text) < limit else f"{msg}\n\n(Open the menu to see the update.)"
        try:
            await self.client.edit_message(p.chat_id, p.msg_id, full, buttons=panels.to_buttons(rows), parse_mode="html", link_preview=False)
        except Exception:  # noqa: BLE001
            await self._reply(event, full, rows)

    # ---------------------------------------------------------------- callbacks
    async def on_callback(self, event: Any) -> None:
        uid = event.sender_id
        data = event.data.decode("utf-8", "ignore")
        parts = data.split(":")
        head = parts[0]

        if data == "x:close":
            self.pending.pop(uid, None)
            await event.answer()
            await self._remove_message(event)
            return

        if head == "m" and len(parts) == 2 and parts[1] in PANEL_NAMES:
            self.pending.pop(uid, None)
            if parts[1] == "admin" and not self.is_owner(uid):
                await event.answer("Owner only.", alert=True)
                return
            text, rows = await self.panel(uid, parts[1])
            await self._edit_panel(event, text, rows)
            await event.answer()
            return

        if head == "set" and len(parts) == 3:
            await self._cb_set(event, uid, parts[1], parts[2])
            return

        if head == "fm":
            await self._cb_format(event, uid, parts)
            return

        if head == "fs":
            await self._cb_shared(event, uid, parts)
            return

        if head == "au":
            await self._cb_auto(event, uid, parts)
            return

        if head == "rn":
            await self._cb_rename(event, uid, parts)
            return

        if head == DBFLOW_PREFIX:
            if self.dbflow is None:
                await event.answer("Uploading to the Database Channel is switched off (DB_CHANNEL and COMMUNICATION_CHAT).", alert=True)
            else:
                await self.dbflow.on_callback(event, parts)
            return

        if head == "sq":
            await self._cb_sequence(event, uid, parts)
            return

        if head == "sm":
            await self._cb_smedia(event, uid, parts)
            return

        if data == "et:stop":
            if self._mode(uid, "ethumb") is not None:
                self._end_mode(uid)
            await self._edit_panel(event, "⏹ Stopped. Send /ethumb again to extract a thumbnail.", [[("✖ Close", "x:close")]])
            await event.answer()
            return

        if head == "in":
            await self._cb_input(event, uid, parts)
            return

        if head == "clr" and len(parts) == 2 and parts[1] in ("keep_audio", "keep_subtitle"):
            _ok, msg = await apply_setting(self.db, uid, parts[1], "off")
            text, rows = await self.panel(uid, "tool")
            await self._edit_panel(event, text, rows)
            await event.answer(clip(plain(msg), 190))
            return

        if data == "help:meta":
            text, rows = panels.metadata_help()
            await self._edit_panel(event, text, rows)
            await event.answer()
            return

        if head == "adm" and len(parts) == 2:
            if not self.is_owner(uid):
                await event.answer("Owner only.", alert=True)
                return
            if parts[1] == "stats":
                await event.answer()
                await self.client.send_message(event.chat_id, await self._stats_text(), parse_mode="html")
            elif parts[1] == "status":
                await event.answer()
                await self.client.send_message(
                    event.chat_id,
                    sysinfo.format_status(None, getattr(self.client.session, "dc_id", None), sysinfo.library_version(), self._speed_text()),
                    parse_mode="html",
                )
            elif parts[1] == "clear":
                count = self.queue.cancel_all()
                await event.answer(f"Cancelling {count} job(s).", alert=True)
            return

        if head == "job" and len(parts) == 3 and parts[1] == "x" and parts[2].isdigit():
            ok = self.queue.cancel(int(parts[2]), user_id=None if self.is_owner(uid) else uid)
            await event.answer("Cancelling…" if ok else "That job already finished.")
            return

        await event.answer()

    async def _cb_set(self, event: Any, uid: int, field: str, value: str) -> None:
        if field == "mode" and value in ("filename", "caption"):
            await self.db.update_settings(uid, mode=value)
            panel, toast = "rename", f"Mode: {value.title()}"
        elif field == "ftype" and value in ("same", "document", "video", "audio"):
            await self.db.update_settings(uid, file_type=value)
            panel, toast = "ftype", "File type: " + ("Same as original" if value == "same" else value.title())
        elif field == "meta" and value in ("on", "off"):
            await self.db.update_settings(uid, metadata_on=(value == "on"))
            panel, toast = "meta", f"Metadata {value.upper()}"
        else:
            await event.answer()
            return
        text, rows = await self.panel(uid, panel)
        await self._edit_panel(event, text, rows)
        await event.answer(toast)

    async def _cb_input(self, event: Any, uid: int, parts: List[str]) -> None:
        if len(parts) == 3 and parts[1] == "cancel":
            self.pending.pop(uid, None)
            back = parts[2] if parts[2] in PANEL_NAMES else "home"
            text, rows = await self.panel(uid, back)
            await self._edit_panel(event, text, rows)
            await event.answer("Cancelled.")
            return
        kind = parts[1] if len(parts) == 2 else ""
        if kind not in panels.PROMPTS:
            await event.answer()
            return
        msg_obj = await event.get_message()
        self.pending[uid] = Pending(
            kind=kind, chat_id=event.chat_id, msg_id=event.message_id, back=BACK[kind],
            is_photo=bool(msg_obj is not None and (msg_obj.photo is not None or getattr(msg_obj, "media", None) is not None)),
            expires=time.time() + self.PENDING_TTL,
        )
        text, rows = panels.input_prompt(kind, BACK[kind])
        await self._edit_panel(event, text, rows)
        await event.answer()

    # ------------------------------------------------------------ saved formats
    async def _format_panel(self, uid: int, fmt: Dict[str, Any], share_inline: Optional[bool] = None) -> panels.Panel:
        """A format's screen: for an auto format it says whether it is the one in use.  ``share_inline`` defaults to whether
        inline mode is known to be on."""
        active = False
        if fmt["kind"] == "auto":
            current = await self.db.get_auto_format(uid)
            active = current is not None and current["id"] == fmt["id"]
        if share_inline is None:
            share_inline = await self._check_inline()
        return panels.format_view(fmt, share_inline=share_inline, active=active, upload_on=self.cfg.upload_enabled)

    async def _show_format(self, event: Any, uid: int, fid: int, note: str = "") -> bool:
        fmt = await self.db.get_format(uid, fid)
        if fmt is None:
            return False
        text, rows = await self._format_panel(uid, fmt)
        await self._edit_panel(event, f"{note}\n\n{text}" if note else text, rows)
        return True

    async def _show_format_list(self, event: Any, uid: int, kind: str) -> None:
        formats = await self.db.list_formats(uid, kind)
        active = await self.db.get_auto_format(uid) if kind == "auto" else None
        text, rows = panels.formats_menu(formats, MAX_FORMATS, kind, active["id"] if active else 0)
        await self._edit_panel(event, text, rows)

    async def _cb_auto(self, event: Any, uid: int, parts: List[str]) -> None:
        """A button of the /start menu's Auto Format screen: use that auto format from now on."""
        self.pending.pop(uid, None)
        fmt = await self.db.select_auto_format(uid, int(parts[1])) if len(parts) == 2 and parts[1].isdigit() else None
        text, rows = await self.panel(uid, "auto")
        await self._edit_panel(event, text, rows)
        if fmt is None:
            await event.answer("That format no longer exists.", alert=True)
        else:
            await event.answer(f"Auto format: {clip(fmt['name'], 40)}")

    async def _cb_format(self, event: Any, uid: int, parts: List[str]) -> None:
        self.pending.pop(uid, None)  # leaving a prompt open by tapping something else abandons it
        action = parts[1] if len(parts) > 1 else ""
        fid = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0

        if action in ("l", "new"):
            kind = parts[2] if len(parts) > 2 else ""
            if kind not in ("auto", "manual"):  # a button of a message from before there were two kinds: start from the choice
                text, rows = await self.panel(uid, "formats")
                await self._edit_panel(event, text, rows)
                await event.answer()
                return
            if action == "l":
                await self._show_format_list(event, uid, kind)
                await event.answer()
            elif await self.db.count_formats(uid, kind) >= MAX_FORMATS:
                await event.answer(f"You already have {MAX_FORMATS} {kind} formats. Delete one first.", alert=True)
            else:
                await self._format_prompt(event, uid, "new", 0, kind)
            return

        fmt = await self.db.get_format(uid, fid) if fid else None
        if fmt is None:
            text, rows = await self.panel(uid, "formats")
            await self._edit_panel(event, text, rows)
            await event.answer("That format no longer exists.", alert=True)
            return

        if action in ("v", "cx"):
            await self._show_format(event, uid, fid)
            await event.answer()
        elif action == "in" and len(parts) == 4 and parts[3] == "channel":
            if fmt["kind"] != "manual" or not self.cfg.upload_enabled:
                await event.answer("Channel is an option of manual formats, while uploading to the Database Channel is set up.", alert=True)
                return
            await self._format_prompt(event, uid, "channel", fid)
        elif action == "chc":
            await self.db.update_format(uid, fid, channel=None)
            await self._show_format(event, uid, fid)
            await event.answer("Channel cleared.")
        elif action == "in" and len(parts) == 4 and parts[3] in (*FIELD_KINDS, "name"):
            await self._format_prompt(event, uid, parts[3], fid)
        elif action == "th":
            await self._format_prompt(event, uid, "thumb", fid)
        elif action == "thd":
            await self.db.set_format_thumb(uid, fid, None)
            await self._show_format(event, uid, fid)
            await event.answer("Thumbnail deleted.")
        elif action == "cr":
            await apply_setting(FormatSettings(self.db, fid), uid, "caption", "reset")
            await self._show_format(event, uid, fid)
            await event.answer("Caption reset.")
        elif action == "use":
            if await self.db.select_auto_format(uid, fid) is None:
                await event.answer("Only an auto format can be used for auto rename.", alert=True)
                return
            await self._show_format(event, uid, fid, note=f"✅ <b>{esc(clip(fmt['name'], 30))}</b> now renames your files.")
            await event.answer(f"Auto format: {clip(fmt['name'], 40)}")
        elif action == "dm":
            settings = await self.db.get_settings(uid)
            if not has_default_metadata(settings):
                await event.answer("Your default metadata is empty. Set it in /metadata first, then apply it.", alert=True)
                return
            await self.db.update_format(uid, fid, **default_metadata_changes(settings))
            await self._show_format(event, uid, fid, note=panels.format_metadata_applied(fmt))
            await event.answer("Default metadata applied.")
        elif action == "cp":
            target = other_kind(fmt["kind"])
            try:
                made = await self.db.copy_format(uid, uid, fid, name_max=NAME_MAX, kind=target)
            except FormatError:  # only "limit" can be raised here
                await event.answer(f"You already have {MAX_FORMATS} {target} formats. Delete one first.", alert=True)
                return
            if made is None:
                await event.answer("That format no longer exists.", alert=True)
                return
            text, rows = panels.format_copied(fmt, made)
            await self._edit_panel(event, text, rows)
            await event.answer(f"Copied to your {target} formats.")
        elif action == "sh":  # Share format, shown while inline mode wasn't known to be on: just try the real button
            text, rows = await self._format_panel(uid, fmt, share_inline=True)
            try:
                await event.edit(text, buttons=panels.to_buttons(rows), parse_mode="html", link_preview=False)
            except errors.MessageNotModifiedError:
                pass
            except errors.BotInlineDisabledError:  # Telegram refuses it: inline mode is off
                self._inline_refused()
                await event.answer(panels.INLINE_OFF, alert=True)
                return
            self._inline_on = True
            await event.answer("Inline mode is on. Tap Share format again.")
        elif action == "del":
            text, rows = panels.format_delete_confirm(fmt)
            await self._edit_panel(event, text, rows)
            await event.answer()
        elif action == "ok":
            await self.db.delete_format(uid, fid)
            await self._show_format_list(event, uid, fmt["kind"])
            await event.answer(f"Deleted {clip(fmt['name'], 40)}.")
        else:
            await event.answer()

    async def _format_prompt(self, event: Any, uid: int, field: str, fid: int, kind: str = "") -> None:
        msg_obj = await event.get_message()
        self.pending[uid] = Pending(
            kind=f"fmt:{field}", chat_id=event.chat_id, msg_id=event.message_id, back="formats",
            is_photo=bool(msg_obj is not None and (msg_obj.photo is not None or getattr(msg_obj, "media", None) is not None)),
            expires=time.time() + self.PENDING_TTL, fid=fid, fmt_kind=kind,
        )
        text, rows = panels.format_prompt(field, fid, kind)
        await self._edit_panel(event, text, rows)
        await event.answer()

    async def _on_format_text(self, event: Any, p: Pending) -> None:
        uid = event.sender_id
        field = p.kind[4:]
        text = self._caption_input(event) if field == "caption" else event.raw_text or ""
        if field == "thumb":
            await self._reply(event, "Send a photo for the thumbnail, or tap Cancel.")
            return
        try:
            if field in ("new", "name"):
                ok, value = validate_name(text)
                if ok and field == "new":
                    fmt = await self.db.create_format(uid, value, kind=p.fmt_kind or "manual")
                    msg = (
                        f"✅ {KIND_LABELS[fmt['kind']]} format <b>{esc(value)}</b> created. Set its template, caption, "
                        "thumbnail and metadata, or tap Apply default metadata."
                    )
                    p.fid = fmt["id"]
                elif ok:
                    if await self.db.update_format(uid, p.fid, name=value) is None:
                        raise LookupError
                    msg = f"✅ Renamed to <b>{esc(value)}</b>."
                else:
                    msg = value
            elif field == "channel":
                ok, channel, msg = parse_format_channel(text)
                if ok:
                    if await self.db.update_format(uid, p.fid, channel=channel) is None:
                        raise LookupError
                    msg = f"✅ Channel saved: <code>{channel}</code>" if channel else "✅ Channel cleared."
            else:
                ok, msg = await apply_setting(FormatSettings(self.db, p.fid), uid, FIELD_KINDS[field], text)
        except FormatError as exc:
            ok = False
            if exc.code == "limit":
                self.pending.pop(uid, None)
                await self._reply(event, f"❌ You already have {MAX_FORMATS} {p.fmt_kind or 'manual'} formats. Delete one first.")
                return
            msg = "❌ You already have a format with that name."
        except LookupError:
            self.pending.pop(uid, None)
            await self._reply(event, "⚠️ That format no longer exists.")
            return
        if not ok:
            await self._reply(event, msg + "\n\nTry again, or tap Cancel.")
            return
        self.pending.pop(uid, None)
        try:
            await event.delete()
        except Exception:  # noqa: BLE001
            pass
        await self._refresh_format_panel(event, p, msg)

    async def _format_thumb_saved(self, event: Any, p: Pending, thumb: bytes) -> None:
        uid = event.sender_id
        self.pending.pop(uid, None)
        if not await self.db.set_format_thumb(uid, p.fid, thumb):
            await self._reply(event, "⚠️ That format no longer exists.")
            return
        await self._refresh_format_panel(event, p, "✅ Thumbnail saved for this format.")

    async def _refresh_format_panel(self, event: Any, p: Pending, msg: str) -> None:
        """Put the format's panel back into the message the prompt was shown in (a new message if that fails)."""
        fmt = await self.db.get_format(event.sender_id, p.fid)
        if fmt is None:
            await self._reply(event, msg)
            return
        text, rows = await self._format_panel(event.sender_id, fmt)
        limit = 1000 if p.is_photo else 3900
        full = f"{msg}\n\n{text}" if visible_len(msg) + visible_len(text) < limit else f"{msg}\n\n(Open the format to see the update.)"
        try:
            await self.client.edit_message(p.chat_id, p.msg_id, full, buttons=panels.to_buttons(rows), parse_mode="html", link_preview=False)
        except Exception:  # noqa: BLE001
            await self._reply(event, full, rows)

    # ------------------------------------------------------------ sharing a format
    async def _check_inline(self, *, force: bool = False) -> bool:
        """Is inline mode on for the bot (@BotFather ▸ Bot Settings ▸ Inline Mode)?

        The Share format button needs it: while it is off, Telegram refuses a message that carries that button.  Once it has
        been seen on it stays on (until Telegram refuses a button); while it is off the bot looks again after
        ``INLINE_RECHECK`` seconds, or at once with ``force``.
        """
        now = time.monotonic()
        if not force:
            if self._inline_on:
                return True
            if self._inline_checked is not None and now - self._inline_checked < INLINE_RECHECK:
                return False
        self._inline_checked = now
        try:
            me = await self.client.get_me()
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read the bot's profile to see whether inline mode is on: %s", exc)
            return self._inline_on
        self._username = getattr(me, "username", None) or self._username
        # the placeholder is only there while inline mode is on ("" = on, without a placeholder text)
        self._inline_on = self._inline_on or getattr(me, "bot_inline_placeholder", None) is not None
        return self._inline_on

    def _inline_refused(self) -> None:
        """Telegram refused a Share button, so inline mode is off: the next look at the bot's profile is a minute away."""
        self._inline_on = False
        self._inline_checked = time.monotonic()

    async def _bot_username(self) -> Optional[str]:
        if self._username is None:
            await self._check_inline(force=True)
        return self._username

    async def on_inline(self, event: Any) -> None:
        """Where the chat picker of a Share format button ends up: the owner of the format gets a card to send to that chat."""
        results: List[Any] = []
        match = SHARE_QUERY.fullmatch((event.text or "").strip())
        if match:
            try:
                results = await self._share_cards(event.sender_id, int(match.group(1)), event.builder)
            except Exception:  # noqa: BLE001
                log.exception("Error making the card of a shared format:")
        await self._answer_inline(event, results)

    async def _share_cards(self, uid: int, fid: int, builder: Any) -> List[Any]:
        """The card for one of the user's formats: its name, and a button that opens it in this bot (``/start fmt_<token>``)."""
        fmt = await self.db.get_format(uid, fid)
        token = await self.db.share_token(uid, fid) if fmt is not None else None
        username = await self._bot_username() if token else None
        if fmt is None or token is None or not username:
            return []
        title, description, text = panels.share_card(fmt)
        link = f"https://t.me/{username}?start={SHARE_PREFIX}{token}"
        card = await builder.article(
            title, description=description, text=text, parse_mode="html", link_preview=False,
            buttons=[[Button.url("👁 View format", link)]],
        )
        return [card]

    @staticmethod
    async def _answer_inline(event: Any, results: List[Any]) -> None:
        try:
            await event.answer(results, cache_time=0, private=True)  # not cached: the card has to show the format as it is now
        except Exception as exc:  # noqa: BLE001 - e.g. the query is too old by now
            log.debug("could not answer an inline query: %s", exc)

    async def _open_shared(self, event: Any, token: str) -> None:
        """``/start fmt_<token>``: what is in a format somebody shared, with a button to add it to your own formats."""
        shared = await self.db.get_shared_format(token) if SHARE_TOKEN.fullmatch(token) else None
        if shared is None:
            await self._reply(event, panels.SHARE_GONE, [[("✖ Close", "x:close")]])
            return
        owner_id, fmt = shared
        text, rows = panels.shared_format(fmt, token, mine=owner_id == event.sender_id)
        thumb = await self.db.get_format_thumb(owner_id, fmt["id"]) if fmt["has_thumb"] else None
        if thumb:  # the format's thumbnail on top, its details as the caption
            bio = io.BytesIO(thumb)
            bio.name = "thumbnail.jpg"
            try:
                await self.client.send_file(event.chat_id, bio, caption=text, buttons=panels.to_buttons(rows), parse_mode="html")
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("could not send the thumbnail of a shared format (%s); showing its details only", exc)
        await self._reply(event, text, rows)

    async def _cb_shared(self, event: Any, uid: int, parts: List[str]) -> None:
        """The button on a shared format: put a copy of it in the user's own formats."""
        token = parts[2] if len(parts) > 2 else ""
        shared = await self.db.get_shared_format(token) if SHARE_TOKEN.fullmatch(token) else None
        mine = None
        if shared is not None:
            if parts[1:2] != ["add"]:
                await event.answer()
                return
            if shared[0] == uid:
                await event.answer("That's your own format.", alert=True)
                return
            try:
                mine = await self.db.copy_format(uid, shared[0], shared[1]["id"], name_max=NAME_MAX)
            except FormatError:  # only "limit" can be raised here
                await event.answer(f"You already have {MAX_FORMATS} {shared[1]['kind']} formats. Delete one first.", alert=True)
                return
        if mine is None:  # the owner deleted it after sharing
            await self._edit_panel(event, panels.SHARE_GONE, [[("✖ Close", "x:close")]])
            await event.answer("That shared format isn't available anymore.", alert=True)
            return
        text, rows = panels.shared_added(mine)
        await self._edit_panel(event, text, rows)
        await event.answer("Added to your formats.")

    # ---------------------------------------------------------- /rename picker
    async def _cb_rename(self, event: Any, uid: int, parts: List[str]) -> None:
        self.pending.pop(uid, None)
        action = parts[1] if len(parts) > 1 else ""
        if action == "p" and len(parts) == 3 and parts[2].isdigit():
            fmt = await self.db.get_format(uid, int(parts[2]))
            if fmt is None or fmt["kind"] != "manual":
                text, rows = panels.rename_picker(await self.db.list_formats(uid, "manual"))
                await self._edit_panel(event, text, rows)
                await event.answer("That format no longer exists.", alert=True)
                return
            mode = self._start_mode(uid, "rename", ONE_FILE_WAIT, {"fid": fmt["id"]})
            text, rows = panels.rename_ready(fmt, ONE_FILE_WAIT // 60)
            await self._edit_panel(event, text + (f"\n\n{mode.dropped}" if mode.dropped else ""), rows)
            await event.answer(f"Format: {clip(fmt['name'], 40)}")
        elif action == "menu":
            if self._mode(uid, "rename") is not None:
                self._end_mode(uid)
            text, rows = panels.rename_picker(await self.db.list_formats(uid, "manual"))
            await self._edit_panel(event, text, rows)
            await event.answer()
        elif action == "stop":
            if self._mode(uid, "rename") is not None:
                self._end_mode(uid)
            await self._edit_panel(event, "⏹ Stopped. Send /rename again to pick a format.", [[("✖ Close", "x:close")]])
            await event.answer()
        else:
            await event.answer()

    # ------------------------------------------------------------ /sequence buttons
    async def _cb_sequence(self, event: Any, uid: int, parts: List[str]) -> None:
        action = parts[1] if len(parts) > 1 else ""
        mode = self._mode(uid, "sequence")
        if mode is None:
            await event.answer("That sequence is over.", alert=True)
            return
        if action == "sort":
            await event.answer()
            await self._run_sort(uid, event.chat_id)
        elif action == "cx":
            self._end_mode(uid)
            await event.answer("Cancelled.")
            await self._edit_panel(event, self._mode_cancelled_text(mode), [])
        else:
            await event.answer()
