"""Step 3 - the openings, found before the walls and masked out of them.

**Why openings come first.** A door is a hole in a wall: the wall's two faces
stop together for its width and start again on the other side. Step 4 closes
small gaps morphologically so that a wall broken by a crossing line reads as
one wall - and a door gap is exactly the size of gap that closing would bridge.
Bridge it and the wall is reported as continuous, the door is gone, and nothing
downstream can know it was ever there.

So the openings are found first and painted **white** onto the plan image
before any closing is done. The gap then survives, the wall reads as two
stretches with a hole between them, and the hole has a record.

Three readings of an opening, and every record says which one found it:

*   **The arc of a hinged door.** A swing is drawn from the hinge and its
    radius *is* the door leaf, so a plan carries its door widths in its
    geometry whether or not it prints a schedule. Recognised by shape - a
    curved path whose box is about as wide as it is tall - never by a
    particular size, because a size is a property of one office's doors.
*   **The mark printed beside it** - ``D01``, ``W12``, ``SD03``. The prefix
    says door or window and the whole mark keys it to a schedule row. The
    prefixes an office uses are configuration, not code (Critical Rule 1).
*   **The glazing drawn inside the wall.** A wall is solid, so nothing is drawn
    inside one. A window is not solid: between the wall's two faces the drawing
    puts the glass, the frame and the sashes, and how many lines are in there
    says which window it is.

**Hough circles are used, and on their own they are useless.** Asked to find
circles on a floor plan, ``cv2.HoughCircles`` proposes thousands - every basin,
every toilet pan, every cooktop burner, every letter O and the north point.
Measured on one sheet: **4,809 proposals**. What makes them usable is that a
proposal is not an answer. Each one is put back to the image as a question with
a checkable answer - *is there ink along this circle, and through how many
**unbroken** degrees?* A swing is a curve and sweeps one continuous quarter
turn; a basin sweeps the whole way round. Counting scattered ink that merely
adds up to a quarter read the four corners of a plain rectangular building as
door swings, painted all four out as openings, and left the building reporting
no walls at all.

So Hough is only ever a proposer here, only on a sheet whose own geometry holds
no arcs to read, and only on one whose drawing really is an embedded picture -
reading the page costs a render and a transform, and an elevation with no doors
on it should pay for neither.

**And it is proposed small and checked full size.** A transform over a 17
megapixel sheet searching radii of 71 to 283 pixels takes **280 seconds**, and
it is only ever guessing at where to look. Measured on the same sheet,
proposing at a quarter of the resolution takes **1.9 seconds** and every real
door still survives the check, because the check is done at full size where the
precision actually matters. The reduction comes from how many pixels a swing
needs to be recognisable as a circle at all, not from the render resolution, so
a 1:200 sheet - where a door is already small - is not reduced.
"""

import math
import re
import time
from dataclasses import dataclass, field

from app.logging_setup import get_logger
from pipeline.plan import textmodel
from pipeline.plan.cvdetect import imaging
from pipeline.plan.cvdetect.settings import number, setting

logger = get_logger()

# How many places round a proposed circle are sampled when asking whether ink
# is actually drawn along it. One sample per two degrees: fine enough that a
# quarter turn is 45 samples, coarse enough to cost nothing.
_SAMPLES_ROUND_A_CIRCLE = 180

# A swing is a part-circle. Anything sweeping more than this is a fitting drawn
# round - a basin, a pan, a burner - not a door.
_MOST_A_SWING_SWEEPS_DEGREES = 200.0


@dataclass
class Opening:
    """One door or window, and how the drawing said so."""

    opening_id: str
    kind: str
    bbox: list
    found_by: str
    confidence: float
    width_mm: float = None
    mark: str = None
    evidence: list = field(default_factory=list)
    wall_id: str = None

    def as_record(self) -> dict:
        return {
            "opening_id": self.opening_id,
            "element_type": self.kind,
            "bbox": [round(v, 2) for v in self.bbox],
            "width_mm": round(self.width_mm, 1) if self.width_mm else None,
            "mark": self.mark,
            "extraction_method": self.found_by,
            "confidence": round(self.confidence, 3),
            "evidence": list(self.evidence),
            "wall_id": self.wall_id,
        }


def detect_openings(page, scale, paths, settings: dict) -> list:
    """Every door and window this sheet states, by whichever reading found it.

    Never raises: a reading that fails is logged and the others still run, so a
    sheet with unreadable curves still reports its printed marks
    (Critical Rule 6).
    """
    openings = []
    for reading, name in (
        (_doors_from_vector_arcs, "door swings in the drawing's own curves"),
        (_openings_from_marks, "opening marks printed on the sheet"),
        (_windows_from_glazing, "glazing drawn inside a wall"),
    ):
        try:
            openings.extend(reading(page, scale, paths, settings))
        except Exception as e:
            logger.exception(f"detect_openings: {name} could not be read: {e}")

    # The page is only asked as a picture where its own geometry held no arcs.
    # Its curves are exact and nothing recovered from pixels can beat them.
    # **And only on a sheet whose drawing really is a picture.** Reading the
    # page costs a render and a Hough transform, and an elevation or a detail
    # sheet that simply has no doors on it should not pay for either. A sheet
    # whose drawing is an embedded image is what this search is for, and the
    # page says so directly for the price of one scan.
    if not any(o.found_by == "arc_geometry" for o in openings):
        if setting(settings, "openings.hough_when_no_vector_arcs", True) and imaging.page_is_a_picture(
            page, number(settings, "openings.picture_share_of_sheet", 0.1)
        ):
            try:
                openings.extend(_doors_from_the_page(page, scale, settings))
            except Exception as e:
                logger.exception(f"detect_openings: the page could not be read for swings: {e}")

    openings = _one_opening_per_place(openings, scale, settings)
    for number_in_order, opening in enumerate(openings, start=1):
        if not opening.opening_id:
            prefix = {"door": "D", "window": "W"}.get(opening.kind, "O")
            opening.opening_id = f"{prefix}{number_in_order:02d}"
    logger.info(
        f"openings: {len(openings)} found "
        f"({sum(1 for o in openings if o.kind == 'door')} doors, "
        f"{sum(1 for o in openings if o.kind == 'window')} windows)"
    )
    return openings


def _doors_from_vector_arcs(page, scale, paths, settings: dict) -> list:
    """Door swings read from the curves the drafter actually plotted.

    The test is shape, not size: a curved path whose bounding box is about as
    wide as it is tall is a quarter turn. One much wider than it is tall is a
    leader, a revision cloud or a piece of lettering set on a curve.
    """
    if not scale.usable:
        return []
    squareness = number(settings, "openings.arc_squareness", 1.6)
    smallest = number(settings, "openings.door_min_width_mm", 600.0)
    largest = number(settings, "openings.door_max_width_mm", 2400.0)

    found = []
    for curve in paths.curves:
        x0, y0, x1, y1 = curve["bbox"]
        width_pt, height_pt = abs(x1 - x0), abs(y1 - y0)
        if width_pt <= 0 or height_pt <= 0:
            continue
        if max(width_pt / height_pt, height_pt / width_pt) > squareness:
            continue
        # The leaf is the radius, which is the longer side of a quarter turn's
        # box - the box of a quarter circle is exactly the radius square.
        leaf_mm = max(width_pt, height_pt) * scale.mm_per_point
        if not (smallest <= leaf_mm <= largest):
            continue
        found.append(
            Opening(
                opening_id="",
                kind="door",
                bbox=[x0, y0, x1, y1],
                found_by="arc_geometry",
                confidence=0.8,
                width_mm=leaf_mm,
                evidence=[f"a door swing {leaf_mm:.0f} mm across is drawn here"],
            )
        )
    return found


def _openings_from_marks(page, scale, paths, settings: dict) -> list:
    """Openings from the codes printed beside them.

    The prefixes are read from the config, because which letters an office puts
    in front of its door and window numbers is exactly the sort of thing one
    office does differently from the next.
    """
    prefixes = setting(settings, "openings.mark_prefixes", {}) or {}
    by_prefix = {}
    for kind, entries in prefixes.items():
        for entry in entries or []:
            by_prefix[str(entry).strip().upper()] = kind
    if not by_prefix:
        return []

    longest = max(len(p) for p in by_prefix)
    pattern = re.compile(rf"^([A-Z]{{1,{longest}}})[\s-]?(\d{{1,3}})([A-Za-z])?$")

    try:
        lines = textmodel.extract_native_lines(page)
    except Exception as e:
        logger.exception(f"opening marks: this sheet's text could not be read: {e}")
        return []

    found = []
    for line in lines:
        text = (line.get("text") or "").strip().upper()
        match = pattern.match(text)
        if not match:
            continue
        kind = by_prefix.get(match.group(1))
        if not kind:
            continue
        found.append(
            Opening(
                opening_id="",
                kind=kind,
                bbox=[float(v) for v in line["bbox"]],
                found_by="printed_mark",
                confidence=0.7,
                mark=text.replace(" ", "").replace("-", ""),
                evidence=[f"the sheet prints the mark {text} here"],
            )
        )
    return found


def _windows_from_glazing(page, scale, paths, settings: dict) -> list:
    """Windows from the lines drawn between a wall's two faces.

    **A wall is solid, so nothing is drawn inside a wall.** A window is not:
    between the wall's two faces the drawing puts the glass, the frame and the
    sashes. So what is looked for is a run of parallel lines whose **outermost
    pair is a wall thickness apart** - that pair is the wall - with one or more
    further lines lying strictly *between* them, which is the glazing.

    **The outer pair is the whole test, and leaving it out was a real defect.**
    Without it, "two parallel lines closer together than a wall is thick" is
    the definition of a wall's own two faces, so every wall on the sheet was
    reported as a window: 112 of them on one floor plan that has about thirty.
    A wall with nothing drawn inside it is a wall.
    """
    if not scale.usable:
        return []
    fewest = int(number(settings, "openings.glazing_min_lines", 2))
    most = int(number(settings, "openings.glazing_max_lines", 6))
    narrowest = number(settings, "openings.window_min_width_mm", 300.0)
    widest = number(settings, "openings.window_max_width_mm", 6000.0)
    thinnest_wall_pt = number(settings, "wall.min_thickness_mm", 70.0) / scale.mm_per_point
    thickest_wall_pt = number(settings, "wall.max_thickness_mm", 320.0) / scale.mm_per_point

    found = []
    for axis in ("h", "v"):
        runs = _parallel_runs(paths.structural, axis, thickest_wall_pt, scale, narrowest, widest)
        for run in runs:
            positions = [r[0] for r in run]
            starts = [r[1] for r in run]
            ends = [r[2] for r in run]

            # The outermost pair has to be a wall, or this is not a window in
            # a wall - it is a wall, or a run of joinery lines.
            across = max(positions) - min(positions)
            if not (thinnest_wall_pt <= across <= thickest_wall_pt):
                continue
            inside = len(run) - 2
            if not (fewest <= inside <= most):
                continue

            span_mm = (max(ends) - min(starts)) * scale.mm_per_point
            if not (narrowest <= span_mm <= widest):
                continue
            if axis == "h":
                bbox = [min(starts), min(positions), max(ends), max(positions)]
            else:
                bbox = [min(positions), min(starts), max(positions), max(ends)]
            found.append(
                Opening(
                    opening_id="",
                    kind="window",
                    bbox=bbox,
                    found_by="glazing_geometry",
                    confidence=0.65,
                    width_mm=span_mm,
                    evidence=[
                        f"{inside} line(s) are drawn inside a "
                        f"{across * scale.mm_per_point:.0f} mm wall over {span_mm:.0f} mm, "
                        "which is glazing - a wall is solid"
                    ],
                )
            )
    return found


def _parallel_runs(segments, axis: str, within_pt: float, scale, narrowest, widest) -> list:
    """Groups of parallel segments that run together and sit close across.

    Each entry is ``(position, start, end)`` in the page's own space.
    """
    lines = []
    for segment in segments:
        dx, dy = abs(segment.x1 - segment.x0), abs(segment.y1 - segment.y0)
        if axis == "h" and dy <= 0.35 and dx > 0:
            lines.append(((segment.y0 + segment.y1) / 2.0, min(segment.x0, segment.x1),
                          max(segment.x0, segment.x1)))
        elif axis == "v" and dx <= 0.35 and dy > 0:
            lines.append(((segment.x0 + segment.x1) / 2.0, min(segment.y0, segment.y1),
                          max(segment.y0, segment.y1)))
    if not lines:
        return []

    narrowest_pt = narrowest / scale.mm_per_point
    widest_pt = widest / scale.mm_per_point
    lines = [line for line in lines if narrowest_pt <= (line[2] - line[1]) <= widest_pt]
    lines.sort()

    runs, current = [], []
    for line in lines:
        if not current:
            current = [line]
            continue
        if line[0] - current[-1][0] > within_pt:
            runs.append(current)
            current = [line]
            continue
        # They have to run together as well as sit close, or a line on the far
        # side of the room joins the group.
        overlap = min(line[2], current[-1][2]) - max(line[1], current[-1][1])
        shorter = min(line[2] - line[1], current[-1][2] - current[-1][1])
        if shorter <= 0 or overlap / shorter < 0.6:
            runs.append(current)
            current = [line]
            continue
        current.append(line)
    if current:
        runs.append(current)
    return runs


def _doors_from_the_page(page, scale, settings: dict) -> list:
    """Door swings proposed by a Hough transform and then actually checked.

    Only ever reached on a sheet whose own geometry holds no arcs - a plan set
    published as pictures. See the module docstring for why the proposal is not
    the answer.
    """
    if not scale.usable:
        return []
    try:
        import cv2
        import numpy as np
    except ImportError:
        logger.warning("OpenCV is not installed, so a sheet drawn as pictures cannot be read")
        return []

    # **The page itself, never the image the walls are drawn from.** On a sheet
    # published as pictures the plan image is drawn from the handful of vector
    # paths the file carries - its frame and title block - and there is not a
    # single door in it. This search exists precisely for the pixels.
    ink = imaging.ink_from_page(page, scale)

    smallest_px = int(round(scale.px_from_mm(number(settings, "openings.door_min_width_mm", 600.0))))
    largest_px = int(round(scale.px_from_mm(number(settings, "openings.door_max_width_mm", 2400.0))))
    if smallest_px < 2 or largest_px <= smallest_px:
        return []

    # **Proposed small, checked full size.** A Hough transform over a 17
    # megapixel sheet, searching radii from 71 to 283 pixels, takes **280
    # seconds** - and it is only ever guessing at where to look. Measured on the
    # same sheet, proposing at a quarter of the resolution takes **1.9 seconds**
    # and every real door still survives the check, because the check is done
    # at full size where the precision actually matters. The reduction is
    # computed from how many pixels a swing needs to be recognisable as a
    # circle at all, not from the render resolution, so a 1:200 sheet - where a
    # door is already small - is not reduced.
    wanted_radius = max(4.0, number(settings, "openings.hough_proposal_radius_px", 18.0))
    divisor = max(1, int(smallest_px // wanted_radius))
    height, width = ink.shape[:2]
    if divisor > 1:
        proposal_image = cv2.resize(
            ink, (width // divisor, height // divisor), interpolation=cv2.INTER_AREA
        )
        proposal_image = ((proposal_image > 0).astype(np.uint8)) * 255
    else:
        proposal_image = ink
    proposal_smallest = max(2, smallest_px // divisor)
    proposal_largest = max(proposal_smallest + 1, largest_px // divisor)

    started = time.perf_counter()
    proposals = cv2.HoughCircles(
        proposal_image,
        cv2.HOUGH_GRADIENT,
        dp=number(settings, "openings.hough_accumulator_ratio", 1.5),
        minDist=max(2, proposal_smallest // 2),
        param1=100,
        param2=max(20, proposal_smallest // 3),
        minRadius=proposal_smallest,
        maxRadius=proposal_largest,
    )
    if proposals is None:
        return []
    proposals = np.uint16(np.around(proposals))[0]
    logger.info(
        f"openings: {len(proposals)} circles proposed at 1/{divisor} of the render in "
        f"{time.perf_counter() - started:.1f}s; each is now checked at full size"
    )

    least_sweep = number(settings, "openings.arc_min_sweep_degrees", 55.0)
    found = []
    for proposed_x, proposed_y, proposed_radius in proposals:
        centre_x = int(proposed_x) * divisor
        centre_y = int(proposed_y) * divisor
        radius = int(proposed_radius) * divisor
        sweep = _sweep_of_ink(ink, centre_x, centre_y, radius, height, width)
        if not (least_sweep <= sweep <= _MOST_A_SWING_SWEEPS_DEGREES):
            continue
        leaf_mm = scale.mm_from_px(float(radius))
        x0, y0 = scale.pixel_to_point(float(centre_x - radius), float(centre_y - radius))
        x1, y1 = scale.pixel_to_point(float(centre_x + radius), float(centre_y + radius))
        found.append(
            Opening(
                opening_id="",
                kind="door",
                bbox=[x0, y0, x1, y1],
                found_by="arc_in_the_page_image",
                confidence=0.6,
                width_mm=leaf_mm,
                evidence=[
                    f"an arc {leaf_mm:.0f} mm across sweeping {sweep:.0f} degrees is drawn "
                    "here on the page"
                ],
            )
        )
    logger.info(f"openings: {len(found)} of those proposals carry a real door swing")
    return found


def _sweep_of_ink(ink, centre_x: int, centre_y: int, radius: int, height: int, width: int) -> float:
    """The longest **unbroken** run of ink around a proposed circle, in degrees.

    This is the whole reason a Hough proposal can be trusted at all. A basin
    drawn round sweeps the full turn; a letter O sweeps the full turn; a door
    swing sweeps one continuous quarter.

    **Unbroken, and that word cost a building.** Counting the total ink found
    anywhere around the circle reported the **four corners of a plain
    rectangular building** as door swings - each corner's two walls cross the
    circle in several places, and the pieces added up to more than a quarter
    turn. Every one was then painted out as an opening and the building
    reported no walls at all. A swing is a curve: its ink is one continuous run
    at a constant distance from the hinge. Scattered crossings that happen to
    total the same are two straight lines.

    Sampled with a one-pixel allowance either side of the radius, because a
    circle fitted to a plotted arc is never exactly on it.
    """
    import numpy as np

    # **Asked of every proposal, so it is asked in numpy.** Written as Python
    # loops this is 180 samples times three allowances times 3,600 proposals -
    # about two million interpreter-level array lookups on one sheet, and it
    # cost more than the Hough transform it was checking.
    angles = _angles()
    on = np.zeros(_SAMPLES_ROUND_A_CIRCLE, dtype=bool)
    for slack in (-1, 0, 1):
        xs = np.rint(centre_x + (radius + slack) * np.cos(angles)).astype(np.int64)
        ys = np.rint(centre_y + (radius + slack) * np.sin(angles)).astype(np.int64)
        inside = (xs >= 0) & (xs < width) & (ys >= 0) & (ys < height)
        if not inside.any():
            continue
        hit = np.zeros(_SAMPLES_ROUND_A_CIRCLE, dtype=bool)
        hit[inside] = ink[ys[inside], xs[inside]] > 0
        on |= hit

    if on.all():
        return 360.0
    if not on.any():
        return 0.0
    # The circle wraps, so the run may straddle the start. Rolling round to a
    # gap first makes one pass enough.
    rolled = np.roll(on, -int(np.argmin(on)))
    longest = run = 0
    for lit in rolled:
        run = run + 1 if lit else 0
        longest = max(longest, run)
    return 360.0 * longest / _SAMPLES_ROUND_A_CIRCLE


_ANGLES = None


def _angles():
    """The sample angles round a circle, worked out once rather than per call."""
    global _ANGLES
    if _ANGLES is None:
        import numpy as np

        _ANGLES = np.linspace(
            0.0, 2.0 * math.pi, _SAMPLES_ROUND_A_CIRCLE, endpoint=False
        )
    return _ANGLES


def _one_opening_per_place(openings: list, scale, settings: dict) -> list:
    """Two readings of the same opening are one opening.

    A door's swing and the ``D07`` printed beside it are the same door, and
    reporting both would double the count. Where two readings claim the same
    place, the stronger reading is kept and the weaker is recorded on it as
    evidence, so nothing is thrown away.
    """
    if len(openings) < 2:
        return openings

    # Which reading wins when two disagree about what an opening is. A drawn
    # symbol outranks a printed label: the symbol is the thing itself, drawn to
    # size, while a label is a reference to a schedule row that may have been
    # typed against the wrong mark.
    rank = {
        "arc_geometry": 4,
        "glazing_geometry": 3,
        "arc_in_the_page_image": 2,
        "printed_mark": 1,
    }
    reach = number(settings, "openings.mask_padding_mm", 40.0)
    reach_pt = reach / scale.mm_per_point if scale.usable else reach

    ordered = sorted(openings, key=lambda o: -rank.get(o.found_by, 0))
    kept = []
    for opening in ordered:
        near = None
        for other in kept:
            if _boxes_meet(opening.bbox, other.bbox, reach_pt):
                near = other
                break
        if near is None:
            kept.append(opening)
            continue
        near.evidence.extend(opening.evidence)
        near.confidence = min(0.95, near.confidence + 0.1)
        if opening.mark and not near.mark:
            near.mark = opening.mark
            near.opening_id = opening.mark
    return kept


def _boxes_meet(first: list, second: list, slack: float) -> bool:
    return not (
        first[2] + slack < second[0]
        or second[2] + slack < first[0]
        or first[3] + slack < second[1]
        or second[3] + slack < first[1]
    )


def openings_mask(page, scale, openings: list, settings: dict):
    """The image Step 4 paints white before it closes any gaps.

    Padded by a configured allowance in millimetres of building, because a
    swing's box stops at the leaf and the jamb is a little beyond it.
    """
    padding_px = scale.px_from_mm(number(settings, "openings.mask_padding_mm", 40.0))
    return imaging.draw_mask(page, scale, [o.bbox for o in openings], padding_px)
