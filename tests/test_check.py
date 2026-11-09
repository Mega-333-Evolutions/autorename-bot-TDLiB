"""/check: what the bot can read from a file's name and caption."""
from .appkit import OWNER, edit_auto, say
from .tgfakes import FakeMessage, document_message

NAME = "[S01 E02] Dragon Ball Super Beerus [1080p].mkv"


def detail(text, block, label):
    """The value shown for ``label`` inside the ``block`` ("Fɪʟᴇɴᴀᴍᴇ" / "Cᴀᴘᴛɪᴏɴ Mᴏᴅᴇ") of a /check reply."""
    section = text.split(f"<b>{block}:</b>\n", 1)[1].split("\n\n", 1)[0]
    line = next(ln for ln in section.splitlines() if ln.startswith(f"➦ {label} ⇛"))
    return line.split("<code>", 1)[1].split("</code>", 1)[0]


async def test_check_lists_what_it_reads_from_the_name_and_the_caption(ctx):
    reply = document_message(NAME, caption="Dragon Ball Super Beerus - S01E02 [720p]")
    ev = await say(ctx, OWNER, "/check", reply=reply)
    text = ev.texts[0]
    assert text.startswith("<b>Yᴏᴜ Cᴀɴ Cʜᴀɴɢᴇ Mᴏᴅᴇ Bʏ</b> /mode")
    assert "<b>Rᴇɴᴀᴍɪɴɢ Mᴏᴅᴇ:</b> <code>FILENAME</code>" in text
    for label, expected in (("Title", "Dragon Ball Super Beerus"), ("Season", "01"), ("Episode", "02"), ("Quality", "1080p")):
        assert detail(text, "Fɪʟᴇɴᴀᴍᴇ", label) == expected
    for label, expected in (("Title", "Dragon Ball Super Beerus"), ("Season", "01"), ("Episode", "02"), ("Quality", "720p")):
        assert detail(text, "Cᴀᴘᴛɪᴏɴ Mᴏᴅᴇ", label) == expected
    assert ev.out[0][2]["parse_mode"] == "html"


async def test_there_is_no_chapter_field(ctx):
    ev = await say(ctx, OWNER, "/check", reply=document_message(NAME))
    assert "Chapter" not in ev.texts[0]


async def test_what_is_not_found_shows_as_none(ctx):
    ev = await say(ctx, OWNER, "/check", reply=document_message("holiday video.mp4"))
    text = ev.texts[0]
    assert detail(text, "Fɪʟᴇɴᴀᴍᴇ", "Season") == "None" and detail(text, "Fɪʟᴇɴᴀᴍᴇ", "Episode") == "None"
    assert all(detail(text, "Cᴀᴘᴛɪᴏɴ Mᴏᴅᴇ", f) == "None" for f in ("Title", "Season", "Episode", "Quality"))
    assert "Year" not in text and "Codec" not in text  # only listed when there is one


async def test_year_and_codec_show_up_when_the_file_has_them(ctx):
    ev = await say(ctx, OWNER, "/check", reply=document_message("Some Movie (2023) 1080p x265.mkv"))
    assert detail(ev.texts[0], "Fɪʟᴇɴᴀᴍᴇ", "Year") == "2023" and detail(ev.texts[0], "Fɪʟᴇɴᴀᴍᴇ", "Codec") != "None"


async def test_the_current_mode_is_shown_and_decides_the_new_name(ctx):
    await edit_auto(ctx, template="[S{season}E{episode}] {title} [{quality}]")
    reply = document_message("random.mkv", caption="One Piece - S01E1147 [720p]")
    ev = await say(ctx, OWNER, "/check", reply=reply)
    assert "<code>FILENAME</code>" in ev.texts[0]
    assert "<b>Nᴇᴡ Nᴀᴍᴇ:</b>\n<code>random.mkv</code>" in ev.texts[0]  # nothing to read in the name, so it is left as it is

    await say(ctx, OWNER, "/mode caption")
    ev = await say(ctx, OWNER, "/check", reply=reply)
    assert "<b>Rᴇɴᴀᴍɪɴɢ Mᴏᴅᴇ:</b> <code>CAPTION</code>" in ev.texts[0]
    assert "<code>[S01E1147] One Piece [720p].mkv</code>" in ev.texts[0]
    assert detail(ev.texts[0], "Fɪʟᴇɴᴀᴍᴇ", "Episode") == "None" and detail(ev.texts[0], "Cᴀᴘᴛɪᴏɴ Mᴏᴅᴇ", "Episode") == "1147"


async def test_the_new_name_follows_the_users_template_and_remove_words(ctx):
    await edit_auto(ctx, template="{title} - E{episode}")
    await say(ctx, OWNER, "/removewords @Anime_X_Universe")
    reply = document_message("1147 - One Piece [Dual] 720p @Anime_X_Universe.mkv")
    ev = await say(ctx, OWNER, "/check", reply=reply)
    assert "<code>One Piece - E1147.mkv</code>" in ev.texts[0]
    assert detail(ev.texts[0], "Fɪʟᴇɴᴀᴍᴇ", "Episode") == "1147" and detail(ev.texts[0], "Fɪʟᴇɴᴀᴍᴇ", "Title") == "One Piece"


async def test_values_are_escaped(ctx):
    ev = await say(ctx, OWNER, "/check", reply=document_message("Fate <b>Zero</b> & Co - 03.mkv"))
    assert "<b>Zero</b>" not in ev.texts[0].split("Fɪʟᴇɴᴀᴍᴇ")[1] and "&lt;b&gt;" in ev.texts[0]


async def test_check_without_a_file_explains_itself(ctx):
    for kwargs in ({}, {"reply": FakeMessage("just text")}, {"reply": FakeMessage("", photo=True)}):
        ev = await say(ctx, OWNER, "/check", **kwargs)
        assert "Reply to a file with /check" in ev.texts[0] and "Fɪʟᴇɴᴀᴍᴇ" not in ev.texts[0]


async def test_check_is_in_the_command_menu(ctx):
    from bot.app import COMMANDS

    assert "check" in dict(COMMANDS)


async def test_the_new_name_is_the_one_the_auto_format_in_use_would_give(ctx):
    await ctx.db.create_format(OWNER, "One", "One - E{episode}", kind="auto")
    two = await ctx.db.create_format(OWNER, "Two", "Two - {title} {3episode}", kind="auto")
    await ctx.db.create_format(OWNER, "Manual", "Manual - {title}")  # a manual format is for /rename, not for the automatic renames
    reply = document_message("1147 - One Piece [Dual] 720p.mkv")
    assert "<code>One - E1147.mkv</code>" in (await say(ctx, OWNER, "/check", reply=reply)).texts[0]
    await ctx.db.select_auto_format(OWNER, two["id"])
    assert "<code>Two - One Piece 1147.mkv</code>" in (await say(ctx, OWNER, "/check", reply=reply)).texts[0]


async def test_without_an_auto_format_the_new_name_follows_the_built_in_template(ctx):
    ev = await say(ctx, OWNER, "/check", reply=document_message("[SubsPlease] Tokyo Revengers - 05 (1080p) [ABCD1234].mkv"))
    assert "<code>[S01E05] Tokyo Revengers [1080p].mkv</code>" in ev.texts[0]
