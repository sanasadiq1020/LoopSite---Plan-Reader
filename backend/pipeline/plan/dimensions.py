"""Day 3 — dimension strings, with the axis they measure and an arithmetic check.

Three things make this more than a number scraper, and all three are needed by
the stages that come after Week 1.

**Axis.**  A dimension printed rotated 90 degrees measures the building's
vertical axis; one printed horizontally measures its horizontal axis.  The PDF
records that as the text's writing direction, so it is read (see
``textmodel``) rather than guessed.  Without it, Day 5 cannot place a wall:
'11,030' alone says nothing about which way the wall runs.  The earlier
version discarded direction entirely, which made wall generation impossible
from its output no matter how accurate the numbers were.

**Chains.**  Dimensions on a drawing are printed in strings: a run of
individual figures along one line, with the total printed alongside, usually
tagged 'OVERALL'.  Grouping them by shared alignment and consistent spacing
recovers those strings.

**An arithmetic check.**  Once a chain and its overall are known, the running
figures must add up to the total.  That comparison is a genuine test of the
extraction — it catches a missed figure, a misread digit, and a figure grabbed
from a neighbouring string — and it needs no external ground truth, because
the drawing is checking itself.  Week 1's rule is that nothing is called
accurate without a calculation and a tolerance; for dimensions, this is that
calculation.  Chains that fail are flagged, never quietly accepted.

Attribution is deliberately conservative.  A dimension is only linked to a
room when one room label is clearly the nearest; where two are equally close
the dimension is reported as unlinked with the reason.  The earlier version
attached the closest label unconditionally, which produced confident but wrong
links (a running dimension in a chain reported as belonging to window W8), and
a wrong link is worse than none because every later stage trusts it.
"""

import re

from pipeline.plan.textmodel import bbox_center, bbox_height, bbox_width
from pipeline.plan.rooms import nearest_room

# Australian drawings write dimensions as bare millimetres ('3600'), with a
# comma thousands separator ('11,830'), or occasionally in metres ('3.6').
# The comma-grouped alternative is listed first so '11,830' is not cut at '11'.
_NUMBER = r"\d{1,3}(?:,\d{3})+|\d+(?:\.\d{1,3})?"

_PAIRED_RE = re.compile(
    r"^\(?\s*(" + _NUMBER + r")\s*(?:MM|M)?\s*[xX×*]\s*(" + _NUMBER + r")\s*(?:MM|M)?\s*\)?$"
)
_MM_RE = re.compile(r"^(" + _NUMBER + r")\s*MM$")
_M_RE = re.compile(r"^(" + _NUMBER + r")\s*M$")
# Reduced-level prefixes are a per-office vocabulary, so the pattern is built
# from config rather than fixed here. A hardcoded list silently missed 'F.C.L.'
# and 'S.S.L.' on an unseen plan set even though both were listed in config.
_DEFAULT_LEVEL_PREFIXES = ["RL", "R.L.", "FFL", "F.F.L.", "NGL", "N.G.L.", "AHD"]
_level_pattern_cache: dict = {}


def _level_pattern(prefixes):
    key = tuple(prefixes)
    cached = _level_pattern_cache.get(key)
    if cached is None:
        # Longest first, so 'F.F.L.' is matched before 'FL'.
        alternatives = "|".join(
            re.escape(prefix) for prefix in sorted(prefixes, key=len, reverse=True)
        )
        cached = re.compile(
            r"^(" + alternatives + r")\s*[:=]?\s*([+-]?\d+(?:\.\d{1,3})?)$",
            re.IGNORECASE,
        )
        _level_pattern_cache[key] = cached
    return cached
_BARE_RE = re.compile(r"^(" + _NUMBER + r")$")

# OCR reads the thousands comma on these drawings as a full stop, so a printed
# "7,370" comes back as "7.370". Both readings give the same length in
# millimetres — as a misread separator it is 7370, and as a genuine metre
# value 7.370 m is also 7370 mm — so a figure in this shape can be converted
# without having to decide which it was. A figure with one or two decimals
# ("3.6") is a real metre value and is handled separately.
_GROUPED_DECIMAL_RE = re.compile(r"^(\d{1,3})\.(\d{3})$")

# Two dimension strings measure the same distance only if both of their ends
# line up this closely. Deliberately tight: a loose match turns a pair of
# strings measuring genuinely different spans into a reported error.
_ENDPOINT_TOLERANCE_PT = 8.0


def _to_mm(number_text: str, unit: str) -> float:
    value = float(number_text.replace(",", ""))
    if unit == "m":
        return value * 1000.0
    return value


def _strip_overall(text: str, overall_keywords: list):
    """Separates a trailing total marker from the figure: '22,530 OVERALL'."""
    cleaned = text.strip()
    for keyword in sorted(overall_keywords, key=len, reverse=True):
        pattern = re.compile(r"\s*" + re.escape(keyword) + r"\s*$", re.IGNORECASE)
        if pattern.search(cleaned):
            return pattern.sub("", cleaned).strip(), True
    return cleaned, False


def _strip_measured_to(text: str, keywords: list):
    """Separates a trailing note saying what a figure measures to.

    Offices differ on this. One writes a bare '10260'; another writes
    '10260 TO WALL', '2600 TO EAVE', '3200 TO BOUNDARY' — the figure and what
    it is measured to, on one printed line. Both are the same measurement.
    Without this the second office's site plan reported no dimensions at all,
    because every figure it prints carries a note.

    The note is kept, not discarded: it says which face of the building the
    figure runs to, which is exactly what a reviewer needs and what Day 5 needs
    to place a wall.
    """
    cleaned = text.strip()
    for keyword in sorted(keywords or [], key=len, reverse=True):
        pattern = re.compile(r"\s*\b" + re.escape(keyword) + r"\b\s*$", re.IGNORECASE)
        if pattern.search(cleaned):
            return pattern.sub("", cleaned).strip(), keyword.upper()
    return cleaned, None


def _classify(text: str, config: dict):
    """Returns a dimension reading for one printed line, or None.

    A bare number is only read as a dimension when the printed line is
    essentially just that number. A number embedded in a sentence or a product
    callout ('100x100 Steel posts') is text on a drawing, not a measurement,
    and inventing a dimension from it would be a silent guess.
    """
    dimension_config = config["dimensions"]
    bare_range = dimension_config["bare_number_range_mm"]
    overall_keywords = dimension_config.get("overall_keywords", ["OVERALL"])

    level_prefixes = dimension_config.get("level_prefixes") or _DEFAULT_LEVEL_PREFIXES

    stripped, is_overall = _strip_overall(text, overall_keywords)
    stripped, measured_to = _strip_measured_to(
        stripped, dimension_config.get("measured_to_keywords", [])
    )
    upper = stripped.upper()

    match = _level_pattern(level_prefixes).match(upper)
    if match:
        # Reduced levels are printed in metres on Australian drawings.
        try:
            metres = float(match.group(2))
        except ValueError:
            return None
        return {
            "kind": "level",
            "value_mm": round(metres * 1000.0, 1),
            "width_mm": None,
            "height_mm": None,
            "unit_source": "explicit_level_metres",
            "is_overall": False,
            "level_reference": match.group(1).upper().replace(".", ""),
            "measured_to": measured_to,
            "base_confidence": 0.9,
        }

    match = _PAIRED_RE.match(upper)
    if match:
        unit = "m" if re.search(r"\bM\b", upper) and "MM" not in upper else "mm"
        try:
            width = _to_mm(match.group(1), unit)
            height = _to_mm(match.group(2), unit)
        except ValueError:
            return None
        return {
            "kind": "paired",
            "value_mm": None,
            "width_mm": width,
            "height_mm": height,
            "unit_source": "explicit" if unit == "m" or "MM" in upper else "assumed_mm",
            "is_overall": is_overall,
            "level_reference": None,
            "measured_to": measured_to,
            "base_confidence": 0.9,
        }

    match = _MM_RE.match(upper)
    if match:
        return {
            "kind": "linear",
            "value_mm": _to_mm(match.group(1), "mm"),
            "width_mm": None,
            "height_mm": None,
            "unit_source": "explicit_mm",
            "is_overall": is_overall,
            "level_reference": None,
            "measured_to": measured_to,
            "base_confidence": 0.95,
        }

    match = _M_RE.match(upper)
    if match:
        return {
            "kind": "linear",
            "value_mm": _to_mm(match.group(1), "m"),
            "width_mm": None,
            "height_mm": None,
            "unit_source": "explicit_metres",
            "is_overall": is_overall,
            "level_reference": None,
            "measured_to": measured_to,
            "base_confidence": 0.85,
        }

    match = _GROUPED_DECIMAL_RE.match(upper)
    if match:
        value = float(match.group(1) + match.group(2))
        if bare_range["min"] <= value <= bare_range["max"]:
            return {
                "kind": "linear",
                "value_mm": value,
                "width_mm": None,
                "height_mm": None,
                "unit_source": "grouped_or_metres",
                "is_overall": is_overall,
                "level_reference": None,
            "measured_to": measured_to,
                "base_confidence": 0.8,
            }

    match = _BARE_RE.match(upper)
    if match:
        try:
            value = float(match.group(1).replace(",", ""))
        except ValueError:
            return None
        if not (bare_range["min"] <= value <= bare_range["max"]):
            return None
        return {
            "kind": "linear",
            "value_mm": value,
            "width_mm": None,
            "height_mm": None,
            "unit_source": "assumed_mm",
            "is_overall": is_overall,
            "level_reference": None,
            "measured_to": measured_to,
            # A bare number carries a real assumption (that the drawing is in
            # millimetres), so it never scores as high as one printed with its
            # unit, and the assumption travels with the record.
            "base_confidence": 0.8,
        }

    return None


def _measured_axis(line: dict) -> str:
    """Which building axis this dimension measures.

    Text written horizontally dimensions the horizontal direction; text rotated
    90 degrees dimensions the vertical direction. This is a drafting
    convention, not an inference from the number.
    """
    return "y" if line["axis"] == "vertical" else "x"


def detect_dimensions(lines: list, config: dict, sheet_id: str, exclude_bboxes=None) -> list:
    thresholds = config["confidence_thresholds"]
    dimension_config = config["dimensions"]
    excluded = exclude_bboxes or set()

    found: list = []
    for line in lines:
        if tuple(line["bbox"]) in excluded:
            continue
        reading = _classify(line["text"], config)
        if reading is None:
            continue

        axis = _measured_axis(line)
        confidence = round(min(line["confidence"] * reading["base_confidence"], 1.0), 3)
        band = (
            "high"
            if confidence >= thresholds["review"]
            else ("review" if confidence >= thresholds["low"] else "low")
        )
        found.append(
            {
                "dimension_id": "",
                "text": line["text"].strip(),
                "kind": reading["kind"],
                "measures_axis": axis if reading["kind"] != "level" else "z",
                "value_mm": reading["value_mm"],
                "width_mm": reading["width_mm"],
                "height_mm": reading["height_mm"],
                "unit_source": reading["unit_source"],
                "unit_assumption": (
                    dimension_config.get("assumed_unit_note")
                    if reading["unit_source"] == "assumed_mm"
                    else None
                ),
                "is_overall": reading["is_overall"],
                "measured_to": reading.get("measured_to"),
                "level_reference": reading["level_reference"],
                "chain_id": None,
                "chain_role": None,
                "linked_room_id": None,
                "link_method": None,
                "link_note": None,
                "bbox": line["bbox"],
                "confidence": confidence,
                "confidence_band": band,
                "extraction_method": line["extraction_method"],
                "review_status": "confirmed" if band == "high" else "needs_review",
            }
        )

    found.sort(key=lambda d: (round(d["bbox"][1], 1), round(d["bbox"][0], 1)))
    for position, dimension in enumerate(found, start=1):
        dimension["dimension_id"] = f"{sheet_id}-DIM{position:03d}"
    return found


# --- Chains ---------------------------------------------------------------


def _chain_key(dimension: dict) -> float:
    """The coordinate a chain shares: the perpendicular one. A horizontal
    string of dimensions all sit at the same y; a vertical string at the same
    x."""
    cx, cy = bbox_center(dimension["bbox"])
    return cy if dimension["measures_axis"] == "x" else cx


def _chain_position(dimension: dict) -> float:
    cx, cy = bbox_center(dimension["bbox"])
    return cx if dimension["measures_axis"] == "x" else cy


def build_chains(dimensions: list, config: dict, sheet_id: str) -> list:
    """Groups dimensions printed as one string, and checks each string adds up.

    A chain is a run of dimensions on the same axis, sharing a common
    perpendicular position (they sit on one dimension line), with no gap larger
    than the configured maximum between consecutive members.
    """
    dimension_config = config["dimensions"]
    alignment_tolerance = dimension_config.get("chain_alignment_tolerance_pt", 6.0)
    max_gap = dimension_config.get("chain_max_gap_pt", 140.0)
    sum_tolerance = dimension_config.get("chain_sum_tolerance_pct", 2.0)
    parallel_tolerance = dimension_config.get("chain_parallel_tolerance_pct", 5.0)

    linear = [
        d
        for d in dimensions
        if d["kind"] == "linear" and d["value_mm"] is not None and not d["is_overall"]
    ]
    overalls = [d for d in dimensions if d["kind"] == "linear" and d["is_overall"]]

    chains: list = []
    for axis in ("x", "y"):
        members = [d for d in linear if d["measures_axis"] == axis]
        if not members:
            continue
        members.sort(key=lambda d: (_chain_key(d), _chain_position(d)))

        bands: list = []
        for dimension in members:
            key = _chain_key(dimension)
            placed = False
            for band in bands:
                if abs(_chain_key(band[0]) - key) <= alignment_tolerance:
                    band.append(dimension)
                    placed = True
                    break
            if not placed:
                bands.append([dimension])

        for band in bands:
            band.sort(key=_chain_position)
            run = [band[0]]
            for previous, current in zip(band, band[1:]):
                gap = _chain_position(current) - _chain_position(previous)
                if gap > max_gap:
                    if len(run) >= 2:
                        chains.append((axis, run))
                    run = [current]
                else:
                    run.append(current)
            if len(run) >= 2:
                chains.append((axis, run))

    # Prepare each chain, then assign the printed overalls.
    prepared: list = []
    for position, (axis, members) in enumerate(chains, start=1):
        chain_id = f"{sheet_id}-CH{position:02d}"
        total = sum(d["value_mm"] for d in members)
        extent = (
            min(d["bbox"][0] for d in members),
            min(d["bbox"][1] for d in members),
            max(d["bbox"][2] for d in members),
            max(d["bbox"][3] for d in members),
        )
        for dimension in members:
            dimension["chain_id"] = chain_id
            dimension["chain_role"] = "running"
        prepared.append((chain_id, axis, members, total, extent))

    # An 'OVERALL' figure belongs to exactly one dimension string — the one it
    # spans. Letting several chains each claim the same overall produced a
    # fabricated failure on the supplied plan: a short three-figure chain was
    # compared against the building's full 19,920 mm width. So each overall is
    # assigned once, to the chain it fits best (nearest perpendicular, and
    # covering the largest share of the overall's own span).
    assignment: dict = {}
    for candidate in overalls:
        if candidate["value_mm"] is None:
            continue
        best = None
        best_score = None
        for chain_id, axis, members, total, extent in prepared:
            if candidate["measures_axis"] != axis:
                continue
            candidate_position = _chain_position(candidate)
            span_start = extent[0] if axis == "x" else extent[1]
            span_end = extent[2] if axis == "x" else extent[3]
            if not (span_start - 20.0 <= candidate_position <= span_end + 20.0):
                continue
            distance = abs(_chain_key(candidate) - _chain_key(members[0]))
            span = max(span_end - span_start, 1.0)
            # Prefer the longest chain the overall sits over, then the nearest.
            score = (-span, distance)
            if best_score is None or score < best_score:
                best_score = score
                best = chain_id
        if best is not None and best not in assignment:
            assignment[best] = candidate

    records: list = []
    for chain_id, axis, members, total, extent in prepared:
        matched_overall = assignment.get(chain_id)

        check = {
            "overall_dimension_id": None,
            "overall_mm": None,
            "sum_of_running_mm": round(total, 1),
            "difference_mm": None,
            "variance_pct": None,
            "tolerance_pct": sum_tolerance,
            "result": "not_checked",
            "note": "No total is printed alongside this string, so there is nothing to check it against.",
        }
        if matched_overall is not None:
            matched_overall["chain_id"] = chain_id
            matched_overall["chain_role"] = "overall"
            overall_mm = matched_overall["value_mm"]
            difference = total - overall_mm
            variance = (abs(difference) / overall_mm * 100.0) if overall_mm else None
            check.update(
                {
                    "overall_dimension_id": matched_overall["dimension_id"],
                    "overall_mm": round(overall_mm, 1),
                    "difference_mm": round(difference, 1),
                    "variance_pct": round(variance, 2) if variance is not None else None,
                    "result": (
                        "pass"
                        if variance is not None and variance <= sum_tolerance
                        else "fail"
                    ),
                    "note": (
                        "The individual figures add up to the total printed on the drawing."
                        if variance is not None and variance <= sum_tolerance
                        else (
                            "The individual figures do not add up to the total printed on "
                            "the drawing. A figure may be missing or misread."
                        )
                    ),
                }
            )

        records.append(
            {
                "chain_id": chain_id,
                "axis": axis,
                "member_dimension_ids": [d["dimension_id"] for d in members],
                "member_count": len(members),
                "sum_mm": round(total, 1),
                "bbox": [round(v, 2) for v in extent],
                "check": check,
                "parallel_check": None,
            }
        )

    _check_parallel_chains(records, parallel_tolerance)
    return records


def _check_parallel_chains(chains: list, tolerance_pct: float) -> None:
    """Cross-checks two dimension strings that measure the same span.

    A floor plan normally dimensions each direction more than once — a string
    above the plan and another below it, both spanning the whole building.
    Those are two independent extractions of the same distance, so requiring
    them to agree is a real test that needs no 'OVERALL' tag and no external
    ground truth. It is what gives an arithmetic check on plans that never
    print the word.

    This comparison uses a wider tolerance than the printed-total check, and
    deliberately so: two strings spanning the same opening can measure to
    different faces of the same wall and differ by a wall thickness without
    either being wrong. Only a difference too large to explain that way is
    worth raising.

    Two chains are compared only when **both ends** line up, not merely when
    they overlap. That distinction is what makes the check trustworthy: a
    string running from the front wall to the garage and another running from
    the front wall to the rear wall overlap almost completely, yet they
    measure different distances, so requiring them to be equal would report a
    fabricated error. Only strings that start and finish at the same place
    measure the same distance. Fewer checks that mean something are worth more
    than many that need explaining away.
    """
    for axis in ("x", "y"):
        candidates = [c for c in chains if c["axis"] == axis]
        for index, chain in enumerate(candidates):
            if chain["check"]["result"] in ("pass", "fail"):
                continue
            if chain.get("parallel_check"):
                continue
            start = chain["bbox"][0] if axis == "x" else chain["bbox"][1]
            end = chain["bbox"][2] if axis == "x" else chain["bbox"][3]
            span = end - start
            if span <= 0:
                continue
            endpoint_tolerance = max(_ENDPOINT_TOLERANCE_PT, span * 0.02)
            for other in candidates[index + 1 :]:
                if other.get("parallel_check"):
                    continue
                other_start = other["bbox"][0] if axis == "x" else other["bbox"][1]
                other_end = other["bbox"][2] if axis == "x" else other["bbox"][3]
                if (
                    abs(other_start - start) > endpoint_tolerance
                    or abs(other_end - end) > endpoint_tolerance
                ):
                    continue

                difference = chain["sum_mm"] - other["sum_mm"]
                reference = max(chain["sum_mm"], other["sum_mm"])
                variance = (abs(difference) / reference * 100.0) if reference else None
                result = {
                    "compared_with": other["chain_id"],
                    "this_sum_mm": chain["sum_mm"],
                    "other_sum_mm": other["sum_mm"],
                    "difference_mm": round(difference, 1),
                    "variance_pct": round(variance, 2) if variance is not None else None,
                    "tolerance_pct": tolerance_pct,
                    "result": "pass"
                    if variance is not None and variance <= tolerance_pct
                    else "fail",
                    "note": (
                        "Two dimension strings covering the same distance agree."
                        if variance is not None and variance <= tolerance_pct
                        else (
                            "Two dimension strings covering the same distance disagree. "
                            "At least one is incomplete or misread."
                        )
                    ),
                }
                chain["parallel_check"] = result
                other["parallel_check"] = {**result, "compared_with": chain["chain_id"],
                                           "this_sum_mm": other["sum_mm"],
                                           "other_sum_mm": chain["sum_mm"],
                                           "difference_mm": round(-difference, 1)}
                break


# --- Attribution ----------------------------------------------------------


def link_dimensions_to_rooms(dimensions: list, rooms: list, config: dict) -> None:
    """Links a dimension to a room only where the geometry is unambiguous."""
    dimension_config = config["dimensions"]
    search_heights = dimension_config.get("room_label_search_heights", 22.0)

    room_by_dimension_bbox = {}
    for room in rooms:
        if room.get("dimension_bbox"):
            room_by_dimension_bbox[tuple(room["dimension_bbox"])] = room

    for dimension in dimensions:
        # A paired dimension that was read as a room's size callout is already
        # attributed with certainty — the room label sits directly above it.
        room = room_by_dimension_bbox.get(tuple(dimension["bbox"]))
        if room is not None:
            dimension["linked_room_id"] = room["room_id"]
            dimension["link_method"] = "room_size_callout"
            dimension["link_note"] = f"Printed directly beneath “{room['name']}”."
            continue

        if dimension["kind"] == "level":
            dimension["link_note"] = "A level reference, not a room measurement."
            continue

        height = bbox_height(dimension["bbox"]) or 8.0
        width = bbox_width(dimension["bbox"]) or 8.0
        radius = max(height, width) * search_heights
        room, reason = nearest_room(rooms, dimension["bbox"], radius)
        if room is None:
            dimension["link_note"] = reason or "Not linked to a room."
            continue
        dimension["linked_room_id"] = room["room_id"]
        dimension["link_method"] = "nearest_room_label"
        dimension["link_note"] = (
            "Closest room label on the sheet, and clearly closer than any other. "
            "Based on position only — the drawing does not state the link."
        )
