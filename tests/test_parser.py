import pytest

from bot.parser import (
    DEFAULT_TEMPLATE,
    apply_remove_words,
    compute_new_name,
    finalize_name,
    build_values,
    missing_variables,
    padded_episode,
    parse_caption,
    parse_info,
    render_template,
    sanitize,
    split_ext,
)


@pytest.mark.parametrize(
    "name,title,season,episode,quality,year",
    [
        ("[SubsPlease] Tokyo Revengers - 05 (1080p) [ABCD1234].mkv", "Tokyo Revengers", "01", "05", "1080p", ""),
        ("Show.Name.S02E10.Some.Episode.Title.720p.WEB-DL.x264-GRP.mkv", "Show Name", "02", "10", "720p", ""),
        ("Naruto Shippuden Episode 123 Dual Audio 720p.mp4", "Naruto Shippuden", "01", "123", "720p", ""),
        ("Inception.2010.1080p.BluRay.x264-GROUP.mkv", "Inception", "", "", "1080p", "2010"),
        ("Blade.Runner.2049.2017.1080p.BluRay.mkv", "Blade Runner 2049", "", "", "1080p", "2017"),
        ("One Piece - 1000 [1080p].mkv", "One Piece", "01", "1000", "1080p", ""),
        ("Attack_on_Titan_S4E28_480p.mp4", "Attack on Titan", "04", "28", "480p", ""),
        ("Show 1x05 720p.mkv", "Show", "01", "05", "720p", ""),
        ("[S01E05] Title [720p].mkv", "Title", "01", "05", "720p", ""),
        ("Title (2021) - 05 [720p].mkv", "Title", "01", "05", "720p", "2021"),
        ("Some Movie 1920x1080.mkv", "Some Movie", "", "", "1080p", ""),
        ("Plain Name.mkv", "Plain Name", "", "", "", ""),
        ("Show S2 - 07 [1080p].mkv", "Show", "02", "07", "1080p", ""),
        ("Movie 4K HDR.mkv", "Movie", "", "", "2160p", ""),
    ],
)
def test_parse_info(name, title, season, episode, quality, year):
    info = parse_info(name)
    assert (info.title, info.season, info.episode, info.quality, info.year) == (
        title, season, episode, quality, year
    )


def test_resolution_is_not_an_episode():
    info = parse_info("Some Movie 1920x1080.mkv")
    assert info.episode == ""


def test_codec_detection():
    assert parse_info("Show.S01E01.1080p.x265-GRP.mkv").codec == "x265"
    assert parse_info("Show.S01E01.1080p.H.264.mkv").codec == "H264"
    assert parse_info("Show - 01 [HEVC].mkv").codec == "HEVC"


def test_title_only_marker_at_start():
    info = parse_info("S01E05 Some Title.mkv")
    assert info.title == "Some Title"
    assert (info.season, info.episode) == ("01", "05")


def test_split_ext():
    assert split_ext("a.b.mkv") == ("a.b", ".mkv")
    assert split_ext("Season 1.5", known_only=True) == ("Season 1.5", "")
    assert split_ext("Season 1.5") == ("Season 1.5", "")
    assert split_ext("noext") == ("noext", "")
    assert split_ext("x.MKV", known_only=True) == ("x", ".MKV")
    assert split_ext("weird.abc", known_only=True) == ("weird.abc", "")
    assert split_ext("weird.abc") == ("weird", ".abc")


def test_render_template_drops_group_with_empty_variable():
    values = {"title": "Show", "season": "", "episode": "", "quality": "720p", "year": "", "codec": "", "filename": "x", "ext": "mkv"}
    assert render_template(DEFAULT_TEMPLATE, values) == "Show [720p]"
    values["quality"] = ""
    assert render_template(DEFAULT_TEMPLATE, values) == "Show"


def test_render_template_full():
    values = {"title": "Show", "season": "01", "episode": "05", "quality": "720p", "year": "", "codec": "", "filename": "x", "ext": "mkv"}
    assert render_template(DEFAULT_TEMPLATE, values) == "[S01E05] Show [720p]"
    assert render_template("{title} - {episode}", values) == "Show - 05"
    assert render_template("{title}.S{season}E{episode}", values) == "Show.S01E05"


def test_render_template_values_are_not_reparsed():
    values = {"title": "{quality} [x]", "season": "", "episode": "", "quality": "720p", "year": "", "codec": "", "filename": "x", "ext": "mkv"}
    assert render_template("{title} [{quality}]", values) == "{quality} [x] [720p]"


def test_render_template_unknown_token_kept():
    values = {"title": "T", "season": "", "episode": "", "quality": "", "year": "", "codec": "", "filename": "x", "ext": "mkv"}
    assert render_template("{title} {nope}", values) == "T {nope}"


def test_compute_new_name_filename_mode():
    name, info = compute_new_name(
        template=DEFAULT_TEMPLATE,
        mode="filename",
        original_name="[SubsPlease] Tokyo Revengers - 05 (1080p) [ABCD1234].mkv",
        original_ext=".mkv",
    )
    assert name == "[S01E05] Tokyo Revengers [1080p].mkv"
    assert info.episode == "05"


def test_compute_new_name_caption_mode_uses_caption():
    name, _ = compute_new_name(
        template=DEFAULT_TEMPLATE,
        mode="caption",
        original_name="random_file_123.mkv",
        original_ext=".mkv",
        caption="Bleach S01E12 720p",
    )
    assert name == "[S01E12] Bleach [720p].mkv"


def test_compute_new_name_caption_mode_falls_back_to_filename():
    name, _ = compute_new_name(
        template=DEFAULT_TEMPLATE,
        mode="caption",
        original_name="One.Piece.E1000.1080p.mkv",
        original_ext=".mkv",
        caption="Hello everyone, enjoy!",
    )
    assert name == "[S01E1000] One Piece [1080p].mkv"


def test_compute_new_name_caption_fills_missing_from_filename():
    name, _ = compute_new_name(
        template=DEFAULT_TEMPLATE,
        mode="caption",
        original_name="Show.S01E03.1080p.mkv",
        original_ext=".mkv",
        caption="Show Name S01E03",
    )
    assert name == "[S01E03] Show Name [1080p].mkv"


def test_caption_title_from_first_line_details_from_rest():
    info = parse_caption("My Show\nEpisode: 5\nQuality: 720p")
    assert info.title == "My Show"
    assert info.quality == "720p"


def test_compute_new_name_remove_words_and_illegal_chars():
    name, _ = compute_new_name(
        template="{title} {quality}",
        mode="filename",
        original_name="Re:Zero - 03 [720p] @SomeChannel.mkv",
        original_ext=".mkv",
        remove_words=["@SomeChannel"],
    )
    assert name == "Re Zero 720p.mkv"


def test_compute_new_name_keeps_extension_and_no_double_ext():
    name, _ = compute_new_name(
        template="{title}.{ext}",
        mode="filename",
        original_name="Movie.2020.720p.mkv",
        original_ext=".mkv",
    )
    assert name == "Movie.mkv"


def test_compute_new_name_missing_everything_falls_back_to_original():
    name, _ = compute_new_name(
        template="[S{season}E{episode}]",
        mode="filename",
        original_name="holiday video.mp4",
        original_ext=".mp4",
    )
    assert name == "holiday video.mp4"


def test_compute_new_name_uses_original_ext_when_name_has_none():
    name, _ = compute_new_name(
        template="{title}", mode="filename", original_name="Movie", original_ext=".mp4"
    )
    assert name == "Movie.mp4"


def test_finalize_truncates_by_bytes_and_keeps_ext():
    out = finalize_name("é" * 300, ".mkv")
    assert out.endswith(".mkv")
    assert len(out.encode("utf-8")) <= 200


def test_sanitize_and_remove_words():
    assert sanitize("  ..hidden:name  ") == "hidden name"
    assert apply_remove_words("Show [Group] 720p", ["[group]"]) == "Show 720p"


# ---- episode numbers written first / bare / in brackets (e.g. "1147 - One Piece [Dual] 720p @Channel") ----


@pytest.mark.parametrize(
    "name,title,episode,quality",
    [
        ("1147 - One Piece [Dual] 720p @Anime_X_Universe.mkv", "One Piece", "1147", "720p"),
        ("1145 - One Piece [Dual] 720p @Anime_X_Universe.mkv", "One Piece", "1145", "720p"),
        ("1147 - One Piece @Anime_X_Universe.mkv", "One Piece", "1147", ""),
        ("1147-One Piece [720p].mkv", "One Piece", "1147", "720p"),
        ("1147 – One Piece 720p.mkv", "One Piece", "1147", "720p"),
        ("1147_-_One_Piece_[Dual]_720p.mkv", "One Piece", "1147", "720p"),
        ("[Group] 1147 - One Piece 720p.mkv", "One Piece", "1147", "720p"),
        ("1147v2 - One Piece 720p.mkv", "One Piece", "1147", "720p"),
        ("05 - Naruto [720p].mkv", "Naruto", "05", "720p"),
        ("1080 - One Piece [Dual] 720p.mkv", "One Piece", "1080", "720p"),  # 1080 is an episode here, not a resolution
        ("One Piece 1147 720p.mkv", "One Piece", "1147", "720p"),
        ("One.Piece.1147.720p.WEB-DL.mkv", "One Piece", "1147", "720p"),
        ("One Piece 1147 Dual Audio 720p.mkv", "One Piece", "1147", "720p"),
        ("One Piece 1147 [Dual].mkv", "One Piece", "1147", ""),
        ("Naruto Shippuden 500 - Name.mkv", "Naruto Shippuden", "500", ""),
        ("[1147] One Piece 720p.mkv", "One Piece", "1147", "720p"),
        ("One Piece [1147] [720p].mkv", "One Piece", "1147", "720p"),
        ("[Group] Title [05] [720p].mkv", "Title", "05", "720p"),
    ],
)
def test_episode_without_a_marker_word(name, title, episode, quality):
    info = parse_info(name)
    assert (info.title, info.season, info.episode, info.quality) == (title, "01", episode, quality)


@pytest.mark.parametrize(
    "name,title",
    [
        ("3-gatsu no Lion 720p.mkv", "3-gatsu no Lion"),
        ("5-toubun no Hanayome [1080p].mkv", "5-toubun no Hanayome"),
        ("86-Eighty Six 720p.mkv", "86-Eighty Six"),
        ("2001 - A Space Odyssey (1968).mkv", "2001 - A Space Odyssey"),
        ("Blade Runner 2049 1080p BluRay.mkv", "Blade Runner"),
        ("12 Angry Men (1957) 1080p BluRay.mkv", "12 Angry Men"),
        ("300 (2006) 1080p BluRay.mkv", "300"),
        ("Room 237 (2012) 1080p.mkv", "Room 237"),
        ("Apollo 13 (1995) 1080p.mkv", "Apollo 13"),
        ("Spider-Man 3 1080p WEB-DL.mkv", "Spider-Man 3"),
        ("Toy Story 4 720p.mkv", "Toy Story 4"),
        ("Oppenheimer.2023.1080p.BluRay.x265.10bit.mkv", "Oppenheimer"),
        ("Title [2019] [720p].mkv", "Title"),
        ("Some Movie 1920x1080.mkv", "Some Movie"),
        ("Title 10bit 720p.mkv", "Title 10bit"),
    ],
)
def test_numbers_that_belong_to_titles_are_not_episodes(name, title):
    info = parse_info(name)
    assert info.episode == "" and info.title == title


def test_an_explicit_marker_beats_a_leading_number():
    info = parse_info("86 - Eighty Six - 05 (1080p).mkv")
    assert (info.title, info.episode, info.quality) == ("86 - Eighty Six", "05", "1080p")


def test_channel_tags_end_the_title():
    assert parse_info("One Piece @Anime_X_Universe 720p.mkv").title == "One Piece"
    assert parse_info("Some @ Title.mkv").title == "Some @ Title"


def test_the_users_one_piece_files_get_their_episode_number():
    for number in ("1145", "1146", "1147"):
        name, _info = compute_new_name(
            template="One Piece - E{episode}", mode="filename", original_ext=".mkv",
            original_name=f"{number} - One Piece [Dual] 720p @Anime_X_Universe.mkv",
        )
        assert name == f"One Piece - E{number}.mkv"
    name, _ = compute_new_name(
        template=DEFAULT_TEMPLATE, mode="filename", original_ext=".mkv",
        original_name="1147 - One Piece [Dual] 720p @Anime_X_Universe.mkv",
    )
    assert name == "[S01E1147] One Piece [720p].mkv"


def test_missing_variables_only_reports_holes_outside_bracket_groups():
    info = parse_info("Plain Movie.mkv")
    assert missing_variables("One Piece - E{episode}", info) == ["episode"]
    assert missing_variables("S{season}E{episode} {title}", info) == ["season", "episode"]
    assert missing_variables("{title} {quality} {quality}", info) == ["quality"]
    assert missing_variables(DEFAULT_TEMPLATE, info) == []  # bracket groups vanish on purpose
    assert missing_variables("{title} ({year})", info) == []
    assert missing_variables("{title} {nope}", info) == []
    assert missing_variables("One Piece - E{episode}", parse_info("1147 - One Piece 720p.mkv")) == []


# ---------------------------------------------------------------------------
# {1episode} {2episode} {3episode}: the episode with zeros in front
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "detected, zeros, expected",
    [
        ("4", 1, "04"), ("04", 1, "04"), ("14", 1, "14"), ("100", 1, "100"), ("004", 1, "004"),  # {1episode}: at least 2 digits
        ("4", 2, "004"), ("04", 2, "004"), ("004", 2, "004"), ("005", 2, "005"), ("14", 2, "014"), ("1147", 2, "1147"),
        ("4", 3, "0004"), ("04", 3, "0004"), ("004", 3, "0004"), ("12", 3, "0012"),  # {3episode}: at least 4 digits
        ("321", 3, "0321"), ("1321", 3, "1321"), ("1147", 3, "1147"), ("12345", 3, "12345"),  # wide enough: no zero is added
        ("", 1, ""), ("", 3, ""),  # no episode found: nothing to pad
    ],
)
def test_padded_episode(detected, zeros, expected):
    assert padded_episode(detected, zeros) == expected


@pytest.mark.parametrize("written", ["E4", "E04", "E004", "Episode 4", "S01E4", "S01E004"])
def test_the_padded_variables_do_not_depend_on_how_the_episode_was_written(written):
    info = parse_info(f"Show {written} 720p.mkv")
    values = build_values(info, "Show", ".mkv")
    assert [values[name] for name in ("episode", "1episode", "2episode", "3episode")] == ["04", "04", "004", "0004"]
    assert "4episode" not in values  # three padded variables: up to four digits


def test_the_padded_variables_in_a_template():
    name, _ = compute_new_name(
        template="{title} - {1episode} - {2episode} - {3episode}", mode="filename",
        original_name="One Piece - 4 [1080p].mkv", original_ext=".mkv",
    )
    assert name == "One Piece - 04 - 004 - 0004.mkv"
    name, _ = compute_new_name(
        template="{title} {2episode}", mode="filename", original_name="1147 - One Piece [Dual] 720p.mkv", original_ext=".mkv",
    )
    assert name == "One Piece 1147.mkv"  # a number that is wide enough keeps its digits


@pytest.mark.parametrize("original, expected", [
    ("One Piece - 1321 [1080p].mkv", "One Piece 1321.mkv"),  # four digits already: no zero is added
    ("One Piece - 321 [1080p].mkv", "One Piece 0321.mkv"),  # three digits: one zero
    ("One Piece - 21 [1080p].mkv", "One Piece 0021.mkv"),
    ("One Piece - 1 [1080p].mkv", "One Piece 0001.mkv"),
    ("1321 - One Piece [Dual] 720p.mkv", "One Piece 1321.mkv"),
    ("One Piece S01E0321 720p.mkv", "One Piece 0321.mkv"),  # zeros that were already there are not counted twice
])
def test_third_padded_episode_is_four_digits_wide(original, expected):
    name, _ = compute_new_name(template="{title} {3episode}", mode="filename", original_name=original, original_ext=".mkv")
    assert name == expected


def test_a_fourth_padded_variable_does_not_exist():
    values = build_values(parse_info("One Piece - 4 [1080p].mkv"), "One Piece", ".mkv")
    assert "4episode" not in values
    name, _ = compute_new_name(
        template="{title} {4episode}", mode="filename", original_name="One Piece - 4 [1080p].mkv", original_ext=".mkv"
    )
    assert name == "One Piece {4episode}.mkv"  # an unknown variable is left as plain text


def test_without_an_episode_a_group_with_a_padded_variable_goes_away():
    info = parse_info("Plain Movie.mkv")
    values = build_values(info, "Plain Movie", ".mkv")
    assert values["1episode"] == values["3episode"] == ""
    assert render_template("{title} [E{3episode}]", values) == "Plain Movie"
    assert missing_variables("{title} E{2episode}", info) == ["2episode"]  # a bare one leaves a hole in the name
    assert missing_variables("{title} [E{2episode}] {episode} {3episode}", info) == ["episode", "3episode"]
    assert missing_variables("{title} E{2episode}", parse_info("One Piece - 05.mkv")) == []
