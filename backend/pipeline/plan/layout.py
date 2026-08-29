"""Day 3 — page layout geometry: ruling lines, table cells, label/value pairing.

This is the module that replaces distance-guessing with real geometry.

The previous approach paired a title-block label with its value by taking
"whichever text block is nearest, within 8% of the page diagonal, roughly
below or to the right".  That fails in a specific and dangerous way: when a
label's own cell is *empty* (a cover sheet that leaves "Scale" blank), the
nearest text is the neighbouring column's value, so the field silently
reports the wrong thing — measured on the supplied plans as scale being
reported as the drawing date.

Two independent geometric sources are used here instead:

*   **Ruling lines** (``page.get_drawings()``).  Both supplied plan sets draw
    the title block as real vector rectangles, so the *outer* cell that
    encloses a label can be recovered exactly and used as a hard boundary.
    This is only ever used as a boundary, never as the sole answer, because
    (measured) these title blocks do **not** draw every internal column
    separator — the outer frame is reliable, the inner grid is not.

*   **Label-derived column bands.**  A title block is a grid of label/value
    pairs where the label sits at the top-left of its cell and the value sits
    directly beneath it, left-aligned to the same x.  So the labels printed on
    one row *are themselves* the column boundaries: 'Project No:' at x=926,
    'Date:' at x=995 and 'Scale:' at x=1050 mean the Scale column runs from
    1050 to the enclosing frame edge, and nothing outside that band can ever
    be read as the scale.  This is derived per page from the labels actually
    found, so it carries no hardcoded coordinate and works on a title block of
    any shape.

The second supplied plan writes its title block the other way round — label
and value side by side on one row ('PROJECT NO' then 'D-00-03', 'REV NO' then
'1').  Both arrangements are handled by the same pair of probes below, and
which one produced a value is recorded on the field as ``technique`` so the
UI can show *how* every value was found.
"""

import fitz

from app.logging_setup import get_logger
from pipeline.plan.textmodel import (
    bbox_height,
    normalize_label,
    vertical_overlap,
)

logger = get_logger()

# A drawn segment counts as a ruling only if it is straight to within this
# many points and at least this long — filters out hatching, arrowheads and
# the thousands of tiny strokes that make up the drawing itself.
_STRAIGHTNESS_TOLERANCE_PT = 0.8
_MIN_RULING_LENGTH_PT = 6.0

# When no enclosing rectangle can be found, a value may still be paired with
# its label, but only within this multiple of the label's own height. Ties the
# search radius to the printed text size rather than to the page size, so a
# title block set in 4pt type does not get a 60pt search window.
_MAX_ROW_GAP_IN_LABEL_HEIGHTS = 6.0
_MAX_COLUMN_GAP_IN_LABEL_HEIGHTS = 6.0

# Column band tolerance: a value left-aligned under its label can start a
# fraction of a character before the label's own left edge.
_COLUMN_LEFT_SLACK_PT = 3.0


def page_drawings(page) -> list:
    """The page's drawn paths, read once.

    Reading them is the single most expensive thing done to a vector sheet —
    one floor plan here holds 16,117 items — and the page was being asked for
    them twice, once to classify the page and once to find its ruling lines.
    The result is cached on the page object, so it lives as long as the open
    document.
    """
    cached = getattr(page, "_loopsite_drawings", None)
    if cached is None:
        cached = _drawings_in_page_space(page, page.get_drawings())
        try:
            page._loopsite_drawings = cached
        except AttributeError:
            pass
    return cached


def _drawings_in_page_space(page, drawings: list) -> list:
    """Drawn paths in the space the page displays in.

    The same rotation that moves text off the marked-up sheet moves the line
    work with it — and walls are found from that line work, so on a rotated
    sheet the horizontal and vertical faces are swapped and nothing pairs up.
    See ``textmodel._in_page_space`` for what a page rotation is.
    """
    if not page.rotation:
        return drawings

    matrix = page.rotation_matrix
    for path in drawings:
        turned_items = []
        for item in path.get("items", []):
            kind = item[0]
            if kind == "l":
                turned_items.append((kind, item[1] * matrix, item[2] * matrix))
            elif kind == "re":
                rect = fitz.Rect(item[1]) * matrix
                rect.normalize()
                turned_items.append((kind,) + (rect,) + tuple(item[2:]))
            elif kind == "qu":
                turned_items.append((kind, item[1] * matrix))
            elif kind == "c":
                turned_items.append((kind,) + tuple(point * matrix for point in item[1:]))
            else:
                turned_items.append(item)
        path["items"] = turned_items
        if path.get("rect") is not None:
            rect = fitz.Rect(path["rect"]) * matrix
            rect.normalize()
            path["rect"] = rect
    return drawings


def extract_rulings(page) -> dict:
    """Axis-aligned drawn lines, split into horizontals and verticals.

    Each entry is (position, span_start, span_end): a horizontal is
    (y, x_from, x_to) and a vertical is (x, y_from, y_to).
    """
    horizontals: list = []
    verticals: list = []
    try:
        drawings = page_drawings(page)
    except Exception as e:
        logger.exception(f"extract_rulings: get_drawings() failed: {e}")
        return {"h": [], "v": []}

    def add_segment(x0, y0, x1, y1):
        if abs(y0 - y1) <= _STRAIGHTNESS_TOLERANCE_PT and abs(x0 - x1) >= _MIN_RULING_LENGTH_PT:
            horizontals.append(((y0 + y1) / 2.0, min(x0, x1), max(x0, x1)))
        elif abs(x0 - x1) <= _STRAIGHTNESS_TOLERANCE_PT and abs(y0 - y1) >= _MIN_RULING_LENGTH_PT:
            verticals.append(((x0 + x1) / 2.0, min(y0, y1), max(y0, y1)))

    for path in drawings:
        for item in path.get("items", []):
            try:
                kind = item[0]
                if kind == "l":
                    a, b = item[1], item[2]
                    add_segment(a.x, a.y, b.x, b.y)
                elif kind == "re":
                    r = item[1]
                    add_segment(r.x0, r.y0, r.x1, r.y0)
                    add_segment(r.x0, r.y1, r.x1, r.y1)
                    add_segment(r.x0, r.y0, r.x0, r.y1)
                    add_segment(r.x1, r.y0, r.x1, r.y1)
            except Exception:
                # A malformed path item must never take the page down.
                continue

    return {"h": horizontals, "v": verticals}


def enclosing_cell(x: float, y: float, rulings: dict, page_width: float, page_height: float):
    """The rectangle formed by the nearest ruling line on each side of a point.

    Returns (x0, y0, x1, y1). Where no ruling bounds a side, the page edge is
    used, so this always returns a usable rectangle and never raises.
    """
    left = 0.0
    right = page_width
    top = 0.0
    bottom = page_height

    for vx, vy0, vy1 in rulings.get("v", []):
        if vy0 - 1.0 <= y <= vy1 + 1.0:
            if vx <= x and vx > left:
                left = vx
            elif vx >= x and vx < right:
                right = vx

    for hy, hx0, hx1 in rulings.get("h", []):
        if hx0 - 1.0 <= x <= hx1 + 1.0:
            if hy <= y and hy > top:
                top = hy
            elif hy >= y and hy < bottom:
                bottom = hy

    return (left, top, right, bottom)


def find_label_lines(lines: list, wanted_label: str) -> list:
    """Lines whose whole text is exactly the wanted label.

    Exactness matters: a note sentence containing the word "scale" is not a
    scale label, and treating it as one is how a construction note ends up
    reported as a title-block field.
    """
    wanted = normalize_label(wanted_label)
    return [ln for ln in lines if normalize_label(ln["text"]) == wanted]


def _row_of(line: dict, lines: list) -> list:
    """Every line sharing a printed row with this one, left to right."""
    row = []
    for other in lines:
        overlap = vertical_overlap(line["bbox"], other["bbox"])
        shortest = min(bbox_height(line["bbox"]), bbox_height(other["bbox"])) or 1.0
        if overlap / shortest >= 0.5:
            row.append(other)
    row.sort(key=lambda ln: ln["bbox"][0])
    return row



# --- Reading a title block that is printed sideways -----------------------
#
# Australian offices commonly run the title strip up the right edge of the
# sheet, with every label and value rotated 90 degrees. The rules for finding
# a value do not change — it still sits to the right of its label, or beneath
# it — but "right" and "beneath" are directions in the *drawing's* frame, not
# the page's.
#
# So instead of a second set of probes, the geometry is turned 90 degrees,
# the same rules are applied, and the answer is turned back. One set of rules,
# working in whichever direction the sheet was drafted.


def _turn_bbox(bbox: list, page_height: float) -> list:
    """A box, with the page turned 90 degrees clockwise."""
    x0, y0, x1, y1 = bbox
    return [page_height - y1, x0, page_height - y0, x1]


def _turn_back_bbox(bbox: list, page_height: float) -> list:
    """The inverse of ``_turn_bbox``."""
    a0, b0, a1, b1 = bbox
    return [b0, page_height - a1, b1, page_height - a0]


def _turn_line(line: dict, page_height: float) -> dict:
    turned = dict(line)
    turned["bbox"] = _turn_bbox(line["bbox"], page_height)
    turned["axis"] = "horizontal" if line.get("axis") == "vertical" else "vertical"
    turned["_original"] = line
    return turned


def _turn_rulings(rulings: dict, page_height: float) -> dict:
    """Ruling lines under the same turn. A horizontal rule becomes a vertical
    one and the other way round."""
    turned_h = [(x, page_height - y1, page_height - y0) for x, y0, y1 in rulings.get("v", [])]
    turned_v = [(page_height - y, x0, x1) for y, x0, x1 in rulings.get("h", [])]
    return {"h": turned_h, "v": turned_v}


def value_candidates(
    label_line: dict,
    lines: list,
    rulings: dict,
    page_width: float,
    page_height: float,
    all_label_texts: set,
) -> list:
    """Every plausible value for one label, each tagged with how it was found.

    Returns a list of {"lines": [...], "technique": str, "bbox": [...]},
    strongest first. An empty list is a real and useful answer — it means the
    label's cell is blank on this sheet, which must surface as "not detected"
    rather than borrowing a neighbour's value.
    """
    if label_line.get("axis") == "vertical":
        # The whole title block is drafted sideways. Turn the page, use the
        # ordinary rules, turn the answer back.
        turned = value_candidates(
            _turn_line(label_line, page_height),
            [_turn_line(line, page_height) for line in lines],
            _turn_rulings(rulings, page_height),
            page_height,
            page_width,
            all_label_texts,
        )
        for candidate in turned:
            candidate["bbox"] = _turn_back_bbox(candidate["bbox"], page_height)
            candidate["lines"] = [
                line.get("_original", line) for line in candidate["lines"]
            ]
        return turned

    lx0, ly0, lx1, ly1 = label_line["bbox"]
    label_h = bbox_height(label_line["bbox"]) or 6.0
    cell = enclosing_cell((lx0 + lx1) / 2.0, (ly0 + ly1) / 2.0, rulings, page_width, page_height)

    row = _row_of(label_line, lines)
    # Where this label's column ends: at the next label to its right on the
    # same row, otherwise at the enclosing frame edge.
    band_right = cell[2]
    for other in row:
        if other is label_line:
            continue
        if other["bbox"][0] > lx0 + 1.0:
            band_right = min(band_right, other["bbox"][0])
            break
    band_left = max(cell[0], lx0 - _COLUMN_LEFT_SLACK_PT)

    candidates: list = []

    # --- Probe 1: the next cell to the right on the same row ---------------
    for other in row:
        if other is label_line or other["bbox"][0] <= lx1:
            continue
        if normalize_label(other["text"]) in all_label_texts:
            break  # two labels side by side: this label's own cell is empty
        gap = other["bbox"][0] - lx1
        if gap > label_h * _MAX_ROW_GAP_IN_LABEL_HEIGHTS:
            break
        if other["bbox"][0] >= cell[2]:
            break  # outside the enclosing frame — a different part of the sheet
        candidates.append(
            {"lines": [other], "technique": "label_value_right", "bbox": list(other["bbox"])}
        )
        break

    # --- Probe 2: the cell directly beneath, inside this label's column ----
    # The enclosing frame is deliberately NOT used as a bottom clamp here.
    # These title blocks frequently rule a line between a label and its own
    # value (label cell above, value cell below), so clamping at the first
    # rule under the label discards the value it is looking for — measured on
    # the supplied set as eight sheets losing a scale that was plainly
    # printed. Three independent constraints already bound this probe: the
    # column band (left/right), the maximum gap in label heights, and
    # stopping at the next label in the same column.
    below = []
    label_centre_y = (ly0 + ly1) / 2.0
    for other in lines:
        if other is label_line:
            continue
        ox0, oy0, ox1, oy1 = other["bbox"]
        # "Below" is judged by the centres, not the top edges. A value printed
        # in larger type than its label has a taller box that starts level
        # with the label's own band, and an edge test rejects it by a fraction
        # of a point — which is how a plainly printed drawing number came to
        # be reported as missing.
        if (oy0 + oy1) / 2.0 <= label_centre_y:
            continue
        if oy0 < ly0 + label_h * 0.3:
            continue  # starts above the label — not beneath it
        centre_x = (ox0 + ox1) / 2.0
        if not (band_left <= ox0 + 0.5 or band_left <= centre_x <= band_right):
            continue
        if ox0 >= band_right - 0.5:
            continue  # belongs to the next column
        if oy0 - ly1 > label_h * _MAX_COLUMN_GAP_IN_LABEL_HEIGHTS:
            continue
        below.append(other)

    if below:
        below.sort(key=lambda ln: (ln["bbox"][1], ln["bbox"][0]))
        # Stop at the next label in this column — anything past it belongs to
        # the following field, not to this one.
        kept = []
        for ln in below:
            if normalize_label(ln["text"]) in all_label_texts:
                break
            kept.append(ln)
        if kept:
            # A value printed across two stacked lines in the same cell
            # ('REFLECTED CEILING' / 'PLAN') is one value — group the run of
            # consecutive lines that sit tight against each other.
            group = [kept[0]]
            for prev, cur in zip(kept, kept[1:]):
                line_gap = cur["bbox"][1] - prev["bbox"][3]
                if line_gap > max(bbox_height(prev["bbox"]), 2.0) * 0.9:
                    break
                group.append(cur)
            x0 = min(ln["bbox"][0] for ln in group)
            y0 = min(ln["bbox"][1] for ln in group)
            x1 = max(ln["bbox"][2] for ln in group)
            y1 = max(ln["bbox"][3] for ln in group)
            candidates.append(
                {"lines": group, "technique": "label_value_below", "bbox": [x0, y0, x1, y1]}
            )

    return candidates


def joined_text(lines: list) -> str:
    """Text of a multi-line value cell, joined in reading order.

    A hyphen or comma left dangling at the end of the first line ('PLAN DET. &
    INT. ELEV. -' / 'KITCHEN') means the title was wrapped mid-phrase, so the
    parts are joined with a single space and the printed wording is preserved
    exactly otherwise.
    """
    parts = [ln["text"].strip() for ln in lines if ln["text"].strip()]
    if not parts:
        return ""
    out = parts[0]
    for part in parts[1:]:
        if out.endswith("-") and not out.endswith(" -"):
            out = out[:-1] + part  # hyphenated word split across lines
        elif out.endswith(".") and part[:1].isdigit():
            # An abbreviation whose number wrapped to the next line:
            # 'WINDOW SCHEDULE SHT.' + '1' is 'SHT.1', not 'SHT. 1'.
            out = out + part
        else:
            out = f"{out} {part}"
    return " ".join(out.split())


