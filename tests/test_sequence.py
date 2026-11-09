import pytest

from bot.sequence import MAX_LISTED, SeqItem, analyse, detect, format_report, ranges


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
