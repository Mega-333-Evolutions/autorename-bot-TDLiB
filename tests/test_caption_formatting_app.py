"""A caption keeps the formatting it was sent with (bold, italic, links...): nobody has to type HTML.

The caption of the auto format is edited on that format's screen in /formats (the Caption button), so ``autofmt`` is where it is read back."""
from bot.pipeline import make_caption
from bot.tg import html as telegram_html

from .appkit import OWNER, autofmt, click, data_of, edit_auto, ensure_auto, say, say_formatted
from .test_formats_app import make_format

FORMATTED = "[b]Watch[/b] {title} [i]episode {episode}[/i] on [a=https://t.me/Anime_X]our channel[/a]"
STORED = '<b>Watch</b> {title} <i>episode {episode}</i> on <a href="https://t.me/Anime_X">our channel</a>'


async def set_caption(ctx, markup, *, formatted=True):
    """Tap Caption on the auto format's screen and send the caption (``markup`` as ``say_formatted`` takes it, or typed as it is
    with ``formatted=False``).

    Returns the message sent and the text the prompt's message turned into once the caption was taken (empty when it was refused)."""
    auto = await ensure_auto(ctx.db)
    edited = len(ctx.client.edited)
    await click(ctx, OWNER, f"fm:in:{auto['id']}:caption", msg_id=500)
    ev = await (say_formatted if formatted else say)(ctx, OWNER, markup)
    return ev, (ctx.client.edited[-1][1] if len(ctx.client.edited) > edited else "")


# ---------------------------------------------------------------------------
# the Caption button of the auto format, then the caption
# ---------------------------------------------------------------------------


async def test_the_caption_prompt_keeps_the_formatting(ctx):
    auto = await ensure_auto(ctx.db)
    cb = await click(ctx, OWNER, f"fm:in:{auto['id']}:caption", msg_id=500)
    assert "No HTML needed" in cb.edits[-1][0]
    ev = await say_formatted(ctx, OWNER, FORMATTED, mid=60)
    assert ev.deleted and OWNER not in ctx.app.pending
    assert (await autofmt(ctx))["caption"] == STORED
    (chat, text, kw), = ctx.client.edited
    assert kw["message"] == 500 and "Caption template saved" in text and "Unknown" not in text
    assert STORED in text and "&lt;b&gt;" not in text  # the format's screen shows the caption formatted
    assert f"fm:in:{auto['id']}:caption" in data_of(kw["buttons"])


async def test_the_format_screen_shows_the_caption_with_its_formatting(ctx):
    auto = await edit_auto(ctx, caption=STORED)
    text = (await click(ctx, OWNER, f"fm:v:{auto['id']}")).edits[-1][0]
    assert STORED in text  # shown as the person wrote it, not as tags to read
    assert "&lt;b&gt;" not in text and "&lt;a href" not in text


async def test_a_caption_may_have_several_lines(ctx):
    await set_caption(ctx, "[b]{filename}[/b]\nsecond [i]line[/i] {size}")
    assert (await autofmt(ctx))["caption"] == "<b>{filename}</b>\nsecond <i>line</i> {size}"


async def test_emoji_before_the_formatted_words_do_not_shift_it(ctx):
    await set_caption(ctx, "🔥🔥 [b]{filename}[/b] 👨‍👩‍👧 [i]{size}[/i]")
    assert (await autofmt(ctx))["caption"] == "🔥🔥 <b>{filename}</b> 👨‍👩‍👧 <i>{size}</i>"


async def test_html_typed_by_hand_still_works_when_nothing_is_formatted(ctx):
    await set_caption(ctx, "<i>{filename}</i> · <a href=\"https://t.me/x\">join</a>", formatted=False)
    assert (await autofmt(ctx))["caption"] == '<i>{filename}</i> · <a href="https://t.me/x">join</a>'


async def test_what_telegram_finds_by_itself_is_not_formatting(ctx):
    """A #hashtag or an @mention is an entity too, but nothing was formatted: typed tags are still taken as typed."""
    await set_caption(ctx, "<i>{filename}</i> [tag]#anime[/tag] [at]@channel[/at]")
    assert (await autofmt(ctx))["caption"] == "<i>{filename}</i> #anime @channel"


async def test_once_words_are_formatted_the_message_is_taken_as_written(ctx):
    await set_caption(ctx, "<b>{filename}</b> [i]x[/i]")
    assert (await autofmt(ctx))["caption"] == "&lt;b&gt;{filename}&lt;/b&gt; <i>x</i>"  # the tags the person typed are just text


async def test_a_spoiler_is_not_kept_so_its_words_stay_plain(ctx):
    await set_caption(ctx, "[spoiler]secret[/spoiler] {title}")
    assert (await autofmt(ctx))["caption"] == "secret {title}"


async def test_reset_still_resets_when_it_was_made_bold(ctx):
    await edit_auto(ctx, caption="custom {filename}")
    _ev, text = await set_caption(ctx, "[b]reset[/b]")
    assert (await autofmt(ctx))["caption"] == "<b>{filename}</b>" and "Caption template saved" in text


async def test_a_variable_cut_by_the_formatting_is_still_a_whole_variable(ctx):
    """Double-tapping {filename} selects the word without its braces: the bold covers the whole variable all the same."""
    _ev, text = await set_caption(ctx, "{[b]filename[/b]} · {siz[i]e} left[/i]")
    assert (await autofmt(ctx))["caption"] == "<b>{filename}</b> · <i>{size} left</i>"
    assert "Unknown" not in text


async def test_unknown_variables_are_still_reported(ctx):
    _ev, text = await set_caption(ctx, "[b]{filename}[/b] {nonsense}")
    assert "Unknown variables" in text and "{nonsense}" in text
    assert (await autofmt(ctx))["caption"] == "<b>{filename}</b> {nonsense}"


async def test_a_caption_that_is_too_long_with_its_link_addresses_is_refused(ctx):
    ev, text = await set_caption(ctx, f"[a=https://example.com/{'a' * 800}]link[/a] {{filename}}")
    assert "too long" in ev.texts[0] and "link addresses" in ev.texts[0] and text == ""
    assert (await autofmt(ctx))["caption"] == "<b>{filename}</b>"  # nothing was saved


async def test_the_caption_prompt_still_takes_typed_html_and_reset(ctx):
    auto = await ensure_auto(ctx.db)
    await click(ctx, OWNER, f"fm:in:{auto['id']}:caption")
    await say(ctx, OWNER, "<u>{filename}</u> {size}")
    assert (await autofmt(ctx))["caption"] == "<u>{filename}</u> {size}"
    await click(ctx, OWNER, f"fm:in:{auto['id']}:caption")
    await say_formatted(ctx, OWNER, "[b]reset[/b]")
    assert (await autofmt(ctx))["caption"] == "<b>{filename}</b>"


async def test_formatting_sent_to_another_prompt_is_not_turned_into_html(ctx):
    """Only captions are rich text: a template or a title is plain text, whatever it was formatted with."""
    auto = await ensure_auto(ctx.db)
    await click(ctx, OWNER, f"fm:in:{auto['id']}:template")
    await say_formatted(ctx, OWNER, "[b]{title}[/b] [i]{quality}[/i]")
    assert (await autofmt(ctx))["template"] == "{title} {quality}"


async def test_a_prompt_that_is_refused_stays_open_for_another_try(ctx):
    auto = await ensure_auto(ctx.db)
    await click(ctx, OWNER, f"fm:in:{auto['id']}:caption")
    ev = await say_formatted(ctx, OWNER, f"[a=https://example.com/{'a' * 800}]link[/a]")
    assert "too long" in ev.texts[0] and "Try again" in ev.texts[0] and OWNER in ctx.app.pending
    await say_formatted(ctx, OWNER, "[b]{filename}[/b] ok")
    assert OWNER not in ctx.app.pending and (await autofmt(ctx))["caption"] == "<b>{filename}</b> ok"


# ---------------------------------------------------------------------------
# a saved format's own caption
# ---------------------------------------------------------------------------


async def test_a_formats_caption_prompt_keeps_the_formatting(ctx):
    await edit_auto(ctx, caption="<u>{filename}</u>")  # the auto format in use has a caption of its own
    fmt = await make_format(ctx)
    i = fmt["id"]
    cb = await click(ctx, OWNER, f"fm:in:{i}:caption", msg_id=710)
    assert "No HTML needed" in cb.edits[-1][0]
    ev = await say_formatted(ctx, OWNER, FORMATTED)
    assert ev.deleted and OWNER not in ctx.app.pending
    assert (await ctx.db.get_format(OWNER, i))["caption"] == STORED
    assert (await autofmt(ctx))["caption"] == "<u>{filename}</u>"  # the auto format in use was never touched
    chat, text, kw = ctx.client.edited[-1]
    assert kw["message"] == 710 and "Caption template saved" in text
    assert "<b>Caption</b> ▸ " + STORED in text and "· default" not in text  # the format's screen shows it formatted
    assert f"fm:cr:{i}" in data_of(kw["buttons"])


async def test_a_formats_caption_made_bold_exactly_like_the_default_counts_as_the_default(ctx):
    fmt = await make_format(ctx, caption="<u>{filename}</u>")
    await click(ctx, OWNER, f"fm:in:{fmt['id']}:caption")
    await say_formatted(ctx, OWNER, "[b]{filename}[/b]")
    chat, text, kw = ctx.client.edited[-1]
    assert (await ctx.db.get_format(OWNER, fmt["id"]))["caption"] == "<b>{filename}</b>"
    assert "· default" in text and f"fm:cr:{fmt['id']}" not in data_of(kw["buttons"])


async def test_a_format_caption_reset_works_in_bold_and_other_fields_stay_plain(ctx):
    fmt = await make_format(ctx, caption="<u>{filename}</u>")
    i = fmt["id"]
    await click(ctx, OWNER, f"fm:in:{i}:caption")
    await say_formatted(ctx, OWNER, "[b]reset[/b]")
    assert (await ctx.db.get_format(OWNER, i))["caption"] == "<b>{filename}</b>"
    await click(ctx, OWNER, f"fm:in:{i}:title")
    await say_formatted(ctx, OWNER, "[b]One Piece[/b]")
    assert (await ctx.db.get_format(OWNER, i))["meta"]["title"] == "One Piece"  # metadata is not rich text


# ---------------------------------------------------------------------------
# a link in the caption must not put a link preview on the settings screens
# ---------------------------------------------------------------------------


async def test_a_link_in_a_caption_gets_no_link_preview_on_any_screen_that_shows_it(ctx):
    auto = await edit_auto(ctx, caption=STORED)
    cb = await click(ctx, OWNER, f"fm:v:{auto['id']}")
    assert STORED in cb.edits[-1][0] and cb.edit_kwargs[-1]["link_preview"] is False

    await click(ctx, OWNER, f"fm:in:{auto['id']}:caption", msg_id=500)
    await say_formatted(ctx, OWNER, FORMATTED)
    assert STORED in ctx.client.edited[-1][1] and ctx.client.edited[-1][2]["link_preview"] is False

    fmt = await make_format(ctx, caption=STORED)
    cb = await click(ctx, OWNER, f"fm:v:{fmt['id']}")
    assert STORED in cb.edits[-1][0] and cb.edit_kwargs[-1]["link_preview"] is False
    await click(ctx, OWNER, f"fm:in:{fmt['id']}:caption", msg_id=710)
    await say_formatted(ctx, OWNER, FORMATTED)
    assert STORED in ctx.client.edited[-1][1] and ctx.client.edited[-1][2]["link_preview"] is False
    cb = await click(ctx, OWNER, f"fm:sh:{fmt['id']}")
    assert cb.edit_kwargs and all(kw.get("link_preview") is False for kw in cb.edit_kwargs)


# ---------------------------------------------------------------------------
# what the renamed file gets
# ---------------------------------------------------------------------------


async def test_the_formatting_follows_the_values_that_replace_the_variables(ctx):
    await set_caption(ctx, "[b]{filename}[/b] · [i]{size}[/i] · [a=https://t.me/x]{original}[/a]")
    caption = make_caption((await autofmt(ctx))["caption"], new_name="One Piece - E5.mkv", orig_name="1147 - One Piece [Dual].mkv",
                           ext=".mkv", size=1536, duration=0)
    text, entities = telegram_html.parse(caption)  # what Telegram makes of it when it is sent with parse_mode="html"
    assert text == "One Piece - E5 · 1.50 KB · 1147 - One Piece [Dual].mkv"
    shown = {type(e).__name__: (text[e.offset:e.offset + e.length], getattr(e, "url", None)) for e in entities}
    assert shown == {
        "MessageEntityBold": ("One Piece - E5", None),
        "MessageEntityItalic": ("1.50 KB", None),
        "MessageEntityTextUrl": ("1147 - One Piece [Dual].mkv", "https://t.me/x"),
    }


async def test_a_name_with_html_characters_cannot_break_the_formatting(ctx):
    await set_caption(ctx, "[b]{filename}[/b] [i]{original}[/i]")
    caption = make_caption((await autofmt(ctx))["caption"], new_name="Tom & <Jerry>.mkv", orig_name="a<b>&c.mkv", ext=".mkv", size=1, duration=0)
    text, entities = telegram_html.parse(caption)
    assert text == "Tom & <Jerry> a<b>&c.mkv"
    assert [(type(e).__name__, e.offset, e.length) for e in entities] == [("MessageEntityBold", 0, 13), ("MessageEntityItalic", 14, 10)]
