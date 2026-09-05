"""The scale a sheet prints about itself, read from its title block.

This is the **fallback**, and it matters that it is one. Measuring the scale off
the sheet's own dimension figures (``calibration.py``) is evidence: the figure
and the line that measures it are both on the paper and they check each other.
A printed ratio is a *claim* - a sheet re-plotted from A3 to A4 still prints
"1:100" and is not at 1:100 any more. So this is only consulted where the
measurement could not be made, and whatever it returns is marked as unverified.

But a great many sheets cannot be measured, and they are not unusual sheets: a
plan set published as pictures has no dimension lines to measure against at
all, and a sheet with two or three figures on it has too few to pool. Measured
on one unseen 17-sheet set, only 9 sheets could measure themselves. Without
this fallback the other 8 report no lengths - which is honest, and useless.

**Where it looks.** A title block sits along an edge of the sheet - AS 1100
puts it bottom-right, and offices in practice use the bottom strip, the right
strip, and occasionally the left or the top. So the four edge bands are
searched first and the rest of the sheet only after. That ordering is what
separates *this sheet's* scale from the scales printed beside its details: on
one real detail sheet the title block says ``1:50 @ A3`` while ``1:50`` is also
set in large type under one drawing and a drawing index elsewhere lists 1:200,
1:100 and 1:50 - only one of those is the sheet's own.

**What it has to survive**, all met on real sheets rather than imagined:

*   ``SCALE: 1:100 @ A3`` on one line.
*   ``Scale:`` on one line with ``1:100 @ A3`` on the next - a label above its
    value, which is how a ruled title block sets out a cell.
*   ``SCALE:`` with ``1 : 200`` printed beside it, spaces and all.
*   ``1:100MM FALL ON SPANDECK`` - **not a scale.** A ratio immediately
    followed by a letter is a fall, a grade or a product code.
*   ``DO NOT SCALE DRAWING`` and ``DO NOT SCALE FROM DRAWINGS`` - **not a
    scale label.** Both are printed in the bottom strip of real sheets, right
    where a scale would be.
*   ``NTS`` / ``NOT TO SCALE`` - a positive statement that nothing on this
    drawing may be measured, which is different from finding nothing.
*   A cover sheet printing ``Scale:`` with no value beside it, and a drawing
    index listing every scale in the set. Neither is this sheet's scale.

**The sheet size is part of the claim.** ``1:50 @ A3`` says the ratio holds
when the drawing is printed at A3. If the page really is A3 the ratio stands as
printed; if the page is A4, the drawing has been reduced and every length on it
is out by the ratio of the two sheets' long edges - which is a correction this
can make exactly, because ISO sheet sizes are a standard and the page states
its own size. That is the whole reason offices print the sheet size next to the
scale.

**It never guesses.** Where no ratio can be tied to this sheet, or where two
equally-supported ratios disagree, it returns ``None`` with the reason logged.
A wrong scale makes every length wrong by the same factor with nothing looking
odd, which is the one failure this product must never produce silently.
"""

import re
from dataclasses import dataclass, field

from app.logging_setup import get_logger
from pipeline.plan.cvdetect.settings import number, setting

logger = get_logger()

# One PDF point is 1/72 inch of paper. A unit definition, not a tuning value.
MM_PER_POINT_AT_FULL_SIZE = 25.4 / 72.0

# ISO 216 sheet sizes in millimetres, short edge first. These are an
# international standard - the same physical constant as "a point is 1/72
# inch" - so they belong in code rather than in an office's config file
# (CLAUDE.md 4C). An office that plots on ANSI or ARCH sizes states no A-size
# suffix, and nothing here fires.
ISO_SHEET_MM = {
    "A0": (841.0, 1189.0),
    "A1": (594.0, 841.0),
    "A2": (420.0, 594.0),
    "A3": (297.0, 420.0),
    "A4": (210.0, 297.0),
}

# A printed scale ratio. Three guards, each put there by a real sheet:
#
#   (?<![\d.:/])   a ratio is not the tail of a bigger number, and "10:30" is
#                  a time - the numerator is checked below as well.
#   (?![\dA-Za-z]) a ratio is not immediately followed by a letter. On one real
#                  floor plan "1:100MM FALL ON SPANDECK" is a fall, not a scale,
#                  and it sits inside the title-block band.
_RATIO = re.compile(r"(?<![\d.:/])(\d{1,2})\s*[:/]\s*(\d{1,5})(?![\dA-Za-z])")

# The sheet the ratio is stated at: "@ A3", "AT A1", "(A3)", " A3".
_STATED_SHEET = re.compile(r"(?:@|\bAT\b|\()\s*(A[0-4])\b|\bA([0-4])\b")

# A positive statement that nothing on this drawing may be measured.
_NOT_TO_SCALE = re.compile(r"\bN\.?\s?T\.?\s?S\.?\b|\bNOT\s+TO\s+SCALE\b", re.I)

# The word that labels a scale cell.
_SCALE_WORD = re.compile(r"\bSCALES?\b", re.I)

# ...and the two ways that word appears on a drawing while labelling nothing.
# Both are printed in the bottom strip of real sheets, exactly where a title
# block is, so neither can be left to chance.
_NOT_A_SCALE_LABEL = re.compile(r"DO\s+NOT\s+SCALE|NOT\s+TO\s+SCALE", re.I)

# Architectural ratios are stated the small way round: 1:100, occasionally 2:1
# for an enlarged detail. A numerator larger than this is a time, a date or a
# reference, not a scale.
_LARGEST_NUMERATOR = 5
_LARGEST_DENOMINATOR = 5000


@dataclass
class PrintedScale:
    """A scale statement found on the sheet, and everything known about it."""

    numerator: int
    denominator: int
    mm_per_point: float
    text: str
    bbox: list
    band: int
    labelled: bool
    order: int = 0
    stated_sheet: str = None
    page_sheet: str = None
    sheet_correction: float = 1.0
    note: str = ""
    evidence: list = field(default_factory=list)

    @property
    def ratio(self) -> str:
        return f"{self.numerator}:{self.denominator}"

    @property
    def in_title_block(self) -> bool:
        return self.band > 0

    @property
    def rank(self) -> int:
        """How strongly this statement is tied to *this sheet*.

        A ratio printed in the title block beside the word SCALE is the sheet
        saying what it is drawn at. One printed out on the paper under a
        drawing is that drawing's caption, and one in a drawing index belongs
        to a different sheet entirely. The tiers, strongest first:

        | 4 | labelled, in the bottom or right strip - AS 1100's own position |
        | 3 | labelled, in the left or top strip - the uncommon variants |
        | 2 | labelled, anywhere else on the sheet |
        | 0 | **unlabelled - not usable, wherever it is printed** |

        **Zero matters as much as four.** A title block *labels* its scale
        cell; a ratio with no label beside it is a caption under a drawing.
        Two real sheets make the point: a details sheet prints ``1:20`` under
        one detail while its other drawings are marked NTS, and a floor plan
        prints ``1:50`` in large type under one drawing while its title block
        says ``1:50 @ A3`` - taking the caption would be right by luck on the
        second and wrong on the first. Measured across all three plan sets in
        use, every scale actually recovered scores 4, so refusing the
        unlabelled ones costs nothing and removes the whole class of error.
        """
        if not self.labelled:
            return 0
        return {2: 4, 1: 3}.get(self.band, 2)


def _text_height(bbox: list) -> float:
    """A line's printed type size, whichever way round it is printed.

    The shorter side of the box: a line set the ordinary way round is as tall
    as its type, and one printed rotated 90 degrees is as *wide* as its type.
    Using the height alone makes the allowance five times too generous on
    exactly the sideways title blocks this has to read.
    """
    return max(min(abs(bbox[2] - bbox[0]), abs(bbox[3] - bbox[1])), 0.1)


def page_sheet_size(page):
    """The ISO sheet this page is, and its size in millimetres.

    Returns ``(name, short_mm, long_mm)``; the name is ``None`` where the page
    is not within tolerance of any A size - an office plotting on ANSI or on a
    trimmed sheet, which is a fact about the drawing and not an error.
    """
    try:
        width_mm = page.rect.width * MM_PER_POINT_AT_FULL_SIZE
        height_mm = page.rect.height * MM_PER_POINT_AT_FULL_SIZE
    except Exception as e:
        logger.exception(f"page_sheet_size: this page has no readable size: {e}")
        return (None, 0.0, 0.0)

    short_mm, long_mm = min(width_mm, height_mm), max(width_mm, height_mm)
    best, best_error = None, None
    for name, (iso_short, iso_long) in ISO_SHEET_MM.items():
        error = max(abs(short_mm - iso_short) / iso_short, abs(long_mm - iso_long) / iso_long)
        if best_error is None or error < best_error:
            best, best_error = name, error
    # Two per cent covers a trimmed edge and a rounded mediabox; it does not
    # cover the gap between one A size and the next, which is 41%.
    return (best if best_error is not None and best_error <= 0.02 else None, short_mm, long_mm)


def _title_block_bands(page, share: float) -> list:
    """The four edge strips a title block is drawn in.

    AS 1100 puts the title block bottom-right, and offices in practice use the
    bottom strip, the right strip and occasionally the left or the top - all
    four have been seen on real plan sets (CLAUDE.md 4AC). Which edge it is on
    is not worth guessing at, because a strip is cheap to search and the
    ranking below settles ties.
    """
    try:
        rect = page.rect
        x0, y0, x1, y1 = rect.x0, rect.y0, rect.x1, rect.y1
    except Exception:
        return []
    width, height = x1 - x0, y1 - y0
    if width <= 0 or height <= 0:
        return []
    deep, across = height * share, width * share
    # The strength of each strip, not merely its extent. AS 1100 puts the
    # title block bottom-right, so those two are where a scale printed near
    # the edge really is the sheet's own. The left and top strips are the
    # uncommon variants, and a drawing runs into them far more often.
    return [
        (2, (x0, y1 - deep, x1, y1)),       # bottom
        (2, (x1 - across, y0, x1, y1)),     # right
        (1, (x0, y0, x0 + across, y1)),     # left
        (1, (x0, y0, x1, y0 + deep)),       # top
    ]


def _band_of(bbox: list, bands: list) -> int:
    """The strongest strip this text sits in: 2, 1, or 0 for none."""
    centre_x = (bbox[0] + bbox[2]) / 2.0
    centre_y = (bbox[1] + bbox[3]) / 2.0
    best = 0
    for strength, (bx0, by0, bx1, by1) in bands:
        if bx0 <= centre_x <= bx1 and by0 <= centre_y <= by1:
            best = max(best, strength)
    return best


def _label_lines(lines: list) -> list:
    """Lines carrying the word SCALE as a label, and only those.

    ``DO NOT SCALE DRAWING`` is printed across the bottom strip of real sheets,
    right where a title block is. Counted as a label it would make whatever
    ratio happened to sit near it look like the sheet's own scale.
    """
    labels = []
    for line in lines:
        text = line.get("text") or ""
        if not _SCALE_WORD.search(text) or _NOT_A_SCALE_LABEL.search(text):
            continue
        labels.append(line)
    return labels


def _is_labelled(
    value_bbox: list, same_line_text: str, labels: list, reach_heights: float,
    column_headers: set = frozenset(),
) -> bool:
    """Whether a ratio is printed as the value of a SCALE cell.

    Either the word is on the same line as the ratio, or a SCALE line sits
    **beside it on the left** or **directly above it** - which is how a ruled
    title block sets out a cell, and how both plan sets in use actually print
    it. The allowance is measured in the label's own printed type size, so it
    means the same on an A4 sheet and an A0 one.

    **A word with a column of values under it is a table heading, not a cell
    label.** A cover sheet's drawing index has SCALE as a column header with a
    row for every sheet in the set - measured on one real cover, 22 ratios
    stacked beneath it listing 1:200, 1:100 and 1:50. Read as a label, the
    first of them became "this sheet's scale" with the highest confidence the
    reader can give, on a sheet that draws nothing at all. A title block's
    scale cell holds exactly one value.
    """
    if _SCALE_WORD.search(same_line_text) and not _NOT_A_SCALE_LABEL.search(same_line_text):
        return True

    vx0, vy0, vx1, vy1 = value_bbox
    for index, label in enumerate(labels):
        lx0, ly0, lx1, ly1 = label["bbox"]
        reach = reach_heights * _text_height(label["bbox"])

        # Beside it, on the same row: the label is to the left and the two
        # overlap vertically.
        if min(vy1, ly1) - max(vy0, ly0) > 0 and lx1 - 1.0 <= vx0 <= lx1 + reach:
            return True
        # Above it, in the same column: they overlap horizontally and the value
        # sits just below - unless this label heads a whole column of them.
        if min(vx1, lx1) - max(vx0, lx0) > 0 and ly1 - 1.0 <= vy0 <= ly1 + reach:
            if index not in column_headers:
                return True
    return False


def _column_headers(lines: list, labels: list) -> set:
    """Which SCALE words head a column of ratios rather than label one value.

    Counted down the whole column beneath the word, not merely within the
    label's own reach: an index's rows carry on well past it, and that is
    exactly what distinguishes them from a cell's single value.
    """
    headers = set()
    for index, label in enumerate(labels):
        lx0, ly0, lx1, ly1 = label["bbox"]
        beneath = 0
        for line in lines:
            bbox = line.get("bbox")
            if not bbox:
                continue
            if bbox[1] < ly1 - 1.0:
                continue
            if min(bbox[2], lx1) - max(bbox[0], lx0) <= 0:
                continue
            if _RATIO.search(line.get("text") or ""):
                beneath += 1
        if beneath > 1:
            headers.add(index)
    return headers


def _statements(lines: list, bands: list, reach_heights: float) -> list:
    """Every scale ratio printed on the sheet, with what is known about each."""
    labels = _label_lines(lines)
    headers = _column_headers(lines, labels)
    found = []
    for line in lines:
        text = (line.get("text") or "").strip()
        if not text:
            continue
        bbox = [float(v) for v in line["bbox"]]
        for match in _RATIO.finditer(text):
            numerator, denominator = int(match.group(1)), int(match.group(2))
            if not (1 <= numerator <= _LARGEST_NUMERATOR):
                continue
            if not (1 <= denominator <= _LARGEST_DENOMINATOR):
                continue
            found.append(
                PrintedScale(
                    numerator=numerator,
                    denominator=denominator,
                    mm_per_point=MM_PER_POINT_AT_FULL_SIZE * denominator / numerator,
                    text=text,
                    bbox=bbox,
                    band=_band_of(bbox, bands),
                    order=match.start(),
                    labelled=_is_labelled(bbox, text, labels, reach_heights, headers),
                    stated_sheet=_stated_sheet_in(text),
                )
            )
    return found


def _stated_sheet_in(text: str) -> str:
    match = _STATED_SHEET.search(text.upper())
    if not match:
        return None
    return match.group(1) or (f"A{match.group(2)}" if match.group(2) else None)


def _says_not_to_scale(lines: list, bands: list, labels: list, reach_heights: float) -> bool:
    """Whether the sheet's own title block states that it is not to scale.

    Only the title block counts. ``NTS`` printed under one detail on a sheet
    that also carries a 1:20 drawing says something about that detail, not
    about the sheet - and treating it as the sheet's answer would throw away a
    drawing that can perfectly well be measured.
    """
    for line in lines:
        text = (line.get("text") or "").strip()
        if not text or not _NOT_TO_SCALE.search(text):
            continue
        bbox = [float(v) for v in line["bbox"]]
        if _band_of(bbox, bands) <= 0:
            continue
        if _SCALE_WORD.search(text) or _is_labelled(bbox, text, labels, reach_heights):
            return True
    return False


def scale_from_title_block(page, lines: list, settings: dict):
    """The scale this sheet prints about itself, or ``None``.

    ``lines`` are the sheet's printed text lines - the caller's own recognition
    results where it has them, so a scanned sheet is read from exactly the text
    the rest of the reader is using.

    Returns a :class:`PrintedScale`, or ``None`` where the sheet states no
    usable scale, states that it is not to scale, or states two that disagree
    with equal authority. Never raises and never guesses.
    """
    try:
        share = number(settings, "scale.title_block_band_share", 0.25)
        reach_heights = number(settings, "scale.label_reach_text_heights", 6.0)
        bands = _title_block_bands(page, share)
        if not bands or not lines:
            logger.info("title-block scale: this sheet has no text to read a scale from")
            return None

        labels = _label_lines(lines)
        if _says_not_to_scale(lines, bands, labels, reach_heights):
            logger.info("title-block scale: this sheet's title block states it is not to scale")
            return None

        statements = _statements(lines, bands, reach_heights)
        if not statements:
            logger.info("title-block scale: this sheet prints no scale ratio")
            return None

        best_rank = max(s.rank for s in statements)
        if best_rank == 0:
            # Nothing was in the title block and nothing carried the word
            # SCALE. What is left is a caption under a drawing or a row of a
            # drawing index, and neither is this sheet speaking about itself.
            logger.info(
                "title-block scale: the only ratios on this sheet are drawing captions or "
                "index rows, not its own title block"
            )
            return None

        shortlist = [s for s in statements if s.rank == best_rank]
        chosen = _one_of(shortlist, best_rank)
        if chosen is None:
            return None

        _apply_sheet_size(page, chosen, settings)
        logger.info(
            f"title-block scale: this sheet states {chosen.ratio}"
            + (f" at {chosen.stated_sheet}" if chosen.stated_sheet else "")
            + f", which makes one point {chosen.mm_per_point:.2f} mm"
        )
        return chosen
    except Exception as e:
        logger.exception(f"scale_from_title_block: this sheet's scale could not be read: {e}")
        return None


def _one_of(shortlist: list, rank: int):
    """Picks between equally-supported statements, or refuses to.

    A sheet may legitimately print more than one scale - a plan at 1:100 with
    an enlarged detail beside it at 1:1 prints both - and where they are
    printed **in the title block against the word SCALE**, the first in reading
    order is the drawing's own and the rest belong to its details. That is a
    drafting convention rather than a guess.

    Anywhere weaker than that, two different ratios with the same claim on the
    sheet are simply ambiguous, and nothing is returned.
    """
    denominators = {(s.numerator, s.denominator) for s in shortlist}
    if len(denominators) == 1:
        return shortlist[0]

    if rank == 4:
        # Reading order: down the sheet, then across, then left to right
        # within one printed line - a title block commonly prints
        # "SCALE: 1:100, 1:1" as a single line, where "first" means the first
        # of the two on that line and nothing about position can say so.
        ordered = sorted(
            shortlist,
            key=lambda s: (round(s.bbox[1], 1), round(s.bbox[0], 1), s.order),
        )
        chosen = ordered[0]
        chosen.evidence.append(
            f"This sheet's title block prints {len(denominators)} scales; the first, "
            f"{chosen.ratio}, is the drawing's own and the rest belong to its details."
        )
        return chosen

    logger.info(
        f"title-block scale: this sheet prints {len(denominators)} different scales with equal "
        "authority, so none of them can be taken as its own"
    )
    return None


def _apply_sheet_size(page, chosen: PrintedScale, settings: dict) -> None:
    """Corrects the ratio where the sheet was printed at a size it was not drawn for.

    ``1:50 @ A3`` says the ratio holds when the drawing is printed at A3. If the
    page really is A3 the ratio stands. If the page is A4, the drawing has been
    reduced and every length taken from the printed ratio would be out by the
    ratio of the two sheets' long edges - 1.414 between one A size and the next,
    which is not a rounding error, it is a wall 3 metres long reported as 2.1.

    This is exactly why offices print the sheet size next to the scale, and it
    is only possible because ISO sizes are a standard and the page states its
    own size.
    """
    name, _short_mm, long_mm = page_sheet_size(page)
    chosen.page_sheet = name
    if not chosen.stated_sheet or not setting(settings, "scale.correct_for_stated_sheet_size", True):
        return
    stated = ISO_SHEET_MM.get(chosen.stated_sheet)
    if not stated or long_mm <= 0:
        return

    correction = stated[1] / long_mm
    tolerance = number(settings, "scale.sheet_size_tolerance_pct", 2.0) / 100.0
    if abs(correction - 1.0) <= tolerance:
        chosen.sheet_correction = 1.0
        chosen.note = (
            f"The sheet states {chosen.ratio} at {chosen.stated_sheet} and the page is "
            f"{chosen.stated_sheet}, so the printed scale stands as stated."
        )
        return

    chosen.sheet_correction = correction
    chosen.mm_per_point *= correction
    chosen.note = (
        f"The sheet states {chosen.ratio} at {chosen.stated_sheet}, but this page is "
        f"{name or f'{long_mm:.0f} mm on its long edge'} - so the drawing has been "
        f"re-printed at a different size and every length on it is out by a factor of "
        f"{correction:.3f}. That correction has been applied; check the printed scale "
        "before relying on lengths taken from this sheet."
    )
    logger.warning(
        f"title-block scale: stated at {chosen.stated_sheet} but the page is "
        f"{name or 'a non-standard size'}; correcting by {correction:.3f}"
    )
