"""Regression tests for staying alive under traffic.

Each names the failure it prevents. All three of these actually happened on a
deployed server: two people uploading at the same time exhausted its memory and
it was killed, and the second upload deleted the first one's folder underneath
the reader who was still looking at it.
"""

import threading
import time
from pathlib import Path

import pytest

from app import workload
from app.paths import OUTPUT_PLAN_DIR, prune_runs, remove_run


# --- how many plans are read at once --------------------------------------


def test_only_the_allowed_number_of_plans_are_read_at_once():
    """Reading two plans at once on a small machine is how it runs out of
    memory and is killed, which loses every plan on it — including those
    belonging to people who were only reading results."""
    at_once = []
    peak = 0
    lock = threading.Lock()

    def read_a_plan():
        with workload.a_turn_to_read():
            nonlocal peak
            with lock:
                at_once.append(1)
                peak = max(peak, len(at_once))
            time.sleep(0.05)
            with lock:
                at_once.pop()

    workers = []
    for _ in range(5):
        workload.claim_a_place()
        worker = threading.Thread(target=read_a_plan)
        worker.start()
        workers.append(worker)
    for worker in workers:
        worker.join(timeout=30)

    assert peak <= workload.MAX_CONCURRENT
    assert workload.snapshot()["reading_now"] == 0
    assert workload.snapshot()["waiting"] == 0


def test_a_full_queue_is_refused_rather_than_accepted_and_dropped():
    """A queue with no limit is a slower way to run out of memory. The reply
    says the server is busy; it does not take work it cannot do."""
    for _ in range(workload.MAX_WAITING):
        workload.claim_a_place()
    try:
        with pytest.raises(workload.TooBusy):
            workload.claim_a_place()
    finally:
        for _ in range(workload.MAX_WAITING):
            workload.release_place()
    assert workload.snapshot()["waiting"] == 0


def test_a_waiting_plan_keeps_saying_that_it_is_waiting(monkeypatch):
    """Waiting silently is indistinguishable from being stuck, and the browser
    gives up on a server that says nothing."""
    # A tenth of a second instead of the real few seconds, so the test does not
    # have to wait out a real queue to prove the report is made.
    monkeypatch.setattr(workload, "_WAIT_TICK_SECONDS", 0.05)
    said = []
    holding = threading.Event()
    holders_in = threading.Semaphore(0)

    def hold_a_slot():
        with workload.a_turn_to_read():
            holders_in.release()
            holding.wait(timeout=10)

    # Every slot has to be taken before anything waits, and how many there are
    # depends on the memory this machine has.
    holders = []
    for _ in range(workload.MAX_CONCURRENT):
        workload.claim_a_place()
        holder = threading.Thread(target=hold_a_slot)
        holder.start()
        holders.append(holder)
    for _ in range(workload.MAX_CONCURRENT):
        assert holders_in.acquire(timeout=10)

    def wait_for_a_turn():
        workload.claim_a_place()
        with workload.a_turn_to_read(on_wait=lambda state: said.append(state)):
            pass

    waiter = threading.Thread(target=wait_for_a_turn)
    waiter.start()
    time.sleep(0.3)
    holding.set()
    waiter.join(timeout=10)
    for holder in holders:
        holder.join(timeout=10)

    assert said, "a plan that waited said nothing while it waited"


# --- whose folder may be removed ------------------------------------------


def _make_run(name: str):
    folder = OUTPUT_PLAN_DIR / name
    (folder / "pages").mkdir(parents=True, exist_ok=True)
    (folder / "source.pdf").write_bytes(b"%PDF-1.4\n")
    return folder


def test_a_new_upload_does_not_delete_a_plan_someone_else_is_reading():
    """The marked-up sheets, page images and downloads are all drawn on demand
    from the run folder's source PDF. Deleting another reader's folder took
    their results away underneath them, with no picture and no explanation —
    which is exactly what happened with the site open in two tabs."""
    theirs = _make_run("29990101T000000Z-theirs")
    mine = _make_run("29990101T000001Z-mine")
    try:
        prune_runs(keep_run_id=mine.name, keep_recent=5, max_age_hours=6)
        assert theirs.is_dir(), "another reader's plan was deleted by this upload"
        assert mine.is_dir()
    finally:
        remove_run(theirs.name)
        remove_run(mine.name)


def test_a_run_nobody_can_be_reading_any_more_is_cleared():
    """Kept for ever is not the answer either: a folder holds a whole plan
    set, and nothing on screen can ever open one that old again."""
    old = _make_run("19990101T000000Z-old")
    import os

    long_ago = time.time() - 60 * 60 * 24
    os.utime(old, (long_ago, long_ago))
    current = _make_run("29990101T000002Z-current")
    try:
        prune_runs(keep_run_id=current.name, keep_recent=1, max_age_hours=6)
        assert not old.is_dir()
        assert current.is_dir()
    finally:
        remove_run(old.name)
        remove_run(current.name)


def test_removing_one_run_removes_only_that_one():
    """Discarding used to remove every folder on the server, so one reader
    closing their tab deleted the plan another was still looking at."""
    first = _make_run("29990101T000003Z-first")
    second = _make_run("29990101T000004Z-second")
    try:
        assert remove_run(first.name) is True
        assert not first.is_dir()
        assert second.is_dir()
    finally:
        remove_run(first.name)
        remove_run(second.name)


def test_asking_whether_recognition_is_available_costs_nothing():
    """Asking used to import the recognition library, which takes 184 MB and
    24 seconds — paid by every upload that so much as asked, including one
    whose every sheet carries its own text and never needed it."""
    import sys

    from pipeline.plan.ocr import ocr_is_available

    loaded_before = "paddle" in sys.modules or "paddleocr" in sys.modules
    available, why = ocr_is_available()
    assert isinstance(available, bool)
    if not loaded_before:
        assert "paddleocr" not in sys.modules, (
            "asking whether recognition is available loaded it"
        )
    if not available:
        assert why, "an unavailable engine must say why"


# --- the entry point a hosted Space starts --------------------------------


def test_the_space_entry_point_serves_the_same_api():
    """A Space runs one Python file, and that file must start the same
    application — not a second copy of it that can drift."""
    import importlib.util

    root = Path(__file__).resolve().parents[2]
    entry = root / "space_app.py"
    assert entry.is_file(), "the file a Space is told to run is missing"

    spec = importlib.util.spec_from_file_location("space_app_under_test", entry)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    from app.main import app as the_real_api

    assert module.api is the_real_api, "the Space would start a different application"


def test_the_landing_page_never_stands_in_front_of_the_api():
    """The page at / is a courtesy. Anything that answered for /api/... would
    take the whole service down while looking perfectly healthy.

    Asked of the application rather than of its list of routes, because what
    matters is which one answers — and a newer FastAPI does not keep an
    included router's routes in a flat list to be read off.
    """
    import importlib.util

    from fastapi.testclient import TestClient

    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "space_app_routes", root / "space_app.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    with TestClient(module._with_a_landing_page(module.api)) as client:
        answer = client.get("/api/plan/health")
        assert answer.status_code == 200, "the landing page answered for the API"
        assert answer.json()["status"] == "ok"

        page = client.get("/")
        assert page.status_code == 200
        assert "text/html" in page.headers["content-type"]


def test_the_landing_page_never_takes_a_port_of_its_own():
    """Mounted with its defaults, the Space's toolkit starts a Node rendering
    server on the very port the API is served on, and the API can then never
    bind it. It worked on a laptop, where Node is not installed, and failed on
    the Space, where it is."""
    root = Path(__file__).resolve().parents[2]
    source = (root / "space_app.py").read_text(encoding="utf-8")
    assert "mount_gradio_app" in source
    assert "ssr_mode=False" in source, "the rendering server would take the API's port"


def test_there_is_exactly_one_list_of_packages():
    """Two lists drift apart, and the drift is only found when a deployment
    breaks. It has to be the one at the top of the repository, because a Space
    mounts that file on its own with none of the repository around it — a list
    pointing elsewhere with `-r` cannot be followed, and the build stops before
    anything is installed."""
    root = Path(__file__).resolve().parents[2]
    at_the_root = (root / "requirements.txt").read_text(encoding="utf-8")
    assert "fastapi" in at_the_root, "the real list must be the one a Space can read"
    assert "-r " not in at_the_root, "a Space cannot follow a pointer to another file"
    assert not (root / "backend" / "requirements.txt").exists(), "there are two lists again"


def test_character_recognition_can_be_turned_off_without_rebuilding(monkeypatch):
    """The recognition models are the largest thing this application holds -
    184 MB on top of a 345 MB working set. Whether a machine has room for them
    is a fact about the machine, so it is a setting rather than a rebuild."""
    from pipeline.plan.ocr import ocr_is_available

    monkeypatch.setenv("OCR_ENABLED", "false")
    available, why = ocr_is_available()
    assert available is False
    assert "switched off" in why

    monkeypatch.setenv("OCR_ENABLED", "true")
    available, _ = ocr_is_available()
    assert available is True


def test_a_sheet_says_recognition_was_switched_off_rather_than_coming_back_blank(
    monkeypatch, tmp_path
):
    """A reader cannot tell a drawing with nothing on it from a drawing this
    tool could not read. Every sheet that produces no text says why."""
    from pipeline.plan.ocr import run_ocr_on_page

    monkeypatch.setenv("OCR_ENABLED", "false")
    result = run_ocr_on_page(tmp_path / "never_read.png")
    assert result["status"] == "unavailable"
    assert result["blocks"] == []
    assert "not available on this server" in result["error"]


def test_how_many_plans_at_once_follows_the_memory_the_machine_has():
    """One at a time is right for a small container and wrong for a large one,
    and which of the two this is only becomes knowable once it is running."""
    from app.workload import (
        _MEMORY_PER_READING_MB,
        _MEMORY_TO_LEAVE_ALONE_MB,
        _MOST_WORTH_RUNNING_AT_ONCE,
    )

    def fits(megabytes):
        room = megabytes - _MEMORY_TO_LEAVE_ALONE_MB
        if room <= 0:
            return 1
        return max(1, min(int(room // _MEMORY_PER_READING_MB), _MOST_WORTH_RUNNING_AT_ONCE))

    # 345 MB is the measured peak for one reading. A box that cannot hold two
    # of those must never try, whatever else is true.
    assert fits(512) == 1
    assert fits(1024) == 1
    assert fits(16384) == _MOST_WORTH_RUNNING_AT_ONCE
    assert fits(64) == 1, "a figure smaller than the reserve must not go negative"


def test_a_container_is_asked_about_its_own_limit_first(tmp_path, monkeypatch):
    """The machine underneath a container may have far more memory than the
    container is allowed. Answering from the machine is how a service decides
    it can read six plans at once inside a box that allows one."""
    from app import workload

    limit = tmp_path / "memory.max"
    limit.write_text("536870912")  # 512 MB
    real_open = open

    def fake_open(path, *args, **kwargs):
        if str(path) == "/sys/fs/cgroup/memory.max":
            return real_open(limit, *args, **kwargs)
        raise OSError("not this one")

    monkeypatch.setattr("builtins.open", fake_open)
    assert round(workload._memory_this_machine_has_mb()) == 512
