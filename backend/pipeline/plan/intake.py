"""Day 1-2 — PDF intake + plan-reading text pipeline.

Classifies every page (vector / raster / mixed), renders a 150 DPI thumbnail,
extracts whatever native text/bounding boxes exist, runs OCR as a fallback on
pages that need it, and writes source-linked output files. Construction PDFs
here are usually scanned/image-based, so a raster page having near-zero
native text is expected and normal — that is exactly what OCR (ocr.py)
exists to cover.

Every page is processed inside its own try/except: one bad page is logged and
marked failed, it never takes down the whole run (Critical Rule 6).
"""

import csv
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import fitz  # PyMuPDF

from app import progress
from app.logging_setup import get_logger
from app.paths import (
    CONFIG_DIR,
    OUTPUT_ISSUES_DIR,
    OUTPUT_PLAN_DIR,
    PROJECT_ROOT,
    discard_other_runs,
    run_plan_dir,
)
from pipeline.plan.ocr import render_dpi as ocr_render_dpi
from pipeline.plan.ocr import run_ocr_on_page, should_run_ocr
from pipeline.plan.overlay import render_overlay
from pipeline.plan.reading import (
    _empty_page,
    analyze_page,
    compute_metrics,
    load_config,
    page_lines_and_rulings,
    write_plan_reading_csvs,
    write_unresolved_items_csv,
)
from pipeline.plan.accuracy import (
    plan_file_named_in,
    written_for_another_plan,
    build_ground_truth_template,
    compare,
    load_ground_truth,
    write_accuracy_report,
    write_ground_truth_template,
)
from pipeline.plan.openings import reconcile_openings_with_schedules
from pipeline.plan.sheetindex import cross_check_pages

logger = get_logger()

THUMBNAIL_DPI = 150
MIN_TEXT_CHARS_FOR_VECTOR = 20
FULL_PAGE_IMAGE_AREA_RATIO = 0.6


def generate_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def _image_area_ratio(page: "fitz.Page") -> float:
    """Rough fraction of the page area covered by embedded raster images."""
    page_area = page.rect.width * page.rect.height
    if page_area <= 0:
        return 0.0

    covered = 0.0
    for img in page.get_images(full=True):
        xref = img[0]
        try:
            rects = page.get_image_rects(xref)
        except Exception:
            continue
        for rect in rects:
            covered += rect.width * rect.height

    return min(covered / page_area, 1.0)


def classify_page(page: "fitz.Page") -> tuple[str, str]:
    """Returns (classification, human-readable reasoning) for traceability/logs."""
    try:
        # Uses the text already extracted for this page rather than asking
        # PyMuPDF to build a second text page for it.
        from pipeline.plan.textmodel import native_text_of

        text = native_text_of(page).strip()
        text_len = len(text)
        image_ratio = _image_area_ratio(page)
        from pipeline.plan.layout import page_drawings

        drawing_count = len(page_drawings(page))

        has_significant_text = text_len >= MIN_TEXT_CHARS_FOR_VECTOR
        has_full_page_image = image_ratio >= FULL_PAGE_IMAGE_AREA_RATIO

        if has_full_page_image and not has_significant_text:
            return "raster", f"image_ratio={image_ratio:.2f}, text_chars={text_len}"
        if has_significant_text and not has_full_page_image:
            return "vector", f"text_chars={text_len}, image_ratio={image_ratio:.2f}"
        if has_significant_text and has_full_page_image:
            return "mixed", f"text_chars={text_len}, image_ratio={image_ratio:.2f}"
        if drawing_count > 0 and not has_full_page_image:
            return "vector", f"drawing_count={drawing_count}, text_chars={text_len}"

        return "unknown", (
            f"text_chars={text_len}, image_ratio={image_ratio:.2f}, "
            f"drawing_count={drawing_count}"
        )
    except Exception as e:  # never let classification crash the page
        logger.exception(f"classify_page failed: {e}")
        return "unknown", f"classification_error: {e}"


def render_page(page: "fitz.Page", dpi: int = THUMBNAIL_DPI):
    """One render of a page, reused by everything that needs its pixels.

    The page image is wanted three times over — as the thumbnail, as the
    background of the marked-up sheet, and (on a sheet drawn as a picture) as
    the source of its wall lines. Rendering it once and passing it on removed
    six of a six-page upload's fourteen renders.
    """
    zoom = dpi / 72.0
    return page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))


def render_thumbnail(page: "fitz.Page", out_path: Path, dpi: int = THUMBNAIL_DPI, pixmap=None):
    (pixmap if pixmap is not None else render_page(page, dpi)).save(str(out_path))


def extract_native_text_blocks(page: "fitz.Page") -> list[dict]:
    """Line-level native text for raw_text.json.

    The plan-reading pipeline builds its own richer text model (direction,
    font size, de-duplication) in pipeline.plan.textmodel; this thinner form
    exists so raw_text.json stays a plain, readable record of what was on the
    page, which is what Week 1 Gate 3 asks for.
    """
    from pipeline.plan.textmodel import extract_native_lines

    return [
        {"text": line["text"], "bbox": line["bbox"], "axis": line["axis"], "size": line["size"]}
        for line in extract_native_lines(page)
    ]


# Week 1 Gate 2 requires every page to appear once with its printed sheet
# number/title when present, its discipline/type, its vector/raster/mixed
# status and its extraction status. The identity columns come from the
# plan-reading stage, which is why the register is written after it runs.
CSV_COLUMNS = [
    "page_number",
    "sheet_id",
    "sheet_number",
    "sheet_title",
    "discipline",
    "page_type",
    "scale",
    "revision",
    "classification",
    "extraction_status",
    "extraction_method",
    "native_text_char_count",
    "ocr_text_char_count",
    "ocr_status",
    "unresolved_p1",
    "unresolved_p2",
    "width_pt",
    "height_pt",
    "thumbnail_path",
    "overlay_path",
    "error",
]


def _write_sheet_register_csv(csv_path: Path, sheets: list[dict]) -> None:
    """Handbook-required sheet_register.csv — one row per supplied page,
    every page appearing exactly once (Week 1 Definition of Done, Gate 2)."""
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for s in sheets:
            writer.writerow({column: s.get(column, "") for column in CSV_COLUMNS} | {
                "thumbnail_path": s.get("thumbnail_url", ""),
                "overlay_path": s.get("overlay_url") or "",
                "error": s.get("error") or "",
            })


def process_upload(
    file_bytes: bytes,
    original_filename: str,
    session_id: str,
    progress_token: str = "",
) -> dict:
    run_id = generate_run_id()
    run_dir = run_plan_dir(run_id)
    pages_dir = run_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    file_hash = hashlib.sha256(file_bytes).hexdigest()
    source_path = run_dir / "source.pdf"
    source_path.write_bytes(file_bytes)  # preserve the approved input unchanged

    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
    except Exception as e:
        logger.exception(f"run={run_id} could not open PDF '{original_filename}': {e}")
        raise ValueError(f"Could not open PDF: {e}") from e

    page_count = doc.page_count
    sheets: list[dict] = []
    raw_text_pages: list[dict] = []
    plan_reading_pages: list[dict] = []
    # Page objects are kept so the drawing index, the cross-check and the
    # overlays can be produced after every page has been read — the index is
    # printed on one sheet but describes them all.
    page_objects: dict = {}
    page_renders: dict = {}

    # How much OCR this whole upload is allowed, so a scanned set can never
    # leave the browser waiting for an hour.
    ocr_seconds_used = 0.0
    try:
        ocr_run_budget = float(
            load_config().get("ocr", {}).get("run_budget_seconds", 900)
        )
    except Exception:
        ocr_run_budget = 900.0

    progress.set_page_count(progress_token, page_count)

    for i in range(page_count):
        page_number = i + 1
        try:
            page = doc.load_page(i)
            classification, reasoning = classify_page(page)

            thumb_filename = f"page_{page_number:03d}.png"
            thumb_path = pages_dir / thumb_filename
            page_render = render_page(page)
            render_thumbnail(page, thumb_path, pixmap=page_render)

            text_blocks = extract_native_text_blocks(page)
            native_char_count = sum(len(b["text"]) for b in text_blocks)

            ocr_blocks: list[dict] = []
            ocr_char_count = 0
            ocr_status = "skipped"
            ocr_error = None
            ocr_duration_s = 0.0

            if should_run_ocr(classification, native_char_count):
                if ocr_seconds_used >= ocr_run_budget:
                    # One upload gets a fixed amount of OCR. A scanned set of
                    # twenty sheets would otherwise run for over an hour with
                    # the browser showing nothing at all. The page is still
                    # read from whatever text it has, and it says plainly that
                    # it was not read by OCR rather than looking empty.
                    ocr_status = "not_attempted_budget_spent"
                    logger.warning(
                        f"run={run_id} page={page_number} skipped OCR: this upload's "
                        f"{ocr_run_budget:.0f}s of OCR is already spent"
                    )
                else:
                    logger.info(f"run={run_id} page={page_number} running OCR fallback")
                    ocr_result = run_ocr_on_page(thumb_path)
                    ocr_blocks = ocr_result["blocks"]
                    ocr_char_count = sum(len(b["text"]) for b in ocr_blocks)
                    ocr_status = ocr_result["status"]
                    ocr_error = ocr_result["error"]
                    ocr_duration_s = ocr_result["duration_s"]
                    ocr_seconds_used += ocr_duration_s
                    logger.info(
                        f"run={run_id} page={page_number} OCR status={ocr_status} "
                        f"chars={ocr_char_count} duration={ocr_duration_s}s"
                    )

            # One text model per page, shared by every detector: native text
            # and OCR text merged into a single coordinate system, with
            # duplicates and template overprints already resolved.
            page_lines, text_evidence, rulings = page_lines_and_rulings(
                page, ocr_blocks, ocr_render_dpi(THUMBNAIL_DPI)
            )
            page_reading = analyze_page(
                page_number=page_number,
                page_count=page_count,
                page_width=page.rect.width,
                page_height=page.rect.height,
                lines=page_lines,
                text_evidence=text_evidence,
                rulings=rulings,
                page=page,
            )
            plan_reading_pages.append(page_reading)
            page_objects[page_number] = page
            progress.page_done(
                progress_token,
                page_number,
                page_count,
                page_reading.get("sheet_id") or "",
            )
            page_renders[page_number] = page_render

            has_native = native_char_count > 0
            has_ocr = ocr_char_count > 0
            if has_native and has_ocr:
                extraction_method = "native_and_ocr"
            elif has_native:
                extraction_method = "native"
            elif has_ocr:
                extraction_method = "ocr"
            else:
                extraction_method = "none"

            has_any_text = has_native or has_ocr
            extraction_status = "ok"
            if classification == "unknown" or not has_any_text:
                extraction_status = "partial"
            elif ocr_status in ("timeout", "failed") and not has_native:
                extraction_status = "partial"

            sheets.append(
                {
                    "run_id": run_id,
                    "page_number": page_number,
                    "classification": classification,
                    "extraction_status": extraction_status,
                    "extraction_method": extraction_method,
                    "native_text_char_count": native_char_count,
                    "ocr_text_char_count": ocr_char_count,
                    "ocr_status": ocr_status,
                    "thumbnail_url": f"/api/plan/{run_id}/pages/{thumb_filename}",
                    "width_pt": round(page.rect.width, 2),
                    "height_pt": round(page.rect.height, 2),
                    "error": None,
                }
            )
            raw_text_pages.append(
                {
                    "page_number": page_number,
                    "classification": classification,
                    "classification_reasoning": reasoning,
                    "native_blocks": text_blocks,
                    "ocr_blocks": ocr_blocks,
                    "ocr_status": ocr_status,
                    "ocr_error": ocr_error,
                    "ocr_duration_s": ocr_duration_s,
                }
            )
            logger.info(
                f"run={run_id} page={page_number} classified={classification} "
                f"method={extraction_method} native_chars={native_char_count} "
                f"ocr_chars={ocr_char_count} reasoning=({reasoning})"
            )
        except Exception as e:
            logger.exception(f"run={run_id} page={page_number} FAILED: {e}")
            sheets.append(
                {
                    "run_id": run_id,
                    "page_number": page_number,
                    "classification": "unknown",
                    "extraction_status": "failed",
                    "extraction_method": "none",
                    "native_text_char_count": 0,
                    "ocr_text_char_count": 0,
                    "ocr_status": "skipped",
                    "thumbnail_url": "",
                    "width_pt": 0.0,
                    "height_pt": 0.0,
                    "error": str(e),
                }
            )
            raw_text_pages.append(
                {
                    "page_number": page_number,
                    "classification": "unknown",
                    "classification_reasoning": "processing error",
                    "native_blocks": [],
                    "ocr_blocks": [],
                    "ocr_status": "skipped",
                    "ocr_error": None,
                    "ocr_duration_s": 0.0,
                    "error": str(e),
                }
            )
            plan_reading_pages.append(
                _empty_page(page_number, f"Page failed before plan-reading could run: {e}")
            )

    # --- Drawing index and cross-check -----------------------------------
    # The index is normally on the cover but is searched for on every page, so
    # a set that prints it elsewhere still gets the benefit. Once found, every
    # sheet's own title block is checked against it and any disagreement
    # becomes a visible finding rather than a silent choice.
    config = load_config()
    sheet_index = next(
        (r["sheet_index"] for r in plan_reading_pages if r.get("sheet_index")), None
    )

    # Door and window schedules are printed on their own sheets, so marks can
    # only be matched to them once every page has been read.
    try:
        opening_reconciliation = reconcile_openings_with_schedules(plan_reading_pages)
    except Exception as e:
        logger.exception(f"run={run_id} opening reconciliation failed: {e}")
        opening_reconciliation = {}

    try:
        cross_check = cross_check_pages(plan_reading_pages, sheet_index, config)
    except Exception as e:
        logger.exception(f"run={run_id} cross-check failed: {e}")
        cross_check = {}

    # --- Source overlays --------------------------------------------------
    overlays_dir = run_dir / "overlays"
    overlays_dir.mkdir(parents=True, exist_ok=True)
    for reading in plan_reading_pages:
        progress.set_stage(progress_token, "Marking up the sheets", 88)
        page = page_objects.get(reading["page_number"])
        if page is None:
            continue
        overlay_filename = f"overlay_{reading['page_number']:03d}.png"
        if render_overlay(
            page,
            reading,
            overlays_dir / overlay_filename,
            config,
            page_pixmap=page_renders.get(reading["page_number"]),
        ):
            reading["overlay_url"] = f"/api/plan/{run_id}/overlays/{overlay_filename}"

    # --- Fold the reading back into the sheet register (Gate 2) -----------
    reading_by_page = {r["page_number"]: r for r in plan_reading_pages}
    for sheet in sheets:
        reading = reading_by_page.get(sheet["page_number"])
        if reading is None:
            continue
        fields = reading["title_block"]
        sheet["sheet_id"] = reading["sheet_id"]
        sheet["sheet_number"] = fields["sheet_number"]["value"] or ""
        sheet["sheet_title"] = fields["sheet_title"]["value"] or ""
        sheet["discipline"] = fields["discipline"]["value"] or ""
        sheet["page_type"] = reading["page_type"]["value"]
        sheet["scale"] = fields["scale"]["value"] or ""
        sheet["revision"] = fields["revision"]["value"] or ""
        sheet["overlay_url"] = reading.get("overlay_url") or ""
        sheet["unresolved_p1"] = sum(
            1 for item in reading["unresolved_items"] if item["severity"] == "P1"
        )
        sheet["unresolved_p2"] = sum(
            1 for item in reading["unresolved_items"] if item["severity"] == "P2"
        )

    # A ready-to-fill reference template for whichever sheet carries the most
    # to check. Written every run so a reviewer never has to start from a blank
    # page — and clearly marked so a seeded row is never mistaken for evidence.
    try:
        richest = max(
            (p for p in plan_reading_pages if not p.get("error")),
            key=lambda p: len(p["rooms"]) + len(p.get("openings", [])) + len(p["dimensions"]),
            default=None,
        )
        if richest is not None:
            page_object = page_objects.get(richest["page_number"])
            template_rows = build_ground_truth_template(
                richest,
                page_object.rect.width if page_object else 0.0,
                page_object.rect.height if page_object else 0.0,
            )
            write_ground_truth_template(
                run_dir / "ground_truth_template.csv", template_rows, plan_file=original_filename
            )
    except Exception as e:
        logger.exception(f"run={run_id} could not write the ground-truth template: {e}")

    # Day 4. Measured against the manually checked reference, which is the only
    # thing that can turn output into an accuracy claim (Handbook Section 7).
    accuracy_report = {}
    try:
        reference_path = PROJECT_ROOT / "tests" / "ground_truth.csv"
        reference = load_ground_truth(reference_path)
        named_plan = plan_file_named_in(reference_path)
        if reference and named_plan and named_plan.lower() != original_filename.lower():
            # The checking sheet on file describes a different plan. Scoring
            # this upload against it would invent misses, because both
            # documents have a "Page 1".
            accuracy_report = written_for_another_plan(named_plan, original_filename)
            logger.warning(
                f"run={run_id} the checking sheet is written for {named_plan!r}, "
                f"not {original_filename!r} - no accuracy measured"
            )
        elif reference:
            accuracy_report = compare(plan_reading_pages, reference, config)
            write_accuracy_report(
                run_dir / "accuracy_report.csv", run_id, accuracy_report
            )
        else:
            accuracy_report = {
                "reference_rows": 0,
                "per_item_type": {},
                "note": (
                    "No accuracy has been measured for this plan, and none is needed to "
                    "use it. Everything on this page was read from the PDF on its own, "
                    "and each value shows where on the sheet it came from and how "
                    "certain it is. If you want a measured figure as well, download the "
                    "Checking sheet above, answer its rows against the drawing, and save "
                    "it as tests/ground_truth.csv."
                ),
            }
    except Exception as e:
        logger.exception(f"run={run_id} accuracy comparison failed: {e}")

    try:
        metrics = compute_metrics(plan_reading_pages, cross_check, opening_reconciliation)
    except Exception as e:
        logger.exception(f"run={run_id} metrics failed: {e}")
        metrics = {}

    doc.close()

    sheet_register = {
        "run_id": run_id,
        "original_filename": original_filename,
        "file_sha256": file_hash,
        "page_count": page_count,
        "sheets": sheets,
    }
    (run_dir / "sheet_register.json").write_text(
        json.dumps(sheet_register, indent=2), encoding="utf-8"
    )
    _write_sheet_register_csv(run_dir / "sheet_register.csv", sheets)
    (run_dir / "raw_text.json").write_text(
        json.dumps({"run_id": run_id, "pages": raw_text_pages}, indent=2),
        encoding="utf-8",
    )

    plan_reading_generated_at = datetime.now(timezone.utc).isoformat()
    try:
        (run_dir / "plan_reading.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "format_version": PLAN_READING_FORMAT,
                    "generated_at": plan_reading_generated_at,
                    "sheet_index": sheet_index,
                    "cross_check": cross_check,
                    "opening_reconciliation": opening_reconciliation,
                    "accuracy": accuracy_report,
                    "metrics": metrics,
                    "pages": plan_reading_pages,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        issues_dir = OUTPUT_ISSUES_DIR / run_id
        issues_dir.mkdir(parents=True, exist_ok=True)
        write_unresolved_items_csv(
            issues_dir / "unresolved_items.csv", run_id, plan_reading_pages
        )
        write_plan_reading_csvs(run_dir, run_id, plan_reading_pages)
    except Exception as e:
        # plan_reading.json is additive on top of the Day 1/2 outputs above —
        # a failure here must not lose the sheet register/raw text already
        # written to disk (Critical Rule 6).
        logger.exception(f"run={run_id} could not write plan_reading outputs: {e}")

    manifest = {
        "run_id": run_id,
        "original_filename": original_filename,
        "file_sha256": file_hash,
        "page_count": page_count,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "source_path": str(source_path.relative_to(PROJECT_ROOT)),
        "session_id": session_id,
    }
    (run_dir / "input_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    # One plan at a time, on disk as well as on screen. The interface has no
    # run browser (Section 3.B.14), so a folder for a plan nobody can open is
    # only clutter. The run just written is kept; everything before it goes.
    try:
        removed = discard_other_runs(keep_run_id=run_id)
        if removed:
            logger.info(f"run={run_id} cleared {removed} earlier run folder(s)")
    except Exception as e:
        logger.exception(f"run={run_id} could not clear earlier runs: {e}")

    progress.finish(progress_token, run_id)
    logger.info(f"run={run_id} COMPLETE pages={page_count} file={original_filename}")

    return sheet_register


# Every plan_reading.json records the shape it was written in. An upload
# processed by an older version of the pipeline has a different shape, and
# rendering it would either fail or show a half-empty screen — so it is
# detected here and the caller is told to run the file through again, rather
# than the interface silently showing blanks.
PLAN_READING_FORMAT = 7


def load_plan_reading(run_id: str) -> dict | None:
    path = run_plan_dir(run_id) / "plan_reading.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("format_version") != PLAN_READING_FORMAT:
        return None
    return data


def plan_reading_is_outdated(run_id: str) -> bool:
    """True when this upload exists but was processed by an earlier version."""
    path = run_plan_dir(run_id) / "plan_reading.json"
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return True
    return data.get("format_version") != PLAN_READING_FORMAT


def load_release_info() -> dict:
    """What this release is and what it cannot do, from /config.

    Read from the file rather than written into the interface, so a change to
    what the product claims is a change to one configuration file and never a
    change to the words on a screen.
    """
    path = CONFIG_DIR / "version.json"
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
        return {key: value for key, value in info.items() if not key.startswith("_")}
    except Exception as e:
        logger.exception(f"could not read {path}: {e}")
        # Never invent a version. An interface that cannot say what it is says so.
        return {}


def load_sheet_register(run_id: str):
    """A finished run's sheet register, as the upload would have returned it."""
    path = run_plan_dir(run_id) / "sheet_register.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.exception(f"could not read the sheet register for {run_id}: {e}")
        return None


def load_manifest(run_id: str) -> dict | None:
    path = run_plan_dir(run_id) / "input_manifest.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


_PAGE_FILENAME_RE = re.compile(r"^page_\d{3}\.png$")


def resolve_page_image_path(run_id: str, filename: str) -> Path | None:
    """Only ever returns a path inside this run's own pages/ folder, and only
    for filenames matching the exact pattern this pipeline generates — blocks
    path traversal (`../../secrets`) regardless of what the caller sends."""
    if not _PAGE_FILENAME_RE.match(filename):
        return None
    path = run_plan_dir(run_id) / "pages" / filename
    if not path.is_file():
        return None
    return path


def resolve_sheet_register_csv_path(run_id: str) -> Path | None:
    path = run_plan_dir(run_id) / "sheet_register.csv"
    if not path.is_file():
        return None
    return path


_OVERLAY_FILENAME_RE = re.compile(r"^overlay_\d{3}\.png$")


def resolve_overlay_image_path(run_id: str, filename: str) -> Path | None:
    """Same containment rule as the page images: only a filename this
    pipeline generates, only inside this run's own folder."""
    if not _OVERLAY_FILENAME_RE.match(filename):
        return None
    path = run_plan_dir(run_id) / "overlays" / filename
    if not path.is_file():
        return None
    return path


_EXPORT_FILENAMES = {
    "rooms.csv",
    "dimensions.csv",
    "schedule_rows.csv",
    "walls.csv",
    "openings.csv",
    "accuracy_report.csv",
    "ground_truth_template.csv",
}


def resolve_export_path(run_id: str, filename: str) -> Path | None:
    if filename not in _EXPORT_FILENAMES:
        return None
    path = run_plan_dir(run_id) / filename
    if not path.is_file():
        return None
    return path


def build_overlays_zip(run_id: str) -> Path | None:
    """Every marked-up sheet for a run, as one download.

    Built on request rather than at upload time so a run that is never
    downloaded costs nothing, and rebuilt each time so it always matches the
    overlays currently on disk.
    """
    import zipfile

    overlays_dir = run_plan_dir(run_id) / "overlays"
    if not overlays_dir.is_dir():
        return None
    images = sorted(overlays_dir.glob("overlay_*.png"))
    if not images:
        return None

    zip_path = run_plan_dir(run_id) / f"marked_up_sheets_{run_id}.zip"
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for image in images:
                archive.write(image, arcname=image.name)
    except Exception as e:
        logger.exception(f"run={run_id} could not build the overlay download: {e}")
        return None
    return zip_path


def resolve_unresolved_csv_path(run_id: str) -> Path | None:
    path = OUTPUT_ISSUES_DIR / run_id / "unresolved_items.csv"
    if not path.is_file():
        return None
    return path


def run_belongs_to_session(run_id: str, session_id: str) -> bool:
    """The actual access-control check — used before returning sheets,
    serving a page image, or listing a run in someone's history. A run with
    no recorded owner (pre-Level-2 test data) belongs to no one."""
    manifest = load_manifest(run_id)
    if manifest is None:
        return False
    return manifest.get("session_id") == session_id
