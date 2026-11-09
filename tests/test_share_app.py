"""Sharing a format: the Share format button, the card it sends to a chat, the page that opens, and Add to my formats."""
import logging

from sqlalchemy import func, select

from bot import app as appmod
from bot import panels
from bot.db import MAX_FORMATS, FormatShare
from bot.tg import buttons, types

from .appkit import ADMIN_CFG, LOG, OWNER, STRANGER, click, data_of, jpeg, say
from .tdfakes import check_object
from .test_formats_app import make_format
from .tgfakes import FakeInlineQuery, dispatch


async def share_count(ctx):
    async with ctx.db._sessions() as session:
        return (await session.execute(select(func.count()).select_from(FormatShare))).scalar()


async def ask(ctx, sender, text):
    """Somebody types ``@bot <text>`` in a chat."""
    query = FakeInlineQuery(sender, text)
    await dispatch(ctx.client, query)
    return query


async def share(ctx, name="One Piece", **changes):
    fmt = await make_format(ctx, name, **changes)
    return fmt, await ctx.db.share_token(OWNER, fmt["id"])


def share_buttons(rows):
    return [b for row in rows for b in row if b.text == "📤 Share format"]


def card_text(card):
    """The words of the message a card sends to the chat."""
    return card["input_message_content"]["text"]["text"]


# ---------------------------------------------------------------------------
# the Share format button
# ---------------------------------------------------------------------------


async def test_the_format_screen_has_a_share_button_that_opens_the_chat_picker(ctx):
    fmt = await make_format(ctx)
    cb = await click(ctx, OWNER, f"fm:v:{fmt['id']}")
    (button,) = share_buttons(cb.edits[-1][1])
    assert isinstance(button.type, types.InlineButtonTypeSwitchInline)
    assert button.type.query == f"share_{fmt['id']}" and button.type.same_peer is False  # pick any chat
    assert appmod.SHARE_QUERY.fullmatch(button.type.query)
    data = data_of(cb.edits[-1][1])
    assert data.index(f"inline:share_{fmt['id']}") < data.index(f"fm:del:{fmt['id']}")  # right above Delete


async def test_the_share_button_is_a_valid_telegram_button():
    """The format screen's rows become the keyboard TDLib is given, and pytdbot accepts every part of it (no network needed)."""
    fmt = {"id": 12, "name": "One Piece", "has_thumb": False, "template": "{title}", "caption": "<b>{filename}</b>",
           "meta": {k: "" for k in ("title", "author", "artist", "audio", "subtitle", "video")}, "keep_audio": [], "keep_subtitle": []}
    _text, rows = panels.format_view(fmt, share_inline=True)
    markup = buttons.markup(panels.to_buttons(rows))
    check_object(markup)
    kinds = [b["type"]["@type"] for row in markup["rows"] for b in row]
    assert kinds.count("inlineKeyboardButtonTypeSwitchInline") == 1 and kinds.count("inlineKeyboardButtonTypeCallback") == len(kinds) - 1
    (switch,) = [b for row in markup["rows"] for b in row if b["type"]["@type"] == "inlineKeyboardButtonTypeSwitchInline"]
    assert switch["type"]["query"] == "share_12" and switch["type"]["target_chat"]["@type"] == "targetChatChosen"  # any chat can be picked


async def test_without_inline_mode_the_share_button_explains_how_to_turn_it_on(ctx):
    ctx.client.inline_placeholder, ctx.client.server_inline = None, False
    fmt = await make_format(ctx)
    cb = await click(ctx, OWNER, f"fm:v:{fmt['id']}")  # the screen still works
    data = data_of(cb.edits[-1][1])
    assert f"fm:sh:{fmt['id']}" in data and not any(d.startswith("inline:") for d in data)
    assert cb.alerts == []

    cb = await click(ctx, OWNER, f"fm:sh:{fmt['id']}")
    assert cb.alerts == [panels.INLINE_OFF] and "Inline Mode" in cb.alerts[0] and len(cb.alerts[0]) <= 200
    assert cb.edits == []


async def test_turning_inline_mode_on_is_noticed(ctx):
    ctx.client.inline_placeholder, ctx.client.server_inline = None, False
    fmt = await make_format(ctx)
    await click(ctx, OWNER, f"fm:v:{fmt['id']}")
    calls = ctx.client.me_calls

    ctx.client.inline_placeholder, ctx.client.server_inline = "", True  # on, without a placeholder text
    cb = await click(ctx, OWNER, f"fm:v:{fmt['id']}")  # within the minute the bot doesn't ask again
    assert f"fm:sh:{fmt['id']}" in data_of(cb.edits[-1][1]) and ctx.client.me_calls == calls

    ctx.app._inline_checked -= appmod.INLINE_RECHECK + 1
    cb = await click(ctx, OWNER, f"fm:v:{fmt['id']}")
    assert f"inline:share_{fmt['id']}" in data_of(cb.edits[-1][1]) and ctx.client.me_calls == calls + 1
    await click(ctx, OWNER, f"fm:v:{fmt['id']}")
    assert ctx.client.me_calls == calls + 1  # on stays on, no more questions


async def test_tapping_the_plain_share_button_tries_the_real_one(ctx):
    """If the profile doesn't say so but Telegram accepts the share button, inline mode is on."""
    ctx.client.inline_placeholder = None  # the profile says off, the server accepts the button
    fmt = await make_format(ctx)
    cb = await click(ctx, OWNER, f"fm:v:{fmt['id']}")
    assert f"fm:sh:{fmt['id']}" in data_of(cb.edits[-1][1])

    cb = await click(ctx, OWNER, f"fm:sh:{fmt['id']}")
    assert cb.toasts == ["Inline mode is on. Tap Share format again."]
    assert f"inline:share_{fmt['id']}" in data_of(cb.edits[-1][1])
    cb = await click(ctx, OWNER, f"fm:v:{fmt['id']}")
    assert f"inline:share_{fmt['id']}" in data_of(cb.edits[-1][1])  # remembered


async def test_switching_inline_mode_off_later_does_not_break_the_format_screen(ctx):
    fmt = await make_format(ctx)
    cb = await click(ctx, OWNER, f"fm:v:{fmt['id']}")
    assert f"inline:share_{fmt['id']}" in data_of(cb.edits[-1][1])

    ctx.client.inline_placeholder, ctx.client.server_inline = None, False  # switched off in @BotFather meanwhile
    cb = await click(ctx, OWNER, f"fm:v:{fmt['id']}")
    assert cb.alerts == [] and f"fm:sh:{fmt['id']}" in data_of(cb.edits[-1][1])  # the plain button instead of an error
    cb = await click(ctx, OWNER, f"fm:sh:{fmt['id']}")
    assert cb.alerts == [panels.INLINE_OFF]


async def test_a_refused_share_button_is_also_handled_when_the_screen_comes_back_after_a_prompt(ctx):
    fmt = await make_format(ctx)
    await click(ctx, OWNER, f"fm:v:{fmt['id']}")  # inline mode is seen on
    ctx.client.inline_placeholder, ctx.client.server_inline = None, False
    await click(ctx, OWNER, f"fm:in:{fmt['id']}:title", msg_id=700)
    ev = await say(ctx, OWNER, "One Piece")
    assert "Something went wrong" not in "".join(ev.texts)
    (message,) = [t for t in ev.texts if "🗂 One Piece" in t]  # the screen was sent again, in a new message
    assert f"fm:sh:{fmt['id']}" in data_of(ev.out[-1][1]) and "Title" in message


# ---------------------------------------------------------------------------
# the card that goes to the chat
# ---------------------------------------------------------------------------


async def test_the_chat_picker_gets_a_card_that_opens_the_format(ctx):
    fmt = await make_format(ctx, "One Piece")
    query = await ask(ctx, OWNER, f"share_{fmt['id']}")
    assert query.answers[-1][1] == {"cache_time": 0, "private": True}
    (card,) = query.results
    assert card["@type"] == "inputInlineQueryResultArticle" and card["title"] == "🗂 One Piece"
    assert card["description"] == "Tap to send this format to the chat"
    token = await ctx.db.share_token(OWNER, fmt["id"])
    message = card["input_message_content"]
    assert "One Piece" in card_text(card) and [e["type"]["@type"] for e in message["text"]["entities"]] == ["textEntityTypeBold"]
    assert message["link_preview_options"]["is_disabled"] is True
    (button,) = [b for row in card["reply_markup"]["rows"] for b in row]
    assert button["text"] == "👁 View format" and button["type"]["url"] == f"https://t.me/RenameBot?start=fmt_{token}"
    check_object(card)  # something TDLib accepts

    again = await ask(ctx, OWNER, f"share_{fmt['id']}")  # the same link every time
    assert again.results[0]["reply_markup"]["rows"][0][0]["type"]["url"] == button["type"]["url"]


async def test_the_card_does_not_give_away_what_is_in_the_format(ctx):
    fmt = await make_format(ctx, "One Piece", meta={"title": "Secret title", "author": "@private"})
    (card,) = (await ask(ctx, OWNER, f"share_{fmt['id']}")).results
    assert "Secret title" not in card_text(card) and "@private" not in card_text(card)
    assert "E{episode}" not in card_text(card)


async def test_inline_queries_get_nothing_for_anything_but_the_owners_own_formats(ctx):
    mine = await make_format(ctx, "Mine")
    theirs = await make_format(ctx, "Theirs", uid=ADMIN_CFG)
    for who, text in (
        (OWNER, "share_999"), (OWNER, f"share_{theirs['id']}"), (OWNER, "hello"), (OWNER, ""), (OWNER, "share_"),
        (OWNER, f"share_{mine['id']}x"), (OWNER, "share_" + "9" * 30), (ADMIN_CFG, f"share_{mine['id']}"),
    ):
        query = await ask(ctx, who, text)
        assert query.results == [] and len(query.answers) == 1, (who, text)
    assert await share_count(ctx) == 0  # no link was made for any of them


async def test_strangers_get_no_results_and_no_notice(ctx):
    fmt = await make_format(ctx)
    query = await ask(ctx, STRANGER, f"share_{fmt['id']}")
    assert query.results == [] and len(query.answers) == 1
    assert STRANGER not in ctx.app._warned and not [m for m in ctx.client.sent if m[0] == LOG]  # anyone can type the bot's name


async def test_a_failure_making_the_card_still_answers_the_query(ctx, monkeypatch, caplog):
    fmt = await make_format(ctx)

    async def broken(*_a, **_k):
        raise RuntimeError("database is down")

    monkeypatch.setattr(ctx.db, "share_token", broken)
    with caplog.at_level(logging.ERROR):
        query = await ask(ctx, OWNER, f"share_{fmt['id']}")
    assert query.results == [] and len(query.answers) == 1
    assert "Error making the card" in caplog.text


# ---------------------------------------------------------------------------
# opening a shared format
# ---------------------------------------------------------------------------


async def test_opening_a_shared_format_shows_it_and_offers_to_add_it(ctx):
    fmt, token = await share(ctx, meta={"title": "One Piece", "video": "HD"}, keep_audio=["japanese", "2"])
    ev = await say(ctx, ADMIN_CFG, f"/start fmt_{token}")
    (text,) = ev.texts
    assert "📦 One Piece" in text and "shared with you" in text and "Main menu" not in text
    for label in ("Template", "Thumbnail", "Title", "Author", "Artist", "Audio", "Subtitle", "Video", "Keep Audio", "Keep Subtitle"):
        assert label in text, label
    assert "One Piece - E{episode}" in text and "<b>Title</b> ▸ <code>One Piece</code>" in text and "Japanese, 2" in text
    assert data_of(ev.out[0][1]) == [f"fs:add:{token}", "x:close"]
    assert await ctx.db.count_formats(ADMIN_CFG) == 0  # nothing is added until they say so


async def test_a_shared_format_shows_its_caption_and_the_copy_keeps_it(ctx):
    fmt, token = await share(ctx, caption="<i>{filename}</i> · {size}")
    ev = await say(ctx, ADMIN_CFG, f"/start fmt_{token}")
    assert "<b>Caption</b> ▸ <i>{filename}</i> · {size}" in ev.texts[0] and "· default" not in ev.texts[0]
    await click(ctx, ADMIN_CFG, f"fs:add:{token}")
    (mine,) = await ctx.db.list_formats(ADMIN_CFG)
    assert mine["caption"] == "<i>{filename}</i> · {size}"  # theirs from now on
    await ctx.db.update_format(ADMIN_CFG, mine["id"], caption="<b>{title}</b>")
    assert (await ctx.db.get_format(OWNER, fmt["id"]))["caption"] == "<i>{filename}</i> · {size}"

    _plain, plain_token = await share(ctx, "Dragon Ball")  # without a caption of its own: the file name in bold
    ev = await say(ctx, ADMIN_CFG, f"/start fmt_{plain_token}")
    assert "<b>Caption</b> ▸ <b>{filename}</b> · default" in ev.texts[0]


async def test_a_plain_start_still_opens_the_menu(ctx):
    for text in ("/start", "/start hello", "/start fmt", "/start x_fmt_abc"):
        ev = await say(ctx, ADMIN_CFG, text)
        assert "m:formats" in data_of(ev.out[0][1]), text


async def test_a_shared_format_with_a_thumbnail_comes_as_a_photo(ctx):
    fmt, token = await share(ctx)
    picture = jpeg((320, 180))
    await ctx.db.set_format_thumb(OWNER, fmt["id"], picture)
    ev = await say(ctx, ADMIN_CFG, f"/start fmt_{token}")
    assert ev.out == []
    ((chat, file, kw),) = ctx.client.files
    assert chat == ADMIN_CFG and file.getvalue() == picture and file.name.endswith(".jpg")
    assert "📦 One Piece" in kw["caption"] and "✅ included" in kw["caption"] and kw["parse_mode"] == "html"
    assert data_of(kw["buttons"]) == [f"fs:add:{token}", "x:close"]


async def test_if_the_thumbnail_cannot_be_sent_the_details_still_come(ctx):
    fmt, token = await share(ctx)
    await ctx.db.set_format_thumb(OWNER, fmt["id"], jpeg())
    ctx.client.fail_send_file = True
    ev = await say(ctx, ADMIN_CFG, f"/start fmt_{token}")
    assert len(ev.out) == 1 and "📦 One Piece" in ev.texts[0] and f"fs:add:{token}" in data_of(ev.out[0][1])


async def test_the_page_shows_the_format_as_it_is_now(ctx):
    fmt, token = await share(ctx)
    await ctx.db.update_format(OWNER, fmt["id"], template="Changed - {title}")
    ev = await say(ctx, ADMIN_CFG, f"/start fmt_{token}")
    assert "Changed - {title}" in ev.texts[0]


async def test_the_owner_opening_their_own_link_gets_no_add_button(ctx):
    fmt, token = await share(ctx)
    ev = await say(ctx, OWNER, f"/start fmt_{token}")
    assert "your own format" in ev.texts[0] and data_of(ev.out[0][1]) == [f"fm:v:{fmt['id']}", "x:close"]
    cb = await click(ctx, OWNER, f"fs:add:{token}")  # a button from an old message
    assert cb.alerts == ["That's your own format."] and await ctx.db.count_formats(OWNER) == 1


async def test_strangers_cannot_open_a_shared_format(ctx):
    fmt, token = await share(ctx)
    ev = await say(ctx, STRANGER, f"/start fmt_{token}")
    assert ev.out == []
    assert len([m for m in ctx.client.sent if m[0] == LOG and "Ignored" in m[1]]) == 1  # the owner hears about it, once
    cb = await click(ctx, STRANGER, f"fs:add:{token}")
    assert cb.edits == [] and await ctx.db.count_formats(STRANGER) == 0


async def test_links_that_lead_nowhere_say_so(ctx):
    fmt, token = await share(ctx)
    await ctx.db.delete_format(OWNER, fmt["id"])
    for payload in (token, "unknown", "", "!!!", "x" * 40, "../../etc"):
        ev = await say(ctx, ADMIN_CFG, f"/start fmt_{payload}")
        assert ev.texts == [panels.SHARE_GONE], payload
        assert data_of(ev.out[0][1]) == ["x:close"]
    cb = await click(ctx, ADMIN_CFG, f"fs:add:{token}")
    assert cb.alerts == ["That shared format isn't available anymore."] and cb.edits[-1][0] == panels.SHARE_GONE
    for data in ("fs:add", "fs:add:", "fs:add:!!", "fs:nope:" + token, "fs"):
        cb = await click(ctx, ADMIN_CFG, data)
        assert await ctx.db.count_formats(ADMIN_CFG) == 0, data


async def test_an_unknown_action_on_a_live_link_does_nothing(ctx):
    _fmt, token = await share(ctx)
    cb = await click(ctx, ADMIN_CFG, f"fs:nope:{token}")
    assert cb.answers == [(None, False)] and cb.edits == [] and await ctx.db.count_formats(ADMIN_CFG) == 0


# ---------------------------------------------------------------------------
# Add to my formats
# ---------------------------------------------------------------------------


async def test_adding_a_shared_format_gives_the_user_their_own_copy(ctx):
    fmt, token = await share(ctx, meta={"title": "One Piece"}, keep_audio=["japanese"], keep_subtitle=["english"])
    await ctx.db.set_format_thumb(OWNER, fmt["id"], jpeg())
    cb = await click(ctx, ADMIN_CFG, f"fs:add:{token}")
    (mine,) = await ctx.db.list_formats(ADMIN_CFG)
    assert mine["id"] != fmt["id"] and mine["name"] == "One Piece" and mine["template"] == "One Piece - E{episode}"
    assert mine["meta"]["title"] == "One Piece" and mine["keep_audio"] == ["japanese"] and mine["keep_subtitle"] == ["english"]
    assert mine["has_thumb"] and await ctx.db.get_format_thumb(ADMIN_CFG, mine["id"]) == jpeg()
    assert cb.toasts == ["Added to your formats."]
    text, buttons = cb.edits[-1]
    assert "is now in your manual formats" in text and "/rename" in text and data_of(buttons) == [f"fm:v:{mine['id']}", "x:close"]
    assert mine["kind"] == "manual" and await ctx.db.count_formats(ADMIN_CFG, "auto") == 0

    # the copy is theirs: changing or deleting it leaves the owner's alone, and it shows up in their lists
    await ctx.db.update_format(ADMIN_CFG, mine["id"], template="Mine - {title}")
    assert (await ctx.db.get_format(OWNER, fmt["id"]))["template"] == "One Piece - E{episode}"
    ev = await say(ctx, ADMIN_CFG, "/rename")
    assert f"rn:p:{mine['id']}" in data_of(ev.out[0][1])
    cb = await click(ctx, ADMIN_CFG, f"fm:v:{mine['id']}")  # "Open format"
    assert "🗂 One Piece" in cb.edits[-1][0] and f"inline:share_{mine['id']}" in data_of(cb.edits[-1][1])
    await ctx.db.delete_format(ADMIN_CFG, mine["id"])
    assert await ctx.db.get_format(OWNER, fmt["id"]) is not None


async def test_a_copy_is_renamed_when_the_name_is_taken_and_stops_at_the_limit(ctx):
    _fmt, token = await share(ctx)
    await make_format(ctx, "one piece", uid=ADMIN_CFG)
    await click(ctx, ADMIN_CFG, f"fs:add:{token}")
    assert [f["name"] for f in await ctx.db.list_formats(ADMIN_CFG)] == ["one piece", "One Piece (2)"]

    for i in range(MAX_FORMATS - 2):
        await make_format(ctx, f"F{i}", uid=ADMIN_CFG)
    cb = await click(ctx, ADMIN_CFG, f"fs:add:{token}")
    assert cb.alerts == [f"You already have {MAX_FORMATS} manual formats. Delete one first."] and cb.edits == []
    assert await ctx.db.count_formats(ADMIN_CFG) == MAX_FORMATS


async def test_a_format_deleted_by_its_owner_cannot_be_added_but_copies_stay(ctx):
    fmt, token = await share(ctx)
    await click(ctx, ADMIN_CFG, f"fs:add:{token}")
    await ctx.db.delete_format(OWNER, fmt["id"])
    assert await ctx.db.count_formats(ADMIN_CFG) == 1  # their copy is theirs
    cb = await click(ctx, ADMIN_CFG, f"fs:add:{token}")
    assert cb.alerts == ["That shared format isn't available anymore."] and await ctx.db.count_formats(ADMIN_CFG) == 1


async def test_a_format_that_vanishes_while_it_is_being_added_is_reported(ctx, monkeypatch):
    _fmt, token = await share(ctx)

    async def gone(*_a, **_k):
        return None

    monkeypatch.setattr(ctx.db, "copy_format", gone)
    cb = await click(ctx, ADMIN_CFG, f"fs:add:{token}")
    assert cb.alerts == ["That shared format isn't available anymore."] and cb.edits[-1][0] == panels.SHARE_GONE


async def test_the_shared_link_works_for_many_people_and_many_times(ctx):
    _fmt, token = await share(ctx)
    await click(ctx, ADMIN_CFG, f"fs:add:{token}")
    await click(ctx, ADMIN_CFG, f"fs:add:{token}")
    assert [f["name"] for f in await ctx.db.list_formats(ADMIN_CFG)] == ["One Piece", "One Piece (2)"]
    assert await ctx.db.count_formats(OWNER) == 1


# ---------------------------------------------------------------------------
# auto formats are shared and added as auto formats
# ---------------------------------------------------------------------------


async def test_a_shared_auto_format_is_added_as_an_auto_format(ctx):
    fmt, token = await share(ctx, "Auto One", kind="auto", meta={"title": "One Piece"})
    ev = await say(ctx, ADMIN_CFG, f"/start fmt_{token}")
    assert "🤖 Auto" in ev.texts[0] and "An auto rename format shared with you" in ev.texts[0] and "your auto formats" in ev.texts[0]
    cb = await click(ctx, ADMIN_CFG, f"fs:add:{token}")
    (mine,) = await ctx.db.list_formats(ADMIN_CFG)
    assert mine["kind"] == "auto" and mine["name"] == "Auto One" and mine["meta"]["title"] == "One Piece"
    text, buttons = cb.edits[-1]
    assert "is now in your auto formats" in text and "/start menu" in text and data_of(buttons) == [f"fm:v:{mine['id']}", "x:close"]
    assert (await ctx.db.get_auto_format(ADMIN_CFG))["id"] == mine["id"]  # their first auto format is the one in use
    assert await ctx.db.count_formats(ADMIN_CFG, "manual") == 0 and (await ctx.db.get_format(OWNER, fmt["id"]))["kind"] == "auto"

    title, description, card = panels.share_card(fmt)
    assert "An auto rename format for this bot" in card and title == "🗂 Auto One" and description


async def test_a_shared_format_keeps_its_kind_whoever_opens_it(ctx):
    _fmt, token = await share(ctx, "Auto One", kind="auto")
    ev = await say(ctx, OWNER, f"/start fmt_{token}")  # the owner looking at their own card
    assert "your own format" in ev.texts[0] and "🤖 Auto" in ev.texts[0]
    _plain, plain_token = await share(ctx, "Manual One")
    ev = await say(ctx, ADMIN_CFG, f"/start fmt_{plain_token}")
    assert "A manual rename format shared with you" in ev.texts[0] and "✋ Manual" in ev.texts[0]


async def test_the_limit_of_a_shared_format_is_the_one_of_its_kind(ctx):
    _a, auto_token = await share(ctx, "Auto One", kind="auto")
    _m, manual_token = await share(ctx, "Manual One")
    for i in range(MAX_FORMATS):
        await make_format(ctx, f"F{i}", uid=ADMIN_CFG)  # twelve manual formats
    cb = await click(ctx, ADMIN_CFG, f"fs:add:{manual_token}")
    assert cb.alerts == [f"You already have {MAX_FORMATS} manual formats. Delete one first."]
    cb = await click(ctx, ADMIN_CFG, f"fs:add:{auto_token}")  # the auto formats are counted apart
    assert cb.alerts == [] and await ctx.db.count_formats(ADMIN_CFG, "auto") == 1
    assert await ctx.db.count_formats(ADMIN_CFG, "manual") == MAX_FORMATS


async def test_the_share_card_of_an_auto_format_is_made_by_the_inline_query(ctx):
    fmt = await make_format(ctx, "Auto One", kind="auto")
    query = await ask(ctx, OWNER, f"share_{fmt['id']}")
    (card,) = query.results
    assert "An auto rename format" in card_text(card)
