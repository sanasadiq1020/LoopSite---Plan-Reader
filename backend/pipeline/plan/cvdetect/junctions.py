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


def extend_along_the_ink(walls: list, mm_per_point: float, config: dict,
                         ink=None, scale=None, junction_slack: float = 10.0) -> int:
    """Carries a wall end on to the wall it meets, for as long as the sheet draws it.

    **A traced end is short of the truth wherever the drawing keeps drawing the
    same wall.** Snapping moves an end that is already beside its neighbour, and
    the ray above carries one a short way onto a wall that already spans its
    line. Neither reaches the common case measured on real sheets: an end that
    stops one to three metres short of the wall it plainly runs into, on a
    drawing that shows the wall continuously all the way there. Measured across
    three plan sets, **35 of 69 such ends have the wall drawn in the gap**, 14 of
    them with unbroken ink over 1.8 to 14.8 m.

    **The geometry proposes and the drawing decides.** Two things must both hold
    before an end is moved, and the second is what keeps this from inventing
    walls:

    1.  *There is something on this wall's own line to meet* - a wall crossing
        it, or one carrying on collinear with it - lying outward of the end.
        Nothing is ever extended towards open paper, because there is nothing
        there to extend to.
    2.  *The sheet's own ink runs the whole way*, inside this wall's own
        thickness band, from the traced end to that target. This is the
        evidence, and it is the drawing's rather than the reader's.

    **What may interrupt the ink, and what may not.** A wall's band is broken
    where another wall crosses it, which is an interruption as wide as the
    crossing wall. It is also broken at a doorway - and a doorway must *never*
    be bridged, or a wall is reported running through its own opening. So the
    longest void allowed is **this wall's own thickness**: a crossing wall of
    like thickness fits inside it and the narrowest doorway an office builds
    does not. That is a physical statement about the drawing, not a tuned
    number, and it needs no setting of its own.

    **What this relies on**: that a wall is drawn continuously along its own
    thickness band, and that a door is wider than a wall is thick. **What would
    break it**: a wall drawn with a long unhatched stretch, which stops the
    extension and simply leaves the end where it was; and a fitting or a run of
    joinery drawn hard along a wall's band past its real end, which would carry
    an end too far - the target requirement bounds that, since it can only ever
    reach as far as the next wall on the same line.

    Never raises, and does nothing at all without an image (Critical Rule 6).
    """
    if ink is None or scale is None:
        return 0
    if not setting(config, "wall.extend_along_the_ink", True):
        return 0
    if len(walls) < 2:
        return 0

    moved = 0
    try:
        for wall in walls:
            along = 0 if wall["runs_along"] == "x" else 1
            across = 1 - along
            position = wall["start_point_pt"][across]
            low = min(wall["start_point_pt"][along], wall["end_point_pt"][along])
            high = max(wall["start_point_pt"][along], wall["end_point_pt"][along])
            band = sorted(wall["face_positions_pt"])
            void = max(band[1] - band[0], 0.5)

            for which in ("low", "high"):
                where = low if which == "low" else high
                if _has_a_junction_at(wall, along, where, junction_slack):
                    continue
                target = _target_on_this_line(
                    wall, walls, along, across, position, where,
                    outward=(which == "high"), slack=junction_slack,
                )
                if target is None:
                    continue
                if not _drawn_all_the_way(ink, scale, along, band, where, target, void):
                    continue
                if which == "low" and target < low:
                    low = target
                elif which == "high" and target > high:
                    high = target
                else:
                    continue
                moved += 1

            _relay(wall, along, low, high, position)
    except Exception as e:
        logger.exception(f"the wall ends could not be carried along the drawing's ink: {e}")

    if moved:
        logger.info(
            f"walls: {moved} end(s) carried on to the wall they meet, along ink the "
            "sheet draws the whole way"
        )
    return moved


def _target_on_this_line(wall, walls, along, across, position, where, outward, slack):
    """The nearest wall outward of this end that lies on this wall's own line."""
    best = None
    for other in walls:
        if other is wall:
            continue
        other_along = 0 if other["runs_along"] == "x" else 1
        other_across = 1 - other_along
        other_low = min(other["start_point_pt"][other_along],
                        other["end_point_pt"][other_along])
        other_high = max(other["start_point_pt"][other_along],
                         other["end_point_pt"][other_along])
        if other_along != along:
            # Crossing this wall's line. Its centreline is the meeting point,
            # and it has to reach across this wall - allowing the same slack a
            # junction is allowed, because at an open corner both walls stop
            # short of each other and neither spans the other.
            edge = other["start_point_pt"][other_across]
            if not (other_low - slack <= position <= other_high + slack):
                continue
        else:
            # Carrying on along the same line, past a doorway or a break in the
            # tracing. Its own band has to sit on this wall's line.
            faces = sorted(other["face_positions_pt"])
            if not (faces[0] - slack <= position <= faces[1] + slack):
                continue
            edge = other_low if outward else other_high
        step = edge - where
        if outward and step <= 0:
            continue
        if not outward and step >= 0:
            continue
        if best is None or abs(step) < abs(best - where):
            best = edge
    return best


def _drawn_all_the_way(ink, scale, along, band, frm, to, void) -> bool:
    """Whether the sheet draws **this wall** the whole way from ``frm`` to ``to``.

    **Both of the wall's faces must be inked, not merely something inside its
    band.** That distinction is the whole safety of this rule, and it was
    learned by watching it fail: asking only whether *any* ink lay in the band
    carried a partition's end up off the top of a house and into the pergola
    drawn above it, because a pergola beam is a line and it happened to lie on
    that partition's own line. The reading then gained a 4.7 m "external wall"
    in the roof strip and the traced building measured 11% *over* what the sheet
    prints, having been 17% under.

    A wall is two parallel faces a thickness apart - that is the definition the
    whole reader is built on - and a roof line, an eave, a setback and a grid
    tick are each a single line. So each face is looked for in its own window,
    a quarter of the band either side of where the face was measured, and both
    must be lit at every step. No single line can satisfy that.

    Sampled at half a point, which is finer than any line weight a wall is
    plotted at, so a stroke cannot fall between two samples.
    """
    low, high = (frm, to) if frm <= to else (to, frm)
    if high - low <= 0:
        return True
    k = getattr(scale, "pixels_per_point", 0.0)
    if k <= 0:
        return False
    height, width = ink.shape[:2]
    thickness = max(band[1] - band[0], 1.0 / k)
    window = max(thickness / 4.0, 1.0 / k)
    steps = max(2, int((high - low) / 0.5))
    step_pt = (high - low) / steps

    def face_is_lit(at, face):
        first = int(round((face - window) * k))
        last = int(round((face + window) * k))
        for offset in range(min(first, last), max(first, last) + 1):
            column, row = (int(at * k), offset) if along == 0 else (offset, int(at * k))
            if 0 <= row < height and 0 <= column < width and ink[row, column]:
                return True
        return False

    run = worst = 0.0
    for index in range(steps):
        at = low + (index + 0.5) * step_pt
        if face_is_lit(at, band[0]) and face_is_lit(at, band[1]):
            run = 0.0
        else:
            run += step_pt
            worst = max(worst, run)
            if worst > void:
                return False
    return True


def _relay(wall: dict, along: int, low: float, high: float, position: float) -> None:
    """Writes a wall's run back onto it, keeping its thickness untouched."""
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


def close_the_graph(walls: list, mm_per_point: float, config: dict,
                    ink=None, scale=None, junction_slack: float = 10.0) -> dict:
    """Snapping and ray casting, repeated until nothing more moves.

    **One pass is not enough, and the reason is geometric rather than a matter
    of tolerance.** Snapping an end onto the wall it was traced up to *moves
    that wall*, which can bring a third wall's end inside reach of it for the
    first time; a ray cast at a wall that has since been extended may now hit
    something it missed. So both are run again until a pass changes nothing, or
    the configured number of passes is spent.

    Bounded on purpose: each pass can only move an end by the reach, so an
    unbounded loop could walk a wall across the sheet a step at a time. The
    passes are counted and reported.
    """
    passes = max(int(number(config, "wall.snap_passes", 3)), 1)
    moved = {"snapped": 0, "extended": 0, "carried": 0, "passes": 0}
    for _ in range(passes):
        snapped = snap_endpoints(walls, mm_per_point, config)
        extended = cast_rays_from_free_ends(walls, mm_per_point, config)
        carried = extend_along_the_ink(
            walls, mm_per_point, config, ink=ink, scale=scale,
            junction_slack=junction_slack,
        )
        moved["snapped"] += snapped
        moved["extended"] += extended
        moved["carried"] += carried
        moved["passes"] += 1
        if not snapped and not extended and not carried:
            break
    return moved


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
