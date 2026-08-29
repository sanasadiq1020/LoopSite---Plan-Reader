"""Central, single-source-of-truth filesystem paths for the backend.

Nothing in the pipeline should hardcode a path string directly — import from
here instead, so the folder structure only needs to change in one place.
"""

import shutil
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


def discard_other_runs(keep_run_id: str = "") -> int:
    """Removes every plan-reading run folder except the one named.

    **A deliberate change from the Handbook's folder spec.** That spec keeps
    every run so an older run's evidence is never overwritten, which is right
    for a batch tool. This is a web app that shows one plan at a time: the
    interface has no run browser, a reader only ever sees the plan they just
    uploaded, and 210 stale folders on disk are not evidence of anything — they
    are the leftovers of tests nobody will read. So the folder on disk now
    matches what the screen shows.

    The run being viewed is never removed, and this is only ever called when a
    new upload replaces it or a reader discards it.
    """
    removed = 0
    if not OUTPUT_PLAN_DIR.is_dir():
        return removed
    for folder in OUTPUT_PLAN_DIR.iterdir():
        if not folder.is_dir() or folder.name == keep_run_id:
            continue
        try:
            shutil.rmtree(folder)
            removed += 1
        except Exception:
            # A folder still open in another window is left alone rather than
            # failing the upload the reader is waiting on.
            pass
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
