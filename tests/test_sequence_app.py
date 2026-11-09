"""/sequence (collect files) and /sort (send them back in episode order, then report gaps and repeats)."""
import asyncio
import time

import pytest

from bot import app as appmod
from bot.tg import errors

from .appkit import ADMIN_CFG, OWNER, STRANGER, click, data_of, say, send_doc

OP = "{n} - One Piece [Dual] 720p @Anime_X_Universe.mkv"


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(appmod, "SEQUENCE_STATUS_DELAY", 0.02)
    monkeypatch.setattr(appmod, "SORT_GAP", 0)
    monkeypatch.setattr(appmod, "SORT_SLOW_GAP", 0)


async def settle(delay=0.12):
    await asyncio.sleep(delay)


async def feed(ctx, numbers, uid=OWNER, first_id=100):
    """Send One Piece episodes in this order; returns {episode: message id} (the last id wins for repeats)."""
    ids = {}
    for k, n in enumerate(numbers):
        await send_doc(ctx, uid, OP.format(n=n), mid=first_id + k)
        ids[n] = first_id + k
    return ids


def sent_ids(ctx, uid=OWNER):
    return [f.id for entity, f, _kw in ctx.client.files if entity == uid]


def texts(ctx):
    return [t for _e, t, _k in ctx.client.sent]


def last_text(ctx):
    return ctx.client.sent[-1][1]


# ---------------------------------------------------------------------------
# collecting
# ---------------------------------------------------------------------------


async def test_sequence_asks_for_files_and_collects_them_instead_of_renaming(ctx):
    ev = await say(ctx, OWNER, "/sequence")
    assert "SEQUENCE" in ev.texts[0] and "/sort" in ev.texts[0] and data_of(ev.out[0][1]) == ["sq:cx"]
    assert ctx.app.modes[OWNER].kind == "sequence"

    for k, n in enumerate((1147, 1145, 1146)):
        got = await send_doc(ctx, OWNER, OP.format(n=n), mid=100 + k)
        assert got.out == []  # nothing is queued, nothing is said per file
    assert ctx.app.queue.waiting == 0 and ctx.app.queue.running == 0
    assert [i.msg_id for i in ctx.app.modes[OWNER].data.items] == [100, 101, 102]


async def test_one_status_message_follows_each_burst_of_files_and_replaces_the_last(ctx):
    await say(ctx, OWNER, "/sequence")
    await feed(ctx, (1, 2, 3))
    await settle()
    (status,) = [(t, k) for _e, t, k in ctx.client.sent if "collected" in t]
    assert "3 files collected" in status[0] and "/sort" in status[0]
    assert data_of(status[1]["buttons"]) == ["sq:sort", "sq:cx"]
    assert ctx.client.deleted == []

    await send_doc(ctx, OWNER, OP.format(n=4), mid=200)
    await settle()
    assert [t for t in texts(ctx) if "collected" in t][-1].startswith("📥 <b>4 files collected")
    assert ctx.client.deleted == [(OWNER, [2001])]  # the old status goes, so the newest one is at the bottom


async def test_a_second_sequence_command_keeps_the_files_already_collected(ctx):
    await say(ctx, OWNER, "/sequence")
    await feed(ctx, (1, 2))
    ev = await say(ctx, OWNER, "/sequence")
    assert "already collecting" in ev.texts[0] and "2 files collected" in ev.texts[0] and data_of(ev.out[0][1]) == ["sq:sort", "sq:cx"]
    assert len(ctx.app.modes[OWNER].data.items) == 2


async def test_the_limit_is_announced_once_and_extra_files_are_ignored(ctx, monkeypatch):
    monkeypatch.setattr(appmod, "SEQUENCE_MAX", 3)
    await say(ctx, OWNER, "/sequence")
    replies = []
    for k in range(6):
        replies += (await send_doc(ctx, OWNER, OP.format(n=k + 1), mid=100 + k)).texts
    assert len(ctx.app.modes[OWNER].data.items) == 3
    assert len(replies) == 1 and "limit: 3 files" in replies[0]
    await settle()
    assert "3 files collected" in texts(ctx)[-1] and "most I can sort" in texts(ctx)[-1]


async def test_a_thousand_files_can_be_collected_and_the_next_one_is_refused(ctx):
    assert appmod.SEQUENCE_MAX == 1000
    ev = await say(ctx, OWNER, "/sequence")
    assert "up to 1000" in ev.texts[0]
    replies = []
    for k in range(1001):
        replies += (await send_doc(ctx, OWNER, OP.format(n=k + 1), mid=100 + k)).texts
    assert len(ctx.app.modes[OWNER].data.items) == 1000
    assert len(replies) == 1 and "limit: 1000 files" in replies[0]
    await settle()
    assert "1000 files collected" in texts(ctx)[-1] and "most I can sort at once (1000)" in texts(ctx)[-1]


async def test_every_user_has_their_own_sequence(ctx):
    await say(ctx, OWNER, "/sequence")
    await say(ctx, ADMIN_CFG, "/sequence")
    await feed(ctx, (3, 1, 2), uid=OWNER, first_id=100)
    await feed(ctx, (9, 8), uid=ADMIN_CFG, first_id=500)
    await say(ctx, ADMIN_CFG, "/sort")
    assert sent_ids(ctx, ADMIN_CFG) == [501, 500] and sent_ids(ctx, OWNER) == []
    await say(ctx, OWNER, "/sort")
    assert sent_ids(ctx, OWNER) == [101, 102, 100]


async def test_strangers_cannot_start_a_sequence(ctx):
    ev = await say(ctx, STRANGER, "/sequence")
    assert ev.out == [] and STRANGER not in ctx.app.modes


# ---------------------------------------------------------------------------
# /sort
# ---------------------------------------------------------------------------


async def test_sort_sends_the_files_back_in_ascending_episode_order(ctx):
    await say(ctx, OWNER, "/sequence")
    ids = await feed(ctx, (1147, 1145, 1148, 1146))
    await say(ctx, OWNER, "/sort")
    assert sent_ids(ctx) == [ids[1145], ids[1146], ids[1147], ids[1148]]
    assert OWNER not in ctx.app.modes
    assert "Sorted 4 files" in last_text(ctx) and "E1145 → E1148" in last_text(ctx) and "No missing or repeated episodes." in last_text(ctx)


async def test_sort_reports_missing_and_repeated_episodes_after_the_files(ctx):
    await say(ctx, OWNER, "/sequence")
    await feed(ctx, (1147, 1145, 1149, 1146, 1146, 1152))
    await say(ctx, OWNER, "/sort")
    assert len(sent_ids(ctx)) == 6
    report = last_text(ctx)
    assert "Sorted 6 files" in report and "E1145 → E1152" in report
    assert "Missing episodes</b>\nE1148, E1150–E1151" in report
    assert "Repeated episodes" in report and "E1146 ×2" in report
    assert "No missing or repeated" not in report
    assert ctx.client.sent[-1][2]["parse_mode"] == "html"


async def test_files_without_an_episode_number_are_sent_last_and_listed(ctx):
    await say(ctx, OWNER, "/sequence")
    await send_doc(ctx, OWNER, "notes.mkv", mid=100)
    await send_doc(ctx, OWNER, OP.format(n=2), mid=101)
    await send_doc(ctx, OWNER, OP.format(n=1), mid=102)
    await say(ctx, OWNER, "/sort")
    assert sent_ids(ctx) == [102, 101, 100]
    assert "No episode number found" in last_text(ctx) and "notes.mkv" in last_text(ctx)


async def test_captions_and_their_formatting_travel_with_each_file(ctx):
    await say(ctx, OWNER, "/sequence")
    await send_doc(ctx, OWNER, OP.format(n=2), mid=100, caption="Episode two <b>bold</b>")
    await send_doc(ctx, OWNER, OP.format(n=1), mid=101)
    ctx.client.messages[100].entities = ["BOLD-ENTITY"]
    await say(ctx, OWNER, "/sort")
    (_e1, first, kw1), (_e2, second, kw2) = ctx.client.files
    assert (first.id, second.id) == (101, 100)
    assert kw1["caption"] == "" and kw2["caption"] == "Episode two <b>bold</b>"
    assert kw2["formatting_entities"] == ["BOLD-ENTITY"] and kw2["parse_mode"] is None  # the text is not parsed a second time


async def test_sort_fetches_the_messages_again_so_old_file_references_still_work(ctx):
    await say(ctx, OWNER, "/sequence")
    await feed(ctx, (2, 1, 3))
    await say(ctx, OWNER, "/sort")
    (chat, ids), = ctx.client.get_calls
    assert chat == OWNER and sorted(ids) == [100, 101, 102] and ids == [101, 100, 102]


async def test_sort_looks_the_files_up_a_hundred_at_a_time_and_keeps_the_order(ctx):
    # One request for more than 300 ids makes Telethon wait 10 s between its chunks of 100: with a thousand files that is
    # a minute and a half of nothing before the first file arrives.  Looking them up as the sort goes avoids it.
    await say(ctx, OWNER, "/sequence")
    at = await feed(ctx, range(250, 0, -1))  # newest first; at[episode] is its message id
    await say(ctx, OWNER, "/sort")
    assert sent_ids(ctx) == [at[n] for n in range(1, 251)]
    assert [len(asked) for _chat, asked in ctx.client.get_calls] == [100, 100, 50]
    assert [asked[0] for _chat, asked in ctx.client.get_calls] == [at[1], at[101], at[201]]
    assert "Sorted 250 files" in last_text(ctx) and "E01 → E250" in last_text(ctx) and "No missing or repeated" in last_text(ctx)


async def test_lookups_follow_the_sending_instead_of_all_coming_first(ctx):
    await say(ctx, OWNER, "/sequence")
    await feed(ctx, range(1, 151))
    sent_so_far = []
    original = ctx.client.get_messages

    async def lookup(entity, ids=None, **kw):
        sent_so_far.append(len(ctx.client.files))
        return await original(entity, ids=ids, **kw)

    ctx.client.get_messages = lookup
    await say(ctx, OWNER, "/sort")
    assert sent_so_far == [0, 100]  # the second hundred is looked up once the first hundred is out


async def test_a_chunk_that_cannot_be_looked_up_is_listed_and_the_rest_still_arrives(ctx, caplog):
    await say(ctx, OWNER, "/sequence")
    at = await feed(ctx, range(1, 251))
    original = ctx.client.get_messages

    async def flaky(entity, ids=None, **kw):
        if ids[0] == at[101]:  # the second chunk: episodes 101-200
            raise RuntimeError("MESSAGE_ID_INVALID")
        return await original(entity, ids=ids, **kw)

    ctx.client.get_messages = flaky
    with caplog.at_level("ERROR"):
        await say(ctx, OWNER, "/sort")
    assert sent_ids(ctx) == [at[n] for n in (*range(1, 101), *range(201, 251))]
    report = last_text(ctx)
    assert "Sorted 150 of 250 files" in report and "Couldn't be sent" in report and "… and 88 more" in report
    assert any("could not look up 100 files" in r.getMessage() for r in caplog.records)  # the log channel hears about it
    assert OWNER not in ctx.app._sorting


async def test_the_rename_mode_decides_where_the_episode_is_read_from(ctx):
    await say(ctx, OWNER, "/mode caption")
    await say(ctx, OWNER, "/sequence")
    await send_doc(ctx, OWNER, "video.mkv", mid=100, caption="One Piece - 1147")
    await send_doc(ctx, OWNER, "other.mkv", mid=101, caption="One Piece - 1145")
    await send_doc(ctx, OWNER, "clip.mkv", mid=102, caption="One Piece - 1146")
    await say(ctx, OWNER, "/sort")
    assert sent_ids(ctx) == [101, 102, 100]
    assert "E1145 → E1147" in last_text(ctx)


async def test_sort_with_nothing_collected_explains_how_it_works(ctx):
    for _ in range(2):
        await say(ctx, OWNER, "/sort")
        assert "Nothing to sort yet" in last_text(ctx) and "/sequence" in last_text(ctx)
        await say(ctx, OWNER, "/sequence")  # started but empty: still nothing to sort
    assert ctx.client.files == []


async def test_after_a_sort_files_are_renamed_as_usual_again(ctx):
    await say(ctx, OWNER, "/sequence")
    await feed(ctx, (1, 2))
    await say(ctx, OWNER, "/sort")
    ev = await send_doc(ctx, OWNER, OP.format(n=3), mid=300)
    assert "Queued" in ev.texts[0] and ctx.app.queue.waiting == 1
    # and a new sequence starts from scratch
    await say(ctx, OWNER, "/sequence")
    await feed(ctx, (7,), first_id=400)
    await say(ctx, OWNER, "/sort")
    assert sent_ids(ctx)[-1] == 400 and "Sorted 1 file" in last_text(ctx)


async def test_a_sequence_that_nobody_finished_is_forgotten_after_its_time(ctx):
    await say(ctx, OWNER, "/sequence")
    await feed(ctx, (1, 2))
    ctx.app.modes[OWNER].expires = time.time() - 1
    await say(ctx, OWNER, "/sort")
    assert "Nothing to sort yet" in last_text(ctx) and ctx.client.files == []
    ev = await send_doc(ctx, OWNER, OP.format(n=3), mid=300)
    assert "Queued" in ev.texts[0]


async def test_every_new_file_gives_the_sequence_more_time(ctx):
    await say(ctx, OWNER, "/sequence")
    mode = ctx.app.modes[OWNER]
    mode.expires = time.time() + 5
    await feed(ctx, (1,))
    assert mode.expires > time.time() + appmod.SEQUENCE_WAIT - 60


async def test_the_status_message_is_removed_and_a_progress_message_replaces_it(ctx, monkeypatch):
    monkeypatch.setattr(appmod, "SORT_PROGRESS_EVERY", 2)
    await say(ctx, OWNER, "/sequence")
    await feed(ctx, (5, 4, 3, 2, 1))
    await settle()
    status_id = [2000 + i + 1 for i, (_e, t, _k) in enumerate(ctx.client.sent) if "collected" in t][-1]
    await say(ctx, OWNER, "/sort")
    assert (OWNER, [status_id]) in ctx.client.deleted
    progress = [t for _c, t, _k in ctx.client.edited]
    assert progress == ["🔢 <b>Sorting…</b> 2 of 5 sent", "🔢 <b>Sorting…</b> 4 of 5 sent"]
    sorting_id = [2000 + i + 1 for i, (_e, t, _k) in enumerate(ctx.client.sent) if "Sorting 5 files" in t][0]
    assert (OWNER, [sorting_id]) in ctx.client.deleted  # the progress message goes before the report


# ---------------------------------------------------------------------------
# buttons
# ---------------------------------------------------------------------------


async def test_the_sort_button_does_the_same_as_the_command(ctx):
    await say(ctx, OWNER, "/sequence")
    await feed(ctx, (3, 1, 2))
    cb = await click(ctx, OWNER, "sq:sort")
    assert cb.answers and sent_ids(ctx) == [101, 102, 100] and "Sorted 3 files" in last_text(ctx)


async def test_the_discard_button_throws_the_files_away(ctx):
    await say(ctx, OWNER, "/sequence")
    await feed(ctx, (3, 1))
    cb = await click(ctx, OWNER, "sq:cx")
    assert "2 files discarded" in cb.edits[-1][0] and cb.edits[-1][1] == []
    assert OWNER not in ctx.app.modes
    await say(ctx, OWNER, "/sort")
    assert "Nothing to sort yet" in last_text(ctx)


async def test_old_buttons_say_the_sequence_is_over(ctx):
    for data in ("sq:sort", "sq:cx"):
        cb = await click(ctx, OWNER, data)
        assert cb.alerts == ["That sequence is over."]
    assert ctx.client.files == [] and ctx.client.sent == []


# ---------------------------------------------------------------------------
# /cancel
# ---------------------------------------------------------------------------


async def test_cancel_discards_the_collected_files(ctx):
    await say(ctx, OWNER, "/sequence")
    await feed(ctx, (2, 1))
    await settle()
    status_id = 2001  # the "2 files collected" message
    before = len([t for t in texts(ctx) if "collected" in t])
    ev = await say(ctx, OWNER, "/cancel")
    assert "Sequence cancelled" in ev.texts[0] and "2 files discarded" in ev.texts[0]
    assert (OWNER, [status_id]) in ctx.client.deleted  # its buttons would be dead, so it goes
    await say(ctx, OWNER, "/sort")
    assert "Nothing to sort yet" in last_text(ctx)
    await settle()
    assert len([t for t in texts(ctx) if "collected" in t]) == before == 1  # no new status after the cancel
    ev = await send_doc(ctx, OWNER, OP.format(n=1), mid=300)
    assert "Queued" in ev.texts[0]


async def test_cancel_leaves_queued_jobs_for_the_next_cancel(ctx):
    await send_doc(ctx, OWNER, OP.format(n=9), mid=50)
    assert ctx.app.queue.waiting == 1
    await say(ctx, OWNER, "/sequence")
    await feed(ctx, (2,))
    ev = await say(ctx, OWNER, "/cancel")
    assert "Sequence cancelled" in ev.texts[0] and "/cancel again" in ev.texts[0] and ctx.app.queue.waiting == 1
    ev = await say(ctx, OWNER, "/cancel")
    assert "Cancelling 1 job" in ev.texts[0]


async def test_cancel_stops_a_sort_that_is_sending(ctx):
    await say(ctx, OWNER, "/sequence")
    await feed(ctx, (5, 4, 3, 2, 1))
    gate = asyncio.Event()
    original = ctx.client.send_file
    calls = []

    async def slow(entity, file, **kw):
        calls.append(file.id)
        if len(calls) == 2:
            await gate.wait()
        return await original(entity, file, **kw)

    ctx.client.send_file = slow
    sorting = asyncio.ensure_future(say(ctx, OWNER, "/sort"))
    for _ in range(200):  # until the second file is in flight (a busy machine may need longer than a fixed pause)
        if len(calls) >= 2:
            break
        await settle(0.01)
    assert calls == [104, 103]  # episode 1, then 2 is in flight

    again = await say(ctx, OWNER, "/sort")  # a second /sort while one is running
    assert again.out == [] and "still sorting" in last_text(ctx)
    ev = await say(ctx, OWNER, "/cancel")
    assert "Stopping the sort" in ev.texts[0]
    gate.set()
    await sorting
    assert sent_ids(ctx) == [104, 103] and "Stopped after 2 of 5 files" in last_text(ctx)
    assert "Sorted" not in last_text(ctx) and OWNER not in ctx.app._sorting


# ---------------------------------------------------------------------------
# things going wrong while sending
# ---------------------------------------------------------------------------


async def test_a_short_flood_wait_is_waited_out_and_the_file_is_sent_again(ctx):
    slept = []

    async def pause(seconds):
        slept.append(seconds)

    ctx.app._pause = pause
    await say(ctx, OWNER, "/sequence")
    await feed(ctx, (2, 1, 3))
    ctx.client.file_errors[100] = [errors.FloodWaitError(90)]
    await say(ctx, OWNER, "/sort")
    assert sent_ids(ctx) == [101, 100, 102] and slept == [91]
    assert "Sorted 3 files" in last_text(ctx) and "Couldn't be sent" not in last_text(ctx)


async def test_a_long_batch_goes_at_about_one_file_a_second_after_the_first_ones(ctx, monkeypatch):
    monkeypatch.setattr(appmod, "SORT_GAP", 0.1)
    monkeypatch.setattr(appmod, "SORT_SLOW_GAP", 0.5)
    monkeypatch.setattr(appmod, "SORT_FAST_FILES", 3)
    paused = []

    async def pause(seconds):
        paused.append(seconds)

    ctx.app._pause = pause
    await say(ctx, OWNER, "/sequence")
    await feed(ctx, range(1, 7))
    await say(ctx, OWNER, "/sort")
    assert paused == [0.1, 0.1, 0.5, 0.5, 0.5, 0.5]  # files 2 and 3 follow quickly, the rest wait their turn
    assert "Sorted 6 files" in last_text(ctx)


async def test_a_very_long_flood_wait_stops_the_sort_and_lists_what_was_not_sent(ctx):
    await say(ctx, OWNER, "/sequence")
    await feed(ctx, (4, 3, 2, 1))
    ctx.client.file_errors[102] = [errors.FloodWaitError(3600)]  # episode 2
    await say(ctx, OWNER, "/sort")
    assert sent_ids(ctx) == [103]  # episode 1 went out, the wait hit at episode 2
    report = last_text(ctx)
    assert "Sorted 1 of 4 files" in report and "wait 1h 0m 0s" in report and "Couldn't be sent" in report
    assert report.count(OP.format(n=2)[:20]) >= 1 and OP.format(n=4)[:4] in report


async def test_a_status_that_is_still_being_sent_when_sort_starts_is_cleaned_up(ctx):
    original = ctx.client.send_message
    release = asyncio.Event()

    async def slow_status(entity, text="", **kw):
        if "collected" in text:
            await release.wait()
        return await original(entity, text, **kw)

    ctx.client.send_message = slow_status
    await say(ctx, OWNER, "/sequence")
    await feed(ctx, (2, 1))
    await settle()  # the status message is on its way, not sent yet
    await say(ctx, OWNER, "/sort")
    release.set()
    await settle()
    collected = [i + 1 + 2000 for i, (_e, t, _k) in enumerate(ctx.client.sent) if "collected" in t]
    assert len(collected) == 1 and (OWNER, collected) in ctx.client.deleted
    assert "Sorted 2 files" in last_text(ctx) or sent_ids(ctx) == [101, 100]


async def test_a_file_that_fails_is_listed_and_the_others_still_arrive(ctx):
    await say(ctx, OWNER, "/sequence")
    await feed(ctx, (3, 2, 1))
    ctx.client.file_errors[101] = [RuntimeError("MEDIA_EMPTY")]  # episode 2
    await say(ctx, OWNER, "/sort")
    assert sent_ids(ctx) == [102, 100]
    report = last_text(ctx)
    assert "Sorted 2 of 3 files" in report and "Couldn't be sent" in report and "2 - One Piece" in report


async def test_a_file_deleted_before_sorting_is_listed_not_fatal(ctx):
    await say(ctx, OWNER, "/sequence")
    await feed(ctx, (3, 2, 1))
    del ctx.client.messages[101]
    await say(ctx, OWNER, "/sort")
    assert sent_ids(ctx) == [102, 100]
    assert "Sorted 2 of 3 files" in last_text(ctx) and "Couldn't be sent" in last_text(ctx)
    assert OWNER not in ctx.app._sorting


async def test_sort_that_blows_up_does_not_leave_the_user_locked_out(ctx, monkeypatch):
    await say(ctx, OWNER, "/sequence")
    await feed(ctx, (2, 1))

    def boom(*a, **kw):
        raise RuntimeError("something nobody expected")

    original = appmod.analyse
    monkeypatch.setattr(appmod, "analyse", boom)
    ev = await say(ctx, OWNER, "/sort")
    assert "Something went wrong" in ev.texts[-1] and OWNER not in ctx.app._sorting
    monkeypatch.setattr(appmod, "analyse", original)
    await say(ctx, OWNER, "/sequence")
    await feed(ctx, (1,), first_id=300)
    await say(ctx, OWNER, "/sort")
    assert sent_ids(ctx) == [300]


async def test_when_telegram_cannot_look_the_files_up_the_user_gets_a_report_and_is_not_locked_out(ctx):
    await say(ctx, OWNER, "/sequence")
    await feed(ctx, (2, 1))

    async def boom(*a, **kw):
        raise RuntimeError("telegram is down")

    original, ctx.client.get_messages = ctx.client.get_messages, boom
    await say(ctx, OWNER, "/sort")
    assert ctx.client.files == [] and OWNER not in ctx.app._sorting
    report = last_text(ctx)
    assert "Sorted 0 of 2 files" in report and "Couldn't be sent" in report and "1 - One Piece" in report
    ctx.client.get_messages = original
    await say(ctx, OWNER, "/sequence")
    await feed(ctx, (1,), first_id=300)
    await say(ctx, OWNER, "/sort")
    assert sent_ids(ctx) == [300]


# ---------------------------------------------------------------------------
# other commands that wait for a file
# ---------------------------------------------------------------------------


async def test_starting_a_rename_ends_the_sequence_and_says_so(ctx):
    fmt = await ctx.db.create_format(OWNER, "One Piece")
    await say(ctx, OWNER, "/sequence")
    await feed(ctx, (1, 2))
    cb = await click(ctx, OWNER, f"rn:p:{fmt['id']}")
    assert "One Piece" in cb.edits[-1][0] and "2 file(s) was discarded" in cb.edits[-1][0]
    assert ctx.app.modes[OWNER].kind == "rename"
    await settle()
    assert [t for t in texts(ctx) if "collected" in t] == []


async def test_a_rename_with_no_sequence_has_no_such_note(ctx):
    fmt = await ctx.db.create_format(OWNER, "One Piece")
    cb = await click(ctx, OWNER, f"rn:p:{fmt['id']}")
    assert "discarded" not in cb.edits[-1][0]


async def test_starting_a_sequence_over_a_rename_session_replaces_it(ctx):
    fmt = await ctx.db.create_format(OWNER, "One Piece")
    await click(ctx, OWNER, f"rn:p:{fmt['id']}")
    ev = await say(ctx, OWNER, "/sequence")
    assert "discarded" not in ev.texts[0] and ctx.app.modes[OWNER].kind == "sequence"
    await send_doc(ctx, OWNER, OP.format(n=1), mid=100)
    assert ctx.app.queue.waiting == 0  # collected, not renamed
