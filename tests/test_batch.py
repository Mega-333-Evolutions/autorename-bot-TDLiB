"""The report on a batch of files: how many were renamed and checked, which came with a warning, which needed another try, which failed."""
from types import SimpleNamespace

from bot import batch
from bot.batch import LISTED, NAME_CLIP, SAFE_LENGTH, TEXT_CLIP, Outcome, check_labels, common_checks, format_batch
from bot.pipeline import Result

CHECKS = "size · name · tracks (1 video, 2 audio) · thumbnail"


def fine(n=1, checks=CHECKS, **kw):
    return Outcome(name=f"Show - {n:04d}.mkv", chat_id=7, checks=checks, **kw)


def warned(n, *warnings):
    return Outcome(name=f"Show - {n:04d}.mkv", chat_id=7, checks="size · name", warnings=warnings)


def failed(n, reason="RuntimeError: boom", state="failed"):
    return Outcome(name=f"Show - {n:04d}.mkv", chat_id=7, state=state, reason=reason)


# ---------------------------------------------------------------------------
# what is said about the files that went well
# ---------------------------------------------------------------------------


def test_a_batch_where_everything_went_well_says_so_and_what_was_checked():
    text = format_batch([fine(1), fine(2), fine(3)])
    assert text.startswith("📋 <b>Batch finished:</b> all 3 files renamed\n\n")
    assert "✅ 3 checked after sending: size · name · tracks · thumbnail" in text
    assert not any(sign in text for sign in "⚠❌🔁🖼❔✖")  # nothing else to say


def test_the_labels_of_a_check_are_its_parts_without_the_detail():
    assert check_labels(CHECKS) == ["size", "name", "tracks", "thumbnail"]
    assert check_labels("size · size · name") == ["size", "name"] and check_labels("") == []


def test_only_what_every_file_had_checked_is_claimed():
    assert common_checks([]) == []
    assert common_checks([fine(1, "size · name · thumbnail"), fine(2, "size · name · tracks (2 audio)")]) == ["size", "name"]
    assert common_checks([fine(1, "size · name"), fine(2, "thumbnail")]) == []
    text = format_batch([fine(1, "size · name · thumbnail"), fine(2, "size · name · tracks (2 audio)")])
    assert text.endswith("✅ 2 checked after sending: size · name") and "thumbnail" not in text and "tracks" not in text
    assert format_batch([fine(1, "size"), fine(2, "name")]).endswith("✅ 2 checked after sending")  # no colon with nothing in common


def test_files_that_could_not_be_checked_are_counted_apart_from_the_checked_ones():
    text = format_batch([fine(1), fine(2, checks=""), fine(3, checks="")])
    assert "✅ 1 checked after sending: size · name · tracks · thumbnail" in text
    assert "❔ 2 sent without a check (Telegram gave no details of them)" in text
    assert "❔ 1 sent without a check (Telegram gave no details of it)" in format_batch([fine(1), fine(2, checks="")])


# ---------------------------------------------------------------------------
# what is said about the files that did not
# ---------------------------------------------------------------------------


def test_a_file_with_a_warning_is_renamed_but_is_named_with_what_was_wrong_and_not_counted_as_checked():
    text = format_batch([fine(1), warned(2, "Telegram sent the file without its thumbnail."), fine(3)])
    assert text.startswith("📋 <b>Batch finished:</b> all 3 files renamed")  # it is renamed, with a warning
    assert "✅ 2 checked after sending" in text
    assert "⚠️ 1 renamed, with a warning:\n• <code>Show - 0002.mkv</code>\n  Telegram sent the file without its thumbnail." in text
    assert "Show - 0001" not in text and "Show - 0003" not in text  # the files that went well are not listed


def test_several_warnings_of_one_file_are_said_together():
    text = format_batch([fine(1), warned(2, "First thing.", "Second thing.")])
    assert "  First thing. Second thing." in text


def test_a_failed_file_is_named_with_its_reason_and_the_report_says_how_to_try_again():
    reason = "RPCError: File download has failed or was canceled (stopped short 4 times) (400)"
    text = format_batch([fine(1), failed(46, reason), fine(3)])
    assert text.startswith("📋 <b>Batch finished:</b> 2 of 3 files renamed")
    assert f"❌ 1 failed:\n• <code>Show - 0046.mkv</code>\n  {reason}\nSend the failed ones again to retry them." in text


def test_a_failure_without_a_reason_and_a_job_that_ended_in_no_state_are_failures_too():
    text = format_batch([fine(1), failed(2, reason=""), failed(3, reason="", state="running")])
    assert "❌ 2 failed:" in text and text.count("it stopped without saying why") == 2
    assert text.startswith("📋 <b>Batch finished:</b> 1 of 3 files renamed")


def test_cancelled_files_are_counted_and_are_not_failures():
    text = format_batch([fine(1), fine(2), Outcome(name="x.mkv", chat_id=7, state="cancelled")])
    assert text.startswith("📋 <b>Batch finished:</b> 2 of 3 files renamed") and "✖ 1 cancelled" in text
    assert "❌" not in text and "Send the failed" not in text


def test_files_sent_again_and_files_tried_again_are_counted_in_the_right_number():
    one = format_batch([fine(1, thumb_resent=True, retries=1), fine(2)])
    assert "🖼 1 lost its thumbnail on the way and was sent again" in one
    assert "🔁 1 had to be tried again because Telegram was busy" in one
    many = format_batch([fine(1, thumb_resent=True, retries=2), fine(2, thumb_resent=True), fine(3, retries=1), fine(4)])
    assert "🖼 2 lost their thumbnail on the way and were sent again" in many
    assert "🔁 2 had to be tried again because Telegram was busy" in many
    assert "✅ 4 checked after sending" in many  # (they were checked after the second try, or after the new thumbnail)


# ---------------------------------------------------------------------------
# long lists and odd text
# ---------------------------------------------------------------------------


def test_names_and_reasons_are_not_read_as_formatting_and_are_cut_to_a_length_that_fits():
    odd = Outcome(name="<b>&</b> " + "n" * 200 + ".mkv", chat_id=7, state="failed", reason="<i>boom</i> " + "r" * 400)
    text = format_batch([fine(1), odd])
    assert "&lt;b&gt;&amp;&lt;/b&gt; " in text and "<b>&</b>" not in text and "<i>" not in text
    assert "n" * NAME_CLIP not in text.replace("&lt;b&gt;&amp;&lt;/b&gt; ", "") and "r" * TEXT_CLIP not in text
    assert "…" in text  # what was cut says so


def test_only_the_first_files_of_a_long_list_are_named_and_the_others_are_counted():
    text = format_batch([fine(0)] + [failed(n) for n in range(1, 13)])
    assert "❌ 12 failed:" in text and text.count("• <code>Show") == LISTED
    assert f"…and {12 - LISTED} more" in text
    assert "Show - 0001.mkv" in text and f"Show - {LISTED:04d}.mkv" in text and f"Show - {LISTED + 1:04d}.mkv" not in text  # (the first ones, in order)
    shown = format_batch([fine(0)] + [failed(n) for n in range(1, LISTED + 1)])
    assert "more" not in shown  # nothing to count when all are named


def test_a_report_on_a_huge_batch_is_still_one_message_and_keeps_its_counts():
    hostile = "<" * 300  # (every one becomes four characters)
    outcomes = [Outcome(name=hostile, chat_id=7, state="failed", reason=hostile) for _ in range(400)]
    outcomes += [Outcome(name=hostile, chat_id=7, warnings=(hostile,), checks="size") for _ in range(400)]
    outcomes += [fine(n) for n in range(700)]
    text = format_batch(outcomes)
    assert len(text) <= SAFE_LENGTH
    assert text.startswith("📋 <b>Batch finished:</b> 1100 of 1500 files renamed")
    assert "❌ 400 failed:" in text and "⚠️ 400 renamed, with a warning:" in text and "✅ 700 checked after sending" in text
    assert "Send the failed ones again to retry them." in text  # the summary survives however many files are named


def test_the_number_of_files_named_goes_down_until_the_report_fits(monkeypatch):
    monkeypatch.setattr(batch, "SAFE_LENGTH", 600)
    outcomes = [failed(n, reason="x" * TEXT_CLIP) for n in range(30)]
    text = format_batch(outcomes)
    assert len(text) <= 600 and "❌ 30 failed:" in text and 0 < text.count("• <code>Show") < LISTED
    monkeypatch.setattr(batch, "SAFE_LENGTH", 10)  # whatever is too long to fit, the counts are still sent
    assert format_batch(outcomes).count("• <code>Show") == 0 and "❌ 30 failed:" in format_batch(outcomes)


# ---------------------------------------------------------------------------
# from a job to its outcome
# ---------------------------------------------------------------------------


def test_an_outcome_is_a_copy_of_how_the_job_went():
    job = SimpleNamespace(new_name="A.mkv", chat_id=5)
    result = Result(state="failed", reason="boom", warnings=["w"], checks="size", retries=2, thumb_resent=True)
    outcome = Outcome.of(job, result)
    assert outcome == Outcome(name="A.mkv", chat_id=5, state="failed", reason="boom", warnings=("w",), checks="size", retries=2, thumb_resent=True)
    result.warnings.append("later")
    job.new_name = "B.mkv"
    assert outcome.warnings == ("w",) and outcome.name == "A.mkv"  # what happens to the job afterwards does not change the report
