"""How many plans this server reads at once, and what happens to the rest.

Reading a plan set is the most expensive thing this application does: a sheet's
line work, its text and its picture all have to be held in memory at once, and
a large drawing is a large amount of memory. One reading at a time is
comfortable on a small server. Two at a time on the same small server is how it
runs out of memory and is killed — and when that happens every plan on it is
lost, including the ones belonging to people who were only looking at results.

So the number read **at the same time** is capped, and everything else waits its
turn rather than competing for memory that is not there. Waiting is slower for
one person; running out of memory is a dead service for everyone.

Three settings, all with working defaults and all environment-driven, because
they describe the machine rather than the drawings:

``MAX_CONCURRENT_READINGS``
    How many plans may be read at once. Default 1.

``MAX_WAITING_READINGS``
    How many more may be queued behind them. Past this the next upload is
    turned away **politely and immediately**, with a message saying to try
    again shortly. A queue with no limit is just a slower way to run out of
    memory.

``READING_QUEUE_TIMEOUT_SECONDS``
    How long an upload will wait for its turn before giving up and saying so.
    Without it, a reading that hangs would hold everything behind it for ever.
"""

import os
import threading
from contextlib import contextmanager

from app.logging_setup import get_logger

logger = get_logger()


def _positive_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


MAX_CONCURRENT = _positive_int("MAX_CONCURRENT_READINGS", 1)
MAX_WAITING = _positive_int("MAX_WAITING_READINGS", 4)
QUEUE_TIMEOUT_SECONDS = _positive_int("READING_QUEUE_TIMEOUT_SECONDS", 600)

# How often a waiting upload says that it is still waiting.
_WAIT_TICK_SECONDS = 3.0

_slots = threading.BoundedSemaphore(MAX_CONCURRENT)
_counter_lock = threading.Lock()
_waiting = 0
_running = 0


class TooBusy(Exception):
    """Raised when there is no room to accept another plan right now.

    Deliberately an ordinary refusal rather than a failure: the server is
    working, it is simply full, and saying so keeps it working for everyone
    already on it.
    """


def snapshot() -> dict:
    """What the server is doing right now — read by the health endpoint."""
    with _counter_lock:
        return {
            "reading_now": _running,
            "waiting": _waiting,
            "capacity": MAX_CONCURRENT,
            "queue_capacity": MAX_WAITING,
        }


def claim_a_place() -> None:
    """Takes a place in the queue, or refuses if the queue is full.

    Called on the request thread, before the reading is started, so a server
    that is full says so in its reply instead of accepting work it cannot do.
    """
    global _waiting
    with _counter_lock:
        if _waiting >= MAX_WAITING:
            raise TooBusy(
                "This server is reading other plans at the moment and cannot take "
                "another one yet. Please try again in a minute — nothing has been lost."
            )
        _waiting += 1


def release_place() -> None:
    """Gives back a place claimed but never used — an upload that failed
    before its reading could start."""
    global _waiting
    with _counter_lock:
        _waiting = max(_waiting - 1, 0)


@contextmanager
def a_turn_to_read(on_wait=None):
    """Waits for a turn, holds it while the plan is read, gives it back.

    The place in the queue is claimed by ``claim_a_place`` on the request
    thread and handed over here, so the count is right from the moment the
    upload is accepted rather than from the moment the reading starts.

    ``on_wait`` is called every few seconds while waiting, with how many plans
    are still ahead. Waiting silently is indistinguishable from being stuck —
    the browser gives up on a server that says nothing — so a queued upload
    keeps saying that it is queued.
    """
    global _waiting, _running
    acquired = False
    waited = 0.0
    while waited < QUEUE_TIMEOUT_SECONDS:
        acquired = _slots.acquire(timeout=_WAIT_TICK_SECONDS)
        if acquired:
            break
        waited += _WAIT_TICK_SECONDS
        if on_wait is not None:
            try:
                on_wait(snapshot())
            except Exception:
                pass  # a progress report may never stop a plan being read

    with _counter_lock:
        _waiting = max(_waiting - 1, 0)
        if acquired:
            _running += 1
    if not acquired:
        raise TooBusy(
            "This plan waited a long time for its turn and was not read. The server "
            "is unusually busy — please try again."
        )
    try:
        yield
    finally:
        with _counter_lock:
            _running = max(_running - 1, 0)
        _slots.release()


def is_anyone_waiting() -> bool:
    with _counter_lock:
        return _waiting > 0 or _running > 0
