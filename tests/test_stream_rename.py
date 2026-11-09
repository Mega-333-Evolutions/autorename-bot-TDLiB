"""Streaming renames: a big file goes straight from the download into the upload, and anything that needs the file takes the usual way.

The relay that moves the bytes (``transfer.Relay``, tested in test_relay) is a stand-in here: these tests are about what the pipeline decides."""
import asyncio
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot import errorlog, pipeline, transfer
from bot.config import load_config
from bot.db import Database
from bot.media import probe
from bot.pipeline import Renamer, embedded_thumb, make_caption, source_attributes
from bot.queue import Job
from bot.tg import Generated, Message, errors, types
from bot.utils import human_duration, human_size

from .appkit import ensure_auto
from .conftest import needs_ffmpeg
from .tdfakes import (audio_content, document_content, formatted, photo_content, remote_id, td_file, td_message, text_content, thumbnail_of,
                      video_content)

MIB = 1024 * 1024
BIG = 40 * MIB  # more than the 10 MB the streaming starts at; nothing this size is really moved: the relay is a stub here
NAME = "[SubsPlease] Tokyo Revengers - 05 (1080p) [ABCD1234].mkv"
NEW_NAME = "[S01E05] Tokyo Revengers [1080p].mkv"
PREVIEW = b"\xff\xd8preview-of-the-original"
BLUR = "AQIDBAUG"  # the tiny blurred preview, base64


def video_attr(duration=1400, w=1920, h=1080):
    return {"duration": duration, "w": w, "h": h}


def audio_attr(duration=215, title="Song", performer="Me"):
    return {"duration": duration, "title": title, "performer": performer}


def preview_sizes():
    """A real preview as well as the tiny blurred one."""
    return "both"


def FakeMessage(name=NAME, size=BIG, *, video=None, audio=None, thumbs=None, mime="video/x-matroska", msg_id=7, document=True, photo=False, dc=None):
    """A message the way TDLib hands it over.  ``video`` / ``audio`` are the details Telegram keeps with a file that was sent as a video or
    a song; ``thumbs`` is ``None``, ``"stripped"`` (only the tiny blurred preview) or ``"both"``; ``dc`` the data center of the file.
    ``document=False`` is a message of just words, ``photo=True`` a picture: neither has a document."""
    persistent = remote_id(dc, kind=4 if video else 9 if audio else 5) if dc else ""
    real = thumbnail_of() if thumbs == "both" else None
    blur = BLUR if thumbs in ("stripped", "both") else None
    if photo:
        content = photo_content(size=size, remote_id="")
    elif not document:
        content = text_content("just words")
    elif video is not None:
        content = video_content(name, size, duration=video["duration"], width=video["w"], height=video["h"], mime=mime, remote_id=persistent,
                                thumbnail=real, minithumbnail=blur)
    elif audio is not None:
        content = audio_content(name, size, duration=audio["duration"], title=audio["title"], performer=audio["performer"], mime=mime,
                                remote_id=persistent)
    else:
        content = document_content(name, size, mime=mime, remote_id=persistent, thumbnail=real, minithumbnail=blur)
    return Message(td_message(content, chat_id=42, plain_id=msg_id, sender=42))


class FakeRelay:
    """Stands in for ``transfer.Relay``: what the pipeline sees of it (``source``, ``cancel``, ``error``), without TDLib."""

    def __init__(self, client, message, new_name, *, cancel=None):
        client.relays.append((message, new_name))
        client.relay = self
        self.source = Generated(new_name, "relay:test", message.file.size)
        self.cancel = asyncio.Event()
        self.error = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class FakeClient:
    def __init__(self, message, source):
        self.message, self.source = message, source
        self.session = SimpleNamespace(dc_id=1)  # the data center the bot's own account is on
        self.sent, self.edits = [], []
        self.relays, self.downloads, self.uploads, self.preview_downloads = [], [], [], 0
        self.relay = None
        self.stream_failure = None  # the download side of a streamed rename fails with this
        self.during_stream = None  # called when a streamed send is under way (a test cancels the job from it)
        self.send_errors = []

    async def get_messages(self, chat_id, ids):
        return self.message

    async def download_media(self, message, file=None, thumb=None, **_):
        assert thumb is not None, "the whole file is fetched by download_to, which is faked"
        self.preview_downloads += 1
        return PREVIEW

    async def download_to(self, message, dest, *, progress=None, cancel=None, max_bytes=None):
        self.downloads.append(Path(dest).name)
        shutil.copy(self.source, dest)
        return Path(dest).stat().st_size

    async def send_file(self, target, source, **kwargs):
        if isinstance(source, Generated):  # streamed: what is sent is the file the relay makes
            if self.stream_failure is not None:  # the relay says why, and the upload stops
                try:  # a failure is raised somewhere, as the real one is: that is where its traceback comes from
                    raise self.stream_failure
                except Exception as failure:  # noqa: BLE001
                    self.relay.error = failure
                self.relay.cancel.set()
                raise errors.TransferCancelled()
            if self.during_stream is not None:
                self.during_stream()
                raise errors.TransferCancelled()
        if self.send_errors:
            raise self.send_errors.pop(0)
        if isinstance(source, Generated):
            kwargs["progress"](source.size // 2, source.size)
            kwargs["progress"](source.size, source.size)
        else:
            self.uploads.append(Path(source).read_bytes())
            kwargs["progress"](1, 1)
        self.sent.append((target, source, kwargs))
        return SimpleNamespace(id=3000 + len(self.sent))

    async def edit_message(self, message, text, buttons=None, parse_mode=None, **_):
        self.edits.append((text, buttons))


@pytest.fixture(autouse=True)
def fresh_stats(monkeypatch):
    monkeypatch.setattr(transfer, "stats", transfer.TransferStats())
    monkeypatch.setattr(transfer, "Relay", FakeRelay)


@pytest.fixture
async def env(tmp_path, media_dir):
    def new_cfg(**environ):
        return load_config({
            "API_ID": "1", "API_HASH": "h", "BOT_TOKEN": "1:t", "OWNER_ID": "42", "TEMP_DIR": str(tmp_path / "work"),
            "TDLIB_DIR": str(tmp_path / "td"), "PROGRESS_INTERVAL": "2", "STREAM_RENAME": "1", **environ,  # streaming is on unless a test turns it off
        })

    db = Database(f"sqlite+aiosqlite:///{tmp_path}/s.db")
    await db.init()
    ns = SimpleNamespace(db=db, media_dir=media_dir, tmp=tmp_path, new_cfg=new_cfg, cfg=new_cfg())
    yield ns
    await db.close()


@pytest.fixture
async def log_channel():
    """What reaches the log channel while a test runs."""
    reports = []

    async def send(text):
        reports.append(text)

    handler = errorlog.install(send)
    yield SimpleNamespace(reports=reports, flush=handler.aclose)
    handler.detach()


async def rename(env, message=None, *, cfg=None, new_name=NEW_NAME, fmt=None, source="sample.mkv"):
    """Run one job; returns ``(client, job)``."""
    message = message or FakeMessage(video=video_attr(), thumbs=preview_sizes())
    client = FakeClient(message, env.media_dir / source)
    job = Job(id=1, user_id=42, chat_id=42, msg_id=7, file_name=getattr(message.file, "name", None) or "x", size=getattr(message.file, "size", 0), new_name=new_name,
              status_msg=SimpleNamespace(id=99), fmt=fmt)
    await Renamer(client, cfg or env.cfg, env.db).process(job)
    return client, job


def attributes_by_type(kwargs):
    return {type(a): a for a in kwargs["attributes"]}


async def auto(env, thumb=None, **changes):
    """The user's auto format (made on demand) with these changes and this thumbnail: what renames the files they just send."""
    fmt = await ensure_auto(env.db, 42)
    if changes:
        await env.db.update_format(42, fmt["id"], **changes)
    if thumb:
        await env.db.set_format_thumb(42, fmt["id"], thumb)
    return await env.db.get_format(42, fmt["id"])


# ---------------------------------------------------------------------------
# the streamed way
# ---------------------------------------------------------------------------


async def test_a_big_plain_rename_is_streamed_with_the_details_telegram_already_has(env):
    message = FakeMessage(video=video_attr(), thumbs=preview_sizes())
    client, job = await rename(env, message)

    assert client.relays == [(message, NEW_NAME)]
    assert client.downloads == [] and client.uploads == []  # nothing was saved, and nothing uploaded from a file
    (target, source, kw), = client.sent
    assert target == 42 and kw["reply_to"] == 7
    assert isinstance(source, Generated) and source.name == NEW_NAME and source.size == BIG  # what is sent is the file the relay makes
    by_type = attributes_by_type(kw)
    assert by_type[types.DocumentAttributeFilename].file_name == NEW_NAME
    video = by_type[types.DocumentAttributeVideo]
    assert (video.duration, video.w, video.h, video.supports_streaming) == (1400, 1920, 1080, True)
    assert kw["force_document"] is False and kw["mime_type"] == "video/x-matroska" and kw["parse_mode"] == "html"
    assert kw["thumb"] == PREVIEW and client.preview_downloads == 1  # the original's own preview, as no frame can be cut from a file
    assert kw["caption"] == "<b>[S01E05] Tokyo Revengers [1080p]</b>"
    assert job.state == "done" and (await env.db.totals())[0] == 1
    assert "Streaming" in client.edits[0][0]  # the progress message says what is going on
    done, buttons = client.edits[-1]
    assert "Done" in done and human_size(BIG) in done and buttons is None
    assert transfer.stats.streamed == 1 and transfer.stats.stream_fallbacks == 0
    assert not (Path(env.cfg.temp_dir) / "job1").exists()


async def test_the_formats_thumbnail_wins_and_the_original_preview_is_then_not_even_fetched(env):
    client, _job = await rename(env, fmt=await auto(env, thumb=b"\xff\xd8mine"))
    assert client.sent[0][2]["thumb"] == b"\xff\xd8mine" and client.preview_downloads == 0 and len(client.relays) == 1

    fmt = await env.db.create_format(42, "One Piece")
    await env.db.set_format_thumb(42, fmt["id"], b"\xff\xd8format")
    client, _job = await rename(env, fmt=await env.db.get_format(42, fmt["id"]))
    assert client.sent[0][2]["thumb"] == b"\xff\xd8format" and len(client.relays) == 1


async def test_a_format_streams_with_its_own_caption(env):
    await auto(env, caption="<i>THE AUTO FORMAT'S {filename}</i>")  # another format's caption is not borrowed
    fmt = await env.db.create_format(42, "One Piece", template="One Piece - E{episode}")
    await env.db.update_format(42, fmt["id"], caption="<b>{filename}</b> · {size} · {duration}")
    client, _job = await rename(env, new_name="One Piece - E5.mkv", fmt=await env.db.get_format(42, fmt["id"]))
    (_t, _f, kw), = client.sent
    assert len(client.relays) == 1
    assert kw["caption"] == f"<b>One Piece - E5</b> · {human_size(BIG)} · {human_duration(1400)}"  # the duration is the one Telegram has


async def test_a_file_that_gets_no_thumbnail_anyway_is_streamed_without_one(env):
    message = FakeMessage("Archive.zip", BIG, mime="application/zip")
    client, _job = await rename(env, message, new_name="Archive 2.zip")
    (_t, _f, kw), = client.sent
    assert len(client.relays) == 1 and kw["thumb"] is None and client.preview_downloads == 0
    assert kw["force_document"] is True and kw["mime_type"] == "application/zip"
    assert [type(a) for a in kw["attributes"]] == [types.DocumentAttributeFilename]


async def test_an_original_sent_as_a_file_is_streamed_as_a_file(env):
    client, _job = await rename(env, FakeMessage(), fmt=await auto(env, thumb=b"\xff\xd8mine"))  # an .mkv without any video details: Telegram sent it as a file
    (_t, _f, kw), = client.sent
    assert len(client.relays) == 1 and kw["force_document"] is True and kw["thumb"] == b"\xff\xd8mine"
    assert types.DocumentAttributeVideo not in attributes_by_type(kw)


async def test_audio_is_streamed_with_the_title_and_artist_telegram_has(env):
    message = FakeMessage("old name.mp3", BIG, audio=audio_attr(), mime="audio/mpeg")
    client, _job = await rename(env, message, new_name="New Song.mp3")
    (_t, _f, kw), = client.sent
    audio = attributes_by_type(kw)[types.DocumentAttributeAudio]
    assert (audio.duration, audio.title, audio.performer) == (215, "Song", "Me") and not audio.voice
    assert kw["force_document"] is False and kw["mime_type"] == "audio/mpeg" and kw["thumb"] is None and len(client.relays) == 1


async def test_a_forced_type_is_followed_when_telegram_has_the_details_for_it(env):
    await env.db.update_settings(42, file_type="document")
    client, _job = await rename(env)  # sent as a video, but the user wants documents
    (_t, _f, kw), = client.sent
    assert len(client.relays) == 1 and kw["force_document"] is True and types.DocumentAttributeVideo not in attributes_by_type(kw)


async def test_the_caption_may_use_the_duration_when_telegram_has_it(env):
    client, _job = await rename(env, fmt=await auto(env, caption="<b>{filename}</b> · {duration}"))
    assert client.sent[0][2]["caption"] == f"<b>[S01E05] Tokyo Revengers [1080p]</b> · {human_duration(1400)}" and len(client.relays) == 1


async def test_a_streamed_file_goes_to_the_target_channel_like_any_other(env):
    await env.db.update_settings(42, target_channel=-100123)
    client, _job = await rename(env)
    (target, _f, kw), = client.sent
    assert target == -100123 and "reply_to" not in kw and "channel" in client.edits[-1][0]


async def test_a_rejected_caption_is_dropped_for_a_streamed_file_too(env):
    message = FakeMessage(video=video_attr(), thumbs=preview_sizes())
    client = FakeClient(message, env.media_dir / "sample.mkv")
    client.send_errors = [errors.MediaCaptionTooLongError("MEDIA_CAPTION_TOO_LONG", 400)]
    job = Job(id=1, user_id=42, chat_id=42, msg_id=7, file_name=NAME, size=BIG, new_name=NEW_NAME, status_msg=SimpleNamespace(id=99))
    await Renamer(client, env.cfg, env.db).process(job)
    (_t, _f, kw), = client.sent
    assert kw["caption"] is None and "caption was rejected" in client.edits[-1][0] and job.state == "done" and len(client.relays) == 1


@pytest.mark.parametrize("dc_id, shown", [(4, " · DC 4 → DC 1"), (None, "")])
async def test_the_progress_message_says_which_dc_the_file_streams_from_and_to(env, dc_id, shown):
    message = FakeMessage(video=video_attr(), thumbs=preview_sizes(), dc=dc_id)  # downloaded from the DC the file is on, uploaded to the bot's own (DC 1 here)
    client, _job = await rename(env, message)
    assert client.edits[0][0].startswith(f"<b>⬇️⬆️ Streaming</b>{shown}\n")


async def test_the_dc_note_leaves_out_the_bots_dc_when_it_is_not_known(env):
    message = FakeMessage(dc=4)
    renamer = Renamer(SimpleNamespace(session=SimpleNamespace(dc_id=None)), env.cfg, env.db)
    assert renamer._dc_note(message, upload=True) == "DC 4" and renamer._dc_note(FakeMessage(document=False), upload=True) == ""


async def test_the_done_message_gives_the_speed_of_the_whole_stream(env, monkeypatch):
    ticks = iter(range(0, 1000, 2))  # every look at the clock is two seconds later than the one before
    monkeypatch.setattr(pipeline, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    client, _job = await rename(env)
    assert f"{human_size(BIG)} · ⬇⬆ {human_size(BIG / 2)}/s streamed" in client.edits[-1][0]


async def test_a_stream_that_took_under_a_second_shows_no_speed(env):
    client, _job = await rename(env)
    assert "Done" in client.edits[-1][0] and "/s" not in client.edits[-1][0]


# ---------------------------------------------------------------------------
# when the file has to be looked at: the usual way (download, ffmpeg, upload)
# ---------------------------------------------------------------------------


def usual_way(client, job):
    """The file was saved and uploaded the usual way, and sent once."""
    assert client.relays == [] and len(client.downloads) == 1 and len(client.uploads) == 1
    assert len(client.sent) == 1 and job.state == "done"
    assert transfer.stats.streamed == 0 and transfer.stats.stream_fallbacks == 0  # not streaming is not a failure


@needs_ffmpeg
@pytest.mark.parametrize("dc_id, shown", [(5, " · DC 5"), (None, "")])
async def test_the_progress_message_says_which_dc_the_file_downloads_from(env, dc_id, shown):
    message = FakeMessage(size=transfer.BIG_FILE_THRESHOLD, video=video_attr(), thumbs=preview_sizes(), dc=dc_id)
    client, job = await rename(env, message)
    usual_way(client, job)
    stages = [text for text, _buttons in client.edits]
    downloading = [t for t in stages if "Downloading" in t]
    assert downloading and all(t.startswith(f"<b>⬇️ Downloading</b>{shown}\n") for t in downloading)
    uploading = [t for t in stages if "Uploading" in t]
    assert uploading and not any("DC" in t for t in uploading)  # that goes to the bot's own DC: only where the file comes from is shown


@needs_ffmpeg
async def test_small_files_are_not_streamed(env):
    message = FakeMessage(size=transfer.BIG_FILE_THRESHOLD, video=video_attr(), thumbs=preview_sizes())
    client, job = await rename(env, message)
    usual_way(client, job)


@needs_ffmpeg
async def test_metadata_and_tracks_change_the_file_so_it_is_not_streamed(env):
    await env.db.update_settings(42, metadata_on=True, meta={"title": "Tokyo Revengers"})
    client, job = await rename(env)
    usual_way(client, job)


@needs_ffmpeg
async def test_streaming_is_off_unless_it_is_switched_on(env):
    for environ in ({"STREAM_RENAME": "0"}, {"STREAM_RENAME": ""}):
        client, job = await rename(env, cfg=env.new_cfg(**environ))
        usual_way(client, job)
    assert load_config({"API_ID": "1", "API_HASH": "h", "BOT_TOKEN": "1:t", "OWNER_ID": "42"}).stream_transfer is False  # experimental: not by default


@needs_ffmpeg
async def test_a_video_without_a_thumbnail_to_reuse_is_saved_so_that_one_can_be_cut_from_it(env):
    for thumbs in (None, "stripped"):  # a blurred stripped preview is no thumbnail
        message = FakeMessage(video=video_attr(), thumbs=thumbs)
        client, job = await rename(env, message)
        usual_way(client, job)
        assert client.sent[0][2]["thumb"][:2] == b"\xff\xd8" and client.sent[0][2]["thumb"] != PREVIEW  # a frame of the video


@needs_ffmpeg
async def test_a_forced_video_or_audio_type_needs_the_details_only_the_file_can_give(env):
    await env.db.update_settings(42, file_type="video")
    client, job = await rename(env, FakeMessage(thumbs=preview_sizes()))  # an .mkv sent as a file: no video details
    usual_way(client, job)
    assert types.DocumentAttributeVideo in attributes_by_type(client.sent[0][2])  # probed from the file instead

    await env.db.update_settings(42, file_type="audio")
    client, job = await rename(env, FakeMessage(video=video_attr(), thumbs=preview_sizes()))
    usual_way(client, job)


@needs_ffmpeg
async def test_a_caption_that_wants_the_duration_needs_the_file_when_telegram_does_not_know_it(env):
    fmt = await auto(env, thumb=b"\xff\xd8mine", caption="<b>{filename}</b> · {duration}")
    client, job = await rename(env, FakeMessage(), fmt=fmt)  # sent as a file: no duration
    usual_way(client, job)
    duration = (await probe(env.media_dir / "sample.mkv")).duration
    assert client.sent[0][2]["caption"] == f"<b>[S01E05] Tokyo Revengers [1080p]</b> · {human_duration(duration)}"  # read from the file


@needs_ffmpeg
async def test_a_message_without_a_document_is_never_streamed(env):
    client, job = await rename(env, FakeMessage(photo=True, size=BIG))
    usual_way(client, job)


# ---------------------------------------------------------------------------
# when streaming goes wrong: the usual way takes over
# ---------------------------------------------------------------------------


def stream_job(env, failure=None, message=None):
    """A client whose streamed send fails the way ``failure`` says, and the job to run on it."""
    client = FakeClient(message or FakeMessage(video=video_attr(), thumbs=preview_sizes()), env.media_dir / "sample.mkv")
    client.stream_failure = failure
    job = Job(id=1, user_id=42, chat_id=42, msg_id=7, file_name=NAME, size=BIG, new_name=NEW_NAME, status_msg=SimpleNamespace(id=99))
    return client, job


@needs_ffmpeg
async def test_a_failed_stream_falls_back_to_the_usual_way_and_says_so(env, log_channel):
    client, job = stream_job(env, RuntimeError("DC unreachable"))
    await Renamer(client, env.cfg, env.db).process(job)
    await log_channel.flush()

    assert len(client.relays) == 1 and len(client.downloads) == 1 and client.uploads == [(env.media_dir / "sample.mkv").read_bytes()]
    (_t, source, kw), = client.sent
    assert not isinstance(source, Generated) and kw["force_document"] is False and kw["caption"] == "<b>[S01E05] Tokyo Revengers [1080p]</b>"
    assert job.state == "done" and (await env.db.totals())[0] == 1  # counted once
    done = client.edits[-1][0]
    assert "Done" in done and "Streaming didn't work for this file" in done
    assert (transfer.stats.streamed, transfer.stats.stream_fallbacks, transfer.stats.last_stream_fallback) == (0, 1, "RuntimeError: DC unreachable")
    (report,) = log_channel.reports  # the log channel is told, with the traceback
    assert "streaming" in report and "RuntimeError: DC unreachable" in report and "Traceback" in report
    assert not (Path(env.cfg.temp_dir) / "job1").exists()


@needs_ffmpeg
@pytest.mark.parametrize("failure", [TimeoutError("the download has not moved for 120 seconds"), OSError(28, "No space left on device"),
                                     errors.RPCError("FILE_REFERENCE_EXPIRED", 400)])
async def test_whatever_the_download_side_died_of_the_usual_way_takes_over(env, failure):
    client, job = stream_job(env, failure)
    await Renamer(client, env.cfg, env.db).process(job)
    assert len(client.relays) == 1 and len(client.downloads) == 1 and len(client.uploads) == 1 and len(client.sent) == 1
    assert job.state == "done" and "Streaming didn't work" in client.edits[-1][0] and (await env.db.totals())[0] == 1
    assert (transfer.stats.streamed, transfer.stats.stream_fallbacks) == (0, 1) and type(failure).__name__ in transfer.stats.last_stream_fallback


async def test_any_other_refusal_after_streaming_fails_the_job_without_a_second_try(env):
    client, job = stream_job(env)
    client.send_errors = [errors.RPCError("CHAT_WRITE_FORBIDDEN", 403)]
    await Renamer(client, env.cfg, env.db).process(job)
    assert job.state == "failed" and client.sent == [] and client.downloads == [] and len(client.relays) == 1  # no download/upload all over again
    assert "Failed" in client.edits[-1][0] and transfer.stats.stream_fallbacks == 0 and (await env.db.totals())[0] == 0


async def test_cancelling_a_stream_cancels_the_job_and_does_not_fall_back(env):
    client, job = stream_job(env)
    client.during_stream = job.cancel.set  # the person presses Cancel while the file is on its way
    await Renamer(client, env.cfg, env.db).process(job)
    assert job.state == "cancelled" and client.sent == [] and client.downloads == [] and "Cancelled" in client.edits[-1][0]
    assert transfer.stats.stream_fallbacks == 0 and transfer.stats.streamed == 0
    assert not (Path(env.cfg.temp_dir) / "job1").exists()


async def test_a_stream_that_failed_and_a_cancel_together_are_a_cancel(env):
    """The relay failing makes the upload stop, which is not the person's doing: but if they cancelled too, the job is cancelled."""
    client, job = stream_job(env, RuntimeError("DC unreachable"))
    job.cancel.set()
    await Renamer(client, env.cfg, env.db).process(job)
    assert job.state == "cancelled" and client.sent == [] and client.downloads == []


async def test_a_room_check_keeps_a_stream_off_a_disk_that_is_nearly_full(env, monkeypatch):
    monkeypatch.setattr(pipeline.shutil, "disk_usage", lambda _path: SimpleNamespace(total=10 * BIG, used=0, free=BIG))
    client, job = stream_job(env)
    await Renamer(client, env.cfg, env.db).process(job)
    assert client.relays == [] and job.state == "failed"  # streaming is not tried, and the usual way has no room either: it says so
    assert "disk space" in client.edits[-1][0]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def test_source_attributes_ignore_round_videos_and_voice_notes():
    video, _none = source_attributes(FakeMessage(video=video_attr()))
    assert (video.duration, video.w, video.h) == (1400, 1920, 1080) and _none is None
    _none, audio = source_attributes(FakeMessage("a.mp3", BIG, audio=audio_attr(), mime="audio/mpeg"))
    assert (audio.duration, audio.title, audio.performer) == (215, "Song", "Me") and _none is None
    round_video = Message(td_message({"@type": "messageVideoNote", "video_note": {"@type": "videoNote", "duration": 5, "length": 240,
                                                                                    "video": td_file(8, 1000, remote_id="x")}}))
    voice = Message(td_message({"@type": "messageVoiceNote", "caption": formatted(), "voice_note": {
        "@type": "voiceNote", "duration": 5, "waveform": "", "mime_type": "audio/ogg", "voice": td_file(9, 500, remote_id="y")}}))
    assert source_attributes(round_video) == (None, None) and source_attributes(voice) == (None, None)
    assert source_attributes(FakeMessage()) == (None, None)  # an .mkv sent as a file: only its name
    assert source_attributes(FakeMessage(document=False)) == (None, None) and source_attributes(object()) == (None, None)


def test_make_caption_fills_in_what_it_is_given():
    text = make_caption("<b>{filename}</b> · {original} · {size} · {duration} · {ext} · {nonsense}", new_name="New - E5.mkv",
                        orig_name="[Grp] Show - 05 [720p].mkv", ext=".mkv", size=3 * MIB, duration=3725)
    assert text == f"<b>New - E5</b> · [Grp] Show - 05 [720p].mkv · {human_size(3 * MIB)} · {human_duration(3725)} · mkv · {{nonsense}}"
    assert make_caption("{duration}", new_name="a.mkv", orig_name="a.mkv", ext=".mkv", size=1, duration=0) is None  # empty: no caption
    assert len(make_caption("x" * 5000, new_name="a.mkv", orig_name="a.mkv", ext=".mkv", size=1, duration=0)) == 1000


def test_make_caption_pads_the_episode_when_asked_to():
    text = make_caption("Ep {episode} / {1episode} / {2episode} / {3episode}", new_name="x.mkv",
                        orig_name="Show - 7 [720p].mkv", ext=".mkv", size=1, duration=0)
    assert text == "Ep 07 / 07 / 007 / 0007"
    assert make_caption("{3episode} {3episode}", new_name="x.mkv", orig_name="Show - 321 [720p].mkv", ext=".mkv", size=1, duration=0) == "0321 0321"
    assert make_caption("{3episode}", new_name="x.mkv", orig_name="Show - 1321 [720p].mkv", ext=".mkv", size=1, duration=0) == "1321"
    assert make_caption("<b>{2episode}</b>", new_name="x.mkv", orig_name="Plain.mkv", ext=".mkv", size=1, duration=0) == "<b></b>"


async def test_the_embedded_preview_is_only_fetched_when_it_is_worth_having():
    class Client:
        def __init__(self, data=PREVIEW, error=None):
            self.data, self.error, self.calls = data, error, 0

        async def download_media(self, msg, file=None, thumb=None):
            self.calls += 1
            if self.error:
                raise self.error
            return self.data

    client = Client()
    assert await embedded_thumb(client, FakeMessage(thumbs="both")) == PREVIEW
    assert await embedded_thumb(client, FakeMessage(thumbs="stripped")) is None and client.calls == 1  # blurred: not asked for
    assert await embedded_thumb(client, FakeMessage(thumbs="stripped"), allow_stripped=True) == PREVIEW
    assert await embedded_thumb(client, FakeMessage(thumbs=None)) is None and await embedded_thumb(client, object()) is None
    assert await embedded_thumb(Client(error=OSError("gone")), FakeMessage(thumbs="both")) is None
    assert await embedded_thumb(Client(data=b""), FakeMessage(thumbs="both")) is None
