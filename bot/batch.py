"""The report on a batch of files: how the files the bot renamed went, in one message once the user's queue is empty.

The pipeline notes how every job ended (``Renamer.on_result``); the app keeps those notes per user until the queue has stayed empty for a
moment and then sends this: how many files were renamed and checked after sending, which came with a warning, which needed a second
try or a new thumbnail, and which failed and why.  The point is that nobody has to open a hundred files to find out whether something
went wrong with one of them.  A single file is not reported on: its own message says it all."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Sequence, Tuple

from .utils import clip, esc

REPORT_MIN = 2  # files a batch needs for a report
LISTED = 8  # files named under the warnings, and under the failures; the others are counted
NAME_CLIP = 60  # characters of a file name that are shown
TEXT_CLIP = 140  # characters of a reason or of a warning that are shown
SAFE_LENGTH = 3800  # Telegram takes 4096 characters in a message: when the report is longer than this, fewer files are named


@dataclass(frozen=True)
class Outcome:
    """How one file ended: a copy of what ``Job`` and ``pipeline.Result`` say, so the report does not depend on what happens to them later."""

    name: str  # what the file was renamed to
    chat_id: int  # the chat with the user: where the report goes
    state: str = "done"  # done | failed | cancelled
    reason: str = ""  # why it failed
    warnings: Tuple[str, ...] = ()
    checks: str = ""  # what was looked at once the file was in its chat: "size · name · tracks (…) · thumbnail"
    retries: int = 0  # how often it was tried again because Telegram was busy
    thumb_resent: bool = False  # it went out without its thumbnail and was sent again

    @classmethod
    def of(cls, job: Any, result: Any) -> "Outcome":
        return cls(
            name=job.new_name, chat_id=job.chat_id, state=result.state, reason=result.reason, warnings=tuple(result.warnings),
            checks=result.checks, retries=result.retries, thumb_resent=result.thumb_resent,
        )


def check_labels(checks: str) -> List[str]:
    """``size · name · tracks (2 audio) · thumbnail`` -> ``["size", "name", "tracks", "thumbnail"]``."""
    labels: List[str] = []
    for part in checks.split(" · "):
        label = part.split(" (")[0].strip()
        if label and label not in labels:
            labels.append(label)
    return labels


def common_checks(outcomes: Sequence[Outcome]) -> List[str]:
    """What was checked on every one of these files (so that the report claims nothing that only some of them had), in the order of the first."""
    found = [check_labels(o.checks) for o in outcomes]
    if not found:
        return []
    return [label for label in found[0] if all(label in labels for labels in found[1:])]


def _plural(count: int, one: str, many: str) -> str:
    return one if count == 1 else many.format(n=count)


def _named(files: Sequence[Outcome], why: Any, listed: int) -> List[str]:
    lines = [f"• <code>{esc(clip(o.name, NAME_CLIP))}</code>\n  {esc(clip(why(o), TEXT_CLIP))}" for o in files[:listed]]
    if len(files) > listed:
        lines.append(f"…and {len(files) - listed} more")
    return lines


def _report(outcomes: Sequence[Outcome], listed: int) -> str:
    total = len(outcomes)
    done = [o for o in outcomes if o.state == "done"]
    cancelled = [o for o in outcomes if o.state == "cancelled"]
    failed = [o for o in outcomes if o.state not in ("done", "cancelled")]  # (a job that ended in no state at all did not work either)
    warned = [o for o in done if o.warnings]
    checked = [o for o in done if not o.warnings and o.checks]
    unchecked = [o for o in done if not o.warnings and not o.checks]
    resent = sum(1 for o in outcomes if o.thumb_resent)
    retried = sum(1 for o in outcomes if o.retries)

    head = f"all {total} files renamed" if len(done) == total else f"{len(done)} of {total} files renamed"
    lines = [f"📋 <b>Batch finished:</b> {head}", ""]
    if checked:
        labels = common_checks(checked)
        lines.append(f"✅ {len(checked)} checked after sending" + (f": {' · '.join(labels)}" if labels else ""))
    if unchecked:
        lines.append(f"❔ {len(unchecked)} sent without a check " + _plural(len(unchecked), "(Telegram gave no details of it)", "(Telegram gave no details of them)"))
    if warned:
        lines.append(f"⚠️ {len(warned)} renamed, with a warning:")
        lines += _named(warned, lambda o: " ".join(o.warnings), listed)
    if resent:
        lines.append("🖼 " + _plural(resent, "1 lost its thumbnail on the way and was sent again", "{n} lost their thumbnail on the way and were sent again"))
    if retried:
        lines.append("🔁 " + _plural(retried, "1 had to be tried again because Telegram was busy", "{n} had to be tried again because Telegram was busy"))
    if failed:
        lines.append(f"❌ {len(failed)} failed:")
        lines += _named(failed, lambda o: o.reason or "it stopped without saying why", listed)
        lines.append("Send the failed ones again to retry them.")
    if cancelled:
        lines.append(f"✖ {len(cancelled)} cancelled")
    return "\n".join(lines)


def format_batch(outcomes: Sequence[Outcome]) -> str:
    """The report (HTML) on a batch: one message, however many files there were."""
    for listed in (LISTED, 4, 2, 1, 0):
        text = _report(outcomes, listed)
        if len(text) <= SAFE_LENGTH:
            break
    return text
