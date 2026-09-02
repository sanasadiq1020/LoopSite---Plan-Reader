"""Day 4 — straight lines recovered from a sheet drawn as an image.

Not every plan set is vector. One of the two supplied sets places its drawing
on the page as **embedded images covering a third of the sheet**, with only
text and a few frame lines as real vector geometry: 400 drawing items against
16,117 on the vector set. Reading its wall lines from `page.get_drawings()`
therefore returns almost nothing, and no amount of tuning the pairing changes
that — the lines are pixels.

So when a sheet's own geometry yields no walls at all, its lines are recovered
from the rendered page instead and handed to exactly the same face-merging and
pairing code. The sheet's own line work is always tried first — it is exact,
and nothing measured off pixels can beat it — so this only ever runs where the
lines are genuinely not in the PDF as lines. The rest of the pipeline neither knows nor
cares which source a segment came from, except that every wall records it.

**Why morphology rather than a Hough transform.** A floor plan is dense with
short strokes — hatching, furniture, door swings, text. A Hough transform
answers "is there evidence of a line here", which on that input produces
thousands of fragments at every angle. Opening the image with a long
horizontal kernel instead answers "is there a continuous run of dark pixels
this long", which is precisely what a drawn wall face is and what a hatch
stroke is not. It is also orientation-exact: architectural plans are drawn on
the axes, so anything not horizontal or vertical is deliberately excluded
rather than approximated.
"""

import io

import fitz

from app.logging_setup import get_logger

logger = get_logger()

# Rendering finer than this costs time without finding more wall faces: at
# 1:100 a 90 mm wall is already about 5 pixels wide at 200 DPI.
_DEFAULT_DPI = 200


def _binary_image(page, dpi: int):
    """The page as a black-on-white mask, ready for morphology."""
    import cv2
    import numpy as np

    scale = dpi / 72.0
    pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csGRAY)
    image = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(pixmap.height, pixmap.width)
    # Adaptive rather than a fixed cut, so a greyed-out background layer or a
    # scan's uneven exposure does not erase the line work.
    mask = cv2.adaptiveThreshold(
        image, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 25, 15
    )
    return mask, scale


def _runs_along(mask, axis: str, minimum_pixels: int):
    """Continuous runs of dark pixels at least `minimum_pixels` long."""
    import cv2

    kernel_size = (minimum_pixels, 1) if axis == "h" else (1, minimum_pixels)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, kernel_size)
    isolated = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)
    contours, _ = cv2.findContours(isolated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    return [cv2.boundingRect(contour) for contour in contours]


def extract_rulings_from_image(page, config: dict, mm_per_point: float) -> dict:
    """Axis-aligned segments read from the rendered page, in PDF points.

    Returns the same shape as ``layout.extract_rulings`` so the two are
    interchangeable: {"h": [(y, x0, x1), ...], "v": [(x, y0, y1), ...]}, with
    ``h_widths``/``v_widths`` beside them. A picture states no stroke width, so
    the width here is the run's own measured thickness in points - which is the
    same thing the PDF would have been stating.
    """
    settings = config.get("raster_lines", {})
    dpi = int(settings.get("render_dpi", _DEFAULT_DPI))
    minimum_length_mm = float(settings.get("min_line_length_mm", 600))

    try:
        import cv2  # noqa: F401
    except ImportError:
        logger.warning("OpenCV is not installed, so image-drawn sheets cannot be measured")
        return {"h": [], "v": [], "h_widths": [], "v_widths": []}

    try:
        mask, scale = _binary_image(page, dpi)
    except Exception as e:
        logger.exception(f"could not render the page for line detection: {e}")
        return {"h": [], "v": [], "h_widths": [], "v_widths": []}

    # A wall face has to be at least this long to be worth looking at, in the
    # pixels of this render.
    minimum_pixels = max(int(minimum_length_mm / mm_per_point * scale), 8)

    horizontals = []
    horizontal_widths = []
    for x, y, width, height in _runs_along(mask, "h", minimum_pixels):
        if height > minimum_pixels:
            continue  # a block of fill, not a line
        centre_y = (y + height / 2.0) / scale
        horizontals.append((centre_y, x / scale, (x + width) / scale))
        horizontal_widths.append(height / scale)

    verticals = []
    vertical_widths = []
    for x, y, width, height in _runs_along(mask, "v", minimum_pixels):
        if width > minimum_pixels:
            continue
        centre_x = (x + width / 2.0) / scale
        verticals.append((centre_x, y / scale, (y + height) / scale))
        vertical_widths.append(width / scale)

    logger.info(
        f"line detection on the rendered page found {len(horizontals)} horizontal and "
        f"{len(verticals)} vertical runs at {dpi} DPI"
    )
    return {
        "h": horizontals,
        "v": verticals,
        "h_widths": horizontal_widths,
        "v_widths": vertical_widths,
    }
