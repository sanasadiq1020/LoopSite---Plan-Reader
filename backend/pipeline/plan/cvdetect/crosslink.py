"""Step 5 - which wall each opening is in.

An opening on its own is a hole in the air. What a model, a schedule, a
take-off and a crew instruction all need is *which wall* it is a hole in, and
that is a spatial question with a measurable answer: an opening belongs to the
wall whose centreline it is nearest, within a distance a wall could plausibly
be from its own door.

Two things this deliberately does not do:

*   **It never picks between two walls that are equally close.** Where a mark
    sits between two candidates and neither is clearly nearer, the opening is
    reported unplaced with that reason. A wrong link is worse than none,
    because every later stage trusts it - the model cuts the void into the
    wrong wall and nothing downstream can tell (Critical Rules 4 and 5).
*   **It never invents a distance.** The reach is stated in millimetres of
    building in ``config/cv_detection.json`` and turned into paper through the
    calibrated scale, so it means the same thing on a 1:50 detail and a 1:200
    site plan.

Every opening also records **how far along its wall** it sits, as a fraction of
the wall's length rather than as a distance. Fractions survive the change from
the page's downward Y into a building's northward Y without anything
downstream having to know that the flip happened.
"""

from app.logging_setup import get_logger
from pipeline.plan.cvdetect.settings import number

logger = get_logger()

# How much nearer the winning wall has to be than the runner-up before the
# choice counts as unambiguous. A ratio, not a distance, so it means the same
# at any scale: a wall 10% nearer than another is not a decision anyone should
# rely on.
_CLEARLY_NEARER = 1.25


def link_openings_to_walls(walls: list, openings: list, scale, settings: dict) -> dict:
    """Attaches each opening to the wall it is in, or reports why it is not.

    Returns ``{wall_id: [opening_id, ...]}``, and writes the reverse link onto
    each opening. Never raises: an opening that cannot be placed is placed
    nowhere and says so (Critical Rule 6).
    """
    links = {wall.element_id: [] for wall in walls}
    if not walls or not openings:
        return links
    if not scale.usable:
        for opening in openings:
            opening.evidence.append(
                "Not linked to a wall: what one point of this sheet measures could "
                "not be established."
            )
        return links

    # **The reader made the gap, so the reader has to allow for it.** Step 3
    # paints every opening white before the walls are closed - that is the
    # whole point of finding openings first - so by the time a wall band
    # exists there is a hole in it exactly where the opening is, padded by
    # ``openings.mask_padding_mm``. An opening therefore never overlaps its
    # own wall, and measuring without allowing for that reported real doors
    # carrying their own D-marks as "no wall within 200 mm". The allowance is
    # the padding this reader itself applied, plus the offset an office may
    # draw a mark at - both from the config, neither invented here.
    reach_mm = number(settings, "crosslink.max_offset_mm", 200.0) + number(
        settings, "openings.mask_padding_mm", 40.0
    )
    reach_pt = reach_mm / scale.mm_per_point

    # **A mark is printed beside its opening; a symbol is drawn on it.** That
    # is a drafting convention rather than a tuning value: a D12 commonly sits
    # inside the room on a leader, while the swing it labels is drawn in the
    # wall itself. Measuring both to the same allowance reported real, marked
    # doors as having no wall near them.
    mark_reach_mm = number(settings, "crosslink.mark_reach_mm", 1000.0) + number(
        settings, "openings.mask_padding_mm", 40.0
    )

    placed = 0
    for opening in openings:
        drawn_on_it = opening.found_by != "printed_mark"
        allowance_mm = reach_mm if drawn_on_it else mark_reach_mm
        try:
            chosen, reason = _wall_for(
                opening, walls, allowance_mm / scale.mm_per_point, allowance_mm, scale
            )
        except Exception as e:
            logger.exception(f"link_openings_to_walls: {opening.opening_id} failed: {e}")
            chosen, reason = None, "This opening could not be placed; the failure is logged."
        if chosen is None:
            opening.wall_id = None
            opening.evidence.append(reason)
            continue
        opening.wall_id = chosen.element_id
        opening.evidence.append(reason)
        chosen.opening_ids.append(opening.opening_id)
        links.setdefault(chosen.element_id, []).append(opening.opening_id)
        placed += 1

    logger.info(
        f"openings on walls: {placed} of {len(openings)} placed "
        f"({placed / max(len(openings), 1):.0%})"
    )
    return links


def _wall_for(opening, walls: list, reach_pt: float, reach_mm: float, scale):
    """The wall an opening is in, or None with the reason it could not be said."""
    try:
        from shapely.geometry import box
    except ImportError:
        return None, "Shapely is not installed here, so openings cannot be placed on walls."

    x0, y0, x1, y1 = opening.bbox
    footprint = box(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))

    # **Measured to the wall band, not to its centreline.** A centreline sits
    # half a thickness inside the wall, so a door drawn hard against its own
    # jamb is already 45 to 115 mm from it before it has moved at all - and a
    # swing's box reaches a whole leaf's width into the room. Measured against
    # centrelines, real doors carrying their own D-marks were reported as "the
    # nearest wall is 235 mm away". The band is the wall as drawn, so an
    # opening in it measures zero.
    reach = []
    for wall in walls:
        shape = wall.outline if wall.outline is not None else wall.centreline
        if shape is None:
            continue
        try:
            distance = footprint.distance(shape)
            overlap = footprint.intersection(shape).area if distance == 0 else 0.0
        except Exception:
            continue
        reach.append((distance, overlap, wall))
    if not reach:
        return None, "There is no wall on this sheet for this opening to sit in."

    # Nearest first; where two are equally near, the one the opening actually
    # sits further into.
    reach.sort(key=lambda entry: (entry[0], -entry[1]))
    nearest, overlap, wall = reach[0]
    if nearest > reach_pt:
        away_mm = nearest * scale.mm_per_point
        return None, (
            f"Not placed on a wall: the nearest wall is {away_mm:.0f} mm away, beyond the "
            f"{reach_mm:.0f} mm a door or window is drawn from the wall it is in."
        )

    # Two walls equally close is not a decision - it is two candidates, and
    # choosing arbitrarily puts the void in the wrong one half the time. But
    # *how much* of the opening lies in each wall is real evidence rather than
    # a coin toss: a door at a corner touches the return wall and lies in its
    # own, and only a genuine tie is reported as one.
    if len(reach) > 1:
        next_distance, next_overlap, next_wall = reach[1]
        tie_on_distance = nearest == 0 and next_distance == 0
        tie_on_distance = tie_on_distance or (
            nearest > 0 and next_distance <= nearest * _CLEARLY_NEARER
        )
        if tie_on_distance and next_overlap >= overlap * _CLEARLY_NEARER ** -1:
            if overlap <= 0 or next_overlap >= overlap / _CLEARLY_NEARER:
                return None, (
                    f"Not placed on a wall: {wall.element_id} and {next_wall.element_id} are "
                    "both equally close to it, and choosing between them would be a guess."
                )

    position = _how_far_along(footprint, wall)
    if position is not None:
        opening.evidence.append(
            f"It sits {position[0]:.0%} to {position[1]:.0%} of the way along "
            f"{wall.element_id}."
        )
    if nearest == 0:
        return wall, f"Placed in {wall.element_id}, which it is drawn inside."
    return wall, (
        f"Placed in {wall.element_id}, which is {nearest * scale.mm_per_point:.0f} mm away."
    )


def _how_far_along(footprint, wall):
    """Where along its wall an opening starts and ends, as two fractions.

    Fractions rather than millimetres, because they survive the turn from the
    page's downward Y into the building's northward Y without anything
    downstream having to know it happened - which is what lets a later stage
    cut the void in the right place.
    """
    try:
        line = wall.centreline
        total = line.length
        if total <= 0:
            return None
        x0, y0, x1, y1 = footprint.bounds
        corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        along = [line.project(_point(x, y)) / total for x, y in corners]
        return (max(0.0, min(along)), min(1.0, max(along)))
    except Exception:
        return None


def _point(x, y):
    from shapely.geometry import Point

    return Point(x, y)
