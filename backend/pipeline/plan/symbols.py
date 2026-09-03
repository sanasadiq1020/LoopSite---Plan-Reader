"""Door and window symbols read from the drawing's own geometry.

A plan does not only *break* its walls where an opening goes — it draws the
opening. Every office draws them the same few ways, because the ways are
conventions rather than opinions:

*   **A window is drawn inside the wall's own thickness.** The wall's two faces
    carry on past it, and between them a run of thin lines is drawn: the glass,
    the frame, the sashes. How many lines says which kind of window it is —
    three or more with a meeting stile between them is a sliding window, two is
    a fixed pane, one is a highlight or an obscure panel.
*   **A hinged door is drawn as its swing**, a quarter circle whose radius is
    the leaf width. That is why a plan set is full of square-ish curved paths
    exactly 820 mm and 1,100 mm across: those *are* the doors, stated in the
    geometry more precisely than any label states them.
*   **A sliding door or window may carry small arrows** showing which way the
    sash runs.

None of this needs a mark to be printed, and none of it needs a schedule. It is
what the drawing says about itself, and it is available on every sheet that
draws the building in plan — which is what makes an automatic reading possible
on a plan set that labels nothing.

Everything here is measured in millimetres through the sheet's own calibrated
scale, and every threshold lives in ``config/wall_config.json``. Nothing in this
module holds a coordinate, a size or a name taken from any particular drawing
(Critical Rule 1).
"""

from app.logging_setup import get_logger
from pipeline.plan.layout import page_drawings

logger = get_logger()


# --- what is drawn inside a wall -------------------------------------------


def window_symbols_in(wall: dict, rulings: dict, mm_per_point: float, settings: dict) -> list:
    """The stretches of this wall that carry a window symbol inside them.

    **The test is what is drawn between the wall's own two faces.** A wall is
    solid: nothing is drawn inside it. A window is not solid, and the drawing
    says so by putting the glass, the frame and the sashes in there — a run of
    thin lines parallel to the wall, in the gap between its faces, over exactly
    the width of the opening.

    Counting those lines is what names the window, and the count is a drafting
    convention rather than a threshold to tune:

    | lines drawn inside the wall | what it is |
    |---|---|
    | three or more | a sliding window or door — the sashes overlap |
    | two | a fixed pane: the glass and its frame |
    | one, with cross marks | a highlight or an obscure panel |

    Two things stop a hatched wall being read as one long window: the run has
    to be a plausible opening width, and it may not cover the whole wall. A
    wall hatched from end to end is a wall drawn in section, not a window.
    """
    axis_key = "h" if wall["runs_along"] == "x" else "v"
    segments = rulings.get(axis_key) or []
    if not segments:
        return []

    low_face, high_face = sorted(wall["face_positions_pt"])
    inset = float(settings.get("symbol_face_inset_pt", 0.15))
    run_low, run_high = _run_of(wall)

    # Only what is drawn strictly between the two faces, and only where it runs
    # along this wall rather than merely passing by.
    inside = []
    for position, start, end in segments:
        if not (low_face + inset < position < high_face - inset):
            continue
        covered = min(end, run_high) - max(start, run_low)
        if covered <= 0:
            continue
        inside.append((max(start, run_low), min(end, run_high), position))
    if not inside:
        return []

    smallest = float(settings.get("min_opening_width_mm", 300))
    largest = float(settings.get("max_opening_width_mm", 6000))
    most_of_the_wall = float(settings.get("max_window_share_of_a_wall", 0.95))
    wall_length_pt = run_high - run_low

    found = []
    for section_start, section_end, members in _sections(inside, settings, mm_per_point):
        width_mm = (section_end - section_start) * mm_per_point
        if not (smallest <= width_mm <= largest):
            continue
        if wall_length_pt > 0 and (section_end - section_start) / wall_length_pt > most_of_the_wall:
            continue

        lines_across = _distinct_positions(members, settings)
        kind, confidence = _window_kind(len(lines_across), settings)
        if not kind:
            continue
        found.append(
            {
                "start_pt": round(section_start, 2),
                "end_pt": round(section_end, 2),
                "width_mm": float(round(width_mm)),
                "lines_inside_the_wall": len(lines_across),
                "kind": kind,
                "confidence": confidence,
            }
        )
    return found


def _sections(inside: list, settings: dict, mm_per_point: float) -> list:
    """Groups the lines drawn inside a wall into the openings they belong to.

    Two lines belong to the same opening when they overlap along the wall. A
    window and the window next to it are separate runs with solid wall between
    them, which is what keeps them apart.
    """
    inside.sort()
    sections = []
    current_start, current_end, members = inside[0][0], inside[0][1], [inside[0]]
    for start, end, position in inside[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
            members.append((start, end, position))
        else:
            sections.append((current_start, current_end, members))
            current_start, current_end, members = start, end, [(start, end, position)]
    sections.append((current_start, current_end, members))
    return sections


def _distinct_positions(members: list, settings: dict) -> list:
    """How many separate lines are drawn across the wall's thickness here.

    Separate, because the same line drawn twice is one line. The tolerance is
    in points for the same reason the collinear one is: it is a tolerance on
    the paper, not a size in the building.
    """
    tolerance = float(settings.get("symbol_line_tolerance_pt", 0.12))
    positions = sorted({round(position, 4) for _s, _e, position in members})
    distinct = []
    for position in positions:
        if not distinct or abs(position - distinct[-1]) > tolerance:
            distinct.append(position)
    return distinct


def _window_kind(line_count: int, settings: dict):
    """What a run of this many lines inside a wall is, and how sure that is."""
    sliding = int(settings.get("sliding_window_min_lines", 3))
    if line_count >= sliding:
        return "sliding_window", 0.85
    if line_count == 2:
        return "fixed_window", 0.75
    if line_count == 1 and settings.get("single_line_is_a_window", True):
        return "highlight_window", 0.6
    return None, 0.0


def _run_of(wall: dict):
    start, end = wall["start_point_pt"], wall["end_point_pt"]
    if wall["runs_along"] == "x":
        return min(start[0], end[0]), max(start[0], end[0])
    return min(start[1], end[1]), max(start[1], end[1])


# --- the swing of a hinged door --------------------------------------------


def door_swings(page, mm_per_point: float, settings: dict) -> list:
    """Every quarter circle on the sheet that is the size of a door leaf.

    **A door swing states the door's width more exactly than its label does.**
    The arc's radius *is* the leaf, so a plan set drawn this way carries its
    door widths in its geometry whether or not it prints a schedule — and the
    radii come out at the sizes doors are actually made in.

    A swing is recognised by shape rather than by any particular size: a path
    containing a curve, whose box is about as wide as it is tall, and whose
    size is a plausible door. A curved path that is much wider than it is tall
    is a leader, a cloud or a piece of lettering.
    """
    if page is None or not settings.get("read_door_swings", True):
        return []
    try:
        drawings = page_drawings(page)
    except Exception as e:
        logger.exception(f"could not read the sheet's drawn paths for door swings: {e}")
        return []

    smallest = float(settings.get("min_door_width_mm", 600))
    largest = float(settings.get("max_door_width_mm", 2400))
    squareness = float(settings.get("swing_squareness", 1.6))

    found = []
    for path in drawings:
        try:
            if not any(item[0] == "c" for item in path.get("items", [])):
                continue
            rect = path.get("rect")
            if rect is None:
                continue
            width, height = rect.x1 - rect.x0, rect.y1 - rect.y0
            if width <= 0 or height <= 0:
                continue
            ratio = width / height
            if not (1.0 / squareness <= ratio <= squareness):
                continue
            radius_mm = max(width, height) * mm_per_point
            if not (smallest <= radius_mm <= largest):
                continue
            found.append(
                {
                    "bbox": [round(rect.x0, 2), round(rect.y0, 2), round(rect.x1, 2), round(rect.y1, 2)],
                    "centre": [round((rect.x0 + rect.x1) / 2, 2), round((rect.y0 + rect.y1) / 2, 2)],
                    "width_mm": float(round(radius_mm)),
                }
            )
        except Exception:
            # One malformed path must never take the sheet down (Critical Rule 6).
            continue

    logger.info(f"{len(found)} door swings read from the drawing's own arcs")
    return found


def swing_against_wall(swing: dict, wall: dict, mm_per_point: float, settings: dict):
    """Where along this wall the door belonging to this swing sits, or None.

    **A swing is drawn beside its door, not on it.** The arc springs from the
    hinge, which is at one jamb, and sweeps into the room. So the arc's box
    touches the wall along one of its sides, and the stretch of wall it touches
    is the opening.
    """
    reach = float(settings.get("swing_reach_mm", 400)) / mm_per_point
    box = swing["bbox"]
    low_face, high_face = sorted(wall["face_positions_pt"])
    run_low, run_high = _run_of(wall)

    if wall["runs_along"] == "x":
        across_low, across_high = box[1], box[3]
        along_low, along_high = box[0], box[2]
    else:
        across_low, across_high = box[0], box[2]
        along_low, along_high = box[1], box[3]

    # The swing has to sit against the wall across its thickness ...
    if across_high < low_face - reach or across_low > high_face + reach:
        return None
    # ... and the stretch it covers has to be on the wall.
    start, end = max(along_low, run_low), min(along_high, run_high)
    if end - start <= 0:
        return None
    return round(start, 2), round(end, 2)


# --- small marks: arrows and crosses ---------------------------------------


def small_marks(page, mm_per_point: float, settings: dict) -> list:
    """Short strokes: the arrows on a sliding sash, the cross on a fixed panel.

    Read as supporting evidence rather than on their own. An arrow is a short
    stroke and so is a hundred other things on a plan; what makes it an arrow
    is that it is inside an opening, which is decided by whatever it is found
    inside.
    """
    if page is None or not settings.get("read_small_marks", True):
        return []
    try:
        drawings = page_drawings(page)
    except Exception as e:
        logger.exception(f"could not read the sheet's drawn paths for small marks: {e}")
        return []

    shortest = float(settings.get("mark_min_length_mm", 60)) / mm_per_point
    longest = float(settings.get("mark_max_length_mm", 700)) / mm_per_point

    marks = []
    for path in drawings:
        for item in path.get("items", []):
            try:
                if item[0] != "l":
                    continue
                a, b = item[1], item[2]
                length = ((b.x - a.x) ** 2 + (b.y - a.y) ** 2) ** 0.5
                if not (shortest <= length <= longest):
                    continue
                marks.append(((a.x + b.x) / 2.0, (a.y + b.y) / 2.0))
            except Exception:
                continue
    return marks


def marks_inside(marks: list, box) -> int:
    """How many small strokes fall inside this box on the sheet."""
    x0, y0, x1, y1 = box
    return sum(1 for x, y in marks if x0 <= x <= x1 and y0 <= y <= y1)



# --- a door swing on a sheet that is stored as a picture -------------------


def curve_paths_on(page) -> int:
    """How many of the sheet's drawn paths contain a curve."""
    try:
        return sum(
            1
            for path in page_drawings(page)
            if any(item[0] == "c" for item in path.get("items", []))
        )
    except Exception:
        return 0


def swings_at_the_openings(
    page, openings: list, walls_by_id: dict, mm_per_point: float, settings: dict
) -> int:
    """Finds the doors on a sheet whose drawing is stored as a picture.

    **A plan set can be published as pictures**, and one of the sets in use is:
    its whole plan is embedded images, so ``page.get_drawings()`` holds almost
    nothing and the quarter circle every hinged door is drawn with is pixels
    rather than a curve. The page is rendered and the arcs are looked for
    there instead.

    **They are looked for at the openings, not across the page**, and that is
    the whole of why this works. Two page-wide searches were tried first and
    both failed, for reasons worth keeping:

    *   *Tracing the outlines and fitting circles to them.* A swing is not a
        shape on its own — it springs from the jamb and closes on the door
        leaf, so it is joined to the wall. The tracing returns one outline of
        17,882 points covering half the drawing, and there is no arc in it to
        fit.
    *   *Hough circles.* On a floor plan it proposed 4,767 of them and the real
        swings were not among the survivors. Hough asks "is there a circle
        here", which every fixture, symbol and letter O answers, while a door
        swing is precisely the case where three quarters of the circle is
        missing.

    What is asked here instead is a question with an answer: *this opening is
    820 mm wide and this is its jamb — is there an arc of radius 820 mm about
    that point?* The place and the radius both come from the reading, so the
    image only has to confirm or deny, and it separates cleanly: on the two
    sheets tried, every real door answers between 92 and 118 degrees and
    everything else stops at 52.

    The jamb is searched for a few pixels around, because the traced wall and
    the drawn hinge are not the same point to the pixel, and at this radius
    being three pixels out breaks the arc into fragments — which is what made
    the first attempt at this find nothing.

    Returns how many openings were confirmed as doors.
    """
    if page is None or not settings.get("read_door_swings_from_the_page", True):
        return 0
    try:
        import numpy as np
    except ImportError:
        return 0
    try:
        from pipeline.plan.rasterlines import _binary_image

        mask, scale = _binary_image(page, int(settings.get("swing_render_dpi", 200)))
    except Exception as e:
        logger.exception(f"could not render the page to look for door swings: {e}")
        return 0

    smallest = float(settings.get("min_door_width_mm", 600)) / mm_per_point
    largest = float(settings.get("max_door_width_mm", 2400)) / mm_per_point
    least_turn = float(settings.get("swing_min_degrees", 55))
    reach = int(settings.get("swing_centre_search_px", 6))

    angles = np.arange(0, 360, 1.0)
    around = (np.cos(np.radians(angles)), np.sin(np.radians(angles)))

    found = 0
    for opening in openings:
        place = opening.get("position_on_wall")
        wall = walls_by_id.get(opening.get("wall_id") or "")
        if not place or not wall:
            continue
        start, end = wall["start_point_pt"], wall["end_point_pt"]
        jambs = [
            (
                start[0] + fraction * (end[0] - start[0]),
                start[1] + fraction * (end[1] - start[1]),
            )
            for fraction in (place["start_fraction"], place["end_fraction"])
        ]
        leaf = (
            (jambs[1][0] - jambs[0][0]) ** 2 + (jambs[1][1] - jambs[0][1]) ** 2
        ) ** 0.5
        if not (smallest <= leaf <= largest):
            continue

        turn = max(
            _widest_arc_near(mask, x * scale, y * scale, leaf * scale, reach, around, np)
            for x, y in jambs
        )
        if turn < least_turn:
            continue

        opening["element_type"] = "door"
        opening["element_type_source"] = "door_swing"
        opening["swing_degrees"] = int(turn)
        opening["wall_note"] = (
            f"The door's swing is drawn here on the page: an arc of {int(turn)} degrees "
            f"about this opening's jamb, the width of the opening."
        )
        evidence = opening.setdefault("evidence", [])
        if "door_swing" not in evidence:
            evidence.append("door_swing")
        found += 1

    if found:
        logger.info(f"{found} openings confirmed as doors by their swing on the page")
    return found


def _widest_arc_near(mask, x: float, y: float, radius: float, reach: int, around, np) -> float:
    """The longest unbroken stretch of a circle about here that is drawn on.

    The centre is tried a few pixels either way, and the radius a little either
    way with it: the wall this opening sits on was traced from the same picture
    and is not exact to the pixel, while an arc only reads as one when the
    circle is where the ink is.
    """
    best = 0.0
    for dx in range(-reach, reach + 1, 2):
        for dy in range(-reach, reach + 1, 2):
            for dr in (-2, 0, 2):
                best = max(best, _arc_drawn_on(mask, x + dx, y + dy, radius + dr, around, np))
    return best


def _arc_drawn_on(mask, x: float, y: float, radius: float, around, np) -> float:
    """How far round this circle the drawing is inked, in degrees, unbroken."""
    height, width = mask.shape
    cos, sin = around
    xs = np.round(x + radius * cos).astype(int)
    ys = np.round(y + radius * sin).astype(int)
    inside = (xs >= 1) & (xs < width - 1) & (ys >= 1) & (ys < height - 1)
    where = np.where(inside)[0]
    if len(where) == 0:
        return 0.0
    column, row = xs[where], ys[where]
    # A pixel either way, so a line drawn a hair off the exact circle still
    # counts as drawn — the alternative is measuring the rendering, not the arc.
    inked = (
        (mask[row, column] > 0)
        | (mask[row - 1, column] > 0)
        | (mask[row + 1, column] > 0)
        | (mask[row, column - 1] > 0)
        | (mask[row, column + 1] > 0)
    )
    on = np.zeros(len(cos), dtype=bool)
    on[where] = inked
    return float(_longest_unbroken(np.concatenate([on, on])))


def _longest_unbroken(doubled) -> int:
    """The longest run of True, allowing for the circle joining up at zero."""
    best = run = 0
    for lit in doubled:
        run = run + 1 if lit else 0
        if run > best:
            best = run
    return min(best, 360)
