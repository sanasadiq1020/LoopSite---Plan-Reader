"""Day 2 — OCR fallback for raster/mixed pages.

Environment note (documented decision, not a silent workaround): PaddlePaddle
3.x's default CPU acceleration (oneDNN) crashes on this machine with
`NotImplementedError: ConvertPirAttribute2RuntimeAttribute ...` on the
PP-OCRv5 mobile models. Disabling oneDNN avoids the crash, but OCR then runs
CPU-only.

**Measured limitation.** On this machine a dense A3 construction sheet
rendered at 150 DPI does not finish within the default 90-second budget: a
scanned copy of the supplied floor plan and door schedule both timed out with
nothing extracted. The pipeline handles that correctly and honestly — the page
is classified as raster, OCR is attempted, the timeout is caught, the sheet is
marked "partial" and the reason is reported — but the practical position is
that image-only sheets currently yield no text on this hardware inside a
web-request time budget. The budget and the render resolution are both
editable in `config/plan_reading.json` so the trade-off can be changed without
touching code; the underlying fix is a faster OCR path (GPU, or a lighter
detector), which is a separate decision.

Two behaviours keep this from harming a normal run:
1. OCR only runs on pages that actually need it (see should_run_ocr) — a page
   with a real native text layer is never OCR'd, so a vector plan set is
   unaffected.
2. Every OCR call is time-boxed. A page that exceeds the budget is marked
   "timeout", logged, and the run continues — it never silently hangs the
   whole upload.
"""

import gc
import importlib.util
import os
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path

os.environ.setdefault("PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT", "False")

from app.logging_setup import get_logger  # noqa: E402

logger = get_logger()

# Defaults, used only if config/plan_reading.json is missing or unreadable.
# The file on disk is the source of truth so the time budget can be raised
# without touching code (Critical Rule 1).
OCR_PAGE_TIMEOUT_SECONDS = 90
MIN_NATIVE_CHARS_TO_SKIP_OCR = 20  # mirrors intake.classify_page's vector threshold


def _settings() -> dict:
    from pipeline.plan.reading import load_config

    try:
        return load_config().get("ocr", {}) or {}
    except Exception:
        return {}


def page_timeout_seconds() -> int:
    try:
        return int(_settings().get("page_timeout_seconds", OCR_PAGE_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        return OCR_PAGE_TIMEOUT_SECONDS


def render_dpi(default: int = 150) -> int:
    try:
        return int(_settings().get("render_dpi", default))
    except (TypeError, ValueError):
        return default

_ocr_engine = None
_executor = ThreadPoolExecutor(max_workers=1)


def should_run_ocr(native_char_count: int, page_has_marks: bool = True) -> bool:
    """Whether a page needs reading by OCR.

    **The test is the text, and only the text.**

    Two earlier versions of this test were wrong in opposite directions, and
    both are worth recording because each produced a plausible-looking run.

    *   It first skipped OCR only on pages classified "vector", which meant a
        page with a full text layer *and* a background image was classified
        "mixed" and read by OCR anyway. One 17-sheet plan set draws every
        sheet over a full-page image and also carries its complete text layer,
        so all 17 pages went to OCR, each timed out, and the upload took 85
        minutes to return text it already had.

    *   Fixing that left the classification in the test, which is what broke
        an unseen single-sheet plan. A sheet whose text has been converted to
        outlines — ordinary in a CAD export, and the whole point of outlining
        is that the letters become line work — carries **no text at all** and
        yet classifies as "vector", because vector is what its drawn line work
        makes it. OCR was skipped, nothing was read, and every field on the
        sheet came back blank with nothing on screen to say why. The same trap
        catches a scan placed on a sheet that also carries a drawn border, and
        any drawing whose fonts have no usable character mapping.

    So the classification is not consulted. A page that carries a real text
    layer is read from that layer, whatever is drawn behind it; a page that
    does not is offered to OCR, whatever it is drawn with. The only page not
    worth the cost is one with nothing on it at all — no text, no line work
    and no image — which is what ``page_has_marks`` reports.
    """
    minimum = MIN_NATIVE_CHARS_TO_SKIP_OCR
    try:
        minimum = int(_settings().get("min_native_chars_to_skip", minimum))
    except (TypeError, ValueError):
        pass
    if native_char_count >= minimum:
        return False
    return bool(page_has_marks)


# A drawing's text is overwhelmingly letters and digits: measured across the
# plan sets in use, the lowest share on any sheet is 0.90, and none of them
# contains a single unreadable character. The defaults below sit far below
# that, so only a text layer that is genuinely broken can trip them.
MIN_READABLE_SHARE = 0.45
MAX_UNREADABLE_SHARE = 0.20

_UNREADABLE_CATEGORIES = {"Cc", "Cf", "Co", "Cn", "Cs"}


def text_layer_is_usable(text: str) -> tuple[bool, str]:
    """Whether a page's own text can actually be read, and why not.

    **A text layer can be present and still be worthless.** A PDF stores which
    glyph to draw, and a mapping back to what that glyph *means* is optional.
    Plotting software and older CAD exports routinely omit it, and the text
    then extracts as the glyph codes themselves — a page's worth of characters
    that are not the words printed on the drawing.

    Counting characters cannot tell the difference, so a sheet like that
    passed as "has its own text", was never offered to character recognition,
    and every value read off it was nonsense or absent, with nothing on screen
    to say so. Reading what the sheet actually shows is the only way through,
    and that means recognising it from the page image like any other sheet
    with no usable text.

    The test is deliberately blunt, because the two cases are not close: a
    construction drawing is nearly all letters and digits, and a broken text
    layer is nearly none.
    """
    import unicodedata

    settings = _settings()
    try:
        min_readable = float(settings.get("min_readable_share", MIN_READABLE_SHARE))
    except (TypeError, ValueError):
        min_readable = MIN_READABLE_SHARE
    try:
        max_unreadable = float(settings.get("max_unreadable_share", MAX_UNREADABLE_SHARE))
    except (TypeError, ValueError):
        max_unreadable = MAX_UNREADABLE_SHARE

    characters = [c for c in (text or "") if not c.isspace()]
    if not characters:
        return True, ""  # nothing to judge; the character count already covers this

    readable = 0
    unreadable = 0
    for c in characters:
        if c.isalnum() and ord(c) < 0x2500:
            readable += 1
        if c == "�" or unicodedata.category(c) in _UNREADABLE_CATEGORIES:
            unreadable += 1

    readable_share = readable / len(characters)
    unreadable_share = unreadable / len(characters)

    if unreadable_share > max_unreadable:
        return False, (
            f"{unreadable_share:.0%} of this sheet's stored text is characters that "
            "cannot be displayed"
        )
    if readable_share < min_readable:
        return False, (
            f"only {readable_share:.0%} of this sheet's stored text is letters and "
            "digits, so the text stored in the file is not the wording printed on "
            "the drawing"
        )
    return True, ""


def _turned_off() -> bool:
    """Whether this deployment has been told not to use character recognition.

    A **runtime** switch, not a build-time one. The recognition models are the
    largest thing this application ever holds — measured at 184 MB on top of a
    345 MB working set — and whether a machine has room for them is a fact
    about the machine, not about the code. Leaving it out of the image means
    rebuilding to change your mind; an environment variable means turning it
    off on a small host and on again on a larger one.

    Everything else is unaffected: a sheet that carries its own text is read
    from that text either way, and a sheet that does not says plainly that
    recognition is switched off here rather than coming back blank.
    """
    raw = os.environ.get("OCR_ENABLED")
    if raw is None:
        return False
    return raw.strip().lower() in ("0", "false", "no", "off")


def ocr_is_available() -> tuple[bool, str]:
    """Whether character recognition can run here at all, and why not.

    A deployment may be built without it deliberately (a smaller image), or
    the machine may not have the memory to load the models. Either way the
    reader is told plainly rather than being shown an empty sheet.

    **Asking must not cost what answering costs.** This used to import the
    recognition library to find out, and that import alone takes **184 MB and
    24 seconds** — paid by every upload that so much as asked the question,
    including one whose every sheet carries its own text and never needed
    recognition at all. On a small server two of those at once is the whole
    machine. Whether the library is installed can be answered from the import
    system without loading anything, so that is what is asked here; the
    library itself is loaded only when a sheet genuinely has to be read.
    """
    if _turned_off():
        return False, "character recognition is switched off on this server"
    if importlib.util.find_spec("paddleocr") is None:
        return False, "the character recognition package is not installed here"
    return True, ""


def release_engine() -> bool:
    """Lets go of the loaded recognition models and gives the memory back.

    The models are ~184 MB and are wanted only while a scanned sheet is being
    read. Holding them for the rest of the container's life is what leaves no
    room for the next reader's plan, so they are released once an upload is
    done and loaded again if another upload needs them.
    """
    global _ocr_engine
    if _ocr_engine is None:
        return False
    _ocr_engine = None
    gc.collect()
    logger.info("character recognition models released")
    return True


def _get_engine():
    global _ocr_engine
    if _ocr_engine is None:
        from paddleocr import PaddleOCR

        t0 = time.time()
        _ocr_engine = PaddleOCR(
            lang="en",
            text_detection_model_name="PP-OCRv5_mobile_det",
            text_recognition_model_name="PP-OCRv5_mobile_rec",
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
        )
        logger.info(f"OCR engine loaded in {time.time() - t0:.2f}s")
    return _ocr_engine


def _run_ocr_sync(image_path: str) -> list[dict]:
    engine = _get_engine()
    results = list(engine.predict(image_path))
    blocks: list[dict] = []
    for r in results:
        texts = r.get("rec_texts", []) or []
        scores = r.get("rec_scores", []) or []
        boxes = r.get("rec_boxes", None)
        for i, text in enumerate(texts):
            text = text.strip()
            if not text:
                continue
            score = float(scores[i]) if i < len(scores) else None
            bbox = None
            if boxes is not None and i < len(boxes):
                b = list(boxes[i])
                bbox = [float(b[0]), float(b[1]), float(b[2]), float(b[3])]
            blocks.append({"text": text, "confidence": score, "bbox": bbox})
    return blocks


def run_ocr_on_page(image_path: Path) -> dict:
    """Returns {blocks, status: ok|timeout|failed|unavailable, error, duration_s}."""
    t0 = time.time()
    budget = page_timeout_seconds()

    # Asked before the work starts, so a deployment that cannot run character
    # recognition says so on the sheet instead of returning an empty page that
    # looks like a plan with nothing on it.
    available, why = ocr_is_available()
    if not available:
        logger.warning(f"character recognition is not available here: {why}")
        return {
            "blocks": [],
            "status": "unavailable",
            "error": (
                "This sheet carries no text of its own, and character recognition "
                "is not available on this server, so no text could be read from it. "
                "The sheet image itself is still shown."
            ),
            "duration_s": 0.0,
        }

    try:
        future = _executor.submit(_run_ocr_sync, str(image_path))
        blocks = future.result(timeout=budget)
        return {
            "blocks": blocks,
            "status": "ok",
            "error": None,
            "duration_s": round(time.time() - t0, 2),
        }
    except FutureTimeoutError:
        logger.warning(f"OCR timed out after {budget}s for {image_path}")
        return {
            "blocks": [],
            "status": "timeout",
            "error": (
                f"Reading this scanned page took longer than the {budget}-second budget, "
                "so no text was taken from it. Raise ocr.page_timeout_seconds in "
                "config/plan_reading.json to allow more time."
            ),
            "duration_s": round(time.time() - t0, 2),
        }
    except MemoryError as e:
        # The recognition models need more memory than this machine has. That
        # is a fact about the server, not about the drawing, and saying so is
        # the difference between a sheet a reader can act on and a blank one.
        logger.exception(f"OCR ran out of memory for {image_path}: {e}")
        return {
            "blocks": [],
            "status": "unavailable",
            "error": (
                "This sheet carries no text of its own, and there was not enough "
                "memory on this server to read it by character recognition. The "
                "sheet image itself is still shown."
            ),
            "duration_s": round(time.time() - t0, 2),
        }
    except Exception as e:
        logger.exception(f"OCR failed for {image_path}: {e}")
        return {
            "blocks": [],
            "status": "failed",
            "error": (
                "This sheet carries no text of its own and could not be read by "
                f"character recognition: {e}"
            ),
            "duration_s": round(time.time() - t0, 2),
        }
