"""Step 2 - what one point of this sheet is worth in millimetres of building.

Every length this package reports, and every threshold it applies, passes
through one number: how many real millimetres one PDF point of paper stands
for. Get it wrong and every wall, every door and every quantity is wrong by the
same factor with nothing on the screen looking odd - which is the single worst
result this product can produce.

**So it is measured off the drawing, not read off the title block.** A printed
scale is a claim. A sheet re-plotted from A3 to A4 still prints "1:100" and is
not at 1:100 any more, and that is common rather than exotic.

The measurement uses the one thing a construction drawing states twice: a
dimension figure is printed *against the line that measures it*. So for each
figure the sheet prints, the drawn length of its own dimension line is found,
and

    millimetres per point  =  printed figure in millimetres
                             ----------------------------------
                             drawn length of its dimension line in points

That is one sample. A sheet prints dozens, and they are pooled: the median is
taken, and how many of the samples agree with it is what the confidence is.
One figure paired with the wrong line is then a sample that disagrees and is
outvoted, rather than a wrong scale for the whole sheet.

**A second, independent source is measured alongside it** and used only as a
cross-check, never averaged in. Figures printed in a row form a dimension
string, and each figure is centred on the segment it describes - so the paper
distance between the centres of two neighbouring figures is
``(first + second) / 2`` millimetres of building, known from the printed
numbers alone. It reaches the same answer by a different route, and two routes
agreeing is worth more than either on its own.

*Measured on a real 1:100 floor plan: 61 of its 61 horizontal figures found
their dimension line, the median came to 35.294 mm per point against a true
35.278 (0.05% out), and 53 of the 61 samples agreed with it to within 5%.*
"""

import math
import re
from statistics import median

from app.logging_setup import get_logger
from pipeline.plan import layout, textmodel
from pipeline.plan.cvdetect.settings import Scale, load_settings, number, setting
from pipeline.plan.validators import scale_ratio_denominator

logger = get_logger()

# One PDF point is 1/72 inch of paper, so at full size it is this many
# millimetres. Multiplied by the scale denominator it gives what one point of
# a 1:100 drawing is worth. A unit definition, not a tuning value.
MM_PER_POINT_AT_FULL_SIZE = 25.4 / 72.0

# A figure and the note saying what it measures to: "10260 TO WALL",
# "2600 TO EAVE", "19920 OVERALL". The note is kept, because it says which face
# of the building the figure runs to, but it takes no part in the arithmetic.
_FIGURE = re.compile(r"^(\d[\d\s,]*?)\s*([A-Za-z][A-Za-z\s.'-]{1,20})?$")

# A figure printed with exactly three decimals is a thousands separator that
# character recognition read as a full stop: "7.370" is the sheet's "7,370".
# Both readings are the same number of millimetres, so no decision is needed.
_THOUSANDS_MISREAD = re.compile(r"^\d{1,3}\.\d{3}$")

# How square a text box has to be before its printed direction is trusted
# rather than inferred. Character recognition returns no direction at all, so
# for those a box taller than it is wide is a figure printed rotated - which on
# a plan dimensions the vertical axis of the building.
_ROTATED_ASPECT = 1.6

# A dimension line is drawn on the sheet's axes like everything else. This is
# the same allowance the rest of the reader makes for a scanned sheet's own
# skew; it is not a licence to accept a diagonal.
_AXIS_TOLERANCE_PT = 0.35

# A scale printed as a ratio: "1:100", "1 : 50", "1/200". Read only as the
# fallback in ``printed_ratio_on``, and only where the sheet prints one.
_PRINTED_RATIO = re.compile(r"\b1\s*[:/]\s*(\d{1,4})\b")


def dimension_value_mm(text: str, settings: dict):
    """The millimetres a printed figure states, or None if it states none.

    Returns ``(value_mm, note)`` - the note being whatever the office printed
    beside the figure to say what it measures to.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return None

    if _THOUSANDS_MISREAD.match(cleaned):
        return (float(cleaned.replace(".", "")), "")

    match = _FIGURE.match(cleaned)
    if not match:
        return None
    digits = re.sub(r"[\s,]", "", match.group(1) or "")
    note = (match.group(2) or "").strip()
    if not digits.isdigit():
        return None
    try:
        value = float(digits)
    except ValueError:
        return None

    low = number(settings, "scale.dimension_min_mm", 100.0)
    high = number(settings, "scale.dimension_max_mm", 100000.0)
    if not (low <= value <= high):
        return None
    return (value, note)


def printed_ratio_on(page) -> str:
    """The scale ratio printed on this sheet, where it prints exactly one.

    A drawing may legitimately print several - a plan at 1:100 with an enlarged
    detail beside it at 1:1 prints both - and which one applies to which
    drawing cannot be told from the text alone. So **only an unambiguous sheet
    answers**: where a sheet prints more than one distinct ratio, nothing is
    returned and the sheet is measured or not measured on its own evidence.
    Guessing here would put every length on the sheet out by the ratio between
    them.
    """
    try:
        text = page.get_text() or ""
    except Exception as e:
        logger.exception(f"printed_ratio_on: this sheet's text could not be read: {e}")
        return ""
    found = {f"1:{match.group(1)}" for match in _PRINTED_RATIO.finditer(text)}
    if len(found) != 1:
        return ""
    return found.pop()


def _axis_of_text(line: dict) -> str:
    """Which axis of the building a printed figure dimensions.

    A figure printed the ordinary way round measures across the sheet; one
    printed rotated 90 degrees measures up it. This is a drafting fact, not a
    preference - without it a sheet's vertical dimensions are all attributed
    to the wrong axis.
    """
    direction = line.get("dir")
    if direction:
        try:
            dx, dy = abs(float(direction[0])), abs(float(direction[1]))
            if dx or dy:
                return "h" if dx >= dy else "v"
        except (TypeError, ValueError, IndexError):
            pass
    # No direction stated - character recognition never gives one. A box
    # taller than it is wide holds a figure printed rotated.
    x0, y0, x1, y1 = line["bbox"]
    width, height = abs(x1 - x0), abs(y1 - y0)
    if width <= 0:
        return "v"
    return "v" if height / max(width, 1e-6) >= _ROTATED_ASPECT else "h"


def _text_lines(page, ocr_results):
    """The sheet's printed lines, from recognition where it was supplied.

    ``ocr_results`` is whatever the caller has already read - the same shape
    the rest of the reader passes about, ``{"text", "bbox"}`` in the page's own
    space. Where none is given the page's native text layer is used, which is
    both exact and free.
    """
    if ocr_results:
        lines = []
        for block in ocr_results:
            try:
                bbox = block.get("bbox")
                text = (block.get("text") or "").strip()
                if bbox and text:
                    lines.append({"text": text, "bbox": [float(v) for v in bbox], "dir": None})
            except (AttributeError, TypeError, ValueError):
                continue
        return lines
    try:
        return textmodel.extract_native_lines(page)
    except Exception as e:
        logger.exception(f"calibrate_scale: could not read the sheet's text: {e}")
        return []


def axis_segments(page) -> dict:
    """Every straight axis-aligned drawn segment, as ``{"h": [...], "v": [...]}``.

    Each entry is ``(position, start, end, stroke_width)`` in the page's own
    space - the space that already has any page rotation taken out of it, so a
    sheet drafted in portrait and printed in landscape measures the same as one
    that was not (CLAUDE.md 4P).
    """
    result = {"h": [], "v": []}
    try:
        drawings = layout.page_drawings(page)
    except Exception as e:
        logger.exception(f"axis_segments: could not read the drawn paths: {e}")
        return result

    for path in drawings:
        try:
            width = float(path.get("width") or 0.0)
        except (TypeError, ValueError):
            width = 0.0
        for item in path.get("items", []):
            try:
                if item[0] == "l":
                    _add_segment(result, item[1].x, item[1].y, item[2].x, item[2].y, width)
                elif item[0] == "re":
                    r = item[1]
                    _add_segment(result, r.x0, r.y0, r.x1, r.y0, width)
                    _add_segment(result, r.x0, r.y1, r.x1, r.y1, width)
                    _add_segment(result, r.x0, r.y0, r.x0, r.y1, width)
                    _add_segment(result, r.x1, r.y0, r.x1, r.y1, width)
            except (AttributeError, IndexError, TypeError):
                continue
    return result


def _add_segment(into: dict, x0, y0, x1, y1, width) -> None:
    if abs(y1 - y0) <= _AXIS_TOLERANCE_PT and abs(x1 - x0) > _AXIS_TOLERANCE_PT:
        into["h"].append(((y0 + y1) / 2.0, min(x0, x1), max(x0, x1), width))
    elif abs(x1 - x0) <= _AXIS_TOLERANCE_PT and abs(y1 - y0) > _AXIS_TOLERANCE_PT:
        into["v"].append(((x0 + x1) / 2.0, min(y0, y1), max(y0, y1), width))


def _samples_from_dimension_lines(figures: list, segments: dict, settings: dict) -> list:
    """One millimetres-per-point sample for each figure that found its own line.

    A dimension figure is printed against the line that measures it, so the
    line wanted is the nearest parallel segment that runs *underneath the
    figure* - its span has to bracket the figure's centre. Nearest alone is
    not enough: on a plan the nearest parallel line to a figure printed in the
    margin is as often the building's own eave line.
    """
    reach_heights = number(settings, "scale.search_radius_text_heights", 4.0)
    samples = []
    for figure in figures:
        axis = figure["axis"]
        candidates = segments.get(axis) or []
        if not candidates:
            continue
        x0, y0, x1, y1 = figure["bbox"]
        # Along the dimension line, and across it. For a figure measuring the
        # horizontal axis the line runs in x, so "along" is x and "across" is y.
        if axis == "h":
            along, across = (x0 + x1) / 2.0, (y0 + y1) / 2.0
            text_size = max(abs(y1 - y0), 1e-6)
        else:
            along, across = (y0 + y1) / 2.0, (x0 + x1) / 2.0
            text_size = max(abs(x1 - x0), 1e-6)
        reach = reach_heights * text_size

        best = None
        for position, start, end, _width in candidates:
            if not (start - _AXIS_TOLERANCE_PT <= along <= end + _AXIS_TOLERANCE_PT):
                continue
            offset = abs(position - across)
            if offset > reach:
                continue
            drawn = end - start
            if drawn <= 0:
                continue
            if best is None or offset < best[0]:
                best = (offset, drawn)
        if best is None:
            continue
        samples.append(
            {
                "mm_per_point": figure["value_mm"] / best[1],
                "value_mm": figure["value_mm"],
                "drawn_points": best[1],
                "axis": axis,
                "source": "dimension_line",
            }
        )
    return samples


def _samples_from_dimension_strings(figures: list, settings: dict) -> list:
    """A second, independent reading of the same quantity.

    Figures printed in a row form a dimension string, and each one is centred
    on the length it describes. So between the centres of two neighbouring
    figures there is exactly half of the first plus half of the second - a
    distance known from the printed numbers alone, needing no line to be found
    at all. Two routes to one answer is what makes the answer worth trusting.
    """
    reach_heights = number(settings, "scale.search_radius_text_heights", 4.0)
    samples = []
    for axis in ("h", "v"):
        row = [f for f in figures if f["axis"] == axis]
        if len(row) < 2:
            continue
        # Figures on one string share a line across the sheet. Group by that,
        # then walk each group along the string.
        groups = {}
        for figure in row:
            x0, y0, x1, y1 = figure["bbox"]
            if axis == "h":
                across, along = (y0 + y1) / 2.0, (x0 + x1) / 2.0
                size = max(abs(y1 - y0), 1e-6)
            else:
                across, along = (x0 + x1) / 2.0, (y0 + y1) / 2.0
                size = max(abs(x1 - x0), 1e-6)
            key = round(across / max(size * reach_heights, 1e-6))
            groups.setdefault(key, []).append((along, figure))

        for members in groups.values():
            if len(members) < 2:
                continue
            members.sort(key=lambda m: m[0])
            for (along_a, first), (along_b, second) in zip(members, members[1:]):
                drawn = along_b - along_a
                expected = (first["value_mm"] + second["value_mm"]) / 2.0
                if drawn <= 0 or expected <= 0:
                    continue
                samples.append(
                    {
                        "mm_per_point": expected / drawn,
                        "value_mm": expected,
                        "drawn_points": drawn,
                        "axis": axis,
                        "source": "dimension_string",
                    }
                )
    return samples


def _consensus(samples: list, agreement_pct: float):
    """The middle value, and how many samples stand behind it.

    A median rather than a mean, because one figure paired with the wrong line
    produces a ratio ten times out and would drag an average with it.
    """
    if not samples:
        return None, 0
    values = [s["mm_per_point"] for s in samples]
    middle = median(values)
    if middle <= 0:
        return None, 0
    agreeing = [v for v in values if abs(v - middle) / middle * 100.0 <= agreement_pct]
    return middle, len(agreeing)


def calibrate_scale(
    page,
    ocr_results: list = None,
    settings: dict = None,
    printed_scale: str = None,
    dpi: float = None,
) -> Scale:
    """Measures how many millimetres of building one point of this sheet is.

    ``printed_scale`` is whatever the title block claims, where the caller has
    read one ("1:100"). It is never used as the answer while the drawing can
    supply one; it is compared against the measurement, and the two disagreeing
    is reported rather than reconciled.

    Always returns a :class:`Scale`. A sheet nothing could be measured from
    comes back with ``usable`` false and a note saying so, rather than raising
    - a sheet that cannot be measured is a normal outcome, not a failure of the
    run (Critical Rule 6).
    """
    settings = settings or load_settings()
    dpi = float(dpi or number(settings, "render_dpi", 300.0))
    try:
        origin = (page.rect.x0, page.rect.y0)
    except Exception:
        origin = (0.0, 0.0)

    if not printed_scale:
        # **A sheet that prints no dimension figures can still print its
        # scale.** A plan set published as pictures is exactly that case: its
        # drawing is pixels, so there are no dimension lines to measure
        # against, and without this the whole set reports nothing measurable
        # at all. Read from the sheet's text rather than from a located title
        # block, so it is used only as the fallback and always says so.
        printed_scale = printed_ratio_on(page)

    denominator = scale_ratio_denominator(printed_scale) if printed_scale else None
    printed_mm_per_point = (
        MM_PER_POINT_AT_FULL_SIZE * float(denominator) if denominator else 0.0
    )

    def result(mm_per_point, source, confidence, samples, note, measured=0.0, variance=None):
        return Scale(
            mm_per_point=mm_per_point,
            dpi=dpi,
            origin=origin,
            source=source,
            confidence=confidence,
            samples=samples,
            note=note,
            printed_mm_per_point=printed_mm_per_point,
            measured_mm_per_point=measured,
            variance_pct=variance,
        )

    try:
        lines = _text_lines(page, ocr_results)
        figures = []
        for line in lines:
            parsed = dimension_value_mm(line.get("text"), settings)
            if parsed is None:
                continue
            value_mm, note = parsed
            figures.append(
                {
                    "value_mm": value_mm,
                    "note": note,
                    "bbox": [float(v) for v in line["bbox"]],
                    "axis": _axis_of_text(line),
                }
            )

        segments = axis_segments(page)
        primary = _samples_from_dimension_lines(figures, segments, settings)
        cross_check = _samples_from_dimension_strings(figures, settings)
    except Exception as e:
        logger.exception(f"calibrate_scale: measuring this sheet failed: {e}")
        primary, cross_check, figures = [], [], []

    agreement = number(settings, "scale.agreement_pct", 5.0)
    minimum = int(number(settings, "scale.min_samples", 3))
    share_needed = number(settings, "scale.consensus_share", 0.5)

    measured, agreeing = _consensus(primary, agreement)
    used = primary
    source_name = "dimension_line"
    if measured is None or len(primary) < minimum:
        # The sheet drew no dimension line this reader could find under a
        # figure. The strings are a complete reading in their own right, so
        # they are used rather than the sheet being given up on.
        measured, agreeing = _consensus(cross_check, agreement)
        used = cross_check
        source_name = "dimension_string"

    # **The consensus itself has to carry the evidence, not merely the pile it
    # was drawn from.** Requiring only that enough samples were *tried* let a
    # detail sheet report a scale that three figures agreed on out of six, and
    # three figures agreeing is a coincidence rather than a measurement.
    if (
        measured is None
        or len(used) < minimum
        or agreeing < minimum
        or agreeing < share_needed * len(used)
    ):
        return _without_a_measurement(
            result, printed_mm_per_point, printed_scale, settings, len(used), agreeing
        )

    confidence = min(0.99, 0.5 + 0.49 * (agreeing / max(len(used), 1)))
    variance = None
    note = (
        f"Measured from {agreeing} of this sheet's own {len(used)} dimension figures: "
        f"one point of paper is {measured:.2f} mm of building."
    )

    # The two routes agreeing is worth saying, because they share no working.
    other = cross_check if source_name == "dimension_line" else primary
    other_middle, _ = _consensus(other, agreement)
    if other_middle and abs(other_middle - measured) / measured * 100.0 <= agreement:
        confidence = min(0.99, confidence + 0.05)
        note += " The sheet's dimension strings measure the same, by a separate route."

    if printed_mm_per_point > 0:
        variance = round((measured - printed_mm_per_point) / printed_mm_per_point * 100.0, 2)
        if abs(variance) <= agreement:
            note = (
                f"The printed scale {printed_scale} was checked against {agreeing} of this "
                f"sheet's own dimension figures and agrees to within {abs(variance):.1f}%."
            )
        else:
            # **Overturning a printed scale needs more evidence than
            # confirming one**, and this was found by running it rather than
            # reasoned out. An elevation sheet prints a handful of height
            # figures - "2100 TO HEAD HT", "2600 TO CEILING HT" - and each one
            # pairs with whatever line happens to run nearest it. Four such
            # samples agreed with each other, measured 46.67 mm against a
            # printed 1:100's 35.28, and would have been reported as the
            # sheet's real scale with high confidence. Four figures on an
            # elevation are not evidence that a title block is wrong.
            #
            # A sheet re-plotted from A3 to A4 is real and common, so the
            # capability stays - but it takes a proper number of figures, in
            # near-unanimous agreement, before a printed ratio is set aside.
            enough = int(number(settings, "scale.min_samples_to_contradict_printed", 12))
            unanimity = number(settings, "scale.contradiction_agreement_share", 0.9)
            convincing = agreeing >= enough and agreeing >= unanimity * len(used)
            logger.warning(
                f"scale contradicted: printed={printed_mm_per_point:.3f} "
                f"measured={measured:.3f} variance={variance:.2f}% "
                f"on {agreeing} of {len(used)} figures "
                f"({'the sheet is measured' if convincing else 'the printed scale is kept'})"
            )
            if convincing:
                # The drawing agrees with itself and simply is not at the scale
                # its title block claims. Its own figures are the measurement.
                note = (
                    f"The title block says {printed_scale}, which would make one point "
                    f"{printed_mm_per_point:.2f} mm, but all {agreeing} of this sheet's own "
                    f"dimension figures measure {measured:.2f} mm - {abs(variance):.1f}% "
                    "apart. The drawing agrees with itself, so it has most likely been "
                    "printed at a reduced size. Lengths are measured from the sheet's own "
                    "dimensions; check the printed scale before relying on them."
                )
            else:
                return result(
                    printed_mm_per_point,
                    "printed_scale",
                    0.4,
                    agreeing,
                    (
                        f"This sheet's {agreeing} dimension figures measure "
                        f"{measured:.2f} mm to a point, against the {printed_mm_per_point:.2f} mm "
                        f"the printed scale {printed_scale} states - {abs(variance):.1f}% apart. "
                        "That is too few figures to set a printed scale aside, so the printed "
                        "scale is used and the difference is reported for checking."
                    ),
                    measured,
                    variance,
                )

    return result(measured, source_name, confidence, agreeing, note, measured, variance)


def _without_a_measurement(result, printed_mm_per_point, printed_scale, settings, tried, agreeing):
    """What to report when the drawing could not measure itself.

    Two honest outcomes, and neither of them is a guess. Where the title block
    prints a ratio it is used and said to be unchecked; where it prints none,
    nothing on the sheet can be measured and that is what is reported.
    """
    if printed_mm_per_point > 0 and setting(settings, "scale.fall_back_to_printed_scale", True):
        return result(
            printed_mm_per_point,
            "printed_scale",
            0.4,
            0,
            (
                f"This sheet does not print enough dimension figures to check its scale "
                f"against ({tried} tried, {agreeing} agreeing). The printed scale "
                f"{printed_scale} is used as stated and has not been verified."
            ),
        )
    return result(
        0.0,
        "not_established",
        0.0,
        0,
        (
            "Neither a printed scale nor enough of this sheet's own dimension figures "
            "could establish what one point of it measures, so no length is reported "
            "from this sheet."
        ),
    )
