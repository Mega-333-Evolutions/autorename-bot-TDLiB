"""Upload a manually renamed episode to the Database Channel, and have the channel post bot publish it.

All of this exists only while DB_CHANNEL and COMMUNICATION_CHAT are both set.

1. After /rename with a manual format has sent the renamed episode, ``DbFlow.offer`` asks (Yes / No) whether to upload it.
2. Yes: the message becomes "Processing…" and the episode is copied into the Database Channel on Telegram's side (forwarded
   without the "Forwarded from" line): nothing is downloaded or uploaded again.
3. When the format has a Channel, "File uploaded to the Database Channel" is then written in the communication chat, with the
   episode number, that channel id, the file name and the id of the copy.  The channel post bot answers there, replying to it:
   "message received", then "post published successfully" or "publication failed: <reason>".
4. Those answers are read here (``on_chat_message``) and shown in the Processing message.  No answer at all: the channel post
   bot is down.

A format without a Channel is only uploaded: no notice is written and nothing is published.

No to the offer asks "upload it and get the Share URL?": Yes copies the episode into the Database Channel, waits for the file
store's Share URL button on the copy, and shows its link in monospace.  When this bot cannot see the button, it asks the channel
post bot ("Get the share URL" in the communication chat) whose userbot can.  Nothing is published.

Deleting what was published: the message that says "Post published successfully" has a "🗑 Delete post" button.
5. It asks "Are you sure you want to delete this post?" in a message of its own (Yes / No).
6. Yes: "Delete the published post" is written in the communication chat, with the channel and the id of the post (the channel
   post bot reported both).  The channel post bot deletes the post in its channel and in My posts and answers "post deleted
   successfully" or "deletion failed: <reason>" (the same Ref / reply conventions as above).
7. Only when that answer is "deleted" is the episode's copy in the Database Channel deleted here.  Any other outcome leaves
   everything as it was and offers "Try again" - asking again is harmless, a post that is already gone counts as deleted.

Nothing about a running upload or deletion is stored: the buttons carry what is needed, and a restart in the middle just leaves
the Processing message as it was.
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set, Tuple
from urllib.parse import unquote

from . import panels
from .config import Config
from .parser import parse_info, split_ext
from .tg import errors
from .utils import clip, esc

log = logging.getLogger(__name__)

PREFIX = "dbu"  # callback data: dbu:y:<chat>:<message>:<channel or 0> (Yes), dbu:n (No);
#                 dbu:d:<copy>:<channel>:<post> (🗑 Delete post), dbu:dy:<copy>:<channel>:<post>:<that message> (Yes), dbu:dn (No)
ACK_WAIT = 60.0  # seconds the channel post bot has to say "message received" before it counts as down
RESULT_WAIT = 900.0  # ... and then to say whether the post was published (it waits at least 10 s for the Share URL button)
DELETE_WAIT = 300.0  # ... or whether the post was deleted (it may be busy making another post first)
NOTICE_HEAD = "File uploaded to the Database Channel"
DELETE_HEAD = "Delete the published post"
LINK_WAIT = 15.0
CALLBACK_MAX = 64  # bytes of callback data Telegram accepts

_SHARE_SHEET = re.compile(r"(?i)^https?://(?:t\.me|telegram\.me|telegram\.dog)/share(?:/url)?\?(.*)$")
_FAILED = re.compile(r"(?i)(?:publication|deletion|share\s+url)\s+failed\s*[:\-–]?[ \t]*(.*)")
_SUCCESS = re.compile(r"(?i)post\s+(?:published|deleted)\s+successfully|share\s+url\s+ready")
_RECEIVED = re.compile(r"(?i)message\s+received")


def episode_of(file_name: str) -> str:
    """The episode number of a file name, as written in it (``05``); empty when it has none."""
    return parse_info(split_ext(file_name or "")[0]).episode


def line_of(label: str, text: str) -> str:
    """The value of a ``Label: value`` line of a message; empty when there is no such line."""
    m = re.search(rf"(?im)^[ \t]*{label}[ \t]*:[ \t]*(.+?)[ \t]*$", text or "")
    return m.group(1).strip() if m else ""


def classify(text: Optional[str]) -> Tuple[str, str]:
    """What a message of the channel post bot says: ``("fail", reason)``, ``("ok", "")``, ``("ack", "")`` or ``("other", "")``."""
    text = text or ""
    failed = _FAILED.search(text)
    if failed:
        return "fail", " ".join(failed.group(1).split())
    if _SUCCESS.search(text):
        return "ok", ""
    if _RECEIVED.search(text):
        return "ack", ""
    return "other", ""


def notice_text(*, episode: str, channel: int, file: str, post_id: Optional[int], ref: str) -> str:
    """The message for the communication chat.  The channel post bot reads the Episode and the Channel from it."""
    lines = [NOTICE_HEAD]
    if episode:
        lines.append(f"Episode: {episode}")
    lines.append(f"Channel: {channel}")
    lines.append(f"File: {file}")
    if post_id:
        lines.append(f"Post: {post_id}")
    lines.append(f"Ref: {ref}")
    return "\n".join(lines)


def delete_notice_text(*, channel: int, message: int, ref: str) -> str:
    """The message for the communication chat that has a published post deleted (channel and post as the channel post bot reported them)."""
    return "\n".join([DELETE_HEAD, f"Channel: {channel}", f"Message: {message}", f"Ref: {ref}"])


def post_message_id(reply: str) -> Optional[int]:
    """The new post as a message of its channel, from "Post published successfully": its "Message:" line, else the last number of its
    "Post:" link (https://t.me/name/57, https://t.me/c/123/57); None when the reply has neither."""
    number = line_of("message", reply)
    if re.fullmatch(r"[0-9]{1,12}", number):
        return int(number)
    found = re.search(r"/([0-9]{1,12})/?$", line_of("post", reply))
    return int(found.group(1)) if found else None


def delete_button(copy_id: int, channel: int, post: int) -> Optional[list]:
    """The 🗑 Delete post button of a published post's message; None when its data would not fit Telegram's limit."""
    data = f"{PREFIX}:d:{copy_id}:{channel}:{post}"
    return [[("🗑 Delete post", data)]] if len(data.encode()) <= CALLBACK_MAX else None


def link_notice_text(*, post_id: int, ref: str) -> str:
    """Asks the channel post bot (which has the userbot) for the Share URL of a file in the Database Channel; nothing is published."""
    return "\n".join(["Get the share URL", f"Post: {post_id}", f"Ref: {ref}"])


def share_link(msg: Any) -> str:
    """The file link of the "Share URL" button of a message ("" when it has none). A Share URL button usually opens Telegram's share
    sheet (t.me/share/url?url=<file link>): that wrapper is taken off."""
    for row in getattr(getattr(msg, "reply_markup", None), "rows", None) or []:
        for button in getattr(row, "buttons", None) or []:
            url = getattr(button, "url", None) or getattr(getattr(button, "type", None), "url", None)  # (older layers: button.url)
            if url and re.search(r"(?i)\bshare\s*url\b", getattr(button, "text", "") or ""):
                m = _SHARE_SHEET.match(url.strip())
                if not m:
                    return url.strip()
                inner = re.search(r"(?:^|&)url=(.*?)(?=&text=|$)", m.group(1))
                link = unquote(inner.group(1)) if inner else ""
                return link if re.match(r"(?i)^(https?://|tg://)\S+$", link) else ""
    return ""


def reply_target(event: Any) -> Optional[int]:
    """The id of the message an incoming message replies to."""
    message = getattr(event, "message", None)
    return getattr(message, "reply_to_msg_id", None) or getattr(event, "reply_to_msg_id", None) or None


def _name(name: str) -> str:
    return f"<code>{esc(clip(name, 120))}</code>" if name else ""


def _why(exc: BaseException) -> str:
    return esc(clip(f"{type(exc).__name__}: {exc}", 250))


def offer_text(name: str, episode: str, channel: Optional[int]) -> str:
    lines = ["📤 <b>Upload this episode to the Database Channel?</b>", _name(name)]
    if channel:
        lines.append(f"🎞 Episode: <b>{esc(episode)}</b>" if episode else "⚠️ No episode number found in the file name (the post uses it).")
        lines.append(f"📣 Channel: <code>{channel}</code>: the channel post bot posts it there.")
    else:
        lines.append("This format has no Channel, so the episode is only uploaded: no post is made.")
    return "\n".join(line for line in lines if line)


def processing_text(name: str, *steps: str) -> str:
    return "\n".join(line for line in ["⏳ <b>Processing…</b>", _name(name), *steps] if line)


def published_text(name: str, reply: str, channel: int) -> str:
    where = line_of("channel", reply) or str(channel)
    link = line_of("post", reply)
    lines = ["✅ <b>Post published successfully</b>", _name(name), f"📣 {esc(clip(where, 80))}"]
    if link:
        lines.append(esc(clip(link, 200)))
    return "\n".join(line for line in lines if line)


def failed_text(name: str, reason: str) -> str:
    lines = ["❌ <b>Publication failed</b>", _name(name), esc(clip(reason or "No reason was given.", 600)), "The episode is in the Database Channel."]
    return "\n".join(line for line in lines if line)


def down_text(name: str, seconds: float) -> str:
    return "\n".join(
        [
            "⚠️ <b>The channel post bot is down or not responding</b>",
            _name(name),
            f"It did not answer in the communication chat within {int(seconds)} seconds. "
            "The episode is in the Database Channel, but nothing was published.",
        ]
    )


def silent_text(name: str, seconds: float) -> str:
    return "\n".join(
        [
            "⚠️ <b>No word from the channel post bot</b>",
            _name(name),
            f"It got the message but did not say whether the post was published in {int(seconds // 60)} minutes. Check the channel.",
        ]
    )


def link_question_text() -> str:
    return (
        "🔗 <b>Upload it to the Database Channel and get the Share URL?</b>\n"
        "You get the link here. Nothing is published: the channel post bot makes no post in the channel of the format."
    )


def link_text(name: str, url: str) -> str:
    return "\n".join(["✅ <b>Uploaded to the Database Channel</b>", _name(name), "🔗 Share URL:", f"<code>{esc(url)}</code>"])


def confirm_text(channel: int) -> str:
    return (
        "🗑 <b>Are you sure you want to delete this post?</b>\n"
        f"It is deleted from the channel <code>{channel}</code> and from My posts of the channel post bot, "
        "and its file from the Database Channel. This cannot be undone."
    )


def deleting_text(*steps: str) -> str:
    return "\n".join(line for line in ["⏳ <b>Deleting the post…</b>", *steps] if line)


def deleted_text(reply: str, copy: bool) -> str:
    """The post is gone: what the channel post bot reported (its channel and notes), and the copy in the Database Channel."""
    lines = ["🗑 <b>Post deleted</b>"]
    where = line_of("channel", reply)
    if where:
        lines.append(f"📣 {esc(clip(where, 80))}")
    lines.append("✅ Deleted from the channel and from My posts")
    lines += [esc(clip(line.strip(), 200)) for line in (reply or "").splitlines() if line.strip().startswith("ℹ️")]
    lines.append("✅ Deleted from the Database Channel" if copy else "ℹ️ There was no copy in the Database Channel to delete.")
    return "\n".join(lines)


def delete_failed_text(reason: str) -> str:
    return "\n".join(
        [
            "❌ <b>The post was not deleted</b>",
            esc(clip(reason or "No reason was given.", 600)),
            "The file in the Database Channel was not touched.",
        ]
    )


def delete_down_text(seconds: float) -> str:
    return "\n".join(
        [
            "⚠️ <b>The channel post bot is down or not responding</b>",
            f"It did not answer in the communication chat within {int(seconds)} seconds, so the post is not deleted. "
            "The file in the Database Channel was not touched.",
        ]
    )


def delete_silent_text(seconds: float) -> str:
    return "\n".join(
        [
            "⚠️ <b>No word from the channel post bot</b>",
            f"It got the message but did not say whether the post was deleted in {int(seconds // 60)} minutes. "
            "Check the channel, then try again. The file in the Database Channel was not touched.",
        ]
    )


def copy_kept_text(reason: str) -> str:
    return "\n".join(
        [
            "⚠️ <b>The post is deleted, but not its file in the Database Channel</b>",
            esc(clip(reason, 400)),
            "Check that the bot is an admin of the Database Channel and may delete messages there, then try again.",
        ]
    )


@dataclass
class Flow:
    """An upload or a deletion that waits for the channel post bot's answers."""

    ref: str  # in the notice and echoed in every answer: how the answers find their way back
    inbox: "asyncio.Queue[Tuple[str, str]]" = field(default_factory=asyncio.Queue)  # (ack | ok | fail, the message)
    notice_id: int = 0  # the notice in the communication chat; an answer is a reply to it


class DbFlow:
    def __init__(
        self, client: Any, cfg: Config, *, ack_wait: float = ACK_WAIT, result_wait: float = RESULT_WAIT, delete_wait: float = DELETE_WAIT
    ):
        self.client = client
        self.cfg = cfg
        self.ack_wait = ack_wait
        self.result_wait = result_wait
        self.delete_wait = delete_wait
        self.link_wait = LINK_WAIT  # seconds this bot looks for the Share URL button itself before it asks the channel post bot
        self.link_poll = 3.0
        self.flows: Dict[str, Flow] = {}
        self._busy: Set[Tuple[int, int]] = set()  # (chat, message) of the uploads and deletions under way: a second tap does nothing
        self._tasks: Set["asyncio.Future[Any]"] = set()

    # ------------------------------------------------------------------ the offer
    async def offer(self, job: Any, target: int, sent: Any) -> None:
        """Ask whether the episode that was just sent (``sent``, in ``target``) goes to the Database Channel too."""
        sent_id = getattr(sent, "id", None)
        if not isinstance(sent_id, int):
            return  # nothing to point at
        channel = (job.fmt or {}).get("channel")
        rows = [[("✅ Yes", f"{PREFIX}:y:{target}:{sent_id}:{channel or 0}"), ("❌ No", f"{PREFIX}:n:{target}:{sent_id}")]]
        await self.client.send_message(
            job.chat_id, offer_text(job.new_name, episode_of(job.new_name), channel), buttons=panels.to_buttons(rows),
            parse_mode="html", link_preview=False, reply_to=sent_id if target == job.chat_id else job.msg_id,
        )

    async def on_callback(self, event: Any, parts: list) -> None:
        action = parts[1] if len(parts) > 1 else ""
        if action in ("d", "dy", "dn", "x"):
            await self._on_delete(event, parts)
            return
        if action in ("l", "g", "z") or (action == "n" and len(parts) == 4):
            await self._on_link(event, parts)
            return
        if action == "n":
            await event.answer()
            await event.edit("👍 Okay, it is not uploaded to the Database Channel.", buttons=None, parse_mode="html")
            return
        try:
            if action != "y" or len(parts) != 5:
                raise ValueError(parts)
            source_chat, source_id, channel = int(parts[2]), int(parts[3]), int(parts[4])
        except ValueError:
            await event.answer()
            return
        key = (event.chat_id, event.message_id)
        if key in self._busy:
            await event.answer("I'm already on it…")
            return
        self._busy.add(key)
        try:
            await event.answer()
            try:
                await event.edit(processing_text(""), buttons=None, parse_mode="html")
            except errors.MessageNotModifiedError:
                pass
        except BaseException:
            self._busy.discard(key)
            raise
        self._spawn(self._run(key, source_chat, source_id, channel or None))

    # --------------------------------------------------------------- the upload
    async def _run(self, key: Tuple[int, int], source_chat: int, source_id: int, channel: Optional[int]) -> None:
        chat_id, msg_id = key
        flow: Optional[Flow] = None
        name = ""
        try:
            msg = await self.client.get_messages(source_chat, ids=source_id)
            if msg is None or getattr(msg, "file", None) is None:
                await self._show(
                    chat_id, msg_id, "❌ <b>That episode is not there anymore</b>\nIt was deleted from the chat, so there is nothing to upload."
                )
                return
            name = getattr(msg.file, "name", None) or ""
            episode = episode_of(name)

            # ---- 1. the Database Channel
            try:
                copy = await self._copy(msg, source_chat)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("could not upload %s to the Database Channel: %s: %s", name, type(exc).__name__, exc)
                retry = [[("🔁 Try again", f"{PREFIX}:y:{source_chat}:{source_id}:{channel or 0}"), ("✖ Close", "x:close")]]
                await self._show(
                    chat_id, msg_id,
                    f"❌ <b>The upload to the Database Channel failed</b>\n{_name(name)}\n{_why(exc)}\n"
                    "Check that the bot is an admin of the Database Channel and can post there.",
                    retry,
                )
                return
            if channel is None:
                await self._show(
                    chat_id, msg_id,
                    f"✅ <b>Uploaded to the Database Channel</b>\n{_name(name)}\nThis format has no Channel, so no post is made.",
                )
                return

            # ---- 2. the communication chat
            flow = Flow(ref=secrets.token_hex(3))
            self.flows[flow.ref] = flow  # before the notice goes out, so that no answer can come back too early
            uploaded = "✅ Uploaded to the Database Channel"
            await self._show(chat_id, msg_id, processing_text(name, uploaded, "⏳ Waiting for the channel post bot…"))
            try:
                note = await self.client.send_message(
                    self.cfg.comm_chat,
                    notice_text(episode=episode, channel=channel, file=name, post_id=_id_of(copy), ref=flow.ref),
                    parse_mode=None, link_preview=False,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("could not write in the communication chat: %s: %s", type(exc).__name__, exc)
                await self._show(
                    chat_id, msg_id,
                    f"⚠️ <b>Uploaded, but I could not write in the communication chat</b>\n{_name(name)}\n{_why(exc)}\n"
                    "Nothing was published. Is the bot a member of that chat?",
                )
                return
            flow.notice_id = _id_of(note) or 0

            # ---- 3. what the channel post bot says
            async def got_it() -> None:
                await self._show(chat_id, msg_id, processing_text(name, uploaded, "📥 The channel post bot got it and is publishing…"))

            rows = None
            verdict, reply = await self._answers(flow, got_it, self.result_wait)
            if verdict == "ok":
                text = published_text(name, reply, channel)
                post = post_message_id(reply)
                rows = delete_button(_id_of(copy) or 0, channel, post) if post else None  # no post id: nothing to delete later
            elif verdict == "fail":
                text = failed_text(name, classify(reply)[1])
            elif verdict == "down":
                text = down_text(name, self.ack_wait)
            else:
                text = silent_text(name, self.result_wait)
            await self._show(chat_id, msg_id, text, rows)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("the upload to the Database Channel went wrong")
            await self._show(chat_id, msg_id, f"❌ <b>Something went wrong</b>\n{_name(name)}\n{_why(exc)}")
        finally:
            self._busy.discard(key)
            if flow is not None:
                self.flows.pop(flow.ref, None)

    async def _copy(self, msg: Any, source_chat: int) -> Any:
        """The episode in the Database Channel, copied on Telegram's side: forwarded without the author, so nothing is downloaded or
        uploaded.  When Telegram refuses that (a chat that forbids forwarding), the same file is sent again by its id, which is as quick."""
        try:
            return await self.client.forward_messages(self.cfg.db_channel, msg.id, from_peer=source_chat, drop_author=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.info("forwarding to the Database Channel was refused (%s: %s); sending the file by its id", type(exc).__name__, exc)
        info = getattr(msg, "file", None)
        return await self.client.send_file(
            self.cfg.db_channel, (getattr(info, "id", "") or msg), caption=getattr(msg, "message", "") or "",
            formatting_entities=getattr(msg, "entities", None) or None, parse_mode=None,
        )

    async def _answers(self, flow: Flow, on_ack: Any, result_wait: float) -> Tuple[str, str]:
        """Wait for the channel post bot: ``("ok" | "fail", its message)``, ``("down", "")`` when it never acknowledged the notice,
        ``("silent", "")`` when it acknowledged it but never said how it went in ``result_wait`` seconds.  ``on_ack()`` is awaited once,
        when the acknowledgement comes."""
        loop = asyncio.get_running_loop()
        acked = False
        deadline = loop.time() + self.ack_wait
        while True:
            try:
                kind, text = await asyncio.wait_for(flow.inbox.get(), timeout=max(0.01, deadline - loop.time()))
            except asyncio.TimeoutError:
                return ("silent" if acked else "down"), ""
            if kind != "ack":
                return kind, text
            if not acked:
                acked = True
                deadline = loop.time() + result_wait
                await on_ack()

    async def _show(self, chat_id: int, msg_id: int, text: str, rows: Optional[list] = None) -> None:
        try:
            await self.client.edit_message(
                chat_id, msg_id, text, buttons=panels.to_buttons(rows) if rows else None, parse_mode="html", link_preview=False
            )
        except errors.MessageNotModifiedError:
            pass
        except Exception as exc:  # noqa: BLE001
            log.warning("could not update the upload message: %s: %s", type(exc).__name__, exc)

    # ------------------------------------------------------- only the Share URL
    async def _on_link(self, event: Any, parts: list) -> None:
        """No to the upload offer asks about the link only (``n``); Yes (``l``) uploads and returns it; ``g`` looks for it again."""
        action = parts[1]
        if action == "z":
            await event.answer()
            await event.edit("👍 Okay, it is not uploaded to the Database Channel.", buttons=None, parse_mode="html")
            return
        try:
            if len(parts) != {"n": 4, "l": 4, "g": 3}[action]:
                raise ValueError(parts)
            numbers = [int(x) for x in parts[2:]]
            if action != "g" and numbers[1] <= 0:
                raise ValueError(parts)
        except ValueError:
            await event.answer()
            return
        if action == "n":
            await event.answer()
            yes = [[("✅ Yes", f"{PREFIX}:l:{numbers[0]}:{numbers[1]}"), ("❌ No", f"{PREFIX}:z")]]
            await event.edit(link_question_text(), buttons=panels.to_buttons(yes), parse_mode="html", link_preview=False)
            return
        key = (event.chat_id, event.message_id)
        if key in self._busy:
            await event.answer("I'm already on it…")
            return
        self._busy.add(key)
        try:
            await event.answer()
            try:
                await event.edit(processing_text(""), buttons=None, parse_mode="html")
            except errors.MessageNotModifiedError:
                pass
        except BaseException:
            self._busy.discard(key)
            raise
        self._spawn(self._run_link(key, *numbers) if action == "l" else self._run_url(key, numbers[0]))

    async def _run_link(self, key: Tuple[int, int], source_chat: int, source_id: int) -> None:
        chat_id, msg_id = key
        name = ""
        try:
            msg = await self.client.get_messages(source_chat, ids=source_id)
            if msg is None or getattr(msg, "file", None) is None:
                await self._show(chat_id, msg_id, "❌ <b>That episode is not there anymore</b>\nIt was deleted from the chat, so there is nothing to upload.")
                return
            name = getattr(msg.file, "name", None) or ""
            try:
                copy = await self._copy(msg, source_chat)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("could not upload %s to the Database Channel: %s: %s", name, type(exc).__name__, exc)
                retry = [[("🔁 Try again", f"{PREFIX}:l:{source_chat}:{source_id}"), ("✖ Close", "x:close")]]
                await self._show(
                    chat_id, msg_id,
                    f"❌ <b>The upload to the Database Channel failed</b>\n{_name(name)}\n{_why(exc)}\n"
                    "Check that the bot is an admin of the Database Channel and can post there.",
                    retry,
                )
                return
            await self._url(chat_id, msg_id, name, _id_of(copy) or 0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("getting the share url went wrong")
            await self._show(chat_id, msg_id, f"❌ <b>Something went wrong</b>\n{_name(name)}\n{_why(exc)}")
        finally:
            self._busy.discard(key)

    async def _run_url(self, key: Tuple[int, int], copy_id: int) -> None:
        try:
            await self._url(key[0], key[1], "", copy_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("getting the share url went wrong")
            await self._show(key[0], key[1], f"❌ <b>Something went wrong</b>\n{_why(exc)}")
        finally:
            self._busy.discard(key)

    async def _url(self, chat_id: int, msg_id: int, name: str, copy_id: int) -> None:
        """The Share URL of the copy: looked for here first, and when this bot cannot see it, the channel post bot (whose userbot can) is asked."""
        done = "✅ Uploaded to the Database Channel" if name else ""
        again = [[("🔁 Look for the URL again", f"{PREFIX}:g:{copy_id}"), ("✖ Close", "x:close")]]
        if not copy_id:
            await self._show(chat_id, msg_id, f"⚠️ <b>Uploaded, but I do not know which post it is</b>\n{_name(name)}\nLook for the Share URL in the Database Channel.")
            return
        await self._show(chat_id, msg_id, processing_text(name, done, "⏳ Waiting for the Share URL button…"))
        url = await self._bot_url(copy_id)
        if not url:
            flow = Flow(ref=secrets.token_hex(3))
            self.flows[flow.ref] = flow
            try:
                try:
                    note = await self.client.send_message(
                        self.cfg.comm_chat, link_notice_text(post_id=copy_id, ref=flow.ref), parse_mode=None, link_preview=False
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    await self._show(
                        chat_id, msg_id,
                        f"⚠️ <b>I could not see the Share URL, and could not write in the communication chat</b>\n{_name(name)}\n{_why(exc)}", again,
                    )
                    return
                flow.notice_id = _id_of(note) or 0

                async def got_it() -> None:
                    await self._show(chat_id, msg_id, processing_text(name, done, "📥 The channel post bot is looking for the Share URL…"))

                await self._show(chat_id, msg_id, processing_text(name, done, "⏳ Asking the channel post bot (its userbot can see it)…"))
                verdict, reply = await self._answers(flow, got_it, self.delete_wait)
            finally:
                self.flows.pop(flow.ref, None)
            if verdict == "ok":
                url = line_of("url", reply)
            if not url:
                why = {
                    "fail": classify(reply)[1], "down": f"The channel post bot did not answer within {int(self.ack_wait)} seconds (down?).",
                    "silent": "The channel post bot did not say how it went.", "ok": "The channel post bot sent no link.",
                }[verdict]
                await self._show(
                    chat_id, msg_id,
                    f"⚠️ <b>Uploaded, but no Share URL</b>\n{_name(name)}\n{esc(clip(why or 'No reason was given.', 500))}", again,
                )
                return
        await self._show(chat_id, msg_id, link_text(name, url))

    async def _bot_url(self, copy_id: int) -> str:
        """Look at the copy for its Share URL button for ``link_wait`` seconds (the file store adds it a few seconds after the post)."""
        loop = asyncio.get_running_loop()
        end = loop.time() + self.link_wait
        while True:
            try:
                url = share_link(await self.client.get_messages(self.cfg.db_channel, ids=copy_id))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.info("could not read the copy in the Database Channel: %s: %s", type(exc).__name__, exc)
                return ""  # this bot cannot read it: no use waiting, the channel post bot is asked
            if url or loop.time() >= end:
                return url
            await asyncio.sleep(self.link_poll)

    # ------------------------------------------------------- deleting what was published
    async def _on_delete(self, event: Any, parts: list) -> None:
        """The buttons of a published post: 🗑 Delete post (``d``), Yes (``dy``), No (``dn``); ``x`` is the "🗑 Post deleted" note an
        earlier version left on the message of a deleted post: it has nothing to do, so pressing it just removes it."""
        action = parts[1]
        if action == "x":
            await event.answer("This post is already deleted.")
            await self._mark_deleted(event.chat_id, event.message_id)
            return
        if action == "dn":
            await event.answer("Okay, nothing is deleted.")
            await self._close(event)
            return
        try:
            if len(parts) != (5 if action == "d" else 6):
                raise ValueError(parts)
            copy_id, channel, post = int(parts[2]), int(parts[3]), int(parts[4])
            shown = int(parts[5]) if action == "dy" else event.message_id  # the message of the post, whose button this is
            if copy_id < 0 or post <= 0 or channel == 0:
                raise ValueError(parts)
        except ValueError:
            await event.answer()
            return
        if action == "d":  # ask first, in a message of its own: the post's message stays as it is
            await event.answer()
            yes = f"{PREFIX}:dy:{copy_id}:{channel}:{post}:{shown}"
            await self.client.send_message(
                event.chat_id, confirm_text(channel), buttons=panels.to_buttons([[("✅ Yes, delete it", yes), ("❌ No", f"{PREFIX}:dn")]]),
                parse_mode="html", link_preview=False, reply_to=shown,
            )
            return
        key = (event.chat_id, event.message_id)
        if key in self._busy:
            await event.answer("I'm already on it…")
            return
        self._busy.add(key)
        try:
            await event.answer()
            try:
                await event.edit(deleting_text(), buttons=None, parse_mode="html")
            except errors.MessageNotModifiedError:
                pass
        except BaseException:
            self._busy.discard(key)
            raise
        self._spawn(self._delete(key, copy_id, channel, post, shown))

    @staticmethod
    async def _close(event: Any) -> None:
        try:
            await event.delete()
        except Exception:  # noqa: BLE001 - Telegram refuses to delete messages older than 48 hours
            try:
                await event.edit("Closed.", buttons=None)
            except Exception:  # noqa: BLE001
                pass

    async def _delete(self, key: Tuple[int, int], copy_id: int, channel: int, post: int, shown: int) -> None:
        """Have the channel post bot delete the post, and when it says it is gone, delete the copy in the Database Channel."""
        chat_id, msg_id = key
        flow: Optional[Flow] = None
        again = [[("🔁 Try again", f"{PREFIX}:dy:{copy_id}:{channel}:{post}:{shown}"), ("✖ Close", f"{PREFIX}:dn")]]
        try:
            flow = Flow(ref=secrets.token_hex(3))
            self.flows[flow.ref] = flow  # before the notice goes out, so that no answer can come back too early
            await self._show(chat_id, msg_id, deleting_text("⏳ Waiting for the channel post bot…"))
            try:
                note = await self.client.send_message(
                    self.cfg.comm_chat, delete_notice_text(channel=channel, message=post, ref=flow.ref), parse_mode=None, link_preview=False
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("could not write in the communication chat: %s: %s", type(exc).__name__, exc)
                await self._show(
                    chat_id, msg_id,
                    f"⚠️ <b>I could not write in the communication chat</b>\n{_why(exc)}\nNothing was deleted. Is the bot a member of that chat?",
                    again,
                )
                return
            flow.notice_id = _id_of(note) or 0

            async def got_it() -> None:
                await self._show(chat_id, msg_id, deleting_text("📥 The channel post bot got it and is deleting the post…"))

            rows = again
            verdict, reply = await self._answers(flow, got_it, self.delete_wait)
            if verdict == "ok":
                problem = await self._delete_copy(copy_id)
                if problem is None:
                    text, rows = deleted_text(reply, bool(copy_id)), None
                    await self._mark_deleted(chat_id, shown)
                else:
                    text = copy_kept_text(problem)
            elif verdict == "fail":
                text = delete_failed_text(classify(reply)[1])
            elif verdict == "down":
                text = delete_down_text(self.ack_wait)
            else:
                text = delete_silent_text(self.delete_wait)
            await self._show(chat_id, msg_id, text, rows)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("deleting the published post went wrong")
            await self._show(chat_id, msg_id, f"❌ <b>Something went wrong</b>\n{_why(exc)}", again)
        finally:
            self._busy.discard(key)
            if flow is not None:
                self.flows.pop(flow.ref, None)

    async def _delete_copy(self, copy_id: int) -> Optional[str]:
        """Delete the episode's copy in the Database Channel: ``None`` when it is gone (it also counts when it was gone already),
        else why not, as text for the message."""
        if not copy_id:
            return None
        try:
            await self.client.delete_messages(self.cfg.db_channel, [copy_id])
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("could not delete message %s of the Database Channel: %s: %s", copy_id, type(exc).__name__, exc)
            return clip(f"{type(exc).__name__}: {exc}", 250)
        return None

    async def _mark_deleted(self, chat_id: int, msg_id: int) -> None:
        """The message of the post keeps its text and loses its Delete button: the post is gone, so there is nothing left to press."""
        try:
            await self.client.edit_message(chat_id, msg_id, buttons=None)
        except errors.MessageNotModifiedError:
            pass
        except Exception as exc:  # noqa: BLE001
            log.warning("could not remove the button of the post message: %s: %s", type(exc).__name__, exc)

    # ------------------------------------------------- the channel post bot's answers
    async def on_chat_message(self, event: Any) -> None:
        """A message of the channel post bot in the communication chat."""
        try:
            if getattr(event, "chat_id", None) != self.cfg.comm_chat or getattr(event, "sender_id", None) != self.cfg.post_bot_id:
                return  # (the event filter does this already)
            text = getattr(event, "raw_text", None) or ""
            kind = classify(text)[0]
            if kind == "other":
                return
            flow = self._flow_for(event, text)
            if flow is not None:
                flow.inbox.put_nowait((kind, text))
        except Exception:  # noqa: BLE001
            log.exception("could not read a message in the communication chat")

    def _flow_for(self, event: Any, text: str) -> Optional[Flow]:
        """The upload an answer belongs to: the one whose Ref it repeats, or whose notice it replies to."""
        ref = line_of("ref", text)
        if ref and ref in self.flows:
            return self.flows[ref]
        target = reply_target(event)
        if target:
            return next((f for f in self.flows.values() if f.notice_id == target), None)
        if len(self.flows) == 1:  # a plain message, and only one upload waiting for an answer
            return next(iter(self.flows.values()))
        return None

    # ---------------------------------------------------------------- plumbing
    def _spawn(self, coro: Any) -> "asyncio.Future[Any]":
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def idle(self) -> None:
        """Wait until no upload is under way (the tests use this)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)
        self._tasks.clear()


def _id_of(sent: Any) -> Optional[int]:
    if isinstance(sent, (list, tuple)):
        sent = sent[0] if sent else None
    value = getattr(sent, "id", None)
    return value if isinstance(value, int) else None
