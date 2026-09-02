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

**A building's walls hold on to each other, and that is now read as well.**
Two parallel faces are only half of what a floor plan draws. The other half is
where those pairs *meet*: an external wall runs round the outside, and every
partition inside it runs up to that wall and stops, making an L at a corner, a
T where a partition lands on a wall, and a + where two partitions cross. Those
meeting points are as much a fact about the drawing as the parallel lines are,
and reading them changes what can be said about a wall:

*   **A short stretch that meets the building is a wall; a short stretch that
    meets nothing is furniture.** A pier, a return, a nib beside a doorway and
    a partition between a WC and a hall are all real and all short, so a plain
    length floor either loses them or lets in every bench top on the sheet. The
    junction tells the two apart, which is why the length floor could be
    dropped from 900 mm to 200 mm without the sheet filling up with joinery.
*   **A wall knows which walls it is joined to** (``connects_to``), and the
    junctions together are a graph - walls the nodes, junctions the edges -
    written out as ``wall_graph.json``. Closing that graph into rooms is a
    later stage; what is built here is the graph it will read.
*   **Outside and inside are told apart by geometry, not by a rectangle.** A
    wall with nothing but open paper on one side of it is an external wall. A
    ray is cast out from each side of every wall, and a side that reaches the
    edge of the building without crossing another wall is an outside face. On
    an L-shaped or a U-shaped plan a bounding rectangle gets that wrong; a ray
    does not. A wall the rays cannot decide is reported as ``unknown`` rather
    than guessed (Critical Rule 5).

Three kinds of line are dropped before any of that, because each of them draws
as two parallel lines and none of them is a wall: **dimension lines**, which
are drawn far thinner than a wall face and carry arrowheads; **hatching**,
which is many parallel strokes closer together than a wall is ever built; and
anything **shorter than the length floor**, which is a fragment.

Every threshold above lives in ``config/wall_config.json`` and every length is
in millimetres through the sheet's own calibrated scale. Nothing here holds a
coordinate, a room name or a count from any particular drawing (Critical Rule 1).
"""

from app.logging_setup import get_logger
from pipeline.plan.rasterlines import extract_rulings_from_image

logger = get_logger()


def _setting(settings: dict, *names, default=None):
    """The first of these settings that is present.

    The wall settings are read from two files - ``plan_reading.json`` and the
    office's own ``wall_config.json`` - and the two name some of the same
    quantities differently. Rather than rename anything and break an office's
    existing file, both names are accepted and the first one found wins.
    """
    for name in names:
        if name in settings and settings[name] is not None:
            return settings[name]
    return default


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


def _pair_faces(faces: list, mm_per_point: float, config: dict, standards: list):
    """Wrapper kept for readers that only want the pairs."""
    return _pair_faces_and_faces(faces, mm_per_point, config, standards)[0]


def _pair_faces_and_faces(faces: list, mm_per_point: float, config: dict, standards: list):
    """Pairs of parallel faces that run together, taken best-first.

    Scored by the length the two faces share, because that is what makes two
    lines a wall. A thickness the office actually builds breaks ties. Each face
    is used once, so one wall produces one record.
    """
    settings = config.get("walls", {})
    min_length_mm = float(_setting(settings, "min_wall_length_mm", default=900))
    min_thickness = float(
        _setting(settings, "min_wall_thickness_mm", "min_thickness_mm", default=70)
    )
    max_thickness = float(
        _setting(settings, "max_wall_thickness_mm", "max_thickness_mm", default=320)
    )
    thickness_tolerance = float(settings.get("nominal_thickness_tolerance_mm", 12))
    min_flank = float(settings.get("opening_min_wall_each_side_mm", 300))
    # **How far two faces run together, as a share of the shorter of them.**
    # A share rather than a length, because that is what makes two lines one
    # wall whatever their size: a face that shares a tenth of its length with
    # another is passing it, not facing it. The *shorter* face is the
    # denominator on purpose - one external face 20 m long faces a dozen
    # internal faces 3 m each, and every one of those is a real wall.
    min_overlap_share = float(_setting(
        settings, "min_parallel_overlap_percent", default=0.0
    )) / 100.0
    min_slenderness = float(_setting(settings, "min_length_to_thickness", default=0.0))

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
            # **A wall is longer than it is thick.** Two lines 230 mm apart that
            # run together for 230 mm are a square, and a square is a column, a
            # symbol, a hatch cell or a fragment - never a wall. This is a fact
            # about walls and it holds at any scale, which is what makes it
            # worth having: the length floor is a size and can be wrong for a
            # small building, but nothing that is as wide as it is long is a
            # wall of any building.
            if overlap < thickness * min_slenderness:
                continue
            shorter = min(end - start, other_end - other_start) * mm_per_point
            if shorter > 0 and (overlap / shorter) < min_overlap_share:
                continue
            nominal_match = standards and min(
                abs(float(n) - thickness) for n in standards
            ) <= thickness_tolerance
            score = overlap * (1.15 if nominal_match else 1.0)
            candidates.append((score, overlap, thickness, index, other_index))

    # **A face is used stretch by stretch, not all at once.** A long external
    # wall is one continuous face on the outside and several shorter ones on
    # the inside, because the rooms behind it break the inner face up. Marking
    # the whole outer face as used the moment it met the first inner one left
    # every later stretch of that wall with nothing to pair against — which is
    # why a wall could be marked up for only half its length, with the rest of
    # it bare. Each pairing now takes only the stretch it covers, and the face
    # stays available everywhere else.
    candidates.sort(key=lambda item: -item[0])
    taken: dict = {}
    used: set = set()
    pairs = []
    for _score, _overlap, thickness, index, other_index in candidates:
        position, start, end, gaps = usable[index]
        other_position, other_start, other_end, other_gaps = usable[other_index]
        together = (max(start, other_start), min(end, other_end))

        free = _longest_free_stretch(
            _still_free(taken.get(index, []), together),
            _still_free(taken.get(other_index, []), together),
        )
        if free is None:
            continue
        run_start, run_end = free
        length_mm = (run_end - run_start) * mm_per_point
        if length_mm < min_length_mm:
            continue

        taken.setdefault(index, []).append(free)
        taken.setdefault(other_index, []).append(free)
        used.add(index)
        used.add(other_index)
        pairs.append(
            {
                "centre_position": (position + other_position) / 2.0,
                "face_positions": [round(position, 2), round(other_position, 2)],
                "start": run_start,
                "end": run_end,
                "thickness_mm": round(thickness, 1),
                "length_mm": round(length_mm, 1),
                "gaps": _shared_gaps(
                    gaps, other_gaps, run_start, run_end, min_flank / mm_per_point
                ),
            }
        )
    # **A face that found no partner is tried again with more room.** The
    # thickness range is what stops a wall being paired with the wrong line, so
    # it is deliberately tight — and a wall drawn a little outside it, a
    # rendered skin or a wall with a service duct in it, then loses its partner
    # and disappears. Widening only for the faces that failed keeps the tight
    # range where it is working.
    stretch = float(_setting(settings, "second_look_widening", default=0.0))
    if stretch > 0 and len(used) < len(usable):
        for index, (position, start, end, _gaps) in enumerate(usable):
            if index in used:
                continue
            for other_index in range(len(usable)):
                if other_index == index or other_index in used:
                    continue
                other_position, other_start, other_end, _o = usable[other_index]
                thickness = abs(other_position - position) * mm_per_point
                if not (
                    min_thickness * (1 - stretch)
                    <= thickness
                    <= max_thickness * (1 + stretch)
                ):
                    continue
                overlap = (min(end, other_end) - max(start, other_start)) * mm_per_point
                if overlap < min_length_mm:
                    continue
                # **The second look widens the thickness range and nothing
                # else.** Every other test is there for its own reason and
                # skipping them let a 230 mm square back in as a wall.
                if overlap < thickness * min_slenderness:
                    continue
                shorter = min(end - start, other_end - other_start) * mm_per_point
                if shorter > 0 and (overlap / shorter) < min_overlap_share:
                    continue
                run_start, run_end = max(start, other_start), min(end, other_end)
                used.add(index)
                used.add(other_index)
                pairs.append(
                    {
                        "centre_position": (position + other_position) / 2.0,
                        "face_positions": [round(position, 2), round(other_position, 2)],
                        "start": run_start,
                        "end": run_end,
                        "thickness_mm": round(thickness, 1),
                        "length_mm": round((run_end - run_start) * mm_per_point, 1),
                        "gaps": [],
                        "found_on_a_second_look": True,
                    }
                )
                break

    return pairs, used, usable


def _still_free(claimed: list, within: tuple) -> list:
    """The parts of ``within`` on one face that no wall has taken yet."""
    free = [within]
    for low, high in claimed:
        remaining = []
        for start, end in free:
            if high <= start or low >= end:
                remaining.append((start, end))
                continue
            if low > start:
                remaining.append((start, low))
            if high < end:
                remaining.append((high, end))
        free = remaining
    return free


def _longest_free_stretch(one: list, other: list):
    """The longest stretch free on both faces at once, or None."""
    best = None
    for a_start, a_end in one:
        for b_start, b_end in other:
            start, end = max(a_start, b_start), min(a_end, b_end)
            if end <= start:
                continue
            if best is None or (end - start) > (best[1] - best[0]):
                best = (start, end)
    return best


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
    page_number=None,
    text_boxes=None,
    rooms=None,
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
    position_tolerance = float(
        _setting(settings, "collinear_tolerance_points", "face_position_tolerance_pt", default=0.6)
    )
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

    # A wall cannot be longer than the longest distance this sheet measures.
    # The drawing states its own overall size in its dimension strings, so the
    # limit comes from the drawing rather than from a setting: on a 20.9 m
    # house the site boundary and the eave line were being paired into 23.9 m
    # "walls", which is the drawing's block edge, not the building.
    # **A wall running north cannot be longer than the building measures north.**
    # The limit used to be the longest distance the sheet measures in any
    # direction, which on a house 20 m wide and 11 m deep let a 17.9 m vertical
    # "wall" through — and that was a dimension string's witness line, drawn
    # from the building out to where the figures are printed, joined onto a
    # real wall face lying on the same line. The sheet states its size in each
    # direction separately, so the limit is taken separately too.
    allowance = float(settings.get("length_allowance", 1.05))
    limit_for = {
        axis: (span * allowance if span else None)
        for axis, span in (sheet_span_mm or {}).items()
    } if isinstance(sheet_span_mm, dict) else {
        "x": sheet_span_mm * allowance if sheet_span_mm else None,
        "y": sheet_span_mm * allowance if sheet_span_mm else None,
    }

    def build(source_rulings: dict) -> list:
        """Every step that decides *whether* something is a wall, in order.

        All of it lives here, and not spread out after the choice of line
        source, because the choice of line source is made by comparing what
        two readings of the same sheet produced — and a count taken before the
        copies and the joinery have been removed is not a count of walls. On
        one sheet the raw vector reading returned six candidates, every one of
        them a fragment that this function goes on to reject, and comparing
        that six against the picture meant the picture was never read at all.
        """
        found = _walls_from(
            source_rulings,
            mm_per_point,
            config,
            standards,
            position_tolerance,
            thickness_tolerance,
            join_gap,
            outside_excluded,
            text_boxes,
        )

        # A candidate longer than anything this sheet measures is marked for
        # review rather than removed. On one floor plan that is exactly right —
        # the 23.9 m pair is the block boundary, not a wall of a 21 m house.
        # But a sheet may also dimension only one wing of what it draws, and
        # discarding a real wall because the drawing did not measure it would
        # be hiding a finding rather than reporting it (Critical Rule 5).
        over_length = 0
        for wall in found:
            limit = limit_for.get(wall["runs_along"])
            if limit and wall["length_mm"] > limit:
                over_length += 1
                wall["longer_than_sheet_measures"] = True
                wall["confidence"] = round(min(wall["confidence"], 0.5), 3)
                wall["confidence_band"] = "review"
            else:
                wall["longer_than_sheet_measures"] = False
        if over_length:
            measured = ", ".join(
                f"{axis}: {value / 1000:.1f} m"
                for axis, value in sorted(limit_for.items())
                if value
            )
            logger.info(
                f"{sheet_id}: {over_length} candidates run longer than this sheet "
                f"measures in their own direction ({measured}) and are marked for review"
            )

        # An external wall is drawn with more lines than its own two faces, so
        # the pairing above produces several overlapping candidates for one
        # wall. Two solids cannot occupy the same space, so those are one wall
        # and are reported once, keeping every break any of them recorded.
        before = len(found)
        found = merge_overlapping_walls(found, mm_per_point, config)
        if before != len(found):
            logger.info(
                f"{sheet_id}: {before} candidates occupy {len(found)} places, so "
                f"{before - len(found)} were copies of a wall already reported"
            )

        # **A short stretch is judged on what it is joined to, not on its
        # length.** A pier, a return, a nib and the partition between a WC and
        # a hall are all real walls and all short; a bench top, a wardrobe and
        # a step are short too and are joined to nothing. The junctions tell
        # the two apart, which is what lets the length floor come down at all.
        before = len(found)
        found = _drop_short_walls_that_meet_nothing(found, config)
        if before != len(found):
            logger.info(
                f"{sheet_id}: {before - len(found)} short candidate(s) meet no other "
                "wall, so they are joinery or furniture rather than walls"
            )
        return found

    # The sheet's own geometry is always tried first — it is the drawing's
    # exact line work, and nothing recovered from pixels can be more accurate
    # than that. The page is only read as a picture when that geometry did not
    # trace a building, because that outcome means the lines are not in the PDF
    # as lines: one supplied set places its whole plan as embedded images and
    # produced 400 drawing items against 16,117 on the vector set.
    #
    # **A building is a closed shape, so it takes at least four walls.** The
    # test used to be "no walls at all", which held only while the length floor
    # was high enough that a stray pair could never qualify. Once a short
    # stretch could be a wall, one pair of lines on a sheet drawn entirely as a
    # picture was enough to call the vector reading a success, and a whole
    # floor plan came back as a single 0.7 m wall. The fuller of the two
    # readings wins, so exact line work is never displaced by pixels that found
    # less.
    enough_to_be_a_building = int(settings.get("min_walls_for_an_unnamed_plan", 4))

    line_source = "vector"
    walls = build(rulings)
    if len(walls) < enough_to_be_a_building and page is not None:
        recovered = extract_rulings_from_image(page, config, mm_per_point)
        if recovered["h"] or recovered["v"]:
            from_picture = build(recovered)
            if len(from_picture) > len(walls):
                walls = from_picture
                line_source = "rendered_page"

    # A building's walls meet each other. A pair of parallel lines touching
    # nothing else is an eave, a roof extent, a fence or a bench.
    alone = mark_walls_that_stand_alone(walls, mm_per_point, config)
    if alone:
        logger.info(
            f"{sheet_id}: {alone} candidate(s) meet no other wall, so they are "
            "marked for review and left out of the model"
        )

    walls.sort(key=lambda w: (-w["length_mm"], w["runs_along"]))
    for position, wall in enumerate(walls, start=1):
        wall["wall_id"] = f"{sheet_id}-W{position:03d}"
        wall["line_source"] = line_source

    # The junctions are read once the walls have their identifiers, because a
    # junction is a statement about two named walls. Outside and inside then
    # follow from the geometry, and the plain-words half of each record last.
    junctions = detect_junctions(walls, config)
    # A wall ends at another wall or at the outside of the building, never in
    # the margin. Cutting the tails changes where walls meet, so the junctions
    # are read again over the walls as they now stand.
    tails = trim_free_tails(walls, rooms or [], mm_per_point, config)
    if tails:
        logger.info(
            f"{sheet_id}: {tails} candidate(s) ran on past the building into the "
            "margin, and were cut back to the last wall they meet"
        )
        junctions = detect_junctions(walls, config)
    classify_outer_inner(walls, config)
    describe_walls(walls, mm_per_point, config, sheet_id, page_number)

    outer = sum(1 for w in walls if w["wall_type"] == "outer")
    inner = sum(1 for w in walls if w["wall_type"] == "inner")
    logger.info(
        f"{sheet_id}: {len(walls)} candidate walls from {line_source} lines "
        f"({sum(1 for w in walls if w['matches_nominal_thickness'])} at a nominal "
        f"thickness), {junctions} junctions, {outer} outer and {inner} inner"
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
    text_boxes=None,
) -> list:
    """Wall records from one set of drawn faces, whatever produced them."""
    settings = config.get("walls", {})
    walls = []
    lone_by_axis = []
    for axis, segments, widths in (
        ("x", rulings.get("h", []), rulings.get("h_widths", [])),
        ("y", rulings.get("v", []), rulings.get("v_widths", [])),
    ):
        # Dimension lines, leaders and hatch strokes are dropped before
        # anything is paired, so they cannot become half of a false wall.
        segments = _drop_lines_that_are_not_wall_faces(segments, widths, settings)
        # Letters are not walls. This runs before pairing, so a word can never
        # become one face of a wall and a line of text can never become two.
        segments = _drop_lettering(segments, axis, text_boxes, settings)
        faces = _merge_faces(segments, position_tolerance, join_gap)
        faces = _drop_hatching(faces, mm_per_point, settings)
        faces = [f for f in faces if outside_excluded(f[0], f[1], f[2], axis)]
        pairs, used_faces, usable_faces = _pair_faces_and_faces(
            faces, mm_per_point, config, standards
        )
        for pair in pairs:
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
                    "merged_from": 1,
                    "meets_another_wall": True,
                    "linked_opening_marks": [],
                    "gaps_pt": [
                        [round(low, 2), round(high, 2)] for low, high in pair["gaps"]
                    ],
                    "thickness_is_assumed": False,
                    "found_on_a_second_look": bool(pair.get("found_on_a_second_look")),
                }
            )

        lone_by_axis.append((usable_faces, used_faces, axis))

    # **A drawn face that never found a partner is still a wall drawn with one
    # face missing** — but only if it runs between other walls. That test needs
    # the paired walls of *both* axes, so it waits until they are all found.
    walls.extend(
        _lone_face_walls(lone_by_axis, walls, mm_per_point, config, standards)
    )
    return walls


def _lone_face_walls(lone_by_axis: list, paired: list, mm_per_point, config, standards):
    """The lone faces that are built into the building, as walls.

    **A lone line touching nothing is a leader, a hatch boundary or a fold
    mark; a lone line running into two walls is a wall with a face missing.**
    Without that test every long line on the sheet became a wall — 119 of them
    on one floor plan — and the building's own outline was lost among them.
    """
    settings = config.get("walls", {})
    if not settings.get("keep_lone_faces", True) or not paired:
        return []
    needed = int(settings.get("lone_face_min_junctions", 2))
    slack = float(settings.get("junction_tolerance_points", 10.0))

    kept = []
    for usable_faces, used_faces, axis in lone_by_axis:
        for candidate in walls_from_lone_faces(
            usable_faces, used_faces, axis, mm_per_point, config, standards
        ):
            meets = sum(
                1
                for wall in paired
                if _junction_between(candidate, wall, slack) is not None
            )
            if meets >= needed:
                candidate["meets_this_many_walls"] = meets
                kept.append(candidate)
    return kept


def _wall_bbox(axis: str, pair: dict) -> list:
    low, high = sorted(pair["face_positions"])
    if axis == "x":
        return [round(pair["start"], 2), round(low, 2), round(pair["end"], 2), round(high, 2)]
    return [round(low, 2), round(pair["start"], 2), round(high, 2), round(pair["end"], 2)]


# --- One wall, reported once ---------------------------------------------


def _band(wall: dict):
    """The strip of paper this wall occupies across its own thickness."""
    low, high = sorted(wall["face_positions_pt"])
    return low, high


def _run(wall: dict):
    """Where this wall starts and ends along its own length, in points."""
    start, end = wall["start_point_pt"], wall["end_point_pt"]
    if wall["runs_along"] == "x":
        return min(start[0], end[0]), max(start[0], end[0])
    return min(start[1], end[1]), max(start[1], end[1])


def _overlap(first, second) -> float:
    return min(first[1], second[1]) - max(first[0], second[0])


def merge_overlapping_walls(walls: list, mm_per_point: float, config: dict) -> list:
    """Candidates occupying the same space are one wall, reported once.

    **Two solids cannot occupy the same place**, which is a fact about
    buildings rather than a setting. An external wall is drawn with more lines
    than its two faces — a lining, a hatch boundary, a cavity — so the pairing
    step, which uses each face once, produces several overlapping candidates
    for the one wall. On a supplied floor plan three "walls" sat within 1.2
    points of each other along the same run, and the effects were not cosmetic:

    *   the wall count was roughly double the building's,
    *   an opening mark had two or three equally close walls, so it was
        reported as ambiguous and never placed — the reason only one mark in
        sixteen ever reached a wall,
    *   each duplicate held only some of the wall's breaks, so a door in the
        wall was invisible to whichever copy did not record it,
    *   and the model stacked overlapping boxes in the same place.

    Candidates are merged when they run along the same axis, their thickness
    bands intersect, and they run together for a real distance. The one kept is
    the one that most looks like a wall — inside the length the sheet measures,
    at a thickness the office builds, longest — and it inherits every break the
    others recorded, so no opening is lost with the copy that held it.
    """
    settings = config.get("walls", {})
    if not settings.get("merge_overlapping_candidates", True):
        return walls
    min_run_overlap = float(settings.get("merge_min_run_overlap_mm", 300)) / mm_per_point

    kept = []
    for axis in ("x", "y"):
        group = [w for w in walls if w["runs_along"] == axis]
        parent = list(range(len(group)))

        def root(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                if not _same_wall(group[i], group[j], mm_per_point, settings):
                    continue
                if _overlap(_run(group[i]), _run(group[j])) < min_run_overlap:
                    continue
                parent[root(i)] = root(j)

        clusters: dict = {}
        for index in range(len(group)):
            clusters.setdefault(root(index), []).append(group[index])

        for members in clusters.values():
            kept.append(_best_candidate(members))

    kept.sort(key=lambda w: (-w["length_mm"], w["runs_along"]))
    return kept


def _same_wall(first: dict, second: dict, mm_per_point: float, settings: dict) -> bool:
    """Whether these two candidates are two readings of the one wall.

    Three things have to hold, and each is a fact about walls rather than a
    setting to be tuned:

    *   **They occupy the same space.** Two solids cannot, so overlapping
        thickness bands mean one of them is not a separate wall.
    *   **Their centrelines nearly coincide.** An external wall is often drawn
        as a brick skin and a frame side by side, and those are two real walls
        whose bands can touch. Two readings of the *same* wall sit on the same
        line.
    *   **They measure nearly the same thickness.** A 79 mm reading and a
        172 mm reading in the same place are not one wall read twice; one of
        them is something else, and merging them loses whichever it was. On a
        plan drawn as a picture, merging without this test cost five of its ten
        openings.
    """
    if _overlap(_band(first), _band(second)) <= 0:
        return False

    first_low, first_high = _band(first)
    second_low, second_high = _band(second)
    apart_mm = abs((first_low + first_high) / 2 - (second_low + second_high) / 2) * mm_per_point
    thinner = min(first["thickness_mm"], second["thickness_mm"])
    share = float(settings.get("merge_centre_share_of_thickness", 0.5))
    if apart_mm > thinner * share:
        return False

    tolerance = float(settings.get("merge_thickness_tolerance_mm", 40))
    return abs(first["thickness_mm"] - second["thickness_mm"]) <= tolerance


# How much of a cluster's own run a candidate must cover to count as a reading
# of that wall rather than a fragment of it. Not a setting: it is the line
# between "these two are the same wall" and "this one is a piece of that one",
# and no office draws that differently.
_COVERS_THE_CLUSTER = 0.75


def _best_candidate(members: list) -> dict:
    """The one of several overlapping candidates that most looks like a wall.

    A candidate the sheet's own dimensions cannot account for is a boundary or
    an eave line rather than a wall (see the length check above), so one that
    fits is preferred over one that does not even when the one that does not is
    longer. After that: a thickness the office actually builds, then length.
    """
    if len(members) == 1:
        return members[0]

    every_break = [
        (start, end) for member in members for start, end in member.get("gaps_pt", [])
    ]

    # **A fragment of a wall is not a reading of that wall.** Once a stretch as
    # short as a nib could be a candidate, a cluster could hold an 8 m wall and
    # a 0.3 m piece of the same wall - and the ranking below, which prefers a
    # thickness the office builds over length, would keep the 0.3 m piece and
    # throw the wall away. On two floor plans that cost about 30 m of traced
    # wall each while the wall count went up, which is the worst shape a change
    # can take: it looks like more and is less.
    #
    # So the choice is made among the candidates that actually cover this
    # cluster. Everything the ranking already knew - the sheet's own length
    # limit, a thickness the office builds, the breaks that are its doors -
    # still decides between them.
    runs = [_run(member) for member in members]
    span = max(high for _low, high in runs) - min(low for low, _high in runs)
    covering = [
        member
        for member, (low, high) in zip(members, runs)
        if span <= 0 or (high - low) >= span * _COVERS_THE_CLUSTER
    ]
    members = covering or members

    def rank(wall: dict):
        low, high = _run(wall)
        # A copy that covers the openings is a better record of this wall than
        # one that stops short of them: the break in a wall is a door, and
        # keeping a copy that does not reach it throws that door away. On a
        # plan drawn as a picture, ranking on length alone lost five of its ten
        # openings.
        kept = sum(1 for start, end in every_break if start >= low and end <= high)
        return (
            0 if wall.get("longer_than_sheet_measures") else 1,
            1 if wall.get("matches_nominal_thickness") else 0,
            kept,
            wall["length_mm"],
        )

    best = max(members, key=rank)
    low, high = _run(best)

    breaks = []
    for start, end in every_break:
        start, end = max(start, low), min(end, high)
        if end > start:
            breaks.append([round(start, 2), round(end, 2)])
    best["gaps_pt"] = _combine_breaks(breaks)
    best["merged_from"] = len(members)
    return best


def _combine_breaks(breaks: list) -> list:
    """Breaks recorded by several copies of one wall, as one list.

    The same door recorded by two copies is one door, so overlapping breaks are
    joined rather than reported twice.
    """
    if not breaks:
        return []
    breaks.sort()
    combined = [list(breaks[0])]
    for start, end in breaks[1:]:
        if start <= combined[-1][1]:
            combined[-1][1] = max(combined[-1][1], end)
        else:
            combined.append([start, end])
    return [[round(s, 2), round(e, 2)] for s, e in combined]


# --- a building's walls hold each other up -------------------------------


def _touching(first: dict, second: dict, slack: float) -> bool:
    """Whether these two walls meet, within a tolerance in points."""
    a, b = first["bbox"], second["bbox"]
    return not (
        a[2] + slack < b[0]
        or b[2] + slack < a[0]
        or a[3] + slack < b[1]
        or b[3] + slack < a[1]
    )


def mark_walls_that_stand_alone(walls: list, mm_per_point: float, config: dict) -> int:
    """Flags every candidate that meets no other candidate.

    **A building's walls form one connected outline.** They meet at corners and
    at junctions; that is what makes them a building rather than a collection
    of lines. A pair of parallel lines touching nothing else is something else
    on the drawing — an eave, a roof extent, a fence, a bench, a leader line.

    This is a fact about buildings rather than a threshold to tune, and it is
    what separates the walls of the plan from the lines drawn around them. It
    is recorded on the candidate rather than used to delete it: the reader sees
    it in the table with the reason, and it is left out of the marked-up sheet
    and the model, where a line that is not a wall does real harm.

    Returns how many were flagged.
    """
    settings = config.get("walls", {})
    if not settings.get("require_walls_to_meet", True):
        return 0
    # Two walls that meet may still be drawn a wall's thickness apart at the
    # corner, so the reach is the thickest wall this office builds.
    standards = settings.get("nominal_thickness_mm", []) or [300]
    slack = float(settings.get("meeting_slack_mm", max(float(s) for s in standards))) / mm_per_point

    # A drawing with almost nothing on it cannot be judged this way: with two
    # or three candidates there is no network to be part of.
    if len(walls) < int(settings.get("min_walls_to_judge_meeting", 4)):
        for wall in walls:
            wall["meets_another_wall"] = True
        return 0

    # **Touching one other line is not being part of a building.** Two lines
    # of a legend row touch each other; so do the two sides of a car drawn in
    # a garage, the pair of witness lines beside a dimension figure, and a
    # cupboard drawn against a bench. Every one of those passed a test that
    # only asked "does anything touch this", and every one of them appeared on
    # the marked-up sheet as a wall.
    #
    # A building is a *connected group*: its walls reach each other, corner to
    # corner and partition to wall, all the way round. So the walls are grouped
    # into everything that reaches everything else, and a group too small to
    # enclose anything is not part of the building. A closed shape takes four
    # walls, which is why that is the size — and it is a fact about shapes, not
    # a threshold to tune. A genuinely separate structure on the same sheet, a
    # detached garage or a shed, is its own group of four or more and survives.
    smallest_group = int(settings.get("min_walls_in_a_group", 4))
    groups = _connected_groups(walls, slack)

    # **A sheet with no connected building cannot be judged this way.** On a
    # slab setout plan the walls are drawn sparsely and none of them reaches
    # another, so every group is of one or two — and applying the rule threw
    # away all 19 of that sheet's walls. The rule says "this group is too small
    # to be part of the building", which means nothing when no group on the
    # sheet is a building. Where the biggest group is itself too small, nothing
    # is judged and every candidate is reported as it was found.
    if max((len(group) for group in groups), default=0) < smallest_group:
        for wall in walls:
            wall["meets_another_wall"] = True
            wall["wall_group_size"] = None
        return 0

    # **A small group of long walls is still part of a building.** Counting
    # alone said otherwise, and on a sheet whose drawing is stored as a picture
    # - where the tracing recovers the walls in pieces rather than as one
    # connected outline - it threw away 32 m of real wall. What a legend row,
    # a car in a garage and a cupboard have in common is not only that they are
    # in a small group: it is that everything in that group is *short*. A wall
    # metres long, joined to another wall metres long, is a building however
    # little else of it was traced.
    long_enough_alone = float(settings.get("min_lone_wall_length_mm", 0.0))

    alone = 0
    for group in groups:
        longest = max(walls[index]["length_mm"] for index in group)
        # A candidate meeting nothing at all is never part of the building,
        # whatever its length — that is the eave, the roof extent, the fence
        # and the block boundary, and every one of them is long. What the
        # length allows for is a *pair* of long walls that meet each other on a
        # sheet where the rest of the outline was not traced.
        big_enough = len(group) >= smallest_group or (
            len(group) >= 2 and longest >= long_enough_alone
        )
        for index in group:
            wall = walls[index]
            wall["meets_another_wall"] = big_enough
            wall["wall_group_size"] = len(group)
            if not big_enough:
                alone += 1
                wall["confidence"] = round(min(wall["confidence"], 0.4), 3)
                wall["confidence_band"] = "review"
    return alone


def _connected_groups(walls: list, slack: float) -> list:
    """The walls grouped into everything that reaches everything else."""
    parent = list(range(len(walls)))

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for i in range(len(walls)):
        for j in range(i + 1, len(walls)):
            if _touching(walls[i], walls[j], slack):
                parent[root(i)] = root(j)

    groups: dict = {}
    for index in range(len(walls)):
        groups.setdefault(root(index), []).append(index)
    return list(groups.values())


# --- Step 6, before pairing: lines that are not wall faces ----------------


def _drop_lines_that_are_not_wall_faces(
    segments: list, widths: list, settings: dict
) -> list:
    """Drops the drawn lines that can never be the edge of a wall.

    **A dimension line is drawn thin.** So is a leader, a hatch stroke and a
    centre line. That is a drafting convention rather than an opinion about a
    particular office, and where the PDF states a stroke width it is the
    cheapest and most reliable way to tell those apart from the edge of a
    wall — before any pairing, so they cannot become half of a false wall.

    A stroke width of zero is not thin, it is **unstated**: a filled shape
    carries none, and so does a line recovered from a page read as a picture.
    Those are kept and judged on everything else. Refusing them would throw
    away every wall on a sheet whose drawing is stored as an image.
    """
    thinner_than = float(_setting(settings, "reject_lines_thinner_than_pt", default=0.0))
    if thinner_than <= 0 or not widths or len(widths) != len(segments):
        return segments
    return [
        segment
        for segment, width in zip(segments, widths)
        if not (0.0 < width < thinner_than)
    ]


def _drop_hatching(faces: list, mm_per_point: float, settings: dict) -> list:
    """Drops runs of faces packed closer together than a wall is ever built.

    **Hatching is many parallel strokes a short distance apart.** So is the
    brick coursing on a section, the boarding on a soffit and the tiling on a
    wet-area plan. Two of those strokes are two parallel lines a plausible
    distance apart as readily as a wall's own two faces are, and one sheet can
    carry hundreds of them.

    What separates hatching from a wall is not any one pair — it is that there
    are *many* of them in one band, each closer to the next than the thinnest
    wall the office builds. So a run of faces whose neighbours are all closer
    together than that, and which is longer than a wall's two faces plus its
    linings, is hatching, and every face in it is dropped.
    """
    min_separation = float(_setting(settings, "min_face_separation_mm", default=0.0))
    most_faces = int(_setting(settings, "max_faces_in_a_hatch_band", default=0))
    if min_separation <= 0 or most_faces <= 0 or len(faces) <= most_faces:
        return faces

    separation_pt = min_separation / mm_per_point
    ordered = sorted(range(len(faces)), key=lambda i: faces[i][0])

    dropped: set = set()
    band = [ordered[0]]
    for index in ordered[1:]:
        previous, current = faces[band[-1]], faces[index]
        overlaps = min(previous[2], current[2]) > max(previous[1], current[1])
        if overlaps and (current[0] - previous[0]) < separation_pt:
            band.append(index)
            continue
        if len(band) > most_faces:
            dropped.update(band)
        band = [index]
    if len(band) > most_faces:
        dropped.update(band)

    if not dropped:
        return faces
    return [face for index, face in enumerate(faces) if index not in dropped]


# --- Step 4: where the walls meet each other -----------------------------


def _junction_between(first: dict, second: dict, tolerance: float):
    """Where and how these two walls meet, or None.

    A wall occupies a rectangle: its run along its own axis, and its thickness
    band across it. Two walls at right angles meet when **each one's thickness
    band reaches the other's run**. That is one test covering all three shapes
    an office draws — a corner (L), a partition landing on a wall (T) and two
    partitions crossing (+) — and which of the three it is follows from whether
    the meeting lands at the end of each wall or partway along it, so the shape
    is read off the drawing rather than assumed.

    Two walls on the same axis meet when they sit on the same line and their
    ends come within the tolerance of each other. That is one wall continuing
    past a doorway, and the graph needs it: without it a run of wall broken by
    two doors arrives as three separate buildings.
    """
    first_run, second_run = _run(first), _run(second)
    first_band, second_band = _band(first), _band(second)

    if first["runs_along"] != second["runs_along"]:
        horizontal, vertical = (
            (first, second) if first["runs_along"] == "x" else (second, first)
        )
        h_run, h_band = _run(horizontal), _band(horizontal)
        v_run, v_band = _run(vertical), _band(vertical)

        along_x = _overlap_span(h_run, v_band, tolerance)
        along_y = _overlap_span(v_run, h_band, tolerance)
        if along_x is None or along_y is None:
            return None

        ends = int(_lands_at_an_end(h_run, along_x, tolerance)) + int(
            _lands_at_an_end(v_run, along_y, tolerance)
        )
        shape = "L" if ends == 2 else ("T" if ends == 1 else "+")
        point = [
            round((along_x[0] + along_x[1]) / 2.0, 2),
            round((along_y[0] + along_y[1]) / 2.0, 2),
        ]
        return shape, point

    if _overlap(first_band, second_band) <= 0:
        return None
    gap = max(first_run[0], second_run[0]) - min(first_run[1], second_run[1])
    if gap > tolerance:
        return None
    meeting = (min(first_run[1], second_run[1]) + max(first_run[0], second_run[0])) / 2.0
    across = (max(first_band[0], second_band[0]) + min(first_band[1], second_band[1])) / 2.0
    point = (
        [round(meeting, 2), round(across, 2)]
        if first["runs_along"] == "x"
        else [round(across, 2), round(meeting, 2)]
    )
    return "collinear", point


def _overlap_span(run, band, tolerance: float):
    """The stretch of ``run`` that ``band`` reaches, allowing the tolerance."""
    low = max(run[0] - tolerance, band[0])
    high = min(run[1] + tolerance, band[1])
    if high < low:
        return None
    return (low, high)


def _lands_at_an_end(run, where, tolerance: float) -> bool:
    """Whether a meeting lands at an end of a wall rather than partway along."""
    return (where[1] - run[0]) <= tolerance or (run[1] - where[0]) <= tolerance


def _junction_pairs(walls: list, tolerance: float) -> dict:
    """Every pair of walls that meet, keyed by index, with shape and point."""
    found: dict = {}
    for index in range(len(walls)):
        for other in range(index + 1, len(walls)):
            meeting = _junction_between(walls[index], walls[other], tolerance)
            if meeting:
                found[(index, other)] = meeting
    return found


def _junction_tolerance(config: dict) -> float:
    """How far apart two walls may be drawn and still be meeting, in points.

    In points rather than millimetres because it is a tolerance on the paper:
    an office draws a junction closed, and what is being allowed for is line
    weight, tracing and the odd unclosed corner — none of which change with the
    scale the sheet happens to be plotted at.
    """
    return float(
        _setting(config.get("walls", {}), "junction_tolerance_points", default=10.0)
    )


def detect_junctions(walls: list, config: dict) -> int:
    """Records, on every wall, which walls it meets and how.

    Returns how many distinct junctions were found. Each wall gains
    ``connects_to`` — the wall ids it meets — and ``junctions``, the same list
    with the shape of each meeting and the point on the sheet it happens at,
    so no junction is anonymous (Critical Rule 4).
    """
    for wall in walls:
        wall["connects_to"] = []
        wall["junctions"] = []
    if not config.get("walls", {}).get("detect_junctions", True):
        return 0

    pairs = _junction_pairs(walls, _junction_tolerance(config))
    for (index, other), (shape, point) in pairs.items():
        first, second = walls[index], walls[other]
        first["connects_to"].append(second["wall_id"])
        second["connects_to"].append(first["wall_id"])
        first["junctions"].append(
            {"with_wall_id": second["wall_id"], "shape": shape, "at_pt": point}
        )
        second["junctions"].append(
            {"with_wall_id": first["wall_id"], "shape": shape, "at_pt": point}
        )
    return len(pairs)


def _drop_short_walls_that_meet_nothing(walls: list, config: dict) -> list:
    """Short stretches joined to the building are walls; the rest are furniture.

    **This is what lets the length floor come down.** A pier, a return, a nib
    beside a doorway and the partition between a WC and a hall are all real
    walls and all short, so a plain length floor either loses them — which is
    what was happening — or lets in every bench top, wardrobe and step on the
    sheet. A junction tells the two apart: a short stretch running into the
    building is part of it, and a short stretch touching nothing is not.
    """
    connected_floor = float(
        _setting(
            config.get("walls", {}), "min_unconnected_wall_length_mm", default=0.0
        )
    )
    if connected_floor <= 0 or len(walls) < 2:
        return walls

    meets: set = set()
    for index, other in _junction_pairs(walls, _junction_tolerance(config)):
        meets.add(index)
        meets.add(other)

    return [
        wall
        for index, wall in enumerate(walls)
        if wall["length_mm"] >= connected_floor or index in meets
    ]


# --- Step 5: the wall graph ----------------------------------------------


def wall_graph_for(walls: list, sheet_id: str, page_number) -> dict:
    """The walls of one sheet as a graph: walls the nodes, junctions the edges.

    Written out rather than acted on here. Closing this graph into rooms, and
    telling a room from the space outside the building, is what it exists for;
    what is built at this stage is the graph itself, with every edge carrying
    the shape of the meeting and the point on the sheet it happens at.
    """
    edges = []
    seen: set = set()
    for wall in walls:
        for junction in wall.get("junctions", []):
            pair = tuple(sorted((wall["wall_id"], junction["with_wall_id"])))
            if pair in seen:
                continue
            seen.add(pair)
            edges.append(
                {
                    "from_wall_id": pair[0],
                    "to_wall_id": pair[1],
                    "shape": junction["shape"],
                    "at_pt": junction["at_pt"],
                }
            )
    edges.sort(key=lambda edge: (edge["from_wall_id"], edge["to_wall_id"]))

    return {
        "sheet_id": sheet_id,
        "page_number": page_number,
        "nodes": [
            {
                "wall_id": wall["wall_id"],
                "wall_type": wall.get("wall_type", "unknown"),
                "orientation": wall.get("orientation"),
                "length_mm": wall["length_mm"],
                "thickness_mm": wall["thickness_mm"],
                "connects_to": wall.get("connects_to", []),
            }
            for wall in walls
        ],
        "edges": edges,
        "junction_count": len(edges),
        "shapes": {
            shape: sum(1 for edge in edges if edge["shape"] == shape)
            for shape in ("L", "T", "+", "collinear")
        },
    }


# --- outside and inside --------------------------------------------------


def classify_outer_inner(walls: list, config: dict) -> None:
    """Marks every wall as an outer wall, an inner wall, or unknown.

    **A wall with nothing but open paper on one side of it is an outer wall.**
    That is the test, and it is geometry rather than a guess: a ray is cast out
    from each face of the wall, and a face whose rays leave everything drawn
    without crossing another wall is looking at the outside.

    A rectangle round the building cannot do this. On an L-shaped or a U-shaped
    plan — and most detached houses become one once the garage and the alfresco
    are on — the walls in the notch are external and nowhere near the
    rectangle's edge, while the walls that *are* on the edge include every
    internal wall that happens to line up with it.

    A wall that meets no other wall is left ``unknown`` rather than guessed: it
    has not been established as part of this building at all, so saying which
    side of it is outside would be inventing an answer (Critical Rule 5).
    """
    settings = config.get("walls", {})
    for wall in walls:
        wall["wall_type"] = "unknown"
    if not settings.get("classify_outer_inner", True) or len(walls) < 2:
        return

    samples = max(int(_setting(settings, "outer_ray_samples", default=5)), 1)
    clear_share = float(_setting(settings, "outer_ray_clear_share", default=0.6))

    for wall in walls:
        if not wall.get("connects_to"):
            continue
        wall["wall_type"] = (
            "outer" if _has_an_open_side(wall, walls, samples, clear_share) else "inner"
        )


def _has_an_open_side(wall: dict, walls: list, samples: int, clear_share: float) -> bool:
    """Whether rays cast from one face of this wall leave the drawing."""
    run = _run(wall)
    low, high = _band(wall)

    points = [
        run[0] + (run[1] - run[0]) * (position + 1) / (samples + 1)
        for position in range(samples)
    ]

    for direction, face in ((-1, low), (1, high)):
        clear = sum(
            1 for along in points if _ray_is_clear(wall, walls, along, face, direction)
        )
        if clear / len(points) >= clear_share:
            return True
    return False


def _ray_is_clear(wall: dict, walls: list, along: float, face: float, direction: int) -> bool:
    """Whether a ray out from one face of this wall crosses no other wall."""
    for other in walls:
        if other is wall:
            continue
        other_run, other_band = _run(other), _band(other)
        if other["runs_along"] == wall["runs_along"]:
            # Parallel: it blocks the ray when it covers this point along the
            # wall and lies beyond this face on the side the ray travels.
            if not (other_run[0] <= along <= other_run[1]):
                continue
            across_low, across_high = other_band
        else:
            # At right angles: it blocks the ray when it crosses this point and
            # its own run reaches past this face on the side of travel.
            if not (other_band[0] <= along <= other_band[1]):
                continue
            across_low, across_high = other_run
        if direction > 0 and across_high > face:
            return False
        if direction < 0 and across_low < face:
            return False
    return True


# --- Step 7: what a wall record says -------------------------------------


def describe_walls(
    walls: list, mm_per_point: float, config: dict, sheet_id: str, page_number
) -> None:
    """Fills in the plain-words half of every wall record.

    The geometry above is measured in PDF points, because that is what the
    drawing is stored in. This turns each wall into the record every later
    stage and every reader actually reads: the two faces it was measured from,
    its centreline, the breaks in it in millimetres, which walls it meets,
    which sheet and page it came from, and how far it should be trusted.
    """
    thresholds = config.get("confidence_thresholds", {})
    high = float(thresholds.get("review", 0.75))
    medium = float(thresholds.get("low", 0.5))

    for wall in walls:
        run_start, run_end = _run(wall)
        first_face, second_face = sorted(wall["face_positions_pt"])
        horizontal = wall["runs_along"] == "x"

        def line(across_first, across_second=None):
            across_second = across_first if across_second is None else across_second
            if horizontal:
                return {
                    "x0": round(run_start, 2),
                    "y0": round(across_first, 2),
                    "x1": round(run_end, 2),
                    "y1": round(across_second, 2),
                }
            return {
                "x0": round(across_first, 2),
                "y0": round(run_start, 2),
                "x1": round(across_second, 2),
                "y1": round(run_end, 2),
            }

        centre = (first_face + second_face) / 2.0
        wall["orientation"] = "horizontal" if horizontal else "vertical"
        wall["face1"] = line(first_face)
        wall["face2"] = line(second_face)
        wall["centerline"] = line(centre)
        wall["gaps"] = [
            {
                "start": round(low, 2),
                "end": round(high_pt, 2),
                "gap_mm": round((high_pt - low) * mm_per_point, 1),
            }
            for low, high_pt in wall.get("gaps_pt", [])
        ]
        wall["junction_count"] = len(wall.get("connects_to", []))
        wall["source_sheet"] = sheet_id
        wall["source_page"] = page_number
        confidence = wall.get("confidence", 0.0)
        wall["confidence_label"] = (
            "high" if confidence >= high else ("medium" if confidence >= medium else "low")
        )
        # A wall is put in front of a reviewer when anything about it is less
        # than settled — not only a low score. A candidate longer than the
        # sheet measures, one meeting no other wall, and one whose thickness is
        # nothing the office builds are each a reason to look at the drawing.
        # **Nothing is set aside without a reason beside it.** Three different
        # rules can decide a pair of lines is not a wall of this building, and
        # a reader looking at a table row that says "not used" is owed which
        # one it was (Critical Rule 5).
        wall["not_used_because"] = None
        if not wall.get("meets_another_wall", True):
            if wall.get("inside_the_drawing") is False:
                wall["not_used_because"] = (
                    "This is outside the part of the sheet the plan is drawn on, so it "
                    "is a dimension line rather than a wall."
                )
            elif wall.get("wall_group_size") == 1:
                wall["not_used_because"] = (
                    "This pair of lines meets no other wall. A building's walls hold on "
                    "to each other, so this is more likely an eave, a roof line, a fence "
                    "or a boundary."
                )
            else:
                wall["not_used_because"] = (
                    "This is part of a small group of short lines that encloses nothing, "
                    "so it is joinery, furniture or a panel on the sheet rather than part "
                    "of the building."
                )
        elif wall.get("trimmed_to_the_drawing"):
            wall["not_used_because"] = None

        wall["review_needed"] = bool(
            wall["confidence_label"] != "high"
            or wall.get("longer_than_sheet_measures")
            or not wall.get("meets_another_wall", True)
            or not wall.get("matches_nominal_thickness")
        )


def walls_as_records(walls: list) -> list:
    """The walls of one sheet in the shape ``walls.json`` is written in.

    A record that says what was found, where it was found and how far to trust
    it — and nothing about how this module happens to store a face internally.
    """
    return [
        {
            "wall_id": wall["wall_id"],
            "wall_type": wall.get("wall_type", "unknown"),
            "orientation": wall.get("orientation"),
            "face1": wall.get("face1"),
            "face2": wall.get("face2"),
            "centerline": wall.get("centerline"),
            "gaps": wall.get("gaps", []),
            "length_mm": wall["length_mm"],
            "thickness_mm": wall["thickness_mm"],
            "nominal_thickness_mm": wall.get("nominal_thickness_mm"),
            "connects_to": wall.get("connects_to", []),
            "junctions": wall.get("junctions", []),
            "source_sheet": wall.get("source_sheet"),
            "source_page": wall.get("source_page"),
            "source_bbox": wall.get("bbox"),
            "measured_from": wall.get("line_source"),
            "longer_than_sheet_measures": wall.get("longer_than_sheet_measures", False),
            "meets_another_wall": wall.get("meets_another_wall", True),
            "not_used_because": wall.get("not_used_because"),
            "trimmed_to_the_drawing": bool(wall.get("trimmed_to_the_drawing")),
            "confidence": wall.get("confidence_label", "low"),
            "confidence_score": wall.get("confidence"),
            "review_needed": wall.get("review_needed", True),
            "review_status": wall.get("review_status", "needs_review"),
        }
        for wall in walls
    ]


# --- letters are not walls -------------------------------------------------


def _drop_lettering(segments: list, axis: str, text_boxes, settings: dict) -> list:
    """Drops the drawn lines that are actually printed words.

    **The single largest source of false walls, and it is worst exactly where
    the reading is weakest.** A sheet whose drawing is stored as a picture has
    its lines recovered by looking for continuous runs of dark pixels — and a
    word set in capitals is a continuous run of dark pixels. So a room name
    became a horizontal line, the top and bottom of the same word became two
    parallel lines a plausible wall thickness apart, and every room label on
    the plan was reported as a wall lying across the middle of its own room.
    On one floor plan 32 of 157 walls were printed words.

    On a sheet stored as line work the same thing happens more quietly: an
    abbreviations list, a materials schedule and a notes column are ruled into
    rows, and those rules are parallel lines a few millimetres apart at drawing
    scale — the same trap the title block was already excluded for.

    **The test is containment, not overlap.** A line is lettering when it runs
    from end to end inside the box of one printed line of text. A real wall
    passing under a room label is many times longer than the label, so only a
    fraction of it is inside and it is kept — which matters, because a plan
    prints its room names on top of the rooms, and every wall of that room
    passes near one.
    """
    if not text_boxes or not settings.get("drop_lettering", True):
        return segments

    padding = float(settings.get("lettering_padding_pt", 1.0))
    inside_share = float(settings.get("lettering_inside_share", 0.8))
    min_span_share = float(settings.get("lettering_min_share_of_the_word", 0.0))

    # A line can only be lettering if it lies within a text box's own extent,
    # so the boxes are indexed across the axis the lines run along and only the
    # few that could contain a given line are ever looked at.
    across = 3 if axis == "x" else 2  # the box side the line's position sits in
    low_side = 1 if axis == "x" else 0
    boxes_by_band: dict = {}
    for box in text_boxes:
        for band in range(
            int((box[low_side] - padding) // _TEXT_BAND_PT),
            int((box[across] + padding) // _TEXT_BAND_PT) + 1,
        ):
            boxes_by_band.setdefault(band, []).append(box)

    kept = []
    for position, start, end in segments:
        span = end - start
        if span <= 0:
            kept.append((position, start, end))
            continue
        lettering = False
        for box in boxes_by_band.get(int(position // _TEXT_BAND_PT), ()):
            if not (box[low_side] - padding <= position <= box[across] + padding):
                continue
            if axis == "x":
                low, high = box[0] - padding, box[2] + padding
            else:
                low, high = box[1] - padding, box[3] + padding
            # **One box, not several added together.** A line covered by three
            # text boxes end to end is a line with words printed along it, not
            # a word. Adding their coverage up said otherwise.
            #
            # **And the line has to be about the size of the word.** A drawn
            # line is the outline of the lettering only if it is as long as the
            # lettering is; a short line sitting inside a long printed note is
            # a piece of the drawing with a note printed over it. Without this,
            # a slab setout plan that prints its notes right across the slab
            # lost 20 of its 25 walls.
            if (min(end, high) - max(start, low)) / span < inside_share:
                continue
            if span < (high - low) * min_span_share:
                continue
            lettering = True
            break
        if not lettering:
            kept.append((position, start, end))
    return kept


# How wide a band the text boxes are bucketed into, in points. Only an index:
# it changes how fast the search is, never which lines it finds.
_TEXT_BAND_PT = 20.0


# --- the building is drawn between its dimension strings -------------------


def drawing_region(rooms: list, chains: list, page_width: float, page_height: float):
    """The part of the sheet the building itself is drawn on.

    **The false wall this exists to stop.** A dimension string is printed
    outside the thing it measures, and from each figure a thin witness line
    runs back to the feature it dimensions. Two witness lines belonging to a
    90 mm dimension are two parallel lines 90 mm apart — which is a wall, by
    every test applied up to here — and they are drawn on the same line as the
    real wall face they measure to, so they merge with it into one face. That
    is how a 20 m house came to report a 17.9 m wall running from the top
    dimension string, down through the building, and out to the bottom one.

    **The rule is a drafting convention, not a threshold.** A building is drawn
    *between* its dimension strings; it is never drawn through them. So the
    strings printed outside the plan bound the region the building occupies,
    and nothing outside that region is a wall.

    Two things keep it safe:

    *   **Only strings printed outside the rooms are used.** A plan may print a
        dimension string across the middle of itself, and using that one would
        cut the building in half. A string overlapping the area where the room
        names are printed is an internal string and is left out of this.
    *   **Where there is nothing to work from it does not apply.** A sheet with
        no room labels, or with no dimension string outside them, returns the
        whole page — the rule then removes nothing rather than guessing at
        where the building is.

    Returns (x0, y0, x1, y1) in points.
    """
    whole_page = (0.0, 0.0, page_width, page_height)
    boxes = [room["bbox"] for room in rooms if room.get("bbox")]
    if len(boxes) < 2 or not chains:
        return whole_page

    inside = (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )

    low_x, high_x = _clear_interval(
        inside[0], inside[2], _bands(chains, "y", inside, 0, 2), page_width
    )
    low_y, high_y = _clear_interval(
        inside[1], inside[3], _bands(chains, "x", inside, 1, 3), page_height
    )
    return (low_x, low_y, high_x, high_y)


def _bands(chains: list, axis: str, inside, low_side: int, high_side: int) -> list:
    """Where each outside dimension string sits, across its own direction.

    ``axis`` is what the string measures; a string measuring across the sheet
    is printed above or below the plan, so it bounds the building vertically.
    """
    bands = []
    for chain in chains:
        if chain.get("axis") != axis or chain.get("member_count", 0) < 2:
            continue
        box = chain.get("bbox")
        if not box:
            continue
        # A string printed over the plan is an internal dimension. Using it
        # would cut the building in half, so it is left out.
        if box[high_side] > inside[low_side] and box[low_side] < inside[high_side]:
            continue
        bands.append((box[low_side], box[high_side]))
    return bands


def _clear_interval(inside_low: float, inside_high: float, bands: list, page_extent: float):
    """The stretch either side of the rooms that no dimension string reaches."""
    low, high = 0.0, page_extent
    for band_low, band_high in bands:
        if band_high <= inside_low:
            low = max(low, band_high)
        elif band_low >= inside_high:
            high = min(high, band_low)
    return low, high


def trim_walls_to_the_drawing(walls: list, region, mm_per_point: float, config: dict):
    """Cuts every candidate back to the part of the sheet the plan is drawn on.

    **Cut, not thrown away, and that distinction is the whole point.** A
    dimension string's witness line is drawn from the feature it measures out
    to where the figure is printed, and it lies on the same line as the wall
    face it measures to — so the two merge into one candidate that is half real
    wall and half witness line. Rejecting it loses the real half: on one floor
    plan five such candidates carried 4.2 m of genuine wall each. Keeping it
    whole reports a wall running out of the building. Cutting it at the edge of
    the drawing keeps exactly what was drawn as a wall.

    A candidate lying wholly outside is left in the list with the reason, kept
    off the marked-up sheet and out of the model (Critical Rule 5). One left
    too short to be a wall after cutting goes the same way.

    Returns (trimmed, dropped).
    """
    settings = config.get("walls", {})
    if not region or not settings.get("require_walls_inside_the_plan", True):
        return 0, 0

    floor = float(_setting(settings, "min_wall_length_mm", default=200))
    x0, y0, x1, y1 = region
    trimmed = dropped = 0

    for wall in walls:
        horizontal = wall["runs_along"] == "x"
        band_low, band_high = _band(wall)
        run_low, run_high = _run(wall)
        across_low, across_high = (y0, y1) if horizontal else (x0, x1)
        along_low, along_high = (x0, x1) if horizontal else (y0, y1)

        wall["inside_the_drawing"] = True
        # Across its own thickness a wall is either on the plan or it is not;
        # there is nothing to cut.
        if band_high < across_low or band_low > across_high:
            wall["inside_the_drawing"] = False
            dropped += 1
            _set_aside(wall)
            continue

        kept_low, kept_high = max(run_low, along_low), min(run_high, along_high)
        if kept_high - kept_low <= 0:
            wall["inside_the_drawing"] = False
            dropped += 1
            _set_aside(wall)
            continue
        if (kept_low, kept_high) == (run_low, run_high):
            continue

        length_mm = (kept_high - kept_low) * mm_per_point
        if length_mm < floor:
            wall["inside_the_drawing"] = False
            dropped += 1
            _set_aside(wall)
            continue

        _recut(wall, kept_low, kept_high, length_mm)
        wall["trimmed_to_the_drawing"] = True
        trimmed += 1

    return trimmed, dropped


def trim_free_tails(walls: list, rooms: list, mm_per_point: float, config: dict) -> int:
    """Cuts off the stretch of a wall that runs past the building into nothing.

    **What this is for.** A dimension string is printed clear of the plan, and
    the witness line running back from each figure lies on the same line as the
    wall face it measures to. The two merge into one candidate that is a real
    wall for part of its length and a witness line for the rest — so it is
    drawn crossing the building's outside wall and carrying on into the margin.
    Cutting it to the edge of the drawing helps, but the edge of the drawing is
    the dimension string itself, and the tail between the building and the
    string survives.

    **The rule is what a wall's end can be.** A wall ends where it meets
    another wall, or at the outside of the building. It never ends in the
    middle of empty paper outside the plan. So the stretch beyond a wall's
    outermost junction is cut off — but *only where that stretch is outside the
    area the rooms are printed in*, because inside the building a wall
    genuinely can stop free: at a doorway, at the end of a nib, at a return.
    That is the distinction that makes this safe.
    """
    settings = config.get("walls", {})
    if not settings.get("trim_free_tails", True) or not rooms:
        return 0

    boxes = [room["bbox"] for room in rooms if room.get("bbox")]
    if len(boxes) < 2:
        return 0
    inside = (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )
    shortest_tail = float(settings.get("shortest_free_tail_mm", 600)) / mm_per_point
    floor = float(_setting(settings, "min_wall_length_mm", default=200))

    trimmed = 0
    for wall in walls:
        junctions = wall.get("junctions") or []
        if not junctions:
            continue
        horizontal = wall["runs_along"] == "x"
        along = 0 if horizontal else 1
        low, high = _run(wall)
        met = [junction["at_pt"][along] for junction in junctions]
        first, last = min(met), max(met)

        kept_low = first if (first - low) > shortest_tail and _outside(
            wall, low, first, inside, horizontal
        ) else low
        kept_high = last if (high - last) > shortest_tail and _outside(
            wall, last, high, inside, horizontal
        ) else high
        if (kept_low, kept_high) == (low, high):
            continue

        length_mm = (kept_high - kept_low) * mm_per_point
        if length_mm < floor:
            continue
        _recut(wall, kept_low, kept_high, length_mm)
        wall["trimmed_to_the_drawing"] = True
        trimmed += 1
    return trimmed


def _outside(wall: dict, low: float, high: float, inside, horizontal: bool) -> bool:
    """Whether this stretch of the wall lies clear of where the rooms print."""
    if horizontal:
        return high <= inside[0] or low >= inside[2]
    return high <= inside[1] or low >= inside[3]


def _set_aside(wall: dict) -> None:
    """Keeps a candidate in the list but out of the drawing and the model."""
    wall["meets_another_wall"] = False
    wall["confidence"] = round(min(wall["confidence"], 0.35), 3)
    wall["confidence_band"] = "review"


def _recut(wall: dict, low: float, high: float, length_mm: float) -> None:
    """Moves a wall's ends to ``low`` and ``high`` along its own axis."""
    horizontal = wall["runs_along"] == "x"
    centre = sum(wall["face_positions_pt"]) / 2.0
    if horizontal:
        wall["start_point_pt"] = [round(low, 2), round(centre, 2)]
        wall["end_point_pt"] = [round(high, 2), round(centre, 2)]
        wall["bbox"] = [round(low, 2), wall["bbox"][1], round(high, 2), wall["bbox"][3]]
    else:
        wall["start_point_pt"] = [round(centre, 2), round(low, 2)]
        wall["end_point_pt"] = [round(centre, 2), round(high, 2)]
        wall["bbox"] = [wall["bbox"][0], round(low, 2), wall["bbox"][2], round(high, 2)]
    wall["length_mm"] = round(length_mm, 1)
    # A break that was in the part cut away is not a door in what is left.
    wall["gaps_pt"] = [
        [max(start, low), min(end, high)]
        for start, end in wall.get("gaps_pt", [])
        if min(end, high) > max(start, low)
    ]


# --- Fix 1: a break where another wall lands is a junction, not a door -----


def break_is_a_junction(
    wall: dict, low: float, high: float, walls: list, slack: float, share: float = 0.6
) -> bool:
    """Whether this break in a wall is where another wall runs into it.

    **Both look identical in the geometry, and they mean opposite things.** A
    door stops both faces of its wall and leaves a hole; a partition landing on
    a wall stops both faces of it too, because the drawing runs the partition's
    own faces through. One is an opening to be cut and counted; the other is
    solid wall with a wall attached.

    What separates them is what is on the other side: a wall. So a break with a
    wall of its own running into it, across it, is a junction and no opening is
    reported there.
    """
    width = high - low
    if width <= 0:
        return False
    for other in walls:
        if other is wall or other["runs_along"] == wall["runs_along"]:
            continue
        band_low, band_high = _band(other)
        # **A junction break is about as wide as the wall that made it.** A
        # partition 90 mm thick landing on a wall breaks it for 90 mm; a door
        # breaks it for 820 mm, and a partition happening to arrive at one jamb
        # of that door does not make the door a junction. Requiring the wall
        # running in to account for most of the break is what separates them,
        # and without it a plan drawn as a picture - where the walls are dense
        # and every opening has a partition near it - lost every opening it had.
        covered = min(band_high, high) - max(band_low, low)
        if covered <= 0 or covered / width < share:
            continue
        # And that wall has to actually reach this one, not merely line up
        # with the break somewhere else on the sheet.
        other_low, other_high = _run(other)
        across_low, across_high = _band(wall)
        if other_high < across_low - slack or other_low > across_high + slack:
            continue
        return True
    return False


# --- Fix 2: what is drawn beyond the building -----------------------------


def building_outline(walls: list, config: dict):
    """The box the building's own connected walls occupy, or None.

    **The outline is the walls that hold each other up.** Everything that
    belongs to the building is joined to the rest of it, so the largest group
    of walls that reach each other *is* the building, and its extent is the
    building's extent. Nothing outside it is a wall of this building — not the
    eave line, not the block boundary, not the roof extent drawn round it.

    Returned as None where no group is big enough to be a building, so a sheet
    that traced very little loses nothing.
    """
    settings = config.get("walls", {})
    if not settings.get("reject_outside_the_building", True) or not walls:
        return None
    standards = settings.get("nominal_thickness_mm", []) or [300]
    slack_mm = float(settings.get("meeting_slack_mm", max(float(s) for s in standards)))
    thickness = max(float(s) for s in standards)
    smallest = int(settings.get("min_walls_in_a_group", 4))

    # The same reach the junction reader uses, in points, from any wall on the
    # sheet — every wall here shares one scale.
    slack = slack_mm / max(_mm_per_point_of(walls, thickness), 1e-6)
    groups = _connected_groups(walls, slack)
    biggest = max(groups, key=len) if groups else []
    # **The outline is only usable when one group really is the building.** On
    # a sheet whose drawing is stored as a picture the tracing recovers the
    # walls in several pieces, and the biggest of those pieces is not the
    # building - taking its box as the outline set aside 18 of that sheet's 38
    # walls for being "outside the building". So it takes a clear majority
    # before anything is judged against it.
    share = float(settings.get("outline_share_of_the_walls", 0.4))
    if len(biggest) < smallest or len(biggest) < len(walls) * share:
        return None

    # Built from the walls whose thickness was measured. A wall kept from a
    # lone face has an assumed thickness and is only there because it meets
    # others, so it can never define where the building's edge is.
    boxes = [
        walls[index]["bbox"]
        for index in biggest
        if not walls[index].get("thickness_is_assumed")
    ] or [walls[index]["bbox"] for index in biggest]
    # A wall may sit a little outside the box its neighbours make - an entry
    # return, a porch pier - so the outline is given the reach a junction has.
    outline = (
        min(b[0] for b in boxes) - slack,
        min(b[1] for b in boxes) - slack,
        max(b[2] for b in boxes) + slack,
        max(b[3] for b in boxes) + slack,
    )

    # **A narrow strip is not a building.** Counting the walls in a group is
    # not enough: on a sheet drawn as a picture the biggest connected piece was
    # twenty walls all lying along one wall of the house, and its box was a
    # band 350 by 90 points on a drawing 770 points across — so half the real
    # walls were "outside the building". The box has to look like a building
    # before anything is measured against it.
    every = [wall["bbox"] for wall in walls]
    whole = (
        min(b[0] for b in every), min(b[1] for b in every),
        max(b[2] for b in every), max(b[3] for b in every),
    )
    whole_area = (whole[2] - whole[0]) * (whole[3] - whole[1])
    outline_area = (outline[2] - outline[0]) * (outline[3] - outline[1])
    least = float(settings.get("outline_share_of_the_drawing", 0.25))
    if whole_area > 0 and outline_area / whole_area < least:
        return None
    return outline


def _mm_per_point_of(walls: list, fallback_thickness: float) -> float:
    """Millimetres per point, recovered from a wall's own measured thickness."""
    for wall in walls:
        low, high = _band(wall)
        span = high - low
        if span > 0 and wall.get("thickness_mm"):
            return wall["thickness_mm"] / span
    return fallback_thickness


def text_bands_to_avoid(lines: list, config: dict) -> list:
    """The parts of the sheet that a note says are not the building.

    A plan labels what it draws round the building: *extent of roof*, *eave
    over*, *skillion*, *site boundary*. Those words are printed **on** the
    lines they name, and those lines are parallel pairs at a plausible wall
    thickness — the eave line and the boundary drawn together were being paired
    into a wall longer than the house.

    The words are configuration, because every office writes them differently,
    and the reach is in points because it is a distance on the paper.
    """
    settings = config.get("walls", {})
    words = [
        str(word).upper()
        for word in settings.get("not_the_building_words", [])
        if str(word).strip()
    ]
    if not words:
        return []
    reach = float(settings.get("not_the_building_reach_pt", 150))

    bands = []
    for line in lines or []:
        text = (line.get("text") or "").upper()
        if not any(word in text for word in words):
            continue
        x0, y0, x1, y1 = line["bbox"]
        bands.append((x0 - reach, y0 - reach, x1 + reach, y1 + reach))
    return bands


def mark_walls_in_dead_ground(walls: list, outline, bands: list, panels: list) -> int:
    """Sets aside every candidate outside the building or on something else.

    Three separate places a pair of parallel lines is not a wall, all handled
    the same way and all reported with the reason: outside the building's own
    outline; inside the reach of a note saying the line is a roof, an eave or a
    boundary; and inside a panel printed on the sheet — a legend, a schedule, a
    notes block — which is ruled into rows a few millimetres apart at drawing
    scale, exactly like a wall.
    """
    set_aside = 0
    for wall in walls:
        box = wall["bbox"]
        reason = None
        if outline and (
            box[2] < outline[0] or box[0] > outline[2]
            or box[3] < outline[1] or box[1] > outline[3]
        ):
            reason = (
                "This is outside the outline of the building, so it is a boundary, an "
                "eave line or a roof extent rather than a wall."
            )
        elif any(_inside(box, band) for band in bands):
            reason = (
                "The sheet prints a note here saying this line is a roof, an eave or a "
                "boundary, so it is not a wall."
            )
        elif any(_inside(box, panel) for panel in panels):
            reason = (
                "This is inside a panel printed on the sheet - a legend, a schedule or "
                "a block of notes - whose ruled rows look like a wall at drawing scale."
            )
        if reason:
            set_aside += 1
            wall["meets_another_wall"] = False
            wall["not_used_because"] = reason
            wall["confidence"] = round(min(wall.get("confidence", 0.5), 0.35), 3)
            wall["confidence_band"] = "review"
    return set_aside


def _inside(box, region) -> bool:
    return (
        region[0] <= box[0] and box[2] <= region[2]
        and region[1] <= box[1] and box[3] <= region[3]
    )


# --- Fix 3: a face whose partner was never found ---------------------------


def _nearest_nominal(thickness_mm, standards: list):
    if not standards:
        return None
    return float(min(standards, key=lambda s: abs(float(s) - (thickness_mm or 0))))


def walls_from_lone_faces(
    faces: list,
    used: set,
    axis: str,
    mm_per_point: float,
    config: dict,
    standards: list,
) -> list:
    """Walls for the drawn faces that never found a partner.

    **A wall drawn with one of its faces missing is still a wall.** It happens
    constantly: the other face is hidden behind a cupboard, drawn as part of
    the joinery, broken into pieces too short to merge, or simply not drawn
    where the plan cuts through a bulkhead. Dropping the face lost the wall
    entirely, which is worse than reporting it with a thickness that has to be
    assumed.

    Two things keep this honest:

    *   **It only applies to a face that meets the building.** A lone line
        touching nothing is a leader, a hatch boundary or a fold mark. A lone
        line running into two walls is a wall with a face missing.
    *   **The assumed thickness is said out loud.** The nearest thickness the
        office actually builds is used, the record carries
        ``thickness_is_assumed``, and it says so on screen and in the
        spreadsheet. Nothing pretends to have measured it.
    """
    settings = config.get("walls", {})
    if not settings.get("keep_lone_faces", True):
        return []

    min_length_mm = float(_setting(settings, "min_wall_length_mm", default=600))
    assumed = _nearest_nominal(
        float(settings.get("assumed_thickness_mm", 90)), standards
    ) or float(settings.get("assumed_thickness_mm", 90))
    half = assumed / mm_per_point / 2.0

    walls = []
    for index, (position, start, end, gaps) in enumerate(faces):
        if index in used:
            continue
        length_mm = (end - start) * mm_per_point
        if length_mm < min_length_mm:
            continue
        if axis == "x":
            start_point, end_point = [start, position], [end, position]
        else:
            start_point, end_point = [position, start], [position, end]
        walls.append(
            {
                "wall_id": "",
                "runs_along": axis,
                "length_mm": round(length_mm, 1),
                "thickness_mm": assumed,
                "thickness_is_assumed": True,
                "nominal_thickness_mm": assumed,
                "thickness_difference_mm": 0.0,
                "matches_nominal_thickness": True,
                "start_point_pt": [round(v, 2) for v in start_point],
                "end_point_pt": [round(v, 2) for v in end_point],
                "face_positions_pt": [
                    round(position - half, 2), round(position + half, 2)
                ],
                "bbox": (
                    [round(start, 2), round(position - half, 2),
                     round(end, 2), round(position + half, 2)]
                    if axis == "x"
                    else [round(position - half, 2), round(start, 2),
                          round(position + half, 2), round(end, 2)]
                ),
                "line_source": "",
                "confidence": 0.45,
                "confidence_band": "review",
                "review_status": "auto_confirmed",
                "merged_from": 1,
                "meets_another_wall": True,
                "linked_opening_marks": [],
                "gaps_pt": [[round(low, 2), round(high, 2)] for low, high in gaps],
            }
        )
    return walls
