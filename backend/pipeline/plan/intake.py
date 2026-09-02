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
    prune_runs,
    run_plan_dir,
)
from pipeline.plan.ocr import render_dpi as ocr_render_dpi
from pipeline.plan.ocr import run_ocr_on_page, should_run_ocr, text_layer_is_usable
from pipeline.plan.overlay import render_overlay
from pipeline.plan.textmodel import release_page_cache
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
from pipeline.plan.openings import (
    reconcile_openings_with_schedules,
    score_openings,
    settle_opening_placement,
)
from pipeline.plan.sheetindex import cross_check_pages

logger = get_logger()

THUMBNAIL_DPI = 150
MIN_TEXT_CHARS_FOR_VECTOR = 20
FULL_PAGE_IMAGE_AREA_RATIO = 0.6


def generate_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def _image_rects(page: "fitz.Page") -> list:
    """Where the embedded pictures sit on the page, worked out once.

    Cached on the page object because three separate questions need it - how
    much of the sheet is a picture, whether the drawing itself is a picture,
    and how the page should be classified - and asking PyMuPDF three times is
    the repeated work that made uploads slow.
    """
    cached = getattr(page, "_loopsite_image_rects", None)
    if cached is not None:
        return cached

    # **Asked for in one call, not once per picture.** `get_image_rects` scans
    # the whole page for each image it is asked about, so a sheet carrying ten
    # pictures was scanned ten times over. `get_image_info` returns every
    # picture's position from a single scan, which is the same answer for a
    # fraction of the work.
    rects = []
    try:
        for info in page.get_image_info(hashes=False, xrefs=False):
            box = info.get("bbox")
            if box:
                rects.append(fitz.Rect(box))
    except Exception:
        rects = []
    try:
        page._loopsite_image_rects = rects
    except AttributeError:
        pass  # a page object that refuses attributes still works, just slower
    return rects


def _image_area_ratio(page: "fitz.Page") -> float:
    """Rough fraction of the page area covered by embedded raster images."""
    page_area = page.rect.width * page.rect.height
    if page_area <= 0:
        return 0.0
    covered = sum(rect.width * rect.height for rect in _image_rects(page))
    return min(covered / page_area, 1.0)


def _drawing_is_a_picture(page: "fitz.Page", text_blocks: list) -> tuple[bool, str]:
    """Whether this sheet's drawing is a picture carrying lettering nobody read.

    **A sheet can have a text layer and still be half unread.** A very common
    shape is a title block typed as real text with a scanned or exported
    *image* of the drawing placed beside it. The sheet then has hundreds of
    characters, so it looks fully readable - and every room name, dimension
    and note inside the picture is invisible, because those are pixels.

    Counting the sheet's characters cannot see this. What can is *where* the
    characters are: if a large part of the sheet is a picture and almost
    nothing is printed inside that part, the lettering in it has not been read
    and the page image has to be looked at.

    The measurement is text lines per 100x100 points of picture. Measured
    across the drawings in use, every sheet that carries its own text prints
    between 0.41 and 2.85 lines that way; the default below is 0.15, which is
    well clear of all of them.
    """
    try:
        settings = load_config().get("ocr", {})
        min_share = float(settings.get("picture_drawing_min_image_share", 0.25))
        max_density = float(settings.get("picture_drawing_max_text_density", 0.15))
    except Exception:
        min_share, max_density = 0.25, 0.15

    rects = _image_rects(page)
    if not rects:
        return False, ""

    page_area = page.rect.width * page.rect.height
    image_area = sum(rect.width * rect.height for rect in rects)
    if page_area <= 0 or image_area <= 0:
        return False, ""
    if image_area / page_area < min_share:
        return False, ""

    inside = 0
    for block in text_blocks:
        x0, y0, x1, y1 = block["bbox"]
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        if any(r.x0 <= cx <= r.x1 and r.y0 <= cy <= r.y1 for r in rects):
            inside += 1

    density = inside / (image_area / 10000.0)
    if density <= max_density:
        return True, (
            f"the drawing on this sheet is a picture covering "
            f"{image_area / page_area:.0%} of it, with almost no text printed inside it"
        )
    return False, ""


def classify_page(page: "fitz.Page") -> tuple[str, str, dict]:
    """Returns (classification, human-readable reasoning, evidence).

    The evidence is returned rather than recomputed by the caller: how much
    text a page carries, how much of it is covered by images and how many
    paths it draws are each wanted again a few lines later, and asking
    PyMuPDF for them twice is the repeated work that made uploads slow.

    The classification describes what the page is **drawn with**. It is
    deliberately not used to decide whether the page needs reading by
    character recognition — that is decided by whether it carries any text,
    which is a different question (see ocr.should_run_ocr).
    """
    evidence = {"text_chars": 0, "image_ratio": 0.0, "drawing_count": 0}
    try:
        # Uses the text already extracted for this page rather than asking
        # PyMuPDF to build a second text page for it.
        from pipeline.plan.textmodel import native_text_of

        text = native_text_of(page).strip()
        text_len = len(text)
        image_ratio = _image_area_ratio(page)
        from pipeline.plan.layout import page_drawings

        drawing_count = len(page_drawings(page))

        evidence = {
            "text_chars": text_len,
            "image_ratio": round(image_ratio, 4),
            "drawing_count": drawing_count,
        }

        has_significant_text = text_len >= MIN_TEXT_CHARS_FOR_VECTOR
        has_full_page_image = image_ratio >= FULL_PAGE_IMAGE_AREA_RATIO

        if has_full_page_image and not has_significant_text:
            return "raster", f"image_ratio={image_ratio:.2f}, text_chars={text_len}", evidence
        if has_significant_text and not has_full_page_image:
            return "vector", f"text_chars={text_len}, image_ratio={image_ratio:.2f}", evidence
        if has_significant_text and has_full_page_image:
            return "mixed", f"text_chars={text_len}, image_ratio={image_ratio:.2f}", evidence
        if drawing_count > 0 and not has_full_page_image:
            return (
                "vector",
                f"drawing_count={drawing_count}, text_chars={text_len}",
                evidence,
            )

        return (
            "unknown",
            (
                f"text_chars={text_len}, image_ratio={image_ratio:.2f}, "
                f"drawing_count={drawing_count}"
            ),
            evidence,
        )
    except Exception as e:  # never let classification crash the page
        logger.exception(f"classify_page failed: {e}")
        return "unknown", f"classification_error: {e}", evidence


def _max_render_megapixels() -> float:
    """How large a page image this machine is willing to hold, in megapixels."""
    try:
        value = float(load_config().get("rendering", {}).get("max_megapixels", 40))
        return value if value > 0 else 40.0
    except Exception:
        return 40.0


def render_page(page: "fitz.Page", dpi: int = THUMBNAIL_DPI):
    """One render of a page, reused by everything that needs its pixels.

    The page image is wanted three times over — as the thumbnail, as the
    background of the marked-up sheet, and (on a sheet drawn as a picture) as
    the source of its wall lines. Rendering it once and passing it on removed
    six of a six-page upload's fourteen renders.

    **A sheet can be larger than the machine reading it.** Drawing sizes are
    not bounded: an A0 sheet at 150 DPI is a 35-megapixel image, around 105 MB
    held at once, and a plotted sheet longer than A0 is larger again. On a
    small server that render is what ends the run, and a run that ends part
    way through is exactly the empty result a reader cannot act on. So the
    resolution is reduced - never the sheet cropped - until the image fits the
    allowance in /config, and the reduction is logged. An ordinary A3 or A1
    sheet is nowhere near the limit and is unaffected.
    """
    zoom = dpi / 72.0
    limit_pixels = _max_render_megapixels() * 1_000_000
    pixels = (page.rect.width * zoom) * (page.rect.height * zoom)
    if pixels > limit_pixels > 0:
        # A whisker under the limit, because the rendered image is a whole
        # number of pixels in each direction and rounding up would otherwise
        # land just over an allowance chosen to be exactly affordable.
        zoom *= (limit_pixels * 0.99 / pixels) ** 0.5
        logger.warning(
            f"page is {page.rect.width:.0f}x{page.rect.height:.0f}pt: rendering it at "
            f"{zoom * 72:.0f} DPI instead of {dpi} to stay within "
            f"{_max_render_megapixels():.0f} megapixels"
        )
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
    "note",
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


def _why_nothing_was_read(
    has_any_text: bool,
    ocr_status: str,
    ocr_error,
    page_has_marks: bool,
    evidence: dict,
    text_layer_problem: str = "",
) -> str | None:
    """A sentence a reader can act on when a sheet came back with nothing.

    Written for the person reading the plan, so it says what happened to
    *their* drawing rather than naming a stage of the pipeline. Returns None
    for a sheet that read normally — a note is only ever an explanation of an
    absence.
    """
    if has_any_text:
        return None

    if not page_has_marks:
        return "This page of the file is blank — there is nothing printed or drawn on it."

    # A sheet whose stored text was discarded as unreadable is a different
    # situation from one that never had any, and the reader is told which:
    # the drawing is fine, the file's own record of its wording is not.
    if text_layer_problem:
        return (
            "The text stored in the file for this sheet cannot be read — "
            f"{text_layer_problem}. This usually means the drawing was exported without "
            "recording what its lettering says. Reading it from the sheet image "
            "produced nothing either, so no values were taken from it."
        )

    if ocr_status in ("unavailable", "failed", "timeout") and ocr_error:
        return str(ocr_error)

    if ocr_status == "not_attempted_budget_spent":
        return (
            "This sheet carries no text of its own, and the time this upload allows "
            "for reading scanned sheets was already spent on earlier sheets, so no "
            "text was read from it. The sheet image itself is still shown."
        )

    if evidence.get("drawing_count", 0) > 0:
        return (
            "No text could be read from this sheet. Its line work is present, so the "
            "lettering is most likely drawn as line work rather than stored as text — "
            "reading it depends on character recognition, which returned nothing here."
        )

    return "No text could be read from this sheet. The sheet image itself is still shown."


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
        raise ValueError(
            "This file could not be opened as a PDF. It may be damaged, or it may not "
            "be a PDF at all."
        ) from e

    # A PDF may be locked. Many drawings issued for tender carry an owner
    # password that permits reading with an empty user password, and those
    # open normally once that is tried. One that needs a real password cannot
    # be read at all, and saying so is the only honest answer — every page
    # would otherwise come back blank with no reason given.
    try:
        if doc.needs_pass and not doc.authenticate(""):
            raise ValueError(
                "This PDF is password-protected, so nothing on it can be read. Please "
                "supply a copy that opens without a password."
            )
    except ValueError:
        raise
    except Exception as e:
        logger.exception(f"run={run_id} could not unlock PDF '{original_filename}': {e}")
        raise ValueError(
            "This PDF appears to be protected and could not be opened for reading."
        ) from e

    if doc.page_count == 0:
        raise ValueError("This PDF has no pages in it.")

    page_count = doc.page_count
    sheets: list[dict] = []
    raw_text_pages: list[dict] = []
    plan_reading_pages: list[dict] = []
    # Page objects are kept so the drawing index, the cross-check and the
    # overlays can be produced after every page has been read — the index is
    # printed on one sheet but describes them all.
    page_objects: dict = {}

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
            classification, reasoning, page_evidence = classify_page(page)
            # Anything printed, drawn or placed on the sheet. A page with none
            # of the three is genuinely empty and is not worth offering to
            # character recognition; a page with any of them may still be
            # carrying its text as line work, and only recognition can say.
            page_has_marks = (
                page_evidence["text_chars"] > 0
                or page_evidence["image_ratio"] > 0
                or page_evidence["drawing_count"] > 0
            )

            # **The page image is not written here.** Nothing on the results
            # screen shows it: the sheet list is a table, and the picture of a
            # sheet is only ever wanted when a reader opens that sheet. Writing
            # all of them during the upload cost a fifth of the whole run for
            # images most readers never look at. The address is fixed by the
            # page number, so it can be given out now and the file made the
            # first time somebody asks for it.
            thumb_filename = f"page_{page_number:03d}.png"

            text_blocks = extract_native_text_blocks(page)
            native_char_count = sum(len(b["text"]) for b in text_blocks)

            # **Stored text is not always the drawing's wording.** A PDF need
            # not record what its glyphs mean, and plotting software often
            # omits it, so a sheet can carry thousands of characters that are
            # not the words printed on it. Counting them cannot tell the
            # difference, so the text itself is judged: a sheet whose stored
            # text is unreadable is treated as having none, is read from its
            # page image like any other such sheet, and says so.
            text_usable, text_layer_problem = text_layer_is_usable(
                "".join(b["text"] for b in text_blocks)
            )
            if not text_usable:
                logger.warning(
                    f"run={run_id} page={page_number} stored text is not readable "
                    f"({text_layer_problem}); reading the sheet from its image instead"
                )
            readable_native_chars = native_char_count if text_usable else 0

            # A sheet can carry a full title block as real text and still have
            # its entire drawing as a picture. The characters are there, so
            # nothing looks wrong, and every room name and dimension inside
            # the picture is unread. Where that is the case the page image is
            # read as well, and the two are merged - the text already on the
            # sheet is never thrown away.
            picture_drawing, picture_reason = _drawing_is_a_picture(page, text_blocks)
            if picture_drawing:
                logger.info(
                    f"run={run_id} page={page_number} reading the page image as well: "
                    f"{picture_reason}"
                )

            ocr_blocks: list[dict] = []
            ocr_char_count = 0
            ocr_status = "skipped"
            ocr_error = None
            ocr_duration_s = 0.0

            if should_run_ocr(readable_native_chars, page_has_marks) or picture_drawing:
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
                    # Reading letters off a picture needs a picture at the
                    # resolution reading needs, which is not the resolution a
                    # reader wants to look at. It is made here, used, and
                    # deleted - so only the sheets that actually need it pay
                    # for it.
                    ocr_image = run_dir / f"_ocr_page_{page_number:03d}.png"
                    render_thumbnail(page, ocr_image, dpi=ocr_render_dpi(THUMBNAIL_DPI))
                    try:
                        ocr_result = run_ocr_on_page(ocr_image)
                    finally:
                        try:
                            ocr_image.unlink(missing_ok=True)
                        except Exception:
                            pass
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
                page, ocr_blocks, ocr_render_dpi(THUMBNAIL_DPI), text_usable
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
            # Everything cached while reading this page is finished with. The
            # page itself is kept, because its pixels are needed later for the
            # marked-up sheet.
            release_page_cache(page)
            progress.page_done(
                progress_token,
                page_number,
                page_count,
                page_reading.get("sheet_id") or "",
            )

            has_native = readable_native_chars > 0
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
            elif ocr_status in ("timeout", "failed", "unavailable") and not has_native:
                extraction_status = "partial"

            # A sheet that produced nothing must say why on its own row. A
            # blank sheet with no explanation is the one outcome a reader
            # cannot act on: they cannot tell a drawing with nothing on it
            # from a drawing this tool could not read.
            sheet_note = _why_nothing_was_read(
                has_any_text=has_any_text,
                ocr_status=ocr_status,
                ocr_error=ocr_error,
                page_has_marks=page_has_marks,
                evidence=page_evidence,
                text_layer_problem=text_layer_problem,
            )
            # The screen shows what was read; the issues log says what to
            # check. A sheet nothing could be read from is exactly what a
            # reviewer needs on that list, with the reason recorded beside it.
            if sheet_note:
                page_reading.setdefault("unresolved_items", []).append(
                    {
                        "item_id": f"P{page_number:02d}-NOTEXT",
                        "category": "page_not_read",
                        "severity": "P1",
                        "reason": sheet_note,
                        "text": None,
                        "bbox": None,
                    }
                )

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
                    "note": sheet_note,
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
                    "note": f"This sheet could not be processed: {e}",
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
    progress.set_stage(progress_token, "Matching the doors and windows to their schedules", 87)
    try:
        opening_reconciliation = reconcile_openings_with_schedules(plan_reading_pages)
    except Exception as e:
        logger.exception(f"run={run_id} opening reconciliation failed: {e}")
        opening_reconciliation = {}

    # Which wall each mark labels, and where along it. This has to follow the
    # reconciliation: the schedule gives the mark its width, and that width is
    # the strongest evidence for which break in which wall it is labelling.
    try:
        opening_reconciliation.update(settle_opening_placement(plan_reading_pages, config))
        opening_reconciliation.update(score_openings(plan_reading_pages))
    except Exception as e:
        logger.exception(f"run={run_id} opening placement failed: {e}")

    progress.set_stage(progress_token, "Checking each sheet against the drawing index", 90)
    try:
        cross_check = cross_check_pages(plan_reading_pages, sheet_index, config)
    except Exception as e:
        logger.exception(f"run={run_id} cross-check failed: {e}")
        cross_check = {}

    # --- Source overlays --------------------------------------------------
    # **Drawn when a reader opens the sheet, not during the upload.** Marking
    # up all of them was the single most expensive thing an upload did - more
    # than a third of the whole run - and on a plan set of twenty sheets a
    # reader opens two or three. The address is fixed by the page number, so
    # it is given out now and the image is drawn the first time it is asked
    # for, which takes about a second.
    progress.set_stage(progress_token, "Working out what was found", 92)
    (run_dir / "overlays").mkdir(parents=True, exist_ok=True)
    for reading in plan_reading_pages:
        if reading.get("error"):
            continue
        overlay_filename = f"overlay_{reading['page_number']:03d}.png"
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
    progress.set_stage(progress_token, "Writing the tables and the issues log", 96)
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

    # Clear out runs nobody can still be reading. A new upload used to delete
    # every other run on the server, which broke the marked-up sheets of
    # anyone who had the site open in a second tab: those are drawn on demand
    # from the saved source PDF, and the folder holding it had gone.
    try:
        settings = load_config().get("runs", {}) or {}
        removed = prune_runs(
            keep_run_id=run_id,
            keep_recent=int(settings.get("keep_recent", 5)),
            max_age_hours=float(settings.get("max_age_hours", 6)),
        )
        if removed:
            logger.info(f"run={run_id} cleared {removed} run folder(s) nobody was reading")
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
PLAN_READING_FORMAT = 8


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


def _page_number_in(filename: str) -> int | None:
    digits = re.findall(r"(\d{3})", filename)
    return int(digits[0]) if digits else None


def _open_source_page(run_id: str, page_number: int):
    """The saved source PDF and one page of it, or (None, None).

    The upload keeps the approved PDF exactly as it arrived, so any picture of
    a sheet can be made again later from the drawing itself rather than from
    something derived from it.
    """
    source = run_plan_dir(run_id) / "source.pdf"
    if not source.is_file() or page_number is None or page_number < 1:
        return None, None
    try:
        doc = fitz.open(source)
        if page_number > doc.page_count:
            doc.close()
            return None, None
        return doc, doc.load_page(page_number - 1)
    except Exception as e:
        logger.exception(f"run={run_id} could not open the saved source PDF: {e}")
        return None, None


def preview_dpi() -> int:
    """The resolution a sheet is shown at when a reader looks at the page.

    Lower than the resolution character recognition needs, because a person
    looking at a sheet and a program reading letters off it want different
    things: at 150 DPI an A3 sheet is a 600 KB image that takes a quarter of a
    second to write, and at 72 DPI it is 200 KB and looks the same on screen.
    """
    try:
        return int(load_config().get("rendering", {}).get("preview_dpi", 72))
    except Exception:
        return 72


def resolve_page_image_path(run_id: str, filename: str) -> Path | None:
    """The picture of one sheet, made the first time it is asked for.

    Only ever returns a path inside this run's own pages/ folder, and only for
    filenames matching the exact pattern this pipeline generates — blocks path
    traversal (`../../secrets`) regardless of what the caller sends.
    """
    if not _PAGE_FILENAME_RE.match(filename):
        return None
    path = run_plan_dir(run_id) / "pages" / filename
    if path.is_file():
        return path

    doc, page = _open_source_page(run_id, _page_number_in(filename))
    if page is None:
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        render_thumbnail(page, path, dpi=preview_dpi())
        return path if path.is_file() else None
    except Exception as e:
        logger.exception(f"run={run_id} could not make the picture of {filename}: {e}")
        return None
    finally:
        doc.close()


def resolve_sheet_register_csv_path(run_id: str) -> Path | None:
    path = run_plan_dir(run_id) / "sheet_register.csv"
    if not path.is_file():
        return None
    return path


_OVERLAY_FILENAME_RE = re.compile(r"^overlay_\d{3}\.png$")


def resolve_overlay_image_path(run_id: str, filename: str) -> Path | None:
    """The marked-up sheet for one page, drawn the first time it is asked for.

    Same containment rule as the page images: only a filename this pipeline
    generates, only inside this run's own folder. Everything it draws comes
    from the reading already saved for that page, so the marked-up sheet
    always shows exactly what was reported for it.
    """
    if not _OVERLAY_FILENAME_RE.match(filename):
        return None
    path = run_plan_dir(run_id) / "overlays" / filename
    if path.is_file():
        return path

    page_number = _page_number_in(filename)
    reading = load_plan_reading(run_id)
    if reading is None or page_number is None:
        return None
    page_reading = next(
        (p for p in reading.get("pages", []) if p.get("page_number") == page_number), None
    )
    if page_reading is None:
        return None

    doc, page = _open_source_page(run_id, page_number)
    if page is None:
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if render_overlay(page, page_reading, path, load_config()):
            return path if path.is_file() else None
        return None
    except Exception as e:
        logger.exception(f"run={run_id} could not mark up sheet {page_number}: {e}")
        return None
    finally:
        doc.close()


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

    # Marked-up sheets are drawn when a sheet is opened, so a reader who
    # downloads the set without opening every sheet would otherwise get a
    # part of it. Any that are missing are drawn here first.
    reading = load_plan_reading(run_id)
    for page_reading in (reading or {}).get("pages", []):
        if page_reading.get("error"):
            continue
        resolve_overlay_image_path(
            run_id, f"overlay_{page_reading['page_number']:03d}.png"
        )

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
