"""Validation + persistence for every user-editable setting.

Used by both the slash commands and the inline "send me the value" flow, so the
two can never disagree.  Functions return ``(ok, html_message)``.

``db`` is whatever keeps the setting: the ``Database`` for the user's own settings, a ``FormatSettings`` for a saved format
(the template and the caption live in formats, not in the user's settings).
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

from .db import DEFAULT_CAPTION, Database
from .media import parse_track_tokens, validate_track_tokens
from .parser import (
    CAPTION_VARS,
    DEFAULT_TEMPLATE,
    TEMPLATE_VARS,
    finalize_name,
    render_template,
    sample_values,
    sanitize,
)
from .panels import fmt_tokens
from .utils import clip, esc

CLEAR_WORDS = {"off", "none", "clear", "reset", "-", "remove"}
META_KINDS = {
    "mt_title": "title", "mt_author": "author", "mt_artist": "artist",
    "mt_audio": "audio", "mt_subtitle": "subtitle", "mt_video": "video",
}
META_COMMANDS = {
    "settitle": "mt_title", "setauthor": "mt_author", "setartist": "mt_artist",
    "setaudio": "mt_audio", "setsubtitle": "mt_subtitle", "setvideo": "mt_video",
}
_TOKEN_RE = re.compile(r"\{(\w+)\}")


def preview_template(template: str) -> str:
    sample, values = sample_values()
    out = finalize_name(sanitize(render_template(template, values)), ".mkv")
    return f"Preview for <code>{esc(sample)}</code>:\n<code>{esc(out)}</code>"


def _is_clear(text: str) -> bool:
    return text.strip().lower() in CLEAR_WORDS


async def apply_setting(db: Database, uid: int, kind: str, text: str) -> Tuple[bool, str]:
    text = (text or "").strip()

    if kind == "template":
        if not text:
            return False, "❌ The template can't be empty."
        if _is_clear(text):
            text = DEFAULT_TEMPLATE
        if len(text) > 200:
            return False, "❌ That template is too long (200 characters max)."
        unknown = sorted(set(_TOKEN_RE.findall(text)) - set(TEMPLATE_VARS))
        await db.update_settings(uid, template=text)
        msg = "✅ Template saved.\n" + preview_template(text)
        if unknown:
            msg += "\n⚠️ Unknown variables (kept as plain text): " + ", ".join("{%s}" % u for u in unknown)
        return True, msg

    if kind == "caption":
        if not text:
            return False, "❌ The caption can't be empty. Send <code>reset</code> to restore the default."
        if _is_clear(text):
            text = DEFAULT_CAPTION
        if len(text) > 800:
            return False, "❌ That caption is too long (800 characters max, formatting and link addresses included)."
        unknown = sorted(set(_TOKEN_RE.findall(text)) - set(CAPTION_VARS))
        await db.update_settings(uid, caption=text)
        msg = "✅ Caption template saved."
        if unknown:
            msg += "\n⚠️ Unknown variables (kept as plain text): " + ", ".join("{%s}" % u for u in unknown)
        return True, msg

    if kind == "remove":
        if _is_clear(text):
            await db.update_settings(uid, remove_words=[])
            return True, "✅ Remove-words list cleared."
        words = [w.strip() for w in re.split(r"[,\n]", text) if w.strip()]
        if not words:
            return False, "❌ Send at least one word, separated by commas."
        if len(words) > 50 or any(len(w) > 60 for w in words):
            return False, "❌ Up to 50 words of 60 characters each."
        await db.update_settings(uid, remove_words=words)
        return True, "✅ I'll strip: " + esc(", ".join(words))

    if kind in META_KINDS:
        field = META_KINDS[kind]
        if not text:
            return False, "❌ Send a value, or <code>off</code> to clear it."
        if _is_clear(text):
            await db.update_settings(uid, meta={field: ""})
            return True, f"✅ {field.title()} cleared."
        if len(text) > 200:
            return False, "❌ That's too long (200 characters max)."
        await db.update_settings(uid, meta={field: text})
        note = ""
        if not (await db.get_settings(uid))["metadata_on"]:
            note = "\nℹ️ Metadata is Off — switch it On in /metadata for this to apply."
        return True, f"✅ {field.title()} set to <code>{esc(clip(text, 80))}</code>.{note}"

    if kind in ("keep_audio", "keep_subtitle"):
        label = "Keep Audio" if kind == "keep_audio" else "Keep Subtitle"
        if _is_clear(text):
            await db.update_settings(uid, **{kind: []})
            return True, f"✅ {label} cleared — all tracks are kept."
        tokens = parse_track_tokens(text)
        if not tokens:
            return False, "❌ Send language names (<code>English Japanese</code>) or numbers (<code>2 1</code>)."
        problem = validate_track_tokens(tokens)
        if problem:
            return False, "❌ " + esc(problem)
        if len(tokens) > 12:
            return False, "❌ That's too many tracks (12 max)."
        await db.update_settings(uid, **{kind: tokens})
        msg = f"✅ {label}: {esc(fmt_tokens(tokens))}"
        if not (await db.get_settings(uid))["metadata_on"]:
            await db.update_settings(uid, metadata_on=True)
            msg += "\nℹ️ Metadata was Off, so I turned it On. Use /metadata to switch it off."
        return True, msg

    raise ValueError(f"unknown setting kind {kind!r}")


def parse_channel(text: str) -> Tuple[bool, Optional[int], str]:
    """``-1001234567890`` -> id; ``off`` -> None."""
    text = text.strip()
    if _is_clear(text):
        return True, None, ""
    if re.fullmatch(r"-?\d{5,20}", text):
        return True, int(text), ""
    return False, None, "❌ Send the numeric chat id, like <code>-1001234567890</code> (not @username)."


_CHANNEL_LINK = re.compile(r"(?:https?://)?(?:www\.)?t\.me/c/(\d{5,15})(?:/\d+){0,2}/?", re.I)
CHANNEL_ID_HINT = "❌ Send the channel id, like <code>-1001234567890</code> (a channel id starts with -100), or <code>off</code> to clear it."


def parse_format_channel(text: str) -> Tuple[bool, Optional[int], str]:
    """The Channel option of a manual format: ``-1001234567890`` -> that id, ``off`` -> ``None``.

    A bare channel id (``1234567890``) and a link to a post of a private channel (``t.me/c/1234567890/5``) work too: both get
    the ``-100`` in front that Telegram puts on channel ids.
    """
    text = (text or "").strip()
    if _is_clear(text):
        return True, None, ""
    link = _CHANNEL_LINK.fullmatch(text)
    if link:
        return True, -1_000_000_000_000 - int(link.group(1)), ""
    if re.fullmatch(r"-?\d{5,20}", text):
        value = int(text)
        if value > 0:
            return True, -1_000_000_000_000 - value, ""
        if value <= -1_000_000_000_001:
            return True, value, ""
    return False, None, CHANNEL_ID_HINT
