"""/smedia: the owner picks an image or a GIF, and /start shows it above the menu."""
import base64
import io
import json
import time
from types import SimpleNamespace

from PIL import Image

from bot import app as appmod
from bot.tg import types

from .appkit import ADMIN_CFG, OWNER, STRANGER, auto_thumb, autofmt, click, data_of, edit_auto, jpeg, say, send_doc
from .tgfakes import FakeEvent, FakeMessage, dispatch, make_file

GIF_BYTES = b"\x00\x00\x00\x18ftypmp42-fake-animation-bytes"


def uploads(ctx):
    """The files the bot uploaded (bytes), as (chat, file, kwargs)."""
    return [(entity, file, kw) for entity, file, kw in ctx.client.files if isinstance(file, io.BytesIO)]


async def send_photo(ctx, mid=50, data=None, sender=OWNER):
    ctx.client.media[mid] = data if data is not None else jpeg((800, 600))
    ev = FakeEvent(sender, message=FakeMessage("", photo=True, mid=mid), photo_bytes=jpeg())  # photo_bytes: what a thumbnail is made of
    await dispatch(ctx.client, ev)
    return ev


async def send_image_file(ctx, name="banner.png", mid=51, data=None, size=200 * 1024, sender=OWNER):
    ctx.client.media[mid] = data if data is not None else jpeg((800, 600))
    return await send_doc(ctx, sender, name, size=size, video=False, mime="image/png", mid=mid)


async def send_gif(ctx, mid=60, data=GIF_BYTES, size=1024 * 1024, sender=OWNER):
    ctx.client.media[mid] = data
    file = make_file("anim.mp4", size, "video/mp4", width=480, height=270, duration=3.5)
    ev = FakeEvent(sender, message=FakeMessage("", mid=mid, file=file, video=True, gif=True))
    await dispatch(ctx.client, ev)
    return ev


async def choose(ctx, kind):
    return await click(ctx, OWNER, f"sm:{kind}")


async def stored(ctx):
    """(description, file bytes) of the saved /start media, or (None, None)."""
    meta = await ctx.db.kv_get("start_media")
    data = await ctx.db.kv_get("start_media_data")
    return (json.loads(meta) if meta else None), (base64.b64decode(data) if data else None)


def telegram_gives_ids(ctx, *ids):
    """Make send_file answer like Telegram does: with a message whose file has an id."""
    original, wanted = ctx.client.send_file, iter(ids)

    async def send_file(entity, file, **kw):
        sent = await original(entity, file, **kw)
        sent.file = SimpleNamespace(id=next(wanted, "FILEID-LATER"))
        return sent

    ctx.client.send_file = send_file


def file_ids_go_stale(ctx):
    """Sending by file id fails (an expired reference); uploading still works."""
    original = ctx.client.send_file

    async def send_file(entity, file, **kw):
        if isinstance(file, str):
            raise RuntimeError("FILE_REFERENCE_EXPIRED")
        return await original(entity, file, **kw)

    ctx.client.send_file = send_file


# ---------------------------------------------------------------------------
# the menu
# ---------------------------------------------------------------------------


async def test_the_menu_offers_image_and_gif(ctx):
    ev = await say(ctx, OWNER, "/smedia")
    assert "START MEDIA" in ev.texts[0] and "/start shows: <b>nothing, just the menu</b>" in ev.texts[0]
    assert data_of(ev.out[0][1]) == ["sm:image", "sm:gif", "x:close"]  # nothing to remove yet
    assert OWNER not in ctx.app.modes


async def test_only_the_owner_can_use_it(ctx):
    assert "smedia" in appmod.OWNER_COMMANDS and "smedia" not in dict(appmod.COMMANDS)  # not in the public command menu
    ev = await say(ctx, ADMIN_CFG, "/smedia")
    assert ev.texts == ["⛔ Only the owner can do that."]
    assert (await say(ctx, STRANGER, "/smedia")).out == []
    cb = await click(ctx, ADMIN_CFG, "sm:image")  # an allowed user who got hold of the buttons
    assert cb.alerts == ["Owner only."] and ADMIN_CFG not in ctx.app.modes
    assert not hasattr(ctx.cfg, "start_image")  # the START_IMAGE setting is gone


async def test_choosing_makes_the_bot_wait_for_that_kind_of_media(ctx):
    cb = await choose(ctx, "image")
    assert "Send the image" in cb.edits[-1][0] and "/cancel to stop" in cb.edits[-1][0]
    assert data_of(cb.edits[-1][1]) == ["sm:menu", "sm:stop"]
    mode = ctx.app.modes[OWNER]
    assert mode.kind == "smedia" and mode.data == {"kind": "image"} and mode.expires > time.time() + 500
    cb = await choose(ctx, "gif")
    assert "Send the GIF" in cb.edits[-1][0] and ctx.app.modes[OWNER].data == {"kind": "gif"}


async def test_back_stop_and_cancel_leave_nothing_waiting(ctx):
    await choose(ctx, "image")
    cb = await click(ctx, OWNER, "sm:menu")
    assert "START MEDIA" in cb.edits[-1][0] and OWNER not in ctx.app.modes
    await choose(ctx, "gif")
    cb = await click(ctx, OWNER, "sm:stop")
    assert "Stopped" in cb.edits[-1][0] and OWNER not in ctx.app.modes
    await choose(ctx, "gif")
    ev = await say(ctx, OWNER, "/cancel")
    assert "Send /smedia again" in ev.texts[0] and OWNER not in ctx.app.modes


# ---------------------------------------------------------------------------
# an image
# ---------------------------------------------------------------------------


async def test_a_photo_becomes_the_start_image_and_is_shown_right_away(ctx):
    await choose(ctx, "image")
    photo = jpeg((800, 600))
    ev = await send_photo(ctx, data=photo)
    (entity, file, kw), = uploads(ctx)
    assert entity == OWNER and file.name == "start.jpg" and file.getvalue() == photo
    assert "Auto Rename Bot" in kw["caption"] and "m:rename" in data_of(kw["buttons"]) and kw["parse_mode"] == "html"
    assert ev.texts == ["✅ Done: /start shows this image from now on."]
    assert OWNER not in ctx.app.modes and ctx.client.downloads == [(50, bytes, None)]
    meta, data = await stored(ctx)
    assert meta == {"kind": "image", "file_id": ""} and data == photo


async def test_the_menu_then_shows_what_is_set_and_offers_to_remove_it(ctx):
    await choose(ctx, "image")
    await send_photo(ctx)
    ev = await say(ctx, OWNER, "/smedia")
    assert "/start shows: <b>an image</b>" in ev.texts[0] and data_of(ev.out[0][1]) == ["sm:image", "sm:gif", "sm:rm", "x:close"]


async def test_an_image_sent_as_a_file_is_made_to_fit_without_being_enlarged(ctx):
    await choose(ctx, "image")
    ev = await send_image_file(ctx, data=jpeg((3000, 2000)))
    assert ev.texts == ["✅ Done: /start shows this image from now on."] and ctx.app.queue.waiting == 0  # not renamed
    _meta, data = await stored(ctx)
    img = Image.open(io.BytesIO(data))
    assert img.format == "JPEG" and img.size == (1280, 853)
    await choose(ctx, "image")
    await send_image_file(ctx, mid=52, data=jpeg((400, 300)))
    assert Image.open(io.BytesIO((await stored(ctx))[1])).size == (400, 300)  # smaller pictures stay as they are


async def test_what_is_not_an_image_does_not_use_up_the_request(ctx):
    await choose(ctx, "image")
    ev = await send_doc(ctx, OWNER, "Episode.mkv", mid=53)
    assert "isn't an image" in ev.texts[0] and ctx.app.queue.waiting == 0 and uploads(ctx) == []  # not renamed either
    ev = await send_gif(ctx)
    assert "isn't an image" in ev.texts[0]
    assert ctx.app.modes[OWNER].kind == "smedia"
    await send_photo(ctx)
    assert len(uploads(ctx)) == 1 and (await stored(ctx))[0]["kind"] == "image"


async def test_a_picture_that_cannot_be_read_is_refused(ctx):
    await choose(ctx, "image")
    ev = await send_image_file(ctx, data=b"this is not a picture")
    assert "couldn't read that image" in ev.texts[0] and await stored(ctx) == (None, None) and uploads(ctx) == []


async def test_a_download_that_gives_nothing_is_reported(ctx):
    await choose(ctx, "image")
    ev = await send_photo(ctx, data=b"")
    assert "couldn't download that" in ev.texts[0] and "/smedia" in ev.texts[0]
    assert await stored(ctx) == (None, None) and OWNER not in ctx.app.modes


async def test_a_file_that_is_too_big_is_refused_and_the_request_stays(ctx):
    await choose(ctx, "image")
    ev = await send_image_file(ctx, size=appmod.START_MEDIA_MAX_BYTES + 1)
    assert "the biggest I can keep is 10.00 MB" in ev.texts[0] and await stored(ctx) == (None, None)
    assert ctx.app.modes[OWNER].kind == "smedia" and ctx.client.downloads == []  # refused before downloading


# ---------------------------------------------------------------------------
# a GIF
# ---------------------------------------------------------------------------


async def test_a_gif_becomes_the_start_gif_and_is_sent_as_an_animation(ctx):
    await choose(ctx, "gif")
    ev = await send_gif(ctx)
    assert ev.texts == ["✅ Done: /start shows this GIF from now on."]
    (entity, file, kw), = uploads(ctx)
    assert entity == OWNER and file.name == "start.mp4" and file.getvalue() == GIF_BYTES and kw["mime_type"] == "video/mp4"
    video = next(a for a in kw["attributes"] if isinstance(a, types.DocumentAttributeVideo))
    assert (video.duration, video.w, video.h, video.supports_streaming) == (3.5, 480, 270, True)
    assert any(isinstance(a, types.DocumentAttributeAnimated) for a in kw["attributes"])
    assert "Auto Rename Bot" in kw["caption"] and kw["buttons"]
    meta, data = await stored(ctx)
    assert meta == {"kind": "gif", "mime": "video/mp4", "width": 480, "height": 270, "duration": 3.5, "file_id": ""} and data == GIF_BYTES
    assert "/start shows: <b>a GIF</b>" in (await say(ctx, OWNER, "/smedia")).texts[0]


async def test_what_is_not_a_gif_does_not_use_up_the_request(ctx):
    await choose(ctx, "gif")
    for ev in (await send_photo(ctx), await send_doc(ctx, OWNER, "Episode.mkv", mid=54)):
        assert "isn't a GIF" in ev.texts[0]
    assert ctx.app.queue.waiting == 0 and ctx.app.modes[OWNER].data == {"kind": "gif"} and await stored(ctx) == (None, None)
    await send_gif(ctx)
    assert (await stored(ctx))[0]["kind"] == "gif"


async def test_a_gif_nobody_asked_for_is_ignored(ctx):
    ev = await send_gif(ctx)
    assert ev.out == [] and await stored(ctx) == (None, None)
    assert (await send_gif(ctx, sender=STRANGER)).out == []


# ---------------------------------------------------------------------------
# one file per request
# ---------------------------------------------------------------------------


async def test_extra_files_right_after_are_skipped_and_do_not_become_the_thumbnail(ctx):
    await choose(ctx, "image")
    await send_photo(ctx, mid=50)
    extra = await send_photo(ctx, mid=51)
    assert "/smedia handles one file at a time" in extra.texts[0]
    assert (await send_photo(ctx, mid=52)).out == []  # the hint is given once
    assert (await send_gif(ctx)).out == [] and (await send_image_file(ctx, mid=53)).out == []
    assert len(uploads(ctx)) == 1 and await autofmt(ctx) is None and ctx.app.queue.waiting == 0  # no photo was saved as a thumbnail
    ctx.app._recent[OWNER][1] -= appmod.ONE_SHOT_GRACE + 1
    ev = await send_photo(ctx, mid=54)
    assert ev.out == [] and await auto_thumb(ctx) is None and await autofmt(ctx) is None  # after the grace a photo is just ignored


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------


async def test_start_without_media_is_just_the_menu(ctx):
    ev = await say(ctx, OWNER, "/start")
    assert "Auto Rename Bot" in ev.texts[0] and ctx.client.files == []


async def test_start_reuses_the_telegram_file_instead_of_uploading_again(ctx):
    telegram_gives_ids(ctx, "FILEID-1")
    await choose(ctx, "image")
    await send_photo(ctx)
    assert (await stored(ctx))[0]["file_id"] == "FILEID-1"  # remembered from the preview
    ev = await say(ctx, ADMIN_CFG, "/start")  # every allowed user sees it
    assert ev.out == [] and len(uploads(ctx)) == 1
    (entity, file, kw) = ctx.client.files[-1]
    assert entity == ADMIN_CFG and file == "FILEID-1" and "Auto Rename Bot" in kw["caption"] and "m:rename" in data_of(kw["buttons"])
    assert "m:admin" not in data_of(kw["buttons"])


async def test_a_stale_file_id_falls_back_to_the_stored_copy_and_is_replaced(ctx):
    telegram_gives_ids(ctx, "FILEID-1", "FILEID-2")
    await choose(ctx, "gif")
    await send_gif(ctx)
    file_ids_go_stale(ctx)
    ev = await say(ctx, OWNER, "/start")
    assert ev.out == []  # still shown with its picture
    entity, file, kw = ctx.client.files[-1]
    assert isinstance(file, io.BytesIO) and file.getvalue() == GIF_BYTES and kw["mime_type"] == "video/mp4"
    assert (await stored(ctx))[0]["file_id"] == "FILEID-2"  # the next /start can use the fresh one


async def test_when_nothing_can_be_sent_start_still_shows_the_menu(ctx):
    await choose(ctx, "image")
    await send_photo(ctx)
    ctx.client.fail_send_file = True
    ev = await say(ctx, OWNER, "/start")
    assert "Auto Rename Bot" in ev.texts[0] and data_of(ev.out[0][1])[:2] == ["m:auto", "m:rename"]


async def test_damaged_saved_media_never_breaks_start(ctx):
    for meta, data in (
        ("not json", "AAAA"), (json.dumps({"kind": "video", "file_id": ""}), "AAAA"), (json.dumps(["kind"]), "AAAA"),
        (json.dumps({"kind": "image", "file_id": ""}), "!!! not base64 !!!"), (json.dumps({"kind": "image", "file_id": ""}), ""),
    ):
        await ctx.app._store_start_media(meta, data)
        ev = await say(ctx, OWNER, "/start")
        assert "Auto Rename Bot" in ev.texts[0], meta
    assert ctx.client.files == []


async def test_the_menu_fits_in_a_caption(ctx):
    await choose(ctx, "image")
    await send_photo(ctx)
    await edit_auto(ctx, template="{title} " * 30)
    await say(ctx, OWNER, "/start")
    assert appmod.visible_len(ctx.client.files[-1][2]["caption"]) < 1024


# ---------------------------------------------------------------------------
# when Telegram refuses it, and removing it
# ---------------------------------------------------------------------------


async def test_a_picture_telegram_refuses_leaves_the_old_one_in_place(ctx):
    await choose(ctx, "image")
    first = jpeg((640, 480))
    await send_photo(ctx, data=first)
    before = await stored(ctx)
    ctx.client.fail_send_file = True
    await choose(ctx, "image")
    ev = await send_photo(ctx, mid=55, data=jpeg((300, 300)))
    assert "wouldn't take that" in ev.texts[0] and "nothing was changed" in ev.texts[0]
    assert await stored(ctx) == before == ({"kind": "image", "file_id": ""}, first)


async def test_a_picture_telegram_refuses_is_not_kept_when_there_was_none(ctx):
    ctx.client.fail_send_file = True
    await choose(ctx, "gif")
    ev = await send_gif(ctx)
    assert "wouldn't take that" in ev.texts[0] and await stored(ctx) == (None, None)


async def test_remove_goes_back_to_the_plain_menu(ctx):
    await choose(ctx, "image")
    await send_photo(ctx)
    cb = await click(ctx, OWNER, "sm:rm")
    assert "nothing, just the menu" in cb.edits[-1][0] and data_of(cb.edits[-1][1]) == ["sm:image", "sm:gif", "x:close"]
    assert cb.toasts == ["Start media removed."] and await stored(ctx) == (None, None)
    count = len(ctx.client.files)
    ev = await say(ctx, OWNER, "/start")
    assert "Auto Rename Bot" in ev.texts[0] and len(ctx.client.files) == count
