"""Day 4 — candidate wall lines.

A wall on a floor plan is drawn as two parallel lines a wall's thickness apart.
That is what is looked for here: pairs of parallel drawn faces, separated by a
plausible thickness, overlapping along enough of their length to be one wall.

The word Week 1 uses is **candidate**, and it is meant literally. Nothing here
claims to have found the building's walls; it claims to have found line pairs
that look like walls, each with a measured length and thickness, a location on
the sheet and a confidence. A reviewer confirms them against the overlay. Day 5
turns the confirmed ones into the canonical model.

Three things make the result usable rather than noise:

*   **Faces are merged first, across openings.** The same wall face is drawn
    several times, and every door or window in it breaks it into separate
    pieces. Collinear pieces are joined — a wall with a window in it is still
    one wall — which is what makes the building's real walls appear at all: on
    the supplied floor plan, joining only touching pieces found nothing longer
    than 6 m on a 20 m building.
*   **Faces are paired by how far they run together, not by which is nearest.**
    A long external wall has many lines close to it: linings, hatching, joinery,
    furniture. Its nearest neighbour is rarely its own other face. So every
    plausible pair is scored by the length the two faces share, a nominal
    thickness breaks ties, and pairs are taken best-first with each face
    belonging to one wall only.
*   **Everything is measured in millimetres, through the calibrated scale.**
    A wall is only reported when the sheet's scale has been confirmed
    (see `scale.py`), because a length in points means nothing on its own.
"""

from app.logging_setup import get_logger
from pipeline.plan.rasterlines import extract_rulings_from_image

logger = get_logger()


def _merge_faces(segments: list, position_tolerance: float, join_gap: float) -> list:
    """Collapses repeated and broken-up drawings of the same line into one face.

    Returns (position, start, end) triples, in points.
    """
    grouped: dict = {}
    for position, start, end in segments:
        key = round(position / position_tolerance)
        grouped.setdefault(key, []).append((position, start, end))

    faces = []
    for spans in grouped.values():
        spans.sort(key=lambda s: s[1])
        current_start, current_end = spans[0][1], spans[0][2]
        members = [spans[0][0]]
        gaps: list = []
        for position, start, end in spans[1:]:
            if start <= current_end + join_gap:
                # The break being bridged is a real break in the drawn line.
                # A door or a window is exactly that, so it is kept rather
                # than thrown away — this is where openings come from on a
                # drawing that prints no D1/W1 marks.
                if start > current_end:
                    gaps.append((current_end, start))
                current_end = max(current_end, end)
                members.append(position)
            else:
                faces.append((sum(members) / len(members), current_start, current_end, gaps))
                current_start, current_end, members, gaps = start, end, [position], []
        faces.append((sum(members) / len(members), current_start, current_end, gaps))
    faces.sort()
    return faces


def _pair_faces(faces: list, mm_per_point: float, config: dict, standards: list) -> list:
    """Pairs of parallel faces that run together, taken best-first.

    Scored by the length the two faces share, because that is what makes two
    lines a wall. A thickness the office actually builds breaks ties. Each face
    is used once, so one wall produces one record.
    """
    settings = config.get("walls", {})
    min_length_mm = float(settings.get("min_wall_length_mm", 900))
    min_thickness = float(settings.get("min_thickness_mm", 70))
    max_thickness = float(settings.get("max_thickness_mm", 320))
    thickness_tolerance = float(settings.get("nominal_thickness_tolerance_mm", 12))
    min_flank = float(settings.get("opening_min_wall_each_side_mm", 300))

    usable = [f for f in faces if (f[2] - f[1]) * mm_per_point >= min_length_mm]

    candidates = []
    for index, (position, start, end, _gaps) in enumerate(usable):
        for other_index in range(index + 1, len(usable)):
            other_position, other_start, other_end, _other_gaps = usable[other_index]
            thickness = abs(other_position - position) * mm_per_point
            if not (min_thickness <= thickness <= max_thickness):
                continue
            overlap = (min(end, other_end) - max(start, other_start)) * mm_per_point
            if overlap < min_length_mm:
                continue
            nominal_match = standards and min(
                abs(float(n) - thickness) for n in standards
            ) <= thickness_tolerance
            score = overlap * (1.15 if nominal_match else 1.0)
            candidates.append((score, overlap, thickness, index, other_index))

    candidates.sort(key=lambda item: -item[0])
    used: set = set()
    pairs = []
    for _score, overlap, thickness, index, other_index in candidates:
        if index in used or other_index in used:
            continue
        used.add(index)
        used.add(other_index)
        position, start, end, gaps = usable[index]
        other_position, other_start, other_end, other_gaps = usable[other_index]
        run_start, run_end = max(start, other_start), min(end, other_end)
        pairs.append(
            {
                "centre_position": (position + other_position) / 2.0,
                "face_positions": [round(position, 2), round(other_position, 2)],
                "start": run_start,
                "end": run_end,
                "thickness_mm": round(thickness, 1),
                "length_mm": round(overlap, 1),
                "gaps": _shared_gaps(
                    gaps, other_gaps, run_start, run_end, min_flank / mm_per_point
                ),
            }
        )
    return pairs


def _shared_gaps(
    gaps: list, other_gaps: list, run_start: float, run_end: float, min_flank: float
) -> list:
    """Breaks that interrupt **both** faces of the wall, at the same place.

    A break in one face alone is a cupboard line, a bench, a change of
    material. A door or a window goes through the wall, so both of its faces
    stop and start again together — that is what makes this an opening rather
    than a gap in one drawn line.
    """
    shared = []
    for start, end in gaps:
        for other_start, other_end in other_gaps:
            low, high = max(start, other_start), min(end, other_end)
            if high <= low:
                continue
            low, high = max(low, run_start), min(high, run_end)
            if high <= low:
                continue
            # An opening has wall on both sides of it. A break that runs to the
            # end of the wall is not a door — it is where the drawing stopped,
            # or where this wall meets another. Requiring a solid run either
            # side is what separates the two, and it removed the 5.4 m
            # "openings" that were really stretches of wall never traced.
            if low - run_start < min_flank or run_end - high < min_flank:
                continue
            shared.append((low, high))
    shared.sort()
    return shared


def _nearest_standard(thickness_mm: float, standards: list):
    """The nominal thickness this measurement is closest to, and how far off.

    Reported rather than applied: the measured value stays as measured, and the
    nominal one is offered as context for a reviewer.
    """
    if not standards:
        return None, None
    best = min(standards, key=lambda s: abs(float(s) - thickness_mm))
    return float(best), round(thickness_mm - float(best), 1)


def detect_walls(
    rulings: dict,
    calibration: dict,
    config: dict,
    sheet_id: str,
    exclude_region=None,
    page=None,
    sheet_span_mm=None,
) -> list:
    """Candidate walls for one sheet, in millimetres.

    Returns an empty list when the sheet's scale could not be confirmed — a
    length that cannot be trusted is worse than no length at all, and the
    reason is reported separately by the scale check.

    ``exclude_region`` is the title block. It is ruled into cells, and those
    rules are parallel lines a few millimetres apart at drawing scale, so
    without excluding it the revisions table is reported as a wall.
    """
    if not calibration.get("usable_for_measurement"):
        return []
    mm_per_point = calibration.get("measured_mm_per_point") or calibration.get(
        "printed_mm_per_point"
    )
    if not mm_per_point:
        return []

    settings = config.get("walls", {})
    position_tolerance = float(settings.get("face_position_tolerance_pt", 0.6))
    standards = settings.get("nominal_thickness_mm", [])
    thickness_tolerance = float(settings.get("nominal_thickness_tolerance_mm", 12))
    # A door or window breaks its wall's face into pieces; collinear pieces
    # separated by less than an opening's width are the same face.
    join_gap = max(float(settings.get("face_join_gap_mm", 6000)) / mm_per_point, 2.0)

    def outside_excluded(position, start, end, axis) -> bool:
        if not exclude_region:
            return True
        x0, y0, x1, y1 = exclude_region
        if axis == "x":
            along_low, along_high, across = start, end, position
            return not (x0 <= along_low and along_high <= x1 and y0 <= across <= y1)
        along_low, along_high, across = start, end, position
        return not (y0 <= along_low and along_high <= y1 and x0 <= across <= x1)

    def build(source_rulings: dict) -> list:
        return _walls_from(
            source_rulings,
            mm_per_point,
            config,
            standards,
            position_tolerance,
            thickness_tolerance,
            join_gap,
            outside_excluded,
        )

    # The sheet's own geometry is always tried first — it is the drawing's
    # exact line work, and nothing recovered from pixels can be more accurate
    # than that. Only when it yields no walls at all is the page rendered and
    # measured as an image, because that outcome means the lines are not in the
    # PDF as lines: one supplied set places its whole plan as embedded images
    # and produced 400 drawing items against 16,117 on the vector set. Deciding
    # on the outcome rather than on a line count means the fallback cannot
    # displace good vector geometry on a sheet that simply has few lines.
    # A wall cannot be longer than the longest distance this sheet measures.
    # The drawing states its own overall size in its dimension strings, so the
    # limit comes from the drawing rather than from a setting: on a 20.9 m
    # house the site boundary and the eave line were being paired into 23.9 m
    # "walls", which is the drawing's block edge, not the building.
    limit = sheet_span_mm * float(settings.get("length_allowance", 1.05)) if sheet_span_mm else None

    line_source = "vector"
    walls = build(rulings)
    if not walls and page is not None:
        recovered = extract_rulings_from_image(page, config, mm_per_point)
        if recovered["h"] or recovered["v"]:
            walls = build(recovered)
            if walls:
                line_source = "rendered_page"

    # A candidate longer than anything this sheet measures is marked for
    # review rather than removed. On one floor plan that is exactly right —
    # the 23.9 m pair is the block boundary, not a wall of a 21 m house. But a
    # sheet may also dimension only one wing of what it draws, and discarding a
    # real wall because the drawing did not measure it would be hiding a
    # finding rather than reporting it (Critical Rule 5).
    over_length = 0
    for wall in walls:
        if limit and wall["length_mm"] > limit:
            over_length += 1
            wall["longer_than_sheet_measures"] = True
            wall["confidence"] = round(min(wall["confidence"], 0.5), 3)
            wall["confidence_band"] = "review"
        else:
            wall["longer_than_sheet_measures"] = False
    if over_length:
        logger.info(
            f"{sheet_id}: {over_length} candidates run longer than the "
            f"{limit / 1000:.1f} m this sheet measures and are marked for review"
        )

    walls.sort(key=lambda w: (-w["length_mm"], w["runs_along"]))
    for position, wall in enumerate(walls, start=1):
        wall["wall_id"] = f"{sheet_id}-W{position:03d}"
        wall["line_source"] = line_source


    logger.info(
        f"{sheet_id}: {len(walls)} candidate walls from {line_source} lines "
        f"({sum(1 for w in walls if w['matches_nominal_thickness'])} at a nominal thickness)"
    )
    return walls


def _walls_from(
    rulings: dict,
    mm_per_point: float,
    config: dict,
    standards: list,
    position_tolerance: float,
    thickness_tolerance: float,
    join_gap: float,
    outside_excluded,
) -> list:
    """Wall records from one set of drawn faces, whatever produced them."""
    walls = []
    for axis, segments in (("x", rulings.get("h", [])), ("y", rulings.get("v", []))):
        faces = _merge_faces(segments, position_tolerance, join_gap)
        faces = [f for f in faces if outside_excluded(f[0], f[1], f[2], axis)]
        for pair in _pair_faces(faces, mm_per_point, config, standards):
            nominal, difference = _nearest_standard(pair["thickness_mm"], standards)
            # A thickness close to one the office actually builds is a stronger
            # candidate than an arbitrary gap that happens to fall in range.
            matches_standard = (
                nominal is not None and abs(difference) <= thickness_tolerance
            )
            length_mm = pair["length_mm"]
            confidence = 0.8 if matches_standard else 0.55
            if length_mm >= 2000:
                confidence = min(confidence + 0.1, 0.95)

            if axis == "x":
                # A horizontal pair of faces is a wall running left to right.
                start_point = [pair["start"], pair["centre_position"]]
                end_point = [pair["end"], pair["centre_position"]]
            else:
                start_point = [pair["centre_position"], pair["start"]]
                end_point = [pair["centre_position"], pair["end"]]

            walls.append(
                {
                    "wall_id": "",
                    "runs_along": axis,
                    "length_mm": length_mm,
                    "thickness_mm": pair["thickness_mm"],
                    "nominal_thickness_mm": nominal,
                    "thickness_difference_mm": difference,
                    "matches_nominal_thickness": matches_standard,
                    "start_point_pt": [round(v, 2) for v in start_point],
                    "end_point_pt": [round(v, 2) for v in end_point],
                    "face_positions_pt": pair["face_positions"],
                    "bbox": _wall_bbox(axis, pair),
                    "line_source": "",
                    "confidence": round(confidence, 3),
                    "confidence_band": "high" if confidence >= 0.75 else "review",
                    "review_status": "needs_review",
                    "linked_opening_marks": [],
                    "gaps_pt": [
                        [round(low, 2), round(high, 2)] for low, high in pair["gaps"]
                    ],
                }
            )

    return walls


def _wall_bbox(axis: str, pair: dict) -> list:
    low, high = sorted(pair["face_positions"])
    if axis == "x":
        return [round(pair["start"], 2), round(low, 2), round(pair["end"], 2), round(high, 2)]
    return [round(low, 2), round(pair["start"], 2), round(high, 2), round(pair["end"], 2)]
