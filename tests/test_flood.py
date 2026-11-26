"""How the bot keeps itself under Telegram's limits (``bot.flood``): the places for transfers, the end of a flood, the spacing of edits."""
import asyncio
import gc

import pytest

from bot import flood as flood_module
from bot.errors import Cancelled
from bot.flood import FloodControl, Slots

from .timeline import Timeline, settle


@pytest.fixture
def line():
    return Timeline()


def make(line, **kwargs):
    return FloodControl(clock=line.clock, sleep=line.sleep, jitter=lambda: 0.0, **kwargs)


class Reporter:
    """Something a chat can watch (the real one is a ProgressReporter)."""


# ---------------------------------------------------------------------------
# places for transfers
# ---------------------------------------------------------------------------


async def test_a_place_is_free_until_it_is_taken():
    slots = Slots(2)
    await slots.acquire()
    assert slots.used == 1 and not slots.crowded
    await slots.acquire()
    assert slots.used == 2 and slots.crowded
    slots.release()
    assert slots.used == 1 and not slots.crowded


async def test_places_are_given_in_the_order_they_were_asked_for():
    slots, order = Slots(1), []
    await slots.acquire()

    async def want(name):
        await slots.acquire()
        order.append(name)

    tasks = [asyncio.ensure_future(want(name)) for name in "abc"]
    await settle()
    assert order == [] and slots.waiting == 3 and slots.crowded
    for expected in ("a", "b", "c"):
        slots.release()
        await settle()
        assert order[-1] == expected and slots.used == 1
    assert order == ["a", "b", "c"]
    await asyncio.gather(*tasks)


async def test_size_is_at_least_one():
    assert Slots(0).size == 1 and Slots(-3).size == 1
    slots = Slots(3)
    slots.resize(0)
    assert slots.size == 1


async def test_more_places_serve_the_ones_waiting_at_once():
    slots, got = Slots(1), []
    await slots.acquire()

    async def want(name):
        await slots.acquire()
        got.append(name)

    tasks = [asyncio.ensure_future(want(name)) for name in "ab"]
    await settle()
    assert got == []
    slots.resize(3)
    await settle()
    assert got == ["a", "b"] and slots.used == 3
    await asyncio.gather(*tasks)


async def test_fewer_places_take_nothing_away_from_the_ones_running():
    slots, got = Slots(2), []
    await slots.acquire()
    await slots.acquire()

    async def want():
        await slots.acquire()
        got.append(1)

    task = asyncio.ensure_future(want())
    await settle()
    slots.resize(1)
    assert slots.used == 2  # both go on
    slots.release()
    await settle()
    assert got == []  # one is still running and that fills the one place there is now
    slots.release()
    await settle()
    assert got == [1] and slots.used == 1
    await task


async def test_a_wait_that_is_cancelled_gives_up_its_turn_and_takes_no_place():
    slots, cancel = Slots(1), asyncio.Event()
    await slots.acquire()
    waiter = asyncio.ensure_future(slots.acquire(cancel))
    await settle()
    assert slots.waiting == 1
    cancel.set()
    with pytest.raises(Cancelled):
        await waiter
    assert slots.waiting == 0 and slots.used == 1
    slots.release()
    assert slots.used == 0  # the place was not given to the one that had given up


async def test_a_wait_that_is_already_cancelled_does_not_start():
    slots, cancel = Slots(1), asyncio.Event()
    cancel.set()
    with pytest.raises(Cancelled):
        await slots.acquire(cancel)
    assert slots.used == 0 and slots.waiting == 0


async def test_a_place_given_in_the_moment_of_the_cancel_goes_back():
    slots, cancel = Slots(1), asyncio.Event()
    await slots.acquire()
    waiter = asyncio.ensure_future(slots.acquire(cancel))
    await settle()
    slots.release()  # the place goes to the waiter ...
    cancel.set()  # ... which is told to stop before it has noticed
    with pytest.raises(Cancelled):
        await waiter
    assert slots.used == 0 and slots.waiting == 0
    await slots.acquire()  # and the next one gets it
    assert slots.used == 1


async def test_a_task_that_is_cancelled_while_it_waits_leaves_the_line():
    slots = Slots(1)
    await slots.acquire()
    waiter = asyncio.ensure_future(slots.acquire())
    await settle()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert slots.waiting == 0 and slots.used == 1
    slots.release()
    assert slots.used == 0


async def test_a_task_cancelled_after_it_was_given_its_place_gives_it_back():
    slots = Slots(1)
    await slots.acquire()
    waiter = asyncio.ensure_future(slots.acquire())
    await settle()
    slots.release()  # given to the waiter, which has not run yet
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert slots.used == 0


# ---------------------------------------------------------------------------
# the end of a flood
# ---------------------------------------------------------------------------


async def test_without_a_flood_nothing_waits(line):
    control = make(line)
    await control.calm()
    assert line.waits == [] and not control.cooling and control.cooling_left() == 0


async def test_a_flood_holds_back_new_work_until_it_is_over(line):
    control = make(line)
    control.hit(3)
    assert control.cooling and control.cooling_left() == pytest.approx(3.5)
    await control.calm()
    assert line.waited() == [3.5] and not control.cooling
    assert control.hits == 1


async def test_a_hold_is_never_longer_than_a_short_while(line):
    control = make(line)
    control.hit(3600)
    assert control.cooling_left() == flood_module.MAX_COOL
    control.hit(2)  # and a shorter flood after it does not shorten it
    assert control.cooling_left() == flood_module.MAX_COOL


async def test_the_hold_is_over_when_time_has_passed(line):
    control = make(line)
    control.hit(3)
    line.now += 2
    assert control.cooling_left() == pytest.approx(1.5)
    line.now += 2
    assert control.cooling_left() == 0 and not control.cooling


async def test_a_flood_met_while_waiting_is_waited_for_too(line):
    control = make(line)

    async def sleep(seconds):
        await line.sleep(seconds)
        if len(line.waits) == 1:
            control.hit(2)

    control._sleep = sleep
    control.hit(3)
    await control.calm()
    assert line.waited() == [3.5, 2.5]


async def test_a_flood_that_never_ends_is_waited_for_only_so_often(line):
    control = make(line)

    async def sleep(seconds):
        await line.sleep(seconds)
        control.hit(1)

    control._sleep = sleep
    control.hit(1)
    await control.calm()
    assert len(line.waits) == flood_module.ROUNDS  # then the work goes on: the next request finds out for itself


async def test_those_that_were_waiting_start_a_moment_apart(line):
    control = FloodControl(clock=line.clock, sleep=line.sleep, jitter=lambda: 0.5)
    control.hit(3)
    await control.calm()
    assert line.waited() == [3.5, 0.5 * flood_module.STAGGER]
    await control.calm()  # nothing to wait for now, so no pause either
    assert line.waited() == [3.5, 0.5 * flood_module.STAGGER]


async def test_waiting_for_the_end_of_a_flood_can_be_cancelled():
    cancel = asyncio.Event()
    never = asyncio.Event()

    async def sleep(seconds):
        await never.wait()

    control = FloodControl(sleep=sleep, jitter=lambda: 0.0)
    control.hit(20)
    waiter = asyncio.ensure_future(control.calm(cancel))
    await settle()
    assert not waiter.done()
    cancel.set()
    with pytest.raises(Cancelled):
        await asyncio.wait_for(waiter, 1)


async def test_a_flood_that_is_over_before_the_cancel_is_not_cancelled(line):
    control = make(line)
    control.hit(3)
    await control.calm(asyncio.Event())
    assert line.waited() == [3.5]


# ---------------------------------------------------------------------------
# how many transfers at once
# ---------------------------------------------------------------------------


async def test_a_download_and_an_upload_each_take_a_place_and_give_it_back(line):
    control = make(line)
    async with control.download():
        assert control.downloads.used == 1 and control.uploads.used == 0
        async with control.upload():
            assert control.uploads.used == 1
    assert control.downloads.used == 0 and control.uploads.used == 0


async def test_the_place_is_given_back_when_the_transfer_fails(line):
    control = make(line)
    with pytest.raises(ValueError):
        async with control.upload():
            raise ValueError
    assert control.uploads.used == 0


async def test_only_so_many_transfers_run_at_once_and_the_rest_wait_their_turn(line):
    control = make(line, uploads=1)
    order, release = [], asyncio.Event()

    async def upload(name):
        async with control.upload():
            order.append(name)
            await release.wait()

    tasks = [asyncio.ensure_future(upload(name)) for name in "abc"]
    await settle()
    assert order == ["a"] and control.uploads.waiting == 2 and control.uploads.crowded
    release.set()
    await asyncio.gather(*tasks)
    assert order == ["a", "b", "c"] and control.uploads.used == 0


async def test_a_transfer_that_waited_for_its_place_waits_for_the_flood_too(line):
    control = make(line)
    control.hit(3)
    async with control.upload():
        assert line.waited() == [3.5]


async def test_a_place_is_not_kept_while_waiting_for_the_flood_when_that_is_cancelled():
    cancel = asyncio.Event()
    never = asyncio.Event()

    async def sleep(seconds):
        await never.wait()

    control = FloodControl(sleep=sleep, jitter=lambda: 0.0)
    control.hit(20)

    async def run():
        async with control.download(cancel):
            raise AssertionError("it should not have started")

    task = asyncio.ensure_future(run())
    await settle()
    assert control.downloads.used == 1  # it has its place and waits for the flood to be over
    cancel.set()
    with pytest.raises(Cancelled):
        await asyncio.wait_for(task, 1)
    assert control.downloads.used == 0


async def test_a_transfer_that_is_cancelled_in_the_line_takes_no_place(line):
    control = make(line, uploads=1)
    cancel = asyncio.Event()
    async with control.upload():
        task = asyncio.ensure_future(control.upload(cancel).__aenter__())
        await settle()
        cancel.set()
        with pytest.raises(Cancelled):
            await task
    assert control.uploads.used == 0 and control.uploads.waiting == 0


async def test_a_flood_met_by_a_transfer_lowers_how_many_run_at_once(line):
    control = make(line, uploads=3, downloads=3)
    control.hit(1, transfer=True)
    assert (control.uploads.size, control.downloads.size) == (2, 2)
    control.hit(1, transfer=True)  # the same flood: the second wait of it does not lower the limits again
    assert (control.uploads.size, control.downloads.size) == (2, 2)
    line.now += flood_module.EPISODE
    control.hit(1, transfer=True)
    assert (control.uploads.size, control.downloads.size) == (1, 1)
    line.now += flood_module.EPISODE
    control.hit(1, transfer=True)  # one at a time is as low as it goes
    assert (control.uploads.size, control.downloads.size) == (1, 1)


async def test_the_limits_are_lowered_by_the_same_steps_but_never_below_one(line):
    control = make(line, uploads=1, downloads=3)
    control.hit(1, transfer=True)
    assert (control.uploads.size, control.downloads.size) == (1, 2)
    line.now += flood_module.EPISODE
    control.hit(1, transfer=True)
    assert (control.uploads.size, control.downloads.size) == (1, 1)


async def test_a_flood_wait_on_an_edit_is_the_chats_limit_and_leaves_the_transfers_alone(line):
    control = make(line, uploads=3, downloads=3)
    control.hit(2)
    assert (control.uploads.size, control.downloads.size) == (3, 3) and control.cooling
    control.hit(flood_module.SERIOUS)  # a long one is the connection's
    assert (control.uploads.size, control.downloads.size) == (2, 2)


async def test_the_limits_come_back_one_step_at_a_time_after_a_quiet_minute(line):
    control = make(line, uploads=3, downloads=3)
    quiet = flood_module.QUIET
    control.hit(1, transfer=True)  # at 0
    line.now = 10
    control.hit(1, transfer=True)  # at 10: a second flood
    assert control.uploads.size == 1
    line.now = 10 + quiet - 1
    control.progress_interval(1, 4.0)  # whoever looks at the limits lets them recover
    assert control.uploads.size == 1
    line.now = 10 + quiet
    control.progress_interval(1, 4.0)
    assert (control.uploads.size, control.downloads.size) == (2, 2)
    line.now = 10 + 2 * quiet - 1
    control.progress_interval(1, 4.0)
    assert control.uploads.size == 2  # each step needs a quiet minute of its own
    line.now = 10 + 2 * quiet
    control.progress_interval(1, 4.0)
    assert (control.uploads.size, control.downloads.size) == (3, 3)
    line.now += 1000
    control.progress_interval(1, 4.0)
    assert (control.uploads.size, control.downloads.size) == (3, 3)  # and never past what was set


async def test_a_new_serious_flood_puts_off_the_recovery(line):
    control = make(line, uploads=3, downloads=3)
    control.hit(1, transfer=True)  # at 0
    line.now = 59
    control.hit(1, transfer=True)  # at 59
    assert control.uploads.size == 1
    line.now = 61  # a minute after the first flood, but two seconds after the second
    control.progress_interval(1, 4.0)
    assert control.uploads.size == 1
    line.now = 59 + flood_module.QUIET
    control.progress_interval(1, 4.0)
    assert control.uploads.size == 2


async def test_a_flood_wait_on_an_edit_does_not_put_off_the_recovery(line):
    control = make(line, uploads=2, downloads=2)
    control.hit(1, transfer=True)  # at 0
    line.now = 50
    control.hit(2)  # an edit met a short one
    line.now = flood_module.QUIET
    control.progress_interval(1, 4.0)
    assert control.uploads.size == 2


async def test_a_transfer_that_is_waiting_gets_the_place_a_recovery_makes(line):
    control = make(line, uploads=2, downloads=2)
    control.hit(1, transfer=True)
    assert control.uploads.size == 1
    release, got = asyncio.Event(), []

    async def upload(name):
        async with control.upload():
            got.append(name)
            await release.wait()

    tasks = [asyncio.ensure_future(upload(name)) for name in "ab"]
    await settle()
    assert got == ["a"] and control.uploads.waiting == 1
    line.now += flood_module.QUIET + 1
    control.progress_interval(1, 4.0)  # a quiet minute: two uploads at once again
    await settle()
    assert got == ["a", "b"] and control.uploads.waiting == 0
    release.set()
    await asyncio.gather(*tasks)


# ---------------------------------------------------------------------------
# messages
# ---------------------------------------------------------------------------


@pytest.fixture
def waits():
    return []


@pytest.fixture
def spaced(waits):
    async def sleep(seconds):
        waits.append(seconds)

    return FloodControl(clock=lambda: 0.0, sleep=sleep, jitter=lambda: 0.0)


async def test_edits_in_one_chat_come_one_after_the_other(spaced, waits):
    await asyncio.gather(*(spaced.pace(7) for _ in range(4)))
    assert waits == pytest.approx([flood_module.EDIT_GAP * n for n in (1, 2, 3)])


async def test_the_gap_between_edits_can_be_set():
    waits = []

    async def sleep(seconds):
        waits.append(seconds)

    control = FloodControl(edit_gap=2.0, clock=lambda: 0.0, sleep=sleep)
    await asyncio.gather(control.pace(1), control.pace(1))
    assert waits == [2.0]


async def test_chats_do_not_wait_for_each_other(spaced, waits):
    await asyncio.gather(spaced.pace(1), spaced.pace(2), spaced.pace(3))
    assert waits == []


async def test_a_chat_that_has_been_quiet_for_a_while_has_its_edit_at_once(line):
    control = make(line)
    await control.pace(1)
    line.now += flood_module.EDIT_GAP
    await control.pace(1)
    assert line.waits == []
    await control.pace(1)  # but one right after it waits
    assert line.waited() == pytest.approx([flood_module.EDIT_GAP])


async def test_waiting_for_the_turn_to_edit_can_be_cancelled():
    never, cancel = asyncio.Event(), asyncio.Event()

    async def sleep(seconds):
        await never.wait()

    control = FloodControl(clock=lambda: 0.0, sleep=sleep)
    await control.pace(1)
    waiter = asyncio.ensure_future(control.pace(1, cancel))
    await settle()
    cancel.set()
    with pytest.raises(Cancelled):
        await asyncio.wait_for(waiter, 1)


async def test_the_chats_that_were_given_a_turn_are_not_kept_for_ever(line):
    control = make(line)
    for chat in range(300):
        await control.pace(chat)
    line.now += 100
    await control.pace(10_000)
    assert len(control._next_edit) == 1


# ---------------------------------------------------------------------------
# the interval between two edits of a progress message
# ---------------------------------------------------------------------------


async def test_one_progress_message_is_edited_as_often_as_was_set(line):
    control = make(line)
    assert control.progress_interval(1, 4.0) == 4.0
    reporter = Reporter()
    control.watch(1, reporter)
    assert control.progress_interval(1, 4.0) == 4.0


async def test_progress_messages_in_one_chat_share_its_edits(line):
    control = make(line)
    reporters = [Reporter() for _ in range(5)]
    for number, reporter in enumerate(reporters, 1):
        control.watch(1, reporter)
        assert control.progress_interval(1, 4.0) == max(4.0, number * flood_module.PROGRESS_SHARE)
    assert control.progress_interval(2, 4.0) == 4.0  # another chat is not affected
    assert control.progress_interval(1, 60.0) == 60.0  # and a long interval is already long enough
    control.unwatch(1, reporters[0])
    assert control.progress_interval(1, 4.0) == 4 * flood_module.PROGRESS_SHARE


async def test_the_same_reporter_is_counted_once(line):
    control = make(line)
    reporter = Reporter()
    control.watch(1, reporter)
    control.watch(1, reporter)
    assert len(control._watching[1]) == 1


async def test_letting_a_reporter_go_that_was_never_watched_is_harmless(line):
    control = make(line)
    control.unwatch(1, Reporter())
    reporter = Reporter()
    control.watch(1, reporter)
    control.unwatch(1, reporter)
    control.unwatch(1, reporter)
    assert 1 not in control._watching


async def test_a_reporter_nothing_refers_to_any_more_is_forgotten(line):
    control = make(line)
    reporters = [Reporter() for _ in range(4)]
    for reporter in reporters:
        control.watch(1, reporter)
    assert control.progress_interval(1, 4.0) == 10.0
    del reporters, reporter
    gc.collect()
    assert control.progress_interval(1, 4.0) == 4.0
    assert 1 not in control._watching


async def test_telegram_being_busy_doubles_the_interval_until_it_has_been_quiet_a_while(line):
    control = make(line, uploads=2, downloads=2)
    control.hit(1, transfer=True)
    assert control.progress_interval(1, 4.0) == 8.0
    line.now += flood_module.QUIET
    assert control.progress_interval(1, 4.0) == 4.0


async def test_the_state_is_described_on_one_line(line):
    control = make(line)
    assert control.describe() == "0/1 uploads · 0/2 downloads · no flood · 0 flood waits since the start"
    async with control.upload():
        assert control.describe() == "1/1 uploads · 0/2 downloads · no flood · 0 flood waits since the start"
    control.hit(4.2)
    assert control.describe() == "0/1 uploads · 0/2 downloads · holding back for 5s · 1 flood wait since the start"


# ---------------------------------------------------------------------------
# the line in /status
# ---------------------------------------------------------------------------


async def test_the_status_line_shows_the_lowered_limits_and_counts_the_waits(line):
    control = make(line, uploads=2, downloads=2)
    async with control.upload(), control.download():
        assert control.describe().startswith("1/2 uploads · 1/2 downloads · no flood")
    control.hit(8.0, transfer=True)  # one transfer step down: one upload and one download at a time
    held = f"{min(8.0 + flood_module.SLACK, flood_module.MAX_COOL):.0f}"
    assert control.describe() == f"0/1 uploads · 0/1 downloads · holding back for {held}s · 1 flood wait since the start"
    control.hit(1.0)
    assert control.describe().endswith("2 flood waits since the start")  # (one wait is "wait", more are "waits")


# ---------------------------------------------------------------------------
# corners: a turn that is over, a long run of floods, a transfer that starts after a quiet while
# ---------------------------------------------------------------------------


async def test_a_turn_that_is_over_by_the_time_a_place_is_free_is_skipped_and_the_place_goes_to_the_next():
    slots, loop = Slots(1), asyncio.get_running_loop()
    over, next_in_line = loop.create_future(), loop.create_future()
    over.cancel()
    slots._line.extend([over, next_in_line])
    slots._serve()
    assert slots.used == 1 and next_in_line.done() and not slots._line  # (a place given to the first one would be taken for ever)


async def test_floods_that_keep_coming_do_not_pile_up_more_steps_than_there_are_places_to_take_away(line):
    control = make(line, uploads=1, downloads=2)  # one step is all there is to lower
    for _ in range(3):
        control.hit(1, transfer=True)
        line.now += flood_module.EPISODE
    assert (control.uploads.size, control.downloads.size) == (1, 1)
    line.now += flood_module.QUIET
    control.progress_interval(1, 4.0)
    assert (control.uploads.size, control.downloads.size) == (1, 2)  # one quiet minute brings that step back, however many floods there were


async def test_a_transfer_that_starts_after_a_quiet_minute_finds_the_limits_back_at_once(line):
    control = make(line, uploads=2, downloads=2)
    control.hit(1, transfer=True)
    assert (control.uploads.size, control.downloads.size) == (1, 1)
    line.now += flood_module.QUIET + 1  # (nothing looked at the limits meanwhile: no progress message asked for its interval)
    async with control.upload():
        assert control.uploads.size == 2 and control.uploads.used == 1  # starting a transfer is a look at them too
    async with control.download():
        assert control.downloads.size == 2
