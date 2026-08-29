"""Day 5 — building the model for one sheet, and writing it out.

One entry point, called when a reader asks for a sheet's model. The model is
built on request rather than during the upload because the reader chooses the
sheet: a plan set often draws the same outline three times over — the floor
plan, the reflected-ceiling plan and the electrical plan — and only the reader
knows which one they want built.

Everything produced lands in the run's own folder, so it is discarded with the
run like every other output.
"""

import json

import fitz

from app.logging_setup import get_logger
from pipeline.model.canonical import (
    build_model,
    choose_default_sheet,
    modellable_sheets,
)
from pipeline.model.exporters import write_glb, write_ifc, write_obj

logger = get_logger()


def model_dir(run_dir):
    """Where a run's 3D output lives. Created on first use."""
    out = run_dir / "model"
    out.mkdir(parents=True, exist_ok=True)
    return out


def model_paths(run_dir, page_number: int) -> dict:
    """The files for one sheet's model. One set per sheet, so a reader can
    build several and compare them."""
    out = model_dir(run_dir)
    return {
        "json": out / f"project_model_page{page_number:03d}.json",
        "glb": out / f"model_page{page_number:03d}.glb",
        "obj": out / f"model_page{page_number:03d}.obj",
        "ifc": out / f"model_page{page_number:03d}.ifc",
    }


def available_sheets(reading: dict, config: dict) -> dict:
    """Which sheets a model can be built from, and which is offered first."""
    sheets = modellable_sheets(reading["pages"], config)
    return {"sheets": sheets, "default_page_number": choose_default_sheet(sheets)}


def build_for_sheet(
    run_dir,
    reading: dict,
    page_number: int,
    config: dict,
    run_id: str,
    source_file: str,
) -> dict:
    """Builds and writes the canonical model and its 3D files for one sheet.

    Returns the model itself, with a record of which files were written. A
    format that could not be written is reported as such rather than leaving
    the reader with a download that does not exist.
    """
    page = next(
        (p for p in reading["pages"] if p["page_number"] == page_number), None
    )
    if page is None:
        raise ValueError(f"This document has no page {page_number}.")

    # The sheet's height in points, needed to turn the page's downward Y into
    # the building's northward Y.
    page_height_pt = 0.0
    try:
        with fitz.open(str(run_dir / "source.pdf")) as document:
            page_height_pt = document[page_number - 1].rect.height
    except Exception as e:
        logger.exception(f"could not read the page size for page {page_number}: {e}")

    from pipeline.model.height import resolve_storey_height

    height = resolve_storey_height(reading["pages"], config)
    model = build_model(page, height, config, run_id, source_file, page_height_pt)

    paths = model_paths(run_dir, page_number)
    try:
        paths["json"].write_text(
            json.dumps(model, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        model_written = True
    except Exception as e:
        logger.exception(f"could not write project_model.json: {e}")
        model_written = False

    model["files"] = {
        "project_model_json": model_written,
        "glb": write_glb(model, paths["glb"]),
        "obj": write_obj(model, paths["obj"]),
        "ifc": write_ifc(model, paths["ifc"]),
    }
    return model


def load_model(run_dir, page_number: int):
    """A model already built for this sheet, or None."""
    path = model_paths(run_dir, page_number)["json"]
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.exception(f"could not read the stored model for page {page_number}: {e}")
        return None
