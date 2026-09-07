"""What the reading says about itself, checked against the same PDF.

**Why this exists.** Every figure beside a marked-up sheet is computed from the
records the sheet was drawn from, so a figure and its drawing can never
disagree — however wrong both are. A count cannot check a drawing, and this
project's own history says what that costs: total wall length went *up* while
the reading got worse, and the sheet that scored best on wall length had not
found a single wall.

So the checks here are built the other way round: each one compares what the
reader produced against **something the drawing states independently** — its own
printed dimension strings, its own overall figures, its own schedule, its own
elevations. Nothing here needs a reference file, a manual step or any per-project
setup, because a user uploads a plan set nobody has seen before.

**Three outcomes are kept apart everywhere, and that is the point of the
module.** A check that does not apply to a sheet is `not_applicable` — an
elevation has no floor area and a cover sheet has no walls, and reporting either
as zero or as a failure is a lie about the drawing. A check that applies but had
nothing to check against is `unverifiable`, with the reason. Only a check that
actually ran reports `pass` or `fail`. **A check that did not run is never
reported as a pass.**
"""

from __future__ import annotations

import csv
import json
import math

from app.logging_setup import get_logger

logger = get_logger()

# The four outcomes, used verbatim in the JSON, the CSV and the interface.
PASS = "pass"
FAIL = "fail"
NOT_APPLICABLE = "not_applicable"
UNVERIFIABLE = "unverifiable"

# How far a detected envelope may sit from the figure the sheet prints before it
# is called a failure. A printed overall is measured to a face and a detected
# envelope to a centreline, so the two differ by a wall thickness at each end
# even when both are right; this is deliberately generous, because the defect it
# exists to catch is a scale error, which is tens of per cent.
ENVELOPE_TOLERANCE_PCT = 10.0


# --- the attrition recorder -------------------------------------------------
#
# Wall counts before and after every stage, recorded as the stages run and read
# back once the sheet is finished. This is observation only: nothing here is
# consulted by any stage, and removing it would not change a single wall.

_ATTRITION: dict = {}


def attrition_reset(sheet_id: str) -> None:
    """Start a fresh trace for one sheet."""
    try:
        _ATTRITION[sheet_id or "?"] = []
    except Exception:
        pass


def attrition_record(sheet_id: str, stage: str, walls) -> None:
    """Note how many walls are live and how many are set aside, at one stage.

    *Live* means no reason has been written against the candidate yet. A stage
    that sets a wall aside writes ``not_used_because``; the wall stays in the
    list, so counting the list alone would show a filter removing nothing.
    """
    try:
        walls = walls or []
        kept = sum(1 for w in walls if not (w or {}).get("not_used_because"))
        _ATTRITION.setdefault(sheet_id or "?", []).append(
            {
                "stage": stage,
                "candidates": len(walls),
                "kept": kept,
                "set_aside": len(walls) - kept,
            }
        )
    except Exception:
        pass


def attrition_for(sheet_id: str) -> list:
    """The trace recorded for one sheet, with what each stage cost."""
    try:
        rows = list(_ATTRITION.get(sheet_id or "?", []))
    except Exception:
        return []
    previous = None
    for row in rows:
        row["kept_change"] = 0 if previous is None else row["kept"] - previous
        previous = row["kept"]
    return rows


# --- small helpers ----------------------------------------------------------


def _number(value):
    try:
        out = float(value)
        return out if math.isfinite(out) else None
    except Exception:
        return None


def _kept_walls(page: dict) -> list:
    return [w for w in (page.get("walls") or []) if not w.get("not_used_because")]


def _mm_per_point(page: dict):
    calibration = page.get("scale_calibration") or {}
    return _number(
        calibration.get("measured_mm_per_point")
        or calibration.get("printed_mm_per_point")
    )


def _draws_a_plan(page: dict) -> bool:
    """Whether walls were actually looked for on this sheet.

    **Not the same question as whether the sheet is called a plan.** A site plan
    is drawn looking down like a floor plan and its title says PLAN, but its
    parallel lines are boundaries, setbacks and driveways, so the reader
    deliberately traces no walls on it - and a check that then reports zero
    walls as a failure is blaming the drawing for a rule the reader applied on
    purpose. The sheet says so itself in ``walls_note``, and that is what is
    read here.
    """
    page_type = page.get("page_type") or {}
    if not page_type.get("draws_a_plan"):
        return False
    if not (page.get("walls") or []) and page.get("walls_note"):
        return False
    return True


def _type_of(page: dict) -> str:
    return ((page.get("page_type") or {}).get("value") or "unknown")


def _stated(page: dict, field: str):
    """The value of one title-block field, out of the record that carries it.

    A field is a record - the value, what was read, how it was found and how
    far to trust it - so the value has to be taken out of it rather than the
    whole record being handed on as a title.
    """
    value = (page.get("title_block") or {}).get(field)
    if isinstance(value, dict):
        value = value.get("value")
    text = str(value).strip() if value not in (None, "") else ""
    return text or None


def _result(status: str, detail: str, **fields) -> dict:
    out = {"status": status, "detail": detail}
    out.update(fields)
    return out


# --- 1. the scale ------------------------------------------------------------


def check_scale(page: dict) -> dict:
    """Is the scale this sheet is measured at supported by its own figures?

    The sheet's dimension strings are the independent evidence: each figure is
    printed against the line that measures it, so the figures and the drawn
    lengths together give a millimetres-per-point that owes nothing to the
    title block. Where they disagree with the printed ratio the sheet is
    *contradicted*; where there are too few to pool, *inconclusive*.
    """
    calibration = page.get("scale_calibration") or {}
    result = (calibration.get("result") or "not_checked").lower()
    measured = _number(calibration.get("measured_mm_per_point"))
    printed = _number(calibration.get("printed_mm_per_point"))
    variance = _number(calibration.get("variance_pct"))
    usable = bool(calibration.get("usable_for_measurement"))
    strings_used = calibration.get("strings_used")
    strings_agreeing = calibration.get("strings_agreeing")

    fields = {
        "result": result,
        "measured_mm_per_point": measured,
        "printed_mm_per_point": printed,
        "variance_pct": variance,
        "strings_used": strings_used,
        "strings_agreeing": strings_agreeing,
        "usable_for_measurement": usable,
        # Everything measured off this sheet is only as good as this answer.
        "source": calibration.get("source"),
        "unverified_reason": calibration.get("unverified_reason"),
        "sheet_size_named": calibration.get("sheet_size_named"),
        "sheet_size_correction": calibration.get("sheet_size_correction"),
        # A scale is verified only where two independent sources agree.
        "scale_dependent_outputs_verified": bool(calibration.get("verified")),
    }

    if result == "confirmed":
        return _result(PASS, "The sheet's own dimension strings confirm the scale it is measured at.", **fields)
    if result == "contradicted":
        return _result(
            FAIL,
            "The sheet's own figures measure a different scale from the one printed on it, "
            "so every length taken from it is unverified.",
            **fields,
        )
    if result == "printed_only":
        # Not a pass. The sheet states a scale and nothing on the sheet checked
        # it, so every length measured from it is unverified.
        return _result(
            UNVERIFIABLE,
            calibration.get("unverified_reason")
            or "This sheet's printed scale was used and nothing on the sheet confirmed it, "
               "so every length taken from it is unverified.",
            **fields,
        )
    if result in ("inconclusive", "not_checked", "unknown", "none", ""):
        return _result(
            UNVERIFIABLE,
            calibration.get("unverified_reason")
            or "This sheet does not print enough dimension strings to check its own scale, "
               "so every length taken from it is unverified.",
            **fields,
        )
    return _result(UNVERIFIABLE, f"The scale check reported '{result}'.", **fields)


# --- 2. the envelope ---------------------------------------------------------


def _printed_overalls(page: dict) -> dict:
    """The largest figure the sheet prints for each axis.

    An overall is preferred where one is printed, because it states the whole
    building in one number. Where none is, the largest single figure on that
    axis stands in for it and the record says so.
    """
    best = {"x": None, "y": None}
    kind = {"x": None, "y": None}
    for chain in page.get("dimension_chains") or []:
        axis = chain.get("axis")
        total = _number(chain.get("sum_mm"))
        if axis in best and total and (best[axis] is None or total > best[axis]):
            best[axis], kind[axis] = total, "dimension_string_total"
    for dimension in page.get("dimensions") or []:
        axis = dimension.get("measures_axis")
        value = _number(dimension.get("value_mm"))
        if axis not in best or not value:
            continue
        if dimension.get("is_overall"):
            if best[axis] is None or value > best[axis] or kind[axis] != "printed_overall":
                best[axis], kind[axis] = value, "printed_overall"
        elif best[axis] is None:
            best[axis], kind[axis] = value, "largest_single_figure"
        elif kind[axis] == "largest_single_figure" and value > best[axis]:
            best[axis] = value
    return {"printed": best, "printed_from": kind}


def check_envelope(page: dict) -> dict:
    """Does the building the walls describe measure what the sheet says it does?

    The strongest check in this module, because it compares a length the reader
    produced against a length the drafter typed, and a scale error of tens of
    per cent shows up immediately.
    """
    if not _draws_a_plan(page):
        return _result(
            NOT_APPLICABLE,
            f"This sheet is a {_type_of(page)}, which does not draw the building in plan.",
        )
    walls = _kept_walls(page)
    mm_per_point = _mm_per_point(page)
    printed = _printed_overalls(page)
    fields = {
        "printed_mm": printed["printed"],
        "printed_from": printed["printed_from"],
        "detected_mm": {"x": None, "y": None},
        "variance_pct": {"x": None, "y": None},
        "walls_measured": len(walls),
    }
    if not walls:
        return _result(UNVERIFIABLE, "No walls were traced on this sheet, so there is no envelope to compare.", **fields)
    if not mm_per_point:
        return _result(UNVERIFIABLE, "This sheet has no established scale, so its walls cannot be measured.", **fields)

    boxes = [w.get("bbox") for w in walls if w.get("bbox") and len(w["bbox"]) == 4]
    if not boxes:
        return _result(UNVERIFIABLE, "The traced walls carry no position on the sheet.", **fields)
    detected = {
        "x": (max(b[2] for b in boxes) - min(b[0] for b in boxes)) * mm_per_point,
        "y": (max(b[3] for b in boxes) - min(b[1] for b in boxes)) * mm_per_point,
    }
    fields["detected_mm"] = {k: round(v, 1) for k, v in detected.items()}

    compared, worst, failures = [], 0.0, []
    for axis in ("x", "y"):
        want = printed["printed"].get(axis)
        if not want:
            continue
        variance = (detected[axis] - want) / want * 100.0
        fields["variance_pct"][axis] = round(variance, 1)
        compared.append(axis)
        if abs(variance) > abs(worst):
            worst = variance
        if abs(variance) > ENVELOPE_TOLERANCE_PCT:
            failures.append(f"{axis}: printed {want:.0f} mm, detected {detected[axis]:.0f} mm ({variance:+.1f}%)")

    fields["axes_compared"] = compared
    fields["tolerance_pct"] = ENVELOPE_TOLERANCE_PCT
    if not compared:
        return _result(
            UNVERIFIABLE,
            "This sheet prints no overall dimension to compare the traced building against.",
            **fields,
        )
    if failures:
        return _result(FAIL, "The traced building does not measure what the sheet prints — " + "; ".join(failures), **fields)
    return _result(PASS, f"The traced building matches the printed overalls on {', '.join(compared)} within {ENVELOPE_TOLERANCE_PCT:.0f}%.", **fields)


# --- 3. the openings, by whichever evidence this PDF carries -----------------


def _schedule_openings(pages: list) -> int:
    """Rows across the whole document that name an opening and give a size.

    A schedule is printed on its own sheet, so this is a document-level count,
    not a per-sheet one.
    """
    total = 0
    for page in pages:
        for table in page.get("schedules") or []:
            columns = [str(c).lower() for c in (table.get("columns") or [])]
            has_mark = any(any(k in c for k in ("mark", "id", "ref", "code", "tag", "no", "item")) for c in columns)
            has_size = any(any(k in c for k in ("width", "size", "height", "w x h", "dim")) for c in columns)
            if has_mark and has_size:
                total += int(table.get("row_count") or len(table.get("rows") or []))
    return total


def _elevation_openings(pages: list) -> int:
    """Openings the elevation sheets show, as a second statement of the same doors."""
    total = 0
    for page in pages:
        if "elevation" in _type_of(page):
            total += len(page.get("openings") or []) + len(page.get("opening_marks") or [])
    return total


def check_openings(page: dict, pages: list, document_evidence: dict) -> dict:
    """The tiered cross-check, using the strongest evidence this PDF carries.

    A plan set states an opening in up to four ways and the strongest available
    is used, because they are not equally good: a schedule row is the office's
    own typed size, while a width annotated on a wall is one figure in one
    place. Which tier was used is always recorded, so a weak check is never
    mistaken for a strong one.
    """
    if not _draws_a_plan(page):
        note = page.get("walls_note")
        return _result(
            NOT_APPLICABLE,
            note or f"This sheet is a {_type_of(page)}; openings are cross-checked on sheets that draw the plan.",
            tier_used=None,
        )

    # **An opening is a hole in a traced wall.** A sheet on which nothing was
    # traced cannot be asked how many of the building's doors it holds - the
    # answer would be a failure of the wall tracing reported as a failure of
    # the openings, on a sheet that may legitimately draw none.
    if not _kept_walls(page):
        return _result(
            UNVERIFIABLE,
            "No walls were traced on this sheet, so there is nothing for an opening to be "
            "found in and nothing to cross-check.",
            tier_used=None,
            found=len(page.get("openings") or []),
        )

    openings = page.get("openings") or []
    marks = page.get("opening_marks") or []
    placed = sum(1 for o in openings if o.get("wall_id"))
    matched = sum(1 for o in openings if o.get("in_schedule") or o.get("schedule_row_id"))
    agreeing = sum(1 for o in openings if int(o.get("evidence_count") or 0) >= 2)

    fields = {
        "tier_used": None,
        "tier_name": None,
        "expected": None,
        "found": len(openings),
        "placed": placed,
        "matched": matched,
        "missing": None,
        "unmatched": None,
        "confirmed_by_two_or_more_sources": agreeing,
        "inter_evidence_agreement_pct": (round(100.0 * agreeing / len(openings), 1) if openings else None),
        "unresolved_gaps": len(page.get("unresolved_gaps") or []),
    }

    schedule_rows = document_evidence.get("schedule_rows") or 0
    elevation_openings = document_evidence.get("elevation_openings") or 0
    annotated = sum(1 for o in openings if "leaf" in str(o.get("found_by") or "").lower()
                    or "dimension" in str(o.get("found_by") or "").lower())

    if schedule_rows and marks:
        # **The expectation is this sheet's own marks, not the document's
        # schedule.** A door and window schedule states every opening in the
        # building; one floor plan draws a part of it, and a reflected-ceiling
        # plan of the same rooms draws the same walls again. Measuring one
        # sheet against the whole schedule reports every sheet as missing most
        # of the building, which says nothing about the reading.
        fields.update(
            tier_used=1,
            tier_name="marks printed on this sheet, keyed to the document's schedule",
            expected=len(marks),
            schedule_rows_in_document=schedule_rows,
        )
        fields["missing"] = max(0, len(marks) - matched)
        fields["unmatched"] = max(0, len(openings) - matched)
        status = PASS if matched and fields["missing"] == 0 else FAIL
        detail = (
            f"All {matched} of this sheet's {len(marks)} printed marks matched a schedule row."
            if status == PASS else
            f"{matched} of this sheet's {len(marks)} printed marks matched a schedule row; "
            f"{fields['missing']} did not."
        )
        return _result(status, detail, **fields)

    if elevation_openings:
        fields.update(tier_used=2, tier_name="openings shown on the elevation sheets", expected=elevation_openings)
        fields["missing"] = max(0, elevation_openings - len(openings))
        fields["unmatched"] = max(0, len(openings) - elevation_openings)
        # **A count, not a one-to-one match** - an elevation shows one side of
        # the building and a plan shows every side at once, so the plan should
        # find at least what the elevations between them show. Fewer is a real
        # shortfall in the tracing; it is never reported as a pass.
        status = PASS if len(openings) >= elevation_openings else FAIL
        detail = (
            f"The elevations show {elevation_openings} openings and this plan found {len(openings)}."
            if status == PASS else
            f"The elevations show {elevation_openings} openings between them; this plan found only "
            f"{len(openings)}. An elevation shows one side and a plan shows every side, so the plan "
            "should find at least as many."
        )
        return _result(status, detail, **fields)

    if marks:
        fields.update(tier_used=3, tier_name="opening marks printed on the plan, no schedule in this document",
                      expected=len(marks))
        fields["missing"] = max(0, len(marks) - placed)
        fields["unmatched"] = max(0, len(openings) - placed)
        status = PASS if placed >= len(marks) else FAIL
        return _result(status, f"{placed} of {len(marks)} printed marks were placed on a wall.", **fields)

    if annotated:
        # **The weakest tier states a size, not a count.** Nothing here says how
        # many openings the building has, so there is no expectation to measure
        # against - only how many of what was found the drawing corroborates.
        fields.update(
            tier_used=4,
            tier_name="opening widths the drawing prints at the opening",
            expected=None,
            corroborated_by_a_printed_width=annotated,
        )
        fields["missing"] = None
        fields["unmatched"] = max(0, len(openings) - annotated)
        return _result(
            PASS,
            f"{annotated} of the {len(openings)} openings found carry a width the drawing prints at "
            "the opening. Nothing in this document says how many openings the building has, so this "
            "corroborates sizes without checking the count.",
            **fields,
        )

    return _result(
        UNVERIFIABLE,
        "This document carries no schedule, no elevation openings, no printed marks and no annotated "
        "widths, so the openings found on this sheet cannot be checked against anything the drawing "
        "states independently.",
        **fields,
    )


# --- 4. closure --------------------------------------------------------------


def check_closure(page: dict) -> dict:
    """How much of what was traced is a building, and how much is loose ends.

    A wall bounds a room or forms part of the shell, so it lies on a closed
    circuit: set off along it, keep turning at the walls it meets, and you come
    back. A roof overhang, a boundary and a fragment of a wall the tracing lost
    lie on no circuit. The 2-core of the wall graph is exactly that set, and it
    is reported rather than acted on.
    """
    if not _draws_a_plan(page):
        return _result(NOT_APPLICABLE, f"This sheet is a {_type_of(page)}, which traces no walls.")
    walls = _kept_walls(page)
    if not walls:
        return _result(UNVERIFIABLE, "No walls were traced on this sheet, so there is nothing to close.")

    live = {w.get("wall_id") for w in walls if w.get("wall_id")}
    neighbours = {wall_id: set() for wall_id in live}
    for wall in walls:
        this = wall.get("wall_id")
        if this not in neighbours:
            continue
        for meeting in wall.get("connects_to") or []:
            other = meeting.get("wall_id") if isinstance(meeting, dict) else meeting
            if other in live and other != this:
                neighbours[this].add(other)
                neighbours[other].add(this)

    # The 2-core: strip every wall with fewer than two neighbours, repeatedly,
    # because removing one loose end can leave the wall it hung off loose too.
    core = dict(neighbours)
    changed = True
    while changed:
        changed = False
        for wall_id in [k for k, v in core.items() if len(v) < 2]:
            for other in core.pop(wall_id, set()):
                core.get(other, set()).discard(wall_id)
            changed = True

    on_circuit = len(core)
    total = len(walls)
    share = round(100.0 * on_circuit / total, 1) if total else 0.0
    fields = {
        "walls": total,
        "on_a_closed_loop": on_circuit,
        "dangling": total - on_circuit,
        "closed_share_pct": share,
        "walls_meeting_nothing": sum(1 for w in walls if not w.get("junction_count")),
    }
    if share >= 60.0:
        return _result(PASS, f"{on_circuit} of {total} walls ({share}%) lie on a closed circuit.", **fields)
    return _result(
        FAIL,
        f"Only {on_circuit} of {total} walls ({share}%) lie on a closed circuit; the rest are "
        "loose ends, which is what an incompletely traced plan looks like.",
        **fields,
    )


# --- 5 and 6. attrition and provenance ---------------------------------------


def check_attrition(page: dict) -> dict:
    """Where the walls went, stage by stage."""
    rows = attrition_for(page.get("sheet_id"))
    if not rows:
        if not _draws_a_plan(page):
            return _result(NOT_APPLICABLE, f"This sheet is a {_type_of(page)}, so no wall stages ran.", stages=[])
        return _result(UNVERIFIABLE, "No wall stages ran on this sheet.", stages=[])
    first, last = rows[0]["kept"], rows[-1]["kept"]
    biggest = min(rows, key=lambda r: r.get("kept_change", 0))
    return _result(
        PASS,
        f"{first} candidate(s) traced, {last} kept. "
        + (f"The largest single loss is {-biggest['kept_change']} at '{biggest['stage']}'."
           if biggest.get("kept_change", 0) < 0 else "No stage removed a candidate."),
        stages=rows, traced=first, kept=last, removed=first - last,
    )


def check_provenance(page: dict) -> dict:
    """How each wall's thickness was arrived at.

    Phase 2 established that an exact face measurement exists and is then
    discarded or averaged with a band reading. This reports it in every run
    rather than only under a probe, so the defect is visible on the sheet it
    happens on.
    """
    if not _draws_a_plan(page):
        return _result(NOT_APPLICABLE, f"This sheet is a {_type_of(page)}, which traces no walls.", counts={})
    walls = _kept_walls(page)
    if not walls:
        return _result(UNVERIFIABLE, "No walls were traced on this sheet.", counts={})
    counts: dict = {}
    for wall in walls:
        key = wall.get("thickness_provenance") or "unrecorded"
        counts[key] = counts.get(key, 0) + 1
    exact = sum(v for k, v in counts.items() if k.startswith("faces"))
    averaged = sum(v for k, v in counts.items() if k.startswith("averaged"))
    mixed = counts.get("averaged_mixed", 0)
    stand_in = sum(1 for w in walls if w.get("thickness_is_a_stand_in"))
    fields = {
        "counts": counts,
        "face_derived": exact,
        "band_derived": counts.get("band", 0),
        "averaged": averaged,
        "averaged_across_different_sources": mixed,
        "walls": len(walls),
        "measured_across_the_band_as_a_stand_in": stand_in,
        "band_readings_are_known_to_read_wide": bool(stand_in),
    }
    if mixed:
        return _result(
            FAIL,
            f"{mixed} wall(s) report a thickness averaged from a face measurement and a band "
            "measurement, which is neither of them.",
            **fields,
        )
    detail = (f"{exact} wall(s) measured between their own drawn faces, "
              f"{stand_in} from the inked band as a stand-in, {averaged} combined across pieces.")
    if stand_in:
        detail += (" A band is measured outer edge to outer edge, so it carries the plotted "
                   "stroke on both sides and reads wider than the wall is.")
    return _result(PASS, detail, **fields)


# --- 7. what this kind of sheet can and cannot give --------------------------

# What each extraction needs before it can even be attempted. A check that does
# not apply to a sheet type is never a failure and never a zero.
EXTRACTIONS = ("title_block", "rooms", "dimensions", "scale", "walls", "openings")


def check_applicability(page: dict) -> dict:
    """Which extractions apply to this sheet, which were tried, which finished."""
    plan = _draws_a_plan(page)
    kind = _type_of(page)
    applicable = {
        "title_block": True,
        "rooms": plan,
        "dimensions": True,
        "scale": True,
        "walls": plan,
        "openings": plan,
    }
    completed = {
        "title_block": bool(page.get("title_block_found")),
        "rooms": bool(page.get("rooms")),
        "dimensions": bool(page.get("dimensions")),
        "scale": (page.get("scale_calibration") or {}).get("result") not in (None, "", "not_checked"),
        "walls": bool(_kept_walls(page)),
        "openings": bool(page.get("openings")),
    }
    rows = {}
    for name in EXTRACTIONS:
        if not applicable[name]:
            rows[name] = {"applicable": False, "attempted": False, "completed": False, "status": NOT_APPLICABLE}
            continue
        done = bool(completed[name])
        rows[name] = {
            "applicable": True,
            "attempted": True,
            "completed": done,
            "status": PASS if done else UNVERIFIABLE,
        }
    return _result(
        PASS,
        f"Sheet read as a {kind}.",
        sheet_type=kind,
        sheet_type_confidence=(page.get("page_type") or {}).get("confidence"),
        draws_a_plan=plan,
        extractions=rows,
    )


# --- why a sheet produced nothing -------------------------------------------


def why_nothing_was_found(page: dict) -> str:
    """One sentence a reader can act on, where a sheet produced no geometry.

    "Nothing found" without a reason is not a result — a reader cannot tell a
    drawing with nothing on it from a drawing this tool could not read.
    """
    if _kept_walls(page) or page.get("rooms") or page.get("dimensions"):
        return ""
    if page.get("error"):
        return f"This sheet could not be read: {page['error']}"
    note = page.get("walls_note")
    if note:
        return str(note)
    kind = _type_of(page)
    if not _draws_a_plan(page):
        return (f"This sheet is a {kind}. Walls, rooms and openings are only read from a sheet "
                "that draws the building in plan, so nothing was looked for here.")
    calibration = page.get("scale_calibration") or {}
    if not calibration.get("usable_for_measurement"):
        return ("No length is measured from this sheet, because what one point of it represents "
                "could not be established.")
    return "No wall, room or dimension was found on this sheet, and no reason was recorded for it."


# --- the whole document ------------------------------------------------------


def _document_evidence(pages: list) -> dict:
    return {
        "schedule_rows": _schedule_openings(pages),
        "elevation_openings": _elevation_openings(pages),
    }


# What a plan set has to carry before each downstream output can be produced.
NEEDS = {
    "3D model": "a sheet that draws the building in plan, with an established scale",
    "elevations": "a sheet that draws the building in plan, with an established scale",
    "opening schedule": "a door or window schedule, or marks printed on the plan",
    "material take-off": "a sheet that draws the building in plan, with an established scale",
}


def run(pages: list, config: dict = None) -> dict:
    """Every check, for every sheet, plus the document-level picture."""
    evidence = _document_evidence(pages)
    sheets = []
    for page in pages:
        sheet = {
            "sheet_id": page.get("sheet_id"),
            "page_number": page.get("page_number"),
            "title": _stated(page, "sheet_title"),
            "sheet_type": _type_of(page),
            "nothing_found_because": why_nothing_was_found(page),
            "checks": {},
        }
        for name, fn in (
            ("scale", lambda p: check_scale(p)),
            ("envelope", lambda p: check_envelope(p)),
            ("openings", lambda p: check_openings(p, pages, evidence)),
            ("closure", lambda p: check_closure(p)),
            ("attrition", lambda p: check_attrition(p)),
            ("provenance", lambda p: check_provenance(p)),
            ("applicability", lambda p: check_applicability(p)),
        ):
            try:
                sheet["checks"][name] = fn(page)
            except Exception as e:
                logger.exception(f"self-check '{name}' failed on {page.get('sheet_id')}: {e}")
                sheet["checks"][name] = _result(UNVERIFIABLE, f"This check could not be run: {e}")
        sheets.append(sheet)

    # **A schedule states the building, so it is reconciled against the whole
    # document.** Every mark is printed again on an elevation and again on a
    # section, so what is counted is distinct marks, not how many times each
    # was drawn.
    marks_seen, marks_matched = set(), set()
    for page in pages:
        for opening in page.get("openings") or []:
            mark = opening.get("mark")
            if not mark:
                continue
            marks_seen.add(str(mark))
            if opening.get("in_schedule") or opening.get("schedule_row_id"):
                marks_matched.add(str(mark))
    schedule_check = {
        "schedule_rows": evidence["schedule_rows"],
        "distinct_marks_drawn": len(marks_seen),
        "distinct_marks_matched_to_a_row": len(marks_matched),
        "drawn_but_not_scheduled": sorted(marks_seen - marks_matched)[:40],
        "status": (
            UNVERIFIABLE if not evidence["schedule_rows"]
            else PASS if marks_seen and marks_seen == marks_matched
            else FAIL
        ),
    }

    types_present = sorted({s["sheet_type"] for s in sheets})
    plans = [s for s in sheets if s["checks"]["applicability"].get("draws_a_plan")]
    scale_ok = [s for s in plans if s["checks"]["scale"]["status"] == PASS]

    unavailable = {}
    if not plans:
        for output, needs in NEEDS.items():
            unavailable[output] = f"No sheet in this document draws the building in plan. Needs {needs}."
    elif not scale_ok:
        for output in ("3D model", "elevations", "material take-off"):
            unavailable[output] = (
                "No sheet with a confirmed scale. Every length would be unverified. "
                f"Needs {NEEDS[output]}."
            )
    if not evidence["schedule_rows"] and not evidence["elevation_openings"]:
        unavailable.setdefault(
            "opening schedule",
            "This document carries no door or window schedule and no elevations, so opening "
            "sizes cannot be checked against anything the drawing states twice.",
        )

    tally: dict = {}
    for sheet in sheets:
        for name, check in sheet["checks"].items():
            bucket = tally.setdefault(name, {PASS: 0, FAIL: 0, NOT_APPLICABLE: 0, UNVERIFIABLE: 0})
            bucket[check["status"]] = bucket.get(check["status"], 0) + 1

    return {
        "sheets": sheets,
        "document": {
            "sheet_count": len(sheets),
            "sheet_types_present": types_present,
            "sheets_drawing_a_plan": len(plans),
            "sheets_with_a_confirmed_scale": len(scale_ok),
            "schedule_rows_in_document": evidence["schedule_rows"],
            "openings_shown_on_elevations": evidence["elevation_openings"],
            "schedule_reconciliation": schedule_check,
            "outputs_unavailable": unavailable,
        },
        "totals": tally,
    }


# --- writing it out ----------------------------------------------------------

CSV_COLUMNS = [
    "sheet_id", "page_number", "sheet_type", "check", "status", "detail",
    "key_figures", "nothing_found_because",
]

# The few numbers worth carrying into a spreadsheet, per check.
_FIGURE_KEYS = {
    "scale": ("result", "measured_mm_per_point", "printed_mm_per_point", "variance_pct",
              "strings_used", "scale_dependent_outputs_verified"),
    "envelope": ("printed_mm", "detected_mm", "variance_pct", "axes_compared"),
    "openings": ("tier_used", "tier_name", "expected", "found", "placed", "matched",
                 "missing", "unmatched", "inter_evidence_agreement_pct"),
    "closure": ("walls", "on_a_closed_loop", "dangling", "closed_share_pct"),
    "attrition": ("traced", "kept", "removed"),
    "provenance": ("face_derived", "band_derived", "averaged", "averaged_across_different_sources"),
    "applicability": ("sheet_type", "sheet_type_confidence", "draws_a_plan"),
}


def _figures(name: str, check: dict) -> str:
    bits = []
    for key in _FIGURE_KEYS.get(name, ()):
        if key in check and check[key] is not None:
            value = check[key]
            bits.append(f"{key}={json.dumps(value) if isinstance(value, (dict, list)) else value}")
    return "; ".join(bits)


def write_outputs(report: dict, out_dir) -> dict:
    """`self_check.json` and `self_check.csv` in the run's own folder."""
    from pathlib import Path

    out_dir = Path(out_dir)
    written = {}
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "self_check.json"
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        written["json"] = str(path)
    except Exception as e:
        logger.exception(f"could not write self_check.json: {e}")
    try:
        path = out_dir / "self_check.csv"
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            for sheet in report.get("sheets") or []:
                for name, check in (sheet.get("checks") or {}).items():
                    writer.writerow({
                        "sheet_id": sheet.get("sheet_id"),
                        "page_number": sheet.get("page_number"),
                        "sheet_type": sheet.get("sheet_type"),
                        "check": name,
                        "status": check.get("status"),
                        "detail": check.get("detail"),
                        "key_figures": _figures(name, check),
                        "nothing_found_because": sheet.get("nothing_found_because") or "",
                    })
        written["csv"] = str(path)
    except Exception as e:
        logger.exception(f"could not write self_check.csv: {e}")
    return written


def summary_table(report: dict) -> str:
    """The table printed at the end of a run, for whoever is watching it."""
    lines = []
    lines.append("")
    lines.append("=" * 104)
    lines.append("SELF-CHECK - every result below is computed from evidence inside the uploaded PDF")
    lines.append("=" * 104)
    header = "%-6s %-14s %-9s %-9s %-11s %-9s %-11s %-11s" % (
        "sheet", "type", "scale", "envelope", "openings", "closure", "provenance", "attrition")
    lines.append(header)
    lines.append("-" * 104)
    short = {PASS: "pass", FAIL: "FAIL", NOT_APPLICABLE: "n/a", UNVERIFIABLE: "unverif."}
    for sheet in report.get("sheets") or []:
        checks = sheet.get("checks") or {}
        lines.append("%-6s %-14s %-9s %-9s %-11s %-9s %-11s %-11s" % (
            sheet.get("sheet_id") or "?",
            (sheet.get("sheet_type") or "?")[:14],
            short.get(checks.get("scale", {}).get("status"), "?"),
            short.get(checks.get("envelope", {}).get("status"), "?"),
            short.get(checks.get("openings", {}).get("status"), "?"),
            short.get(checks.get("closure", {}).get("status"), "?"),
            short.get(checks.get("provenance", {}).get("status"), "?"),
            short.get(checks.get("attrition", {}).get("status"), "?"),
        ))
    lines.append("-" * 104)
    for name, bucket in (report.get("totals") or {}).items():
        lines.append("%-14s pass %-4d FAIL %-4d not applicable %-4d unverifiable %-4d" % (
            name, bucket.get(PASS, 0), bucket.get(FAIL, 0),
            bucket.get(NOT_APPLICABLE, 0), bucket.get(UNVERIFIABLE, 0)))
    document = report.get("document") or {}
    lines.append("-" * 104)
    reconciliation = document.get("schedule_reconciliation") or {}
    if reconciliation:
        lines.append("schedule reconciliation: %s rows, %s distinct marks drawn, %s matched to a row [%s]" % (
            reconciliation.get("schedule_rows"), reconciliation.get("distinct_marks_drawn"),
            reconciliation.get("distinct_marks_matched_to_a_row"), reconciliation.get("status")))
    lines.append("sheets %s | drawing a plan %s | with a confirmed scale %s | schedule rows %s | elevation openings %s" % (
        document.get("sheet_count"), document.get("sheets_drawing_a_plan"),
        document.get("sheets_with_a_confirmed_scale"),
        document.get("schedule_rows_in_document"), document.get("openings_shown_on_elevations")))
    for output, why in (document.get("outputs_unavailable") or {}).items():
        lines.append("  UNAVAILABLE - %s: %s" % (output, why))
    lines.append("=" * 104)
    return "\n".join(lines)
