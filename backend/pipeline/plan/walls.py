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
    return pairs


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

    # An external wall is drawn with more lines than its own two faces, so the
    # pairing above produces several overlapping candidates for one wall. Two
    # solids cannot occupy the same space, so those are one wall and are
    # reported once, keeping every break any of them recorded.
    before = len(walls)
    walls = merge_overlapping_walls(walls, mm_per_point, config)
    if before != len(walls):
        logger.info(
            f"{sheet_id}: {before} candidates occupy {len(walls)} places, so "
            f"{before - len(walls)} were copies of a wall already reported"
        )

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
                    "merged_from": 1,
                    "meets_another_wall": True,
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

    alone = 0
    for wall in walls:
        meets = any(
            other is not wall and _touching(wall, other, slack) for other in walls
        )
        wall["meets_another_wall"] = meets
        if not meets:
            alone += 1
            wall["confidence"] = round(min(wall["confidence"], 0.4), 3)
            wall["confidence_band"] = "review"
    return alone
