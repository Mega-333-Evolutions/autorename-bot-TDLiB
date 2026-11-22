import pytest

from bot.sequence import MAX_LISTED, SeqItem, analyse, detect, detect_quality, format_report, ranges


def item(mid, name, caption=""):
    return SeqItem(msg_id=mid, name=name, caption=caption)


def names(items):
    return [i.name for i in items]


# ---------------------------------------------------------------------------
# reading the episode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("1147 - One Piece [Dual] 720p @Anime_X_Universe.mkv", (1, 1147)),
        ("[SubsPlease] Tokyo Revengers - 05 (1080p) [ABCD1234].mkv", (1, 5)),
        ("Show.S02E14.720p.mkv", (2, 14)),
        ("Show 1x07.mkv", (1, 7)),
        ("One Piece [1146] 720p.mkv", (1, 1146)),
        ("Some Movie 2023 1080p.mkv", (None, None)),
        ("holiday video.mp4", (None, None)),
    ],
)
def test_detect_from_the_file_name(name, expected):
    assert detect(name, "", "filename") == expected


def test_the_rename_mode_decides_which_source_is_read_first_and_the_other_is_the_fallback():
    assert detect("Show S01E02.mkv", "Show S01E09", "filename") == (1, 2)
    assert detect("Show S01E02.mkv", "Show S01E09", "caption") == (1, 9)
    assert detect("random name.mkv", "One Piece - 1147", "filename") == (1, 1147)  # nothing in the name: use the caption
    assert detect("Show S01E02.mkv", "just words", "caption") == (1, 2)  # nothing in the caption: use the name
    assert detect("random.mkv", "", "caption") == (None, None)


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Show S01E03 480p.mkv", 480),
        ("Show S01E03 [720p].mkv", 720),
        ("Show.S01E03.1080p.WEB.mkv", 1080),
        ("Show S01E03 1080P.mkv", 1080),
        ("Show S01E03 1920x1080.mkv", 1080),
        ("Show S01E03 1440p.mkv", 1440),
        ("Show S01E03 2K.mkv", 1440),  # 2K is 1440p
        ("Show S01E03 2k.mkv", 1440),
        ("Show S01E03 [2K] Dual.mkv", 1440),
        ("Show S01E03 (2K).mkv", 1440),
        ("Show S01E03_2K_.mkv", 1440),
        ("Show S01E03 2160p.mkv", 2160),
        ("Show S01E03 4K.mkv", 2160),  # 4K is 2160p
        ("Show S01E03 UHD.mkv", 2160),
        ("Show S01E03 2K 1080p.mkv", 1080),  # a picture height that is written out wins over the nickname
        ("Show S01E03 12K.mkv", None),  # 2K is a word of its own, not the end of another number or name
        ("Show S01E03 B2KX.mkv", None),
        ("Show S01E03 x264 10bit.mkv", None),
        ("Show S01E03.mkv", None),
    ],
)
def test_detect_quality_from_the_file_name(name, expected):
    assert detect_quality(name, "", "filename") == expected


def test_the_quality_is_read_from_the_same_source_as_the_episode():
    assert detect_quality("Show S01E03 480p.mkv", "Show S01E03 1080p", "filename") == 480
    assert detect_quality("Show S01E03 480p.mkv", "Show S01E03 1080p", "caption") == 1080
    assert detect_quality("Show S01E03.mkv", "Show S01E03 720p", "filename") == 720  # nothing in the name: use the caption
    assert detect_quality("Show S01E03 480p.mkv", "just words", "caption") == 480  # nothing in the caption: use the name
    assert detect_quality("Show S01E03 2K.mkv", "Show S01E03 720p", "filename") == 1440
    assert detect_quality("Show S01E03.mkv", "Show S01E03 4K", "filename") == 2160
    assert detect_quality("Show S01E03.mkv", "", "caption") is None


# ---------------------------------------------------------------------------
# ordering
# ---------------------------------------------------------------------------


def test_files_come_out_in_ascending_episode_order_whatever_order_they_were_sent():
    sent = [item(5, "1147 - One Piece.mkv"), item(6, "1145 - One Piece.mkv"), item(7, "1146 - One Piece.mkv"), item(8, "1148 - One Piece.mkv")]
    a = analyse(sent, "filename")
    assert names(a.ordered) == ["1145 - One Piece.mkv", "1146 - One Piece.mkv", "1147 - One Piece.mkv", "1148 - One Piece.mkv"]
    assert a.missing == {} and a.repeated == {} and a.unknown == []
    assert a.first == (1, 1145) and a.last == (1, 1148)


def test_episode_ten_sorts_after_nine_not_after_one():
    a = analyse([item(1, "Show - 10.mkv"), item(2, "Show - 9.mkv"), item(3, "Show - 1.mkv"), item(4, "Show - 100.mkv")], "filename")
    assert [i.episode for i in a.ordered] == [1, 9, 10, 100]


def test_seasons_sort_before_episodes():
    sent = [item(1, "Show S02E01.mkv"), item(2, "Show S01E03.mkv"), item(3, "Show S01E01.mkv"), item(4, "Show S02E02.mkv")]
    a = analyse(sent, "filename")
    assert [(i.season, i.episode) for i in a.ordered] == [(1, 1), (1, 3), (2, 1), (2, 2)]
    assert a.missing == {1: [2]}  # S01E02 is missing; the jump from S01 to S02 is not a gap
    assert a.first == (1, 1) and a.last == (2, 2)


def test_files_without_an_episode_number_go_last_in_the_order_they_were_sent():
    sent = [item(9, "zzz.mkv"), item(3, "Show - 02.mkv"), item(4, "aaa.mkv"), item(2, "Show - 01.mkv")]
    a = analyse(sent, "filename")
    assert names(a.ordered) == ["Show - 01.mkv", "Show - 02.mkv", "aaa.mkv", "zzz.mkv"]
    assert names(a.unknown) == ["aaa.mkv", "zzz.mkv"]


def test_nothing_is_numbered():
    a = analyse([item(1, "b.mkv"), item(2, "a.mkv")], "filename")
    assert names(a.ordered) == ["b.mkv", "a.mkv"] and a.first is None and a.last is None and a.missing == {}


def test_the_same_episode_number_in_two_seasons_sorts_season_one_first_then_season_two():
    # S2E03 was sent before S1E03, and S2E03 is the smaller file name when sorted as text
    a = analyse([item(1, "Show S02E03.mkv"), item(2, "Show S01E03.mkv"), item(3, "Show S01E04.mkv"), item(4, "Show S02E04.mkv")], "filename")
    assert names(a.ordered) == ["Show S01E03.mkv", "Show S01E04.mkv", "Show S02E03.mkv", "Show S02E04.mkv"]
    # the season comes before the quality: a 1080p of season 1 is still sent before a 480p of season 2
    b = analyse([item(1, "Show S02E03 480p.mkv"), item(2, "Show S01E03 1080p.mkv")], "filename")
    assert names(b.ordered) == ["Show S01E03 1080p.mkv", "Show S02E03 480p.mkv"]
    assert b.repeated == {}  # S1E03 and S2E03 are different episodes


def test_the_same_episode_in_several_qualities_sorts_480p_720p_1080p_2k_4k():
    sent = [
        item(1, "Show S01E03 2160p.mkv"),
        item(2, "Show S01E03 720p.mkv"),
        item(3, "Show S01E03 2K.mkv"),
        item(4, "Show S01E03 480p.mkv"),
        item(5, "Show S01E03 1080p.mkv"),
    ]
    assert names(analyse(sent, "filename").ordered) == [
        "Show S01E03 480p.mkv",
        "Show S01E03 720p.mkv",
        "Show S01E03 1080p.mkv",
        "Show S01E03 2K.mkv",
        "Show S01E03 2160p.mkv",
    ]
    # the other names of the same pictures sort the same way
    other = [item(1, "Show S01E03 4K.mkv"), item(2, "Show S01E03 UHD.mkv"), item(3, "Show S01E03 1440p.mkv"), item(4, "Show S01E03 576p.mkv")]
    assert [i.quality for i in analyse(other, "filename").ordered] == [576, 1440, 2160, 2160]


def test_the_episode_comes_before_the_quality():
    sent = [item(1, "Show - 04 480p.mkv"), item(2, "Show - 03 2160p.mkv"), item(3, "Show - 03 480p.mkv"), item(4, "Show - 04 1080p.mkv")]
    assert names(analyse(sent, "filename").ordered) == ["Show - 03 480p.mkv", "Show - 03 2160p.mkv", "Show - 04 480p.mkv", "Show - 04 1080p.mkv"]


def test_a_file_without_a_quality_goes_after_the_qualities_of_its_episode_and_before_the_next_episode():
    sent = [item(1, "Show - 04 480p.mkv"), item(2, "Show - 03.mkv"), item(3, "Show - 03 1080p.mkv"), item(4, "Show - 03 480p.mkv")]
    assert names(analyse(sent, "filename").ordered) == ["Show - 03 480p.mkv", "Show - 03 1080p.mkv", "Show - 03.mkv", "Show - 04 480p.mkv"]


def test_files_with_the_same_episode_and_quality_keep_the_order_they_were_sent_in():
    sent = [item(7, "Show - 03 720p [B].mkv"), item(3, "Show - 03 720p [A].mkv"), item(9, "Show - 03 720p [C].mkv")]
    assert names(analyse(sent, "filename").ordered) == ["Show - 03 720p [A].mkv", "Show - 03 720p [B].mkv", "Show - 03 720p [C].mkv"]


def test_seasons_episodes_and_qualities_together():
    sent = [
        item(1, "Show S02E01 1080p.mkv"),
        item(2, "Show S01E03 720p.mkv"),
        item(3, "Show S02E01 480p.mkv"),
        item(4, "Show S01E02 1080p.mkv"),
        item(5, "Show S01E03 480p.mkv"),
        item(6, "Show S01E03 2K.mkv"),
        item(7, "Show S01E03 1080p.mkv"),
        item(8, "Show S01E03 4K.mkv"),
        item(9, "Show S01E03.mkv"),
        item(10, "Show special.mkv"),
    ]
    a = analyse(sent, "filename")
    assert [(i.season, i.episode, i.quality) for i in a.ordered] == [
        (1, 2, 1080),
        (1, 3, 480),
        (1, 3, 720),
        (1, 3, 1080),
        (1, 3, 1440),
        (1, 3, 2160),
        (1, 3, None),
        (2, 1, 480),
        (2, 1, 1080),
        (None, None, None),  # no episode number: after everything else
    ]
    assert a.first == (1, 2) and a.last == (2, 1) and a.missing == {}


# ---------------------------------------------------------------------------
# gaps and repeats
# ---------------------------------------------------------------------------


def test_missing_episodes_are_the_gaps_between_the_first_and_the_last():
    sent = [item(i, f"One Piece - {n}.mkv") for i, n in enumerate((1145, 1146, 1148, 1151, 1152, 1156), start=1)]
    a = analyse(sent, "filename")
    assert a.missing == {1: [1147, 1149, 1150, 1153, 1154, 1155]}
    # an episode before the first one you sent is not "missing"
    assert analyse([item(1, "S - 5.mkv"), item(2, "S - 6.mkv")], "filename").missing == {}
    assert analyse([item(1, "S - 5.mkv")], "filename").missing == {}


def test_repeated_episodes_list_every_file_that_shares_the_number():
    sent = [item(1, "Show - 05 [A].mkv"), item(2, "Show - 05 [B].mkv"), item(3, "Show - 06.mkv"), item(4, "Show - 05 [C].mkv")]
    a = analyse(sent, "filename")
    assert list(a.repeated) == [(1, 5)] and names(a.repeated[(1, 5)]) == ["Show - 05 [A].mkv", "Show - 05 [B].mkv", "Show - 05 [C].mkv"]
    assert names(a.ordered)[:3] == ["Show - 05 [A].mkv", "Show - 05 [B].mkv", "Show - 05 [C].mkv"]  # both copies are kept, in send order
    assert a.missing == {}


def test_the_same_episode_number_in_different_seasons_is_not_a_repeat():
    a = analyse([item(1, "Show S01E01.mkv"), item(2, "Show S02E01.mkv")], "filename")
    assert a.repeated == {}


def test_the_same_episode_in_another_quality_is_a_version_of_it_not_a_repeat():
    a = analyse([item(1, "Show S01E03 480p.mkv"), item(2, "Show S01E03 720p.mkv"), item(3, "Show S01E03 1080p.mkv"), item(4, "Show S01E04 1080p.mkv")], "filename")
    assert a.repeated == {} and a.missing == {}
    assert a.first == (1, 3) and a.last == (1, 4)
    # a file that does not say its quality is not a copy of one that does
    assert analyse([item(1, "Show - 03.mkv"), item(2, "Show - 03 720p.mkv")], "filename").repeated == {}


def test_the_same_episode_twice_in_the_same_quality_is_a_repeat_and_only_those_files_are_listed():
    sent = [item(1, "Show - 03 720p [A].mkv"), item(2, "Show - 03 1080p.mkv"), item(3, "Show - 03 720p [B].mkv"), item(4, "Show - 03 480p.mkv")]
    a = analyse(sent, "filename")
    assert list(a.repeated) == [(1, 3)] and names(a.repeated[(1, 3)]) == ["Show - 03 720p [A].mkv", "Show - 03 720p [B].mkv"]
    assert names(a.ordered) == ["Show - 03 480p.mkv", "Show - 03 720p [A].mkv", "Show - 03 720p [B].mkv", "Show - 03 1080p.mkv"]


def test_two_names_of_the_same_quality_count_as_the_same_quality():
    assert list(analyse([item(1, "Show - 03 4K.mkv"), item(2, "Show - 03 2160p.mkv")], "filename").repeated) == [(1, 3)]
    assert list(analyse([item(1, "Show - 03 2K.mkv"), item(2, "Show - 03 1440p.mkv")], "filename").repeated) == [(1, 3)]
    assert list(analyse([item(1, "Show - 03.mkv"), item(2, "Show - 03 [Dual].mkv")], "filename").repeated) == [(1, 3)]  # neither says its quality


def test_ranges():
    assert ranges([3, 7, 8, 9]) == "E03, E07–E09"
    assert ranges([1147]) == "E1147"
    assert ranges([9, 3, 4]) == "E03–E04, E09"
    assert ranges([]) == ""


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------


def reports(files, mode="filename", failed=()):
    sent = [item(i, n) for i, n in enumerate(files, start=1)]
    a = analyse(sent, mode)
    return format_report(a, len(sent), len(sent) - len(failed), [item(0, n) for n in failed])


def report(files, mode="filename", failed=()):
    messages = reports(files, mode, failed)
    assert len(messages) == 1
    return messages[0]


def test_a_clean_sequence_says_so():
    text = report(["One Piece - 1145.mkv", "One Piece - 1146.mkv", "One Piece - 1147.mkv"])
    assert "Sorted 3 files" in text and "E1145 → E1147" in text and "No missing or repeated episodes." in text
    assert "Missing" not in text and "Repeated" not in text


def test_files_without_a_number_do_not_hide_that_the_rest_is_complete():
    text = report(["S - 01.mkv", "S - 02.mkv", "notes.mkv"])
    assert "No missing or repeated episodes." in text and "No episode number found" in text


def test_nothing_numbered_means_nothing_to_say_about_gaps():
    text = report(["a.mkv", "b.mkv"])
    assert "No missing or repeated" not in text and "No episode number found" in text and "→" not in text


def test_one_file_is_not_plural():
    assert "Sorted 1 file</b>" in report(["One Piece - 1145.mkv"])


def test_the_report_lists_missing_repeated_and_unnumbered_files():
    text = report(["S - 01.mkv", "S - 02 a.mkv", "S - 02 b.mkv", "S - 05.mkv", "S - 08.mkv", "notes.mkv"])
    assert "Sorted 6 files" in text and "E01 → E08" in text
    assert "Missing episodes</b>\nE03–E04, E06–E07" in text
    assert "Repeated episodes" in text and "E02 ×2" in text and "S - 02 a.mkv" in text and "S - 02 b.mkv" in text
    assert "No episode number found" in text and "notes.mkv" in text
    assert "No missing or repeated" not in text


def test_the_report_does_not_call_other_qualities_of_an_episode_repeats():
    text = report(["S - 01 480p.mkv", "S - 01 720p.mkv", "S - 01 1080p.mkv", "S - 02 1080p.mkv"])
    assert "Sorted 4 files" in text and "E01 → E02" in text and "No missing or repeated episodes." in text
    assert "Repeated" not in text
    # two copies of one quality are still reported, and only those two
    text = report(["S - 01 480p.mkv", "S - 01 720p [a].mkv", "S - 01 720p [b].mkv"])
    assert "E01 ×2" in text and "720p [a]" in text and "720p [b]" in text and "480p" not in text.split("Repeated")[1]


def test_the_report_labels_seasons_only_when_there_are_several():
    one = report(["S S01E01.mkv", "S S01E03.mkv"])
    assert "E02" in one and "S01E02" not in one
    two = report(["S S01E01.mkv", "S S01E03.mkv", "S S02E01.mkv", "S S02E02.mkv", "S S02E02 x.mkv"])
    assert "S01: E02" in two and "S02E02 ×2" in two and "S01E01 → S02E02" in two


def test_the_report_mentions_files_that_could_not_be_sent():
    text = report(["S - 01.mkv", "S - 02.mkv"], failed=["S - 02.mkv"])
    assert "Sorted 1 of 2 files" in text and "Couldn't be sent" in text and "S - 02.mkv" in text


def test_names_are_escaped_and_long_lists_are_cut():
    files = ["a&b <x>.mkv"] + [f"Dup {n}.mkv" for n in range(40)]
    text = report(files)
    assert "a&amp;b &lt;x&gt;.mkv" in text and "<x>" not in text
    assert "… and 29 more" in text and text.count("• ") == MAX_LISTED


def test_ranges_are_cut_when_there_are_too_many_gaps():
    text = ranges(range(2, 2000, 2), limit=300)
    assert len(text) <= 300 and text.endswith(" more") and text.startswith("E02, E04")
    shown = text.count("E")
    assert shown > 10
    assert ranges([3, 7], limit=300) == "E03, E07"


def test_a_huge_report_is_split_into_messages_telegram_accepts():
    files = []
    files += [f"Long Running Show Name With A Rather Long Title - {n}.mkv" for n in range(1, 400, 2)]  # 200 gaps
    for n in range(1, 30):  # many repeated numbers
        files += [f"Another Show S02E{n:02d} [v{k}] with a very long name indeed.mkv" for k in range(5)]
    files += [f"unknown file number {n} with quite a long name for a file.mkv" for n in range(50)]
    messages = reports(files, failed=[f"deleted file {n}.mkv" for n in range(30)])
    assert len(messages) > 1
    assert all(len(m) <= 4000 for m in messages)
    joined = "\n\n".join(messages)
    assert "Missing episodes" in joined and "Repeated episodes" in joined and "No episode number found" in joined and "Couldn't be sent" in joined
    assert messages[0].startswith("✅ <b>Sorted")
