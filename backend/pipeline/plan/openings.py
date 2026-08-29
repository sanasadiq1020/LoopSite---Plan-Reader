"""Day 4 — linking opening marks to their schedule row and to a wall.

A door or window exists in two places on a plan set: as a mark printed on the
drawing (`D3`, `W12`) and as a row in the schedule that gives its size and
type. Day 3 found both. This module joins them, and places each mark on a
candidate wall, producing the record the Handbook's opening-acceptance rule
asks for:

> Every opening receives one stable ID used in plan, 3D, elevation, take-off
> and work package. Required fields: type, mark, wall ID, width, height,
> sill/head when applicable, source sheet, source location, confidence and
> review status.

Two rules from the same section are implemented literally:

*   **"If schedule and drawing disagree, preserve both values and create an
    issue. Do not choose silently."** A mark that appears on the plan but in no
    schedule, and a schedule row with no mark on the plan, are both reported.
    Neither is dropped and neither is invented.
*   **"A missing opening is P1 if the rest of the model works."** An unmatched
    mark is raised at P1; the rest at P2.

Placing a mark on a wall is deliberately conservative. A mark is assigned to a
wall only when one candidate wall is clearly the closest — the mark must sit
within the wall's run and close to its line, and no other wall may be nearly as
close. Where two walls are equally plausible the opening is reported without a
wall, with the reason. An opening on the wrong wall would put a hole in the
wrong place in Day 6's model, which is worse than an opening waiting to be
placed by hand.
"""

from app.logging_setup import get_logger
from pipeline.plan.textmodel import bbox_center

logger = get_logger()


def _distance_to_wall(mark_centre, wall: dict, mm_per_point: float):
    """(perpendicular distance in mm, whether the mark sits within the run)."""
    x, y = mark_centre
    start = wall["start_point_pt"]
    end = wall["end_point_pt"]
    if wall["runs_along"] == "x":
        along, across = x, y
        low, high = min(start[0], end[0]), max(start[0], end[0])
        line = start[1]
    else:
        along, across = y, x
        low, high = min(start[1], end[1]), max(start[1], end[1])
        line = start[0]
    within = low <= along <= high
    return abs(across - line) * mm_per_point, within


def place_openings_on_walls(
    opening_marks: list,
    walls: list,
    calibration: dict,
    config: dict,
    sheet_id: str,
) -> list:
    """One opening record per mark on this sheet, placed on a candidate wall.

    Size and type come from the schedule, which on a real plan set is printed
    on its own sheets rather than on the plan — see
    ``reconcile_openings_with_schedules``, which runs once the whole document
    has been read.
    """
    settings = config.get("openings", {})
    max_distance_mm = float(settings.get("mark_to_wall_max_distance_mm", 900))
    ambiguity_ratio = float(settings.get("mark_to_wall_ambiguity_ratio", 1.6))

    mm_per_point = (
        calibration.get("measured_mm_per_point")
        or calibration.get("printed_mm_per_point")
        or 0.0
    )

    openings = []
    for position, mark in enumerate(sorted(opening_marks, key=lambda m: m["mark_id"]), start=1):
        wall_id = None
        wall_note = None
        if walls and mm_per_point:
            centre = bbox_center(mark["bbox"])
            scored = []
            for wall in walls:
                distance, within = _distance_to_wall(centre, wall, mm_per_point)
                if within and distance <= max_distance_mm:
                    scored.append((distance, wall))
            scored.sort(key=lambda item: item[0])
            if not scored:
                wall_note = "No candidate wall runs close enough to this mark."
            elif len(scored) > 1 and scored[1][0] < scored[0][0] * ambiguity_ratio:
                wall_note = (
                    "Two candidate walls are equally close to this mark, so it has not "
                    "been placed on either."
                )
            else:
                wall_id = scored[0][1]["wall_id"]
                scored[0][1]["linked_opening_marks"].append(mark["mark"])
                wall_note = (
                    f"Nearest candidate wall, {scored[0][0]:.0f} mm away and clearly "
                    "closer than any other."
                )
        elif not walls:
            wall_note = "No candidate walls were found on this sheet."
        else:
            wall_note = "This sheet's scale is unconfirmed, so no wall distance was measured."

        openings.append(
            {
                "opening_id": f"{sheet_id}-OP{position:03d}",
                "mark": mark["mark"],
                "element_type": mark.get("element_type"),
                "wall_id": wall_id,
                "wall_note": wall_note,
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
            }
        )

    logger.info(
        f"{sheet_id}: {len(openings)} opening marks, "
        f"{sum(1 for o in openings if o['wall_id'])} placed on a candidate wall"
    )
    return openings


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
    mismatch the Handbook asks to be preserved rather than resolved: a mark
    drawn with no schedule row, and a schedule row whose mark is drawn on no
    sheet.
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
    matched = placed = total = 0

    for page in pages:
        for opening in page.get("openings", []):
            if opening.get("found_by") == "gap_in_the_wall":
                # This one was measured from a break in a wall, on a drawing
                # that prints no codes at all. It never had a mark, so it can
                # neither match a schedule row nor be missing from one —
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

            confidence = 0.5
            if opening["in_schedule"]:
                confidence += 0.3
            if opening["wall_id"]:
                confidence += 0.15
            confidence = round(min(confidence, 0.95), 3)
            opening["confidence"] = confidence
            opening["confidence_band"] = "high" if confidence >= 0.75 else "review"
            if opening["wall_id"]:
                placed += 1

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
        f"{placed} placed on a wall, {len(scheduled_not_drawn)} scheduled marks not drawn"
    )
    return {
        "marks_on_drawings": total,
        "matched_to_a_schedule": matched,
        "placed_on_a_wall": placed,
        "marks_without_a_schedule": marks_without_a_schedule,
        "scheduled_marks_not_drawn": scheduled_not_drawn,
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


def openings_from_wall_gaps(walls: list, calibration: dict, config: dict, sheet_id: str) -> list:
    """Doors and windows found as breaks in a wall, not as printed marks.

    Most Australian plan sets label their openings D1, W12 and repeat them in a
    schedule, and those are the records that carry a size and a type. But not
    every set does: one of the supplied plans prints no marks at all and simply
    draws the openings, describing them in words beside the drawing.

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
    smallest = min(
        float(door_range.get("min", 600)), float(window_range.get("min", 300))
    )
    largest = max(
        float(door_range.get("max", 6000)), float(window_range.get("max", 6000))
    )

    mm_per_point = calibration.get("measured_mm_per_point") or calibration.get(
        "printed_mm_per_point"
    )
    if not mm_per_point or not calibration.get("usable_for_measurement"):
        return []

    found = []
    for wall in walls:
        for low, high in wall.get("gaps_pt", []):
            width_mm = (high - low) * mm_per_point
            if not (smallest <= width_mm <= largest):
                continue
            half = wall["thickness_mm"] / mm_per_point / 2.0
            across = wall["start_point_pt"][1] if wall["runs_along"] == "x" else wall[
                "start_point_pt"
            ][0]
            if wall["runs_along"] == "x":
                bbox = [low, across - half, high, across + half]
            else:
                bbox = [across - half, low, across + half, high]

            found.append(
                {
                    "opening_id": "",
                    "mark": "",
                    "element_type": None,
                    "wall_id": wall["wall_id"],
                    "wall_note": None,
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
    for position, opening in enumerate(found, start=1):
        opening["opening_id"] = f"{sheet_id}-OPG{position:03d}"
        wall = next(w for w in walls if w["wall_id"] == opening["wall_id"])
        wall["linked_opening_marks"] = wall["linked_opening_marks"] + [opening["opening_id"]]

    logger.info(
        f"{sheet_id}: {len(found)} openings read as breaks in a wall "
        "(no mark printed on the drawing)"
    )
    return found
