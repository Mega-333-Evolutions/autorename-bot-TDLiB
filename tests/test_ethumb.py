"""/ethumb: ask for a file or video, send back its thumbnail."""
import io
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from bot import app as appmod
from bot.tg import types

from .appkit import OWNER, STRANGER, auto_thumb, click, data_of, jpeg, say, send_doc
from .conftest import needs_ffmpeg
from .tgfakes import FakeEvent, FakeMessage, dispatch, document_message

REAL_THUMB = [SimpleNamespace(type="m")]  # stands in for a PhotoSize
STRIPPED = [types.PhotoStrippedSize(type="i")]


def photos_sent(ctx):
    return [(entity, file, kw) for entity, file, kw in ctx.client.files if isinstance(file, io.BytesIO)]


def temp_leftovers(ctx):
    root = Path(ctx.cfg.temp_dir)
    return list(root.glob("ethumb*")) if root.exists() else []


def serve(ctx, monkeypatch, media_dir, name="sample.mkv"):
    """Make the client's download_to copy a real sample file; returns the list of calls."""
    calls = []

    async def fake_download(message, dest, **kw):
        calls.append(kw)
        shutil.copy(media_dir / name, dest)

    monkeypatch.setattr(ctx.client, "download_to", fake_download)
    return calls


# ---------------------------------------------------------------------------
# the flow
# ---------------------------------------------------------------------------


async def test_ethumb_asks_for_a_file(ctx):
    ev = await say(ctx, OWNER, "/ethumb")
    assert "EXTRACT THUMBNAIL" in ev.texts[0] and "one file at a time" in ev.texts[0] and "/cancel" in ev.texts[0]
    assert data_of(ev.out[0][1]) == ["et:stop"]
    mode = ctx.app.modes[OWNER]
    assert mode.kind == "ethumb" and mode.expires > time.time() + 500


async def test_the_files_own_telegram_thumbnail_is_sent_when_it_has_one(ctx, monkeypatch):
    async def no_download(*a, **kw):
        raise AssertionError("nothing needs downloading when Telegram already has a preview")

    monkeypatch.setattr(ctx.client, "download_to", no_download)
    await say(ctx, OWNER, "/ethumb")
    ctx.client.thumb_data[60] = jpeg((320, 180))
    ev = await send_doc(ctx, OWNER, "Episode 01.mp4", thumbs=REAL_THUMB, mid=60)
    assert ev.texts == ["🖼 Extracting the thumbnail…"]
    (entity, file, kw), = photos_sent(ctx)
    assert entity == OWNER and file.name == "thumbnail.jpg" and Image.open(io.BytesIO(file.getvalue())).size == (320, 180)
    assert "Thumbnail of <b>Episode 01.mp4</b>" in kw["caption"] and kw["parse_mode"] == "html"
    assert (OWNER, [1001]) in ctx.client.deleted  # the "Extracting…" message goes
    assert OWNER not in ctx.app.modes and ctx.client.downloads == [(60, bytes, -1)]


@needs_ffmpeg
async def test_without_a_telegram_thumbnail_a_frame_is_grabbed_from_the_video(ctx, media_dir, monkeypatch):
    calls = serve(ctx, monkeypatch, media_dir)
    await say(ctx, OWNER, "/ethumb")
    await send_doc(ctx, OWNER, "Show - 05.mkv", size=(media_dir / "sample.mkv").stat().st_size, mid=61)
    (_e, file, kw), = photos_sent(ctx)
    img = Image.open(io.BytesIO(file.getvalue()))
    assert img.format == "JPEG" and img.size == (320, 240)
    assert len(calls) == 1 and calls[0]["max_bytes"] == appmod.TRACKS_PROBE_BYTES
    assert "Show - 05.mkv" in kw["caption"] and (OWNER, [1001]) in ctx.client.deleted
    assert temp_leftovers(ctx) == []


@needs_ffmpeg
async def test_the_whole_file_is_fetched_when_the_start_has_no_picture(ctx, media_dir, monkeypatch):
    calls = []

    async def fake_download(message, dest, **kw):
        calls.append(kw)
        if "max_bytes" in kw:
            Path(dest).write_bytes(b"\x00" * 4096)  # an mp4 whose index is at the end
        else:
            shutil.copy(media_dir / "sample.mp4", dest)

    monkeypatch.setattr(ctx.client, "download_to", fake_download)
    await say(ctx, OWNER, "/ethumb")
    await send_doc(ctx, OWNER, "Movie.mp4", size=40 * 1024 * 1024, mid=62)
    assert len(calls) == 2 and "max_bytes" not in calls[1]
    assert len(photos_sent(ctx)) == 1 and temp_leftovers(ctx) == []


@needs_ffmpeg
async def test_a_file_with_no_picture_anywhere_gets_an_honest_answer(ctx, monkeypatch):
    async def junk(message, dest, **kw):
        Path(dest).write_bytes(b"\x00" * 4096)

    monkeypatch.setattr(ctx.client, "download_to", junk)
    for size in (2 * 1024 * 1024, 900 * 1024 * 1024):  # small / too big to fetch whole
        ctx.client.edited.clear()
        await say(ctx, OWNER, "/ethumb")
        await send_doc(ctx, OWNER, "Broken.mkv", size=size, mid=63)
        assert "couldn't find a thumbnail" in ctx.client.edited[-1][1], size
    assert photos_sent(ctx) == [] and temp_leftovers(ctx) == []


async def test_files_that_cannot_have_a_frame_are_not_downloaded(ctx, monkeypatch):
    async def no_download(*a, **kw):
        raise AssertionError("a zip has no picture to grab")

    monkeypatch.setattr(ctx.client, "download_to", no_download)
    await say(ctx, OWNER, "/ethumb")
    await send_doc(ctx, OWNER, "archive.zip", size=5 * 1024 * 1024, video=False, mime="application/zip", mid=64)
    assert "couldn't find a thumbnail" in ctx.client.edited[-1][1] and photos_sent(ctx) == []


@needs_ffmpeg
async def test_the_tiny_blurred_preview_is_only_the_last_resort(ctx, media_dir, monkeypatch):
    serve(ctx, monkeypatch, media_dir)
    await say(ctx, OWNER, "/ethumb")
    ctx.client.thumb_data[65] = b"BLURRY-PREVIEW"
    await send_doc(ctx, OWNER, "Show.mkv", size=(media_dir / "sample.mkv").stat().st_size, thumbs=STRIPPED, mid=65)
    (_e, file, _kw), = photos_sent(ctx)
    assert file.getvalue() != b"BLURRY-PREVIEW" and Image.open(io.BytesIO(file.getvalue())).size == (320, 240)  # a real frame wins

    async def junk(message, dest, **kw):
        Path(dest).write_bytes(b"\x00" * 4096)

    monkeypatch.setattr(ctx.client, "download_to", junk)
    await say(ctx, OWNER, "/ethumb")
    await send_doc(ctx, OWNER, "Show2.mkv", size=2 * 1024 * 1024, thumbs=STRIPPED, mid=65)
    assert photos_sent(ctx)[-1][1].getvalue() == b"BLURRY-PREVIEW"  # nothing better could be made


async def test_ethumb_as_a_reply_works_at_once_without_asking(ctx):
    ctx.client.thumb_data[66] = jpeg((320, 180))
    reply = document_message("Episode.mp4", mid=66, thumbs=REAL_THUMB)
    await say(ctx, OWNER, "/ethumb", reply=reply, mid=67)
    assert len(photos_sent(ctx)) == 1 and OWNER not in ctx.app.modes


async def test_it_handles_one_file_per_command(ctx):
    ctx.client.thumb_data[70] = jpeg()
    await say(ctx, OWNER, "/ethumb")
    await send_doc(ctx, OWNER, "One.mp4", thumbs=REAL_THUMB, mid=70)
    extra = await send_doc(ctx, OWNER, "Two.mp4", thumbs=REAL_THUMB, mid=71)  # right after: a hint, not an auto-rename
    assert "/ethumb handles one file at a time" in extra.texts[0] and ctx.app.queue.waiting == 0
    assert (await send_doc(ctx, OWNER, "Three.mp4", thumbs=REAL_THUMB, mid=72)).out == []  # further ones: silence
    assert len(photos_sent(ctx)) == 1
    ctx.app._recent[OWNER][1] -= appmod.ONE_SHOT_GRACE + 1
    later = await send_doc(ctx, OWNER, "Four.mp4", mid=73)
    assert "Queued" in later.texts[0]  # normal auto-rename is back


async def test_a_photo_while_waiting_is_ignored_and_the_wait_goes_on(ctx):
    await say(ctx, OWNER, "/ethumb")
    ev = FakeEvent(OWNER, message=FakeMessage("", photo=True, mid=80), photo_bytes=jpeg())
    await dispatch(ctx.client, ev)
    assert ev.out == [] and await auto_thumb(ctx) is None and ctx.app.modes[OWNER].kind == "ethumb"  # a photo is nobody's thumbnail


async def test_cancel_and_the_stop_button_end_it(ctx):
    await say(ctx, OWNER, "/ethumb")
    ev = await say(ctx, OWNER, "/cancel")
    assert "Cancelled" in ev.texts[0] and OWNER not in ctx.app.modes
    ev = await send_doc(ctx, OWNER, "A.mkv", mid=90)
    assert "Queued" in ev.texts[0]

    await say(ctx, OWNER, "/ethumb")
    cb = await click(ctx, OWNER, "et:stop")
    assert "Stopped" in cb.edits[-1][0] and OWNER not in ctx.app.modes


async def test_it_expires(ctx):
    await say(ctx, OWNER, "/ethumb")
    ctx.app.modes[OWNER].expires = time.time() - 1
    ev = await send_doc(ctx, OWNER, "A.mkv", mid=91)
    assert "Queued" in ev.texts[0]


async def test_strangers_cannot_use_it(ctx):
    ev = await say(ctx, STRANGER, "/ethumb")
    assert ev.out == [] and STRANGER not in ctx.app.modes


async def test_starting_it_ends_a_sequence_and_says_so(ctx):
    await say(ctx, OWNER, "/sequence")
    await send_doc(ctx, OWNER, "Show - 01.mkv", mid=92)
    ev = await say(ctx, OWNER, "/ethumb")
    assert "1 file(s) was discarded" in ev.texts[0] and ctx.app.modes[OWNER].kind == "ethumb"


async def test_a_failing_extraction_is_reported_not_raised(ctx, monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(ctx.client, "download_to", boom)
    await say(ctx, OWNER, "/ethumb")
    ev = await send_doc(ctx, OWNER, "A.mkv", mid=93)
    assert ev.texts == ["🖼 Extracting the thumbnail…"] and "couldn't find a thumbnail" in ctx.client.edited[-1][1]
    assert temp_leftovers(ctx) == []
