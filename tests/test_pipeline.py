import asyncio
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot import errorlog, media
from bot.config import load_config
from bot.db import Database
from bot.errors import MediaError
from bot.flood import FloodControl
from bot.media import probe
from bot.parser import DEFAULT_TEMPLATE, compute_new_name
from bot.pipeline import Renamer, decide_send_type, guess_mime, has_thumbnail, original_name_of, source_ext
from bot.queue import Job
from bot.tg import Message, errors, types

from .appkit import ensure_auto
from .conftest import needs_ffmpeg
from .tdfakes import document_content, td_message, thumbnail_of
from .timeline import Timeline


class FakeMessage:
    def __init__(self, name, size, *, video=True, audio=False, mime="video/x-matroska", msg_id=7, ext=None):
        self.id = msg_id
        self.file = SimpleNamespace(name=name, size=size, ext=ext if ext is not None else Path(name or "").suffix, mime_type=mime)
        self.video = object() if video else None
        self.audio = object() if audio else None
        self.document = object()


class FakeClient:
    def __init__(self, source: Path, message: FakeMessage):
        self.source = source
        self.message = message
        self.sent = []
        self.edits = []
        self.uploads = {}
        self.download_error = None
        self.send_errors = []

    async def get_messages(self, chat_id, ids):
        return self.message

    async def download_to(self, message, dest, *, progress=None, cancel=None, max_bytes=None):
        progress(1, 2)
        if self.download_error:
            raise self.download_error
        shutil.copy(self.source, dest)
        progress(2, 2)
        return Path(dest).stat().st_size

    async def send_file(self, target, source, **kwargs):
        """Keeps a copy of what was sent (the work folder is removed afterwards) and answers with the message that was sent."""
        if self.send_errors:
            raise self.send_errors.pop(0)
        kwargs["progress"](1, 1)
        keep = Path(self.source).parent / ("upload-" + str(len(self.uploads)) + Path(source).suffix)
        shutil.copy(source, keep)
        self.uploads[len(self.uploads)] = keep
        self.sent.append((target, source, kwargs))
        return SimpleNamespace(id=3000 + len(self.sent))

    async def edit_message(self, message, text, buttons=None, parse_mode=None, **_):
        self.edits.append((text, buttons))


@pytest.fixture
async def env(tmp_path, media_dir):
    cfg = load_config({
        "API_ID": "1", "API_HASH": "h", "BOT_TOKEN": "1:t", "OWNER_ID": "42",
        "TEMP_DIR": str(tmp_path / "work"), "PROGRESS_INTERVAL": "2",
    })
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/p.db")
    await db.init()
    ns = SimpleNamespace(cfg=cfg, db=db, media_dir=media_dir, tmp=tmp_path)
    yield ns
    await db.close()


def make(env, name="[SubsPlease] Tokyo Revengers - 05 (1080p) [ABCD1234].mkv", src="sample.mkv", **msg_kwargs):
    source = env.media_dir / src
    message = FakeMessage(name, source.stat().st_size, **msg_kwargs)
    client = FakeClient(source, message)
    renamer = Renamer(client, env.cfg, env.db)
    new_name, _ = compute_new_name(template=DEFAULT_TEMPLATE, mode="filename", original_name=name, original_ext=Path(name).suffix)
    job = Job(id=1, user_id=42, chat_id=42, msg_id=7, file_name=name, size=source.stat().st_size, new_name=new_name,
              status_msg=SimpleNamespace(id=99))
    return client, renamer, job


@needs_ffmpeg
async def test_plain_rename_sends_video_with_thumbnail(env):
    client, renamer, job = make(env)
    await renamer.process(job)

    assert job.new_name == "[S01E05] Tokyo Revengers [1080p].mkv"
    (target, input_file, kw), = client.sent
    assert target == 42 and kw["reply_to"] == 7
    by_type = {type(a): a for a in kw["attributes"]}
    assert by_type[types.DocumentAttributeFilename].file_name == job.new_name
    video = by_type[types.DocumentAttributeVideo]
    assert (video.w, video.h) == (320, 240) and 1 < video.duration < 3 and video.supports_streaming
    assert kw["force_document"] is False and kw["mime_type"] == "video/x-matroska"
    assert kw["thumb"][:2] == b"\xff\xd8"  # generated from a video frame
    assert kw["caption"] == "<b>[S01E05] Tokyo Revengers [1080p]</b>" and kw["parse_mode"] == "html"  # bold, by default
    assert Path(input_file).parent == Path(env.cfg.temp_dir) / "job1"  # what is sent is the file in the job's own folder
    assert client.uploads[0].read_bytes() == (env.media_dir / "sample.mkv").read_bytes()  # untouched without metadata
    assert "Done" in client.edits[-1][0] and client.edits[-1][1] is None
    assert job.state == "done"
    assert (await env.db.totals())[0] == 1
    assert not (Path(env.cfg.temp_dir) / "job1").exists()  # temp files cleaned


@needs_ffmpeg
async def test_notes_from_queueing_appear_in_the_done_message(env):
    client, renamer, job = make(env)
    job.notes = ["Couldn't find {episode} in the file name, so it is left empty."]
    await renamer.process(job)
    assert job.state == "done"
    assert "⚠️ Couldn't find {episode} in the file name" in client.edits[-1][0]


@needs_ffmpeg
async def test_metadata_and_track_selection_are_applied_before_upload(env):
    await env.db.update_settings(
        42, metadata_on=True, keep_audio=["english", "japanese"], keep_subtitle=["2"],
        meta={"title": "Tokyo Revengers", "audio": "{lang}", "subtitle": "English"},
    )
    client, renamer, job = make(env)
    await renamer.process(job)
    out = await probe(client.uploads[0])
    assert [s.lang for s in out.audio] == ["eng", "jpn"]
    assert [s.lang for s in out.subtitles] == ["eng"]
    assert out.tags.get("title") == "Tokyo Revengers"
    assert [s.title for s in out.audio] == ["English", "Japanese"]
    assert client.sent and job.state == "done"


@needs_ffmpeg
async def test_document_file_type_is_forced(env):
    await env.db.update_settings(42, file_type="document")
    client, renamer, job = make(env)
    await renamer.process(job)
    (_t, _f, kw), = client.sent
    assert kw["force_document"] is True
    assert types.DocumentAttributeVideo not in {type(a) for a in kw["attributes"]}


@needs_ffmpeg
async def test_same_type_keeps_document_as_document(env):
    client, renamer, job = make(env, video=False)  # original was sent as a file
    await renamer.process(job)
    (_t, _f, kw), = client.sent
    assert kw["force_document"] is True
    assert types.DocumentAttributeVideo not in {type(a) for a in kw["attributes"]}


@needs_ffmpeg
async def test_forced_video_for_non_video_falls_back_to_document(env, tmp_path):
    blob = tmp_path / "data.zip"
    blob.write_bytes(b"PK" + b"0" * 5000)
    message = FakeMessage("stuff.zip", 5002, video=False, mime="application/zip")
    client = FakeClient(blob, message)
    renamer = Renamer(client, env.cfg, env.db)
    await env.db.update_settings(42, file_type="video")
    job = Job(id=2, user_id=42, chat_id=42, msg_id=7, file_name="stuff.zip", size=5002, new_name="Stuff.zip", status_msg=object())
    await renamer.process(job)
    (_t, _f, kw), = client.sent
    assert kw["force_document"] is True and kw["mime_type"] == "application/zip"
    assert kw["thumb"] is None


async def make_auto(env, thumb=None, **changes):
    """The user's auto format (made on demand), with these changes and this thumbnail."""
    fmt = await ensure_auto(env.db, 42)
    if changes:
        await env.db.update_format(42, fmt["id"], **changes)
    if thumb:
        await env.db.set_format_thumb(42, fmt["id"], thumb)
    return await env.db.get_format(42, fmt["id"])


@needs_ffmpeg
async def test_custom_thumbnail_wins_over_generated(env):
    client, renamer, job = make(env)
    job.fmt = await make_auto(env, thumb=b"\xff\xd8custom")
    await renamer.process(job)
    assert client.sent[0][2]["thumb"] == b"\xff\xd8custom"


@needs_ffmpeg
async def test_target_channel(env):
    await env.db.update_settings(42, target_channel=-100123)
    client, renamer, job = make(env)
    await renamer.process(job)
    (target, _f, kw), = client.sent
    assert target == -100123 and "reply_to" not in kw
    assert "channel" in client.edits[-1][0]


@needs_ffmpeg
async def test_audio_file_sent_as_audio_with_tags(env):
    await env.db.update_settings(42, metadata_on=True, meta={"title": "Song", "artist": "Me"})
    client, renamer, job = make(env, name="old name.mp3", src="sample.mp3", video=False, audio=True, mime="audio/mpeg")
    job.new_name = "New Song.mp3"
    await renamer.process(job)
    (_t, _f, kw), = client.sent
    audio = next(a for a in kw["attributes"] if isinstance(a, types.DocumentAttributeAudio))
    assert audio.title == "Song" and audio.performer == "Me" and audio.duration >= 1
    assert kw["force_document"] is False and kw["mime_type"] == "audio/mpeg"
    assert (await probe(client.uploads[0])).tags["title"] == "Song"


@needs_ffmpeg
async def test_unsupported_container_with_metadata_warns_but_still_renames(env, tmp_path):
    # an .avi has video streams but is outside the supported remux containers
    avi = tmp_path / "x.avi"
    import subprocess
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=1:size=64x64:rate=5",
                    "-c:v", "mpeg4", str(avi)], check=True)
    message = FakeMessage("x.avi", avi.stat().st_size, video=False, mime="video/x-msvideo")
    client = FakeClient(avi, message)
    await env.db.update_settings(42, metadata_on=True, meta={"title": "T"})
    job = Job(id=3, user_id=42, chat_id=42, msg_id=7, file_name="x.avi", size=avi.stat().st_size, new_name="Y.avi", status_msg=object())
    await Renamer(client, env.cfg, env.db).process(job)
    assert client.sent and "not supported" in client.edits[-1][0]


@needs_ffmpeg
async def test_caption_rejected_is_retried_without_caption(env):
    client, renamer, job = make(env)
    client.send_errors = [errors.MediaCaptionTooLongError("MEDIA_CAPTION_TOO_LONG", 400)]
    await renamer.process(job)
    (_t, _f, kw), = client.sent
    assert kw["caption"] is None
    assert "caption was rejected" in client.edits[-1][0] and job.state == "done"


@needs_ffmpeg
async def test_cancel_before_start(env):
    client, renamer, job = make(env)
    job.cancel.set()
    await renamer.process(job)
    assert job.state == "cancelled" and client.sent == []
    assert "Cancelled" in client.edits[-1][0]
    assert not (Path(env.cfg.temp_dir) / "job1").exists()


@pytest.fixture
async def log_channel():
    """What reaches the log channel while a test runs (the error reporter is attached to the root logger, as in main.py)."""
    reports = []

    async def send(text):
        reports.append(text)

    handler = errorlog.install(send)
    yield SimpleNamespace(reports=reports, flush=handler.aclose)
    handler.detach()


@needs_ffmpeg
async def test_failure_is_reported_and_logged(env, log_channel):
    client, renamer, job = make(env)
    client.download_error = RuntimeError("network down")
    await renamer.process(job)
    await log_channel.flush()
    assert job.state == "failed" and client.sent == []
    assert "Failed" in client.edits[-1][0] and "network down" in client.edits[-1][0]
    (report,) = log_channel.reports  # the log channel gets the whole traceback, as a Python code block
    assert report.startswith(f'<pre><code class="language-python">Error processing file:\nuser 42 · job #1\n{job.file_name} → {job.new_name} (')
    assert "Traceback (most recent call last):" in report and report.endswith("RuntimeError: network down</code></pre>")
    assert not (Path(env.cfg.temp_dir) / "job1").exists()


@needs_ffmpeg
async def test_a_finished_job_leaves_nothing_in_the_log_channel(env, log_channel):
    client, renamer, job = make(env)
    await renamer.process(job)
    await log_channel.flush()
    assert job.state == "done" and log_channel.reports == []


@needs_ffmpeg
async def test_not_enough_disk_space(env, monkeypatch):
    client, renamer, job = make(env)
    monkeypatch.setattr(shutil, "disk_usage", lambda p: SimpleNamespace(total=100, used=100, free=10))
    await renamer.process(job)
    assert job.state == "failed" and "disk space" in client.edits[-1][0]


@needs_ffmpeg
async def test_deleted_original_message_fails_cleanly(env):
    client, renamer, job = make(env)

    async def gone(chat_id, ids):
        return None

    client.get_messages = gone
    await renamer.process(job)
    assert job.state == "failed" and "deleted" in client.edits[-1][0]


def test_helpers():
    video = FakeMessage("a.mkv", 1)
    doc = FakeMessage("a.mkv", 1, video=False)
    audio = FakeMessage("a.mp3", 1, video=False, audio=True)
    assert decide_send_type("same", video) == "video"
    assert decide_send_type("same", doc) == "document"
    assert decide_send_type("same", audio) == "audio"
    assert decide_send_type("document", video) == "document"
    assert decide_send_type("audio", doc) == "audio"
    assert guess_mime("x.mkv", None) == "video/x-matroska"
    assert guess_mime("x.unknown123", "foo/bar") == "foo/bar"
    assert guess_mime("x.unknown123", None) == "application/octet-stream"
    nameless = FakeMessage(None, 1, ext=".mp4", msg_id=55)
    assert original_name_of(nameless) == "video_55.mp4"
    assert source_ext(nameless, "video_55.mp4") == ".mp4"
    assert source_ext(FakeMessage("noext", 1, ext=".mkv"), "noext") == ".mkv"


# ---------------------------------------------------------------------------
# saved /rename formats
# ---------------------------------------------------------------------------


async def make_fmt(env, name="One Piece", **changes):
    fmt = await env.db.create_format(42, name, template="One Piece - E{episode}")
    if changes:
        await env.db.update_format(42, fmt["id"], **changes)
    return await env.db.get_format(42, fmt["id"])


@needs_ffmpeg
async def test_a_format_brings_its_own_metadata_tracks_and_thumbnail(env):
    # the default metadata is off and has a different title: a manual format must not depend on either
    await env.db.update_settings(42, metadata_on=False, meta={"title": "DEFAULT"}, keep_audio=["hindi"])
    await make_auto(env, thumb=b"\xff\xd8auto")  # nor on the auto format
    fmt = await make_fmt(env, meta={"title": "One Piece", "audio": "{lang}"}, keep_audio=["english", "japanese"], keep_subtitle=["2"])
    await env.db.set_format_thumb(42, fmt["id"], b"\xff\xd8format")
    fmt = await env.db.get_format(42, fmt["id"])

    client, renamer, job = make(env)
    job.fmt = fmt
    await renamer.process(job)

    out = await probe(client.uploads[0])
    assert [s.lang for s in out.audio] == ["eng", "jpn"] and [s.lang for s in out.subtitles] == ["eng"]
    assert out.tags.get("title") == "One Piece" and [s.title for s in out.audio] == ["English", "Japanese"]
    (_t, _f, kw), = client.sent
    assert kw["thumb"] == b"\xff\xd8format"
    assert job.state == "done"


@needs_ffmpeg
async def test_a_format_without_a_thumbnail_gets_a_frame_of_the_video(env):
    await make_auto(env, thumb=b"\xff\xd8auto")  # another format's thumbnail is not borrowed
    fmt = await make_fmt(env)
    client, renamer, job = make(env)
    job.fmt = fmt
    await renamer.process(job)
    (_t, _f, kw), = client.sent
    assert kw["thumb"][:2] == b"\xff\xd8" and kw["thumb"] != b"\xff\xd8auto"  # cut out of the video


@needs_ffmpeg
async def test_a_manual_format_that_sets_no_metadata_leaves_the_file_untouched_even_if_default_metadata_is_on(env):
    await env.db.update_settings(42, metadata_on=True, meta={"title": "DEFAULT"}, keep_audio=["english"])
    fmt = await make_fmt(env)
    client, renamer, job = make(env)
    job.fmt = fmt
    await renamer.process(job)
    assert client.uploads[0].read_bytes() == (env.media_dir / "sample.mkv").read_bytes()  # no ffmpeg pass
    assert job.state == "done"


@needs_ffmpeg
async def test_an_auto_format_without_metadata_uses_the_default_metadata_while_it_is_on(env):
    await env.db.update_settings(42, metadata_on=True, meta={"title": "DEFAULT TITLE"})
    client, renamer, job = make(env)
    job.fmt = await make_auto(env)  # sets no metadata of its own
    await renamer.process(job)
    assert (await probe(client.uploads[0])).tags.get("title") == "DEFAULT TITLE"

    await env.db.update_settings(42, metadata_on=False)  # switched off: the file is left alone
    client, renamer, job = make(env)
    job.fmt = await make_auto(env)
    await renamer.process(job)
    assert client.uploads[0].read_bytes() == (env.media_dir / "sample.mkv").read_bytes()


@needs_ffmpeg
async def test_an_auto_format_with_metadata_of_its_own_ignores_the_default_metadata(env):
    await env.db.update_settings(42, metadata_on=True, meta={"title": "DEFAULT TITLE"})
    client, renamer, job = make(env)
    job.fmt = await make_auto(env, meta={"title": "OWN TITLE"})
    await renamer.process(job)
    assert (await probe(client.uploads[0])).tags.get("title") == "OWN TITLE"

    await env.db.update_settings(42, metadata_on=False)  # its own metadata applies whatever the switch says
    client, renamer, job = make(env)
    job.fmt = await make_auto(env)
    await renamer.process(job)
    assert (await probe(client.uploads[0])).tags.get("title") == "OWN TITLE"


@needs_ffmpeg
async def test_everything_else_still_comes_from_the_users_settings(env):
    await env.db.update_settings(42, file_type="document", target_channel=-100777)
    fmt = await make_fmt(env, meta={"title": "T"})
    client, renamer, job = make(env)
    job.fmt = fmt
    job.new_name = "One Piece - E5.mkv"
    await renamer.process(job)
    (target, _f, kw), = client.sent
    assert target == -100777 and kw["force_document"] is True  # the file type and the channel are still the user's
    assert kw["caption"] == "<b>One Piece - E5</b>"  # the caption is the format's: it has none, so it is the new name in bold


@needs_ffmpeg
async def test_a_format_sends_its_own_caption(env):
    from bot.utils import human_size

    await make_auto(env, caption="<i>THE AUTO FORMAT'S CAPTION {filename}</i>")  # another format's caption is not borrowed
    fmt = await make_fmt(env, caption="<b>{filename}</b> · {original} · {size}")
    client, renamer, job = make(env)
    job.fmt = fmt
    job.new_name = "One Piece - E5.mkv"
    await renamer.process(job)
    (_t, _f, kw), = client.sent
    size = (env.media_dir / "sample.mkv").stat().st_size
    assert kw["caption"] == f"<b>One Piece - E5</b> · {job.file_name} · {human_size(size)}" and kw["parse_mode"] == "html"


@needs_ffmpeg
async def test_a_format_without_a_caption_sends_the_new_name_in_bold(env):
    await make_auto(env, caption="<i>THE AUTO FORMAT'S CAPTION {filename}</i>")
    fmt = await make_fmt(env)
    client, renamer, job = make(env)
    job.fmt = fmt
    job.new_name = "One Piece - E5.mkv"
    await renamer.process(job)
    assert client.sent[0][2]["caption"] == "<b>One Piece - E5</b>"


@needs_ffmpeg
async def test_an_auto_rename_uses_the_caption_of_its_auto_format(env):
    await env.db.create_format(42, "Unused", template="X")  # a manual format changes nothing for the auto renames
    client, renamer, job = make(env)
    job.fmt = await make_auto(env, caption="<i>{filename}</i> ({quality})")
    await renamer.process(job)
    assert client.sent[0][2]["caption"] == "<i>[S01E05] Tokyo Revengers [1080p]</i> (1080p)"


@needs_ffmpeg
async def test_an_auto_rename_without_any_format_uses_the_built_in_defaults(env):
    client, renamer, job = make(env)
    assert job.fmt is None
    await renamer.process(job)
    (_t, _f, kw), = client.sent
    assert kw["caption"] == "<b>[S01E05] Tokyo Revengers [1080p]</b>"  # the new name in bold
    assert kw["thumb"][:2] == b"\xff\xd8"  # a frame of the video
    assert client.uploads[0].read_bytes() == (env.media_dir / "sample.mkv").read_bytes()
    assert await env.db.list_formats(42) == []  # nothing was made for it


@needs_ffmpeg
async def test_a_format_deleted_while_queued_still_finishes_with_its_snapshot(env):
    fmt = await make_fmt(env, meta={"title": "One Piece"})
    await env.db.set_format_thumb(42, fmt["id"], b"\xff\xd8gone")
    await env.db.delete_format(42, fmt["id"])
    client, renamer, job = make(env)
    job.fmt = fmt  # the snapshot taken when the file was queued
    await renamer.process(job)
    (_t, _f, kw), = client.sent
    assert kw["thumb"][:2] == b"\xff\xd8" and kw["thumb"] != b"\xff\xd8gone" and job.state == "done"  # its thumbnail went with it
    out = await probe(client.uploads[0])
    assert out.tags.get("title") == "One Piece"  # the snapshot's metadata is still applied


@needs_ffmpeg
async def test_the_done_message_says_how_fast_each_way_went(env, monkeypatch):
    from bot import pipeline
    from bot.utils import human_size

    ticks = iter(range(0, 1000, 2))  # every look at the clock is two seconds later than the one before
    monkeypatch.setattr(pipeline, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    client, renamer, job = make(env)
    await renamer.process(job)
    size = (env.media_dir / "sample.mkv").stat().st_size
    done = client.edits[-1][0]
    assert f"{human_size(size)} · ⬇ {human_size(size / 2)}/s · ⬆ {human_size(size / 2)}/s" in done


@needs_ffmpeg
async def test_a_transfer_that_took_under_a_second_shows_no_speed(env):
    client, renamer, job = make(env)
    await renamer.process(job)
    assert "Done" in client.edits[-1][0] and "/s" not in client.edits[-1][0]


# ---------------------------------------------------------------------------
# what comes after a file is in its chat
# ---------------------------------------------------------------------------


def hooks(renamer, heard):
    async def renamed(job, chat, sent):
        heard.append(("renamed", job.id, chat, sent.id))

    async def delivered(job, chat, sent):
        heard.append(("delivered", job.id, chat, sent.id))

    renamer.on_renamed, renamer.on_delivered = renamed, delivered


@needs_ffmpeg
async def test_a_file_renamed_with_the_auto_settings_is_handed_to_on_renamed_only(env):
    heard = []
    client, renamer, job = make(env)  # no format at all: the auto settings
    hooks(renamer, heard)
    await renamer.process(job)
    assert heard == [("renamed", 1, 42, 3001)] and job.state == "done"

    client, renamer, job = make(env)
    job.fmt = await make_auto(env)  # an auto format is the auto settings too
    heard.clear()
    hooks(renamer, heard)
    await renamer.process(job)
    assert heard == [("renamed", 1, 42, 3001)]


@needs_ffmpeg
async def test_a_file_renamed_with_a_manual_format_is_handed_to_on_delivered_only(env):
    heard = []
    client, renamer, job = make(env)
    job.fmt = await make_fmt(env)
    hooks(renamer, heard)
    await renamer.process(job)
    assert heard == [("delivered", 1, 42, 3001)] and job.state == "done"


@needs_ffmpeg
async def test_a_file_that_failed_is_not_handed_to_the_hooks(env):
    heard = []
    client, renamer, job = make(env)
    client.send_errors = [RuntimeError("Telegram said no")]
    hooks(renamer, heard)
    await renamer.process(job)
    assert heard == [] and job.state == "failed"


@needs_ffmpeg
async def test_a_hook_that_breaks_does_not_fail_the_rename(env):
    client, renamer, job = make(env)

    async def broken(job, chat, sent):
        raise RuntimeError("the hook is broken")

    renamer.on_renamed = broken
    await renamer.process(job)
    assert job.state == "done" and "Done" in client.edits[-1][0]


# ---------------------------------------------------------------------------
# a file that failed only because Telegram was busy is tried again
# ---------------------------------------------------------------------------

STOPPED_SHORT = f"{errors.DOWNLOAD_STOPPED} (stopped short 8 times)"


def heard_results(renamer):
    heard = []

    async def result(job, outcome):
        heard.append((job.id, outcome.state, outcome))

    renamer.on_result = result
    return heard


@needs_ffmpeg
async def test_a_file_that_failed_because_telegram_was_busy_is_given_back_with_a_pause(env):
    client, renamer, job = make(env)
    heard = heard_results(renamer)
    client.download_error = errors.RPCError(STOPPED_SHORT, 400)
    await renamer.process(job)
    assert job.state == "queued" and job.attempts == 1 and 55 < job.retry_at - time.time() <= 60
    text, buttons = client.edits[-1]
    assert "Telegram was busy" in text and "Trying again in 1:00 (try 2 of 3)" in text and "Failed" not in text
    assert buttons[0][0].type.data == b"job:x:1"  # it can still be cancelled while it waits
    assert heard == [] and client.sent == []  # it is not over
    assert not (Path(env.cfg.temp_dir) / "job1").exists()


@needs_ffmpeg
async def test_a_flood_wait_that_was_too_long_to_wait_out_is_given_back_too(env):
    client, renamer, job = make(env)
    client.send_errors = [errors.FloodWaitError(900)]
    await renamer.process(job)
    assert job.state == "queued" and job.attempts == 1


@needs_ffmpeg
async def test_the_second_new_try_waits_longer_and_the_third_failure_is_final(env):
    client, renamer, job = make(env)
    heard = heard_results(renamer)
    client.download_error = errors.RPCError(STOPPED_SHORT, 400)
    await renamer.process(job)
    job.state = "running"  # (the queue starts it again)
    await renamer.process(job)
    assert job.state == "queued" and job.attempts == 2 and 175 < job.retry_at - time.time() <= 180
    assert "Trying again in 3:00 (try 3 of 3)" in client.edits[-1][0]
    job.state = "running"
    await renamer.process(job)
    assert job.state == "failed" and job.attempts == 2
    text, buttons = client.edits[-1]
    assert "Failed" in text and "File download has failed" in text and "Tried 3 times." in text and buttons is None
    (job_id, state, outcome), = heard
    assert (job_id, state, outcome.retries) == (1, "failed", 2) and "File download has failed" in outcome.reason


@needs_ffmpeg
async def test_a_file_that_succeeds_after_a_new_try_is_a_success_with_its_retries_counted(env):
    client, renamer, job = make(env)
    heard = heard_results(renamer)
    client.download_error = errors.RPCError(STOPPED_SHORT, 400)
    await renamer.process(job)
    client.download_error = None
    job.state = "running"
    await renamer.process(job)
    assert job.state == "done" and "Done" in client.edits[-1][0]
    (_id, state, outcome), = heard
    assert state == "done" and outcome.retries == 1 and "Tried" not in client.edits[-1][0]


@needs_ffmpeg
async def test_other_failures_are_final_at_once(env):
    client, renamer, job = make(env)
    heard = heard_results(renamer)
    client.download_error = errors.RPCError("FILE_REFERENCE_EXPIRED", 400)
    await renamer.process(job)
    assert job.state == "failed" and job.attempts == 0 and "Tried" not in client.edits[-1][0]
    assert [state for _id, state, _o in heard] == ["failed"]


@needs_ffmpeg
async def test_a_job_the_user_cancelled_is_not_given_back(env):
    client, renamer, job = make(env)

    async def cancel_then_fail(*args, **kwargs):
        job.cancel.set()
        raise errors.RPCError(STOPPED_SHORT, 400)

    client.download_to = cancel_then_fail
    await renamer.process(job)
    assert job.state == "failed" and job.attempts == 0


@needs_ffmpeg
async def test_a_cancelled_job_is_reported_as_cancelled_to_on_result(env):
    client, renamer, job = make(env)
    heard = heard_results(renamer)
    job.cancel.set()
    await renamer.process(job)
    assert [(state, outcome.reason) for _id, state, outcome in heard] == [("cancelled", "")]


@needs_ffmpeg
async def test_a_finished_file_tells_on_result_what_was_checked_and_warned_about(env):
    client, renamer, job = make(env)
    heard = heard_results(renamer)
    job.notes = ["Couldn't find {episode} in the file name, so it is left empty."]
    await renamer.process(job)
    (_id, state, outcome), = heard
    assert state == "done" and outcome.warnings == job.notes and outcome.retries == 0 and not outcome.thumb_resent


@needs_ffmpeg
async def test_a_hook_that_breaks_when_told_the_result_does_not_fail_the_rename(env):
    client, renamer, job = make(env)

    async def broken(job, outcome):
        raise RuntimeError("the hook is broken")

    renamer.on_result = broken
    await renamer.process(job)
    assert job.state == "done"


# ---------------------------------------------------------------------------
# only so many transfers at once
# ---------------------------------------------------------------------------


class BusyClient(FakeClient):
    """Counts the transfers that run at the same moment (each one takes a little while)."""

    def __init__(self, source, message):
        super().__init__(source, message)
        self.now = {"down": 0, "up": 0}
        self.peak = {"down": 0, "up": 0}
        self.gates = {"down": None, "up": None}  # while a gate is not set the transfers of that kind do not finish
        self.pause = 0.03  # how long a transfer takes
        self.uploading = []  # the names of the files whose upload began, in that order

    async def _hold(self, kind):
        self.now[kind] += 1
        self.peak[kind] = max(self.peak[kind], self.now[kind])
        try:
            await asyncio.sleep(self.pause)
            if self.gates[kind] is not None:
                await self.gates[kind].wait()
        finally:
            self.now[kind] -= 1

    async def download_to(self, message, dest, **kwargs):
        await self._hold("down")
        return await super().download_to(message, dest, **kwargs)

    async def send_file(self, target, source, **kwargs):
        self.uploading.append(next(a.file_name for a in kwargs["attributes"] if isinstance(a, types.DocumentAttributeFilename)))
        await self._hold("up")
        return await super().send_file(target, source, **kwargs)


async def until(condition, seconds=20.0):
    """Wait (at most ``seconds``) until ``condition()`` is true."""
    end = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < end, "the state the test waits for was never reached"
        await asyncio.sleep(0.01)


def several(env, count, flood, client_class=BusyClient):
    client, renamer, first = make(env)
    busy = client_class(client.source, client.message)
    busy.flood = flood
    renamer = Renamer(busy, env.cfg, env.db)
    jobs = []
    for number in range(1, count + 1):
        jobs.append(Job(id=number, user_id=42, chat_id=42, msg_id=7, file_name=first.file_name, size=first.size,
                        new_name=f"[S01E0{number}] Tokyo Revengers [1080p].mkv", status_msg=SimpleNamespace(id=90 + number, chat_id=42)))
    return busy, renamer, jobs


@needs_ffmpeg
@pytest.mark.parametrize("uploads, downloads", [(1, 2), (1, 1), (2, 3)])
async def test_however_many_jobs_run_only_so_many_transfers_do(env, uploads, downloads):
    flood = FloodControl(uploads=uploads, downloads=downloads)
    client, renamer, jobs = several(env, 5, flood)
    client.gates = {"down": asyncio.Event(), "up": asyncio.Event()}  # every transfer lasts until its gate is opened
    tasks = [asyncio.ensure_future(renamer.process(job)) for job in jobs]
    await until(lambda: client.now["down"] == downloads and flood.downloads.waiting == 5 - downloads)  # as many as may, the rest in line
    client.gates["down"].set()
    await until(lambda: client.now["up"] == uploads and flood.uploads.waiting >= 1)
    client.gates["up"].set()
    await asyncio.wait_for(asyncio.gather(*tasks), 60)
    assert [job.state for job in jobs] == ["done"] * 5
    assert client.peak == {"down": downloads, "up": uploads}
    assert flood.uploads.used == flood.downloads.used == 0  # every place was given back


@needs_ffmpeg
async def test_a_job_that_has_to_wait_for_a_turn_says_so(env):
    flood = FloodControl(uploads=1, downloads=1)
    client, renamer, jobs = several(env, 3, flood)
    client.gates = {"down": asyncio.Event(), "up": asyncio.Event()}
    tasks = [asyncio.ensure_future(renamer.process(job)) for job in jobs]
    await until(lambda: flood.downloads.waiting == 2)  # one is downloading, the other two wait for it
    client.gates["down"].set()
    await until(lambda: flood.uploads.used == 1 and flood.uploads.waiting >= 1)
    client.gates["up"].set()
    await asyncio.wait_for(asyncio.gather(*tasks), 60)
    waiting = [text for text, _b in client.edits if "Waiting for a turn" in text]
    assert waiting and any("downloaded" in text for text in waiting) and any("uploaded" in text for text in waiting)
    assert all(job.state == "done" for job in jobs)


@needs_ffmpeg
async def test_a_job_that_waits_for_a_flood_to_be_over_says_so_and_starts_afterwards(env):
    line = Timeline()
    flood = FloodControl(clock=line.clock, sleep=line.sleep, jitter=lambda: 0.0)
    client, renamer, (job,) = several(env, 1, flood, FakeClient)
    flood.hit(3)
    await renamer.process(job)
    assert job.state == "done" and line.waited() == [3.5]
    assert any("Telegram asked the bot to slow down" in text and "Starting again in a moment." in text for text, _b in client.edits)


@needs_ffmpeg
async def test_cancelling_a_job_that_waits_for_its_turn_takes_it_out_of_the_line(env):
    flood = FloodControl(uploads=1, downloads=2)
    client, renamer, jobs = several(env, 2, flood)
    client.gates["up"] = asyncio.Event()  # the upload that has its turn does not finish until this is set
    tasks = {job.id: asyncio.ensure_future(renamer.process(job)) for job in jobs}
    await until(lambda: flood.uploads.waiting and client.now["up"])
    assert flood.uploads.waiting == 1 and len(client.uploading) == 1  # one is uploading, the other waits for its turn (which one is chance)
    holder = next(job for job in jobs if job.new_name == client.uploading[0])
    waiter = next(job for job in jobs if job is not holder)
    waiter.cancel.set()
    await asyncio.wait_for(tasks[waiter.id], 10)
    assert waiter.state == "cancelled" and flood.uploads.waiting == 0 and flood.uploads.used == 1
    client.gates["up"].set()
    await asyncio.wait_for(tasks[holder.id], 10)
    assert holder.state == "done" and flood.uploads.used == 0 and len(client.sent) == 1
    assert any("Cancelled" in text for text, _b in client.edits)


@needs_ffmpeg
async def test_without_a_flood_control_nothing_waits_and_nothing_is_said_about_it(env):
    client, renamer, jobs = several(env, 3, None)
    del client.flood
    client.gates["up"] = asyncio.Event()
    tasks = [asyncio.ensure_future(renamer.process(job)) for job in jobs]
    await until(lambda: client.now["up"] == 3)  # nothing holds them back: all three upload at the same time
    client.gates["up"].set()
    await asyncio.wait_for(asyncio.gather(*tasks), 60)
    assert [job.state for job in jobs] == ["done"] * 3
    assert client.peak["up"] == 3 and not any("Waiting" in text for text, _b in client.edits)


# ---------------------------------------------------------------------------
# the thumbnail: did Telegram keep it?
# ---------------------------------------------------------------------------


class RealClient(FakeClient):
    """Answers with ``Message`` objects, as Telegram does: ``thumbs[n]`` says whether the n-th file sent has its thumbnail."""

    def __init__(self, source, message, thumbs=()):
        super().__init__(source, message)
        self.thumbs = list(thumbs)
        self.deleted = []
        self.delete_error = None
        self.sizes = {}  # what to tell about a file: {"size": ..., "name": ...}

    async def send_file(self, target, source, **kwargs):
        reply = await super().send_file(target, source, **kwargs)
        number = len(self.sent)
        kept = self.thumbs[number - 1] if number <= len(self.thumbs) else True
        name = self.sizes.get("name") or next(a.file_name for a in kwargs["attributes"] if isinstance(a, types.DocumentAttributeFilename))
        size = self.sizes.get("size") or Path(source).stat().st_size
        content = document_content(name, size, file_id=100 + number, thumbnail=thumbnail_of(file_id=200 + number) if kept else None)
        return Message(td_message(content, chat_id=target, plain_id=reply.id, out=True))

    async def delete_messages(self, chat, ids):
        if self.delete_error:
            raise self.delete_error
        self.deleted.append((chat, list(ids)))


def real(env, thumbs=(), **msg_kwargs):
    client, renamer, job = make(env, **msg_kwargs)
    client = RealClient(client.source, client.message, thumbs)
    renamer = Renamer(client, env.cfg, env.db)
    return client, renamer, job


def test_a_thumbnail_counts_once_telegram_has_it():
    assert has_thumbnail(Message(td_message(document_content("a.mkv", 10, thumbnail=thumbnail_of()))))
    assert not has_thumbnail(Message(td_message(document_content("a.mkv", 10))))
    assert not has_thumbnail(Message(td_message(document_content("a.mkv", 10, minithumbnail="AAAA"))))  # the tiny blurred one is not it
    local_only = thumbnail_of()
    local_only["file"]["remote"] = {"@type": "remoteFile", "id": "", "unique_id": "", "is_uploading_active": False, "is_uploading_completed": False, "uploaded_size": 0}
    assert not has_thumbnail(Message(td_message(document_content("a.mkv", 10, thumbnail=local_only))))  # never got to Telegram
    assert not has_thumbnail(SimpleNamespace(id=1))
    finished = thumbnail_of()
    finished["file"]["remote"] = {"@type": "remoteFile", "id": "", "unique_id": "", "is_uploading_active": False, "is_uploading_completed": True, "uploaded_size": 10}
    assert has_thumbnail(Message(td_message(document_content("a.mkv", 10, thumbnail=finished))))  # an upload that finished is there, whatever the id says


@needs_ffmpeg
async def test_a_file_that_kept_its_thumbnail_is_sent_once_and_the_check_says_so(env):
    client, renamer, job = real(env)
    heard = heard_results(renamer)
    await renamer.process(job)
    assert len(client.sent) == 1 and client.deleted == [] and job.state == "done"
    done = client.edits[-1][0]
    assert "🔍 Checked: size · name · thumbnail" in done and "⚠️" not in done
    (_i, _s, outcome), = heard
    assert outcome.checks == "size · name · thumbnail" and not outcome.thumb_resent


@needs_ffmpeg
async def test_a_file_that_went_out_without_its_thumbnail_is_sent_again_and_the_first_copy_deleted(env):
    client, renamer, job = real(env, thumbs=[False, True])
    heard, kept = heard_results(renamer), []

    async def renamed(job, chat, sent):
        kept.append(sent.id)

    renamer.on_renamed = renamed
    await renamer.process(job)
    assert len(client.sent) == 2 and client.deleted == [(42, [3001])]  # the first copy is gone
    assert kept == [3002]  # what goes on from here (the question about sorting, say) is the copy that stays
    done = client.edits[-1][0]
    assert "🔍 Checked: size · name · thumbnail" in done and "⚠️" not in done and job.state == "done"
    assert heard[0][2].thumb_resent
    assert any("Uploading again (its thumbnail was lost)" in text for text, _b in client.edits)
    first, second = client.sent[0], client.sent[1]
    assert first[2]["thumb"] == second[2]["thumb"] and first[2]["reply_to"] == second[2]["reply_to"] == 7  # the same file, thumbnail and reply


@needs_ffmpeg
async def test_a_file_that_is_still_without_its_thumbnail_after_a_new_send_stays_with_a_warning(env):
    client, renamer, job = real(env, thumbs=[False, False])
    heard = heard_results(renamer)
    await renamer.process(job)
    assert len(client.sent) == 2 and client.deleted == [(42, [3001])]  # one copy is enough
    done = client.edits[-1][0]
    assert "⚠️ Telegram left the thumbnail out again." in done and "🔍 Checked: size · name\n" in done and job.state == "done"  # (no "thumbnail" checked)
    assert heard[0][2].warnings == ["Telegram left the thumbnail out again."]


@needs_ffmpeg
async def test_when_sending_again_fails_the_first_copy_stays_with_a_warning(env):
    client, renamer, job = real(env, thumbs=[False])
    heard = heard_results(renamer)
    original = client.send_file
    calls = []

    async def second_fails(target, source, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise errors.FloodWaitError(900)
        return await original(target, source, **kwargs)

    client.send_file = second_fails
    await renamer.process(job)
    assert client.deleted == [] and job.state == "done"
    assert "Telegram sent the file without its thumbnail, and sending it again failed (FloodWaitError)." in client.edits[-1][0]
    assert heard[0][2].state == "done" and not heard[0][2].thumb_resent


@needs_ffmpeg
async def test_a_copy_that_cannot_be_deleted_is_a_warning_and_nothing_more(env):
    client, renamer, job = real(env, thumbs=[False, True])
    client.delete_error = errors.RPCError("MESSAGE_DELETE_FORBIDDEN", 400)
    await renamer.process(job)
    assert job.state == "done" and "⚠️ The first copy, which has no thumbnail, could not be deleted." in client.edits[-1][0]


@needs_ffmpeg
async def test_files_in_a_row_that_stay_without_thumbnails_stop_being_sent_again_for_a_while(env, monkeypatch):
    from bot import pipeline

    client, renamer, job = real(env, thumbs=[False] * 40)
    for number in range(1, 5):
        job = Job(id=number, user_id=42, chat_id=42, msg_id=7, file_name=job.file_name, size=job.size, new_name=job.new_name,
                  status_msg=SimpleNamespace(id=99))
        before = len(client.sent)
        await renamer.process(job)
        sends = len(client.sent) - before
        assert sends == (2 if number <= pipeline.THUMB_STRIKES else 1)  # the fourth is not sent again: three in a row went wrong
    assert "⚠️ Telegram sent the file without its thumbnail." in client.edits[-1][0]
    clock = [time.monotonic() + pipeline.THUMB_PAUSE + 1]
    monkeypatch.setattr(pipeline, "time", SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    client.thumbs = [True] * 40
    client.thumbs[len(client.sent)] = False  # the next one lacks its thumbnail again, but it is tried now
    before = len(client.sent)
    await renamer.process(Job(id=9, user_id=42, chat_id=42, msg_id=7, file_name=job.file_name, size=job.size, new_name=job.new_name,
                              status_msg=SimpleNamespace(id=99)))
    assert len(client.sent) - before == 2 and renamer._thumb_strikes == 0  # and a copy that has its thumbnail starts the count again


@needs_ffmpeg
async def test_a_file_that_keeps_its_thumbnail_starts_the_count_of_lost_ones_again(env):
    from bot import pipeline

    # sends, in order: file 1 lost, lost again; file 2 lost, lost again; file 3 kept; files 4, 5 and 6 lost twice each; file 7 lost
    client, renamer, job = real(env, thumbs=[False, False, False, False, True] + [False] * 40)
    sends = []
    for number in range(1, 8):
        before = len(client.sent)
        await renamer.process(Job(id=number, user_id=42, chat_id=42, msg_id=7, file_name=job.file_name, size=job.size, new_name=job.new_name,
                                  status_msg=SimpleNamespace(id=99)))
        sends.append(len(client.sent) - before)
    assert sends == [2, 2, 1, 2, 2, 2, 1]  # the good one in the middle made it "three in a row" only after file 6, not after file 4
    assert renamer._thumb_strikes == pipeline.THUMB_STRIKES


@needs_ffmpeg
async def test_a_file_that_had_no_thumbnail_to_send_is_not_looked_at(env, monkeypatch):
    client, renamer, job = real(env, thumbs=[False])
    monkeypatch.setattr(media, "make_video_thumbnail", lambda *args, **kwargs: _false())
    await renamer.process(job)
    assert len(client.sent) == 1 and client.sent[0][2]["thumb"] is None and client.deleted == []
    done = client.edits[-1][0]
    assert "⚠️ No thumbnail could be made from the video, so it went out without one." in done and "🔍 Checked: size · name\n" in done


async def _false():
    return False


# ---------------------------------------------------------------------------
# what was looked at once the file was sent
# ---------------------------------------------------------------------------


@needs_ffmpeg
async def test_a_file_whose_size_or_name_differs_in_telegram_is_a_warning(env):
    client, renamer, job = real(env)
    client.sizes = {"size": 12345, "name": "something else.mkv"}
    await renamer.process(job)
    done = client.edits[-1][0]
    assert "Telegram has 12.06 KB and the file sent was " in done
    assert "Telegram calls the file “something else.mkv”, not “[S01E05] Tokyo Revengers [1080p].mkv”." in done
    assert "🔍 Checked: thumbnail" in done and job.state == "done"


@needs_ffmpeg
async def test_the_tracks_of_a_file_with_new_metadata_are_checked_and_listed(env):
    await env.db.update_settings(
        42, metadata_on=True, keep_audio=["english", "japanese"], keep_subtitle=["2"],
        meta={"title": "Tokyo Revengers", "audio": "{lang}", "subtitle": "English"},
    )
    client, renamer, job = real(env)
    await renamer.process(job)
    done = client.edits[-1][0]
    assert "🔍 Checked: size · name · tracks (1 video · 2 audio · 1 subtitles) · thumbnail" in done and "⚠️" not in done


@needs_ffmpeg
async def test_a_title_that_did_not_come_through_is_a_warning_and_the_new_file_is_used(env, monkeypatch):
    await env.db.update_settings(42, metadata_on=True, meta={"title": "Tokyo Revengers"})
    monkeypatch.setattr(media, "check_remux", lambda *args: ([], ["The title was not applied."]))
    client, renamer, job = real(env)
    await renamer.process(job)
    assert "⚠️ The title was not applied." in client.edits[-1][0] and "tracks (" in client.edits[-1][0]
    assert (await probe(client.uploads[0])).tags.get("title") == "Tokyo Revengers"  # the file ffmpeg made went out


@needs_ffmpeg
async def test_a_file_that_is_not_what_was_asked_for_is_not_sent_the_original_goes_out_as_it_is(env, monkeypatch):
    await env.db.update_settings(42, metadata_on=True, meta={"title": "Tokyo Revengers"})
    monkeypatch.setattr(media, "check_remux", lambda *args: (["only 1 of 3 audio tracks came through"], ["a note"]))
    client, renamer, job = real(env)
    heard = heard_results(renamer)
    await renamer.process(job)
    assert job.state == "done"
    assert client.uploads[0].read_bytes() == (env.media_dir / "sample.mkv").read_bytes()  # untouched
    done = client.edits[-1][0]
    assert "⚠️ The metadata and track changes could not be made (only 1 of 3 audio tracks came through), so the file went out as it was." in done
    assert "a note" not in done and "tracks (" not in done
    assert heard[0][2].warnings[-1].startswith("The metadata and track changes could not be made")


@needs_ffmpeg
async def test_when_ffmpeg_itself_fails_the_original_goes_out_as_it_is_and_the_user_is_told(env, monkeypatch):
    await env.db.update_settings(42, metadata_on=True, meta={"title": "Tokyo Revengers"})

    async def boom(*args, **kwargs):
        raise MediaError("ffmpeg failed: boom")

    monkeypatch.setattr(media, "apply_plan", boom)
    client, renamer, job = real(env)
    await renamer.process(job)
    assert job.state == "done" and client.uploads[0].read_bytes() == (env.media_dir / "sample.mkv").read_bytes()
    assert "could not be made (ffmpeg failed: boom), so the file went out as it was." in client.edits[-1][0]


@needs_ffmpeg
async def test_a_cancel_while_ffmpeg_runs_is_still_a_cancel(env, monkeypatch):
    from bot.errors import Cancelled

    await env.db.update_settings(42, metadata_on=True, meta={"title": "Tokyo Revengers"})

    async def stopped(*args, **kwargs):
        raise Cancelled()

    monkeypatch.setattr(media, "apply_plan", stopped)
    client, renamer, job = real(env)
    await renamer.process(job)
    assert job.state == "cancelled" and client.sent == []
