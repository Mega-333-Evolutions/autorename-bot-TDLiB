"""/mediainfo: reading a file and laying the report out."""
import pytest

from bot import mediainfo as mi
from bot import telegraph

from .conftest import needs_ffmpeg

TEXT = """General
Complete name                            : /tmp/work/minfo1_2/probe.mkv
Format                                   : Matroska
Conformance errors                       : 2
 0x8538067                               : Yes
  General compliance                     : Element size 1 is more than maximal permitted size 2 (offset 0x34)
 Matroska                                : Yes
CRC_Error_Pos                            : 943
Writing library                          : Lavf

Video
ID                                       : 1
Format                                   : AVC
Title                                    : \x07bell\x1b[0m here

Menu
00:00:00.000                             : Intro
Plain line without a separator
Empty value                              :
"""

FFPROBE = {
    "format": {
        "format_long_name": "Matroska / WebM", "duration": "1441.419",
        "tags": {"title": "Show", "ENCODER": "libebml", "creation_time": "2026-01-01T00:00:00Z", "WEIRD_KEY": "x"},
    },
    "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "hevc", "codec_long_name": "H.265 / HEVC", "profile": "Main 10",
         "level": 123, "width": 1920, "height": 1080, "avg_frame_rate": "24000/1001", "pix_fmt": "yuv420p10le",
         "bits_per_raw_sample": "10", "display_aspect_ratio": "16:9", "color_space": "bt709",
         "disposition": {"default": 1}, "tags": {"BPS": "3200000", "DURATION": "00:24:01.419000000"}},
        {"index": 1, "codec_type": "audio", "codec_name": "aac", "profile": "LC", "channels": 2, "channel_layout": "stereo",
         "sample_rate": "48000", "disposition": {"default": 1, "forced": 0}, "tags": {"language": "jpn", "title": "Japanese"}},
        {"index": 2, "codec_type": "audio", "codec_name": "ac3", "channels": 1, "sample_rate": "44100", "tags": {"language": "hin"}},
        {"index": 3, "codec_type": "subtitle", "codec_name": "ass", "disposition": {"forced": 1}, "tags": {"language": "eng"}},
        {"index": 4, "codec_type": "attachment", "codec_name": "ttf", "tags": {"filename": "font.ttf"}},
    ],
    "chapters": [{"start_time": "0.000000", "tags": {"title": "Intro"}}, {"start_time": "90.5", "tags": {"title": "Main"}}],
}


def lines_of(report, title):
    return dict(report.section(title).lines)


def report_of(*sections, **kw):
    return mi.Report(sections=list(sections), engine=kw.pop("engine", "MediaInfo"), **kw)


# ---------------------------------------------------------------------------
# MediaInfo's number formats
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("num, text", [
    (0, "0 Bytes"), (999, "999 Bytes"), (1024, "1.00 KiB"), (71 * 1024, "71.0 KiB"), (93550, "91.4 KiB"),
    (845 * 1024 ** 2, "845 MiB"), (int(1.46 * 1024 ** 3), "1.46 GiB"), (int(1023.9 * 1024), "1.00 MiB"), (5 * 1024 ** 4, "5.00 TiB"),
])
def test_sizes_are_written_like_mediainfo_does(num, text):
    assert mi.mi_size(num) == text


@pytest.mark.parametrize("bps, text", [
    (62, "62 b/s"), (370_000, "370 kb/s"), (1_936_000, "1 936 kb/s"), (9_999_000, "9 999 kb/s"),
    (12_340_000, "12.3 Mb/s"), (150_000_000, "150 Mb/s"),
])
def test_bit_rates_are_written_like_mediainfo_does(bps, text):
    assert mi.mi_rate(bps) == text


@pytest.mark.parametrize("ms, text", [
    (2023, "2 s 23 ms"), (1_441_000, "24 min 1 s"), (5_040_000, "1 h 24 min"), (0, "0 s 0 ms"), (59_999.6, "1 min 0 s"),
])
def test_durations_are_written_like_mediainfo_does(ms, text):
    assert mi.mi_duration(ms) == text


# ---------------------------------------------------------------------------
# reading MediaInfo's text
# ---------------------------------------------------------------------------


def test_the_text_is_split_into_sections_of_labels_and_values():
    sections = mi.parse_text(TEXT)
    assert [s.title for s in sections] == ["General", "Video", "Menu"]
    general = sections[0].lines
    assert general[0] == ("Complete name", "/tmp/work/minfo1_2/probe.mkv")
    assert (" 0x8538067", "Yes") in general and ("  General compliance", general[4][1]) == general[4]  # nesting is kept
    video = dict(sections[1].lines)
    assert video["Title"] == "bell[0m here"  # control characters are gone
    menu = sections[2].lines
    assert menu[0] == ("00:00:00.000", "Intro") and ("", "Plain line without a separator") in menu and ("Empty value", "") in menu


def test_windows_line_endings_and_blank_runs_are_fine():
    sections = mi.parse_text("General\r\nFormat : MPEG-4\r\n\r\n\r\nAudio\r\nFormat : AAC\r\n")
    assert [(s.title, s.lines) for s in sections] == [("General", [("Format", "MPEG-4")]), ("Audio", [("Format", "AAC")])]


# ---------------------------------------------------------------------------
# the General section of a file we only have the start of
# ---------------------------------------------------------------------------


def test_a_cut_short_file_gets_its_real_name_and_size_and_no_false_alarms():
    report = report_of(*mi.parse_text(TEXT), truncated=True)
    mi.fix_general(report, "Show 01.mkv", 700 * 1024 ** 2, 1_440_000.0)
    general = report.section("General")
    assert [label for label, _ in general.lines] == ["Complete name", "Format", "Writing library", "File size", "Overall bit rate"]
    values = dict(general.lines)
    assert values["Complete name"] == "Show 01.mkv" and values["File size"] == "700 MiB"
    assert values["Overall bit rate"] == "4 078 kb/s"  # 700 MiB over 24 minutes


def test_a_cut_short_file_with_no_duration_loses_a_bit_rate_that_would_be_wrong():
    report = report_of(*mi.parse_text("General\nFormat : X\nOverall bit rate : 11.9 kb/s\nOverall bit rate mode : Constant\n"), truncated=True)
    mi.fix_general(report, "a.bin", 5000, None)
    assert [label for label, _ in report.section("General").lines] == ["Complete name", "Format", "Overall bit rate mode", "File size"]


def test_a_whole_file_keeps_everything_mediainfo_says_about_it():
    report = report_of(*mi.parse_text(TEXT))
    mi.fix_general(report, "x.mkv", 1000, 1000.0)
    labels = [label for label, _ in report.section("General").lines]
    assert "Conformance errors" in labels and "CRC_Error_Pos" in labels and " 0x8538067" in labels  # real problems stay visible
    assert dict(report.section("General").lines)["File size"] == "1000 Bytes" and "Overall bit rate" not in labels


def test_a_report_with_no_general_section_gets_one():
    report = report_of(truncated=False, engine="")
    mi.fix_general(report, "mystery.bin", 2048, None)
    assert report.sections[0].title == "General" and report.sections[0].lines == [("Complete name", "mystery.bin"), ("File size", "2.00 KiB")]


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------


def test_the_summary_line_counts_the_streams():
    general = mi.Section("General", [("Format", "Matroska"), ("Duration", "24 min 1 s")])
    streams = [mi.Section("Video"), mi.Section("Audio #1"), mi.Section("Audio #2"), mi.Section("Text #1"), mi.Section("Menu")]
    assert report_of(general, *streams).summary() == "Matroska · 24 min 1 s · 1 video · 2 audio · 1 subtitle"
    assert report_of(general).summary() == "Matroska · 24 min 1 s"
    assert report_of().summary() == ""


def test_only_real_streams_count_as_streams():
    assert not report_of(mi.Section("General"), mi.Section("Menu"), mi.Section("Telegram")).has_streams
    assert report_of(mi.Section("General"), mi.Section("Image")).has_streams
    assert report_of(mi.Section("General"), mi.Section("Text #12")).has_streams


def test_the_telegram_section_lists_what_telegram_knows_and_skips_the_rest():
    full = mi.telegram_section("Show 01.mkv", 93550, mime="video/x-matroska", sent_as="Video", sent_on="2026-10-21 09:00:00 UTC",
                               dc="4", caption="[HDR] Dual")
    assert full.title == "Telegram" and full.lines == [
        ("File name", "Show 01.mkv"), ("File size", "91.4 KiB (93 550 bytes)"), ("MIME type", "video/x-matroska"),
        ("Sent as", "Video"), ("Sent on", "2026-10-21 09:00:00 UTC"), ("Data center", "4"), ("Caption", "[HDR] Dual"),
    ]
    assert mi.telegram_section("x", 0).lines == [("File name", "x")]


# ---------------------------------------------------------------------------
# laying a section out
# ---------------------------------------------------------------------------


def test_values_line_up_after_the_labels():
    section = mi.Section("Video", [("Format", "AVC"), ("Duration", "2 s"), ("ID", "1")])
    assert mi.render_section(section) == "Format   : AVC\nDuration : 2 s\nID       : 1"


def test_long_values_wrap_under_the_start_of_the_value():
    section = mi.Section("Video", [("Encoding settings", " / ".join(f"option{i}={i}" for i in range(40))), ("ID", "1")])
    lines = mi.render_section(section, width=64).splitlines()
    assert lines[0].startswith("Encoding settings : option0=0 / ") and len(lines) > 3
    indent = " " * len("Encoding settings : ")
    assert all(line.startswith(indent) for line in lines[1:-1]) and lines[-1].startswith("ID")
    assert all(len(line) <= 64 + 8 for line in lines)  # a wrapped value gets at least MIN_VALUE columns


def test_a_very_long_label_does_not_push_every_value_to_the_right():
    section = mi.Section("Video", [("ID", "1"), ("Format settings, Reference frames", "1 frame"), ("Format", "AVC")])
    lines = mi.render_section(section).splitlines()
    assert lines == ["ID     : 1", "Format settings, Reference frames : 1 frame", "Format : AVC"]


def test_unbroken_text_is_still_wrapped_and_lines_end_cleanly():
    section = mi.Section("Menu", [("", "x" * 150), ("Blank", ""), ("Name", "y" * 200)])
    lines = mi.render_section(section, width=64).splitlines()
    assert max(len(line) for line in lines) <= 64 + 8 and all(line == line.rstrip() for line in lines)
    assert "".join(line.strip() for line in lines).count("y") == 200


def test_values_can_be_shortened_to_fit():
    section = mi.Section("General", [("Comment", "z" * 500)])
    line = mi.render_section(section, max_value=50)
    assert line.endswith("…") and len(line.split(" : ")[1]) == 50


# ---------------------------------------------------------------------------
# the Telegraph page
# ---------------------------------------------------------------------------


def test_every_section_becomes_a_heading_and_a_block_and_the_notes_come_last():
    report = report_of(mi.Section("General", [("Format", "Matroska")]), mi.Section("Telegram", [("File name", "a.mkv")]), truncated=True)
    nodes = mi.to_nodes(report)
    assert [n["tag"] for n in nodes] == ["h4", "pre", "h4", "pre", "p", "p"]
    assert nodes[0]["children"] == ["General"] and nodes[1]["children"] == ["Format : Matroska"]
    notes = [n["children"][0]["children"][0] for n in nodes if n["tag"] == "p"]
    assert "Only the start of the file was read" in notes[0] and notes[1] == "Made by the bot with MediaInfo."


def test_extra_notes_are_kept_and_an_unknown_engine_is_not_named():
    report = report_of(mi.Section("General", [("Format", "x")]), engine="", notes=["No tracks could be read."])
    notes = [n["children"][0]["children"][0] for n in mi.to_nodes(report) if n["tag"] == "p"]
    assert notes == ["No tracks could be read.", "Made by the bot."]


def test_a_page_that_is_too_big_has_its_long_values_shortened_first():
    big = mi.Section("Video", [(f"Setting {i}", "v" * 600) for i in range(60)])
    report = report_of(mi.Section("General", [("Format", "x")]), big, mi.Section("Telegram", [("File name", "a")]))
    assert telegraph.content_size(mi.to_nodes(report, limit=10 ** 9)) > 30_000
    nodes = mi.to_nodes(report, limit=30_000)
    assert telegraph.content_size(nodes) <= 30_000
    titles = [n["children"][0] for n in nodes if n["tag"] == "h4"]
    assert titles == ["General", "Video", "Telegram"]  # nothing dropped, only shortened
    assert "…" in nodes[3]["children"][0]


def test_a_file_with_hundreds_of_tracks_drops_the_last_sections_but_keeps_the_ends():
    sections = [mi.Section("General", [("Format", "Matroska")])]
    sections += [mi.Section(f"Text #{i}", [("Format", "UTF-8"), ("Title", "t" * 80), ("Language", "English")]) for i in range(1, 301)]
    sections.append(mi.Section("Telegram", [("File name", "big.mkv")]))
    nodes = mi.to_nodes(report_of(*sections), limit=12_000)
    assert telegraph.content_size(nodes) <= 12_000
    titles = [n["children"][0] for n in nodes if n["tag"] == "h4"]
    assert titles[0] == "General" and titles[-1] == "Telegram" and 3 < len(titles) < 302
    notes = [n["children"][0]["children"][0] for n in nodes if n["tag"] == "p"]
    assert any("page size limit" in note for note in notes)


def test_the_default_limit_is_what_telegraph_accepts():
    report = report_of(mi.Section("General", [("Format", "x")]))
    assert telegraph.content_size(mi.to_nodes(report)) <= telegraph.CONTENT_LIMIT < 64 * 1024


def test_the_plain_text_version_has_everything_the_page_has():
    report = report_of(mi.Section("General", [("Format", "Matroska"), ("Duration", "2 s")]), mi.Section("Telegram", [("File name", "a.mkv")]), truncated=True)
    text = mi.render_text(report)
    assert text.startswith("General\nFormat   : Matroska\nDuration : 2 s\n\nTelegram\nFile name : a.mkv\n")
    assert "Only the start of the file was read" in text and text.rstrip().endswith("Made by the bot with MediaInfo.") and text.endswith("\n")


# ---------------------------------------------------------------------------
# the ffprobe fallback
# ---------------------------------------------------------------------------


def test_ffprobe_output_becomes_mediainfo_style_sections():
    sections = mi.ffprobe_sections(FFPROBE, "Show 01.mkv", 700 * 1024 ** 2)
    assert [s.title for s in sections] == ["General", "Video", "Audio #1", "Audio #2", "Text", "Menu"]
    report = report_of(*sections)
    general = lines_of(report, "General")
    assert general["Complete name"] == "Show 01.mkv" and general["Format"] == "Matroska / WebM" and general["File size"] == "700 MiB"
    assert general["Duration"] == "24 min 1 s" and general["Overall bit rate"] == "4 074 kb/s"
    assert general["Title"] == "Show" and general["Writing application"] == "libebml" and general["Encoded date"] == "2026-01-01T00:00:00Z"
    assert general["Weird Key"] == "x" and general["Attachments"] == "font.ttf"
    video = lines_of(report, "Video")
    assert video["Format"] == "H.265 / HEVC" and video["Format profile"] == "Main 10@L4.1" and video["Frame rate"] == "23.976 FPS"
    assert video["Width"] == "1920 pixels" and video["Height"] == "1080 pixels" and video["Bit depth"] == "10 bits"
    assert video["Bit rate"] == "3 200 kb/s" and video["Duration"] == "24 min 1 s" and video["Default"] == "Yes" and video["Forced"] == "No"
    first, second = lines_of(report, "Audio #1"), lines_of(report, "Audio #2")
    assert first["Language"] == "Japanese" and first["Title"] == "Japanese" and first["Channel(s)"] == "2 channels"
    assert first["Sampling rate"] == "48 kHz" and first["Format profile"] == "LC" and first["Default"] == "Yes"
    assert second["Language"] == "Hindi" and second["Channel(s)"] == "1 channel" and second["Sampling rate"] == "44.1 kHz"
    text = lines_of(report, "Text")
    assert text["Language"] == "English" and text["Forced"] == "Yes" and text["Default"] == "No"
    assert report.section("Menu").lines == [("00:00:00.000", "Intro"), ("00:01:30.500", "Main")]


@pytest.mark.parametrize("codec, level, shown", [
    ("h264", 41, "L4.1"), ("h264", 40, "L4"), ("h264", 12, "L1.2"), ("hevc", 120, "L4"), ("hevc", 153, "L5.1"),
    ("h264", -99, ""), ("vp9", 41, ""), ("h264", None, ""),
])
def test_codec_levels_are_shown_the_way_each_codec_counts_them(codec, level, shown):
    assert mi._level({"codec_name": codec, "level": level}) == shown


def test_ffprobe_with_nothing_to_show_gives_just_a_general_section():
    sections = mi.ffprobe_sections({"format": {"format_name": "mp3"}}, "a.mp3", 0)
    assert [s.title for s in sections] == ["General"] and sections[0].lines == [("Complete name", "a.mp3"), ("Format", "mp3")]


# ---------------------------------------------------------------------------
# real files
# ---------------------------------------------------------------------------


@needs_ffmpeg
async def test_a_real_mkv_is_read_by_mediainfo(media_dir):
    pytest.importorskip("pymediainfo")
    path = media_dir / "sample.mkv"
    report = await mi.read_file(path, "Show 01.mkv", path.stat().st_size, extra=mi.telegram_section("Show 01.mkv", path.stat().st_size))
    assert report.engine == "MediaInfo" and report.has_streams and not report.truncated
    assert [s.title for s in report.sections] == ["General", "Video", "Audio #1", "Audio #2", "Audio #3", "Text #1", "Text #2", "Menu", "Telegram"]
    general = lines_of(report, "General")
    assert general["Complete name"] == "Show 01.mkv" and general["Format"] == "Matroska"  # not the path it was read from
    assert lines_of(report, "Audio #1")["Language"] == "Japanese" and lines_of(report, "Text #2")["Language"] == "English"
    assert "AVC" in lines_of(report, "Video")["Format"] and lines_of(report, "Video")["Width"] == "320 pixels"
    assert report.summary().startswith("Matroska · 2 s") and report.summary().endswith("1 video · 3 audio · 2 subtitle")


@needs_ffmpeg
async def test_without_mediainfo_ffprobe_gives_the_same_layout(media_dir, monkeypatch):
    monkeypatch.setattr(mi, "_mediainfo", lambda path: None)
    path = media_dir / "sample.mkv"
    report = await mi.read_file(path, "Show 01.mkv", path.stat().st_size)
    assert report.engine == "ffprobe" and report.has_streams
    assert [s.title for s in report.sections] == ["General", "Video", "Audio #1", "Audio #2", "Audio #3", "Text #1", "Text #2", "Menu"]
    assert lines_of(report, "General")["Complete name"] == "Show 01.mkv" and lines_of(report, "Audio #3")["Language"] == "Hindi"
    assert report.section("Menu").lines == [("00:00:00.000", "Intro"), ("00:00:01.000", "Main")]


@needs_ffmpeg
async def test_a_failing_mediainfo_falls_back_to_ffprobe(media_dir, monkeypatch):
    def boom(path):
        raise RuntimeError("libmediainfo crashed")

    monkeypatch.setattr(mi, "_mediainfo", boom)
    report = await mi.read_file(media_dir / "sample.mp4", "Movie.mp4", (media_dir / "sample.mp4").stat().st_size)
    assert report.engine == "ffprobe" and report.has_streams and lines_of(report, "Audio #2")["Language"] == "English"


@needs_ffmpeg
async def test_an_audio_file_shows_its_tags(media_dir):
    path = media_dir / "sample.mp3"
    report = await mi.read_file(path, "Song.mp3", path.stat().st_size)
    assert report.has_streams and report.section("Audio") is not None
    assert "Old" in lines_of(report, "General").values()  # the title tag, however the engine labels it


@needs_ffmpeg
async def test_only_the_start_of_an_mkv_still_gets_the_real_size_and_bit_rate(media_dir, tmp_path):
    pytest.importorskip("pymediainfo")
    whole = media_dir / "sample.mkv"
    cut = tmp_path / "probe.mkv"
    cut.write_bytes(whole.read_bytes()[:3000])
    real = whole.stat().st_size
    expected = await mi.read_file(whole, "Show 01.mkv", real)
    report = await mi.read_file(cut, "Show 01.mkv", real)
    assert report.truncated and report.has_streams
    general, reference = lines_of(report, "General"), lines_of(expected, "General")
    assert general["File size"] == reference["File size"] and general["Overall bit rate"] == reference["Overall bit rate"]
    assert not any("Conformance" in label or label.strip() == "CRC_Error_Pos" for label, _ in report.section("General").lines)


@needs_ffmpeg
async def test_an_mp4_cut_before_its_index_has_no_streams_but_still_a_report(media_dir, tmp_path):
    cut = tmp_path / "probe.mp4"
    cut.write_bytes((media_dir / "sample.mp4").read_bytes()[:3000])
    report = await mi.read_file(cut, "Movie.mp4", 40 * 1024 * 1024)
    assert report.truncated and not report.has_streams
    assert lines_of(report, "General")["Complete name"] == "Movie.mp4" and lines_of(report, "General")["File size"] == "40.0 MiB"


async def test_a_file_that_is_not_media_still_gets_a_general_section(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("hello\n" * 100)
    report = await mi.read_file(path, "notes.txt", path.stat().st_size)
    assert not report.has_streams and not report.truncated
    general = lines_of(report, "General")
    assert general["Complete name"] == "notes.txt" and general["File size"] == "600 Bytes"
