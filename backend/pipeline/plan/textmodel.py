"""Day 3 — one uniform, de-duplicated text model for a page.

Everything downstream (title block, rooms, dimensions, schedules, legends,
overlay) reads TextLine records produced here, so there is exactly one place
that decides what "a line of text on this page" means.

Three things this layer fixes that the earlier word-based extraction could
not:

1.  **Writing direction is preserved.**  PyMuPDF's ``get_text("dict")`` gives
    every line a ``dir`` unit vector: ``(1, 0)`` for normal horizontal text
    and ``(0, -1)`` for text rotated 90 degrees.  On a construction drawing
    that is not cosmetic — a rotated dimension figure measures the *vertical*
    axis of the building and a horizontal one measures the *horizontal* axis.
    Deriving it from the bounding-box aspect ratio is a guess; ``dir`` is what
    the PDF itself records, so it is used directly and only falls back to the
    aspect ratio for OCR text (which has no direction information at all).

2.  **One coordinate system.**  Native text is in PDF points; OCR boxes come
    back in pixels of the rendered thumbnail.  Comparing them is meaningless,
    so OCR boxes are converted to points here using the exact DPI they were
    rendered at.  After this module every bbox on a page is in PDF points and
    can be compared freely.

3.  **Template-layer overprints are resolved.**  These plan sets print a
    blank template ("-" placeholders) and then overprint the real value at
    the same coordinates, so the same spot legitimately carries two different
    strings.  Measured on the supplied sets: 43 exact duplicates and 65
    same-origin/different-text pairs across 23 pages.  Left alone, which one
    "wins" depends on iteration order, which makes the whole run
    irreproducible.  The rule applied here is deliberate and narrow: at one
    origin, a placeholder ("-", ".", "N/A", empty) always loses to real
    content; two *different* real values are both kept and marked as a
    conflict so the field that reads them can flag it instead of guessing
    (Critical Rule 5).
"""

import re

import fitz
from functools import lru_cache

from app.logging_setup import get_logger

logger = get_logger()

# Two boxes whose corners agree to within this many points are the same spot
# on the page. Chosen from the measured overprint pairs on the supplied plans,
# where the template dash and the real value share an origin to well under 1pt.
_SAME_ORIGIN_TOLERANCE_PT = 1.5

# Text that carries no information and only exists to fill an empty template
# cell. Anything matching this loses to real content at the same origin.
_PLACEHOLDER_RE = re.compile(
    r"^[\s\-‐-―._·:/]*$|^(N/?A|TBC|TBA|NIL)$", re.IGNORECASE
)

# A line whose bbox is this many times taller than it is wide is rotated, used
# only for OCR text where no direction vector exists.
_OCR_VERTICAL_ASPECT = 1.6


def is_placeholder(text: str) -> bool:
    """True for template filler ("-", "...", "N/A") — never a real value."""
    return bool(_PLACEHOLDER_RE.match(text.strip()))


@lru_cache(maxsize=65536)
def normalize_label(text: str) -> str:
    """Label form used for every label lookup: upper-case, no trailing
    punctuation, single-spaced. 'Drawing No:' and 'DRAWING  NO.' both become
    'DRAWING NO' so config lists stay readable and case-insensitive.

    Cached because the same few hundred strings are normalised over and over:
    a 23-sheet upload called this 1.7 million times, which was a sixth of the
    whole run. The function is pure, so caching cannot change an answer.
    """
    cleaned = text.strip().rstrip(":.").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.upper()


def _axis_from_dir(direction) -> str:
    dx, dy = float(direction[0]), float(direction[1])
    return "horizontal" if abs(dx) >= abs(dy) else "vertical"


def make_line(
    text: str,
    bbox: list,
    extraction_method: str,
    confidence: float,
    axis: str,
    size: float = 0.0,
    bold: bool = False,
    direction=None,
) -> dict:
    """One line of text on the page.

    ``direction`` is the unit vector the text is written along, as the PDF
    itself records it. It is kept rather than reduced to horizontal/vertical,
    because sideways text can run two ways and the two are not the same: a
    title strip up the right edge of a sheet reads bottom to top, and one up
    the left edge reads top to bottom. Knowing which decides which way the
    sheet has to be turned to read it. Text recovered from a page image has no
    direction, and is None here.
    """
    x0, y0, x1, y1 = bbox
    return {
        "text": text,
        "bbox": [
            round(float(x0), 2),
            round(float(y0), 2),
            round(float(x1), 2),
            round(float(y1), 2),
        ],
        "axis": axis,
        "dir": (
            (round(float(direction[0]), 4), round(float(direction[1]), 4))
            if direction is not None
            else None
        ),
        "size": round(float(size), 2),
        "bold": bool(bold),
        "extraction_method": extraction_method,
        "confidence": float(confidence),
        # Set by resolve_overprints() when another line shares this origin.
        "conflicts_with": [],
    }


def _page_dict(page):
    """The page's native text, extracted once.

    PyMuPDF builds a fresh text page for every ``get_text`` call, and the same
    page was being read four times per sheet — page classification, the raw
    text record, and the text model itself. On a 23-sheet upload that was
    twelve seconds of repeated work. The result is cached on the page object,
    so it lives exactly as long as the open document does.
    """
    cached = getattr(page, "_loopsite_text_dict", None)
    if cached is None:
        cached = _in_page_space(page, page.get_text("dict"))
        try:
            page._loopsite_text_dict = cached
        except AttributeError:
            pass  # a page object that refuses attributes still works, just slower
    return cached


def _in_page_space(page, page_dict: dict) -> dict:
    """Text coordinates in the space the page actually displays in.

    **A PDF page can carry its own rotation**, and CAD exports very often do:
    a sheet drafted in portrait and printed in landscape is stored upright with
    a 90-degree page rotation. PyMuPDF then returns text coordinates in the
    *unrotated* space while ``get_pixmap`` renders the *rotated* page — so
    every bounding box lands somewhere else on the image, and every horizontal
    line of text is measured as vertical.

    On a 17-sheet plan set where every page is rotated 90 degrees, that put
    every room and dimension box in the wrong place on the marked-up sheet,
    read a floor plan's room labels as sideways text, and sent the dimension
    figures to the wrong axis. The first two supplied plan sets are both
    unrotated, which is why nothing showed it.

    Turning the coordinates here fixes it once, for everything downstream:
    nothing else in the pipeline needs to know a page was rotated.
    """
    if not page.rotation:
        return page_dict

    matrix = page.rotation_matrix
    a, b, c, d = matrix.a, matrix.b, matrix.c, matrix.d

    def turn_box(bbox):
        turned = fitz.Rect(bbox) * matrix
        turned.normalize()
        return (turned.x0, turned.y0, turned.x1, turned.y1)

    def turn_direction(direction):
        dx, dy = float(direction[0]), float(direction[1])
        return (dx * a + dy * c, dx * b + dy * d)

    for block in page_dict.get("blocks", []):
        if "bbox" in block:
            block["bbox"] = turn_box(block["bbox"])
        for line in block.get("lines", []):
            line["bbox"] = turn_box(line["bbox"])
            if "dir" in line:
                line["dir"] = turn_direction(line["dir"])
            for span in line.get("spans", []):
                span["bbox"] = turn_box(span["bbox"])
    return page_dict


def release_page_cache(page) -> None:
    """Lets go of everything cached on a page once it has been read.

    The text model and the drawn paths are cached on the page object so each is
    produced once instead of four times. That is right while a page is being
    read and wrong afterwards: the pages are kept until the marked-up sheets
    are drawn, and on a plan set of any size the caches accumulate into
    hundreds of megabytes that nothing will use again. Drawing an overlay needs
    the page's pixels, not its text or its line work.
    """
    for attribute in ("_loopsite_text_dict", "_loopsite_drawings"):
        try:
            if hasattr(page, attribute):
                delattr(page, attribute)
        except Exception:
            pass  # a page that will not let go of it is not worth failing over


def native_text_of(page) -> str:
    """The page's plain text, from the one extraction shared by everything."""
    parts = []
    for block in _page_dict(page).get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                parts.append(span.get("text", ""))
    return "".join(parts)


def extract_native_lines(page) -> list:
    """Line-level native text with direction, font size and weight.

    ``get_text("dict")`` already separates a title block's neighbouring cells
    into their own lines: a label and its value arrive as two lines, while a
    title printed with wide gaps around a divider — 'NORTH WING  |  FLOOR
    PLAN' — correctly stays one.  That is why it replaces the previous
    word-gap splitting, which had to guess a column-gutter width and broke a
    real title in half.
    """
    lines: list = []
    try:
        page_dict = _page_dict(page)
    except Exception as e:
        logger.exception(f"extract_native_lines: get_text('dict') failed: {e}")
        return lines

    for block in page_dict.get("blocks", []):
        if block.get("type") != 0:  # 0 = text, 1 = image
            continue
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            text = "".join(s.get("text", "") for s in spans).strip()
            if not text:
                continue
            direction = line.get("dir", (1.0, 0.0))
            size = max((s.get("size", 0.0) for s in spans), default=0.0)
            # PyMuPDF span flag bit 4 (value 16) marks a bold face.
            bold = any(int(s.get("flags", 0)) & 16 for s in spans)
            lines.append(
                make_line(
                    text=re.sub(r"\s+", " ", text),
                    bbox=list(line["bbox"]),
                    extraction_method="native",
                    confidence=1.0,  # PyMuPDF does not score native text
                    axis=_axis_from_dir(direction),
                    size=size,
                    bold=bold,
                    direction=direction,
                )
            )
    return lines


def convert_ocr_blocks(ocr_blocks: list, dpi: int) -> list:
    """OCR boxes are pixels of the DPI-rendered page image; the rest of the
    pipeline works in PDF points. Converting here (rather than keeping two
    incompatible coordinate spaces and refusing to compare them) is what lets
    an OCR'd dimension be associated with an OCR'd room label, and lets the
    overlay draw native and OCR results on the same image."""
    scale = 72.0 / float(dpi) if dpi else 1.0
    lines: list = []
    for b in ocr_blocks:
        bbox = b.get("bbox")
        text = (b.get("text") or "").strip()
        if not bbox or not text:
            continue  # no location means no traceability (Critical Rule 4)
        try:
            x0, y0, x1, y1 = (float(v) * scale for v in bbox)
        except Exception:
            continue
        width, height = abs(x1 - x0), abs(y1 - y0)
        axis = "vertical" if height > width * _OCR_VERTICAL_ASPECT else "horizontal"
        score = b.get("confidence")
        lines.append(
            make_line(
                text=re.sub(r"\s+", " ", text),
                bbox=[x0, y0, x1, y1],
                extraction_method="ocr",
                confidence=float(score) if score is not None else 0.5,
                axis=axis,
                size=round(height, 2),
            )
        )
    return lines


def _origin_key(line: dict):
    x0, y0 = line["bbox"][0], line["bbox"][1]
    step = _SAME_ORIGIN_TOLERANCE_PT
    return (int(round(x0 / step)), int(round(y0 / step)))


def deduplicate(lines: list):
    """Drops lines that repeat the same text at the same place. Returns the
    kept lines and how many were removed (reported as run evidence, never
    hidden)."""
    seen = set()
    kept: list = []
    for line in lines:
        key = (line["text"], *_origin_key(line), round(line["bbox"][2], 1))
        if key in seen:
            continue
        seen.add(key)
        kept.append(line)
    return kept, len(lines) - len(kept)


# How much of a recognised line has to sit on top of text the sheet already
# prints before it is treated as a second reading of the same thing.
_ALREADY_PRINTED_OVERLAP = 0.5
# Grid used to compare only nearby lines rather than every pair.
_OVERLAP_BUCKET_PT = 60.0


def _buckets(bbox, step=_OVERLAP_BUCKET_PT):
    x0, y0, x1, y1 = bbox
    for gx in range(int(x0 // step), int(x1 // step) + 1):
        for gy in range(int(y0 // step), int(y1 // step) + 1):
            yield (gx, gy)


def drop_recognised_text_already_printed(lines: list):
    """Removes recognised text from places the sheet already prints its own.

    A sheet can carry a real title block as text with its drawing placed as a
    picture. Both are read - the title block from the file, the picture by
    character recognition - and recognition also reads the title block again,
    because it reads the whole page image. The same words then appear twice a
    fraction of a point apart, and a title came out as "GROUND FLOOR PLAN
    GROUND FLOOR PLAN".

    Exact de-duplication cannot catch this: the two readings differ in
    position, and often by a character. What settles it is position alone.
    Where the sheet already prints text, that text is what the sheet says, and
    a recognition of the same spot adds nothing. Only recognised text over
    parts of the sheet that print nothing - the picture - is kept.
    """
    native = [ln for ln in lines if ln["extraction_method"] != "ocr"]
    recognised = [ln for ln in lines if ln["extraction_method"] == "ocr"]
    if not native or not recognised:
        return lines, 0

    grid: dict = {}
    for line in native:
        for key in _buckets(line["bbox"]):
            grid.setdefault(key, []).append(line)

    kept = list(native)
    dropped = 0
    for line in recognised:
        x0, y0, x1, y1 = line["bbox"]
        area = max((x1 - x0) * (y1 - y0), 0.01)
        covered = False
        checked = set()
        for key in _buckets(line["bbox"]):
            for other in grid.get(key, ()):
                if id(other) in checked:
                    continue
                checked.add(id(other))
                ox0, oy0, ox1, oy1 = other["bbox"]
                overlap = max(0.0, min(x1, ox1) - max(x0, ox0)) * max(
                    0.0, min(y1, oy1) - max(y0, oy0)
                )
                other_area = max((ox1 - ox0) * (oy1 - oy0), 0.01)
                if overlap / min(area, other_area) >= _ALREADY_PRINTED_OVERLAP:
                    covered = True
                    break
            if covered:
                break
        if covered:
            dropped += 1
        else:
            kept.append(line)
    return kept, dropped


def resolve_overprints(lines: list):
    """Resolves template placeholders overprinted by real values.

    Returns (lines, conflicts). A conflict is two *different* non-placeholder
    strings at the same origin — both lines are kept and cross-referenced via
    ``conflicts_with`` so whichever field reads them reports the ambiguity
    rather than silently taking the first one.
    """
    groups: dict = {}
    for line in lines:
        groups.setdefault(_origin_key(line), []).append(line)

    kept: list = []
    conflicts: list = []
    for group in groups.values():
        if len(group) == 1:
            kept.extend(group)
            continue

        real = [ln for ln in group if not is_placeholder(ln["text"])]
        if not real:
            # Every candidate is filler: keep one so the empty cell is still
            # visible on the overlay, but it will never be accepted as a value.
            kept.append(group[0])
            continue

        distinct = {ln["text"] for ln in real}
        if len(distinct) > 1:
            for ln in real:
                ln["conflicts_with"] = sorted(distinct - {ln["text"]})
            conflicts.append({"bbox": real[0]["bbox"], "values": sorted(distinct)})
        kept.extend(real)
    return kept, conflicts


def build_page_lines(page, ocr_blocks: list, dpi: int, use_native_text: bool = True):
    """The single entry point: native + OCR text for one page, in points,
    de-duplicated, with overprints resolved.

    ``use_native_text`` is False for a sheet whose stored text is not the
    wording printed on it — a font with no mapping back to what its glyphs
    mean. Keeping that text would put nonsense into every table beside what
    character recognition read from the page image, so it is left out and the
    run records that it was (see ocr.text_layer_is_usable).

    The second return value is evidence about what this layer did, so the run
    can report it instead of silently discarding text.
    """
    native = extract_native_lines(page) if use_native_text else []
    ocr = convert_ocr_blocks(ocr_blocks, dpi)
    combined = native + ocr

    # Where the sheet prints its own text, that is what the sheet says; a
    # recognition of the same spot is a second reading of it and is dropped.
    combined, already_printed = drop_recognised_text_already_printed(combined)

    combined, duplicates_removed = deduplicate(combined)
    combined, conflicts = resolve_overprints(combined)

    # Reading order: top-to-bottom, then left-to-right. Deterministic order is
    # what makes two runs of the same PDF produce byte-identical output.
    combined.sort(key=lambda ln: (round(ln["bbox"][1], 1), round(ln["bbox"][0], 1)))

    evidence = {
        "native_text_used": bool(use_native_text),
        "native_line_count": len(native),
        "ocr_line_count": len(ocr),
        "duplicates_removed": duplicates_removed,
        "recognised_text_already_printed": already_printed,
        "overprint_conflicts": conflicts,
        "vertical_line_count": sum(1 for ln in combined if ln["axis"] == "vertical"),
    }
    return combined, evidence


# --- Shared geometry helpers used by every detector -----------------------


def bbox_center(bbox: list):
    x0, y0, x1, y1 = bbox
    return (x0 + x1) / 2.0, (y0 + y1) / 2.0


def bbox_height(bbox: list) -> float:
    return abs(bbox[3] - bbox[1])


def bbox_width(bbox: list) -> float:
    return abs(bbox[2] - bbox[0])


def vertical_overlap(a: list, b: list) -> float:
    """Height of the shared y-range of two boxes, in points. Two lines printed
    on the same table row overlap substantially; two lines on different rows do
    not — this is what groups a row without needing a tolerance tuned per
    drawing."""
    return max(0.0, min(a[3], b[3]) - max(a[1], b[1]))


def horizontal_overlap(a: list, b: list) -> float:
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0]))


def group_into_rows(lines: list, min_overlap_ratio: float = 0.5) -> list:
    """Groups lines into visual rows by real y-overlap rather than by a fixed
    point tolerance, so it behaves the same on a 4pt note and a 14pt title.

    **Only text printed in the same direction shares a row.** A figure printed
    rotated 90 degrees is as tall on the page as it is long, so it overlaps
    every horizontal line beside it; on a floor plan that printed its vertical
    dimensions next to an abbreviations list, one rotated figure chained seven
    separate legend rows into a single row, and the legend then claimed a strip
    of the drawing wide enough to swallow six real dimensions. Two lines at
    right angles to each other are not on the same row of the same table.
    """
    rows: list = []
    for line in sorted(lines, key=lambda ln: (ln["bbox"][1], ln["bbox"][0])):
        placed = False
        for row in rows:
            ref = row[0]
            if ref.get("axis") != line.get("axis"):
                continue
            overlap = vertical_overlap(ref["bbox"], line["bbox"])
            shortest = min(bbox_height(ref["bbox"]), bbox_height(line["bbox"])) or 1.0
            if overlap / shortest >= min_overlap_ratio:
                row.append(line)
                placed = True
                break
        if not placed:
            rows.append([line])
    for row in rows:
        row.sort(key=lambda ln: ln["bbox"][0])
    rows.sort(key=lambda r: r[0]["bbox"][1])
    return rows


