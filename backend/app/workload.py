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
    How many plans may be read at once. **Left unset, this is worked out from
    the memory the machine actually has** — one at a time is right for a small
    container and wrong for a large one, and which of the two this is only
    becomes knowable once it is running. Set it to override.

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


# Measured by watching the process read every plan set in `input/`: the
# heaviest peaks at 345 MB, and it does not creep up over repeated reads. The
# figures below carry a margin on top of that, and room is set aside for the
# process itself and for the character recognition models when a scanned sheet
# needs them.
_MEMORY_PER_READING_MB = 350
_MEMORY_TO_LEAVE_ALONE_MB = 512
# Past this, processors are the limit rather than memory, and a longer queue
# serves people better than more half-speed readings.
_MOST_WORTH_RUNNING_AT_ONCE = 4


def _memory_this_machine_has_mb():
    """Total memory available here, in MB, or None when it cannot be told.

    A container is asked about **its own** limit first. The machine underneath
    may have far more, and answering from that is how a service decides it can
    read six plans at once inside a box that allows one.
    """
    for path, unlimited in (
        ("/sys/fs/cgroup/memory.max", "max"),  # cgroup v2
        ("/sys/fs/cgroup/memory/memory.limit_in_bytes", None),  # cgroup v1
    ):
        try:
            raw = open(path).read().strip()
        except OSError:
            continue
        if raw == unlimited:
            break
        try:
            value = int(raw)
        except ValueError:
            continue
        # cgroup v1 reports an enormous number to mean "no limit".
        if 0 < value < (1 << 60):
            return value / (1024 * 1024)

    try:
        return (os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")) / (1024 * 1024)
    except (ValueError, OSError, AttributeError):
        pass

    # Windows has no sysconf. A development machine is not where this decision
    # matters, but a function that answers on one platform and shrugs on the
    # other cannot be checked on the machine it is written on.
    try:
        import ctypes

        class _MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _MemoryStatus()
        status.dwLength = ctypes.sizeof(_MemoryStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return status.ullTotalPhys / (1024 * 1024)
    except Exception:
        pass
    return None


def _how_many_fit() -> int:
    """How many plans this machine has the memory to read at once.

    **One at a time is right for a small box and wrong for a large one**, and
    which box this is only becomes knowable once it is running. So it is
    worked out from the memory actually available rather than fixed in advance,
    and ``MAX_CONCURRENT_READINGS`` overrides it wherever that is not wanted.
    """
    memory = _memory_this_machine_has_mb()
    if not memory:
        return 1
    room = memory - _MEMORY_TO_LEAVE_ALONE_MB
    if room <= 0:
        return 1
    return max(1, min(int(room // _MEMORY_PER_READING_MB), _MOST_WORTH_RUNNING_AT_ONCE))


MAX_CONCURRENT = _positive_int("MAX_CONCURRENT_READINGS", _how_many_fit())
MAX_WAITING = _positive_int("MAX_WAITING_READINGS", max(4, MAX_CONCURRENT * 4))

_memory = _memory_this_machine_has_mb()
logger.info(
    "this server will read "
    + (
        f"{MAX_CONCURRENT} plan(s) at once, with room for {MAX_WAITING} waiting"
    )
    + (f" ({_memory:.0f} MB available)" if _memory else " (memory unknown, so one at a time)")
)
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
