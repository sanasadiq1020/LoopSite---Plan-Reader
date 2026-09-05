"""The computer-vision reader, in the shape the rest of the pipeline speaks.

``pipeline.plan.cvdetect`` measures walls a different way from
``pipeline.plan.walls``: it closes the drawing into solid bands, measures each
band's thickness with a distance transform and reduces it to a centreline by
skeletonisation, so **a wall is reported once** rather than once per pair of
drawn faces. What it hands back is a centreline and a thickness.

Everything downstream of wall detection - the junction graph, outside versus
inside, detached structures, the marked-up sheet, the opening evidence reader,
``walls.csv``, ``wall_graph.json`` and the 3D model - was written against the
record ``walls.py`` produces. That record is not an accident of that module; it
is the pipeline's canonical wall, and it carries things the centreline alone
does not: which faces the wall was measured from, where it runs, and **where it
is broken**.

So this module is an adapter, not a second pipeline. It runs the
computer-vision reader for the geometry and then hands the result through the
**same** post-processing ``walls.py`` already applies - the junctions, the free
tails, the detached structures, the outer/inner classification and the
plain-words description are the identical functions, imported and reused. That
way the new reader changes how a wall is *measured* and nothing at all about
how it is reported, checked or drawn.

**The one thing that has to be rebuilt is the breaks, and it is exact.** The
computer-vision reader paints every opening white *before* it closes the
drawing (that is the whole reason it finds openings first: closing a gap the
width of a wall would seal a door shut). So a wall with a door in it does not
arrive as one wall with a gap - it arrives as **two collinear pieces of the
same thickness with a space between them**, and that space is precisely where
the opening was. Putting those pieces back together, and recording the space as
a break, reconstructs the canonical record exactly. Nothing is invented: the
gap was measured, it was simply expressed the other way round.

Whether the reader is used at all is a setting - ``walls.reader`` in
``config/wall_config.json`` - so it can be turned off on a deployed server
without a rebuild, and so the two readers can be compared on the same plan.
"""

import math

from app.logging_setup import get_logger
from pipeline.plan import walls as legacy
from pipeline.plan.cvdetect import junctions as cv_junctions
from pipeline.plan.cvdetect import openings as cv_openings
from pipeline.plan.cvdetect import settings as cv_settings
from pipeline.plan.cvdetect import vectorpaths, wallgeometry
from pipeline.plan.cvdetect.settings import Scale, setting

logger = get_logger()

# A centreline is simplified before it gets here, so a jog smaller than the
# wall's own thickness is drafting noise rather than a change of direction.
# Expressed as a share of the thickness, so it means the same at any scale.
_JOG_SHARE_OF_THICKNESS = 1.0


def reader_name(config: dict) -> str:
    """Which wall reader this run should use: "cvdetect" or "legacy"."""
    try:
        return str((config.get("walls") or {}).get("reader", "cvdetect")).strip().lower()
    except AttributeError:
        return "cvdetect"


def detect_walls(
    rulings: dict,
    calibration: dict,
    config: dict,
    sheet_id: str,
    exclude_region=None,
    page=None,
    sheet_span_mm=None,
    page_number=None,
    text_boxes=None,
    rooms=None,
) -> list:
    """Candidate walls for one sheet, measured by the computer-vision reader.

    Deliberately the same signature as ``walls.detect_walls``, so the
    orchestrator chooses a reader rather than being rewritten around one.

    Returns an empty list when the sheet's scale could not be confirmed - a
    length that cannot be trusted is worse than no length at all - and when the
    page itself is not available, because this reader measures the page rather
    than a list of ruling lines.
    """
    if not calibration.get("usable_for_measurement"):
        return []
    mm_per_point = calibration.get("measured_mm_per_point") or calibration.get(
        "printed_mm_per_point"
    )
    if not mm_per_point or page is None:
        return []

    try:
        detection = cv_settings.load_settings()
        # **The scale the pipeline already established, not a second opinion.**
        # ``scale.py`` has verified this sheet's scale against its own dimension
        # strings and the run reports that figure everywhere. Letting the
        # computer-vision reader measure its own would give one sheet two
        # scales that could disagree, and every length would then depend on
        # which module a reader happened to be looking at (Critical Rule 2).
        scale = Scale(
            mm_per_point=float(mm_per_point),
            dpi=cv_settings.number(detection, "render_dpi", 300.0),
            origin=(page.rect.x0, page.rect.y0),
            source="pipeline_scale_check",
            confidence=1.0,
            note="Measured by the sheet's own scale check.",
        )

        paths = vectorpaths.parse_paths(page, detection)
        found_openings = cv_openings.detect_openings(page, scale, paths, detection)
        mask = cv_openings.openings_mask(page, scale, found_openings, detection)
        measured, diagnostics = wallgeometry.detect_walls(
            page, scale, paths, detection,
            openings_mask=mask, sheet_name=sheet_id,
        )
    except Exception as e:
        # A reader that cannot run must not take the sheet down with it: the
        # rooms, dimensions and schedules on it are still worth reporting
        # (Critical Rule 6).
        logger.exception(f"{sheet_id}: the computer-vision wall reader failed: {e}")
        return []

    spans = []
    for wall in measured:
        spans.extend(_straight_spans(wall, scale))
    if not spans:
        logger.info(f"{sheet_id}: the computer-vision reader traced no wall on this sheet")
        return []

    walls = _rejoin_across_openings(spans, mm_per_point, config)
    # **The gaps come from the faces, not from the shape of the band.** The
    # morphology cannot be relied on to leave a doorway open, so the breaks are
    # read off the two drawn faces before anything is closed and written
    # straight onto the wall that runs along the same line.
    _inject_face_gaps(walls, diagnostics.get("face_pairs") or [], mm_per_point, config)
    # **What the drawing says outright, punched where it says it.** A swing
    # arc, a window symbol and a printed mark each name an opening and where
    # it is; waiting for the closing to leave a hole there throws that away.
    _punch_openings_onto_walls(walls, found_openings, scale, mm_per_point, config)
    _fill_in_thickness_context(walls, config)

    line_source = (
        "cv_vector" if diagnostics.get("line_source") == "vector_paths" else "cv_raster"
    )
    _through_the_same_post_processing(
        walls, mm_per_point, config, sheet_id, page_number, rooms, line_source
    )
    logger.info(
        f"{sheet_id}: computer-vision reader gave {len(measured)} centrelines -> "
        f"{len(walls)} walls ({sum(len(w['gaps_pt']) for w in walls)} breaks), "
        f"from {line_source} lines"
    )
    return walls


# --- a centreline is a polyline; a canonical wall runs one way -------------


def _straight_spans(wall, scale) -> list:
    """One centreline, cut into the straight axis-aligned runs it is made of.

    The canonical record says a wall ``runs_along`` x or y, because that is
    what every later stage measures against - a junction is where one wall's
    band crosses another's run, and neither means anything for a diagonal. A
    skeleton, though, comes back as a polyline: a wall traced round a corner is
    one line with a bend in it.

    So the polyline is walked and cut wherever it genuinely changes direction.
    A jog smaller than the wall's own thickness is not a change of direction -
    it is where the skeleton wandered inside the band it was traced from - so
    those are absorbed rather than becoming two walls with a step between them.
    """
    try:
        points = list(wall.centreline.coords)
    except Exception:
        return []
    if len(points) < 2:
        return []

    thickness_pt = max(wall.thickness_mm / scale.mm_per_point, 0.1)
    jog = thickness_pt * _JOG_SHARE_OF_THICKNESS

    runs, current, axis = [], [points[0]], None
    for previous, point in zip(points, points[1:]):
        dx, dy = abs(point[0] - previous[0]), abs(point[1] - previous[1])
        if max(dx, dy) <= jog:
            # Too short to say which way it goes; it belongs to whatever run
            # it is in the middle of.
            current.append(point)
            continue
        step_axis = "x" if dx >= dy else "y"
        if axis is None or step_axis == axis:
            axis = step_axis
            current.append(point)
            continue
        runs.append((axis, current))
        current, axis = [previous, point], step_axis
    if axis is not None and len(current) >= 2:
        runs.append((axis, current))

    spans = []
    for run_axis, run_points in runs:
        span = _span_of(run_axis, run_points, wall, scale, thickness_pt)
        if span is not None:
            spans.append(span)
    return spans


def _span_of(axis: str, points: list, wall, scale, thickness_pt: float):
    """One straight run as a canonical wall record, before it is rejoined."""
    along = 0 if axis == "x" else 1
    across = 1 - along
    start = min(p[along] for p in points)
    end = max(p[along] for p in points)
    if end - start <= 0:
        return None
    # The position across the wall is the run's own average: a skeleton wanders
    # by a fraction of a pixel and the average is the line it was traced from.
    position = sum(p[across] for p in points) / len(points)

    if axis == "x":
        start_point = [start, position]
        end_point = [end, position]
    else:
        start_point = [position, start]
        end_point = [position, end]

    return {
        "runs_along": axis,
        "position_pt": position,
        "start_pt": start,
        "end_pt": end,
        "thickness_mm": wall.thickness_mm,
        "length_mm": (end - start) * scale.mm_per_point,
        "start_point_pt": [round(v, 2) for v in start_point],
        "end_point_pt": [round(v, 2) for v in end_point],
        "face_positions_pt": [
            round(position - thickness_pt / 2.0, 2),
            round(position + thickness_pt / 2.0, 2),
        ],
        "confidence": wall.confidence,
        "measured_from": wall.extraction_method,
        "drawn_as": wall.drawn_as,
    }


# --- the breaks, put back the way the pipeline expresses them --------------


def _rejoin_across_openings(spans: list, mm_per_point: float, config: dict) -> list:
    """Collinear pieces of one wall, put back together with their breaks.

    **This is the inverse of Step 3 of the computer-vision reader.** That step
    paints every opening white before the drawing is closed, so a wall with a
    door in it arrives here as two collinear pieces of the same thickness with
    a space between them. The space is exactly where the opening was, measured
    off the drawing rather than assumed - so joining the pieces and recording
    the space as a break restores the canonical record without inventing
    anything.

    It matters because the opening evidence reader works from those breaks: a
    break is a *candidate* opening, which the drawing then has to confirm four
    other ways. Without them, a plan set that prints no opening marks would
    report no openings at all.

    Two pieces are the same wall when they lie on the same line, measure the
    same thickness, and the space between them is no wider than an opening. The
    thickness test is the one that has to be there: a 90 mm partition and a
    230 mm external wall running along the same line are two different walls,
    and merging them would report a break where the building simply changes.
    """
    settings = config.get("walls", {})
    detection = cv_settings.load_settings()
    widest_opening_mm = float(settings.get("face_join_gap_mm", 6000))
    narrowest_break_mm = float(settings.get("min_opening_width_mm", 300))

    # **A centreline is not a drawn face, and it cannot be held to a drawn
    # face's tolerance.** ``collinear_tolerance_points`` is 0.6 pt because a
    # face is exact: the office plotted it, and two pieces of it are on the
    # same line to within the line's own weight. A centreline is *derived* -
    # it sits down the middle of a closed band, so it moves whenever the band's
    # width changes, which it does wherever a lining, a hatch boundary or a
    # return is picked up on one side of a doorway and not the other.
    #
    # Measured on one real floor plan: of the 72 collinear pairs that have a
    # plausible opening between them, the two centrelines sit up to **2.6 pt**
    # apart and their thicknesses differ by up to **102 mm**. Held to 0.6 pt
    # and 12 mm, only 4 breaks were recovered from a plan that has dozens.
    #
    # So both tolerances are a share of the wall's **own thickness**, which is
    # scale-free and physically right: a wall's thickness is the smallest
    # distance that can separate two different parallel walls, and two walls of
    # a building are a room apart, not half a thickness.
    position_share = cv_settings.number(
        detection, "wall.centreline_tolerance_share_of_thickness", 0.5
    )
    thickness_share = cv_settings.number(detection, "wall.thickness_agreement_share", 0.5)

    def same_line(first: dict, second: dict) -> bool:
        thickness = max(min(first["thickness_mm"], second["thickness_mm"]), 1.0)
        allowed_pt = (thickness * position_share) / mm_per_point
        if abs(first["position_pt"] - second["position_pt"]) > max(allowed_pt, 0.6):
            return False
        return abs(first["thickness_mm"] - second["thickness_mm"]) <= thickness * thickness_share

    # **Clustered, not bucketed, and that distinction cost most of the breaks.**
    # Rounding a position onto a grid of the tolerance puts two pieces of one
    # wall in different buckets whenever they happen to straddle a grid line -
    # 0.29 rounds to 0 and 0.31 to 1, though they are 0.02 apart. Measured on a
    # real 23-sheet set, bucketing recovered **11 breaks where the drawing has
    # 87**, and every break lost is an opening the reader can no longer see.
    # The same applies to thickness: one wall measured at 88 mm and 95 mm along
    # its length is one wall.
    groups = []
    for axis in ("x", "y"):
        along_axis = sorted(
            (s for s in spans if s["runs_along"] == axis),
            key=lambda s: (s["position_pt"], s["thickness_mm"]),
        )
        current = []
        for span in along_axis:
            if current and same_line(current[-1], span):
                current.append(span)
                continue
            if current:
                groups.append(current)
            current = [span]
        if current:
            groups.append(current)

    walls = []
    for members in groups:
        members.sort(key=lambda s: s["start_pt"])
        run = [members[0]]
        for span in members[1:]:
            gap_mm = (span["start_pt"] - run[-1]["end_pt"]) * mm_per_point
            if gap_mm <= widest_opening_mm:
                run.append(span)
                continue
            walls.append(_one_wall(run, mm_per_point, narrowest_break_mm))
            run = [span]
        walls.append(_one_wall(run, mm_per_point, narrowest_break_mm))
    return walls


def _one_wall(run: list, mm_per_point: float, narrowest_break_mm: float) -> dict:
    """One canonical wall from the collinear pieces that make it up."""
    axis = run[0]["runs_along"]
    start = min(piece["start_pt"] for piece in run)
    end = max(piece["end_pt"] for piece in run)
    position = sum(piece["position_pt"] for piece in run) / len(run)
    thickness_mm = sum(piece["thickness_mm"] for piece in run) / len(run)
    half = (thickness_mm / mm_per_point) / 2.0

    gaps = []
    for before, after in zip(run, run[1:]):
        gap_mm = (after["start_pt"] - before["end_pt"]) * mm_per_point
        # A space narrower than the smallest opening the office builds is where
        # the tracing lost the line, not a hole in the wall.
        if gap_mm >= narrowest_break_mm:
            gaps.append([round(before["end_pt"], 2), round(after["start_pt"], 2)])

    if axis == "x":
        start_point, end_point = [start, position], [end, position]
        bbox = [round(start, 2), round(position - half, 2), round(end, 2), round(position + half, 2)]
    else:
        start_point, end_point = [position, start], [position, end]
        bbox = [round(position - half, 2), round(start, 2), round(position + half, 2), round(end, 2)]

    confidence = min(piece["confidence"] for piece in run)
    return {
        "wall_id": "",
        "runs_along": axis,
        "length_mm": (end - start) * mm_per_point,
        "thickness_mm": thickness_mm,
        "nominal_thickness_mm": None,
        "thickness_difference_mm": None,
        "matches_nominal_thickness": False,
        "start_point_pt": [round(v, 2) for v in start_point],
        "end_point_pt": [round(v, 2) for v in end_point],
        "face_positions_pt": [round(position - half, 2), round(position + half, 2)],
        "bbox": bbox,
        "line_source": "",
        "confidence": round(confidence, 3),
        "confidence_band": "high" if confidence >= 0.75 else "review",
        "review_status": "needs_review",
        "merged_from": len(run),
        "meets_another_wall": True,
        "linked_opening_marks": [],
        "gaps_pt": gaps,
        "thickness_is_assumed": False,
        "found_on_a_second_look": False,
        "drawn_dashed": False,
        "stroke_pt": 0.0,
        "interior_drawn_as": run[0].get("drawn_as", "outline"),
    }


def sever_at_openings(walls: list, mm_per_point: float, config: dict,
                      openings: list = None) -> list:
    """Cuts every wall at the openings it carries, into the stretches either side.

    **This is a rendering and export step, not a reading step**, and that
    separation is the point. The record the pipeline carries keeps each parent
    wall whole with its ``gaps_pt``, because the opening reader, the schedule
    reconciliation and the take-off all read exactly that list - severing
    during extraction removed it and openings fell from 121 to 102 on one plan
    set and from 9 to 0 on another. What a *drawing* should show is different:
    a wall with a door in it is two stretches, not one line with a hole
    annotated on it. So the severing happens here, on the way to the picture
    and to the vector export, and the reading is untouched.

    **A door is not a hole in a wall; it is where the wall stops and starts
    again.** Tracing a wall straight through its own doorway and cutting a hole
    afterwards asks the morphology to leave a gap it has every reason to close -
    it joins the wall to the jambs, the leaf and the swing arc drawn inside the
    opening. Treating the opening as a boundary instead makes the wall's own
    geometry say where it ends.

    Each severed stretch keeps the thickness, the faces and the band of the
    wall it came from, and records that it terminates at an opening. What is
    lost is the wall record's ``gaps_pt``, and that matters: the opening reader
    downstream reads exactly that list to find candidates. So this is a real
    trade and it is measured rather than assumed - see the note in
    ``config/cv_detection.json``.

    A stretch shorter than the office's own shortest wall is not kept: a nib
    between two doors is real, a two-millimetre sliver is the tracing.

    **Every opening, not only the ones the faces disagreed about.** ``gaps_pt``
    holds the breaks the wall's own two faces were measured to have. That is one
    of four ways this reader finds an opening: a door's swing, a window drawn
    inside the wall and a printed mark reconciled against a schedule all place
    an opening on a wall that has no measured gap there - a vector sheet finds
    most of its openings that way. Severing on ``gaps_pt`` alone therefore drew
    the centreline straight through 13 of 53 openings, and every one of them was
    a door or a window the reading had already found and placed. So the openings
    placed on each wall are cut as well, at the very span the picture draws them
    at, which is what makes the drawing and the reading say the same thing.
    """
    settings = config.get("walls", {})
    shortest = float(settings.get("min_wall_length_mm", 900)) / mm_per_point
    placed = _opening_spans(openings, walls)

    severed, cut = [], 0
    for wall in walls:
        along = 0 if wall["runs_along"] == "x" else 1
        run_low = min(wall["start_point_pt"][along], wall["end_point_pt"][along])
        run_high = max(wall["start_point_pt"][along], wall["end_point_pt"][along])
        gaps = _spans_to_cut(wall, placed, along, run_low, run_high)
        if not gaps:
            severed.append(wall)
            continue

        edges = [run_low]
        for low, high in gaps:
            edges.extend([low, high])
        edges.append(run_high)

        pieces = []
        for start, end in zip(edges[0::2], edges[1::2]):
            if end - start < shortest:
                continue
            pieces.append(_stretch_of(wall, start, end, mm_per_point))
        if not pieces:
            # Every stretch was too short to be a wall, so nothing here was one.
            continue
        cut += len(pieces) - 1
        severed.extend(pieces)

    if cut:
        logger.info(f"walls: {cut} centreline(s) severed at an opening jamb")
    return severed


def _opening_spans(openings, walls: list) -> list:
    """Every opening on the sheet as (axis, box), ready to cut whatever it crosses.

    The axis is the host wall's, where the opening was placed on one, and
    otherwise the longer side of the opening's own box - a door is drawn wider
    across the wall than through it.
    """
    hosts = {wall.get("wall_id"): wall for wall in walls if wall.get("wall_id")}
    spans = []
    for opening in openings or []:
        host = hosts.get(opening.get("wall_id"))
        box = opening_span(opening, host)
        if box is None:
            continue
        if host is not None and host.get("runs_along") in ("x", "y"):
            along = 0 if host["runs_along"] == "x" else 1
        else:
            along = 0 if (box[2] - box[0]) >= (box[3] - box[1]) else 1
        spans.append((along, box))
    return spans


def opening_span(opening: dict, host) -> list:
    """The stretch of wall one opening occupies, in points, or None.

    **Where it sits on its wall, not where its mark is printed.** A mark is
    printed beside its opening - commonly inside the room on a leader - so an
    opening read from one carries the mark's own little box as its
    ``source_bbox``, seven points square and clear of the wall. Cutting or
    drawing there puts the doorway where the label is: measured, five doors on
    one floor plan were drawn as holes floating below the wall they belong to,
    with the wall itself running unbroken behind them.

    So the placement the reader worked out is used first - the fractions of the
    way along its wall that the opening starts and ends, which is the same
    record the 3D model is cut from (Critical Rule 2) - and the box is used only
    where there is no placement, which is the case for an opening measured
    directly from the break in the wall.
    """
    where = opening.get("position_on_wall") or {}
    start, end = where.get("start_fraction"), where.get("end_fraction")
    if host is not None and start is not None and end is not None:
        try:
            along = 0 if host["runs_along"] == "x" else 1
            run_low = min(host["start_point_pt"][along], host["end_point_pt"][along])
            run_high = max(host["start_point_pt"][along], host["end_point_pt"][along])
            stretch = run_high - run_low
            low = run_low + float(start) * stretch
            high = run_low + float(end) * stretch
            band_low, band_high = sorted(host.get("face_positions_pt") or [0.0, 0.0])
            if high > low and band_high > band_low:
                if along == 0:
                    return [low, band_low, high, band_high]
                return [band_low, low, band_high, high]
        except (KeyError, TypeError, ValueError, IndexError):
            pass

    box = opening.get("source_bbox")
    if box and len(box) == 4:
        return [float(value) for value in box]
    return None


def _spans_to_cut(wall: dict, openings: list, along: int,
                  run_low: float, run_high: float) -> list:
    """Where this wall is to be cut: its measured gaps, and every opening it passes through.

    An opening's span is taken from the box the overlay draws it at, so the cut
    and the magenta rectangle over it are the same stretch of paper - a hole
    drawn in one place and cut in another would be worse than either.

    **A doorway is a hole in that place, not a hole in one record.** An opening
    is placed on the one wall it was matched to, and a plan commonly traces more
    than one candidate along the same line - a brick skin and a frame, or the
    sashes drawn inside the opening itself. Cutting only the wall an opening
    names left the others drawn straight across their own doorway: measured, two
    openings on two sheets, one of them a candidate lying *entirely* inside a
    doorway, which is a sliding door's own leaf and not a wall at all. So an
    opening cuts everything running its way whose thickness band it overlaps.
    Nothing perpendicular is touched, because such a wall is not running the
    opening's way.

    Overlapping spans are merged, because two readings of one door - the swing
    and the break it sits in - are one doorway and must not cut a sliver of wall
    out between them.
    """
    across = 1 - along
    band_low, band_high = sorted(wall.get("face_positions_pt") or [0.0, 0.0])

    spans = [(float(low), float(high)) for low, high in wall.get("gaps_pt") or []]

    for opening_along, box in openings:
        if opening_along != along:
            continue
        if box[across + 2] <= band_low or box[across] >= band_high:
            continue
        low, high = box[along], box[along + 2]
        if high <= run_low or low >= run_high:
            continue
        spans.append((max(low, run_low), min(high, run_high)))

    if not spans:
        return []

    merged = []
    for low, high in sorted(spans):
        if high <= low:
            continue
        if merged and low <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], high))
        else:
            merged.append((low, high))
    return merged


def _stretch_of(wall: dict, start: float, end: float, mm_per_point: float) -> dict:
    """One stretch of a severed wall, keeping everything but the run."""
    piece = dict(wall)
    along = 0 if wall["runs_along"] == "x" else 1
    across = wall["start_point_pt"][1 - along]
    low, high = sorted(wall["face_positions_pt"])

    if along == 0:
        piece["start_point_pt"] = [round(start, 2), across]
        piece["end_point_pt"] = [round(end, 2), across]
        piece["bbox"] = [round(start, 2), round(low, 2), round(end, 2), round(high, 2)]
    else:
        piece["start_point_pt"] = [across, round(start, 2)]
        piece["end_point_pt"] = [across, round(end, 2)]
        piece["bbox"] = [round(low, 2), round(start, 2), round(high, 2), round(end, 2)]

    piece["length_mm"] = (end - start) * mm_per_point
    piece["gaps_pt"] = []
    piece["terminates_at_an_opening"] = True
    piece["connects_to"] = []
    piece["junctions"] = []
    return piece


def _keep_what_encloses_something(walls: list, mm_per_point: float, config: dict) -> int:
    """Sets aside every candidate that neither closes a room nor holds the outside.

    **The invariant is a fact about buildings, not a threshold.** A wall bounds
    a room or forms part of the shell, so it lies on a **closed circuit** of
    walls: set off along it, keep turning at the walls it meets, and you arrive
    back where you started. A roof overhang, a carport rafter, a site boundary,
    a grid tick and a dimension extension line lie on no such circuit.

    **The graph is built on junction points, and a wall is a path through them
    rather than a single edge.** That distinction is the whole thing, and
    getting it wrong destroyed the building: with one edge per wall, an
    external wall crossed by five partitions *mid-span* has neither of its own
    ends attached to anything, so it is dropped - and then all five partitions
    lose an end and go with it. The collapse cascades and every wall on the
    sheet is set aside. Measured: 61 of 61 on one floor plan, and it stayed at
    zero survivors however far the endpoints were snapped, which is what showed
    the fault was in the formulation rather than in the tolerance.

    So each wall contributes an edge between each *consecutive pair* of
    junctions along it. The external wall above becomes four edges, each
    partition one, and the circuits round the rooms close properly.

    A wall carrying fewer than two junctions is on no circuit by definition:
    it is joined to the building at one point at most, which is what a rafter
    and an eave look like.

    Candidates are set aside **with the reason**, never deleted (Critical
    Rule 5).
    """
    if not setting(cv_settings.load_settings(), "wall.require_enclosure", True):
        return 0
    live = {
        wall["wall_id"]: wall
        for wall in walls
        if not wall.get("not_used_because")
    }
    if len(live) < 3:
        return 0

    slack = max(float(config.get("walls", {}).get("junction_tolerance_points", 10)), 1.0)
    nodes = _JunctionPoints(slack)
    edges = []          # (node_a, node_b, wall_id)
    on_a_circuit = set()

    for wall_id, wall in live.items():
        along = 0 if wall["runs_along"] == "x" else 1
        points = []
        for junction in wall.get("junctions") or []:
            point = junction.get("at_pt")
            if point and len(point) >= 2:
                points.append((float(point[along]), nodes.id_for(point)))
        if len(points) < 2:
            continue
        points.sort()
        for (_first_at, first), (_second_at, second) in zip(points, points[1:]):
            if first != second:
                edges.append((first, second, wall_id))

    if not edges:
        return 0

    # Repeatedly drop any point with fewer than two edges still on it, and the
    # edges hanging off it. What survives is what lies on a cycle.
    live_edges = set(range(len(edges)))
    while True:
        at_point = {}
        for index in live_edges:
            first, second, _ = edges[index]
            at_point.setdefault(first, set()).add(index)
            at_point.setdefault(second, set()).add(index)
        going = set()
        for point, on_it in at_point.items():
            if len(on_it) < 2:
                going |= on_it
        if not going:
            break
        live_edges -= going

    for index in live_edges:
        on_a_circuit.add(edges[index][2])

    # **Off the circuit is not enough on its own, and this is the difference
    # between pruning a rafter and deleting a building.** A rafter, a carport
    # joist, a grid tick and a boundary all have a genuinely FREE end - they
    # run out into open paper. A real wall that the tracing failed to connect
    # usually has a junction at each end and is merely off the circuit because
    # a neighbour of its neighbour is missing. Measured, the free ends on one
    # floor plan sit a median of 877 mm from the nearest wall, so what is
    # missing there is wall, not precision - and setting those walls aside on
    # the circuit test alone removed 80% of a floor plan.
    #
    # So both must hold: on no circuit, AND running out into open paper.
    shell = _Shell(list(live.values()))

    set_aside = 0
    for wall_id, wall in live.items():
        if wall_id in on_a_circuit or wall.get("terminates_at_an_opening"):
            continue
        if not _runs_out_into_open_paper(wall, shell.without(wall_id)):
            continue
        wall["not_used_because"] = (
            "This line is on no closed circuit of walls, so it bounds no room and is no "
            "part of the outside of the building. A roof line, an eave, a rafter, a "
            "boundary or a setting-out line looks like this."
        )
        wall["review_needed"] = True
        set_aside += 1

    if set_aside:
        logger.info(
            f"walls: {set_aside} candidate(s) lie on no closed circuit and were set "
            f"aside; {len(live) - set_aside} bound a room or the outside of the building"
        )
    return set_aside


class _Shell:
    """Where the building is, as seen by every wall except the one being judged.

    **Leave-one-out, and that is the whole of it.** The box has to say where the
    building is *without* the candidate under test, or the test is circular: an
    eave running three metres out from a corner extends the very outline meant
    to judge it, so it is always inside and never set aside. Two narrower
    definitions were tried first and both were wrong in an instructive way:

    *   *The walls that closed a circuit.* Far too small - measured on one floor
        plan, walls lying plainly inside the house, a 9.5 m one and a 5.9 m
        external wall among them, fell outside that box and were set aside.
        Deleting the middle of a house is a worse fault than the one guarded
        against.
    *   *The walls held at both ends.* Better, but on a sparsely traced sheet it
        still left an 8.76 m external wall outside the building it runs the
        length of.

    Every other live wall is in it, so the box is the building as this sheet
    actually traced it. Each bound therefore keeps its two most extreme values,
    so leaving one wall out costs nothing to work out.
    """

    def __init__(self, walls):
        self._ends = []
        for side, sign in ((0, 1), (1, 1), (2, -1), (3, -1)):
            best = []
            for wall in walls:
                box = wall.get("bbox")
                if not box or len(box) != 4:
                    continue
                best.append((sign * box[side], wall.get("wall_id")))
            best.sort()
            self._ends.append([(sign * value, wall_id) for value, wall_id in best[:2]])
        self._enough = len([w for w in walls if w.get("bbox")]) >= 4

    def without(self, wall_id):
        """The four bounds with this wall left out, or None if too few remain."""
        if not self._enough:
            return None
        bounds = []
        for side in range(4):
            values = [value for value, owner in self._ends[side] if owner != wall_id]
            if not values:
                return None
            bounds.append(values[0])
        return tuple(bounds)


def _runs_out_into_open_paper(wall: dict, shell, slack: float = 10.0) -> bool:
    """Whether this wall has an end that meets nothing, out beyond the building.

    **Two things, and the second is what keeps the building.** A free end is
    what a rafter, a joist, an eave, a boundary and a setting-out tick have in
    common - but a partition also stops free at a doorway, at a nib and at a
    return, and so does every wall whose neighbour the tracing missed. On a free
    end alone the invariant set aside 34, 44 and 7 walls on the three plan sets,
    taking retention down to 13 per cent on one sheet: that is not pruning a
    roof, it is deleting a house.

    So the free end also has to lie **outside the shell** - the box every other
    wall on the sheet occupies, which is the building as this sheet traced it. A partition's free end is inside the house; a rafter running out
    over the carport, a joist above the roof line and a boundary down the block
    have an end that is not.

    **How far outside is allowed differs along the wall and across it**, and
    that is a fact about walls rather than a tolerance to tune. Along its own
    run, an end beyond the building is exactly the thing being looked for, so
    only the junction slack is allowed. Across its thickness, a wall sitting on
    the edge of the shell is the outermost wall of the house and is *level* with
    it, not beyond it - so its own thickness is allowed. Without that, an 8.76 m
    external wall lying half a point below the shell's own edge had *both* its
    ends counted as outside and was set aside, though it runs the length of the
    building it belongs to.
    """
    if shell is None:
        return False
    along = 0 if wall["runs_along"] == "x" else 1
    across = 1 - along
    position = wall["start_point_pt"][across]
    faces = sorted(wall.get("face_positions_pt") or [position, position])
    sideways = max(slack, faces[1] - faces[0])
    if not (shell[across] - sideways <= position <= shell[across + 2] + sideways):
        # It does not run alongside the building at all, which the outline rule
        # in the reader proper is what judges - not this one.
        return False

    low = min(wall["start_point_pt"][along], wall["end_point_pt"][along])
    high = max(wall["start_point_pt"][along], wall["end_point_pt"][along])
    at = [
        junction["at_pt"][along]
        for junction in (wall.get("junctions") or [])
        if junction.get("at_pt") and len(junction["at_pt"]) > along
    ]
    for end in (low, high):
        if any(abs(point - end) <= slack for point in at):
            continue
        if not (shell[along] - slack <= end <= shell[along + 2] + slack):
            return True
    return False


class _JunctionPoints:
    """Junction points, grouped by proximity rather than rounded onto a grid.

    Two walls meeting at a corner report the meeting at very nearly - not
    exactly - the same point. Rounding those onto a grid of the tolerance puts
    them in different cells whenever they straddle a line: 4.9 and 5.1 round to
    0 and 1 though they are 0.2 apart. Then no two walls agree on the node they
    met at and no circuit ever closes. This project has recorded that mistake
    three times now, so it is a class rather than a line of code.
    """

    def __init__(self, slack: float):
        self._slack = slack
        self._centres = []

    def id_for(self, point) -> int:
        x, y = float(point[0]), float(point[1])
        for index, (cx, cy) in enumerate(self._centres):
            if abs(x - cx) <= self._slack and abs(y - cy) <= self._slack:
                return index
        self._centres.append((x, y))
        return len(self._centres) - 1


def _punch_openings_onto_walls(walls, openings, scale, mm_per_point: float, config: dict):
    """Cuts each opening the drawing states into the wall centreline it sits on.

    **An anchor is the drawing speaking, and it should not have to wait for the
    morphology to agree.** A door swing's arc is the leaf swept to its own
    width, a window symbol is the glazing drawn between the wall's faces, and a
    printed mark keys the opening to a schedule row. Each one says *there is an
    opening here, and this wide* - so it is cut into the wall directly, rather
    than hoping the closing left a hole in the band at the same place. It did
    not: closing joins the wall to the jambs, the leaf and the arc drawn inside
    the doorway (see ``breaks.py``).

    **The wall is severed, not shortened.** The gap is recorded on the wall and
    the wall keeps its own start and end, so the stretches either side stay in
    the record with their endpoints intact - which is what the junction graph
    and the model need, and what ``openingevidence`` tests when it asks whether
    an opening has wall on both sides of it. An opening that would leave no
    wall on one side is not cut at all: that is a wall ending, not a door.

    Only an opening carrying a **measured width** is cut. A mark with no width
    behind it says where something is but not how wide, and inventing a figure
    there would be worse than leaving the wall whole - the mark is still
    reported, and ``openingevidence`` still weighs it.
    """
    if not walls or not openings:
        return
    settings = config.get("walls", {})
    flank = float(settings.get("opening_min_wall_each_side_mm", 300)) / mm_per_point
    narrowest = float(settings.get("min_opening_width_mm", 300)) / mm_per_point
    detection = cv_settings.load_settings()
    share = cv_settings.number(
        detection, "breaks.band_match_share_of_thickness", 1.5
    )
    floor = cv_settings.number(detection, "breaks.collinear_tolerance_pt", 0.6)
    cut = 0

    for opening in openings:
        width_mm = getattr(opening, "width_mm", None)
        if not width_mm:
            continue
        try:
            x0, y0, x1, y1 = (float(v) for v in opening.bbox)
        except (TypeError, ValueError):
            continue
        width_pt = float(width_mm) / mm_per_point

        for wall in walls:
            along = 0 if wall["runs_along"] == "x" else 1
            low, high = sorted(wall["face_positions_pt"])
            centre = (low + high) / 2.0
            across_centre = (y0 + y1) / 2.0 if along == 0 else (x0 + x1) / 2.0
            reach = max(floor, (wall["thickness_mm"] * share) / mm_per_point)
            if abs(across_centre - centre) > reach:
                continue

            run_low = min(wall["start_point_pt"][along], wall["end_point_pt"][along])
            run_high = max(wall["start_point_pt"][along], wall["end_point_pt"][along])
            box_low, box_high = (x0, x1) if along == 0 else (y0, y1)
            if min(box_high, run_high) - max(box_low, run_low) <= 0:
                continue

            # **Where along the wall, and how wide.** The mark's own box is not
            # the opening - a swing's box is the leaf swept round, and a mark
            # is printed beside the door - so the box says where and the
            # measured width says how wide.
            middle = (max(box_low, run_low) + min(box_high, run_high)) / 2.0
            gap_low, gap_high = middle - width_pt / 2.0, middle + width_pt / 2.0

            # Wall has to remain on both sides of it, or this is the end of a
            # wall rather than a door in one.
            if gap_low - run_low < flank or run_high - gap_high < flank:
                continue
            if gap_high - gap_low < narrowest:
                continue
            if any(
                gap_low < existing[1] and existing[0] < gap_high
                for existing in wall["gaps_pt"]
            ):
                break
            wall["gaps_pt"].append([round(gap_low, 2), round(gap_high, 2)])
            cut += 1
            break

    for wall in walls:
        wall["gaps_pt"].sort()
    if cut:
        logger.info(
            f"openings: {cut} cut into a wall centreline from the drawing's own anchors"
        )


def _inject_face_gaps(walls: list, pairs: list, mm_per_point: float, config: dict):
    """Puts each paired face's breaks straight onto the wall centreline it belongs to.

    **This does not wait for the band to break.** The morphology cannot be
    relied on to leave a doorway open - it joins the wall to the jambs, the leaf
    and the swing arc drawn inside the opening, and the band comes out
    continuous. So the gaps are not inferred from the shape of the band at all.
    They are read off the two drawn faces before anything is closed
    (``breaks.py``), and written directly onto the wall that runs along the same
    line.

    A paired face carries the same three things a wall does - the band it
    occupies across its thickness, the run it covers along its length, and the
    breaks in it - so matching one to the other is a comparison of those, not a
    guess:

    *   **the same line**, within a share of the wall's own thickness. The pair
        that revealed a break is not always the pair the band was closed from -
        a lining or a hatch boundary shifts it - and a one-point tolerance
        placed **none of 35 real gaps** on one sheet read as a picture.
    *   **overlapping runs**, so a break is only written on a wall that actually
        covers that stretch of paper.
    *   **the break inside the wall's own run**, clipped to it, because a wall
        traced shorter than its faces must not claim a gap beyond its end.

    Nothing is invented: every gap written here was measured as a place where
    *both* drawn faces stop. Whether it is a door, a window or a cupboard is
    still decided afterwards by ``openingevidence``.
    """
    if not walls or not pairs:
        return
    settings = config.get("walls", {})
    narrowest = float(settings.get("min_opening_width_mm", 300)) / mm_per_point
    detection = cv_settings.load_settings()
    # **A whole thickness, not half of one, and that is measured.** The band a
    # wall was closed from is not the pair of faces that revealed its break:
    # closing picks up a lining, a hatch boundary or a return on one side and
    # shifts the band across by part of a thickness. On one sheet read as a
    # picture every real pair missed its wall by 3.0 to 3.7 points against a
    # reach of 2.5 - so 35 real gaps were found and none of them written.
    share = cv_settings.number(detection, "breaks.band_match_share_of_thickness", 1.0)
    floor = cv_settings.number(detection, "breaks.collinear_tolerance_pt", 0.6)
    written = 0

    for wall in walls:
        axis = "h" if wall["runs_along"] == "x" else "v"
        along = 0 if wall["runs_along"] == "x" else 1
        run_low = min(wall["start_point_pt"][along], wall["end_point_pt"][along])
        run_high = max(wall["start_point_pt"][along], wall["end_point_pt"][along])
        band_low, band_high = sorted(wall["face_positions_pt"])
        centre = (band_low + band_high) / 2.0
        reach = max(floor, (wall["thickness_mm"] * share) / mm_per_point)

        for pair in pairs:
            if pair["axis"] != axis:
                continue
            if abs(pair["position"] - centre) > reach:
                continue
            if min(pair["end"], run_high) - max(pair["start"], run_low) <= 0:
                continue
            for low, high in pair["gaps"]:
                gap_low, gap_high = max(low, run_low), min(high, run_high)
                if gap_high - gap_low < narrowest:
                    continue
                if any(
                    gap_low < existing[1] and existing[0] < gap_high
                    for existing in wall["gaps_pt"]
                ):
                    continue
                wall["gaps_pt"].append([round(gap_low, 2), round(gap_high, 2)])
                written += 1

    for wall in walls:
        wall["gaps_pt"].sort()
    if written:
        logger.info(
            f"breaks: {written} gap(s) injected onto wall centrelines from the paired faces"
        )


def _fill_in_thickness_context(walls: list, config: dict) -> None:
    """The nearest thickness the office actually builds, beside the measured one.

    Never substituted for the measurement - reported alongside it, exactly as
    the other reader does, because a wall measured at 234 mm is a 230 mm wall
    and a wall measured at 234 mm is also still 234 mm on this drawing.
    """
    settings = config.get("walls", {})
    standards = settings.get("nominal_thickness_mm", []) or []
    tolerance = float(settings.get("nominal_thickness_tolerance_mm", 12))
    for wall in walls:
        nominal, difference = legacy._nearest_standard(wall["thickness_mm"], standards)
        wall["nominal_thickness_mm"] = nominal
        wall["thickness_difference_mm"] = difference
        wall["matches_nominal_thickness"] = (
            nominal is not None and abs(difference) <= tolerance
        )
        # A thickness the office actually builds is stronger evidence than a
        # gap that merely falls in range, and a long wall stronger than a stub.
        confidence = 0.8 if wall["matches_nominal_thickness"] else 0.55
        if wall["length_mm"] >= 2000:
            confidence = min(confidence + 0.1, 0.95)
        wall["confidence"] = round(min(confidence, wall["confidence"] + 0.1), 3)
        wall["confidence_band"] = "high" if wall["confidence"] >= 0.75 else "review"


# --- the identical tail the other reader runs ------------------------------


def _through_the_same_post_processing(
    walls, mm_per_point, config, sheet_id, page_number, rooms, line_source
) -> None:
    """Everything ``walls.detect_walls`` does once it has its candidates.

    Imported and reused rather than reimplemented, so that the two readers
    cannot drift apart in how a wall is *judged* - only in how it was measured.
    A second copy of this sequence would be a second truth about what a
    junction is (Critical Rule 2).
    """
    # **The walls have to meet each other before anything can ask whether they
    # do.** A centreline stops at the face of the wall it runs into, half a
    # thickness short of its centreline, so without this the junction graph is
    # far too sparse for any circuit to close.
    cv_junctions.snap_endpoints(walls, mm_per_point, cv_settings.load_settings())
    cv_junctions.cast_rays_from_free_ends(
        walls, mm_per_point, cv_settings.load_settings()
    )

    alone = legacy.mark_walls_that_stand_alone(walls, mm_per_point, config)
    if alone:
        logger.info(
            f"{sheet_id}: {alone} candidate(s) meet no other wall, so they are "
            "marked for review and left out of the model"
        )

    walls.sort(key=lambda w: (-w["length_mm"], w["runs_along"]))
    for position, wall in enumerate(walls, start=1):
        wall["wall_id"] = f"{sheet_id}-W{position:03d}"
        wall["line_source"] = line_source

    legacy.detect_junctions(walls, config)
    tails = legacy.trim_free_tails(walls, rooms or [], mm_per_point, config)
    if tails:
        logger.info(
            f"{sheet_id}: {tails} candidate(s) ran on past the building into the "
            "margin, and were cut back to the last wall they meet"
        )
        legacy.detect_junctions(walls, config)
    detached = legacy.mark_detached_structures(walls, config)
    if detached:
        logger.info(
            f"{sheet_id}: {detached} wall(s) belong to a structure standing apart "
            "from the building"
        )
    legacy.classify_outer_inner(walls, config)
    legacy.describe_walls(walls, mm_per_point, config, sheet_id, page_number)
    # **A wall either closes a room or holds up the outside of the building.**
    # Last, and that is not arbitrary: ``describe_walls`` rewrites each record
    # from the geometry, ``not_used_because`` included, so a reason written
    # before it is silently thrown away - measured, the invariant set aside 61
    # candidates and 61 of them came back.
    _keep_what_encloses_something(walls, mm_per_point, config)
