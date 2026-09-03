"""Linking opening marks to their schedule row, to a wall, and to a place on it.

A door or a window exists in two places on a plan set: as a mark printed on the
drawing (`D3`, `W12`) and as a row in the schedule that gives its size and
type. This module joins them, places each mark on a candidate wall and says
**where along that wall** the opening sits, producing the record the opening
rule asks for:

> Every opening receives one stable ID used in plan, 3D, elevation, take-off
> and work package. Required fields: type, mark, wall ID, width, height,
> sill/head when applicable, source sheet, source location, confidence and
> review status.

Two rules are implemented literally:

*   **"If schedule and drawing disagree, preserve both values and create an
    issue. Do not choose silently."** A mark that appears on the plan but in no
    schedule, and a schedule row with no mark on the plan, are both reported.
    Neither is dropped and neither is invented.
*   **"A missing opening is P1 if the rest of the model works."** An unmatched
    mark is raised at P1; the rest at P2.

**A position, not just a wall.** Knowing which wall a door is in is not enough
to cut it: a hole has to go somewhere along that wall. So every placed opening
carries the fraction of the way along its wall that it starts and ends, and
says whether that came from a break measured in the drawing or from where the
mark itself is printed. Fractions run from the wall's own start point to its
end point, so they survive the turn from the page's downward Y into the
building's northward Y without anything downstream having to know it happened.

**Placement is settled once the whole document has been read**, because the
schedule that gives a mark its width is printed on its own sheet, and that
width is the strongest evidence there is for which break in which wall the mark
is labelling.
"""

from app.logging_setup import get_logger
from pipeline.plan.walls import break_is_a_junction
from pipeline.plan.symbols import (
    door_swings,
    marks_inside,
    small_marks,
    swing_against_wall,
    window_symbols_in,
)
from pipeline.plan.textmodel import bbox_center

logger = get_logger()


def _project_onto_wall(mark_centre, wall: dict, mm_per_point: float):
    """Where this mark falls on this wall, and how far off it is.

    Returns (distance across the wall in mm, how far the projection falls
    beyond the wall's ends in mm, the fraction of the way along the wall).
    A mark is printed beside the opening it labels rather than on top of it, so
    both distances matter: one says the mark is beside this wall, the other
    says it is beside this *part* of it.
    """
    x, y = mark_centre
    start, end = wall["start_point_pt"], wall["end_point_pt"]
    if wall["runs_along"] == "x":
        along, across, line = x, y, start[1]
        low, high = start[0], end[0]
    else:
        along, across, line = y, x, start[0]
        low, high = start[1], end[1]

    span = high - low
    if span <= 0:
        return None
    if along < low:
        beyond = (low - along) * mm_per_point
    elif along > high:
        beyond = (along - high) * mm_per_point
    else:
        beyond = 0.0
    fraction = min(max((along - low) / span, 0.0), 1.0)
    return abs(across - line) * mm_per_point, beyond, fraction


def _breaks_on(wall: dict, mm_per_point: float) -> list:
    """Every break in this wall, as fractions along it and a width in mm."""
    start, end = wall["start_point_pt"], wall["end_point_pt"]
    if wall["runs_along"] == "x":
        low, high = start[0], end[0]
    else:
        low, high = start[1], end[1]
    span = high - low
    if span <= 0:
        return []

    found = []
    for break_start, break_end in wall.get("gaps_pt") or []:
        first = min(max((break_start - low) / span, 0.0), 1.0)
        last = min(max((break_end - low) / span, 0.0), 1.0)
        if last <= first:
            continue
        found.append(
            {
                "centre_fraction": (first + last) / 2.0,
                "start_fraction": first,
                "end_fraction": last,
                "width_mm": round((break_end - break_start) * mm_per_point, 1),
            }
        )
    return found


def _millimetres_per_point(calibration: dict) -> float:
    return float(
        calibration.get("measured_mm_per_point")
        or calibration.get("printed_mm_per_point")
        or 0.0
    )


def place_openings_on_walls(
    opening_marks: list,
    walls: list,
    calibration: dict,
    config: dict,
    sheet_id: str,
) -> list:
    """One opening record per mark on this sheet, with its candidate walls.

    Which wall it actually belongs to is settled by
    ``settle_opening_placement`` once every sheet has been read.
    """
    settings = config.get("openings", {})
    max_across_mm = float(settings.get("mark_to_wall_max_distance_mm", 2000))
    max_beyond_mm = float(settings.get("mark_beyond_wall_end_max_mm", 1200))
    mm_per_point = _millimetres_per_point(calibration)

    openings = []
    marks = sorted(opening_marks, key=lambda m: m["mark_id"])
    for position, mark in enumerate(marks, start=1):
        candidates = []
        if walls and mm_per_point:
            centre = bbox_center(mark["bbox"])
            for wall in walls:
                projected = _project_onto_wall(centre, wall, mm_per_point)
                if projected is None:
                    continue
                across, beyond, fraction = projected
                if across > max_across_mm or beyond > max_beyond_mm:
                    continue
                candidates.append(
                    {
                        "wall_id": wall["wall_id"],
                        "across_mm": round(across, 1),
                        "beyond_mm": round(beyond, 1),
                        "fraction": round(fraction, 5),
                        "nominal_thickness": bool(wall.get("matches_nominal_thickness")),
                        "breaks": _breaks_on(wall, mm_per_point),
                    }
                )
            candidates.sort(key=lambda c: c["across_mm"] + c["beyond_mm"])

        openings.append(
            {
                "opening_id": f"{sheet_id}-OP{position:03d}",
                "mark": mark["mark"],
                "element_type": mark.get("element_type"),
                "wall_id": None,
                "wall_note": None,
                "position_on_wall": None,
                "width_mm": None,
                "height_mm": None,
                "sill_height_mm": None,
                "head_height_mm": None,
                "location_on_plan": None,
                "schedule_sheet": None,
                "schedule_row_id": None,
                "in_schedule": False,
                "found_by": "mark_on_the_drawing",
                "source_sheet": sheet_id,
                "source_bbox": mark["bbox"],
                "confidence": 0.5,
                "confidence_band": "review",
                "review_status": "needs_review",
                "_candidates": candidates,
                "_no_walls": not walls,
                "_no_scale": bool(walls) and not mm_per_point,
            }
        )

    logger.info(
        f"{sheet_id}: {len(openings)} opening marks, "
        f"{sum(1 for o in openings if o['_candidates'])} with a wall beside them"
    )
    return openings


def _has_break_near(candidate: dict, search_share: float) -> bool:
    return any(
        abs(b["centre_fraction"] - candidate["fraction"]) <= search_share
        for b in candidate["breaks"]
    )


def _position(wall, start_fraction: float, end_fraction: float, measured_from: str):
    """Where an opening sits along its wall, as fractions and as millimetres."""
    if wall is None:
        return None
    length = float(wall.get("length_mm") or 0.0)
    centre = (start_fraction + end_fraction) / 2.0
    return {
        "start_fraction": round(start_fraction, 5),
        "end_fraction": round(end_fraction, 5),
        "centre_fraction": round(centre, 5),
        "from_wall_start_mm": round(centre * length, 1),
        "width_mm": round((end_fraction - start_fraction) * length, 1),
        "measured_from": measured_from,
    }


def _width_centred_on(wall, fraction: float, width_mm):
    """The schedule's width, centred where the mark points."""
    if wall is None:
        return None
    length = float(wall.get("length_mm") or 0.0)
    if not length or not width_mm:
        return None
    share = min(float(width_mm) / length, 1.0)
    start = min(max(fraction - share / 2.0, 0.0), 1.0 - share)
    return _position(wall, start, start + share, "the_mark_on_the_drawing")


def settle_opening_placement(pages: list, config: dict) -> dict:
    """Which wall each mark labels, and where along it the opening sits.

    Three kinds of answer, strongest first, and every opening says which one it
    got:

    1.  **A break in a wall the width of this opening.** A door or a window
        stops both faces of its wall at the same place, and the schedule says
        how wide it is. A break beside the mark measuring what the schedule
        says *is* the opening: its position and its width are both measured off
        the drawing.
    2.  **The mark projected onto the wall it is beside.** Where the tracing
        found no break there — an opening drawn hatched, or a face the tracing
        joined straight through — the mark still says which wall and roughly
        where along it. The width then comes from the schedule and the position
        from the mark, and the record says exactly that.
    3.  **Not placed**, with the reason. Where two different walls are equally
        beside the mark and nothing separates them, no wall is chosen. An
        opening cut into the wrong wall is a hole in the wrong place, which is
        worse than one waiting to be placed by hand.
    """
    settings = config.get("openings", {})
    width_tolerance_mm = float(settings.get("break_width_tolerance_mm", 250))
    width_tolerance_share = float(settings.get("break_width_tolerance_share", 0.2))
    search_share = float(settings.get("break_search_share_of_wall", 0.25))
    ambiguity_ratio = float(settings.get("mark_to_wall_ambiguity_ratio", 1.6))

    walls_by_id = {}
    for page in pages:
        for wall in page.get("walls", []):
            walls_by_id[wall["wall_id"]] = wall

    counts = {
        "from_a_break": 0,
        "from_the_mark": 0,
        "not_placed": 0,
        "on_a_sheet_with_no_walls": 0,
    }

    for page in pages:
        marks = [
            o
            for o in page.get("openings", [])
            if o.get("found_by") == "mark_on_the_drawing"
        ]
        # **A break holds one opening.** Two doors cannot occupy the same hole,
        # so the breaks on a sheet are handed out best-first rather than each
        # mark taking its own favourite: without this, two 820 mm doors both
        # claimed the same 899 mm break and one of them was cut in the wrong
        # place. Closest agreement with the schedule's width wins, then the
        # mark nearest the break.
        offers = []
        for opening in marks:
            scheduled_width = opening.get("width_mm")
            if not scheduled_width:
                continue
            allowed = max(width_tolerance_mm, float(scheduled_width) * width_tolerance_share)
            for candidate in opening.get("_candidates") or []:
                for index, opening_break in enumerate(candidate["breaks"]):
                    difference = abs(opening_break["width_mm"] - float(scheduled_width))
                    if difference > allowed:
                        continue
                    away = abs(opening_break["centre_fraction"] - candidate["fraction"])
                    if away > search_share:
                        continue
                    offers.append(
                        (
                            round(difference, 1),
                            round(away, 5),
                            candidate["across_mm"],
                            opening["opening_id"],
                            (candidate["wall_id"], index),
                            candidate,
                            opening_break,
                        )
                    )
        offers.sort(key=lambda offer: offer[:3])
        taken_breaks: set = set()
        taken_openings: dict = {}
        for _difference, _away, _across, opening_id, break_key, candidate, opening_break in offers:
            if opening_id in taken_openings or break_key in taken_breaks:
                continue
            taken_openings[opening_id] = (candidate, opening_break)
            taken_breaks.add(break_key)

        for opening in page.get("openings", []):
            candidates = opening.pop("_candidates", None) or []
            no_walls = opening.pop("_no_walls", False)
            no_scale = opening.pop("_no_scale", False)
            if opening.get("found_by") != "mark_on_the_drawing":
                continue
            if no_walls:
                counts["on_a_sheet_with_no_walls"] += 1

            scheduled_width = opening.get("width_mm")
            chosen, matched_break = taken_openings.get(opening["opening_id"], (None, None))

            # 2. the wall the mark is beside, position taken from the mark
            if chosen is None and candidates:
                best = candidates[0]
                best_distance = best["across_mm"] + best["beyond_mm"]
                rivals = [
                    c
                    for c in candidates[1:]
                    if c["across_mm"] + c["beyond_mm"] < max(best_distance, 1.0) * ambiguity_ratio
                ]
                if rivals:
                    # A wall with a break where the mark points beats one with
                    # none, and a wall at a thickness the office builds beats
                    # one that is not. Only where nothing at all separates them
                    # is the mark left unplaced.
                    ranked = sorted(
                        [best] + rivals,
                        key=lambda c: (
                            -int(_has_break_near(c, search_share)),
                            -int(c["nominal_thickness"]),
                            c["across_mm"] + c["beyond_mm"],
                        ),
                    )
                    leader, runner_up = ranked[0], ranked[1]
                    if (
                        _has_break_near(leader, search_share)
                        != _has_break_near(runner_up, search_share)
                        or leader["nominal_thickness"] != runner_up["nominal_thickness"]
                    ):
                        chosen = leader
                    else:
                        opening["wall_note"] = (
                            f"{len(rivals) + 1} walls are equally close to this mark and "
                            "nothing separates them, so it has not been placed on any of them."
                        )
                else:
                    chosen = best

            if chosen is None:
                if not opening.get("wall_note"):
                    if no_walls:
                        opening["wall_note"] = "No walls were traced on this sheet."
                    elif no_scale:
                        opening["wall_note"] = (
                            "This sheet's scale is unconfirmed, so no distance to a wall "
                            "could be measured."
                        )
                    else:
                        opening["wall_note"] = (
                            "No traced wall runs close enough to this mark."
                        )
                if not no_walls:
                    counts["not_placed"] += 1
                continue

            wall = walls_by_id.get(chosen["wall_id"])
            opening["wall_id"] = chosen["wall_id"]
            if wall is not None and opening["mark"] not in wall["linked_opening_marks"]:
                wall["linked_opening_marks"].append(opening["mark"])

            if matched_break is not None:
                opening["position_on_wall"] = _position(
                    wall,
                    matched_break["start_fraction"],
                    matched_break["end_fraction"],
                    "break_in_the_wall",
                )
                opening["wall_note"] = (
                    "Placed on the break in this wall, which measures "
                    f"{matched_break['width_mm']:.0f} mm against the schedule's "
                    f"{float(scheduled_width):.0f} mm."
                )
                counts["from_a_break"] += 1
            else:
                opening["position_on_wall"] = _width_centred_on(
                    wall, chosen["fraction"], scheduled_width
                )
                opening["wall_note"] = (
                    f"Placed on the nearest wall, {chosen['across_mm']:.0f} mm from the "
                    "mark. Where it sits along the wall is where the mark points; the "
                    "tracing found no break there to measure."
                )
                counts["from_the_mark"] += 1

    on_a_plan = counts["from_a_break"] + counts["from_the_mark"] + counts["not_placed"]
    counts["marks_on_a_sheet_with_walls"] = on_a_plan
    logger.info(
        f"opening marks placed: {counts['from_a_break']} on a measured break, "
        f"{counts['from_the_mark']} from the mark alone, {counts['not_placed']} of "
        f"{on_a_plan} not placed; {counts['on_a_sheet_with_no_walls']} marks are on "
        "sheets that draw no plan"
    )
    return counts


def _mark_key(mark: str) -> str:
    """A mark, comparable across the drawing and the schedule.

    An office may print 'W-01' in its schedule and 'W 01' on the plan, and the
    mark reader strips the separator from the drawn one. Comparing the two
    without normalising both sides reported a door as drawn but unscheduled and
    the same door as scheduled but undrawn.
    """
    return "".join(character for character in str(mark).upper() if character.isalnum())


def reconcile_openings_with_schedules(pages: list) -> dict:
    """Matches every mark on every drawing against the schedules in the set.

    A plan set prints its door and window schedules on their own sheets, so
    this can only run once the whole document has been read. It fills each
    opening's size and type from its schedule row, and reports both kinds of
    mismatch that are to be preserved rather than resolved: a mark drawn with
    no schedule row, and a schedule row whose mark is drawn on no sheet.
    """
    rows_by_mark: dict = {}
    for page in pages:
        for table in page.get("schedules", []):
            for row in table["rows"]:
                if row.get("mark"):
                    rows_by_mark.setdefault(_mark_key(row["mark"]), []).append(
                        (page, table, row)
                    )

    matched_marks = set()
    marks_without_a_schedule = []
    matched = total = 0

    for page in pages:
        for opening in page.get("openings", []):
            if not opening.get("mark"):
                # **This one was never marked.** It was measured from a break
                # in a wall, or read from the window drawn inside it, or from a
                # door's swing — on a drawing that prints no codes at all. It
                # can neither match a schedule row nor be missing from one, and
                # counting it as an unmatched mark produced 45 findings that
                # said nothing and an opening count nearly half made of blanks.
                continue
            total += 1
            entries = rows_by_mark.get(_mark_key(opening["mark"]))
            if entries:
                source_page, table, row = entries[0]
                matched_marks.add(_mark_key(opening["mark"]))
                opening.update(
                    {
                        "element_type": row.get("element_type") or opening["element_type"],
                        "width_mm": row.get("width_mm"),
                        "height_mm": row.get("height_mm"),
                        "sill_height_mm": _numeric(row, "window_sill_height", "sill_height"),
                        "head_height_mm": _numeric(row, "window_head_height", "head_height"),
                        "location_on_plan": row.get("values", {}).get("location"),
                        "schedule_sheet": source_page["sheet_id"],
                        "schedule_row_id": row.get("row_id"),
                        "in_schedule": True,
                    }
                )
                matched += 1
            else:
                marks_without_a_schedule.append(
                    {"sheet_id": page["sheet_id"], "mark": opening["mark"]}
                )

    scheduled_not_drawn = [
        {
            "mark": mark,
            "schedule_sheet": entries[0][0]["sheet_id"],
            "table_id": entries[0][1]["table_id"],
        }
        for mark, entries in sorted(rows_by_mark.items())
        if mark not in matched_marks
    ]

    logger.info(
        f"openings reconciled: {matched}/{total} marks matched to a schedule, "
        f"{len(scheduled_not_drawn)} scheduled marks not drawn"
    )
    return {
        "marks_on_drawings": total,
        "matched_to_a_schedule": matched,
        "marks_without_a_schedule": marks_without_a_schedule,
        "scheduled_marks_not_drawn": scheduled_not_drawn,
    }


def score_openings(pages: list) -> dict:
    """Confidence for every opening, once its schedule row and wall are known.

    Kept apart from the two steps above so that each does one thing: the
    reconciliation says what the schedule holds, the placement says which wall,
    and this says how much of that was established.
    """
    placed = with_position = measured_position = total = 0
    for page in pages:
        for opening in page.get("openings", []):
            total += 1
            confidence = 0.5
            if opening.get("in_schedule"):
                confidence += 0.3
            if opening.get("wall_id"):
                confidence += 0.1
                placed += 1
            position = opening.get("position_on_wall") or {}
            if position:
                with_position += 1
                if position.get("measured_from") == "break_in_the_wall":
                    confidence += 0.05
                    measured_position += 1
            confidence = round(min(confidence, 0.95), 3)
            opening["confidence"] = confidence
            opening["confidence_band"] = "high" if confidence >= 0.75 else "review"
    return {
        "openings": total,
        "placed_on_a_wall": placed,
        "with_a_position_on_the_wall": with_position,
        "position_measured_from_the_drawing": measured_position,
    }


def _numeric(row, *keys):
    if not row:
        return None
    values = row.get("values", {})
    for key in keys:
        raw = values.get(key)
        if raw is None:
            continue
        digits = "".join(c for c in str(raw) if c.isdigit())
        if digits:
            return float(digits)
    return None


# --- Openings read from the drawing itself, with no mark to read ----------


def _words_of(text: str) -> list:
    return [w for w in "".join(c if c.isalnum() else " " for c in str(text).upper()).split() if w]


def kind_from_words_beside_it(bbox, lines, mm_per_point: float, settings: dict):
    """What the drawing calls this opening, read from the words printed beside it.

    A plan set that prints no D1/W12 marks still describes its openings in
    words on the drawing — "Sliding door", "Double glazed window", "Awning".
    That is the sheet stating the kind, and the kind is what makes a height
    possible: a door reaches the floor and a window sits above a sill, and no
    plan shows either because a plan is a horizontal cut.

    Returns (kind, the words it was read from) or (None, None). The vocabulary
    lives in `/config`, so an office using its own wording is a configuration
    entry rather than a code change.
    """
    vocabulary = settings.get("type_words") or {}
    if not vocabulary or not lines or not mm_per_point:
        return None, None
    furthest = float(settings.get("type_word_max_distance_mm", 1500))

    centre_x = (bbox[0] + bbox[2]) / 2.0
    centre_y = (bbox[1] + bbox[3]) / 2.0
    found = []
    for line in lines:
        box = line.get("bbox") or []
        if len(box) != 4:
            continue
        away = (
            ((centre_x - (box[0] + box[2]) / 2.0) ** 2
             + (centre_y - (box[1] + box[3]) / 2.0) ** 2) ** 0.5
        ) * mm_per_point
        if away > furthest:
            continue
        words = _words_of(line.get("text", ""))
        if not words:
            continue
        joined = " ".join(words)
        for kind, phrases in vocabulary.items():
            for phrase in phrases:
                wanted = " ".join(_words_of(phrase))
                if not wanted:
                    continue
                if wanted in words or f" {wanted} " in f" {joined} ":
                    found.append((away, kind, line.get("text", "").strip()))
    if not found:
        return None, None
    found.sort()
    nearest = found[0]
    # Where a door word and a window word are the same distance away, the
    # drawing has not settled it and neither has this.
    if any(entry[1] != nearest[1] and entry[0] <= nearest[0] + 1.0 for entry in found):
        return None, None
    return nearest[1], nearest[2]


def openings_from_wall_gaps(
    walls: list,
    calibration: dict,
    config: dict,
    sheet_id: str,
    lines: list = None,
) -> list:
    """Doors and windows found as breaks in a wall, not as printed marks.

    Most Australian plan sets label their openings D1, W12 and repeat them in a
    schedule, and those are the records that carry a size and a type. But not
    every set does: one supplied plan prints no marks at all and simply draws
    the openings, describing them in words beside the drawing.

    An opening is still perfectly visible in the geometry. A wall is two
    parallel faces, and a door or a window stops **both** of them at the same
    place and starts them again on the other side. Those shared breaks are
    already known — bridging them is what makes a wall with a window in it read
    as one wall — so they are reported rather than discarded.

    What is claimed and what is not: the position, the width, and which wall it
    is in are measured. Whether it is a door or a window is **not** claimed,
    because the drawing does not say so here; a height is not claimed either,
    because a plan does not show one. Those come from a schedule, and where
    there is no schedule they stay empty rather than being invented.
    """
    settings = config.get("openings", {})
    door_range = settings.get("plausible_door_width_mm", {})
    window_range = settings.get("plausible_window_width_mm", {})
    smallest = min(float(door_range.get("min", 600)), float(window_range.get("min", 300)))
    largest = max(float(door_range.get("max", 6000)), float(window_range.get("max", 6000)))

    mm_per_point = _millimetres_per_point(calibration)
    if not mm_per_point or not calibration.get("usable_for_measurement"):
        return []

    # A break where another wall runs into this one is a junction, not a door.
    # Both stop the wall's two faces at the same place and they mean opposite
    # things: one is a hole to be cut and counted, the other is solid wall with
    # a wall attached to it.
    standards = config.get("walls", {}).get("nominal_thickness_mm", []) or [300]
    junction_slack = float(
        config.get("walls", {}).get("junction_tolerance_points", 10.0)
    )
    junction_share = float(
        config.get("walls", {}).get("junction_share_of_a_break", 0.6)
    )

    found = []
    for wall in walls:
        run_low, run_high = _span_on_wall(wall)
        for opening_break in _breaks_on(wall, mm_per_point):
            width_mm = opening_break["width_mm"]
            if not (smallest <= width_mm <= largest):
                continue
            break_low = run_low + opening_break["start_fraction"] * (run_high - run_low)
            break_high = run_low + opening_break["end_fraction"] * (run_high - run_low)
            if break_is_a_junction(
                wall, break_low, break_high, walls, junction_slack, junction_share
            ):
                continue
            half = wall["thickness_mm"] / mm_per_point / 2.0
            start, end = wall["start_point_pt"], wall["end_point_pt"]
            if wall["runs_along"] == "x":
                low = start[0] + opening_break["start_fraction"] * (end[0] - start[0])
                high = start[0] + opening_break["end_fraction"] * (end[0] - start[0])
                across = start[1]
                bbox = [low, across - half, high, across + half]
            else:
                low = start[1] + opening_break["start_fraction"] * (end[1] - start[1])
                high = start[1] + opening_break["end_fraction"] * (end[1] - start[1])
                across = start[0]
                bbox = [across - half, low, across + half, high]

            kind, described_as = kind_from_words_beside_it(
                bbox, lines or [], mm_per_point, settings
            )
            found.append(
                {
                    "opening_id": "",
                    "mark": "",
                    "element_type": kind,
                    "element_type_source": (
                        "described_beside_the_opening" if kind else "not_stated"
                    ),
                    "described_as": described_as,
                    "wall_id": wall["wall_id"],
                    "wall_note": (
                        "Measured from the break in this wall. The drawing prints no "
                        "mark for it."
                    ),
                    "position_on_wall": _position(
                        wall,
                        opening_break["start_fraction"],
                        opening_break["end_fraction"],
                        "break_in_the_wall",
                    ),
                    # Whole millimetres. A tenth of a millimetre measured off
                    # a drawing is precision the drawing does not have.
                    "width_mm": float(round(width_mm)),
                    "height_mm": None,
                    "sill_height_mm": None,
                    "head_height_mm": None,
                    "location_on_plan": None,
                    "schedule_sheet": None,
                    "schedule_row_id": None,
                    "in_schedule": False,
                    "found_by": "gap_in_the_wall",
                    "source_sheet": sheet_id,
                    "source_bbox": [round(v, 2) for v in bbox],
                    "confidence": 0.6,
                    "confidence_band": "review",
                    "review_status": "needs_review",
                }
            )

    found.sort(key=lambda o: (o["source_bbox"][1], o["source_bbox"][0]))
    walls_by_id = {w["wall_id"]: w for w in walls}
    for position, opening in enumerate(found, start=1):
        opening["opening_id"] = f"{sheet_id}-OPG{position:03d}"
        wall = walls_by_id[opening["wall_id"]]
        wall["linked_opening_marks"] = wall["linked_opening_marks"] + [opening["opening_id"]]

    named = sum(1 for o in found if o["element_type"])
    logger.info(
        f"{sheet_id}: {len(found)} openings read as breaks in a wall "
        f"(no mark printed on the drawing); {named} named a door or a window by "
        "the words printed beside them"
    )
    return found


# --- four ways a drawing says an opening is here ---------------------------
#
# A plan states its openings in up to four separate ways, and a good plan set
# states each one twice or more. What follows reads all four, puts them
# together, and decides — nothing here is handed to a person to sort out:
#
#   1. **The symbol inside the wall.** The glass, the frame and the sashes,
#      drawn between the wall's two faces. Says the kind and the width.
#   2. **The swing of a hinged door.** A quarter circle whose radius is the
#      leaf. Says it is a door, and how wide.
#   3. **The mark printed beside it** — D1, W12, SD04 — which carries a
#      schedule row and therefore a real size and type.
#   4. **The break in the wall**, where both faces stop and start again.
#      Says where and how wide, and nothing else.
#
# Two or more of those agreeing is as good as this reading gets; one is
# ordinary and usable; a break on its own is a hole of unknown kind. All three
# outcomes are recorded and used. None of them stops to ask.


def _span_on_wall(wall: dict):
    start, end = wall["start_point_pt"], wall["end_point_pt"]
    if wall["runs_along"] == "x":
        return min(start[0], end[0]), max(start[0], end[0])
    return min(start[1], end[1]), max(start[1], end[1])


def _fractions(wall: dict, low: float, high: float):
    run_low, run_high = _span_on_wall(wall)
    length = run_high - run_low
    if length <= 0:
        return 0.0, 1.0
    return (
        max(0.0, (low - run_low) / length),
        min(1.0, (high - run_low) / length),
    )


def _box_across(wall: dict, low: float, high: float, mm_per_point: float):
    """The opening's own box on the sheet: its width, the wall's thickness."""
    half = wall["thickness_mm"] / mm_per_point / 2.0
    start = wall["start_point_pt"]
    if wall["runs_along"] == "x":
        return [low, start[1] - half, high, start[1] + half]
    return [start[0] - half, low, start[0] + half, high]


def openings_from_symbols(
    walls: list,
    rulings: dict,
    page,
    calibration: dict,
    config: dict,
    sheet_id: str,
) -> list:
    """Openings read from what the drawing draws inside and against its walls.

    This is what makes a plan set that labels nothing still readable: the
    symbols are there whether or not a mark is printed, and they carry the kind
    of opening as well as its width.
    """
    settings = config.get("walls", {})
    mm_per_point = _millimetres_per_point(calibration)
    if not mm_per_point or not calibration.get("usable_for_measurement"):
        return []

    marks = small_marks(page, mm_per_point, settings)
    swings = door_swings(page, mm_per_point, settings)

    # **An opening has wall on both sides of it.** A run of lines reaching the
    # end of the wall is not a window in that wall: it is the wall's own lining
    # or hatch carrying on into the next one, and without this rule a hatched
    # wall was reported as a 5.3 m "fixed window" covering nine tenths of it.
    # The same figure the break reader already uses, because it is the same
    # fact about a wall.
    flank = float(
        config.get("openings", {}).get("opening_min_wall_each_side_mm", 300)
    ) / mm_per_point

    found = []
    for wall in walls:
        run_low, run_high = _span_on_wall(wall)
        for symbol in window_symbols_in(wall, rulings, mm_per_point, settings):
            if symbol["start_pt"] - run_low < flank or run_high - symbol["end_pt"] < flank:
                continue
            box = _box_across(wall, symbol["start_pt"], symbol["end_pt"], mm_per_point)
            arrows = marks_inside(marks, box)
            kind = symbol["kind"]
            confidence = symbol["confidence"]
            # An arrow inside the opening is the drawing saying the sash runs.
            # It cannot make a fixed pane slide, but it settles the case where
            # the line count is on the edge.
            if arrows >= int(settings.get("arrows_for_a_sliding_sash", 2)):
                kind = "sliding_window"
                confidence = min(confidence + 0.05, 0.95)
            found.append(
                _symbol_record(
                    wall, symbol["start_pt"], symbol["end_pt"], mm_per_point, sheet_id,
                    kind=kind,
                    width_mm=symbol["width_mm"],
                    confidence=confidence,
                    found_by="window_symbol",
                    note=(
                        f"{symbol['lines_inside_the_wall']} lines are drawn inside this "
                        "wall over this stretch, which is how a window is drawn."
                    ),
                )
            )

        for swing in swings:
            against = swing_against_wall(swing, wall, mm_per_point, settings)
            if not against:
                continue
            # A swing that covers the whole of a wall has not been matched to
            # the wall the door is in — it has been matched to a fragment
            # shorter than the door itself, which cannot hold it.
            covered = (against[1] - against[0]) / max(run_high - run_low, 1e-6)
            if covered > float(settings.get("max_swing_share_of_a_wall", 0.9)):
                continue
            found.append(
                _symbol_record(
                    wall, against[0], against[1], mm_per_point, sheet_id,
                    kind="door",
                    width_mm=swing["width_mm"],
                    confidence=0.85,
                    found_by="door_swing",
                    note=(
                        "The door's swing is drawn here, and the arc's radius is the "
                        f"leaf: {swing['width_mm']:.0f} mm."
                    ),
                )
            )
    return found


def _symbol_record(
    wall, low, high, mm_per_point, sheet_id, *, kind, width_mm, confidence, found_by, note
):
    start_fraction, end_fraction = _fractions(wall, low, high)
    return {
        "opening_id": "",
        "mark": "",
        "element_type": kind,
        "element_type_source": found_by,
        "described_as": None,
        "wall_id": wall["wall_id"],
        "wall_note": note,
        "position_on_wall": _position(wall, start_fraction, end_fraction, found_by),
        "width_mm": float(round(width_mm)),
        "height_mm": None,
        "sill_height_mm": None,
        "head_height_mm": None,
        "location_on_plan": None,
        "schedule_sheet": None,
        "schedule_row_id": None,
        "in_schedule": False,
        "found_by": found_by,
        "source_sheet": sheet_id,
        "source_bbox": [
            round(v, 2) for v in _box_across(wall, low, high, mm_per_point)
        ],
        "confidence": confidence,
        "confidence_band": "high" if confidence >= 0.75 else "review",
        "review_status": "auto_confirmed",
        "evidence": [found_by],
    }


# --- putting the four together ---------------------------------------------


def _overlap_on_the_wall(first: dict, second: dict) -> float:
    """How much of the wall these two openings both claim, as a fraction."""
    one, other = first.get("position_on_wall"), second.get("position_on_wall")
    if not one or not other:
        return 0.0
    low = max(one["start_fraction"], other["start_fraction"])
    high = min(one["end_fraction"], other["end_fraction"])
    if high <= low:
        return 0.0
    shortest = min(
        one["end_fraction"] - one["start_fraction"],
        other["end_fraction"] - other["start_fraction"],
    )
    return (high - low) / shortest if shortest > 0 else 0.0


# What each source is worth when two of them disagree about what an opening is.
# **A drawn symbol outranks a printed label, and a label outranks a bare gap.**
# The symbol and the swing are the thing itself, drawn to size; the label is a
# reference to a schedule row that may have been typed against the wrong mark;
# and a gap says only that the wall stops.
_SOURCE_RANK = {
    # The swing is the strongest thing on the sheet: it is the door leaf itself,
    # drawn to its own width. The window drawn inside the wall is next — it is
    # the opening, but its width is read off the ends rather than swept out. A
    # printed mark is a reference to a schedule row that may have been typed
    # against the wrong code, and a break says only that the wall stops.
    "door_swing": 4,
    "window_symbol": 3,
    "mark_on_the_drawing": 2,
    "gap_in_the_wall": 1,
}


def merge_opening_evidence(page: dict, config: dict) -> dict:
    """Joins every reading of the same opening into one record, and decides.

    An opening a plan states three ways is one opening, not three. They are
    merged when they sit on the same wall over the same stretch, and the record
    that survives keeps every source that saw it — which is what the confidence
    is then computed from:

    | sources agreeing | confidence | what happens |
    |---|---|---|
    | two or more | high | confirmed |
    | one | medium | confirmed, and the record says which one |
    | a break in the wall alone | low | reported as an opening of unknown kind |

    Nothing is set aside for a person to decide. Where two sources disagree
    about *what* the opening is, the ranking above settles it and the losing
    reading is kept on the record rather than thrown away, so the disagreement
    is visible without being in the way.
    """
    settings = config.get("walls", {})
    same_opening = float(settings.get("same_opening_overlap_share", 0.5))
    apart_mm = float(settings.get("same_opening_apart_mm", 300))

    openings = page.get("openings") or []
    walls_by_id = {wall["wall_id"]: wall for wall in page.get("walls") or []}
    mm_per_point = _millimetres_per_point(page.get("scale_calibration") or {})
    apart = (apart_mm / mm_per_point) if mm_per_point else 0.0
    for opening in openings:
        opening.setdefault("evidence", [opening.get("found_by", "gap_in_the_wall")])
        opening["placed_bbox"] = _placed_box(opening, walls_by_id, mm_per_point)

    # **Two readings of one opening are in the same place on the paper.**
    # Grouping them by which wall they were put on looked more precise and was
    # not: a wall drawn with a lining produces two overlapping candidates, and
    # the mark landed on one of them while the symbol drawn inside it landed on
    # the other — so the same window was reported twice, on two walls, in the
    # same place. Where they are on the sheet does not care which candidate
    # they were attached to.
    #
    # The mark's own box is left out of this: it is printed *beside* the
    # opening, often inside the room with a leader, so it says nothing about
    # where the opening is. What is compared is the stretch of wall each
    # reading claims, which every reading has once its mark has been placed.
    placed = [o for o in openings if o.get("position_on_wall") and o.get("wall_id")]
    loose = [o for o in openings if o not in placed]

    clusters: list = []
    for opening in sorted(placed, key=lambda o: _claimed_box(o)[0]):
        for cluster in clusters:
            if any(
                _same_place(opening, member, same_opening, apart)
                for member in cluster
            ):
                cluster.append(opening)
                break
        else:
            clusters.append([opening])

    merged_total = 0
    kept = []
    for cluster in clusters:
        merged_total += len(cluster) - 1
        kept.append(_one_opening_from(cluster))

    for opening in loose:
        _settle_confidence(opening)
    kept.extend(loose)
    # Working state, not a result: it is the stretch of wall each reading
    # claimed, used to decide which readings are the same opening.
    for opening in kept:
        opening.pop("placed_bbox", None)
    kept.sort(key=lambda o: (o["source_bbox"][1], o["source_bbox"][0]))

    # Every opening carries an identifier, whether the drawing labelled it or
    # not. Without one it cannot be pointed at from a table, an overlay or the
    # model.
    for position, opening in enumerate(kept, start=1):
        if not opening.get("opening_id"):
            opening["opening_id"] = f"{page['sheet_id']}-OPG{position:03d}"

    page["openings"] = kept
    return {"merged": merged_total, "openings": len(kept)}


def _placed_box(opening: dict, walls_by_id: dict, mm_per_point: float):
    """The stretch of wall this reading claims, as a box on the sheet.

    This — not the mark's own box — is what says where an opening is. A mark is
    printed beside its opening, commonly inside the room with a leader, and two
    marks for two different windows can be printed side by side.
    """
    place = opening.get("position_on_wall")
    wall = walls_by_id.get(opening.get("wall_id") or "")
    if not place or not wall or not mm_per_point:
        return None
    try:
        start, end = wall["start_point_pt"], wall["end_point_pt"]
        half = wall["thickness_mm"] / mm_per_point / 2.0
        if wall["runs_along"] == "x":
            low = start[0] + place["start_fraction"] * (end[0] - start[0])
            high = start[0] + place["end_fraction"] * (end[0] - start[0])
            return [min(low, high), start[1] - half, max(low, high), start[1] + half]
        low = start[1] + place["start_fraction"] * (end[1] - start[1])
        high = start[1] + place["end_fraction"] * (end[1] - start[1])
        return [start[0] - half, min(low, high), start[0] + half, max(low, high)]
    except Exception:
        return None


def _claimed_box(opening: dict):
    """The stretch of wall this reading claims, as a box on the sheet."""
    return opening.get("placed_bbox") or opening["source_bbox"]


def _same_place(first: dict, second: dict, share: float, apart: float = 0.0) -> bool:
    """Whether these two readings are of the same opening.

    Two ways, either of which is enough:

    *   **They overlap.** A symbol drawn inside a wall and the break in that
        wall are the same stretch of it, read two ways.
    *   **They are on the same wall and close together.** A door's swing is
        drawn from the hinge and a break is measured between the jambs; the two
        can sit alongside each other rather than on top. Nearer than a door is
        wide, on the same wall, they are one door — there is not room for two.
    """
    a, b = _claimed_box(first), _claimed_box(second)
    width = min(a[2], b[2]) - max(a[0], b[0])
    height = min(a[3], b[3]) - max(a[1], b[1])
    if width > 0 and height > 0:
        smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
        if smaller > 0 and (width * height) / smaller >= share:
            return True
    if apart <= 0 or first.get("wall_id") != second.get("wall_id"):
        return False
    gap = max(
        max(a[0], b[0]) - min(a[2], b[2]),
        max(a[1], b[1]) - min(a[3], b[3]),
    )
    return gap <= apart


def _one_opening_from(cluster: list) -> dict:
    """The single record for an opening several sources saw."""
    if len(cluster) == 1:
        opening = cluster[0]
        _settle_confidence(opening)
        return opening

    # The record to build on is the one carrying a schedule row where there is
    # one, because that is the only source with a height on it; otherwise the
    # highest-ranked source wins.
    def rank(opening):
        return (
            1 if opening.get("in_schedule") else 0,
            _SOURCE_RANK.get(opening.get("found_by"), 0),
            opening.get("confidence", 0.0),
        )

    best = max(cluster, key=rank)
    evidence = []
    for opening in cluster:
        for source in opening.get("evidence", []):
            if source not in evidence:
                evidence.append(source)
    best["evidence"] = evidence

    # The kind of opening comes from the strongest source that names one, which
    # is not always the record being built on: a schedule row gives the height,
    # a drawn swing gives the fact that it is a door.
    named = [o for o in cluster if o.get("element_type")]
    if named:
        chosen = max(named, key=lambda o: _SOURCE_RANK.get(o.get("found_by"), 0))
        if chosen is not best and _SOURCE_RANK.get(
            chosen.get("found_by"), 0
        ) > _SOURCE_RANK.get(best.get("found_by"), 0):
            best["element_type"] = chosen["element_type"]
            best["element_type_source"] = chosen.get("found_by")
    # A width measured off the drawing is kept where the record has none.
    if not best.get("width_mm"):
        for opening in cluster:
            if opening.get("width_mm"):
                best["width_mm"] = opening["width_mm"]
                break
    # A mark from any of them names the opening on the sheet.
    if not best.get("mark"):
        for opening in cluster:
            if opening.get("mark"):
                best["mark"] = opening["mark"]
                break

    _settle_confidence(best)
    return best


def _settle_confidence(opening: dict) -> None:
    """How far to trust an opening, from how many ways the drawing said it.

    And **the decision that follows from it**, because nothing here waits for a
    person: two sources or more is confirmed, one is confirmed and says which,
    and a bare break in a wall is reported as an opening whose kind the drawing
    never stated.
    """
    sources = [s for s in opening.get("evidence", []) if s]
    count = len(set(sources))
    if count >= 2:
        opening["confidence"] = max(opening.get("confidence", 0.0), 0.9)
        opening["confidence_band"] = "high"
        opening["review_status"] = "auto_confirmed"
        opening["how_it_was_decided"] = (
            f"{count} separate readings of the drawing agree that there is an opening "
            "here, so it is taken as confirmed."
        )
    elif count == 1 and sources[0] != "gap_in_the_wall":
        opening["confidence"] = max(opening.get("confidence", 0.0), 0.7)
        opening["confidence_band"] = (
            "high" if opening.get("confidence", 0) >= 0.75 else "review"
        )
        opening["review_status"] = "auto_confirmed"
        opening["how_it_was_decided"] = (
            f"Read from {_in_words(sources[0])}. That is one reading, and it is used."
        )
    else:
        opening["confidence"] = min(opening.get("confidence", 0.5), 0.5)
        opening["confidence_band"] = "low"
        opening["review_status"] = "auto_confirmed"
        if not opening.get("element_type"):
            opening["element_type"] = "unknown_opening"
            opening["element_type_source"] = "not_stated"
        opening["how_it_was_decided"] = (
            "The wall stops and starts again here, and nothing else on the sheet says "
            "what goes in the gap, so it is reported as an opening of unknown kind."
        )


def _in_words(source: str) -> str:
    return {
        "window_symbol": "the window drawn inside the wall",
        "door_swing": "the door's swing drawn on the plan",
        "mark_on_the_drawing": "the mark printed beside it",
        "gap_in_the_wall": "the break in the wall",
    }.get(source, source)


# --- every opening is named on the sheet -----------------------------------


def name_openings(pages: list, config: dict) -> None:
    """Gives every opening a short label, and puts it on the marked-up sheet.

    **An opening a reader cannot name is one they cannot check.** Where the
    drawing prints a mark that is the name, because that is what the schedule
    is keyed on. Where it prints none — and a great many plans print none — a
    short one is made from what the opening is: ``D`` for a door, ``W`` for a
    window, ``O`` where the drawing never said. Numbered in reading order down
    the sheet, so two runs of the same plan name the same opening the same way.
    """
    prefixes = config.get("walls", {}).get(
        "opening_label_prefixes",
        {"door": "D", "sliding_window": "W", "fixed_window": "W",
         "highlight_window": "W", "window": "W"},
    )
    default = config.get("walls", {}).get("opening_label_default", "O")

    for page in pages:
        counters: dict = {}
        for opening in page.get("openings") or []:
            if opening.get("mark"):
                opening["display_mark"] = opening["mark"]
                continue
            # An opening the drawing never named still has to be nameable, or a
            # row of the table cannot be found on the sheet.
            prefix = prefixes.get(opening.get("element_type") or "", default)
            counters[prefix] = counters.get(prefix, 0) + 1
            opening["display_mark"] = f"{prefix}{counters[prefix]:02d}"
            opening["display_mark_is_made_up"] = True
