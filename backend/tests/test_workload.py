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
    holder_started = threading.Event()

    def hold_the_slot():
        with workload.a_turn_to_read():
            holder_started.set()
            time.sleep(0.4)

    workload.claim_a_place()
    holder = threading.Thread(target=hold_the_slot)
    holder.start()
    assert holder_started.wait(timeout=10)

    workload.claim_a_place()
    with workload.a_turn_to_read(on_wait=lambda state: said.append(state)):
        pass
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
    """The page mounted at / is a courtesy. A mount that swallowed /api/... would
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


def test_a_space_reads_one_list_of_packages_not_two():
    """Two lists drift apart, and the drift is only found when a deployment
    breaks."""
    root = Path(__file__).resolve().parents[2]
    at_the_root = (root / "requirements.txt").read_text(encoding="utf-8")
    assert "-r backend/requirements.txt" in at_the_root
    assert "fastapi" not in at_the_root, "the root list repeats a package instead of pointing at it"
