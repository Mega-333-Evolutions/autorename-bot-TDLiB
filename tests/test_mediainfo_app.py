"""/mediainfo in the chat: reply to a file, get a Telegraph link."""
import io
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bot import app as appmod
from bot.telegraph import Telegraph, TelegraphError

from .appkit import OWNER, STRANGER, say, send_doc
from .conftest import needs_ffmpeg
from .faketelegraph import serve_fake_telegraph
from .tgfakes import FakeMessage, document_message

URL = "https://telegra.ph/Show-05-10-21"
HINT = "Reply to a file or video with /mediainfo"


class StubTelegraph:
    """What the bot talks to instead of Telegraph: remembers the pages it was asked for."""

    def __init__(self, url=URL, error=None):
        self.url, self.error, self.calls = url, error, []

    async def create_page(self, title, nodes):
        self.calls.append((title, nodes))
        if self.error is not None:
            raise self.error
        return self.url


def blocks(nodes):
    """{heading: text of its block} for a page's nodes."""
    return {n["children"][0]: nodes[i + 1]["children"][0] for i, n in enumerate(nodes) if n["tag"] == "h4"}


def notes(nodes):
    return [n["children"][0]["children"][0] for n in nodes if n["tag"] == "p"]


def serve(ctx, media_dir, name="sample.mkv"):
    """Make the client's download copy a real sample file; returns the list of calls (the keyword arguments of each)."""
    calls = []

    async def fake_download(message, dest, **kw):
        calls.append(kw)
        shutil.copy(media_dir / name, dest)

    ctx.client.download_to = fake_download
    return calls


def serve_junk(ctx, content=b"\x00" * 4096):
    calls = []

    async def fake_download(message, dest, **kw):
        calls.append(kw)
        Path(dest).write_bytes(content)

    ctx.client.download_to = fake_download
    return calls


def leftovers(ctx):
    root = Path(ctx.cfg.temp_dir)
    return list(root.glob("minfo*")) if root.exists() else []


def use_stub(ctx, **kw):
    ctx.app.telegraph = StubTelegraph(**kw)
    return ctx.app.telegraph


def real_size(media_dir, name="sample.mkv"):
    return (media_dir / name).stat().st_size


# ---------------------------------------------------------------------------
# the flow
# ---------------------------------------------------------------------------


@needs_ffmpeg
async def test_reply_to_a_video_and_the_telegraph_link_comes_back(ctx, media_dir):
    calls = serve(ctx, media_dir)
    tg = use_stub(ctx)
    video = document_message("Show - 05.mkv", real_size(media_dir), mid=70, caption="Dual Audio")
    video.date = datetime(2026, 10, 19, 21, 30, 12, tzinfo=timezone.utc)
    video.document.dc_id = 4
    ev = await say(ctx, OWNER, "/mediainfo", reply=video)
    assert ev.texts == ["🔎 Reading the file…"]

    entity, text, kw = ctx.client.edited[-1]
    assert entity.id == 1001  # the "Reading…" message became the answer
    assert "🔎 <b>MediaInfo</b> · Show - 05.mkv" in text and URL in text and "Matroska" in text and "1 video · 3 audio · 2 subtitle" in text
    assert kw["parse_mode"] == "html" and kw["link_preview"] is False
    (button,), = kw["buttons"]
    assert button.type.url == URL and button.text.startswith("📄")

    (title, nodes), = tg.calls
    assert title == "MediaInfo – Show - 05.mkv"
    page = blocks(nodes)
    assert list(page) == ["General", "Video", "Audio #1", "Audio #2", "Audio #3", "Text #1", "Text #2", "Menu", "Telegram"]
    assert "Show - 05.mkv" in page["General"] and "probe.mkv" not in page["General"]  # the file's own name, not the temp copy
    telegram = page["Telegram"]
    for line in ("File name   : Show - 05.mkv", "MIME type   : video/x-matroska", "Sent as     : Video",
                 "Sent on     : 2026-10-19 21:30:12 UTC", "Data center : 4", "Caption     : Dual Audio"):
        assert line in telegram, line
    assert len(calls) == 1 and calls[0]["max_bytes"] == appmod.TRACKS_PROBE_BYTES  # the start of the file was enough
    assert leftovers(ctx) == []


async def test_without_a_file_there_is_a_hint_and_nothing_is_downloaded(ctx):
    async def no_download(*a, **kw):
        raise AssertionError("there is nothing to read")

    ctx.client.download_to = no_download
    tg = use_stub(ctx)
    for reply in (None, FakeMessage("just some text")):
        ev = await say(ctx, OWNER, "/mediainfo", reply=reply)
        assert HINT in ev.texts[0] and "Telegraph" in ev.texts[0]
    assert tg.calls == [] and ctx.client.edited == []


@needs_ffmpeg
async def test_a_file_sent_with_the_command_as_its_caption_is_read_not_renamed(ctx, media_dir):
    serve(ctx, media_dir)
    tg = use_stub(ctx)
    for mid, caption in ((71, "/mediainfo"), (72, "/mediainfo@my_rename_bot")):
        ev = await send_doc(ctx, OWNER, "Show.mkv", size=real_size(media_dir), caption=caption, mid=mid)
        assert ev.texts == ["🔎 Reading the file…"]  # no "Queued" message from the auto-renamer
    assert ctx.app.queue.waiting == 0 and len(tg.calls) == 2
    assert "Caption" not in blocks(tg.calls[0][1])["Telegram"]  # the caption was only the command


async def test_other_captions_still_rename_the_file(ctx):
    ev = await send_doc(ctx, OWNER, "Show.mkv", size=5 * 1024 * 1024, caption="/mediainfoX", mid=73)
    assert ev.texts[0].startswith("📥 <b>Queued</b>") and ctx.app.queue.waiting == 1


@needs_ffmpeg
async def test_a_video_whose_index_is_at_the_end_is_read_from_the_whole_file(ctx, media_dir):
    calls = []

    async def fake_download(message, dest, **kw):
        calls.append(kw)
        if "max_bytes" in kw:
            Path(dest).write_bytes((media_dir / "sample.mp4").read_bytes()[:3000])  # an mp4 cut before its index
        else:
            shutil.copy(media_dir / "sample.mp4", dest)

    ctx.client.download_to = fake_download
    tg = use_stub(ctx)
    await say(ctx, OWNER, "/mediainfo", reply=document_message("Movie.mp4", 40 * 1024 * 1024, mid=74, mime="video/mp4"))
    assert len(calls) == 2 and "max_bytes" in calls[0] and "max_bytes" not in calls[1]
    assert any("needs the whole file" in text for _e, text, _k in ctx.client.edited)
    (_title, nodes), = tg.calls
    assert list(blocks(nodes))[:2] == ["General", "Video"] and "No tracks" not in " ".join(notes(nodes))
    assert URL in ctx.client.edited[-1][1] and leftovers(ctx) == []


@pytest.mark.parametrize("size, why", [(2 * 1024 * 1024, "may be damaged"), (900 * 1024 * 1024, "too big for me to download completely")])
async def test_a_video_with_no_readable_tracks_still_gets_a_page_that_says_so(ctx, size, why):
    calls = serve_junk(ctx)
    tg = use_stub(ctx)
    await say(ctx, OWNER, "/mediainfo", reply=document_message("Broken.mkv", size, mid=75))
    assert len(calls) == 1  # too small to need more, or too big to fetch whole
    (_title, nodes), = tg.calls
    assert any(why in note for note in notes(nodes))
    assert list(blocks(nodes)) == ["General", "Telegram"] and URL in ctx.client.edited[-1][1]
    assert leftovers(ctx) == []


async def test_a_file_that_is_not_media_is_read_from_its_start_only(ctx):
    calls = serve_junk(ctx, b"PK\x03\x04" + b"\x00" * 4000)
    tg = use_stub(ctx)
    await say(ctx, OWNER, "/mediainfo", reply=document_message("archive.zip", 50 * 1024 * 1024, mid=76, video=False, mime="application/zip"))
    assert len(calls) == 1 and "max_bytes" in calls[0]
    (_title, nodes), = tg.calls
    page = blocks(nodes)
    assert list(page) == ["General", "Telegram"] and "archive.zip" in page["General"] and "50.0 MiB" in page["General"]
    assert "Sent as   : Document" in page["Telegram"] and "No tracks" not in " ".join(notes(nodes))
    assert any("Only the start of the file was read" in note for note in notes(nodes))


async def test_a_download_that_fails_is_reported_and_cleaned_up(ctx):
    async def fail(message, dest, **kw):
        Path(dest).write_bytes(b"partial")
        raise ConnectionError("connection reset")

    ctx.client.download_to = fail
    tg = use_stub(ctx)
    await say(ctx, OWNER, "/mediainfo", reply=document_message("Show.mkv", 5 * 1024 * 1024, mid=77))
    assert ctx.client.edited[-1][1] == "❌ I couldn't read that file." and tg.calls == [] and leftovers(ctx) == []


# ---------------------------------------------------------------------------
# when Telegraph does not work
# ---------------------------------------------------------------------------


@needs_ffmpeg
async def test_when_telegraph_fails_the_report_comes_as_a_text_file(ctx, media_dir):
    serve(ctx, media_dir)
    use_stub(ctx, error=TelegraphError("Telegraph asks to wait 5 seconds", "FLOOD_WAIT_5"))
    await say(ctx, OWNER, "/mediainfo", reply=document_message("Show: 05?.mkv", real_size(media_dir), mid=78))
    (entity, file, kw), = [f for f in ctx.client.files if isinstance(f[1], io.BytesIO)]
    assert entity == OWNER and file.name == "Show_ 05_ - MediaInfo.txt" and kw["force_document"] is True
    assert "Telegraph asks to wait 5 seconds, so here it is as a text file." in kw["caption"] and kw["parse_mode"] == "html"
    text = file.getvalue().decode()
    assert text.startswith("General\n") and "\nVideo\n" in text and "\nTelegram\nFile name" in text and "Show: 05?.mkv" in text
    assert (OWNER, [1001]) in ctx.client.deleted  # the "Reading…" message goes
    assert leftovers(ctx) == []


async def test_another_telegraph_failure_does_not_show_its_details(ctx):
    serve_junk(ctx)
    use_stub(ctx, error=RuntimeError("secret token abc123 rejected"))
    await say(ctx, OWNER, "/mediainfo", reply=document_message("Show.mkv", 2 * 1024 * 1024, mid=79))
    (_e, _file, kw), = [f for f in ctx.client.files if isinstance(f[1], io.BytesIO)]
    assert "Telegraph is not available, so here it is as a text file." in kw["caption"] and "abc123" not in kw["caption"]


async def test_an_unexpected_error_does_not_leave_the_reading_message_hanging(ctx, monkeypatch):
    def broken(message, caption=True):
        raise RuntimeError("boom")

    monkeypatch.setattr(appmod, "telegram_facts", broken)
    ev = await say(ctx, OWNER, "/mediainfo", reply=document_message("Show.mkv", 2 * 1024 * 1024, mid=80))
    assert ev.texts == ["🔎 Reading the file…"]
    assert ctx.client.edited[-1][1] == "⚠️ Something went wrong while reading that file."


# ---------------------------------------------------------------------------
# access and plumbing
# ---------------------------------------------------------------------------


async def test_only_allowed_users_can_use_it(ctx, monkeypatch):
    tg = use_stub(ctx)
    ev = await say(ctx, STRANGER, "/mediainfo", reply=document_message("Show.mkv", 1024, mid=81))
    assert ev.out == [] and tg.calls == []


async def test_the_command_is_in_the_menu():
    commands = dict(appmod.COMMANDS)
    assert "mediainfo" in commands and 3 <= len(commands["mediainfo"]) <= 256


async def test_the_telegraph_token_lives_in_the_database(ctx):
    assert await ctx.app._load_telegraph_token() is None
    await ctx.app._save_telegraph_token("abc")
    assert await ctx.app._load_telegraph_token() == "abc" and await ctx.db.kv_get("telegraph_token") == "abc"
    await ctx.app._save_telegraph_token("")  # what the client does when Telegraph rejects the token
    assert await ctx.db.kv_get("telegraph_token") is None


@needs_ffmpeg
async def test_the_whole_chain_with_the_real_client_and_a_fake_telegraph(ctx, media_dir):
    serve(ctx, media_dir, "sample.mp4")
    fake = await serve_fake_telegraph()
    try:
        ctx.app.telegraph = Telegraph(ctx.app._load_telegraph_token, ctx.app._save_telegraph_token, api=fake.url)
        ctx.app.telegraph.set_author("Rename Bot", "https://t.me/rename_bot")
        await say(ctx, OWNER, "/mediainfo", reply=document_message("Movie.mp4", real_size(media_dir, "sample.mp4"), mid=82, mime="video/mp4"))
        await say(ctx, OWNER, "/mediainfo", reply=document_message("Movie 2.mp4", real_size(media_dir, "sample.mp4"), mid=83, mime="video/mp4"))
        assert fake.accounts == 1 and len(fake.pages) == 2  # one account, kept for the second page
        assert await ctx.db.kv_get("telegraph_token") == "token1"
        page = fake.pages[0]
        assert page["title"] == "MediaInfo – Movie.mp4" and page["author_name"] == "Rename Bot" and page["author_url"] == "https://t.me/rename_bot"
        assert "Audio #2" in page["content"] and "Movie.mp4" in page["content"]
        assert "https://telegra.ph/Page-1" in ctx.client.edited[-2][1] and "https://telegra.ph/Page-2" in ctx.client.edited[-1][1]
    finally:
        await fake.server.close()
