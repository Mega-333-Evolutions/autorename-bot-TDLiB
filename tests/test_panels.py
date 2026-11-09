import copy
import html
import re

import pytest

from bot import panels
from bot.app import BACK, PANEL_NAMES, SHARE_QUERY
from bot.db import DEFAULT_SETTINGS
from bot.media import LANGUAGES
from bot.tg import types
from bot.utils import code_each

from .tgfakes import button_rows, variables_line

CAPTION_LIMIT = 1000  # Telegram allows 1024 for a caption; keep a margin
TEMPLATE_LIST = "{title} {season} {episode} {1episode} {2episode} {3episode} {quality} {year} {codec} {filename} {ext}"
CAPTION_LIST = "{filename} {original} {title} {season} {episode} {1episode} {2episode} {3episode} {quality} {year} {codec} {size} {duration}"


def visible(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def settings(**over):
    s = copy.deepcopy(DEFAULT_SETTINGS)
    s.update(over)
    return s


def worst_case():
    longest = sorted(LANGUAGES, key=len, reverse=True)[:12]
    return settings(
        mode="caption", file_type="document", metadata_on=True,
        remove_words=["R" * 60] * 50,
        meta={k: "M" * 200 for k in DEFAULT_SETTINGS["meta"]},
        keep_audio=list(longest), keep_subtitle=list(longest), target_channel=-1001234567890123,
    )


def sample_format(**over):
    fmt = {"id": 12, "name": "One Piece", "kind": "manual", "has_thumb": False, "template": "[S{season}E{episode}] {title}",
           "caption": "<b>{filename}</b>", "meta": {k: "" for k in ("title", "author", "artist", "audio", "subtitle", "video")}, "keep_audio": [], "keep_subtitle": []}
    fmt.update(over)
    return fmt


def worst_format(fid=12, kind="auto", **over):
    longest = sorted(LANGUAGES, key=len, reverse=True)[:12]
    fmt = sample_format(
        id=fid, kind=kind, name="N" * 30, has_thumb=True, template="W" * 200, caption="C" * 800,
        meta={k: "M" * 200 for k in DEFAULT_SETTINGS["meta"]}, keep_audio=list(longest), keep_subtitle=list(longest),
    )
    fmt.update(over)
    return fmt


def every_panel(s, auto=None):
    """Every panel of the bot for these settings; ``auto`` is the auto format in use (``None``: the user has none)."""
    formats = [worst_format(i, "auto", name=f"{i:02d}" + "N" * 28) for i in range(1, 13)]
    manual = [worst_format(i + 20, "manual", name=f"{i:02d}" + "M" * 28) for i in range(1, 13)]
    out = {
        "home_owner": panels.home(s, owner=True, auto=auto),
        "home_user": panels.home(s, owner=False, auto=auto),
        "rename": panels.rename(s, auto),
        "meta": panels.metadata(s),
        "tool": panels.tool(s),
        "ftype": panels.filetype(s),
        "help": panels.help_panel(),
        "metahelp": panels.metadata_help(),
        "admin": panels.admin(1, list(range(1000, 1030)), 3, 4, -1001234567890),
        "formats_home": panels.formats_home(12, 12, 12),
        "auto_menu": panels.auto_menu(formats, 3, 12),
        "auto_menu_empty": panels.auto_menu([], 0, 12),
        "auto_list": panels.formats_menu(formats, 12, "auto", 3),
        "manual_list": panels.formats_menu(manual, 12, "manual"),
        "manual_list_empty": panels.formats_menu([], 12, "manual"),
        "auto_format": panels.format_view(formats[2], active=False),
        "auto_format_in_use": panels.format_view(formats[2], active=True, share_inline=True),
        "manual_format": panels.format_view(manual[0], share_inline=True),
        "copied": panels.format_copied(formats[0], manual[0]),
        "delete": panels.format_delete_confirm(formats[0]),
        "picker": panels.rename_picker(manual),
        "picker_empty": panels.rename_picker([]),
        "ready": panels.rename_ready(manual[0], 10),
        "shared": panels.shared_format(formats[0], "tok"),
        "shared_mine": panels.shared_format(manual[0], "tok", mine=True),
        "shared_added": panels.shared_added(manual[0]),
    }
    out.update({f"prompt_{k}": panels.input_prompt(k, BACK[k]) for k in panels.PROMPTS})
    out.update({f"format_prompt_{k}": panels.format_prompt(k, 12) for k in panels.FORMAT_PROMPTS if k != "new"})
    out["format_prompt_new"] = panels.format_prompt("new", 0, "auto")
    return out


CASES = [(settings(), None), (worst_case(), None), (worst_case(), worst_format())]
CASE_IDS = ["defaults", "worst-case settings", "worst-case settings and format"]


@pytest.mark.parametrize("s, auto", CASES, ids=CASE_IDS)
def test_every_panel_fits_in_a_photo_caption(s, auto):
    for name, (text, _rows) in every_panel(s, auto=auto).items():
        assert len(visible(text)) <= CAPTION_LIMIT, f"{name} is {len(visible(text))} characters"


@pytest.mark.parametrize("s, auto", CASES, ids=CASE_IDS)
def test_callback_data_is_short_unique_per_panel_and_labels_are_present(s, auto):
    for name, (_text, rows) in every_panel(s, auto=auto).items():
        seen = set()
        for row in rows:
            assert 1 <= len(row) <= 4, name
            for label, data in row:
                assert label.strip() and len(label) <= 32, (name, label)
                assert 0 < len(data.encode()) <= 64, (name, data)
                assert data not in seen, (name, data)
                seen.add(data)


def test_every_panel_html_is_balanced():
    for name, (text, _rows) in every_panel(worst_case(), auto=worst_format()).items():
        for tag in ("b", "code", "i"):
            assert text.count(f"<{tag}>") == text.count(f"</{tag}>"), (name, tag)


@pytest.mark.parametrize("name, text, shown", [
    ("template prompt", panels.FORMAT_PROMPTS["template"], TEMPLATE_LIST),
    ("caption prompt", panels.FORMAT_PROMPTS["caption"], CAPTION_LIST),
    ("help panel", panels.help_panel()[0], TEMPLATE_LIST),
])
def test_every_variable_is_its_own_tap_to_copy_block(name, text, shown):
    # Tapping a <code> block in Telegram copies all of it, so "{quality}" must not share a block with the other variables.
    line, blocks = variables_line(text)
    assert line.split(": ", 1)[1] == shown, name  # the list reads the same as it always did
    assert blocks == shown.split(), name  # ...with one block per variable
    assert "<code>{quality}</code>" in text, name


def test_code_each_makes_one_escaped_block_per_item():
    assert code_each(["{a}", "<b>", 3]) == "<code>{a}</code> <code>&lt;b&gt;</code> <code>3</code>"
    assert code_each([]) == ""


def test_the_share_button_is_the_chat_picker_or_the_plain_button_that_explains():
    fmt = sample_format()
    on = [d for row in panels.format_view(fmt, share_inline=True)[1] for _l, d in row]
    off = [d for row in panels.format_view(fmt, share_inline=False)[1] for _l, d in row]
    assert "inline:share_12" in on and "fm:sh:12" not in on
    assert "fm:sh:12" in off and not any(d.startswith("inline:") for d in off)
    assert panels.format_view(fmt)[1] == panels.format_view(fmt, share_inline=False)[1]  # the safe one is the default
    assert SHARE_QUERY.fullmatch(panels.share_query(12))  # what the bot listens for is what the button types


def test_without_inline_swaps_only_the_share_buttons():
    rows = panels.format_view(sample_format(), share_inline=True)[1]
    assert panels.without_inline(rows) == panels.format_view(sample_format(), share_inline=False)[1]
    assert panels.without_inline([[("A", "a:1"), ("B", "in:thing")]]) == [[("A", "a:1"), ("B", "in:thing")]]
    assert panels.without_inline([]) == []


def test_inline_rows_become_share_buttons_and_the_rest_stay_callbacks():
    (row,) = panels.to_buttons([[("📤 Share", "inline:share_7"), ("Go", "a:1")]])
    assert isinstance(row[0].type, types.InlineButtonTypeSwitchInline)
    assert row[0].type.query == "share_7" and row[0].type.same_peer is False  # any chat can be picked
    assert isinstance(row[1].type, types.InlineButtonTypeCallback) and row[1].type.data == b"a:1"


def test_the_shared_format_page_and_card():
    fmt = sample_format(meta={"title": "<One Piece>", "author": "", "artist": "", "audio": "", "subtitle": "", "video": ""}, has_thumb=True)
    text, rows = panels.shared_format(fmt, "tok_en-1")
    assert "<b>📦 One Piece</b>" in text and "✅ included" in text and "&lt;One Piece&gt;" in text
    assert rows == [[("➕ Add to my formats", "fs:add:tok_en-1")], [("✖ Close", "x:close")]]
    text, rows = panels.shared_format(fmt, "tok_en-1", mine=True)
    assert "your own format" in text and rows == [[("🗂 Open format", "fm:v:12")], [("✖ Close", "x:close")]]
    title, description, card = panels.share_card(sample_format(name="A & B"))
    assert title == "🗂 A & B" and "A &amp; B" in card and "<b>" in card and description


def test_every_input_button_has_a_prompt_and_a_back_target():
    assert set(panels.PROMPTS) == set(BACK)
    assert set(BACK.values()) <= PANEL_NAMES
    for name, (_text, rows) in every_panel(settings()).items():
        for row in rows:
            for _label, data in row:
                if data.startswith("in:") and not data.startswith("in:cancel"):
                    assert data[3:] in panels.PROMPTS, (name, data)
                if data.startswith("m:"):
                    assert data[2:] in PANEL_NAMES, (name, data)


def test_navigation_always_offers_a_way_out():
    for name, (_text, rows) in every_panel(settings()).items():
        data = [d for row in rows for _l, d in row]
        if name.startswith("prompt_"):
            assert any(d.startswith("in:cancel:") for d in data), name
        elif name.startswith("format_prompt_"):
            assert any(d.startswith(("fm:cx:", "fm:l:")) for d in data), name
        elif name in ("delete", "ready"):  # a choice and a wait for a file: they have their own way out (Keep it, Stop)
            assert any(d.startswith(("fm:v:", "rn:stop")) for d in data), name
        else:
            assert "x:close" in data, name


def test_home_shows_admin_button_only_to_the_owner():
    owner = [d for row in panels.home(settings(), owner=True)[1] for _l, d in row]
    user = [d for row in panels.home(settings(), owner=False)[1] for _l, d in row]
    assert "m:admin" in owner and "m:admin" not in user
    for expected in ("m:auto", "m:rename", "m:meta", "m:ftype", "m:formats", "m:help"):
        assert expected in user
    assert "m:cap" not in owner + user  # the caption and the thumbnail are edited in /formats, not in the start menu


def test_the_start_menu_has_no_way_to_edit_the_template_caption_or_thumbnail():
    auto = sample_format(kind="auto")
    for name, (_text, rows) in every_panel(settings(), auto=auto).items():
        if name.startswith(("format_prompt_", "auto_format", "manual_format", "shared", "copied", "delete", "picker", "ready")):
            continue  # a format's own screens are where those are edited
        data = [d for row in rows for _l, d in row]
        assert not any(d in ("in:template", "in:caption", "clr:caption", "m:cap") or d.startswith("th:") for d in data), name
        assert not any(label in ("Template", "Set Caption", "Caption & Thumbnail", "View Thumb", "Delete Thumb")
                       for row in rows for label, _d in row), name
    rename_text, rename_rows = panels.rename(settings(), auto)
    assert "Change it in /formats" in rename_text and [d for row in rename_rows for _l, d in row][2:4] == ["in:remove", "m:home"]
    assert set(panels.PROMPTS) == {"remove", "mt_title", "mt_author", "mt_artist", "mt_audio", "mt_subtitle", "mt_video", "keep_audio", "keep_subtitle"}
    assert "template" in panels.FORMAT_PROMPTS and "caption" in panels.FORMAT_PROMPTS and "thumb" in panels.FORMAT_PROMPTS  # only here


def test_the_help_lists_size_and_resize_and_says_where_a_format_is_edited():
    text = visible(panels.help_panel()[0])
    assert "/size and /resize (reply to a picture)" in text
    assert "/formats" in text and "template, caption, thumbnail and metadata" in text
    assert "Caption & Thumbnail" not in text and "Send a photo" not in text  # both are gone


def test_home_metadata_line_says_what_the_auto_format_in_use_will_do():
    def line(s, auto=None):
        return re.search(r"Metadata: (ON|OFF)", visible(panels.home(s, owner=False, auto=auto)[0])).group(1)

    assert line(settings(metadata_on=False)) == "OFF" and line(settings(metadata_on=True)) == "ON"  # no auto format: the switch
    plain = sample_format(kind="auto")
    own = sample_format(kind="auto", meta={**plain["meta"], "title": "T"})
    assert line(settings(metadata_on=False), own) == "ON"  # the format carries metadata of its own: the switch is not asked
    assert line(settings(metadata_on=False), plain) == "OFF" and line(settings(metadata_on=True), plain) == "ON"  # none: the default decides
    assert line(settings(metadata_on=False), sample_format(kind="auto", keep_audio=["english"])) == "ON"


def test_active_choices_get_a_checkmark():
    def labels(panel):
        return {data: label for row in panel[1] for label, data in row}

    r = labels(panels.rename(settings(mode="caption")))
    assert r["set:mode:caption"].endswith("✅") and not r["set:mode:filename"].endswith("✅")
    f = labels(panels.filetype(settings(file_type="audio")))
    assert [k for k, v in f.items() if v.endswith("✅")] == ["set:ftype:audio"]
    m = labels(panels.metadata(settings(metadata_on=True)))
    assert m["set:meta:on"].endswith("✅") and not m["set:meta:off"].endswith("✅")


def test_metadata_panel_matches_the_reference_layout():
    text, rows = panels.metadata(settings(metadata_on=True, keep_audio=["english", "2"], meta={"title": "T", "author": "", "artist": "", "audio": "", "subtitle": "", "video": ""}))
    plain = visible(text)
    assert "currently: On" in plain
    for label in ("Title", "Author", "Artist", "Audio", "Subtitle", "Video", "Keep Audio", "Keep Subtitle"):
        assert label in plain
    assert "English, 2" in plain
    data = [d for row in rows for _l, d in row]
    for expected in ("set:meta:on", "set:meta:off", "in:mt_title", "in:mt_author", "in:mt_artist", "in:mt_audio",
                     "in:mt_subtitle", "in:mt_video", "m:tool", "help:meta"):
        assert expected in data


def test_tool_panel_holds_keep_audio_and_keep_subtitle():
    text, rows = panels.tool(settings(keep_subtitle=["english"]))
    data = [d for row in rows for _l, d in row]
    assert {"in:keep_audio", "in:keep_subtitle", "clr:keep_audio", "clr:keep_subtitle"} <= set(data)
    assert "m:meta" in data  # Back goes to the metadata panel
    assert "English" in visible(text)


def test_fmt_tokens():
    assert panels.fmt_tokens([]) == "—"
    assert panels.fmt_tokens(["english", "2", "jpn"]) == "English, 2, Japanese"
    assert panels.fmt_tokens(["klingon"]) == "Klingon"


def test_html_in_user_values_is_escaped():
    s = settings(meta={**DEFAULT_SETTINGS["meta"], "title": "<i>x</i>"})
    auto = sample_format(kind="auto", name="<u>Mine</u>", template="<b>{title}</b> & co")
    assert "<b>{title}</b>" not in panels.rename(s, auto)[0]
    assert "&lt;b&gt;" in panels.rename(s, auto)[0] and "<u>Mine</u>" not in panels.rename(s, auto)[0]
    assert "<b>{title}</b>" not in panels.home(s, owner=True, auto=auto)[0] and "<u>" not in panels.home(s, owner=True, auto=auto)[0]
    assert "<i>x</i>" not in panels.metadata(s)[0]


def test_to_buttons_builds_callback_buttons():
    rows = [[("A", "a:1"), ("B", "b:2")], [("C", "c:3")]]
    assert button_rows(panels.to_buttons(rows)) == rows


def test_known_languages_lists_names():
    assert "english" in panels.known_languages()


def test_the_format_screen_shows_its_caption_and_offers_to_reset_a_custom_one():
    plain = panels.format_view(sample_format())
    assert "<b>Caption</b> ▸ <b>{filename}</b> · default" in plain[0]
    assert "fm:in:12:caption" in [d for row in plain[1] for _l, d in row] and "fm:cr:12" not in [d for row in plain[1] for _l, d in row]

    custom = panels.format_view(sample_format(caption="<i>{filename}</i> · {size}"))
    assert "<b>Caption</b> ▸ <i>{filename}</i> · {size}" in custom[0] and "default" not in custom[0].split("Thumbnail")[0]
    assert ("↩ Reset Caption", "fm:cr:12") in [button for row in custom[1] for button in row]
    assert panels.without_inline(custom[1]) == panels.format_view(sample_format(caption="<i>{filename}</i> · {size}"), share_inline=False)[1]
    assert "caption" in panels.FORMAT_PROMPTS["caption"].lower() and "<code>reset</code>" in panels.FORMAT_PROMPTS["caption"]
