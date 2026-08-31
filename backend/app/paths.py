"""Central, single-source-of-truth filesystem paths for the backend.

Nothing in the pipeline should hardcode a path string directly — import from
here instead, so the folder structure only needs to change in one place.
"""

import shutil
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

INPUT_DIR = PROJECT_ROOT / "input"
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = PROJECT_ROOT / "logs"
DOCS_DIR = PROJECT_ROOT / "docs"
RELEASES_DIR = PROJECT_ROOT / "releases"

OUTPUT_DIR = PROJECT_ROOT / "output"
OUTPUT_PLAN_DIR = OUTPUT_DIR / "plan"
OUTPUT_MODEL_DIR = OUTPUT_DIR / "model"
OUTPUT_ELEVATIONS_DIR = OUTPUT_DIR / "elevations"
OUTPUT_COMMERCIAL_DIR = OUTPUT_DIR / "commercial"
OUTPUT_CREW_DIR = OUTPUT_DIR / "crew"
OUTPUT_ISSUES_DIR = OUTPUT_DIR / "issues"


def run_plan_dir(run_id: str) -> Path:
    """Per-run output folder for the plan-reading stage."""
    return OUTPUT_PLAN_DIR / run_id


def remove_run(run_id: str) -> bool:
    """Removes exactly one run's folder. Never touches another."""
    if not run_id:
        return False
    folder = OUTPUT_PLAN_DIR / run_id
    if not folder.is_dir():
        return False
    try:
        shutil.rmtree(folder)
        return True
    except Exception:
        # A folder still open in another window is left alone rather than
        # failing the request the reader is waiting on.
        return False


def prune_runs(keep_run_id: str = "", keep_recent: int = 5, max_age_hours: float = 6.0) -> int:
    """Clears out runs nobody is looking at any more, and only those.

    **A new upload used to delete every other run on the server**, which was
    right while the interface could only ever show one plan and one person
    used it. It is wrong the moment two of them are open at once: the second
    upload deleted the first one's folder, and that reader's marked-up sheets,
    page images and downloads — all drawn on demand from the saved source PDF —
    stopped existing underneath them. What they saw was a sheet that could not
    be produced, with no picture behind it and no explanation.

    So a run is now kept until there is a real reason to remove it: it is not
    among the most recent few, **and** it is old enough that nobody can still
    be reading it. Run folders are named by the moment they were created, so
    "most recent" needs nothing but their names.

    Traceability is unchanged — every kept run still holds its source PDF,
    every table and its manifest — and session isolation is unchanged too: a
    folder being on disk has never been what allows anyone to read it.
    """
    removed = 0
    if not OUTPUT_PLAN_DIR.is_dir():
        return removed

    now = time.time()
    folders = sorted(
        (f for f in OUTPUT_PLAN_DIR.iterdir() if f.is_dir()),
        key=lambda f: f.name,
        reverse=True,
    )
    for position, folder in enumerate(folders):
        if folder.name == keep_run_id or position < keep_recent:
            continue
        try:
            age_hours = (now - folder.stat().st_mtime) / 3600.0
        except OSError:
            age_hours = max_age_hours + 1
        if age_hours < max_age_hours:
            continue
        if remove_run(folder.name):
            removed += 1
    return removed


def ensure_core_dirs() -> None:
    for d in (
        INPUT_DIR,
        CONFIG_DIR,
        DATA_DIR,
        LOGS_DIR,
        DOCS_DIR,
        RELEASES_DIR,
        OUTPUT_PLAN_DIR,
        OUTPUT_MODEL_DIR,
        OUTPUT_ELEVATIONS_DIR,
        OUTPUT_COMMERCIAL_DIR,
        OUTPUT_CREW_DIR,
        OUTPUT_ISSUES_DIR,
    ):
        d.mkdir(parents=True, exist_ok=True)
