"""Where a wall is broken, read off the faces **before** anything is closed.

This is the step that makes the computer-vision reader able to find openings at
all, and it has to happen before the morphology rather than after it.

**Why closing cannot leave the breaks behind.** Step 4 joins a wall's two drawn
faces into one solid band by closing with a kernel the width of the thickest
wall the office builds - 320 mm, which is 38 pixels on a 1:100 sheet at 300
DPI. A door is 820 mm, so in principle closing cannot bridge it. In practice it
does, because a doorway is not empty on a drawing: the office draws the jambs
across the wall, the door leaf, the swing arc springing from the hinge and
often a threshold line, and every one of those is ink inside the opening.
Closing joins the wall to that ink and the ink to the far side, and the band
comes out continuous. The break is gone before anything can look for it, and a
break that never formed cannot be reported, reviewed or explained.

**The faces still say where it is.** A door goes through the wall, so **both**
of its faces stop at the same place and start again together. That is a fact
about the drawn line work, and the line work is still intact at this point -
before any closing, before any rasterising. So the faces are paired here, the
gaps that interrupt both of them are found, and those become explicit boxes
that are punched out of the mask *before* it is closed. The gap then survives
the morphology because the morphology is never given the chance to fill it.

Two things are punched out, and both matter:

*   **Shared gaps between paired faces** - the geometric evidence, and the only
    evidence on a plan set that prints no opening marks at all.
*   **The openings already found** in Step 3 - the printed marks (``D01``,
    ``W12``) and the door swings read off the drawing's own curves. Where a
    mark is printed the drawing is telling us directly, and a mark's box does
    not need a shared gap to be believed.

**The clearance is dynamic**, because a fixed one is wrong at both ends. A
padded box has to clear the jamb lines drawn across the wall, and those are
drawn at the wall's own thickness - so the clearance is the larger of a stated
allowance in millimetres and a share of the thickness of the wall the break is
in. On a 90 mm partition that is a small box; on a 350 mm cavity wall it is a
proportionally larger one, which is what the drawing itself does.

Nothing here invents an opening. A punched box only means "do not let the
morphology weld this shut" - whether it is really a door, a window or a
cupboard is decided afterwards by the four readings in ``openingevidence`` and
by ``openings.py``, exactly as before.
"""

import math

from app.logging_setup import get_logger
from pipeline.plan import walls as legacy
from pipeline.plan.cvdetect.settings import number

logger = get_logger()

# A drawn line is on one axis or the other. This is the same allowance the rest
# of the reader makes for a scanned sheet's own skew.
_AXIS_TOLERANCE_PT = 0.35


def segments_by_axis(paths) -> dict:
    """Step 1's structural segments as ``(position, start, end)`` per axis.

    The shape ``layout.extract_rulings`` and ``rasterlines`` both return, so the
    face merging below does not care which of the three produced them.
    """
    result = {"h": [], "v": [], "h_widths": [], "v_widths": []}
    for segment in getattr(paths, "structural", []) or []:
        x0, y0, x1, y1 = segment.x0, segment.y0, segment.x1, segment.y1
        if abs(y1 - y0) <= _AXIS_TOLERANCE_PT and abs(x1 - x0) > _AXIS_TOLERANCE_PT:
            result["h"].append(((y0 + y1) / 2.0, min(x0, x1), max(x0, x1)))
            result["h_widths"].append(segment.width)
        elif abs(x1 - x0) <= _AXIS_TOLERANCE_PT and abs(y1 - y0) > _AXIS_TOLERANCE_PT:
            result["v"].append(((x0 + x1) / 2.0, min(y0, y1), max(y0, y1)))
            result["v_widths"].append(segment.width)
    return result


def shared_gaps(rulings: dict, scale, settings: dict) -> list:
    """Every place where **both** faces of a wall stop together.

    Returns page-space boxes ``[x0, y0, x1, y1]`` spanning the wall's own
    thickness band across the break, with the dynamic clearance already added.

    The face merging and the shared-gap test are ``walls.py``'s own functions,
    imported and reused. What makes a break a break is one fact about drawing -
    both faces stop - and there must not be two answers to it in one codebase
    (Critical Rule 2).
    """
    if not scale.usable:
        return []
    mm_per_point = scale.mm_per_point
    narrowest_mm = number(settings, "openings.window_min_width_mm", 300.0)
    widest_mm = number(settings, "openings.door_max_width_mm", 2400.0)
    widest_mm = max(widest_mm, number(settings, "openings.window_max_width_mm", 6000.0))
    position_tolerance = number(settings, "breaks.collinear_tolerance_pt", 0.6)
    # A face broken by a doorway is still one face; pieces closer than the
    # widest opening are joined so that the gap between them is *recorded*
    # rather than ending the face.
    join_gap = max(widest_mm / mm_per_point, 2.0)

    # **Faces are paired best-first, each used once - and that is the whole
    # difference between a break and an artefact.** Pairing every plausible
    # pair lets one face pair with a dozen others, and every fragmentation gap
    # in any of them becomes a "shared gap": measured on one sheet read as a
    # picture, 69 of them, which punched so much out of the mask that the sheet
    # went from 37 walls to 16. ``walls.py`` already scores each pair by the
    # length the two faces run together and takes them best-first with each
    # face used once, and it computes the shared gaps as it goes - so it is
    # reused rather than answered a second way (Critical Rule 2).
    paired = _legacy_shaped(settings, mm_per_point)
    standards = paired["walls"]["nominal_thickness_mm"]

    boxes = []
    for axis, key, widths_key in (("h", "h", "h_widths"), ("v", "v", "v_widths")):
        segments = rulings.get(key) or []
        if len(segments) < 2:
            continue
        widths = rulings.get(widths_key) or []
        try:
            faces = legacy._merge_faces(
                segments, position_tolerance, join_gap,
                widths if len(widths) == len(segments) else None,
                None,
            )
            pairs, _used, _usable = legacy._pair_faces_and_faces(
                faces, mm_per_point, paired, standards
            )
        except Exception as e:
            logger.exception(f"shared_gaps: faces could not be paired on {axis}: {e}")
            continue

        for pair in pairs:
            low_face, high_face = sorted(pair["face_positions"])
            separation = high_face - low_face
            for low, high in pair.get("gaps") or []:
                width_mm = (high - low) * mm_per_point
                if not (narrowest_mm <= width_mm <= widest_mm):
                    continue
                boxes.append(
                    _box(axis, low, high, low_face, high_face, separation,
                         mm_per_point, settings)
                )
    if boxes:
        logger.info(f"breaks: {len(boxes)} shared gap(s) found in the faces before closing")
    return boxes


def _legacy_shaped(settings: dict, mm_per_point: float) -> dict:
    """cvdetect's own settings in the shape ``walls.py``'s pairing reads.

    The values come from ``config/cv_detection.json`` so this module stays
    configurable in one place; only their *names* are translated.
    """
    return {
        "walls": {
            "min_wall_length_mm": number(settings, "wall.min_length_mm", 600.0),
            "min_thickness_mm": number(settings, "wall.min_thickness_mm", 70.0),
            "max_thickness_mm": number(settings, "wall.max_thickness_mm", 320.0),
            "min_length_to_thickness": number(settings, "wall.min_length_to_thickness", 1.5),
            "min_parallel_overlap_percent": number(
                settings, "breaks.min_face_overlap_share", 0.5
            ) * 100.0,
            "opening_min_wall_each_side_mm": number(
                settings, "breaks.min_wall_each_side_mm", 300.0
            ),
            "nominal_thickness_tolerance_mm": 12,
            "nominal_thickness_mm": [70, 90, 110, 140, 190, 230, 270, 290],
            "second_look_widening": 0.0,
        }
    }


def _box(axis, low, high, position, other_position, separation, mm_per_point, settings):
    """One break as a page-space box, with the dynamic clearance added.

    The clearance has to clear the jamb lines the office draws across the wall,
    and those are drawn at the wall's own thickness - so it is the larger of a
    stated allowance and a share of that thickness. A fixed figure is wrong at
    both ends: too small on a 350 mm cavity wall and needlessly large on a
    90 mm partition.
    """
    stated = number(settings, "breaks.clearance_mm", 40.0) / mm_per_point
    share = number(settings, "breaks.clearance_share_of_thickness", 0.5)
    clearance = max(stated, separation * share)
    across_low, across_high = position - clearance, other_position + clearance
    if axis == "h":
        return [low - clearance, across_low, high + clearance, across_high]
    return [across_low, low - clearance, across_high, high + clearance]


def boxes_for_the_mask(page, scale, paths, settings, openings=None, rulings=None) -> list:
    """Everything that must survive the closing, as page-space boxes.

    The shared gaps read off the faces, plus the openings Step 3 already found -
    a printed mark or a drawn swing is the drawing telling us directly, and it
    does not need a shared gap to be believed.
    """
    boxes = []
    try:
        source = rulings if rulings is not None else segments_by_axis(paths)
        boxes.extend(shared_gaps(source, scale, settings))
    except Exception as e:
        logger.exception(f"boxes_for_the_mask: the faces could not be read: {e}")

    for opening in openings or []:
        try:
            box = list(opening.bbox)
            if len(box) == 4:
                boxes.append(box)
        except Exception:
            continue
    return boxes
