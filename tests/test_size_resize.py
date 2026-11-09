"""/size and /resize: reply to a picture; the first tells its dimensions, the second sends it back at exactly the size given."""
import asyncio
import io

import pytest
from PIL import Image

from bot import app as appmod
from bot import media

from .appkit import OWNER, STRANGER, click, jpeg, say
from .tgfakes import FakeEvent, FakeMessage, dispatch, document_message, make_file

FULL_HD = (1920, 1080)
PROCESSING = "⏳ Processing…"
HINT = "Give Some Size To Resize, Like <code>/resize 720 1080</code>"


def png(size=FULL_HD, mode="RGB", color=(10, 120, 200)):
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, "PNG")
    return buf.getvalue()


def photo(ctx, mid=50, data=None):
    """A photo that somebody sent: Telegram gives it whole."""
    ctx.client.media[mid] = data if data is not None else jpeg(FULL_HD)
    return FakeMessage("", photo=True, mid=mid)


def video(ctx, mid=60, thumb=None):
    """A video: all that can be read from it is its preview."""
    ctx.client.thumb_data[mid] = thumb if thumb is not None else jpeg((320, 180))
    return document_message("Movie.mkv", 90 * 1024 * 1024, mid=mid, video=True)


def image_file(ctx, mid=61, data=None, size=300 * 1024, name="poster.png", mime="image/png"):
    """A picture sent as a file: the file is the picture, its preview is only a small copy."""
    ctx.client.media[mid] = data if data is not None else png(FULL_HD)
    ctx.client.thumb_data[mid] = jpeg((320, 180))
    return document_message(name, size, mid=mid, video=False, mime=mime)


def sent_files(ev):
    return [kw for _text, _buttons, kw in ev.out if kw.get("file") is not None]


def result_image(ev):
    (kw,) = sent_files(ev)
    return Image.open(io.BytesIO(kw["file"].getvalue()))


async def settle(ctx):
    """Let the background tasks (the hint that removes itself) finish."""
    await asyncio.gather(*list(ctx.app._tasks))


@pytest.fixture
def quick_hint(monkeypatch):
    monkeypatch.setattr(appmod, "HINT_SECONDS", 0)


# ---------------------------------------------------------------------------
# the commands
# ---------------------------------------------------------------------------


def test_both_commands_are_in_the_menu_and_the_old_resize_wording_is_gone():
    menu = dict(appmod.COMMANDS)
    assert "size" in menu and "resize" in menu
    assert "reply" in menu["size"].lower() and "reply" in menu["resize"].lower() and "thumbnails" not in menu["resize"]


async def test_commands_are_for_allowed_users_only(ctx):
    msg = photo(ctx)
    ev = await say(ctx, STRANGER, "/size", reply=msg)
    assert ev.out == []
    ev = await say(ctx, STRANGER, "/resize 10 10", reply=msg)
    assert ev.out == [] and ctx.client.downloads == []


async def test_the_commands_work_with_the_bot_name_added(ctx):
    ev = await say(ctx, OWNER, "/size@RenameBot", reply=photo(ctx))
    assert ctx.client.edited[-1][1].endswith("<code>1920 x 1080</code>")
    ev = await say(ctx, OWNER, "/resize@RenameBot 64 48", reply=photo(ctx, mid=51))
    assert result_image(ev).size == (64, 48)


# ---------------------------------------------------------------------------
# /size
# ---------------------------------------------------------------------------


async def test_size_of_a_photo_is_the_size_of_the_whole_photo(ctx):
    ev = await say(ctx, OWNER, "/size", reply=photo(ctx))
    assert ev.texts == [PROCESSING]  # one message, which is then edited into the answer
    status, text, kw = ctx.client.edited[-1]
    assert status.id == 1001 and kw["parse_mode"] == "html"
    assert text == "Dimension Of This Image Is\n<code>1920 x 1080</code>"
    assert ctx.client.downloads == [(50, bytes, None)] and ctx.client.deleted == []


async def test_size_of_a_video_gif_or_sticker_with_no_picture_inside_is_the_size_of_its_preview(ctx):
    await say(ctx, OWNER, "/size", reply=video(ctx))
    assert ctx.client.edited[-1][1] == "Dimension Of This Image Is\n<code>320 x 180</code>"
    assert ctx.client.downloads == [(60, bytes, -1)]  # the largest preview Telegram keeps with it

    ctx.client.thumb_data[63] = jpeg((256, 144))
    gif = FakeMessage("", file=make_file("a.mp4", 100_000, "video/mp4"), gif=True, mid=63)
    await say(ctx, OWNER, "/size", reply=gif)
    assert ctx.client.edited[-1][1].endswith("<code>256 x 144</code>") and ctx.client.downloads[-1] == (63, bytes, -1)


async def test_size_of_a_picture_sent_as_a_file_is_the_size_of_the_picture_not_of_its_preview(ctx):
    await say(ctx, OWNER, "/size", reply=image_file(ctx))
    assert ctx.client.edited[-1][1].endswith("<code>1920 x 1080</code>")  # not the 320 x 180 preview
    assert ctx.client.downloads == [(61, bytes, None)]


async def test_size_of_a_still_sticker_is_read_from_the_sticker(ctx):
    ctx.client.media[62] = png((512, 460), "RGBA")
    sticker = FakeMessage("", file=make_file("sticker.webp", 30_000, "image/webp"), sticker=True, mid=62)
    await say(ctx, OWNER, "/size", reply=sticker)
    assert ctx.client.edited[-1][1].endswith("<code>512 x 460</code>")


async def test_size_counts_a_picture_that_is_stored_sideways_as_turned(ctx):
    img = Image.new("RGB", (200, 100), (200, 0, 0))
    exif = img.getexif()
    exif[274] = 6  # shown turned a quarter
    buf = io.BytesIO()
    img.save(buf, "PNG", exif=exif)
    await say(ctx, OWNER, "/size", reply=image_file(ctx, data=buf.getvalue()))
    assert ctx.client.edited[-1][1].endswith("<code>100 x 200</code>")


async def test_size_without_media_asks_for_it(ctx):
    ev = await say(ctx, OWNER, "/size")
    assert ev.texts == ["Reply to a picture or other media with /size to get its dimensions."]
    plain_text = FakeMessage("just words", mid=70)
    ev = await say(ctx, OWNER, "/size", reply=plain_text)
    assert len(ev.out) == 1 and "Reply to a picture" in ev.texts[0]
    assert ctx.client.downloads == [] and ctx.client.edited == []


async def test_size_says_so_when_there_is_no_picture_to_read(ctx):
    audio = document_message("Song.mp3", 4 * 1024 * 1024, mid=71, video=False, mime="audio/mpeg")  # no preview at all
    await say(ctx, OWNER, "/size", reply=audio)
    assert ctx.client.edited[-1][1] == "❌ I couldn't get a picture from that."

    location = FakeMessage("", mid=72)
    location.media = object()  # a map, a contact, a poll ...: media, but nothing that is a picture
    await say(ctx, OWNER, "/size", reply=location)
    assert ctx.client.edited[-1][1] == "❌ There's no picture in that." and len(ctx.client.downloads) == 1


async def test_size_says_so_when_the_picture_cannot_be_read(ctx):
    await say(ctx, OWNER, "/size", reply=photo(ctx, data=b"definitely not an image"))
    assert ctx.client.edited[-1][1] == "❌ I couldn't read that image."


async def test_size_will_not_download_a_huge_picture(ctx):
    big = image_file(ctx, size=2 * appmod.RESIZE_MAX_BYTES)
    await say(ctx, OWNER, "/size", reply=big)
    assert ctx.client.edited[-1][1] == "❌ That picture is 40.00 MB; I can read pictures up to 20.00 MB." and ctx.client.downloads == []
    ctx.client.edited.clear()
    await say(ctx, OWNER, "/size", reply=image_file(ctx, mid=62, size=appmod.RESIZE_MAX_BYTES))  # right at the limit is read
    assert ctx.client.edited[-1][1].endswith("<code>1920 x 1080</code>")


# ---------------------------------------------------------------------------
# /resize: asking for it right
# ---------------------------------------------------------------------------


async def test_resize_without_media_asks_for_it_before_anything_else(ctx):
    ev = await say(ctx, OWNER, "/resize")
    assert ev.texts == ["Reply to a picture or other media with /resize and a size, like <code>/resize 720 1080</code>."]
    ev = await say(ctx, OWNER, "/resize 720 1080")
    assert "Reply to a picture" in ev.texts[0] and len(ev.out) == 1
    ev = await say(ctx, OWNER, "/resize 720 1080", reply=FakeMessage("words", mid=70))
    assert "Reply to a picture" in ev.texts[0] and len(ev.out) == 1
    assert ctx.client.downloads == [] and ctx.app._tasks == set() and not ctx.app.modes  # and nothing is waiting for a picture


async def test_resize_without_a_size_gives_the_hint_and_removes_it_after_a_few_seconds(ctx, quick_hint):
    ev = await say(ctx, OWNER, "/resize", reply=photo(ctx))
    assert ev.texts == [HINT] and ev.out[0][1] is None  # no size menu, no buttons: just the hint
    await settle(ctx)
    assert ctx.client.deleted == [(OWNER, [1001])]  # the hint, and only the hint, is removed
    assert ctx.client.downloads == []  # nothing was downloaded for it


async def test_the_hint_stays_for_five_seconds(ctx):
    assert appmod.HINT_SECONDS == 5
    ev = await say(ctx, OWNER, "/resize", reply=photo(ctx))
    assert ev.texts == [HINT] and ctx.client.deleted == []  # it is still there
    (waiting,) = ctx.app._tasks  # a task is waiting to remove it
    assert not waiting.done()
    waiting.cancel()
    await asyncio.gather(waiting, return_exceptions=True)
    assert ctx.client.deleted == []


@pytest.mark.parametrize("args", [
    "720", "720 1080 1", "a b", "720 b", "720x1080", "720,1080", "-5 10", "7.5 10", "720 1080 px", "٧٢٠ ١٠٨٠", "1_0 2_0",
])
async def test_resize_wants_exactly_two_numbers_separated_by_a_space(ctx, quick_hint, args):
    ev = await say(ctx, OWNER, f"/resize {args}", reply=photo(ctx))
    assert ev.texts == [HINT], args
    await settle(ctx)
    assert ctx.client.downloads == [] and sent_files(ev) == []


@pytest.mark.parametrize("args", ["0 100", "100 0", "4097 100", "100 4097", "99999 99999", "000 5"])
async def test_resize_keeps_to_what_it_can_make(ctx, args):
    ev = await say(ctx, OWNER, f"/resize {args}", reply=photo(ctx))
    assert ev.texts == [f"❌ Each side can be 1 to {appmod.RESIZE_MAX_SIDE} pixels, like <code>/resize 720 1080</code>."]
    assert ctx.client.downloads == [] and ctx.app._tasks == set()  # this one is an answer to read, it stays


# ---------------------------------------------------------------------------
# /resize: the result
# ---------------------------------------------------------------------------


async def test_resize_sends_the_picture_back_at_exactly_that_size_and_removes_the_status(ctx):
    ev = await say(ctx, OWNER, "/resize 720 1080", reply=photo(ctx))
    assert ev.texts[0] == PROCESSING and len(ev.out) == 2
    (kw,) = sent_files(ev)
    img = result_image(ev)
    assert img.format == "PNG" and img.size == (720, 1080)  # the shape of the picture is not kept
    assert kw["file"].name == "resized.png" and kw["force_document"] is False
    assert ctx.client.deleted == [(OWNER, [1001])]  # the "Processing" message is gone; the result stays
    assert ctx.client.edited == [] and ctx.client.files == []  # it was a reply to the command, not a separate send
    assert ctx.client.downloads == [(50, bytes, None)]


@pytest.mark.parametrize("wanted", [(100, 100), (3, 900), (1, 1), (1920, 1080), (4096, 1), (1, 4096)])
async def test_resize_makes_exactly_the_size_asked_for_whatever_the_picture_looked_like(ctx, wanted):
    ev = await say(ctx, OWNER, "/resize {} {}".format(*wanted), reply=photo(ctx, data=png((640, 480))))
    assert result_image(ev).size == wanted


async def test_extra_spaces_between_the_numbers_do_not_matter(ctx):
    ev = await say(ctx, OWNER, "/resize   64    48  ", reply=photo(ctx))
    assert result_image(ev).size == (64, 48)


async def test_resize_stretches_the_picture_it_does_not_crop_or_pad_it(ctx):
    left_red_right_blue = Image.new("RGB", (200, 100), (0, 0, 255))
    left_red_right_blue.paste((255, 0, 0), (0, 0, 100, 100))
    buf = io.BytesIO()
    left_red_right_blue.save(buf, "PNG")
    ev = await say(ctx, OWNER, "/resize 40 400", reply=photo(ctx, data=buf.getvalue()))
    img = result_image(ev).convert("RGB")
    assert img.getpixel((2, 5)) == (255, 0, 0) and img.getpixel((37, 5)) == (0, 0, 255)
    assert img.getpixel((2, 395)) == (255, 0, 0) and img.getpixel((37, 395)) == (0, 0, 255)  # still the whole picture, top to bottom


async def test_a_picture_sent_as_a_file_is_resized_whole_not_its_preview(ctx):
    ev = await say(ctx, OWNER, "/resize 960 540", reply=image_file(ctx))
    assert result_image(ev).size == (960, 540) and ctx.client.downloads == [(61, bytes, None)]


async def test_a_video_gives_its_preview_resized(ctx):
    ev = await say(ctx, OWNER, "/resize 640 360", reply=video(ctx))
    assert result_image(ev).size == (640, 360) and ctx.client.downloads == [(60, bytes, -1)]


async def test_transparency_is_kept(ctx):
    ev = await say(ctx, OWNER, "/resize 50 20", reply=image_file(ctx, data=png((200, 200), "RGBA", (0, 0, 0, 0))))
    img = result_image(ev)
    assert img.mode == "RGBA" and img.getpixel((10, 10))[3] == 0


async def test_resize_turns_a_picture_stored_sideways_upright_first(ctx):
    img = Image.new("RGB", (200, 100), (0, 0, 255))
    img.paste((255, 0, 0), (0, 0, 100, 100))  # the left half is red
    exif = img.getexif()
    exif[274] = 6  # shown turned a quarter: the red half ends up on top
    buf = io.BytesIO()
    img.save(buf, "PNG", exif=exif)
    ev = await say(ctx, OWNER, "/resize 100 200", reply=image_file(ctx, data=buf.getvalue()))
    out = result_image(ev).convert("RGB")
    assert out.size == (100, 200)
    assert out.getpixel((50, 10)) != out.getpixel((50, 190))  # top and bottom differ (left and right would not)
    assert out.getpixel((10, 100)) == out.getpixel((90, 100))


@pytest.mark.parametrize("mode,saved_as", [("P", "PNG"), ("1", "PNG"), ("LA", "PNG"), ("L", "PNG"), ("CMYK", "JPEG")])
async def test_every_kind_of_picture_is_taken(ctx, mode, saved_as):
    buf = io.BytesIO()
    Image.new(mode, (30, 20)).save(buf, saved_as)
    ev = await say(ctx, OWNER, "/resize 15 10", reply=image_file(ctx, data=buf.getvalue()))
    assert result_image(ev).size == (15, 10), mode


# ---------------------------------------------------------------------------
# /resize: as a photo or as a file
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("size,as_file", [
    ((720, 1080), False), ((1280, 1280), False), ((1281, 100), True), ((100, 1500), True), ((4096, 4096), True),
    ((1000, 50), False), ((1000, 49), True), ((1000, 40), True), ((30, 1000), True), ((1, 1), False),
])
async def test_it_comes_as_a_photo_unless_telegram_would_change_it(ctx, size, as_file):
    assert appmod.fits_as_photo(*size, 1000) is (not as_file)
    if max(size) > 1500 or min(size) < 4:  # the rule is checked for all of them; the real thing for the cheap ones
        return
    ev = await say(ctx, OWNER, "/resize {} {}".format(*size), reply=photo(ctx, data=png((64, 48))))
    assert sent_files(ev)[0]["force_document"] is as_file


async def test_a_result_too_big_for_a_photo_comes_as_a_file(ctx, monkeypatch):
    monkeypatch.setattr(appmod, "PHOTO_MAX_BYTES", 50)  # any PNG is bigger than that
    ev = await say(ctx, OWNER, "/resize 60 40", reply=photo(ctx))
    assert sent_files(ev)[0]["force_document"] is True


def test_the_photo_rule():
    assert appmod.fits_as_photo(1280, 1280, 10) and appmod.fits_as_photo(1280, 64, 10)  # 20 times as long as high is the most
    assert not appmod.fits_as_photo(1280, 63, 10) and not appmod.fits_as_photo(63, 1280, 10)
    assert not appmod.fits_as_photo(1281, 1281, 10) and not appmod.fits_as_photo(100, 100, 10 * 1024 * 1024 + 1)
    assert appmod.fits_as_photo(100, 100, 10 * 1024 * 1024)


# ---------------------------------------------------------------------------
# /resize: when it can't
# ---------------------------------------------------------------------------


async def test_resize_says_so_when_there_is_no_picture_to_read(ctx):
    audio = document_message("Song.mp3", 4 * 1024 * 1024, mid=71, video=False, mime="audio/mpeg")
    ev = await say(ctx, OWNER, "/resize 10 10", reply=audio)
    assert ctx.client.edited[-1][1] == "❌ I couldn't get a picture from that." and sent_files(ev) == []
    assert ctx.client.deleted == []  # the message that says so stays


async def test_resize_says_so_when_the_picture_cannot_be_read(ctx):
    ev = await say(ctx, OWNER, "/resize 10 10", reply=photo(ctx, data=b"definitely not an image"))
    assert ctx.client.edited[-1][1] == "❌ I couldn't read that image." and sent_files(ev) == []


async def test_resize_will_not_download_a_huge_picture(ctx):
    ev = await say(ctx, OWNER, "/resize 10 10", reply=image_file(ctx, size=2 * appmod.RESIZE_MAX_BYTES))
    assert "40.00 MB" in ctx.client.edited[-1][1] and ctx.client.downloads == [] and sent_files(ev) == []


async def test_resize_will_not_decode_a_picture_with_too_many_pixels(ctx, monkeypatch):
    monkeypatch.setattr(media, "RESIZE_MAX_PIXELS", 100 * 100)
    ev = await say(ctx, OWNER, "/resize 10 10", reply=photo(ctx, data=png((101, 100))))
    assert ctx.client.edited[-1][1] == "❌ That picture has too many pixels for me to resize." and sent_files(ev) == []
    ev = await say(ctx, OWNER, "/resize 10 10", reply=photo(ctx, mid=51, data=png((100, 100))))
    assert result_image(ev).size == (10, 10)  # right at the limit is fine


async def test_resize_says_so_when_telegram_refuses_the_result(ctx):
    class Refusing(FakeEvent):
        async def reply(self, text=None, **kw):
            if kw.get("file") is not None:
                raise RuntimeError("PHOTO_INVALID_DIMENSIONS")
            return await self.respond(text, **kw)

    ev = Refusing(OWNER, "/resize 10 10", reply=photo(ctx))
    await dispatch(ctx.client, ev)
    assert ctx.client.edited[-1][1] == "❌ Telegram wouldn't take the resized picture." and ctx.client.deleted == []


# ---------------------------------------------------------------------------
# what /resize used to be is gone
# ---------------------------------------------------------------------------


async def test_the_old_resize_buttons_do_nothing(ctx):
    for data in ("rz:s:320", "rz:custom", "rz:menu", "rz:stop"):
        cb = await click(ctx, OWNER, data)
        assert cb.edits == [] and cb.alerts == [], data
    assert not ctx.app.modes and not ctx.app.pending


async def test_a_picture_sent_after_the_command_is_not_resized_and_nothing_waits_for_one(ctx):
    ev = await say(ctx, OWNER, "/resize 320 200")
    assert "Reply to a picture" in ev.texts[0] and not ctx.app.modes and not ctx.app.pending
    ev = FakeEvent(OWNER, message=photo(ctx, mid=80), photo_bytes=jpeg())
    await dispatch(ctx.client, ev)
    assert ev.out == [] and ctx.client.files == []
