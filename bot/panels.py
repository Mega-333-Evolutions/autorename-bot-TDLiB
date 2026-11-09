"""Inline-menu panels: pure functions returning ``(html_text, button_rows)``.

Button rows are lists of ``(label, callback_data)``; ``to_buttons`` converts them
as Telegram buttons.  Panel texts stay well below Telegram's 1024-character caption
limit because the home banner makes every panel a photo caption.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import richtext
from .db import DEFAULT_CAPTION, DEFAULT_FORMAT
from .formats import KIND_ICONS, KIND_LABELS, has_metadata, other_kind
from .media import LANGUAGES, canonical_lang
from .parser import CAPTION_VARS, TEMPLATE_VARS
from .tg import Button
from .utils import clip, code_each, esc

Row = List[Tuple[str, str]]
Panel = Tuple[str, List[Row]]

DOT = "◈"

# The variable lists shown in the panels and prompts: every variable is its own <code> block, so tapping one copies only it.
TEMPLATE_VARIABLES = code_each("{%s}" % name for name in TEMPLATE_VARS)
CAPTION_VARIABLES = code_each("{%s}" % name for name in CAPTION_VARS if name != "ext")  # {ext} was never listed for captions
# What {1episode} ... {3episode} do, in one line (they are the episode with zeros in front, 2 to 4 digits wide)
EPISODE_ZEROS = (
    "Zeros in front of the episode: <code>{1episode}</code> makes it 2 digits (4 → 04), <code>{2episode}</code> 3 digits (4 → 004), "
    "<code>{3episode}</code> 4 digits (4 → 0004). A number that already has that many digits stays as it is."
)


INLINE = "inline:"  # a button whose data starts with this opens Telegram's chat picker, with "@bot <the rest>" typed in the chat


def share_query(fid: int) -> str:
    """The inline query a Share format button types into the chosen chat (``App.on_inline`` answers it with a card)."""
    return f"share_{fid}"


def to_buttons(rows: Sequence[Row]):
    def button(label: str, data: str):
        if data.startswith(INLINE):
            return Button.switch_inline(label, query=data[len(INLINE):], same_peer=False)
        return Button.inline(label, data.encode())

    return [[button(label, data) for label, data in row] for row in rows]


def without_inline(rows: Sequence[Row]) -> List[Row]:
    """The rows with every Share format button turned into the plain one that explains how to switch inline mode on."""
    prefix = INLINE + "share_"
    return [[(label, "fm:sh:" + data[len(prefix):] if data.startswith(prefix) else data) for label, data in row] for row in rows]


def nav(back: str = "", *, home: bool = True) -> List[Row]:
    row: Row = []
    if back:
        row.append(("◀ Back", f"m:{back}"))
    if home and back != "home":
        row.append(("🏠 Home", "m:home"))
    row.append(("✖ Close", "x:close"))
    return [row]


def _check(label: str, active: bool) -> str:
    return f"{label} ✅" if active else label


def _code(value: str, limit: int = 100) -> str:
    return f"<code>{esc(clip(value, limit))}</code>" if value else "—"


def fmt_tokens(tokens: Iterable[str]) -> str:
    """``["english", "2"]`` -> ``English, 2``"""
    out = []
    for t in tokens:
        out.append(t if t.isdigit() else (canonical_lang(t) or t).title())
    return ", ".join(out) if out else "—"


# ---------------------------------------------------------------------------
# panels
# ---------------------------------------------------------------------------


def _auto_name(auto: Optional[Dict[str, Any]]) -> str:
    """The auto format in use, for a panel; ``auto`` is ``None`` for someone who has none."""
    return f"<b>{esc(clip(auto['name'], 30))}</b>" if auto else "none saved (the default is used)"


def _template_of(auto: Optional[Dict[str, Any]]) -> str:
    return auto["template"] if auto else DEFAULT_FORMAT["template"]


def home(s: Dict[str, Any], *, owner: bool, auto: Optional[Dict[str, Any]] = None) -> Panel:
    """The main menu.  ``auto`` is the auto format in use (``None``: the user has none)."""
    channel = f"channel <code>{s['target_channel']}</code>" if s.get("target_channel") else "this chat"
    metadata_on = s["metadata_on"] or bool(auto and has_metadata(auto))  # a format with metadata of its own writes it, switch or not
    text = (
        "<b>🏷 Auto Rename Bot</b>\n\n"
        "Send me a file (document, video or audio) and I'll rename it with your auto format and send it back.\n\n"
        f"<b>Mode:</b> <code>{s['mode'].upper()}</code> · <b>Type:</b> <code>{s['file_type'].upper()}</code>"
        f" · <b>Metadata:</b> <code>{'ON' if metadata_on else 'OFF'}</code>\n"
        f"<b>Auto format:</b> {_auto_name(auto)}\n"
        f"<b>Template:</b> {_code(_template_of(auto), 120)}\n"
        f"<b>Send to:</b> {channel}"
    )
    rows: List[Row] = [
        [("Auto Format", "m:auto"), ("Rename", "m:rename")],
        [("Metadata", "m:meta"), ("File Type", "m:ftype")],
        [("Formats", "m:formats"), ("Help", "m:help")] + ([("Admin", "m:admin")] if owner else []),
        [("✖ Close", "x:close")],
    ]
    return text, rows


def rename(s: Dict[str, Any], auto: Optional[Dict[str, Any]] = None) -> Panel:
    words = ", ".join(s["remove_words"]) or "—"
    text = (
        "<b>RENAME MODE</b>\n\n"
        "Filename mode reads details from the file name.\n"
        "Caption mode reads details from the message caption.\n\n"
        f"Current mode: <code>{s['mode'].upper()}</code>\n"
        "Command: /mode\n\n"
        f"<b>Template</b> of the auto format {_auto_name(auto)}: {_code(_template_of(auto), 120)}\n"
        "Change it in /formats.\n\n"
        f"<b>Remove words:</b> {esc(clip(words, 100))}"
    )
    rows: List[Row] = [
        [(_check("Filename", s["mode"] == "filename"), "set:mode:filename"),
         (_check("Caption", s["mode"] == "caption"), "set:mode:caption")],
        [("Remove words", "in:remove")],
    ] + nav("home")
    return text, rows


def metadata(s: Dict[str, Any]) -> Panel:
    m = s["meta"]
    on = s["metadata_on"]
    lines = [f"<b>🏷 Your default metadata is currently: {'On' if on else 'Off'}</b>", ""]
    for label, key in (("Title", "title"), ("Author", "author"), ("Artist", "artist"),
                       ("Audio", "audio"), ("Subtitle", "subtitle"), ("Video", "video")):
        lines.append(f"{DOT} <b>{label}</b> ▸ {_code(m.get(key, ''), 60)}")
    lines.append(f"{DOT} <b>Keep Audio</b> ▸ {esc(fmt_tokens(s['keep_audio']))}")
    lines.append(f"{DOT} <b>Keep Subtitle</b> ▸ {esc(fmt_tokens(s['keep_subtitle']))}")
    lines += ["", "Auto renames use it when their format has no metadata of its own. To put it in a format, tap "
                  "<b>Apply default metadata</b> on the format."]
    rows: List[Row] = [
        [(_check("On", on), "set:meta:on"), (_check("Off", not on), "set:meta:off")],
        [("Title", "in:mt_title"), ("Author", "in:mt_author"), ("Artist", "in:mt_artist")],
        [("Audio", "in:mt_audio"), ("Subtitle", "in:mt_subtitle"), ("Video", "in:mt_video")],
        [("Tool", "m:tool")],
        [("How to Set Metadata", "help:meta")],
    ] + nav("home")
    return "\n".join(lines), rows


def tool(s: Dict[str, Any]) -> Panel:
    text = (
        "<b>🛠 TOOL — keep tracks</b>\n\n"
        "Only the tracks you list are kept, in the order you list them. Everything else is dropped.\n\n"
        f"{DOT} <b>Keep Audio</b> ▸ {esc(fmt_tokens(s['keep_audio']))}\n"
        f"{DOT} <b>Keep Subtitle</b> ▸ {esc(fmt_tokens(s['keep_subtitle']))}\n\n"
        "Use language names (<code>English Japanese</code>) or track numbers (<code>2 1</code>).\n"
        "Commands: /keepaudio · /keepsubtitle · /tracks (reply to a file)\n"
        "Needs metadata to be On."
    )
    rows: List[Row] = [
        [("Keep Audio", "in:keep_audio"), ("Keep Subtitle", "in:keep_subtitle")],
        [("Clear Audio", "clr:keep_audio"), ("Clear Subtitle", "clr:keep_subtitle")],
    ] + nav("meta")
    return text, rows


def metadata_help() -> Panel:
    text = (
        "<b>How to set metadata</b>\n\n"
        "Tap a field, then send the value — or use a command:\n"
        "/settitle Tokyo Revengers\n"
        "/setauthor @MyChannel\n"
        "/setartist @MyChannel\n"
        "/setaudio {lang} Audio\n"
        "/setsubtitle {lang}\n"
        "/setvideo @MyChannel\n\n"
        "Send <code>off</code> instead of a value to clear a field. "
        "<code>{lang}</code> becomes each track's language name.\n"
        "Then switch metadata On. Works for mkv, mp4, m4v, mov and webm (title/artist tags for mp3, m4a, flac, ogg, opus)."
    )
    return text, nav("meta")


def filetype(s: Dict[str, Any]) -> Panel:
    cur = s["file_type"]
    text = (
        "<b>FILE TYPE</b>\n\n"
        "How renamed files are sent:\n"
        "• <b>Same as original</b> — video stays video, audio stays audio, a document stays a document\n"
        "• <b>Document / Video / Audio</b> — always send it that way\n\n"
        "The file extension always stays the same.\n\n"
        f"Current: <code>{cur.upper()}</code>\nCommand: /filetype"
    )
    rows: List[Row] = [
        [(_check("Same as original", cur == "same"), "set:ftype:same"),
         (_check("Document", cur == "document"), "set:ftype:document")],
        [(_check("Video", cur == "video"), "set:ftype:video"), (_check("Audio", cur == "audio"), "set:ftype:audio")],
    ] + nav("home")
    return text, rows


def help_panel() -> Panel:
    text = (
        "<b>How to use</b>\n\n"
        "1. Send a file — I rename it with your <b>auto format</b> (pick it in the /start menu) and send it back.\n"
        "2. /rename — one file with a <b>manual format</b>: pick one, then send the file.\n"
        "3. /formats — 12 auto and 12 manual formats. A format holds the template, caption, thumbnail and metadata: edit, copy between kinds, share.\n"
        "4. /mode — read details from the file name or the caption.\n"
        "5. /metadata, /keepaudio, /keepsubtitle — default metadata and tracks (formats can copy it).\n"
        "6. /filetype — send as document, video or audio.\n"
        "7. /sequence then /sort — put files in episode order.\n"
        "8. /check, /mediainfo, /ethumb, /dc, /speed — inspect files.\n"
        "9. /size and /resize (reply to a picture) — its dimensions, or a copy at an exact size.\n"
        "10. /queue and /cancel — manage jobs.\n\n"
        f"Template variables: {TEMPLATE_VARIABLES}\n"
        "A [bracket] group is dropped when a variable inside it is empty."
    )
    return text, nav("home")


def admin(owner_id: int, admins: Sequence[int], running: int, waiting: int, log_channel: Any) -> Panel:
    shown = ", ".join(f"<code>{a}</code>" for a in list(admins)[:10]) or "none"
    more = f" (+{len(admins) - 10} more)" if len(admins) > 10 else ""
    text = (
        "<b>ADMIN</b>\n\n"
        f"<b>Owner:</b> <code>{owner_id}</code>\n"
        f"<b>Allowed users:</b> {shown}{more}\n"
        f"<b>Queue:</b> {running} running · {waiting} waiting\n"
        f"<b>Log channel:</b> {('<code>%s</code>' % log_channel) if log_channel else 'not set (LOG_CHANNEL)'}\n\n"
        "Commands: /addadmin &lt;id&gt; · /deladmin &lt;id&gt; · /admins · /smedia"
    )
    rows: List[Row] = [
        [("📊 Stats", "adm:stats"), ("🟢 Status", "adm:status")],
        [("🛑 Cancel all jobs", "adm:clear")],
    ] + nav("home")
    return text, rows


# ---------------------------------------------------------------------------
# saved formats
# ---------------------------------------------------------------------------

FORMAT_FIELD_LABELS = (
    ("title", "Title"), ("author", "Author"), ("artist", "Artist"),
    ("audio", "Audio"), ("subtitle", "Subtitle"), ("video", "Video"),
)


def _format_buttons(formats: Sequence[Dict[str, Any]], prefix: str, active_id: int = 0) -> List[Row]:
    """One button per format, two to a row; the one in use (``active_id``) is ticked."""
    rows: List[Row] = []
    row: Row = []
    for f in formats:
        row.append((_check(clip(f["name"], 24), f["id"] == active_id), f"{prefix}:{f['id']}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def formats_home(auto_count: int, manual_count: int, limit: int) -> Panel:
    """/formats: the two kinds of format, to choose from."""
    text = (
        "<b>🗂 FORMATS</b>\n\n"
        "A format saves its own template, caption, thumbnail, metadata titles and Keep Audio / Keep Subtitle lists "
        f"under a name, like <i>One Piece</i>. You can keep {limit} of each kind:\n\n"
        "🤖 <b>Auto rename</b> — renames every file you send me. Choose the one in use in the /start menu.\n"
        "✋ <b>Manual rename</b> — you pick one with /rename, for a single file.\n\n"
        "A format can be copied from one kind to the other."
    )
    rows: List[Row] = [[(f"🤖 Auto rename ({auto_count}/{limit})", "fm:l:auto"), (f"✋ Manual rename ({manual_count}/{limit})", "fm:l:manual")]]
    return text, rows + nav("home")


def formats_menu(formats: Sequence[Dict[str, Any]], limit: int, kind: str = "manual", active_id: int = 0) -> Panel:
    """The formats of one kind (``active_id``: the auto format in use)."""
    if kind == "auto":
        intro = "These rename every file you send me. The one marked ✅ is in use: tap a format to view or edit it."
    else:
        intro = "You pick one of these with /rename, for a single file. Tap a format to view or edit it."
    text = f"<b>{KIND_ICONS[kind]} {KIND_LABELS[kind].upper()} FORMATS</b> ({len(formats)}/{limit})\n\n{intro}"
    if not formats:
        text += f"\n\nYou have no {kind} formats yet."
    rows = _format_buttons(formats, "fm:v", active_id)
    if len(formats) < limit:
        rows.append([("➕ New format", f"fm:new:{kind}")])
    else:
        text += f"\n\nYou have all {limit} {kind} formats. Delete one to add another."
    return text, rows + nav("formats")


def auto_menu(formats: Sequence[Dict[str, Any]], active_id: int, limit: int) -> Panel:
    """The /start menu's Auto Format screen: choose the auto format that renames your files."""
    text = (
        "<b>🤖 AUTO RENAME FORMAT</b>\n\n"
        "Every file you send me is renamed with the format marked ✅. Tap another one to use it instead.\n"
        "For a single file with a manual format, use /rename."
    )
    if not formats:
        text += (
            "\n\nYou have no auto formats yet, so the default template is used. Create one to set your own template, "
            "caption, thumbnail and metadata."
        )
    rows = _format_buttons(formats, "au", active_id)
    if active_id:
        rows.append([("✏️ Edit the one in use", f"fm:v:{active_id}")])
    last: Row = [("⚙️ Manage", "fm:l:auto")]
    if len(formats) < limit:
        last.append(("➕ New format", "fm:new:auto"))
    return text, rows + [last] + nav("home")


def _custom_caption(fmt: Dict[str, Any]) -> bool:
    """Has the format a caption of its own?  Otherwise it sends the new file name in bold."""
    return fmt["caption"] != DEFAULT_CAPTION


THUMB_NONE = "— (a frame of the video is used)"


def _format_lines(fmt: Dict[str, Any], *, thumb_yes: str, thumb_no: str) -> List[str]:
    """What a format holds, one line per field."""
    m = fmt["meta"]
    lines = [
        f"{DOT} <b>Template</b> ▸ {_code(fmt['template'], 60)}",
        f"{DOT} <b>Caption</b> ▸ {richtext.preview(fmt['caption'], 60)}" + ("" if _custom_caption(fmt) else " · default"),
        f"{DOT} <b>Thumbnail</b> ▸ " + (thumb_yes if fmt.get("has_thumb") else thumb_no),
    ]
    for key, label in FORMAT_FIELD_LABELS:
        lines.append(f"{DOT} <b>{label}</b> ▸ {_code(m.get(key, ''), 50)}")
    lines.append(f"{DOT} <b>Keep Audio</b> ▸ {esc(clip(fmt_tokens(fmt['keep_audio']), 50))}")
    lines.append(f"{DOT} <b>Keep Subtitle</b> ▸ {esc(clip(fmt_tokens(fmt['keep_subtitle']), 50))}")
    return lines


def format_view(fmt: Dict[str, Any], *, share_inline: bool = False, active: bool = False, upload_on: bool = False) -> Panel:
    """A format's screen.  ``active``: it is the auto format in use.  ``share_inline``: the Share format button opens the chat
    picker straight away; that only works while inline mode is on for the bot (Telegram refuses the message otherwise), so
    without it the button explains how.  ``upload_on``: uploading to the Database Channel is set up, so a manual format shows
    its Channel option."""
    kind = fmt.get("kind", "manual")
    has_channel_option = upload_on and kind == "manual"
    lines = [f"<b>🗂 {esc(clip(fmt['name'], 30))}</b> · {KIND_ICONS[kind]} {KIND_LABELS[kind]}", ""]
    lines += _format_lines(fmt, thumb_yes="✅ saved", thumb_no=THUMB_NONE)
    if has_channel_option:
        lines.append(f"{DOT} <b>Channel</b> ▸ " + (_code(str(fmt["channel"])) if fmt.get("channel") else "— (optional)"))
    if kind == "manual":
        lines += ["", "Use it with /rename. Mode, file type and the rest come from your normal settings."]
    elif active:
        lines += ["", "✅ In use: every file you send is renamed with it."]
    else:
        lines += ["", "Not in use. Tap Use for auto rename to rename your files with it."]
    if kind == "auto" and not has_metadata(fmt):
        lines.append("No metadata of its own: your default metadata is used while it is On.")
    fid = fmt["id"]
    thumb_row: Row = [("Thumbnail", f"fm:th:{fid}")]
    if fmt.get("has_thumb"):
        thumb_row.append(("🗑 Delete Thumb", f"fm:thd:{fid}"))
    rows: List[Row] = [
        [("Name", f"fm:in:{fid}:name"), ("Template", f"fm:in:{fid}:template"), ("Caption", f"fm:in:{fid}:caption")],
        thumb_row,
    ]
    if _custom_caption(fmt):
        rows.append([("↩ Reset Caption", f"fm:cr:{fid}")])
    rows += [
        [("Title", f"fm:in:{fid}:title"), ("Author", f"fm:in:{fid}:author"), ("Artist", f"fm:in:{fid}:artist")],
        [("Audio", f"fm:in:{fid}:audio"), ("Subtitle", f"fm:in:{fid}:subtitle"), ("Video", f"fm:in:{fid}:video")],
        [("Keep Audio", f"fm:in:{fid}:keep_audio"), ("Keep Subtitle", f"fm:in:{fid}:keep_subtitle")],
    ]
    if has_channel_option:
        channel_row: Row = [("Channel", f"fm:in:{fid}:channel")]
        if fmt.get("channel"):
            channel_row.append(("🗑 Clear Channel", f"fm:chc:{fid}"))
        rows.append(channel_row)
    rows += [
        [("🏷 Apply default metadata", f"fm:dm:{fid}")],
        [(f"📋 Copy to {other_kind(kind)} formats", f"fm:cp:{fid}")],
    ]
    if kind == "auto" and not active:
        rows.append([("✅ Use for auto rename", f"fm:use:{fid}")])
    rows += [
        [("📤 Share format", INLINE + share_query(fid) if share_inline else f"fm:sh:{fid}")],
        [("🗑 Delete format", f"fm:del:{fid}")],
        [("◀ Back", f"fm:l:{kind}"), ("🏠 Home", "m:home"), ("✖ Close", "x:close")],
    ]
    return "\n".join(lines), rows


def format_copied(source: Dict[str, Any], copy: Dict[str, Any]) -> Panel:
    """What copying a format to the other kind shows."""
    kind = copy["kind"]
    text = (
        f"✅ <b>{esc(clip(source['name'], 30))}</b> is copied to your {kind} formats as <b>{esc(clip(copy['name'], 30))}</b>.\n"
        "The copy is a format of its own: change it without touching the original."
    )
    rows: List[Row] = [
        [("📂 Open the copy", f"fm:v:{copy['id']}")],
        [("◀ Back to the original", f"fm:v:{source['id']}")],
        [("✖ Close", "x:close")],
    ]
    return text, rows


def format_metadata_applied(fmt: Dict[str, Any]) -> str:
    """The note shown above a format after "Apply default metadata"."""
    return f"✅ Your default metadata is now in <b>{esc(clip(fmt['name'], 30))}</b>."


INLINE_OFF = "Sharing needs inline mode. In @BotFather: /mybots → this bot → Bot Settings → Inline Mode → Turn on. Then tap Share format again."
SHARE_GONE = "⚠️ <b>That shared format isn't available anymore.</b>\nThe person who shared it deleted it. Ask them to share it again."


def _a(kind: str) -> str:
    """``An auto`` / ``A manual``"""
    return f"{'An' if kind == 'auto' else 'A'} {kind}"


def share_card(fmt: Dict[str, Any]) -> Tuple[str, str, str]:
    """``(title, description, html text)`` of the card that is sent to the chat the owner picked."""
    name = clip(fmt["name"], 30)
    kind = fmt.get("kind", "manual")
    return (
        f"🗂 {name}",
        "Tap to send this format to the chat",
        f"📦 <b>{esc(name)}</b>\n{_a(kind)} rename format for this bot. Open it to see what's in it and add it to your own formats.",
    )


def shared_format(fmt: Dict[str, Any], token: str, *, mine: bool = False) -> Panel:
    """What opening a shared format shows: what is in it, and a button to add it to your own formats."""
    kind = fmt.get("kind", "manual")
    lines = [
        f"<b>📦 {esc(clip(fmt['name'], 30))}</b> · {KIND_ICONS[kind]} {KIND_LABELS[kind]}",
        "This is your own format." if mine else f"{_a(kind)} rename format shared with you.",
        "",
    ]
    lines += _format_lines(fmt, thumb_yes="✅ included", thumb_no=THUMB_NONE)
    use = "/rename" if kind == "manual" else "auto rename"
    if mine:
        lines += ["", "Whoever you send it to can look at it here and add a copy to their own formats."]
        rows: List[Row] = [[("🗂 Open format", f"fm:v:{fmt['id']}")]]
    else:
        lines += ["", f"Add it to your {kind} formats to use it for {use}. You can change anything in your copy afterwards."]
        rows = [[("➕ Add to my formats", f"fs:add:{token}")]]
    return "\n".join(lines), rows + [[("✖ Close", "x:close")]]


def shared_added(fmt: Dict[str, Any]) -> Panel:
    kind = fmt.get("kind", "manual")
    use = "Use it with /rename" if kind == "manual" else "Choose it in the /start menu to rename your files with it"
    text = f"✅ <b>{esc(clip(fmt['name'], 30))}</b> is now in your {kind} formats.\n{use}, or open it to change anything."
    return text, [[("🗂 Open format", f"fm:v:{fmt['id']}")], [("✖ Close", "x:close")]]


def format_delete_confirm(fmt: Dict[str, Any]) -> Panel:
    text = f"<b>Delete the format “{esc(clip(fmt['name'], 30))}”?</b>\n\nIts template, caption, thumbnail and metadata are removed. This can't be undone."
    return text, [[("🗑 Yes, delete", f"fm:ok:{fmt['id']}"), ("◀ Keep it", f"fm:v:{fmt['id']}")]]


def rename_picker(formats: Sequence[Dict[str, Any]]) -> Panel:
    """/rename: the manual formats (an auto format is chosen in the /start menu)."""
    if not formats:
        text = (
            "<b>🏷 MANUAL RENAME</b>\n\nYou have no manual formats yet.\n"
            "A format keeps a template, caption, thumbnail and metadata under a name, like <i>One Piece</i>. Create one, then come back here.\n"
            "Files you just send me are renamed with your auto format."
        )
        return text, [[("➕ Create a format", "fm:new:manual")], [("✖ Close", "x:close")]]
    text = "<b>🏷 MANUAL RENAME</b>\n\nChoose the manual format to rename with. I'll ask for your file next."
    rows = _format_buttons(formats, "rn:p") + [[("⚙️ Manage formats", "fm:l:manual"), ("✖ Close", "x:close")]]
    return text, rows


def rename_ready(fmt: Dict[str, Any], minutes: int) -> Panel:
    text = (
        f"✅ <b>{esc(clip(fmt['name'], 30))}</b> selected.\n\n"
        "Now send me the file to rename. I'll apply this format to it and send it back.\n"
        "It works for one file at a time: for another file, send /rename again.\n"
        f"I'll wait {minutes} minutes. /cancel to stop."
    )
    return text, [[("🔄 Change format", "rn:menu"), ("⏹ Stop", "rn:stop")]]


def _detail_lines(info: Any) -> str:
    """Title, Season, Episode and Quality always (``None`` when not found); Year and Codec only when found."""
    fields = [("Title", info.title), ("Season", info.season), ("Episode", info.episode), ("Quality", info.quality)]
    fields += [(label, value) for label, value in (("Year", info.year), ("Codec", info.codec)) if value]
    return "\n".join(f"➦ {label} ⇛ <code>{esc(clip(value, 60)) if value else 'None'}</code>" for label, value in fields)


def check_text(mode: str, name_info: Any, caption_info: Any, new_name: str) -> str:
    """What /check shows: the details read from the file name and from the caption, and the name they give."""
    return (
        "<b>Yᴏᴜ Cᴀɴ Cʜᴀɴɢᴇ Mᴏᴅᴇ Bʏ</b> /mode\n\n"
        f"<b>Rᴇɴᴀᴍɪɴɢ Mᴏᴅᴇ:</b> <code>{esc(mode.upper())}</code>\n\n"
        f"<b>Fɪʟᴇɴᴀᴍᴇ:</b>\n{_detail_lines(name_info)}\n\n"
        f"<b>Cᴀᴘᴛɪᴏɴ Mᴏᴅᴇ:</b>\n{_detail_lines(caption_info)}\n\n"
        f"<b>Nᴇᴡ Nᴀᴍᴇ:</b>\n<code>{esc(clip(new_name, 150))}</code>"
    )


def ethumb_prompt(minutes: int) -> Panel:
    text = (
        "<b>🖼 EXTRACT THUMBNAIL</b>\n\n"
        "Send me a file or video and I'll send back its thumbnail.\n"
        "It works for one file at a time: for another file, send /ethumb again.\n"
        f"I'll wait {minutes} minutes. /cancel to stop."
    )
    return text, [[("⏹ Stop", "et:stop")]]


def smedia_menu(current: str) -> Panel:
    """/smedia: ``current`` is what /start shows above the menu now: ``image``, ``gif`` or ``""`` (nothing)."""
    shown = {"image": "an image", "gif": "a GIF"}.get(current, "nothing, just the menu")
    text = (
        "<b>🖼 START MEDIA</b>\n\n"
        f"/start shows: <b>{shown}</b>\n\n"
        "Choose what goes above the menu. I keep a copy of it, so it survives restarts."
    )
    rows: List[Row] = [[("🖼 Image", "sm:image"), ("🎞 GIF", "sm:gif")]]
    if current:
        rows.append([("🗑 Remove", "sm:rm")])
    return text, rows + [[("✖ Close", "x:close")]]


def smedia_prompt(kind: str, minutes: int) -> Panel:
    what = "the image (a photo, or an image sent as a file)" if kind == "image" else "the GIF (a Telegram GIF or animation)"
    text = (
        f"<b>{'🖼' if kind == 'image' else '🎞'} Send {what}.</b>\n\n"
        "I'll set it as the /start media and show you the result.\n"
        f"I'll wait {minutes} minutes. /cancel to stop."
    )
    return text, [[("◀ Back", "sm:menu"), ("⏹ Stop", "sm:stop")]]


def sequence_intro(limit: int) -> Panel:
    text = (
        "<b>📑 SEQUENCE</b>\n\n"
        f"Send me the files to put in order (up to {limit}), in any order. I only collect them for now.\n"
        "When they're all here, send /sort: I'll send them back in episode order, then list the missing and repeated episodes.\n"
        "The episode is read from the file name, or from the caption when your /mode is Caption.\n"
        "/cancel to throw them away."
    )
    return text, [[("✖ Cancel", "sq:cx")]]


def sequence_status(count: int, limit: int) -> Panel:
    text = f"📥 <b>{count} file{'s' if count != 1 else ''} collected</b>"
    if count >= limit:
        text += f"\n\nThat's the most I can sort at once ({limit}). Send /sort now."
    else:
        text += "\n\nSend more files, or /sort when you're done."
    return text, [[("🔢 Sort now", "sq:sort"), ("🗑 Discard", "sq:cx")]]


# ---------------------------------------------------------------------------
# input prompts
# ---------------------------------------------------------------------------

PROMPTS: Dict[str, str] = {
    "remove": (
        "<b>Send the words to strip from names.</b>\n\n"
        "Separate them with commas, e.g. <code>@SomeChannel, [Group], www.site.com</code>\n"
        "Send <code>off</code> to clear the list."
    ),
    "mt_title": "<b>Send the Title</b> to write into the file's metadata (or <code>off</code> to clear).",
    "mt_author": "<b>Send the Author</b> to write into the file's metadata (or <code>off</code> to clear).",
    "mt_artist": "<b>Send the Artist</b> to write into the file's metadata (or <code>off</code> to clear).",
    "mt_audio": "<b>Send the title for audio tracks</b> (or <code>off</code> to clear).\n<code>{lang}</code> becomes each track's language, e.g. <code>{lang} Audio</code>.",
    "mt_subtitle": "<b>Send the title for subtitle tracks</b> (or <code>off</code> to clear).\n<code>{lang}</code> becomes each track's language.",
    "mt_video": "<b>Send the title for the video track</b> (or <code>off</code> to clear).",
    "keep_audio": (
        "<b>Which audio tracks should I keep?</b>\n\n"
        "Send language names (<code>English Japanese</code>) or track numbers (<code>2 1</code>). "
        "The order you give is the output order; all other tracks are dropped.\n"
        "Send <code>off</code> to keep everything."
    ),
    "keep_subtitle": (
        "<b>Which subtitle tracks should I keep?</b>\n\n"
        "Send language names (<code>English</code>) or track numbers (<code>2</code>). "
        "The order you give is the output order; all other tracks are dropped.\n"
        "Send <code>off</code> to keep everything."
    ),
}


def input_prompt(kind: str, back: str) -> Panel:
    return PROMPTS[kind], [[("✖ Cancel", f"in:cancel:{back}")]]


FORMAT_PROMPTS: Dict[str, str] = {
    "new": "<b>Send a name for the new format</b>, like <code>One Piece</code> or <code>Dragon Ball</code> (30 characters max).",
    "name": "<b>Send the new name</b> for this format (30 characters max).",
    "thumb": "<b>Send a photo</b> to use as this format's thumbnail.",
    "template": (
        "<b>Send the new filename template.</b>\n\n"
        f"Variables: {TEMPLATE_VARIABLES}\n"
        f"{EPISODE_ZEROS}\n"
        "A [bracket] or (paren) group disappears when a variable inside it is empty.\n"
        "If only an episode number is found, the season defaults to 01.\n\n"
        "Example: <code>[S{season}E{episode}] {title} [{quality}]</code>"
    ),
    "caption": (
        "<b>Send the caption for this format.</b>\n\n"
        f"Variables: {CAPTION_VARIABLES}\n"
        "Format it the way you do in any message (<b>bold</b>, <i>italic</i>, links…) and I'll keep it exactly like that. "
        "No HTML needed.\n"
        "Send <code>reset</code> to go back to the default: the new file name in bold."
    ),
    "title": PROMPTS["mt_title"], "author": PROMPTS["mt_author"], "artist": PROMPTS["mt_artist"],
    "audio": PROMPTS["mt_audio"], "subtitle": PROMPTS["mt_subtitle"], "video": PROMPTS["mt_video"],
    "keep_audio": PROMPTS["keep_audio"], "keep_subtitle": PROMPTS["keep_subtitle"],
    "channel": (
        "<b>Send the id of the channel</b> this format's episodes are published in, like <code>-1001234567890</code>.\n\n"
        "After /rename I can upload the episode to the Database Channel; the channel post bot then posts it in this channel.\n"
        "It is optional: send <code>off</code> to clear it (the episode is then only uploaded, nothing is posted)."
    ),
}


def format_prompt(field: str, fid: int, kind: str = "") -> Panel:
    """The "send me the value" screen for one field of a format (``fid`` 0 = a format of ``kind`` that is being created)."""
    cancel = f"fm:cx:{fid}" if fid else (f"fm:l:{kind}" if kind else "m:formats")
    return FORMAT_PROMPTS[field], [[("✖ Cancel", cancel)]]


def known_languages() -> str:
    return ", ".join(sorted(LANGUAGES))
