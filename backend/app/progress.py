"""Live progress for an upload that is still being read.

Reading a plan takes seconds on a small set and half a minute on a large one,
and a spinner that says nothing tells the reader neither how far it has got nor
whether it has stalled. So each upload reports what it is doing, sheet by
sheet, and the browser asks for that while it waits.

**Why a token rather than the run id.** The run id only exists once the upload
has been accepted and the run folder created, which is already several seconds
in. The browser needs something to ask about from the first moment, so it
brings its own token with the upload and asks about that.

The registry is deliberately small and in-memory: progress is only interesting
while a request is in flight, and a record that is never collected is dropped
once it is older than the longest an upload can take.
"""

import threading
import time

from app.logging_setup import get_logger

logger = get_logger()

# A record is forgotten this long after its last update. Comfortably longer
# than the slowest upload (a scanned set spends its whole OCR budget), so a
# record is never dropped while it is still being asked about.
_KEEP_SECONDS = 60 * 60

_lock = threading.Lock()
_records: dict = {}


def start(token: str, filename: str) -> None:
    """Begins reporting for one upload."""
    if not token:
        return
    with _lock:
        _forget_old_records()
        _records[token] = {
            "filename": filename,
            "stage": "Opening the plan",
            "pages_done": 0,
            "pages_total": 0,
            "percent": 0,
            "finished": False,
            "failed": False,
            "run_id": None,
            "updated_at": time.time(),
        }


def set_page_count(token: str, page_count: int) -> None:
    _update(token, pages_total=page_count, stage=f"Found {page_count} sheets")


# **How long the finishing work takes, per record it has to write.** Measured
# across the plan sets in use: the whole of it — matching the marks to their
# schedules, the cross-check, the metrics and writing every table — comes to
# about a quarter of a millisecond for each record on the document. It is here
# as a rate rather than as a share of the bar precisely so that it holds on a
# document unlike the ones it was measured on: a plan set with one sheet and an
# enormous schedule gets a larger slice than a twenty-sheet set with few
# records, because it genuinely takes longer.
_FINISHING_SECONDS_PER_RECORD = 0.00024

# The finishing work is never allowed to look like nothing, nor to take over
# the bar. Below the first the bar would jump at the end; above the second
# something has gone wrong with the estimate and the sheets should still own
# most of it.
_SMALLEST_FINISHING_SHARE = 2.0
_LARGEST_FINISHING_SHARE = 25.0


def page_done(
    token: str,
    page_number: int,
    page_count: int,
    sheet_label: str = "",
    records: int = 0,
) -> None:
    """One sheet finished.

    Reading the sheets is nearly all of the work — measured, 96 to 98 per cent
    of it on every plan set tried — and the finishing work is the rest. The
    share each gets is worked out **from this run**, not fixed in advance: the
    sheets are timed as they are read, so by the time two have gone through the
    run knows what a sheet of this document costs, and the finishing work is
    estimated from how many records there are to write.

    It used to be a flat 85 per cent for the sheets, which gave three per cent
    of the work fifteen per cent of the bar — so the bar sat at 85 looking
    stalled while the run was very nearly done.
    """
    with _lock:
        record = _records.get(token)
        if record is None:
            return
        started = record.get("sheets_started_at") or time.time()
        record.setdefault("sheets_started_at", started)
        # What the finishing work will have to write, counted as it is found
        # rather than guessed from the number of sheets.
        record["records_so_far"] = record.get("records_so_far", 0) + max(records, 0)
        record["sheets_share"] = _sheets_share(record, page_number, page_count, started)
        share = (page_number / page_count * record["sheets_share"]) if page_count else 0
        record.update(
            pages_done=page_number,
            pages_total=page_count,
            percent=_never_backwards(record, min(share, record["sheets_share"])),
            stage=f"Read sheet {page_number} of {page_count}"
            + (f" — {sheet_label}" if sheet_label else ""),
            updated_at=time.time(),
        )


def _sheets_share(record: dict, page_number: int, page_count: int, started: float) -> float:
    """What share of the bar the sheets are worth, from this run's own timing.

    Only worked out once two sheets have gone through, because one sheet is not
    a rate. Until then the sheets are assumed to be all of it, which is very
    nearly true and is never wrong in the direction that matters — the bar does
    not stall.
    """
    if page_number < 2 or not page_count:
        return record.get("sheets_share", 100.0 - _SMALLEST_FINISHING_SHARE)

    per_sheet = (time.time() - started) / page_number
    sheets_seconds = per_sheet * page_count
    # The records this document is actually producing, projected to all of its
    # sheets. A plan set of schedules makes far more of them than one of
    # elevations, and the finishing work is the thing that has to write them.
    per_page_records = record.get("records_so_far", 0) / page_number
    finishing_seconds = (
        per_page_records * page_count * _FINISHING_SECONDS_PER_RECORD
    )
    if sheets_seconds <= 0:
        return record.get("sheets_share", 100.0 - _SMALLEST_FINISHING_SHARE)

    finishing = 100.0 * finishing_seconds / (sheets_seconds + finishing_seconds)
    finishing = max(_SMALLEST_FINISHING_SHARE, min(_LARGEST_FINISHING_SHARE, finishing))
    # **The estimate is free to move; the bar is not.** Pinning the share so it
    # could only grow meant the first sheet's guess was the last word — a
    # document that turned out to have a great deal to write could never claim
    # its share back. What must never go backwards is the bar itself, and that
    # is held below, where the percentage is set.
    return 100.0 - finishing


def _never_backwards(record: dict, percent: float) -> int:
    """A bar that goes backwards is worse than one that pauses."""
    return max(int(record.get("percent", 0)), int(percent))


def begin_finishing(token: str, steps: list) -> None:
    """The steps left once every sheet has been read, and what each will do.

    ``steps`` is a list of (what it is called, how much it has to get through).
    The weights are real counts taken from the document — the marks to match,
    the sheets to check against the index, the records to write — so a plan set
    with a large schedule gives its matching step a bigger slice than one with
    a small schedule, on the same bar.
    """
    with _lock:
        record = _records.get(token)
        if record is None:
            return
        weights = [max(float(weight), 0.0) for _label, weight in steps]
        total = sum(weights) or 1.0
        record["finishing_steps"] = [
            {"label": label, "share": weight / total}
            for (label, _w), weight in zip(steps, weights)
        ]
        record["finishing_done"] = 0.0


def finishing_step(token: str, index: int) -> None:
    """Starts one of the finishing steps, and moves the bar on by its share."""
    with _lock:
        record = _records.get(token)
        if record is None:
            return
        steps = record.get("finishing_steps") or []
        if not (0 <= index < len(steps)):
            return
        base = record.get("sheets_share", 85.0)
        done = record.get("finishing_done", 0.0)
        record.update(
            stage=steps[index]["label"],
            percent=_never_backwards(record, min(base + (100.0 - base) * done, 99)),
            updated_at=time.time(),
        )
        record["finishing_done"] = done + steps[index]["share"]


def set_stage(token: str, stage: str, percent: int) -> None:
    _update(token, stage=stage, percent=percent)


def finish(token: str, run_id: str) -> None:
    _update(token, stage="Done", percent=100, finished=True, run_id=run_id)


def fail(token: str, message: str) -> None:
    _update(token, stage=message, finished=True, failed=True)


def read(token: str):
    """The current state of one upload, or None if nothing is known about it."""
    with _lock:
        record = _records.get(token)
        return dict(record) if record else None


def _update(token: str, **fields) -> None:
    if not token:
        return
    with _lock:
        record = _records.get(token)
        if record is None:
            return
        record.update(fields)
        record["updated_at"] = time.time()


def _forget_old_records() -> None:
    """Caller holds the lock."""
    cutoff = time.time() - _KEEP_SECONDS
    for token in [t for t, r in _records.items() if r["updated_at"] < cutoff]:
        _records.pop(token, None)
