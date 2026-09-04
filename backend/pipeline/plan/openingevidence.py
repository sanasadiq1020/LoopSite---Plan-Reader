"""What makes a break in a wall a door or a window: four separate readings.

**A gap is not an opening.** A wall is two parallel faces, and both of them
stop at the same place for a great many reasons that are not a hole: where
another wall lands on it, where a cupboard or a bulkhead is drawn against it,
where the drawing was traced from a picture and the line thinned out, where a
hatch boundary ends. Reporting every one of those as a door produced openings
nobody could check and a count nearly half made of blanks.

So a break in a wall is a **candidate**, and the drawing has to say
independently that something goes in it. It can say so four ways, and a plan
set drawn with any care says it twice or more:

1.  **The arc of a hinged door.** Its centre is the hinge, which sits at a jamb
    of the gap, and its radius *is* the door leaf. Asking the drawing "is there
    an arc centred here whose radius is this gap's width?" is a question with
    an answer, unlike asking it where the doors are.
2.  **The mark printed beside it** - ``D12``, ``W04``, ``SD01``. The prefix
    says whether it is a door or a window and the whole mark keys it to a
    schedule row.
3.  **The glazing drawn inside the wall.** A wall is solid and nothing is drawn
    inside one; a window is not, and the drawing puts the glass and its frame
    between the faces over exactly the width of the opening.
4.  **The schedule row** that mark names, which carries the width, the height
    and the type as the office itself stated them.
5.  **The leaf width printed at the opening** - ``870``, ``920``. Many plans
    dimension a door this way instead of, or as well as, scheduling it, and it
    is the drawing stating the opening's own size at the opening's own place.

**And the count decides**, with nothing set aside for a person:

| sources | what happens |
|---|---|
| none | **not an opening.** Written to the issues log as an unresolved gap |
| one | an opening, and ``review_needed`` is true |
| two or more | an opening, and ``review_needed`` is false |

A source can only ever be counted once, however many ways it was reached: the
arc read off the drawing's own curves and the arc read off the page as a
picture are the same arc, so both are ``arc_geometry``. Counting them twice
would turn one reading into a confirmation of itself.

Every threshold lives in ``config/opening_config.json``. Nothing in this module
carries a coordinate, a room name, a mark or a count from any drawing
(Critical Rule 1).
"""

import re

from app.logging_setup import get_logger
from pipeline.plan.symbols import _arc_drawn_on, page_drawings
from pipeline.plan.textmodel import bbox_center

logger = get_logger()

# The four readings, named once. These strings are the evidence vocabulary for
# the whole pipeline - the table on screen, the CSV and the issues log all
# spell them this way - so nothing else invents a name of its own.
ARC_GEOMETRY = "arc_geometry"
TEXT_LABEL = "text_label"
GLAZING_SYMBOL = "glazing_symbol"
SCHEDULE_ENTRY = "schedule_entry"
LEAF_DIMENSION = "leaf_dimension"

SOURCES = (
    ARC_GEOMETRY, TEXT_LABEL, GLAZING_SYMBOL, SCHEDULE_ENTRY, LEAF_DIMENSION,
)

# What each reading is called on screen and in the issues log. A reader is
# never shown a field name.
IN_WORDS = {
    ARC_GEOMETRY: "the door's swing drawn on the plan",
    TEXT_LABEL: "the mark printed beside it",
    GLAZING_SYMBOL: "the glazing drawn inside the wall",
    SCHEDULE_ENTRY: "the schedule row for that mark",
    LEAF_DIMENSION: "the leaf width printed at the opening",
}

# Which source names the kind of opening most authoritatively when two of them
# disagree. The swing is the door leaf itself, swept to its own width; the
# glazing is the opening, drawn between the faces; a schedule row and a mark
# are both references to something typed, and a mark can be typed against the
# wrong code. A printed leaf width is the weakest: it says a size, and only a
# drafting convention says that size is a door - so it never overrules a symbol
# drawn to scale.
#
# The numbers were rescaled when the fifth was added; the order of the first
# four is unchanged, and only their order is ever compared.
SOURCE_RANK = {
    ARC_GEOMETRY: 5,
    GLAZING_SYMBOL: 4,
    SCHEDULE_ENTRY: 3,
    TEXT_LABEL: 2,
    LEAF_DIMENSION: 1,
}


def settings_for(config: dict) -> dict:
    """The opening reader's own settings."""
    return config.get("openings", {})


def _millimetres_per_point(calibration: dict) -> float:
    return float(
        calibration.get("measured_mm_per_point")
        or calibration.get("printed_mm_per_point")
        or 0.0
    )


def _run_of(wall: dict):
    start, end = wall["start_point_pt"], wall["end_point_pt"]
    if wall["runs_along"] == "x":
        return min(start[0], end[0]), max(start[0], end[0])
    return min(start[1], end[1]), max(start[1], end[1])


def _point_along(wall: dict, position_pt: float):
    """The point on the sheet this far along the wall's centreline."""
    start = wall["start_point_pt"]
    if wall["runs_along"] == "x":
        return (position_pt, start[1])
    return (start[0], position_pt)


def _fractions(wall: dict, low: float, high: float):
    run_low, run_high = _run_of(wall)
    length = run_high - run_low
    if length <= 0:
        return 0.0, 1.0
    return (
        max(0.0, (low - run_low) / length),
        min(1.0, (high - run_low) / length),
    )


def _box_across(wall: dict, low: float, high: float, mm_per_point: float):
    """The gap's own box on the sheet: its width, the wall's thickness."""
    half = wall["thickness_mm"] / mm_per_point / 2.0
    start = wall["start_point_pt"]
    if wall["runs_along"] == "x":
        return [low, start[1] - half, high, start[1] + half]
    return [start[0] - half, low, start[0] + half, high]


# --- the candidates --------------------------------------------------------


def gap_candidates(walls: list, calibration: dict, config: dict, sheet_id: str) -> list:
    """Every break in a wall worth asking the drawing about.

    A break is kept as a candidate when it is a plausible opening width and has
    wall on both sides of it. **A break where another wall lands is a junction,
    not a door**, and the two are told apart by width: a junction break is
    about as wide as the wall that made it, while a door is the width of a
    door.
    """
    from pipeline.plan.walls import break_is_a_junction

    mm_per_point = _millimetres_per_point(calibration)
    if not mm_per_point or not calibration.get("usable_for_measurement"):
        return []

    settings = settings_for(config)
    limits = settings.get("candidate_gap", {})
    smallest = float(limits.get("min_width_mm", 300))
    largest = float(limits.get("max_width_mm", 6000))
    flank = float(limits.get("min_wall_each_side_mm", 300)) / mm_per_point

    wall_settings = config.get("walls", {})
    junction_slack = float(wall_settings.get("junction_tolerance_points", 10.0))
    junction_share = float(wall_settings.get("junction_share_of_a_break", 0.6))

    candidates = []
    for wall in walls:
        run_low, run_high = _run_of(wall)
        span = run_high - run_low
        if span <= 0:
            continue
        for gap_start, gap_end in wall.get("gaps_pt") or []:
            low, high = min(gap_start, gap_end), max(gap_start, gap_end)
            width_mm = (high - low) * mm_per_point
            if not (smallest <= width_mm <= largest):
                continue
            if low - run_low < flank or run_high - high < flank:
                continue
            if break_is_a_junction(
                wall, low, high, walls, junction_slack, junction_share
            ):
                continue
            start_fraction, end_fraction = _fractions(wall, low, high)
            candidates.append(
                {
                    "wall": wall,
                    "wall_id": wall["wall_id"],
                    "start_pt": round(low, 3),
                    "end_pt": round(high, 3),
                    "start_fraction": start_fraction,
                    "end_fraction": end_fraction,
                    "width_mm": float(round(width_mm)),
                    "jambs": [_point_along(wall, low), _point_along(wall, high)],
                    "bbox": [
                        round(v, 2) for v in _box_across(wall, low, high, mm_per_point)
                    ],
                    "sheet_id": sheet_id,
                    "evidence": [],
                }
            )
    return candidates


# --- source 1: the arc a hinged door is drawn with --------------------------


def _arcs_on(page) -> list:
    """Every arc the drawing itself holds, as a centre and a radius in points.

    A quarter circle stored as a Bezier gives both up exactly: the arc bulges
    away from its centre, so of the corners of the box its two ends make, the
    centre is the one **furthest** from the middle of the curve.
    """
    if page is None:
        return []
    try:
        drawings = page_drawings(page)
    except Exception as e:
        logger.exception(f"could not read the sheet's drawn paths for door arcs: {e}")
        return []

    arcs = []
    for path in drawings:
        try:
            for item in path.get("items", []):
                if item[0] != "c" or len(item) < 5:
                    continue
                p1, p2, p3, p4 = item[1], item[2], item[3], item[4]
                # The point halfway round the curve, from the Bezier itself.
                middle = (
                    (p1.x + 3 * p2.x + 3 * p3.x + p4.x) / 8.0,
                    (p1.y + 3 * p2.y + 3 * p3.y + p4.y) / 8.0,
                )
                x0, x1 = min(p1.x, p4.x), max(p1.x, p4.x)
                y0, y1 = min(p1.y, p4.y), max(p1.y, p4.y)
                corners = [(x0, y0), (x0, y1), (x1, y0), (x1, y1)]
                centre = max(
                    corners,
                    key=lambda c: (c[0] - middle[0]) ** 2 + (c[1] - middle[1]) ** 2,
                )
                first = ((p1.x - centre[0]) ** 2 + (p1.y - centre[1]) ** 2) ** 0.5
                last = ((p4.x - centre[0]) ** 2 + (p4.y - centre[1]) ** 2) ** 0.5
                if first <= 0 or last <= 0:
                    continue
                # A curve whose two ends are not the same distance from that
                # corner is not an arc about it - it is a fillet, a cloud or a
                # piece of lettering.
                if abs(first - last) / max(first, last) > 0.25:
                    continue
                arcs.append({"centre": centre, "radius_pt": (first + last) / 2.0})
        except Exception:
            # One malformed path must never take the sheet down (Critical Rule 6).
            continue
    return arcs


def _radius_agrees(radius_mm: float, width_mm: float, settings: dict) -> bool:
    allowed = max(
        float(settings.get("radius_tolerance_mm", 150)),
        float(settings.get("radius_tolerance_share", 0.2)) * width_mm,
    )
    return abs(radius_mm - width_mm) <= allowed


def arc_evidence(candidates: list, page, calibration: dict, config: dict) -> int:
    """Marks every candidate gap that a door's swing is drawn against.

    **Centre near a jamb, radius the width of the gap.** Both come from the
    reading, so the drawing only has to confirm or deny - which is why this
    works where searching the page for circles does not.

    The drawing's own curves are read first and are exact. Only where the sheet
    holds none against any candidate is the page rendered and the same question
    asked of the pixels, and the answer is the same source either way: an arc
    read two ways is one arc.
    """
    settings = settings_for(config).get("arc_geometry", {})
    if not settings.get("enabled", True) or not candidates:
        return 0
    mm_per_point = _millimetres_per_point(calibration)
    if not mm_per_point:
        return 0

    near = float(settings.get("centre_to_jamb_max_mm", 400)) / mm_per_point
    found = 0
    for arc in _arcs_on(page):
        radius_mm = arc["radius_pt"] * mm_per_point
        for candidate in candidates:
            if ARC_GEOMETRY in candidate["evidence"]:
                continue
            if not _radius_agrees(radius_mm, candidate["width_mm"], settings):
                continue
            if not any(
                ((arc["centre"][0] - x) ** 2 + (arc["centre"][1] - y) ** 2) ** 0.5 <= near
                for x, y in candidate["jambs"]
            ):
                continue
            candidate["evidence"].append(ARC_GEOMETRY)
            candidate["arc_radius_mm"] = float(round(radius_mm))
            candidate["arc_read_from"] = "the drawing's own curve"
            found += 1
            break

    # **The page is read only where the sheet's own curves answered nothing.**
    # The drawing's arcs are exact and nothing recovered from pixels can beat
    # them, so they are never displaced — this is for the sheet drawn as a
    # picture, which has none to displace.
    if any(ARC_GEOMETRY in candidate["evidence"] for candidate in candidates):
        return found
    if not settings.get("read_from_the_page_when_the_drawing_has_none", True):
        return 0
    return _arcs_on_the_page(candidates, page, mm_per_point, settings)


def _arcs_on_the_page(candidates: list, page, mm_per_point: float, settings: dict) -> int:
    """The same question, asked of the page as a picture.

    A plan set can be published as pictures, and one drawn that way holds no
    curve for its doors at all - the quarter circle is pixels. The place and
    the radius still come from the reading; only the confirming is done here.

    The jamb is searched a few pixels around, because the wall was traced from
    the same picture and is not exact to the pixel: at this radius, being three
    pixels out breaks the arc into fragments and nothing is found at all.
    """
    if page is None:
        return 0
    try:
        import numpy as np
    except ImportError:
        return 0
    try:
        from pipeline.plan.rasterlines import _rendered_page

        # The same render the wall reader uses, so the arcs and the walls they
        # spring from are in one coordinate system. It comes back in grey, and
        # ink is dark: what is wanted here is simply where the drawing is inked.
        grey, scale, origin = _rendered_page(
            page, float(settings.get("page_render_dpi", 200))
        )
        mask = (grey < int(settings.get("ink_darker_than", 200))).astype(np.uint8)
    except Exception as e:
        logger.exception(f"could not render the page to look for door arcs: {e}")
        return 0

    least_turn = float(settings.get("min_arc_degrees", 55))
    reach = int(settings.get("centre_search_px", 6))
    slack = int(settings.get("radius_search_px", 2))
    angles = np.arange(0, 360, 1.0)
    around = (np.cos(np.radians(angles)), np.sin(np.radians(angles)))

    found = 0
    for candidate in candidates:
        if ARC_GEOMETRY in candidate["evidence"]:
            continue
        leaf = candidate["end_pt"] - candidate["start_pt"]
        if leaf <= 0:
            continue
        # A page whose mediabox does not start at the corner of the paper
        # renders from its own origin; dropping that offset would look for
        # every arc the same distance away from the door it belongs to.
        turn = max(
            _widest_arc_near(
                mask,
                (x - origin[0]) * scale,
                (y - origin[1]) * scale,
                leaf * scale,
                reach, around, np, slack,
            )
            for x, y in candidate["jambs"]
        )
        if turn < least_turn:
            continue
        candidate["evidence"].append(ARC_GEOMETRY)
        candidate["arc_degrees"] = int(turn)
        candidate["arc_radius_mm"] = candidate["width_mm"]
        candidate["arc_read_from"] = "the page as a picture"
        found += 1
    return found


def _widest_arc_near(mask, x: float, y: float, radius: float, reach: int, around, np, slack: int = 2) -> float:
    """The longest unbroken stretch of a circle about here that is drawn on.

    The centre is tried a few pixels either way, and the radius a little either
    way with it: the wall this gap sits in was traced from the same picture and
    is not exact to the pixel, while an arc only reads as one when the circle is
    where the ink is. Asked at the exact computed jamb alone, this finds
    nothing at all.
    """
    best = 0.0
    for dx in range(-reach, reach + 1, 2):
        for dy in range(-reach, reach + 1, 2):
            for dr in (-slack, 0, slack):
                best = max(best, _arc_drawn_on(mask, x + dx, y + dy, radius + dr, around, np))
    return best


# --- source 2: the mark printed beside it -----------------------------------


def _compiled_patterns(settings: dict) -> list:
    compiled = []
    for entry in settings.get("patterns", []) or []:
        try:
            compiled.append(
                (re.compile(entry["pattern"], re.IGNORECASE), entry["element_type"])
            )
        except Exception as e:
            logger.exception(f"an opening label pattern could not be read: {e}")
    return compiled


def type_from_label(mark: str, config: dict):
    """What a mark says this opening is, from its prefix, or None."""
    text = "".join(str(mark).split()).upper()
    if not text:
        return None
    for pattern, element_type in _compiled_patterns(
        settings_for(config).get("text_label", {})
    ):
        if pattern.match(text):
            return element_type
    return None


def label_evidence(
    candidates: list, opening_marks: list, calibration: dict, config: dict
) -> int:
    """Marks every candidate gap that has a door or window mark printed beside it.

    The mark is printed *beside* the opening, commonly inside the room on a
    leader, so the nearest candidate gap within reach is the one it labels -
    and each mark labels one gap, so the nearest pairing is taken first and
    each is used once. Two marks cannot mean the same hole.
    """
    settings = settings_for(config).get("text_label", {})
    if not settings.get("enabled", True) or not candidates or not opening_marks:
        return 0
    mm_per_point = _millimetres_per_point(calibration)
    if not mm_per_point:
        return 0
    furthest = float(settings.get("max_distance_mm", 2000)) / mm_per_point

    pairs = []
    for mark in opening_marks:
        element_type = type_from_label(mark.get("mark", ""), config)
        if not element_type:
            continue
        x, y = bbox_center(mark["bbox"])
        for index, candidate in enumerate(candidates):
            box = candidate["bbox"]
            near_x = min(max(x, box[0]), box[2])
            near_y = min(max(y, box[1]), box[3])
            away = ((x - near_x) ** 2 + (y - near_y) ** 2) ** 0.5
            if away <= furthest:
                pairs.append((away, mark["mark_id"], index, mark, element_type))

    pairs.sort(key=lambda pair: (pair[0], pair[1], pair[2]))
    used_marks, used_gaps, found = set(), set(), 0
    for away, mark_id, index, mark, element_type in pairs:
        if mark_id in used_marks or index in used_gaps:
            continue
        used_marks.add(mark_id)
        used_gaps.add(index)
        candidate = candidates[index]
        candidate["evidence"].append(TEXT_LABEL)
        candidate["mark"] = mark["mark"]
        candidate["mark_bbox"] = mark["bbox"]
        candidate["label_element_type"] = element_type
        candidate["label_distance_mm"] = round(away * mm_per_point, 1)
        found += 1
    return found


# --- source 3: the glazing drawn inside the wall ----------------------------


def _thin_segments(rulings: dict, axis_key: str, settings: dict) -> list:
    """The sheet's lines on one axis drawn thinly enough to be glazing.

    A wall's own face is plotted heavier than the glass inside it. ``0.0`` means
    the PDF states no width for that segment, which is common, and a segment
    saying that is judged on everything else rather than thrown away.
    """
    segments = rulings.get(axis_key) or []
    widths = rulings.get(f"{axis_key}_widths") or []
    thinnest = float(settings.get("max_line_width_pt", 0.8))
    kept = []
    for index, segment in enumerate(segments):
        width = widths[index] if index < len(widths) else 0.0
        if width and width > thinnest:
            continue
        kept.append(segment)
    return kept


def _distinct(positions: list, tolerance: float) -> int:
    distinct = []
    for position in sorted(positions):
        if not distinct or abs(position - distinct[-1]) > tolerance:
            distinct.append(position)
    return len(distinct)


def glazing_evidence(candidates: list, rulings: dict, config: dict) -> int:
    """Marks every candidate gap with the glass and its frame drawn inside it.

    **A wall is solid, so nothing is drawn inside one.** What is drawn between
    the two faces over the width of the gap is the window. Both directions are
    read, because offices draw it both ways: lines running *across* the wall's
    thickness inside the opening, and lines running *along* it between the
    faces, which is how most Australian sets draw a sliding or a fixed pane.
    """
    settings = settings_for(config).get("glazing_symbol", {})
    if not settings.get("enabled", True) or not candidates or not rulings:
        return 0

    fewest = int(settings.get("min_lines", 2))
    most = int(settings.get("max_lines", 6))
    tolerance = float(settings.get("line_tolerance_pt", 0.12))
    inset = float(settings.get("face_inset_pt", 0.15))
    least_share = float(settings.get("min_share_of_the_gap", 0.5))

    found = 0
    for candidate in candidates:
        wall = candidate["wall"]
        faces = wall.get("face_positions_pt") or []
        if len(faces) != 2:
            continue
        low_face, high_face = sorted(faces)
        gap_low, gap_high = candidate["start_pt"], candidate["end_pt"]
        gap_span = gap_high - gap_low
        if gap_span <= 0:
            continue
        along_key = "h" if wall["runs_along"] == "x" else "v"
        across_key = "v" if wall["runs_along"] == "x" else "h"

        lines = 0
        drawn = ""
        if settings.get("along_the_wall", True):
            # A thin line running along the wall, between its two faces, over
            # most of the gap: the glass and its frame.
            positions = [
                position
                for position, start, end in _thin_segments(rulings, along_key, settings)
                if low_face + inset < position < high_face - inset
                and (min(end, gap_high) - max(start, gap_low)) >= least_share * gap_span
            ]
            lines = _distinct(positions, tolerance)
            drawn = "along the wall, between its two faces"
        if lines < fewest and settings.get("across_the_wall", True):
            # A thin line crossing the wall's thickness, inside the gap.
            positions = [
                position
                for position, start, end in _thin_segments(rulings, across_key, settings)
                if gap_low <= position <= gap_high
                and start <= low_face + inset
                and end >= high_face - inset
            ]
            lines = _distinct(positions, tolerance)
            drawn = "across the wall's thickness, inside the opening"

        if not (fewest <= lines <= most):
            continue
        candidate["evidence"].append(GLAZING_SYMBOL)
        candidate["glazing_lines"] = lines
        candidate["glazing_drawn"] = drawn
        found += 1
    return found


# --- source 4: the schedule row that mark names -----------------------------


# --- source 5: the leaf width printed at the opening ------------------------


def leaf_dimension_evidence(
    candidates: list, dimensions: list, calibration: dict, config: dict
) -> int:
    """Marks every candidate gap that the drawing dimensions as a door leaf.

    **A plan commonly states a door's width at the door**, printed across the
    opening - ``870``, ``920`` - rather than only in a schedule. That figure is
    the drawing stating this opening's own size, in this opening's own place.

    Two rules keep an ordinary dimension from being read as a door, and they
    are drafting conventions rather than tuning:

    *   **A leaf dimension is printed across the wall; a wall dimension is
        printed clear of the building.** A setout string runs in the margin on
        its own dimension line, so the figure's box must overlap the band
        between the wall's own two faces and sit within reach of its line. This
        is what stops the 19,920 OVERALL string, the room setout figures and
        every eave and boundary dimension from confirming a hole.
    *   **It must be a size a leaf is made in.** A figure outside that range is
        measuring something else.

    **One figure confirms one gap.** A figure printed between two doorways
    belongs to one of them, so the pairings are taken nearest-first and each
    figure is used once - two doors cannot share one printed width.
    """
    settings = settings_for(config).get("leaf_dimension", {})
    if not settings.get("enabled", True) or not candidates or not dimensions:
        return 0
    mm_per_point = _millimetres_per_point(calibration)
    if not mm_per_point:
        return 0

    smallest = float(settings.get("leaf_dim_min_mm", 500))
    largest = float(settings.get("leaf_dim_max_mm", 2500))
    reach = float(settings.get("leaf_dim_wall_reach_mm", 200)) / mm_per_point
    slack = float(settings.get("span_slack_mm", 200)) / mm_per_point
    between_the_faces = bool(settings.get("must_sit_between_the_wall_faces", True))

    pairs = []
    for index, dimension in enumerate(dimensions):
        value = dimension.get("value_mm")
        box = dimension.get("bbox") or []
        if value is None or len(box) != 4:
            continue
        # An overall or a total measures the building, never one leaf.
        if dimension.get("is_overall"):
            continue
        if not (smallest <= float(value) <= largest):
            continue
        centre = ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)

        for position, candidate in enumerate(candidates):
            wall = candidate["wall"]
            faces = wall.get("face_positions_pt") or []
            if len(faces) != 2:
                continue
            along = 0 if wall["runs_along"] == "x" else 1
            across = 1 - along
            low_face, high_face = sorted(faces)

            # The figure is printed across the opening it measures, so it lies
            # within the gap's own run along the wall.
            if not (candidate["start_pt"] - slack <= centre[along]
                    <= candidate["end_pt"] + slack):
                continue
            # ... and on the wall's line rather than out on a dimension string.
            line = wall["start_point_pt"][across]
            off_the_wall = abs(centre[across] - line)
            if off_the_wall > reach:
                continue
            # **Printed across the wall, not beside it.** A wall dimension sits
            # clear of the building; a leaf dimension straddles the wall it
            # measures, so its box has to reach the band between the faces.
            if between_the_faces:
                box_low, box_high = box[across], box[across + 2]
                if box_high < low_face or box_low > high_face:
                    continue
            pairs.append((round(off_the_wall, 4), index, position, float(value)))

    # Nearest the wall line first, so a figure printed on the wall is preferred
    # to one merely within reach of it; the figure's own order breaks ties, so
    # two runs of the same plan pair them the same way.
    pairs.sort()
    used_figures, confirmed, found = set(), set(), 0
    for off_the_wall, index, position, value in pairs:
        if index in used_figures or position in confirmed:
            continue
        used_figures.add(index)
        confirmed.add(position)
        candidate = candidates[position]
        candidate["evidence"].append(LEAF_DIMENSION)
        candidate["leaf_dimension_mm"] = value
        candidate["leaf_dimension_off_the_wall_mm"] = round(off_the_wall * mm_per_point, 1)
        candidate["leaf_dimension_text"] = dimensions[index].get("text")
        found += 1
    return found


# The schedule reading itself lives in ``openings.reconcile_openings_with_schedules``,
# which already walks every schedule table in the whole document and matches
# each mark against it. It attaches ``SCHEDULE_ENTRY`` to the openings it
# matches. There is one implementation of it rather than two that can drift
# apart, and it runs document-wide because a door schedule is printed on its
# own sheet.
#
# It can only ever reach an opening that already carries a mark, which is why a
# gap with none of the first three readings can be settled sheet by sheet: no
# schedule can rescue a gap nothing else spoke for.


# --- the candidate becomes an opening, or it does not -----------------------


def _element_type(candidate: dict):
    """What the drawing said this is, from the strongest source that named it."""
    named = []
    if ARC_GEOMETRY in candidate["evidence"]:
        named.append((SOURCE_RANK[ARC_GEOMETRY], "door", ARC_GEOMETRY))
    if GLAZING_SYMBOL in candidate["evidence"]:
        named.append((SOURCE_RANK[GLAZING_SYMBOL], "window", GLAZING_SYMBOL))
    if TEXT_LABEL in candidate["evidence"] and candidate.get("label_element_type"):
        named.append(
            (SOURCE_RANK[TEXT_LABEL], candidate["label_element_type"], TEXT_LABEL)
        )
    if LEAF_DIMENSION in candidate["evidence"]:
        # A leaf width printed at an opening is a door convention on these
        # plans, so it names a door - but it is a hint, and the weakest of the
        # readings, so a symbol drawn to size always outranks it.
        named.append(
            (SOURCE_RANK[LEAF_DIMENSION],
             candidate.get("leaf_type_hint") or "door",
             LEAF_DIMENSION)
        )
    if not named:
        return None, "not_stated"
    best = max(named, key=lambda entry: entry[0])
    return best[1], best[2]


def _why(candidate: dict) -> str:
    """One sentence, for the person reading the plan, saying what was found."""
    parts = []
    if ARC_GEOMETRY in candidate["evidence"]:
        parts.append(
            "a door's swing is drawn about one of its jambs, "
            f"{candidate.get('arc_radius_mm', candidate['width_mm']):.0f} mm across "
            f"(read from {candidate.get('arc_read_from', 'the drawing')})"
        )
    if TEXT_LABEL in candidate["evidence"]:
        parts.append(f"the mark {candidate.get('mark')} is printed beside it")
    if GLAZING_SYMBOL in candidate["evidence"]:
        parts.append(
            f"{candidate.get('glazing_lines')} thin lines are drawn "
            f"{candidate.get('glazing_drawn')}, which is how glazing is drawn"
        )
    if LEAF_DIMENSION in candidate["evidence"]:
        parts.append(
            f"the drawing prints {candidate.get('leaf_dimension_mm'):.0f} mm across "
            "it, which is the width of a leaf"
        )
    if not parts:
        return "Nothing on the sheet says what goes in this gap."
    return "The wall stops here, and " + "; ".join(parts) + "."


def opening_from(candidate: dict) -> dict:
    """The opening record for a gap the drawing confirmed."""
    wall = candidate["wall"]
    element_type, type_source = _element_type(candidate)
    length_mm = float(wall.get("length_mm") or 0.0)
    centre = (candidate["start_fraction"] + candidate["end_fraction"]) / 2.0
    return {
        "opening_id": "",
        "mark": candidate.get("mark", ""),
        "element_type": element_type,
        "element_type_source": type_source,
        "described_as": None,
        "wall_id": candidate["wall_id"],
        "wall_note": _why(candidate),
        "position_on_wall": {
            "start_fraction": round(candidate["start_fraction"], 5),
            "end_fraction": round(candidate["end_fraction"], 5),
            "centre_fraction": round(centre, 5),
            "from_wall_start_mm": round(centre * length_mm, 1),
            "width_mm": round(
                (candidate["end_fraction"] - candidate["start_fraction"]) * length_mm, 1
            ),
            "measured_from": "break_in_the_wall",
        },
        # Whole millimetres. A tenth of a millimetre measured off a drawing is
        # precision the drawing does not have.
        "width_mm": float(round(candidate["width_mm"])),
        "height_mm": None,
        "sill_height_mm": None,
        "head_height_mm": None,
        "location_on_plan": None,
        "schedule_sheet": None,
        "schedule_row_id": None,
        "in_schedule": False,
        "found_by": max(
            candidate["evidence"], key=lambda source: SOURCE_RANK.get(source, 0)
        ),
        "source_sheet": candidate["sheet_id"],
        "source_bbox": candidate["bbox"],
        "confidence": 0.0,
        "confidence_band": "review",
        "review_status": "needs_review",
        "review_needed": True,
        "evidence": list(candidate["evidence"]),
    }


def unresolved_gap_from(candidate: dict) -> dict:
    """A break in a wall that no reading of the drawing confirmed.

    **Not an opening, and not thrown away either.** It is a real break in a
    real wall and a reviewer may want to know it is there, so it carries its
    wall, its width and where on the sheet to look, and it goes to the issues
    log rather than onto the screen as a door nobody can check.
    """
    return {
        "gap_id": "",
        "wall_id": candidate["wall_id"],
        "width_mm": float(round(candidate["width_mm"])),
        "source_sheet": candidate["sheet_id"],
        "source_bbox": candidate["bbox"],
        "position_on_wall": {
            "start_fraction": round(candidate["start_fraction"], 5),
            "end_fraction": round(candidate["end_fraction"], 5),
        },
        "reason": (
            f"The wall {candidate['wall_id']} stops for {candidate['width_mm']:.0f} mm "
            "here and starts again, but nothing else on the sheet says an opening is "
            "there: no door swing about either jamb, no mark printed beside it and no "
            "glazing drawn inside the wall. It is reported as a gap to check rather "
            "than as a door or a window."
        ),
    }


def settle_evidence(opening: dict, config: dict) -> None:
    """How far to trust an opening, and whether a person needs to look at it.

    Counted once per source, however many ways that source was reached. **None
    of the four and it is not an opening at all** - such a candidate never
    becomes one of these records.
    """
    confidence = settings_for(config).get("confidence", {})
    sources = {source for source in opening.get("evidence", []) if source in SOURCES}
    count = len(sources)
    high_from = float(confidence.get("high_band_from", 0.75))

    if count >= 2:
        opening["confidence"] = float(confidence.get("two_or_more_sources", 0.9))
        opening["review_needed"] = False
        opening["how_it_was_decided"] = (
            f"{count} separate readings of the drawing agree that there is an opening "
            "here — "
            + ", ".join(IN_WORDS.get(source, source) for source in sorted(sources))
            + ". It is taken as confirmed."
        )
    else:
        opening["confidence"] = float(confidence.get("one_source", 0.7))
        opening["review_needed"] = True
        only = next(iter(sources), None)
        opening["how_it_was_decided"] = (
            f"Read from {IN_WORDS.get(only, only)}, and nothing else on the sheet says "
            "the same. It is reported as an opening for a reviewer to check."
        )
    # **A printed leaf width raises the confidence but never settles the
    # review.** It is the drawing stating this opening's own size at this
    # opening's own place, which is worth something on its own; it is still one
    # reading, so on its own it leaves `review_needed` true exactly as any
    # single reading does.
    if LEAF_DIMENSION in sources:
        bonus = float(
            settings_for(config).get("leaf_dimension", {}).get("confidence_bonus", 0.3)
        )
        opening["confidence"] = round(min(opening["confidence"] + bonus, 0.95), 3)
    opening["confidence_band"] = (
        "high" if opening["confidence"] >= high_from else "review"
    )
    opening["review_status"] = (
        "needs_review" if opening["review_needed"] else "auto_confirmed"
    )
    opening["evidence_count"] = count
    if not opening.get("element_type"):
        opening["element_type"] = "unknown_opening"
        opening["element_type_source"] = "not_stated"


def read_openings_from_the_drawing(
    walls: list,
    rulings: dict,
    page,
    opening_marks: list,
    calibration: dict,
    config: dict,
    sheet_id: str,
    dimensions: list = None,
):
    """Every break in this sheet's walls, judged against the drawing.

    Returns ``(openings, unresolved_gaps)``. The schedule reading is added
    afterwards, once the whole document has been read, because a schedule is
    printed on its own sheet - and it can only ever reach a gap that already
    carries a mark, so it can never turn a gap with nothing said about it into
    an opening.
    """
    candidates = gap_candidates(walls, calibration, config, sheet_id)
    if not candidates:
        return [], []

    try:
        arcs = arc_evidence(candidates, page, calibration, config)
    except Exception as e:
        logger.exception(f"{sheet_id}: reading door arcs failed: {e}")
        arcs = 0
    try:
        labels = label_evidence(candidates, opening_marks or [], calibration, config)
    except Exception as e:
        logger.exception(f"{sheet_id}: reading opening marks failed: {e}")
        labels = 0
    try:
        glazing = glazing_evidence(candidates, rulings or {}, config)
    except Exception as e:
        logger.exception(f"{sheet_id}: reading glazing symbols failed: {e}")
        glazing = 0
    try:
        leaves = leaf_dimension_evidence(
            candidates, dimensions or [], calibration, config
        )
    except Exception as e:
        logger.exception(f"{sheet_id}: reading printed leaf widths failed: {e}")
        leaves = 0

    openings, unresolved = [], []
    for candidate in candidates:
        if candidate["evidence"]:
            openings.append(opening_from(candidate))
        else:
            unresolved.append(unresolved_gap_from(candidate))

    openings.sort(key=lambda o: (o["source_bbox"][1], o["source_bbox"][0]))
    unresolved.sort(key=lambda g: (g["source_bbox"][1], g["source_bbox"][0]))
    walls_by_id = {wall["wall_id"]: wall for wall in walls}
    for position, opening in enumerate(openings, start=1):
        opening["opening_id"] = f"{sheet_id}-OPG{position:03d}"
        wall = walls_by_id.get(opening["wall_id"])
        if wall is not None:
            wall["linked_opening_marks"] = list(
                wall.get("linked_opening_marks") or []
            ) + [opening["opening_id"]]
    for position, gap in enumerate(unresolved, start=1):
        gap["gap_id"] = f"{sheet_id}-GAP{position:03d}"

    logger.info(
        f"{sheet_id}: {len(candidates)} breaks in walls — {len(openings)} confirmed as "
        f"openings ({arcs} by a door's arc, {labels} by a printed mark, {glazing} by "
        f"glazing drawn inside the wall, {leaves} by a leaf width printed across "
        f"them), {len(unresolved)} left as gaps to check"
    )
    return openings, unresolved
