"""Making the walls of a building actually meet each other.

A building's walls hold each other up, and every later stage leans on that: the
junction graph, outside versus inside, the detached-structure test, and the
closed-circuit invariant that decides whether a candidate encloses anything at
all. All of them ask "does this wall meet that one?", and all of them get the
wrong answer when a traced centreline stops three points short of the wall it
runs into.

**It stops short for reasons that have nothing to do with the building.** A
centreline is the middle of a closed band, and the band ends where the ink ends
- so a wall arriving at another wall stops at the *face* of it, half a
thickness away from its centreline, and a wall traced from a picture stops
wherever the ink thinned. Measured on one real floor plan, 17 of the 44
standing walls had a single junction and one had none: the graph was far too
sparse for any circuit to close, so the enclosure test set aside 44 of 61
candidates - the whole building.

So the endpoints are snapped first. Each wall end is offered to the walls near
it, and where one passes within the reach, the end is moved onto it. Nothing is
invented: the wall is extended or trimmed **along its own axis only**, by at
most the reach, onto a wall that was already there. A wall's position across
its own thickness is never touched, because that is a measurement.

**The reach is stated in millimetres of building**, not points of paper, so it
means the same on a 1:50 detail and a 1:200 site plan. 150 mm is chosen for a
physical reason rather than a fitted one: half of a 300 mm cavity wall is
150 mm, so it is exactly far enough to carry a centreline from the face it was
traced to onto the centreline of the wall it meets, and no further.
"""

from app.logging_setup import get_logger
from pipeline.plan.cvdetect.settings import number, setting

logger = get_logger()


def snap_endpoints(walls: list, mm_per_point: float, config: dict) -> int:
    """Moves wall ends onto the walls they were traced up to. Returns how many.

    Runs before the junctions are read, because a junction is what this makes
    possible. Never raises: a wall that cannot be snapped is left exactly where
    it was traced (Critical Rule 6).
    """
    if not setting(config, "wall.snap_endpoints", True):
        return 0
    if len(walls) < 2 or not mm_per_point:
        return 0

    reach = number(config, "wall.snap_reach_mm", 150.0) / mm_per_point
    snapped = 0
    try:
        for wall in walls:
            snapped += _snap_one(wall, walls, reach)
    except Exception as e:
        logger.exception(f"the wall ends could not be snapped: {e}")
        return snapped

    if snapped:
        logger.info(
            f"walls: {snapped} end(s) snapped onto the wall they were traced up to"
        )
    return snapped


def cast_rays_from_free_ends(walls: list, mm_per_point: float, config: dict) -> int:
    """Extends a wall end that points at another wall's body, up to the reach.

    Snapping moves an end onto a wall it is already beside. This is the other
    case: an end that stops short **along its own line** of a wall it is
    plainly running into, which is what a partition traced a little shy of the
    wall it lands on looks like. The ray is cast along the wall's own axis - a
    wall is never moved sideways, because its position across its thickness is
    a measurement - and it stops at the first wall body it meets.

    **Measured, and the measurement is the point.** On one real floor plan the
    free ends sit a median of 877 mm from the nearest other wall, and only 2 of
    29 are within 250 mm. So this closes a real but small share of the gap: the
    rest are not imprecision, they are wall the tracing never found, and no ray
    can extend a centreline to a wall that was never traced.
    """
    reach = number(config, "wall.ray_reach_mm", 250.0) / mm_per_point
    if reach <= 0 or len(walls) < 2:
        return 0

    extended = 0
    try:
        for wall in walls:
            along = 0 if wall["runs_along"] == "x" else 1
            across = 1 - along
            position = wall["start_point_pt"][across]
            low = min(wall["start_point_pt"][along], wall["end_point_pt"][along])
            high = max(wall["start_point_pt"][along], wall["end_point_pt"][along])

            for which in ("low", "high"):
                where = low if which == "low" else high
                if _has_a_junction_at(wall, along, where):
                    continue
                hit = _ray_hits(wall, walls, along, across, position, where,
                                reach, outward=(which == "high"))
                if hit is None:
                    continue
                if which == "low" and hit < low:
                    low = hit
                elif which == "high" and hit > high:
                    high = hit
                else:
                    continue
                extended += 1

            if along == 0:
                wall["start_point_pt"] = [round(low, 2), position]
                wall["end_point_pt"] = [round(high, 2), position]
            else:
                wall["start_point_pt"] = [position, round(low, 2)]
                wall["end_point_pt"] = [position, round(high, 2)]
            face_low, face_high = sorted(wall["face_positions_pt"])
            if along == 0:
                wall["bbox"] = [round(low, 2), round(face_low, 2),
                                round(high, 2), round(face_high, 2)]
            else:
                wall["bbox"] = [round(face_low, 2), round(low, 2),
                                round(face_high, 2), round(high, 2)]
    except Exception as e:
        logger.exception(f"the free wall ends could not be extended: {e}")

    if extended:
        logger.info(f"walls: {extended} free end(s) extended onto the wall they point at")
    return extended


def _has_a_junction_at(wall: dict, along: int, where: float, slack: float = 10.0) -> bool:
    for junction in wall.get("junctions") or []:
        point = junction.get("at_pt")
        if point and len(point) > along and abs(point[along] - where) <= slack:
            return True
    return False


def _ray_hits(wall, walls, along, across, position, where, reach, outward):
    """The nearest wall body the ray meets, or None."""
    best = None
    for other in walls:
        if other is wall:
            continue
        other_along = 0 if other["runs_along"] == "x" else 1
        if other_along == along:
            continue
        other_across = 1 - other_along
        body = other["start_point_pt"][other_across]
        span_low = min(other["start_point_pt"][other_along],
                       other["end_point_pt"][other_along])
        span_high = max(other["start_point_pt"][other_along],
                        other["end_point_pt"][other_along])
        # The other wall has to actually cross this one's line.
        if not (span_low <= position <= span_high):
            continue
        step = body - where
        if outward and step <= 0:
            continue
        if not outward and step >= 0:
            continue
        if abs(step) > reach:
            continue
        if best is None or abs(step) < abs(best - where):
            best = body
    return best


def _snap_one(wall: dict, walls: list, reach: float) -> int:
    """Both ends of one wall, offered to everything near them."""
    along = 0 if wall["runs_along"] == "x" else 1
    across = 1 - along
    position = wall["start_point_pt"][across]
    start = min(wall["start_point_pt"][along], wall["end_point_pt"][along])
    end = max(wall["start_point_pt"][along], wall["end_point_pt"][along])

    moved = 0
    for which, where in (("start", start), ("end", end)):
        target = _nearest_wall_at(wall, walls, along, across, position, where, reach)
        if target is None:
            continue
        if which == "start":
            if target >= end:
                continue
            start = target
        else:
            if target <= start:
                continue
            end = target
        moved += 1

    if not moved:
        return 0
    if along == 0:
        wall["start_point_pt"] = [round(start, 2), position]
        wall["end_point_pt"] = [round(end, 2), position]
    else:
        wall["start_point_pt"] = [position, round(start, 2)]
        wall["end_point_pt"] = [position, round(end, 2)]

    low, high = sorted(wall["face_positions_pt"])
    if along == 0:
        wall["bbox"] = [round(start, 2), round(low, 2), round(end, 2), round(high, 2)]
    else:
        wall["bbox"] = [round(low, 2), round(start, 2), round(high, 2), round(end, 2)]
    return moved


def _nearest_wall_at(wall, walls, along, across, position, where, reach):
    """Where this end should move to, or None to leave it be.

    Two ways a wall end meets another wall, and both are ordinary drafting:

    *   **It runs into one crossing it** - a partition landing on an external
        wall, or a corner. The other wall lies across this one's axis, and the
        end is moved onto its centreline.
    *   **It butts up against one carrying on** - the same wall traced in two
        pieces, or a corner drawn open. The other lies on the same line, and
        the end is moved to close the gap.

    The nearest is taken, and only within the reach.
    """
    best, target = reach, None
    for other in walls:
        if other is wall:
            continue
        other_along = 0 if other["runs_along"] == "x" else 1
        other_across = 1 - other_along
        other_position = other["start_point_pt"][other_across]
        other_start = min(other["start_point_pt"][other_along],
                          other["end_point_pt"][other_along])
        other_end = max(other["start_point_pt"][other_along],
                        other["end_point_pt"][other_along])

        if other_along != along:
            # Crossing. Its centreline sits at `other_position` on this wall's
            # own axis, and this wall's end has to be near it - but the other
            # wall also has to actually reach across this one, or they are two
            # walls passing at a distance.
            gap = abs(other_position - where)
            if gap >= best:
                continue
            if not (other_start - reach <= position <= other_end + reach):
                continue
            best, target = gap, other_position
        else:
            # Carrying on, on the same line. Its own position across must match
            # this one's, or it is a different wall running parallel.
            if abs(other_position - position) > reach:
                continue
            for edge in (other_start, other_end):
                gap = abs(edge - where)
                if gap < best:
                    best, target = gap, edge
    return target
