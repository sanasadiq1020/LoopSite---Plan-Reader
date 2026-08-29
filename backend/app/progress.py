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


def page_done(token: str, page_number: int, page_count: int, sheet_label: str = "") -> None:
    """One sheet finished.

    Reading the sheets is the bulk of the work but not all of it — the
    overlays, the tables and the downloads come after — so the sheets are
    reported as the first 85% and the rest fills in behind them. A bar that
    sits at 100% while work continues is worse than no bar.
    """
    share = int(page_number / page_count * 85) if page_count else 0
    _update(
        token,
        pages_done=page_number,
        pages_total=page_count,
        percent=min(share, 85),
        stage=f"Read sheet {page_number} of {page_count}"
        + (f" — {sheet_label}" if sheet_label else ""),
    )


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
