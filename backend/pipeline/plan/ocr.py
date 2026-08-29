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


def should_run_ocr(classification: str, native_char_count: int) -> bool:
    """Whether a page needs reading by OCR.

    **The test is the text, not the picture behind it.** This used to skip OCR
    only on pages classified "vector", which meant a page with a full text
    layer *and* a background image was classified "mixed" and read by OCR
    anyway. One 17-sheet plan set draws every sheet over a full-page image and
    also carries its complete text layer, so all 17 pages went to OCR, each
    timed out after 300 seconds, and the upload took **85 minutes** to return
    text it already had.

    A page that already carries a real text layer is read from that layer,
    whatever is drawn behind it. OCR is for pages that have no text of their
    own — that is what it is for, and it is the only case where its cost is
    worth paying.
    """
    minimum = MIN_NATIVE_CHARS_TO_SKIP_OCR
    try:
        minimum = int(_settings().get("min_native_chars_to_skip", minimum))
    except (TypeError, ValueError):
        pass
    if native_char_count >= minimum:
        return False
    return classification != "vector"


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
    """Returns {blocks, status: ok|timeout|failed, error, duration_s}."""
    t0 = time.time()
    budget = page_timeout_seconds()
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
    except Exception as e:
        logger.exception(f"OCR failed for {image_path}: {e}")
        return {
            "blocks": [],
            "status": "failed",
            "error": str(e),
            "duration_s": round(time.time() - t0, 2),
        }
