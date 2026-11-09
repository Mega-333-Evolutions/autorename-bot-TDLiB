import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot import errorlog
from bot.config import load_config
from bot.db import Database
from bot.media import probe
from bot.parser import DEFAULT_TEMPLATE, compute_new_name
from bot.pipeline import Renamer, decide_send_type, guess_mime, original_name_of, source_ext
from bot.queue import Job
from bot.tg import errors, types

from .appkit import ensure_auto
from .conftest import needs_ffmpeg


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

    async def edit_message(self, message, text, buttons=None, parse_mode=None):
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
