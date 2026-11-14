import asyncio

import pytest

from bot.errors import Cancelled, MediaError
from bot.media import (
    ImageTooBig,
    MediaInfo,
    Stream,
    apply_plan,
    build_plan,
    canonical_lang,
    extract_frame,
    format_tracks,
    frame_seeks,
    image_size,
    make_video_thumbnail,
    parse_probe,
    parse_track_tokens,
    prepare_thumb,
    probe,
    resize_exact,
    resolve_tokens,
    validate_track_tokens,
)

from .conftest import ffprobe_json, needs_ffmpeg


def audio(index, lang="", title=""):
    return Stream(index=index, kind="audio", codec="aac", lang=lang, title=title, channels=2)


def sub(index, lang="", title=""):
    return Stream(index=index, kind="subtitle", codec="subrip", lang=lang, title=title)


# ---------------------------------------------------------------------------
# pure logic
# ---------------------------------------------------------------------------


def test_parse_track_tokens():
    assert parse_track_tokens("English, Japanese") == ["english", "japanese"]
    assert parse_track_tokens("2 1") == ["2", "1"]
    assert parse_track_tokens("eng,eng jpn") == ["eng", "jpn"]
    assert parse_track_tokens("") == []


def test_validate_track_tokens():
    assert validate_track_tokens(["2", "english", "pt-br"]) is None
    assert validate_track_tokens(["0"]) is not None
    assert validate_track_tokens(["en;rm"]) is not None


def test_canonical_lang():
    assert canonical_lang("eng") == "english"
    assert canonical_lang("EN-us") == "english"
    assert canonical_lang("jpn") == "japanese"
    assert canonical_lang("chi") == canonical_lang("zho") == "chinese"
    assert canonical_lang("und") is None
    assert canonical_lang("") is None


def test_resolve_by_language_name_and_code_in_token_order():
    streams = [audio(1, "jpn"), audio(2, "eng"), audio(3, "hin")]
    assert [s.index for s in resolve_tokens(streams, ["english", "japanese"])] == [2, 1]
    assert [s.index for s in resolve_tokens(streams, ["eng", "jpn"])] == [2, 1]
    assert [s.index for s in resolve_tokens(streams, ["hindi"])] == [3]


def test_resolve_by_number_is_one_based_position_not_stream_index():
    streams = [audio(5, "jpn"), audio(9, "eng")]
    assert [s.index for s in resolve_tokens(streams, ["2", "1"])] == [9, 5]
    assert resolve_tokens(streams, ["3"]) == []


def test_resolve_dedupes_and_keeps_all_matches_of_a_language():
    streams = [audio(1, "eng", "Dub"), audio(2, "jpn"), audio(3, "eng", "Commentary")]
    assert [s.index for s in resolve_tokens(streams, ["english", "1"])] == [1, 3]
    assert [s.index for s in resolve_tokens(streams, ["english", "japanese"])] == [1, 3, 2]


def test_resolve_untagged_track_uses_title_words():
    streams = [audio(1, "und", "Japanese [Original]"), audio(2, "", "English Dub")]
    assert [s.index for s in resolve_tokens(streams, ["english"])] == [2]
    assert [s.index for s in resolve_tokens(streams, ["japanese"])] == [1]


def test_resolve_tagged_language_is_not_overridden_by_title():
    streams = [audio(1, "jpn", "English commentary")]
    assert resolve_tokens(streams, ["english"]) == []


def test_resolve_unknown_language_word_matches_raw_code():
    streams = [audio(1, "xyz"), audio(2, "eng")]
    assert [s.index for s in resolve_tokens(streams, ["xyz"])] == [1]


def make_info():
    return MediaInfo(
        duration=2,
        streams=[
            Stream(0, "video", codec="h264", width=320, height=240),
            audio(1, "jpn", "Japanese Original"),
            audio(2, "eng"),
            audio(3, "hin"),
            sub(4, "jpn"),
            sub(5, "eng"),
            Stream(6, "attachment", codec="ttf"),
        ],
    )


def test_build_plan_none_when_metadata_off_or_nothing_to_do():
    info = make_info()
    assert build_plan(info, {"metadata_on": False, "keep_audio": ["1"]}, ".mkv") is None
    assert build_plan(info, {"metadata_on": True, "meta": {}}, ".mkv") is None
    assert build_plan(info, {"metadata_on": True, "keep_audio": ["1"]}, ".avi") is None


def test_build_plan_maps_in_requested_order_and_drops_others():
    plan = build_plan(
        make_info(),
        {"metadata_on": True, "keep_audio": ["english", "japanese"], "keep_subtitle": ["2"], "meta": {}},
        ".mkv",
    )
    assert plan is not None
    maps = [plan.args[i + 1] for i, a in enumerate(plan.args) if a == "-map"]
    # video, audio eng(2) then jpn(1), subtitle #2 -> stream 5, attachment kept; hindi + jpn sub dropped
    assert maps == ["0:0", "0:2", "0:1", "0:5", "0:6"]
    assert plan.args[plan.args.index("-disposition:a:0") + 1] == "+default"
    assert plan.args[plan.args.index("-disposition:a:1") + 1] == "-default"
    assert plan.args[plan.args.index("-disposition:s:0") + 1] == "+default"


def test_build_plan_audio_safeguard():
    plan = build_plan(make_info(), {"metadata_on": True, "keep_audio": ["klingon"], "meta": {}}, ".mkv")
    assert plan is not None
    assert [s.index for s in plan.kept_audio] == [1, 2, 3]
    assert plan.warnings


@pytest.mark.parametrize("format_name, ext, forced", [
    ("matroska,webm", ".mp4", "matroska"),  # a Matroska file called .mp4: it stays Matroska (an MP4 cannot hold SRT or fonts)
    ("matroska,webm", ".m4v", "matroska"),
    ("matroska,webm", ".mov", "matroska"),
    ("mov,mp4,m4a,3gp,3g2,mj2", ".mkv", "mp4"),  # an MP4 called .mkv: Matroska cannot hold its subtitles
    ("mov,mp4,m4a,3gp,3g2,mj2", ".webm", "mp4"),
    ("matroska,webm", ".mkv", None),  # the name says what the file is: ffmpeg's own choice
    ("matroska,webm", ".webm", None),
    ("matroska,webm", ".mka", None),
    ("mov,mp4,m4a,3gp,3g2,mj2", ".mp4", None),
    ("mov,mp4,m4a,3gp,3g2,mj2", ".mov", None),
    ("mov,mp4,m4a,3gp,3g2,mj2", ".m4v", None),
    ("", ".mp4", None),  # not known: nothing is forced
    ("avi", ".mp4", None),  # not a container this knows: left to ffmpeg, as before
])
def test_build_plan_keeps_the_container_the_file_really_is(format_name, ext, forced):
    info = make_info()
    info.format_name = format_name
    plan = build_plan(info, {"metadata_on": True, "meta": {"title": "T"}}, ext)
    assert plan is not None
    if forced is None:
        assert "-f" not in plan.args
    else:
        assert plan.args[plan.args.index("-f") + 1] == forced
        assert plan.args.index("-f") > plan.args.index("copy")  # an output option, after the maps
        assert plan.args.count("-f") == 1


def test_build_plan_subtitles_dropped_when_nothing_matches():
    plan = build_plan(make_info(), {"metadata_on": True, "keep_subtitle": ["french"], "meta": {}}, ".mkv")
    assert plan.kept_subtitles == []
    maps = [plan.args[i + 1] for i, a in enumerate(plan.args) if a == "-map"]
    assert "0:4" not in maps and "0:5" not in maps


def test_build_plan_metadata_fields_and_lang_placeholder():
    plan = build_plan(
        make_info(),
        {
            "metadata_on": True,
            "keep_audio": ["japanese", "english"],
            "meta": {"title": "Tokyo Revengers", "author": "@chan", "artist": "@chan",
                     "audio": "{lang} Audio", "subtitle": "English", "video": "@chan"},
        },
        ".mkv",
    )
    a = plan.args
    assert ["-metadata", "title=Tokyo Revengers"] == a[a.index("title=Tokyo Revengers") - 1: a.index("title=Tokyo Revengers") + 1]
    assert "author=@chan" in a and "artist=@chan" in a
    assert "title=Japanese Audio" in a and "title=English Audio" in a
    assert a.index("title=Japanese Audio") < a.index("title=English Audio")
    assert "-metadata:s:v:0" in a and "-metadata:s:s:0" in a


def test_build_plan_mp3_tags_only():
    plan = build_plan(MediaInfo(), {"metadata_on": True, "meta": {"title": "T", "author": "A"}}, ".mp3")
    assert plan is not None
    assert "title=T" in plan.args and "artist=A" in plan.args
    assert build_plan(MediaInfo(), {"metadata_on": True, "meta": {}}, ".mp3") is None


def test_format_tracks():
    text = format_tracks(make_info())
    assert "1. Japanese (jpn)" in text and "2. English (eng)" in text and "3. Hindi (hin)" in text
    assert "Subtitles" in text and "“Japanese Original”" in text


def test_parse_probe_duration_fallbacks():
    info = parse_probe({"format": {"duration": "12.5"}, "streams": []})
    assert info.duration == 12.5
    info = parse_probe({"format": {}, "streams": [{"index": 0, "codec_type": "video", "tags": {"DURATION": "00:00:03.500000000"}}]})
    assert info.duration == 3.5


# ---------------------------------------------------------------------------
# real ffprobe / ffmpeg
# ---------------------------------------------------------------------------


@needs_ffmpeg
async def test_probe_real_mkv(media_dir):
    info = await probe(media_dir / "sample.mkv")
    assert [s.kind for s in info.streams] == ["video", "audio", "audio", "audio", "subtitle", "subtitle", "attachment"]
    assert [s.lang for s in info.audio] == ["jpn", "eng", "hin"]
    assert [s.lang for s in info.subtitles] == ["jpn", "eng"]
    assert info.width == 320 and info.height == 240
    assert 1.5 < info.duration < 2.6


@needs_ffmpeg
async def test_probe_missing_file_raises(tmp_path):
    with pytest.raises(MediaError):
        await probe(tmp_path / "nope.mkv")


@needs_ffmpeg
async def test_apply_plan_mkv_reorders_drops_and_tags(media_dir, tmp_path):
    src = media_dir / "sample.mkv"
    info = await probe(src)
    settings = {
        "metadata_on": True,
        "keep_audio": ["english", "japanese"],
        "keep_subtitle": ["english"],
        "meta": {"title": "Tokyo Revengers", "author": "@chan", "artist": "@chan",
                 "audio": "{lang} Audio", "subtitle": "{lang}", "video": "@chan video"},
    }
    plan = build_plan(info, settings, ".mkv")
    dst = tmp_path / "out.mkv"
    await apply_plan(src, dst, plan)

    data = ffprobe_json(dst, "-show_chapters")
    streams = data["streams"]
    kinds = [s["codec_type"] for s in streams]
    assert kinds == ["video", "audio", "audio", "subtitle", "attachment"]
    audios = [s for s in streams if s["codec_type"] == "audio"]
    assert [a["tags"]["language"] for a in audios] == ["eng", "jpn"]  # reordered, hindi dropped
    assert [a["tags"]["title"] for a in audios] == ["English Audio", "Japanese Audio"]
    assert audios[0]["disposition"]["default"] == 1 and audios[1]["disposition"]["default"] == 0
    sub_stream = next(s for s in streams if s["codec_type"] == "subtitle")
    assert sub_stream["tags"]["language"] == "eng" and sub_stream["tags"]["title"] == "English"
    assert sub_stream["disposition"]["default"] == 1
    video = streams[0]
    assert video["tags"]["title"] == "@chan video"
    fmt_tags = {k.lower(): v for k, v in data["format"]["tags"].items()}  # Matroska stores AUTHOR/ARTIST upper-case
    assert fmt_tags["title"] == "Tokyo Revengers"
    assert fmt_tags["author"] == "@chan" and fmt_tags["artist"] == "@chan"
    assert len(data["chapters"]) == 2  # chapters survive
    attach = next(s for s in streams if s["codec_type"] == "attachment")
    assert attach["tags"]["filename"] == "font.ttf"  # fonts survive


@needs_ffmpeg
async def test_apply_plan_numbers_follow_given_order(media_dir, tmp_path):
    src = media_dir / "sample.mkv"
    info = await probe(src)
    plan = build_plan(info, {"metadata_on": True, "keep_audio": ["3", "1"], "keep_subtitle": ["2", "1"], "meta": {}}, ".mkv")
    dst = tmp_path / "out.mkv"
    await apply_plan(src, dst, plan)
    out = await probe(dst)
    assert [s.lang for s in out.audio] == ["hin", "jpn"]
    assert [s.lang for s in out.subtitles] == ["eng", "jpn"]


@needs_ffmpeg
async def test_apply_plan_audio_safeguard_keeps_everything(media_dir, tmp_path):
    src = media_dir / "sample.mkv"
    info = await probe(src)
    plan = build_plan(info, {"metadata_on": True, "keep_audio": ["french"], "meta": {}}, ".mkv")
    dst = tmp_path / "out.mkv"
    await apply_plan(src, dst, plan)
    assert len((await probe(dst)).audio) == 3


@needs_ffmpeg
async def test_apply_plan_mp4(media_dir, tmp_path):
    src = media_dir / "sample.mp4"
    info = await probe(src)
    plan = build_plan(info, {"metadata_on": True, "keep_audio": ["english", "japanese"], "keep_subtitle": ["1"],
                             "meta": {"title": "MP4 Title"}}, ".mp4")
    dst = tmp_path / "out.mp4"
    await apply_plan(src, dst, plan)
    out = await probe(dst)
    assert [s.lang for s in out.audio] == ["eng", "jpn"]
    assert [s.kind for s in out.streams].count("subtitle") == 1
    assert out.tags.get("title") == "MP4 Title"


@needs_ffmpeg
async def test_apply_plan_matroska_called_mp4_stays_matroska_with_every_track(media_dir, tmp_path):
    """The failure that was reported: ``Could not find tag for codec subrip in stream #3, codec not currently supported in container``."""
    src = tmp_path / "Anime_Fury_S_04_EP_07_Tokyo_Revengers_1080p_Dual_Audio.mp4"
    src.write_bytes((media_dir / "sample.mkv").read_bytes())  # Matroska inside, MP4 on the label
    info = await probe(src)
    assert "matroska" in info.format_name
    plan = build_plan(info, {"metadata_on": True, "meta": {"title": "Tokyo Revengers", "audio": "{lang}", "subtitle": "{lang}"}}, ".mp4")
    dst = tmp_path / "Tokyo Revengers S04 E07.mp4"
    await apply_plan(src, dst, plan)
    out = await probe(dst)
    assert "matroska" in out.format_name  # still what it was
    assert [(s.kind, s.codec) for s in out.streams] == [(s.kind, s.codec) for s in info.streams]  # subtitles and the font included
    assert out.tags["title"] == "Tokyo Revengers" and [s.title for s in out.audio] == ["Japanese", "English", "Hindi"]


@needs_ffmpeg
async def test_apply_plan_matroska_called_mp4_with_keep_lists(media_dir, tmp_path):
    src = tmp_path / "episode.mp4"
    src.write_bytes((media_dir / "sample.mkv").read_bytes())
    plan = build_plan(await probe(src), {"metadata_on": True, "keep_audio": ["english"], "keep_subtitle": ["english"], "meta": {}}, ".mp4")
    dst = tmp_path / "out.mp4"
    await apply_plan(src, dst, plan)
    out = await probe(dst)
    assert [s.lang for s in out.audio] == ["eng"] and [s.lang for s in out.subtitles] == ["eng"]


@needs_ffmpeg
async def test_apply_plan_mp4_called_mkv_stays_mp4(media_dir, tmp_path):
    """The other way round: Matroska cannot hold the ``mov_text`` subtitles of an MP4, so this used to fail as well."""
    src = tmp_path / "episode.mkv"
    src.write_bytes((media_dir / "sample.mp4").read_bytes())
    info = await probe(src)
    plan = build_plan(info, {"metadata_on": True, "meta": {"title": "Kept"}}, ".mkv")
    dst = tmp_path / "out.mkv"
    await apply_plan(src, dst, plan)
    out = await probe(dst)
    assert "mp4" in out.format_name and out.tags["title"] == "Kept"
    assert [(s.kind, s.codec) for s in out.streams] == [(s.kind, s.codec) for s in info.streams]


@needs_ffmpeg
async def test_apply_plan_mp3_title(media_dir, tmp_path):
    src = media_dir / "sample.mp3"
    plan = build_plan(await probe(src), {"metadata_on": True, "meta": {"title": "New Title", "artist": "Me"}}, ".mp3")
    dst = tmp_path / "out.mp3"
    await apply_plan(src, dst, plan)
    out = await probe(dst)
    assert out.tags["title"] == "New Title" and out.tags["artist"] == "Me"


@needs_ffmpeg
async def test_apply_plan_failure_raises_media_error(tmp_path):
    bad = tmp_path / "bad.mkv"
    bad.write_bytes(b"not a video")
    from bot.media import Plan

    with pytest.raises(MediaError):
        await apply_plan(bad, tmp_path / "out.mkv", Plan(["-map", "0:0", "-c", "copy"]))


@needs_ffmpeg
async def test_cancel_kills_ffmpeg(media_dir, tmp_path):
    cancel = asyncio.Event()
    cancel.set()
    info = await probe(media_dir / "sample.mkv")
    plan = build_plan(info, {"metadata_on": True, "keep_audio": ["1"], "meta": {}}, ".mkv")
    with pytest.raises(Cancelled):
        await apply_plan(media_dir / "sample.mkv", tmp_path / "out.mkv", plan, cancel)


@needs_ffmpeg
async def test_thumbnail_and_prepare_thumb(media_dir, tmp_path):
    out = tmp_path / "t.jpg"
    assert await make_video_thumbnail(media_dir / "sample.mkv", out, 2.0)
    assert out.read_bytes()[:2] == b"\xff\xd8"
    from PIL import Image
    import io

    big = io.BytesIO()
    Image.effect_noise((1600, 1200), 80).convert("RGB").save(big, "PNG")
    jpg = prepare_thumb(big.getvalue())
    img = Image.open(io.BytesIO(jpg))
    assert max(img.size) <= 320 and len(jpg) <= 200 * 1024 and img.format == "JPEG"
    assert not await make_video_thumbnail(tmp_path / "missing.mkv", tmp_path / "x.jpg", 1.0)


# ---------------------------------------------------------------------------
# /ethumb, /size and /resize helpers
# ---------------------------------------------------------------------------


def test_frame_seeks_start_at_about_a_tenth_and_fall_back_to_earlier_positions():
    assert frame_seeks(1400) == [60.0, 20.0, 5.0, 0.0]
    assert frame_seeks(100) == [10.0, 5.0, 0.0]
    assert frame_seeks(2) == [0.2, 0.0]
    assert frame_seeks(0) == [1.0, 0.0]  # duration unknown (a cut-off file)
    assert frame_seeks(-5) == [1.0, 0.0]


def _png(size, color=(10, 120, 200), mode="RGB", exif_orientation=None):
    import io
    from PIL import Image

    img = Image.new(mode, size, color)
    kw = {}
    if exif_orientation:
        exif = img.getexif()
        exif[274] = exif_orientation
        kw["exif"] = exif
    buf = io.BytesIO()
    img.save(buf, "PNG", **kw)
    return buf.getvalue()


def _opened(data):
    import io
    from PIL import Image

    return Image.open(io.BytesIO(data))


def test_image_size_is_the_width_and_the_height():
    assert image_size(_png((1920, 1080))) == (1920, 1080)
    assert image_size(_png((7, 300))) == (7, 300)


def test_image_size_counts_a_picture_stored_sideways_as_turned_upright():
    assert image_size(_png((200, 100), exif_orientation=6)) == (100, 200)
    assert image_size(_png((200, 100), exif_orientation=8)) == (100, 200)
    assert image_size(_png((200, 100), exif_orientation=1)) == (200, 100)
    assert image_size(_png((200, 100), exif_orientation=3)) == (200, 100)  # upside down: the same width and height


def test_image_size_rejects_what_is_not_an_image():
    with pytest.raises(Exception):
        image_size(b"definitely not an image")


@pytest.mark.parametrize("source,wanted", [
    ((1920, 1080), (720, 1080)), ((100, 50), (400, 400)), ((1000, 2000), (3, 900)), ((640, 480), (1, 1)), ((64, 64), (64, 64)),
])
def test_resize_exact_gives_exactly_the_size_asked_for_whatever_the_shape(source, wanted):
    img = _opened(resize_exact(_png(source), *wanted))
    assert img.format == "PNG" and img.size == wanted


def test_resize_exact_stretches_the_whole_picture():
    from PIL import Image

    two_halves = Image.new("RGB", (200, 100), (0, 0, 255))
    two_halves.paste((255, 0, 0), (0, 0, 100, 100))
    import io

    buf = io.BytesIO()
    two_halves.save(buf, "PNG")
    out = _opened(resize_exact(buf.getvalue(), 40, 400)).convert("RGB")
    assert out.getpixel((2, 5)) == out.getpixel((2, 395)) == (255, 0, 0)
    assert out.getpixel((37, 5)) == out.getpixel((37, 395)) == (0, 0, 255)


def test_resize_exact_keeps_transparency_and_does_not_turn_it_white_or_black():
    img = _opened(resize_exact(_png((200, 200), (0, 0, 0, 0), mode="RGBA"), 100, 50))
    assert img.mode == "RGBA" and img.getpixel((50, 25))[3] == 0


def test_resize_exact_turns_a_picture_stored_sideways_upright_first():
    from PIL import Image
    import io

    img = Image.new("RGB", (200, 100), (0, 0, 255))
    img.paste((255, 0, 0), (0, 0, 100, 100))  # the left half is red
    exif = img.getexif()
    exif[274] = 6
    buf = io.BytesIO()
    img.save(buf, "PNG", exif=exif)
    out = _opened(resize_exact(buf.getvalue(), 100, 200)).convert("RGB")
    assert out.getpixel((50, 10)) == (255, 0, 0) and out.getpixel((50, 190)) == (0, 0, 255)  # red on top: it was turned


@pytest.mark.parametrize("mode,saved_as", [("P", "PNG"), ("1", "PNG"), ("LA", "PNG"), ("L", "PNG"), ("CMYK", "JPEG"), ("RGBA", "PNG")])
def test_resize_exact_takes_every_kind_of_picture(mode, saved_as):
    import io
    from PIL import Image

    buf = io.BytesIO()
    Image.new(mode, (30, 20)).save(buf, saved_as)
    img = _opened(resize_exact(buf.getvalue(), 15, 10))
    assert img.format == "PNG" and img.size == (15, 10)


def test_resize_exact_keeps_a_palette_picture_smooth_not_blocky():
    import io
    from PIL import Image

    ramp = Image.new("L", (256, 1))
    ramp.putdata(list(range(256)))
    buf = io.BytesIO()
    ramp.convert("P").save(buf, "PNG")
    out = _opened(resize_exact(buf.getvalue(), 64, 1)).convert("L")
    values = [out.getpixel((x, 0)) for x in range(64)]
    assert len(set(values)) > 50 and values == sorted(values)  # a gradient, not a few flat bands


def test_resize_exact_refuses_a_picture_with_too_many_pixels(monkeypatch):
    monkeypatch.setattr("bot.media.RESIZE_MAX_PIXELS", 400)
    with pytest.raises(ImageTooBig):
        resize_exact(_png((21, 20)), 5, 5)
    assert _opened(resize_exact(_png((20, 20)), 5, 5)).size == (5, 5)  # right at the limit is fine
    assert issubclass(ImageTooBig, ValueError)


def test_resize_exact_rejects_what_is_not_an_image():
    with pytest.raises(Exception):
        resize_exact(b"definitely not an image", 100, 100)


@needs_ffmpeg
async def test_extract_frame_writes_a_jpeg_and_tries_the_next_position_after_a_miss(media_dir, tmp_path):
    out = tmp_path / "f.jpg"
    assert await extract_frame(media_dir / "sample.mkv", out, [1.0])
    assert out.read_bytes()[:2] == b"\xff\xd8"
    from PIL import Image

    assert Image.open(out).size == (320, 240)  # the sample is smaller than the 1280 px limit
    out.unlink()
    assert await extract_frame(media_dir / "sample.mkv", out, [500.0, 0.0])  # nothing at 500 s: the next position works
    assert out.stat().st_size > 0


@needs_ffmpeg
async def test_extract_frame_scales_down_wide_pictures(media_dir, tmp_path):
    from PIL import Image

    out = tmp_path / "small.jpg"
    assert await extract_frame(media_dir / "sample.mkv", out, [0.0], width=160)
    assert Image.open(out).size == (160, 120)


@needs_ffmpeg
async def test_extract_frame_gives_up_on_junk_without_raising(tmp_path):
    junk = tmp_path / "junk.mkv"
    junk.write_bytes(b"\x00" * 4096)
    assert not await extract_frame(junk, tmp_path / "j.jpg", [1.0, 0.0])
    assert not await extract_frame(tmp_path / "missing.mkv", tmp_path / "m.jpg", [0.0])
