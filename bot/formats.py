"""Saved formats (auto and manual): the pieces shared by the menus, the handlers and the pipeline.

A format is a named bundle of a naming template, a caption, a thumbnail, the six metadata titles and the Keep
Audio / Keep Subtitle lists.  Every other setting (mode, file type, remove words, target channel) still comes
from the user's normal settings.  The caption never does: a format that was given none sends the new
file name in bold.

There are two kinds, twelve of each.  An **auto** format renames every file the user sends (the one in use is chosen from
the /start menu); a **manual** format is chosen with /rename and renames one file.  The user's default metadata is a preset:
a format can copy it ("Apply default metadata"), and an auto format that has no metadata of its own uses it while it is On.
"""
from __future__ import annotations

import copy
from typing import Any, Dict, Optional, Tuple

from .db import DEFAULT_CAPTION, DEFAULT_FORMAT, DEFAULT_KIND, DEFAULT_SETTINGS, Database

NAME_MAX = 30
KIND_LABELS = {"auto": "Auto", "manual": "Manual"}
KIND_ICONS = {"auto": "🤖", "manual": "✋"}
# format field -> the kind settings_ops.apply_setting() understands
FIELD_KINDS: Dict[str, str] = {
    "template": "template", "caption": "caption",
    "title": "mt_title", "author": "mt_author", "artist": "mt_artist",
    "audio": "mt_audio", "subtitle": "mt_subtitle", "video": "mt_video",
    "keep_audio": "keep_audio", "keep_subtitle": "keep_subtitle",
}
_EDITABLE = ("template", "caption", "meta", "keep_audio", "keep_subtitle")


def other_kind(kind: str) -> str:
    return "manual" if kind == "auto" else "auto"


def validate_name(text: Optional[str]) -> Tuple[bool, str]:
    """``(True, cleaned name)`` or ``(False, html error)``."""
    name = " ".join((text or "").split())
    if not name:
        return False, "❌ Send a name for the format."
    if len(name) > NAME_MAX:
        return False, f"❌ That name is too long ({NAME_MAX} characters max)."
    return True, name


def has_metadata(fmt: Dict[str, Any]) -> bool:
    return any(fmt["meta"].values()) or bool(fmt["keep_audio"] or fmt["keep_subtitle"])


def has_default_metadata(settings: Dict[str, Any]) -> bool:
    """Has the user set any default metadata (a title or the like, or a Keep Audio / Keep Subtitle list)?"""
    return any(settings["meta"].values()) or bool(settings["keep_audio"] or settings["keep_subtitle"])


def default_metadata_changes(settings: Dict[str, Any]) -> Dict[str, Any]:
    """What ``Database.update_format`` takes to make a format's metadata the user's default metadata."""
    return dict(meta=dict(settings["meta"]), keep_audio=list(settings["keep_audio"]), keep_subtitle=list(settings["keep_subtitle"]))


def default_auto_format() -> Dict[str, Any]:
    """What renames the files of a user who has no auto format: the built-in template and caption, no thumbnail, no metadata."""
    out = copy.deepcopy(DEFAULT_FORMAT)
    out.update(kind="auto", id=0, name="", has_thumb=False)
    return out


def apply_format(settings: Dict[str, Any], fmt: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The user's settings with the format's template, caption, metadata and keep lists laid over them.

    ``fmt`` is ``None`` for an auto rename of a user who has no auto format: the built-in defaults.

    Metadata is on whenever the format sets anything, whatever the Metadata switch says: the format carries its own
    metadata.  A manual format with none leaves the file alone (no ffmpeg pass).  An auto format with none leaves the user's
    default metadata in charge: it applies while its switch is On, as it always did for auto renames.
    """
    fmt = fmt or default_auto_format()
    out = copy.deepcopy(settings)
    out["template"] = fmt["template"]
    out["caption"] = fmt.get("caption") or DEFAULT_CAPTION  # never a caption from anywhere else
    if has_metadata(fmt) or fmt.get("kind", DEFAULT_KIND) == "manual":
        out["meta"] = dict(fmt["meta"])
        out["keep_audio"] = list(fmt["keep_audio"])
        out["keep_subtitle"] = list(fmt["keep_subtitle"])
        out["metadata_on"] = has_metadata(fmt)
    return out


def _as_settings(fmt: Dict[str, Any]) -> Dict[str, Any]:
    """A format shaped like the user's settings, which is what ``apply_setting`` reads."""
    out = copy.deepcopy(DEFAULT_SETTINGS)
    out.update(
        template=fmt["template"], caption=fmt["caption"], keep_audio=list(fmt["keep_audio"]),
        keep_subtitle=list(fmt["keep_subtitle"]), metadata_on=True,
    )
    out["meta"] = dict(fmt["meta"])
    return out


class FormatSettings:
    """Edits one saved format through the two methods ``apply_setting`` uses on ``Database``.

    That keeps a single copy of every validation rule and message for the user's settings and the formats.
    Metadata is always "on" here, so the "metadata is off" notes never show for a format.
    """

    def __init__(self, db: Database, fid: int):
        self.db = db
        self.fid = fid

    async def get_settings(self, uid: int) -> Dict[str, Any]:
        fmt = await self.db.get_format(uid, self.fid)
        if fmt is None:
            raise LookupError("that format no longer exists")
        return _as_settings(fmt)

    async def update_settings(self, uid: int, **changes: Any) -> Dict[str, Any]:
        wanted = {k: v for k, v in changes.items() if k in _EDITABLE}
        if wanted and await self.db.update_format(uid, self.fid, **wanted) is None:
            raise LookupError("that format no longer exists")
        return await self.get_settings(uid)
