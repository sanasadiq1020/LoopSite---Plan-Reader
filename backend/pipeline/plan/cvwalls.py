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
from pipeline.plan.cvdetect import openings as cv_openings
from pipeline.plan.cvdetect import settings as cv_settings
from pipeline.plan.cvdetect import vectorpaths, wallgeometry
from pipeline.plan.cvdetect.settings import Scale

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
