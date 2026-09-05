"""Step 4 - the wall bands, their outlines and their centrelines.

What arrives here is a binary image of the drawing with the noise already gone
(Step 1) and the openings already painted white (Step 3). What leaves is a list
of walls, each with a measured thickness, a centreline as a Shapely
``LineString`` and an outline as a ``Polygon``.

The sequence, and why each part is there:

1.  **Closing, with a kernel taken from the scale.** A wall drawn as an
    outline is two faces its own thickness apart, so the kernel has to reach
    right across the wall to join them - the width of the **thickest** wall the
    office builds. Sized at the thinnest instead, a 230 mm wall's faces are
    never joined at all: measured on a building drawn to known dimensions, that
    reported no walls whatever. Two different walls are a room apart, so a
    kernel this size joins a wall to itself and never to its neighbour, and
    anything closed into a blob wider than a wall is caught by the thickness
    test below. It is computed from the calibrated scale every time - 38 pixels
    on a 1:100 sheet at 300 DPI and 19 on a 1:200 one - and writing either
    number down would be a promise about one drawing.

2.  **The distance transform, which is where the thickness comes from.** Every
    pixel in a band is labelled with how far it is from the nearest paper, so
    on the centre of a wall that value is exactly half the wall's thickness.
    That makes the thickness a *measurement* rather than an assumption, and it
    is the same figure that decides whether a band is a wall at all: below the
    thinnest wall the office builds it is a lining, a hatch boundary or a
    dimension line, and above the thickest it is a filled panel.

3.  **Contours, for the outline** (``cv2.findContours``), and

4.  **Thinning, for the centreline** (``cv2.ximgproc.thinning``), which is the
    one step that reduces a band of any shape to a single line down the middle
    of it - including at a corner and at a T, which is exactly where a wall
    reader has to be right. **Each component is padded with blank margin
    first**: cropped tightly to its own bounding box a shape touches all four
    borders of the crop, thinning treats out-of-bounds as background, and the
    border pixels are kept as though they were the edge of the shape. Measured
    on a building drawn to known dimensions, every centreline came out on the
    outer face of its wall and every thickness read 81 mm against a drawn
    230 mm - a wrong answer that looked entirely reasonable.

5.  **Simplification** (``cv2.approxPolyDP``) at a tolerance in millimetres of
    building, then Shapely geometry for everything downstream.

**Thinning is done per connected component, and that is not a detail.** Run
over the whole sheet it takes **52 seconds** on one A3 floor plan at 300 DPI,
because it is iterative and it scans all 17 megapixels on every pass - and 93%
of them are blank paper. Run on each component inside its own bounding box it
takes **2.9 seconds** for exactly the same answer: components are 8-disconnected
by definition, so thinning one can never depend on another, and each one's box
is a few per cent of the sheet. Measured on the same sheet, an eighteen-fold
saving with the skeleton unchanged.
"""

import math
from dataclasses import dataclass, field

from app.logging_setup import get_logger
from pipeline.plan.cvdetect import breaks, imaging
from pipeline.plan.cvdetect.settings import number, setting

logger = get_logger()

# A component smaller than this many pixels is a speck: a full stop, a hatch
# dot, a compression artefact. Counted in pixels because that is what it is -
# a mark too small for the render to have resolved at all.
_SMALLEST_COMPONENT_PX = 20

# Directions an Australian drawing hatches a wall at. Kept here rather than in
# the config because 45 degrees is what a hatch *is* under AS 1100; an office
# that hatches at 30 sets it in the config file.
_HATCH_KERNEL_LENGTH_PX = 9

# Blank margin put round each component before it is thinned. See
# ``_walls_from_band`` for what happens without it.
_PAD_PX = 2


@dataclass
class Wall:
    """One wall band, measured off the drawing.

    Carries the twelve fields Critical Rule 12 requires of every canonical
    element record, so that a wall read by this module drops straight into the
    same model as one read any other way.
    """

    element_id: str
    centreline: object
    outline: object
    thickness_mm: float
    length_mm: float
    source_sheet: str
    source_bbox: list
    extraction_method: str
    confidence: float
    element_type: str = "wall"
    storey: str = None
    review_status: str = "unreviewed"
    linked_issue_ids: list = field(default_factory=list)
    material: str = None
    fill_share: float = 0.0
    drawn_as: str = "outline"
    opening_ids: list = field(default_factory=list)
    note: str = ""

    def as_record(self) -> dict:
        coords = list(self.centreline.coords) if self.centreline is not None else []
        return {
            "element_id": self.element_id,
            "element_type": self.element_type,
            "storey": self.storey,
            "geometry": {
                "centreline": [[round(x, 2), round(y, 2)] for x, y in coords],
                "outline": _outline_coords(self.outline),
            },
            "dimensions": {
                "thickness_mm": round(self.thickness_mm, 1),
                "length_mm": round(self.length_mm, 1),
            },
            "material": self.material,
            "source_sheet": self.source_sheet,
            "source_bbox": [round(v, 2) for v in self.source_bbox],
            "extraction_method": self.extraction_method,
            "confidence": round(self.confidence, 3),
            "review_status": self.review_status,
            "linked_issue_ids": list(self.linked_issue_ids),
            "opening_ids": list(self.opening_ids),
            "interior_fill_share": round(self.fill_share, 3),
            "interior_drawn_as": self.drawn_as,
            "note": self.note,
        }


def _outline_coords(outline):
    if outline is None:
        return []
    try:
        return [[round(x, 2), round(y, 2)] for x, y in outline.exterior.coords]
    except Exception:
        return []


def why_this_sheet_has_no_walls(page, settings: dict) -> str:
    """A sentence, where this sheet is one whose drawing has no walls in it.

    **A site plan draws the block, not the building**, and a roof plan draws
    what is over it. Their parallel lines are boundaries, setbacks, easements,
    fences, driveways, kerbs and contours on the one, and battens, ridges,
    valleys and gutter lines on the other - and none of those is a wall. Worse,
    the thickness test cannot separate them: at 1:200 the band that means "a
    70 to 320 mm wall" is 1 to 4.5 points of paper, and on a site plan almost
    every pair of lines is that far apart.

    Measured on a real site-plan-and-roof-plan sheet, tracing it produced
    **124 walls and 400 metres** of them, every one a boundary or a batten. It
    is exactly the shape of error this reader exists to avoid: plausible,
    confidently reported, and completely wrong. The main pipeline records the
    same finding for the same reason (CLAUDE.md 4AE).

    **The sheet's own title is the evidence**, and the wording is configuration
    rather than a list in code (Critical Rule 1), because what one office calls
    a stormwater plan another calls a drainage plan. A sheet that names *both*
    a kind with walls and a kind without - a floor plan with a small roof plan
    inset beside it - is traced: the safe direction is to read it and let the
    geometry decide.
    """
    try:
        text = (page.get_text() or "").upper()
    except Exception as e:
        logger.exception(f"why_this_sheet_has_no_walls: this sheet's text could not be read: {e}")
        return ""
    if not text:
        return ""

    without = [
        str(w).upper() for w in (setting(settings, "page.never_trace_walls_on", []) or [])
        if str(w).upper() in text
    ]
    if not without:
        return ""
    if any(
        str(w).upper() in text
        for w in (setting(settings, "page.traces_walls_on", []) or [])
    ):
        return ""
    named = without[0].title()
    return (
        f"This sheet is a {named}, so no walls were traced on it. The parallel lines on a "
        "drawing of this kind are boundaries, setbacks, fences, driveways, battens and "
        "gutter lines - none of them a wall of the building."
    )


def build_ink(page, scale, paths, settings: dict):
    """The binary image the walls are measured from, and where it came from.

    **The drawing's own line work is always tried first.** It is exact, and
    nothing recovered from pixels can beat it. Only where a sheet stores its
    drawing as embedded images - which one real Australian plan set in use does
    - is the page rendered and thresholded instead, and every wall then records
    that its measurement came from pixels rather than from geometry.
    """
    structural = paths.structural if paths else []
    if len(structural) >= 4 or paths.fills:
        return imaging.ink_from_paths(page, scale, structural, paths.fills), "vector_paths"
    logger.info(
        "this sheet holds no usable drawn line work, so its lines are recovered from the page"
    )
    return imaging.ink_from_page_lines(page, scale, settings), "page_image"


def detect_walls(
    page,
    scale,
    paths,
    settings: dict,
    openings_mask=None,
    sheet_name: str = "",
    ink=None,
) -> tuple:
    """Wall bands, outlines and centrelines, measured off this sheet.

    Returns ``(walls, diagnostics)``. Never raises: a sheet this cannot read
    comes back with no walls and a note saying why, because a sheet that
    defeats the reader is a normal outcome and not a failed run
    (Critical Rule 6).
    """
    diagnostics = {
        "line_source": None, "components": 0, "candidates": 0,
        "notes": [], "face_pairs": [],
    }

    refusal = why_this_sheet_has_no_walls(page, settings)
    if refusal:
        diagnostics["notes"].append(refusal)
        logger.info(f"walls: not traced on {sheet_name or 'this sheet'} - {refusal}")
        return [], diagnostics

    if not scale.usable:
        diagnostics["notes"].append(
            "No length is measured from this sheet, because what one point of it "
            "represents could not be established."
        )
        return [], diagnostics

    try:
        import cv2
        import numpy as np
    except ImportError:
        logger.warning("OpenCV is not installed, so walls cannot be measured on this server")
        diagnostics["notes"].append("This server cannot measure walls: OpenCV is not installed.")
        return [], diagnostics

    try:
        if ink is None:
            ink, source = build_ink(page, scale, paths, settings)
        else:
            source = "vector_paths" if paths and paths.structural else "page_image"

        # **The breaks are read off the faces before anything is closed.**
        # Closing joins a wall to the jamb lines, the leaf and the swing arc
        # drawn inside its own doorway, and the band comes out continuous - so
        # the gap has to be punched out before the morphology is given the
        # chance to weld it shut. See ``breaks.py``.
        mask, vector_breaks = _mask_with_the_breaks(
            page, scale, paths, settings, openings_mask, None
        )
        diagnostics["face_pairs"] = vector_breaks
        strip = _noise_to_strip(page, scale, paths, settings)
        walls, components = _measure(
            page, scale, ink, source, settings, mask, sheet_name, vector_breaks, strip
        )

        # **The switch is decided on what the reading produced, not on how much
        # geometry was there.** A plan set can be published as embedded images
        # with only its frame and title block as real paths - one of the sets in
        # use is exactly that, with 164 drawn segments on a sheet whose whole
        # building is pixels. Counting paths, that sheet looks like a vector
        # drawing and comes back with one wall on it. A building is a closed
        # shape, so it takes at least four walls; below that the geometry has
        # not traced a building, whatever it returned.
        #
        # The sheet's own line work is always tried first, because it is exact
        # and nothing recovered from pixels can beat it - and the fuller of the
        # two readings wins, so exact geometry is never displaced by pixels that
        # found less.
        fewest = int(number(settings, "wall.min_walls_for_vector", 4))
        if source == "vector_paths" and len(walls) < fewest:
            logger.info(
                f"this sheet's own geometry traced {len(walls)} walls, which is not a "
                "building; it is read as a picture as well"
            )
            # The picture's own line work, recovered once and used twice: to
            # draw the mask, and to find the breaks in its faces.
            page_rulings = imaging.page_line_work(page, scale)
            page_mask, page_breaks = _mask_with_the_breaks(
                page, scale, paths, settings, openings_mask, page_rulings
            )
            from_page, page_components = _measure(
                page, scale,
                imaging.ink_from_rulings(page, scale, page_rulings, settings),
                "page_image", settings, page_mask, sheet_name, page_breaks, strip,
            )
            if len(from_page) > len(walls):
                walls, components, source = from_page, page_components, "page_image"
                diagnostics["face_pairs"] = page_breaks
                diagnostics["notes"].append(
                    "This sheet stores its drawing as a picture rather than as line work, "
                    "so its walls were measured from the page image. They are less exact "
                    "than walls measured from a drawing's own geometry."
                )

        diagnostics["line_source"] = source
        diagnostics["components"] = components
        diagnostics["candidates"] = len(walls)
    except Exception as e:
        logger.exception(f"detect_walls: this sheet could not be measured: {e}")
        diagnostics["notes"].append("This sheet's walls could not be measured; the failure is logged.")
        return [], diagnostics

    logger.info(
        f"walls: {len(walls)} measured from {diagnostics['components']} bands "
        f"({source}) on {sheet_name or 'this sheet'}"
    )
    return walls, diagnostics


def _noise_to_strip(page, scale, paths, settings: dict):
    """Everything that must never reach the wall tracing, as one mask.

    Two things, and both are ink that looks exactly like a wall once it is
    pixels:

    *   **Printed text.** A word set in capitals is a continuous run of dark
        pixels, and its top and bottom are two parallel lines a plausible wall
        thickness apart. The box is taken from the sheet's own text lines, so
        it is exact rather than guessed at.
    *   **Door swings.** The arc is drawn inside the doorway, so closing joins
        the wall to the arc and the arc to the far jamb - the opening is welded
        shut before a break can form. Only the arc itself is removed, not the
        opening: where the doorway is, and how wide, is Step 3's answer.

    Never raises, and returns None where there is nothing to strip, so a sheet
    whose text cannot be read is traced exactly as it was before.
    """
    if not setting(settings, "noise.strip_text_and_swings", True):
        return None
    boxes = []
    try:
        from pipeline.plan import textmodel

        pad = scale.px_from_pt(number(settings, "noise.text_padding_pt", 1.0))
        for line in textmodel.extract_native_lines(page) or []:
            box = line.get("bbox")
            if box and len(box) == 4:
                boxes.append([float(v) for v in box])
    except Exception as e:
        logger.exception(f"the printed text could not be read off this sheet: {e}")
        pad = 0.0

    try:
        squareness = number(settings, "openings.arc_squareness", 1.6)
        smallest = number(settings, "openings.door_min_width_mm", 600.0)
        largest = number(settings, "openings.door_max_width_mm", 2400.0)
        for curve in getattr(paths, "curves", []) or []:
            x0, y0, x1, y1 = curve["bbox"]
            across, down = abs(x1 - x0), abs(y1 - y0)
            if across <= 0 or down <= 0:
                continue
            if max(across / down, down / across) > squareness:
                continue
            leaf_mm = max(across, down) * scale.mm_per_point
            if smallest <= leaf_mm <= largest:
                boxes.append([x0, y0, x1, y1])
    except Exception as e:
        logger.exception(f"the door swings could not be read off this sheet: {e}")

    if not boxes:
        return None
    logger.info(f"{len(boxes)} text box(es) and door swing(s) kept out of the wall tracing")
    return imaging.draw_mask(page, scale, boxes, pad)


def _mask_with_the_breaks(page, scale, paths, settings, openings_mask, rulings):
    """The openings mask, with every shared gap in the faces punched out too.

    Returns ``(mask, pairs)`` - the mask Step 4 subtracts from the ink before
    closing, and the paired faces themselves, because a pair carries its band,
    its run and its gaps, and those gaps are injected onto the wall centrelines
    afterwards. Never raises: a
    sheet whose faces cannot be read keeps whatever mask Step 3 gave it, which
    is what the reader did before this existed (Critical Rule 6).
    """
    import cv2

    pairs = breaks.paired_faces_with_gaps(page, scale, paths, settings, rulings=rulings)
    if not setting(settings, "breaks.punch_before_closing", True):
        # The pairs are still reported: their gaps are injected onto the wall
        # centrelines afterwards, which is where they actually do their work.
        # What is switched off is only the cutting of the mask.
        return openings_mask, pairs

    boxes = breaks.boxes_for_the_mask(pairs, scale, settings)
    if not boxes:
        return openings_mask, pairs

    punched = imaging.draw_mask(page, scale, boxes, 0.0)
    if openings_mask is None:
        return punched, pairs
    try:
        return cv2.bitwise_or(openings_mask, punched), pairs
    except Exception:
        return punched, pairs


def _measure(
    page, scale, ink, source, settings, openings_mask, sheet_name,
    face_pairs=None, strip_mask=None,
):
    """Closing, distance transform, contours and centrelines, over one image."""
    import cv2
    import numpy as np

    if ink is None:
        return [], 0

    # **The openings are taken out before anything is closed.** This is the
    # whole point of finding them first: closing bridges a gap the width of a
    # wall, and a door gap is exactly that size.
    if openings_mask is not None:
        ink = cv2.bitwise_and(ink, cv2.bitwise_not(openings_mask))

    # **Lettering and door swings never reach the tracing.** A room name set in
    # capitals is a continuous run of dark pixels, so the top and bottom of the
    # word are two parallel lines a plausible wall thickness apart - measured on
    # one floor plan, 32 of its 157 "walls" were printed words lying across
    # their own rooms. A swing arc is worse than useless here: it is ink drawn
    # *inside* the doorway, and closing joins the wall to it and it to the far
    # side, welding the opening shut before any break can form.
    if strip_mask is not None:
        ink = cv2.bitwise_and(ink, cv2.bitwise_not(strip_mask))

    thinnest_px = max(
        2, int(round(scale.px_from_mm(number(settings, "wall.min_thickness_mm", 70.0))))
    )
    thickest_px = max(
        thinnest_px + 1,
        scale.px_from_mm(number(settings, "wall.max_thickness_mm", 320.0)),
    )
    # **The kernel has to reach across the wall, not across the thinnest wall.**
    # A wall drawn as an outline is two faces its own thickness apart, so
    # closing at the *thinnest* wall the office builds cannot join a 230 mm
    # wall's faces at all - measured on a building drawn to known dimensions, it
    # reported no walls whatever. Two different walls are a room apart, so a
    # kernel the size of the thickest wall joins a wall to itself and never to
    # its neighbour; anything closed into a blob wider than that is caught by
    # the thickness test below.
    kernel_px = max(
        2,
        int(round(
            scale.px_from_mm(number(settings, "wall.max_thickness_mm", 320.0))
            * number(settings, "wall.closing_share_of_thickest_wall", 1.0)
        )),
    )
    closed = cv2.morphologyEx(
        ink, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_px, kernel_px))
    )

    # Half the wall's thickness, at every pixel of it.
    distance = cv2.distanceTransform(closed, cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
    band = (distance >= thinnest_px / 2.0).astype(np.uint8) * 255

    return _walls_from_band(
        band, distance, ink, scale, settings, sheet_name, source, thickest_px,
        openings_mask, face_pairs,
    )


def _walls_from_band(
    band, distance, ink, scale, settings, sheet_name, source, thickest_px,
    openings_mask=None, face_pairs=None,
):
    """Outlines by contour, centrelines by thinning, one component at a time."""
    import cv2
    import numpy as np

    count, labels, stats, _centroids = cv2.connectedComponentsWithStats(band, 8)
    shortest_mm = number(settings, "wall.min_length_mm", 600.0)
    slenderness = number(settings, "wall.min_length_to_thickness", 1.5)
    simplify_px = max(1.0, scale.px_from_mm(number(settings, "wall.simplify_mm", 30.0)))

    walls = []
    gathered = []
    # **A band too small to hold the shortest wall the office builds cannot
    # hold a wall.** The shortest wall is 600 mm long and the thinnest is
    # 70 mm, so no wall band covers less than their product - and a sheet read
    # as a picture is full of components smaller than that: every letter, every
    # hatch dot, every compression artefact. Each one was being padded,
    # thinned, contoured and measured to be thrown away. Computed from the
    # calibrated scale, so it means the same on any sheet at any scale.
    thinnest_px = max(
        2.0, scale.px_from_mm(number(settings, "wall.min_thickness_mm", 70.0))
    )
    smallest_band_px = max(
        _SMALLEST_COMPONENT_PX,
        scale.px_from_mm(number(settings, "wall.min_length_mm", 600.0))
        * scale.px_from_mm(number(settings, "wall.min_thickness_mm", 70.0)),
    )

    for label in range(1, count):
        x, y, width, height, area = stats[label]
        if area < smallest_band_px:
            continue
        # **Padded, and the padding is not cosmetic.** Cropped tightly to its
        # own bounding box a component touches all four borders of the crop,
        # and a thinning algorithm treats out-of-bounds as background - so
        # border pixels look like the edge of the shape and are kept. Measured
        # on a rectangular building drawn to known dimensions, every
        # centreline came out on the *outer face* of its wall instead of down
        # the middle, and every thickness read 81 mm against a drawn 230 mm.
        # One pixel of blank margin puts the shape wholly inside the crop.
        piece = np.zeros((height + 2 * _PAD_PX, width + 2 * _PAD_PX), dtype=np.uint8)
        piece[_PAD_PX:_PAD_PX + height, _PAD_PX:_PAD_PX + width] = (
            labels[y:y + height, x:x + width] == label
        ).astype(np.uint8) * 255
        offset = (x - _PAD_PX, y - _PAD_PX)

        outline = _outline_of(piece, offset, scale)
        padded_ink = np.zeros_like(piece)
        padded_ink[_PAD_PX:_PAD_PX + height, _PAD_PX:_PAD_PX + width] = ink[
            y:y + height, x:x + width
        ]
        fill_share, drawn_as = _how_the_interior_is_drawn(padded_ink, piece, settings)

        # **A component's bounding box is not its size, and thinning pays for
        # the box.** The walls of a building are one connected network, so its
        # component sprawls: measured on one real sheet, two components had
        # boxes of 7.9 and 8.7 megapixels on an 8.7 megapixel image, and the
        # boxes of all its components added to **18.7 megapixels on an 8.7
        # megapixel sheet**. Thinning is iterative and scans the whole box on
        # every pass, so it took **14.1 of the 15 seconds** that stage took.
        #
        # So a box larger than the budget is thinned at a reduced resolution.
        # **The thickness does not go with it**: that is measured from the
        # full-resolution distance transform at the centreline's own pixels, so
        # it is unchanged. Only the *path* down the middle is traced coarsely,
        # and it is simplified to ``simplify_mm`` afterwards anyway - at 1:100
        # a divisor of two moves a centreline by at most 17 mm against a 30 mm
        # simplification tolerance.
        divisor = _thinning_divisor(piece.shape, thinnest_px, settings)
        to_thin = piece
        if divisor > 1:
            small = cv2.resize(
                piece,
                (max(1, piece.shape[1] // divisor), max(1, piece.shape[0] // divisor)),
                interpolation=cv2.INTER_AREA,
            )
            # Anything partly covered stays ink, so a band never breaks apart
            # on the way down and a wall is not severed by the resizing.
            to_thin = ((small > 0).astype(np.uint8)) * 255

        try:
            skeleton = cv2.ximgproc.thinning(to_thin)
        except Exception:
            skeleton = _thin_without_opencv_contrib(to_thin)
        if skeleton is None:
            continue

        traced = _trace_skeleton(skeleton)
        if divisor > 1:
            traced = [run * divisor for run in traced]
        # **A run that stops at a doorway is not a spur.** Spur pruning exists
        # to remove the whiskers thinning grows off a rounded corner, and it
        # tests for a short run with an end no other run reaches. But the
        # breaks punched out before closing deliberately create exactly that:
        # every wall either side of a door now ends free, at the door. Left
        # alone, pruning ate the very pieces the breaks had just revealed - and
        # a wall piece between two doorways is free at both ends.
        traced = _prune_spurs(
            traced, scale, settings, _ends_at_an_opening(openings_mask, offset, scale, settings)
        )
        traced = _join_runs_that_carry_on(traced, scale, settings)

        # **Collected, not built yet.** A wall of a building is one thing, but
        # on a sheet read as a picture it is traced as several - and the pieces
        # land in different connected components, because the band is severed
        # wherever the picture's ink thinned. Building each piece into a wall
        # here would put every one of them through the length floor first, and
        # a 0.5 m piece of a 14.7 m wall does not survive that. So the runs of
        # every component are gathered, put back together, and only then judged.
        for points in traced:
            if len(points) >= 2:
                gathered.append(
                    {
                        "points": points + np.array(offset, dtype=points.dtype),
                        "fill_share": fill_share,
                        "drawn_as": drawn_as,
                    }
                )

    for run in _merge_collinear_runs(gathered, distance, scale, settings):
        points = run["points"]
        simplified = cv2.approxPolyDP(
            points.reshape(-1, 1, 2).astype(np.int32), simplify_px, False
        ).reshape(-1, 2)
        if len(simplified) < 2:
            continue

        thickness_mm = run.get("thickness_mm")
        if thickness_mm is None:
            thickness_mm = scale.mm_from_px(
                _thickness_along(distance, points, (0, 0)) * 2.0
            )
        # **A wall's thickness is the distance between its two drawn faces.**
        # The band is only a stand-in for that, and on a sheet read as a
        # picture it is a poor one: closing with a kernel the width of the
        # thickest wall joins the wall to whatever ink is drawn against it -
        # a fitting, a hatch, a run of joinery - and the band comes out wider
        # than the wall. Measured on one such sheet, eleven of its longest
        # walls were thrown away as "too thick" at 322 to 779 mm, and among
        # them were its 29.6 m, 28.5 m and 17.7 m runs. The faces were paired
        # before any of that (``breaks.py``) and they measure the real
        # thickness, so where a run lies along a pair, the pair's figure is
        # used and the band's is set aside.
        from_faces = _thickness_from_the_faces(run, face_pairs, scale, settings)
        if from_faces:
            thickness_mm = from_faces
        # **A wall is two faces, so a line with no twin is not a wall.** This
        # is the rule that clears the stray lines running off into the paper:
        # a page border, a grid tick, an extension line and a roof overhang are
        # each drawn once, and none of them has a second face a wall's
        # thickness away. Everything the building is built from does. Measured
        # on one sheet read as a picture, only 5 of its 28 traced runs lay on a
        # paired face - the other 23 were the sheet border and blobs the
        # closing had joined together.
        elif setting(settings, "wall.require_paired_faces", True) and face_pairs:
            continue
        if not (
            number(settings, "wall.min_thickness_mm", 70.0)
            <= thickness_mm
            <= number(settings, "wall.max_thickness_mm", 320.0)
        ):
            continue

        wall = _wall_from_points(
            simplified, (0, 0), scale, thickness_mm, sheet_name, source,
            run["fill_share"], run["drawn_as"], len(walls) + 1,
        )
        if wall is None:
            continue
        if wall.length_mm < shortest_mm:
            continue
        # A wall is longer than it is thick. Two lines 230 mm apart running
        # together for 230 mm are a square, and a square is a column, a symbol
        # or a fragment - never a wall. This holds at any scale and on any size
        # of building, which is what makes it worth having beside a length floor.
        if wall.length_mm < slenderness * wall.thickness_mm:
            continue
        walls.append(wall)

    return walls, count - 1


def _thickness_from_the_faces(run: dict, face_pairs, scale, settings: dict):
    """The thickness of the paired faces this run lies along, where there is one.

    Matched the same way a gap is matched to a wall: the same direction, the
    same line within a lateral tolerance on the paper, and runs that actually
    overlap. Returns None where no pair covers this run, so the band's own
    measurement stands.
    """
    if not face_pairs or "axis" not in run:
        return None
    if not setting(settings, "wall.thickness_from_the_faces", True):
        return None
    lateral = scale.px_from_pt(
        number(settings, "wall.collinear_lateral_tolerance_pt", 3.5)
    )
    # **The spacing a twin has to sit at.** An Australian residential wall runs
    # from a 90 mm stud partition to a 290 mm cavity wall, so a second line
    # closer than 90 mm is a lining, a hatch boundary or the other side of one
    # plotted stroke, and one further than 300 mm is a different wall with a
    # room between them.
    twin_min = number(settings, "wall.twin_min_mm", 90.0)
    twin_max = number(settings, "wall.twin_max_mm", 300.0)
    best, nearest = None, None
    for pair in face_pairs:
        if pair["axis"] != run["axis"]:
            continue
        if not (twin_min <= pair["thickness_mm"] <= twin_max):
            continue
        # The pair is in points; the run is in pixels of the rendered page.
        position = scale.px_from_pt(pair["position"] - _origin_along(scale, run["axis"]))
        if abs(position - run["position"]) > lateral:
            continue
        start = scale.px_from_pt(pair["start"] - _origin_across(scale, run["axis"]))
        end = scale.px_from_pt(pair["end"] - _origin_across(scale, run["axis"]))
        overlap = min(end, run["end"]) - max(start, run["start"])
        if overlap <= 0:
            continue
        if best is None or overlap > best:
            best, nearest = overlap, pair["thickness_mm"]
    return nearest


def _origin_along(scale, axis: str) -> float:
    """The page origin on the axis a run's position is measured across."""
    return scale.origin[1] if axis == "h" else scale.origin[0]


def _origin_across(scale, axis: str) -> float:
    """The page origin on the axis a run's length is measured along."""
    return scale.origin[0] if axis == "h" else scale.origin[1]


def _merge_collinear_runs(gathered: list, distance, scale, settings: dict) -> list:
    """Pieces of one wall, traced separately, put back into one run.

    **A wall is one thing; a picture traces it as several.** On a sheet whose
    drawing is an embedded image the band is severed wherever the ink thinned,
    wherever a fitting was drawn against the wall and wherever the picture was
    compressed - so a 14.7 m external wall arrives as a dozen pieces, and those
    pieces land in *different connected components*, which is why nothing
    downstream could put them together. Measured on one such sheet: the face
    pairing found the real walls as 416-point runs while the tracing returned
    13 to 22 point fragments, so the gaps read off those faces had no wall long
    enough to be written onto - 35 real gaps found and four written.

    Two pieces are the same wall when they run the same way, sit on the same
    line, and measure the same thickness. The lateral tolerance is stated on
    the **paper** rather than in millimetres of building, because what is being
    allowed for is how far a skeleton wanders inside a band whose width varies -
    a property of the tracing, not of the building.

    A piece that bends is left alone. It is already more than one straight run,
    and joining it to anything would invent a corner.
    """
    import numpy as np

    if not gathered or not setting(settings, "wall.merge_collinear_runs", True):
        return gathered

    lateral = scale.px_from_pt(
        number(settings, "wall.collinear_lateral_tolerance_pt", 3.5)
    )
    widest_gap = scale.px_from_mm(number(settings, "wall.collinear_max_gap_mm", 6000.0))
    agreement = number(settings, "wall.thickness_agreement_share", 0.5)

    straight, bent = [], []
    for run in gathered:
        points = run["points"]
        spread_x = float(points[:, 0].max() - points[:, 0].min())
        spread_y = float(points[:, 1].max() - points[:, 1].min())
        if spread_y <= lateral and spread_x > spread_y:
            run["axis"], run["position"] = "h", float(points[:, 1].mean())
            run["start"], run["end"] = float(points[:, 0].min()), float(points[:, 0].max())
        elif spread_x <= lateral and spread_y > spread_x:
            run["axis"], run["position"] = "v", float(points[:, 0].mean())
            run["start"], run["end"] = float(points[:, 1].min()), float(points[:, 1].max())
        else:
            bent.append(run)
            continue
        run["thickness_mm"] = scale.mm_from_px(
            _thickness_along(distance, points, (0, 0)) * 2.0
        )
        straight.append(run)

    merged = []
    for axis in ("h", "v"):
        along = sorted(
            (r for r in straight if r["axis"] == axis),
            key=lambda r: (r["position"], r["start"]),
        )
        band = []
        for run in along:
            if band and run["position"] - band[-1]["position"] <= lateral:
                band.append(run)
                continue
            merged.extend(_join_along(band, widest_gap, agreement, axis, np))
            band = [run]
        merged.extend(_join_along(band, widest_gap, agreement, axis, np))

    if len(merged) < len(straight):
        logger.info(
            f"walls: {len(straight)} traced runs merged into {len(merged)} continuous walls"
        )
    return merged + bent


def _join_along(band: list, widest_gap: float, agreement: float, axis: str, np) -> list:
    """One band's runs, joined where they carry on and their thickness agrees."""
    if not band:
        return []
    band = sorted(band, key=lambda r: r["start"])
    joined, run = [], [band[0]]
    for piece in band[1:]:
        thickness = max(min(piece["thickness_mm"], run[-1]["thickness_mm"]), 1.0)
        carries_on = (
            piece["start"] - run[-1]["end"] <= widest_gap
            and abs(piece["thickness_mm"] - run[-1]["thickness_mm"])
            <= thickness * agreement
        )
        if carries_on:
            run.append(piece)
            continue
        joined.append(_one_run(run, axis, np))
        run = [piece]
    joined.append(_one_run(run, axis, np))
    return joined


def _one_run(pieces: list, axis: str, np) -> dict:
    """The pieces of one wall as a single straight centreline."""
    if len(pieces) == 1:
        return pieces[0]
    start = min(p["start"] for p in pieces)
    end = max(p["end"] for p in pieces)
    position = sum(p["position"] for p in pieces) / len(pieces)
    thickness = sum(p["thickness_mm"] for p in pieces) / len(pieces)
    if axis == "h":
        points = np.array([[start, position], [end, position]], dtype=np.int32)
    else:
        points = np.array([[position, start], [position, end]], dtype=np.int32)
    return {
        "points": points,
        "fill_share": max(p["fill_share"] for p in pieces),
        "drawn_as": pieces[0]["drawn_as"],
        "axis": axis,
        "position": position,
        "start": start,
        "end": end,
        "thickness_mm": thickness,
    }


def _thinning_divisor(shape, thinnest_px: float, settings: dict) -> int:
    """How far to reduce a component before thinning it, and never further.

    Two limits, and the second is what keeps it safe. The budget says how big a
    box may be before it is worth reducing at all. The floor says the thinnest
    wall the office builds must still be several pixels across after the
    reduction - below that a band is a line, and thinning a line gives a
    skeleton that wanders instead of running down the middle.
    """
    budget = number(settings, "wall.thinning_pixel_budget", 2_000_000.0)
    keep_px = number(settings, "wall.thinning_min_wall_px", 4.0)
    box = float(shape[0]) * float(shape[1])
    if budget <= 0 or box <= budget:
        return 1
    wanted = int(math.ceil(math.sqrt(box / budget)))
    allowed = int(max(1, thinnest_px // max(keep_px, 1.0)))
    return max(1, min(wanted, allowed))


def _outline_of(piece, offset, scale):
    """The band's own outline, in the page's space, as a Shapely polygon."""
    import cv2

    try:
        from shapely.geometry import Polygon
    except ImportError:
        return None
    contours, _hierarchy = cv2.findContours(piece, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    biggest = max(contours, key=cv2.contourArea)
    if len(biggest) < 4:
        return None
    points = [
        scale.pixel_to_point(float(p[0][0] + offset[0]), float(p[0][1] + offset[1]))
        for p in biggest
    ]
    try:
        polygon = Polygon(points)
        return polygon if polygon.is_valid else polygon.buffer(0)
    except Exception:
        return None


def _how_the_interior_is_drawn(ink_piece, piece, settings):
    """Whether this band is blacked in, hatched at 45 degrees, or just outlined.

    Australian plans do all three, and which one an office uses is a habit
    rather than a fact about the wall - so this is **recorded, never required**.
    Requiring a fill would delete every wall on a plan set that outlines its
    walls, which is most of them. It raises the confidence where it is present
    and says so on the record.

    Returns ``(fill_share, how)`` where ``how`` is "solid", "hatched" or
    "outline".

    **Measured against the ink, never against the closed band.** Comparing the
    closed image with a band derived from that same closed image reports every
    wall on every sheet as 100% filled, which is what it did at first: the
    answer was one by construction and said nothing about the drawing.

    **And a solid fill is not hatching**, which the first version could not
    tell: a diagonal kernel fits inside a blacked-in wall as happily as inside
    a hatched one, so 45 of 66 outlined walls came back "hatched". Hatching is
    diagonal strokes with paper between them - so it needs the diagonals to
    carry most of the ink *and* the band not to be solid.
    """
    import cv2
    import numpy as np

    inside = piece > 0
    if not inside.any():
        return 0.0, "outline"
    ink_inside = float((ink_piece[inside] > 0).mean())

    solid_from = number(settings, "wall.solid_fill_share", 0.85)
    if ink_inside >= solid_from:
        return ink_inside, "solid"

    diagonal_share = 0.0
    try:
        for angle in setting(settings, "wall.hatch_angle_degrees", [45.0, 135.0]) or []:
            kernel = _line_kernel(float(angle), _HATCH_KERNEL_LENGTH_PX)
            opened = cv2.morphologyEx(ink_piece, cv2.MORPH_OPEN, kernel)
            diagonal_share = max(diagonal_share, float((opened[inside] > 0).mean()))
    except Exception:
        return ink_inside, "outline"

    # Hatching is what the wall is drawn *with*, so the diagonals have to be
    # most of the ink in the band - not merely present in it.
    carries = number(settings, "wall.hatch_share_of_ink", 0.5)
    if ink_inside > 0 and diagonal_share / ink_inside >= carries and diagonal_share > 0.05:
        return ink_inside, "hatched"
    return ink_inside, "outline"


def _line_kernel(angle_degrees: float, length: int):
    """A one-pixel-wide line at an angle, for finding hatching by opening."""
    import cv2
    import numpy as np

    kernel = np.zeros((length, length), dtype=np.uint8)
    centre = length // 2
    radians = math.radians(angle_degrees)
    for step in range(-centre, centre + 1):
        x = int(round(centre + step * math.cos(radians)))
        y = int(round(centre + step * math.sin(radians)))
        if 0 <= x < length and 0 <= y < length:
            kernel[y, x] = 1
    return kernel


def _thickness_along(distance, points, offset) -> float:
    """The band's half-thickness along a centreline, as its median.

    A median rather than a mean, because a centreline running through a
    junction picks up the far larger distance value there - a corner where two
    230 mm walls meet is 325 mm across the diagonal - and one junction would
    otherwise put the whole wall outside every thickness the office builds.
    """
    import numpy as np

    values = []
    height, width = distance.shape[:2]
    for x, y in points:
        page_x, page_y = int(x + offset[0]), int(y + offset[1])
        if 0 <= page_y < height and 0 <= page_x < width:
            values.append(float(distance[page_y, page_x]))
    if not values:
        return 0.0
    return float(np.median(values))


def _wall_from_points(points, offset, scale, thickness_mm, sheet_name, source, fill_share, drawn_as, index):
    """One wall record, with its centreline and outline as Shapely geometry."""
    try:
        from shapely.geometry import LineString
    except ImportError:
        logger.warning("Shapely is not installed, so wall geometry cannot be built")
        return None

    coords = [
        scale.pixel_to_point(float(x + offset[0]), float(y + offset[1])) for x, y in points
    ]
    try:
        centreline = LineString(coords)
    except Exception:
        return None
    if centreline.length <= 0:
        return None

    length_mm = centreline.length * scale.mm_per_point
    # The outline is the centreline given its measured thickness - one
    # geometry, not two. Drawing the outline separately would be a second
    # truth about the same wall, which is what Critical Rule 2 forbids.
    half = (thickness_mm / scale.mm_per_point) / 2.0
    try:
        outline = centreline.buffer(half, cap_style=2, join_style=2)
    except Exception:
        outline = None

    x0, y0, x1, y1 = centreline.bounds
    # A wall the office blacked in or hatched said twice that it is a wall;
    # one drawn as an outline said it once.
    confidence = 0.6 + (0.15 if drawn_as in ("solid", "hatched") else 0.0)
    if source == "page_image":
        confidence -= 0.1

    return Wall(
        element_id=f"W-{index:03d}",
        centreline=centreline,
        outline=outline,
        thickness_mm=thickness_mm,
        length_mm=length_mm,
        source_sheet=sheet_name,
        source_bbox=[x0 - half, y0 - half, x1 + half, y1 + half],
        extraction_method=(
            "skeletonised wall band from the drawing's own line work"
            if source == "vector_paths"
            else "skeletonised wall band from the sheet read as a picture"
        ),
        confidence=max(0.1, min(0.95, confidence)),
        fill_share=fill_share,
        drawn_as=drawn_as,
        note=(
            "Measured from the page image rather than the drawing's own geometry."
            if source == "page_image"
            else ""
        ),
    )


def _trace_skeleton(skeleton) -> list:
    """A one-pixel skeleton turned into ordered runs of points.

    A skeleton is a picture, and a wall is a line - so the picture has to be
    walked. Junction pixels are lifted out first, because a T is where three
    walls meet and running through it would join two different walls into one
    polyline. What is left is a set of simple runs, each walked from one end.
    """
    import cv2
    import numpy as np

    if skeleton is None or not skeleton.any():
        return []
    on = (skeleton > 0).astype(np.uint8)
    neighbours = cv2.filter2D(on, cv2.CV_8U, np.ones((3, 3), dtype=np.uint8)) - on
    runs_mask = (on > 0) & (neighbours < 3)
    if not runs_mask.any():
        return []

    count, labels = cv2.connectedComponents(runs_mask.astype(np.uint8), 8)
    if count < 2:
        return []

    # **One scan of the image, not one per run.** ``np.nonzero(labels ==
    # label)`` reads the whole component for every branch in it, so a band
    # holding 400 runs was read 400 times over - measured, 4.7 of the 12.9
    # seconds this stage took, and it grows with the square of how well the
    # tracing works. The pixels are read once and sorted into their runs.
    ys, xs = np.nonzero(runs_mask)
    if len(ys) < 2:
        return []
    of_run = labels[ys, xs]
    order = np.argsort(of_run, kind="stable")
    ys, xs, of_run = ys[order], xs[order], of_run[order]
    starts = np.searchsorted(of_run, np.arange(1, count), side="left")
    ends = np.searchsorted(of_run, np.arange(1, count), side="right")

    traced = []
    for first, last in zip(starts, ends):
        if last - first < 2:
            continue
        pixels = set(zip(ys[first:last].tolist(), xs[first:last].tolist()))
        ordered = _walk(pixels)
        if len(ordered) >= 2:
            traced.append(np.array([(x, y) for y, x in ordered], dtype=np.int32))
    return traced


def _ends_at_an_opening(openings_mask, offset, scale, settings):
    """A test for whether a traced run's end lies where an opening was punched.

    Returns a callable, or None where nothing was punched. The allowance is the
    same clearance the punching used, so an end anywhere inside the box that
    made the gap counts as being at the opening rather than free.
    """
    if openings_mask is None:
        return None
    reach = int(round(max(2.0, scale.px_from_mm(
        number(settings, "breaks.clearance_mm", 40.0)
    ))))
    height, width = openings_mask.shape[:2]

    def at_an_opening(point) -> bool:
        x = int(round(float(point[0]) + offset[0]))
        y = int(round(float(point[1]) + offset[1]))
        low_y, high_y = max(0, y - reach), min(height, y + reach + 1)
        low_x, high_x = max(0, x - reach), min(width, x + reach + 1)
        if low_y >= high_y or low_x >= high_x:
            return False
        return bool(openings_mask[low_y:high_y, low_x:high_x].any())

    return at_an_opening


def _prune_spurs(runs: list, scale, settings: dict, at_an_opening=None) -> list:
    """Drops the short dead-end stubs that closing leaves behind.

    Closing a wall's two faces into a solid band rounds its corners, and
    thinning a rounded corner grows a little whisker off the centreline. Each
    whisker is a run with one free end, and left in they are counted as walls
    and they break the wall they hang off into pieces.

    A run is a spur when it is short **and** has at least one end that no other
    run reaches. A short run joined at both ends is a nib, a pier or the stub
    between two doorways, and those are real.
    """
    if not runs:
        return runs
    import numpy as np

    longest_spur_px = scale.px_from_mm(number(settings, "wall.spur_mm", 600.0))
    snap = max(2.0, scale.px_from_mm(number(settings, "wall.min_thickness_mm", 70.0)))

    kept = list(runs)
    for _pass in range(3):
        ends = {}
        for index, run in enumerate(kept):
            for end in (run[0], run[-1]):
                ends.setdefault(_node(end, snap), []).append(index)
        dropped = set()
        for index, run in enumerate(kept):
            if _run_length(run) > longest_spur_px:
                continue
            free = 0
            for end in (run[0], run[-1]):
                if len(ends.get(_node(end, snap), [])) >= 2:
                    continue
                if at_an_opening is not None and at_an_opening(end):
                    continue
                free += 1
            if free:
                dropped.add(index)
        if not dropped:
            break
        kept = [run for index, run in enumerate(kept) if index not in dropped]
    return kept


def _join_runs_that_carry_on(runs: list, scale, settings: dict) -> list:
    """Puts back together a wall that a junction cut in half.

    A skeleton is split at every junction, because running through a T would
    join two different walls into one polyline. But a wall does not stop at a
    T - the partition stops, the wall carries on - so the two pieces either
    side of it are one wall and must be reported as one, or a 12 m external
    wall arrives as five stubs.

    Two runs carry on from each other when they meet at a point and leave it in
    nearly opposite directions. At a corner they leave at right angles and are
    left as two, which is right: a wall running north and a wall running east
    are two walls.
    """
    if len(runs) < 2:
        return runs
    import numpy as np

    tolerance = number(settings, "wall.carry_on_angle_degrees", 25.0)
    snap = max(2.0, scale.px_from_mm(number(settings, "wall.min_thickness_mm", 70.0)))

    pieces = [list(run) for run in runs]
    joined = True
    while joined:
        joined = False
        ends = {}
        for index, piece in enumerate(pieces):
            if piece is None:
                continue
            ends.setdefault(_node(piece[0], snap), []).append((index, 0))
            ends.setdefault(_node(piece[-1], snap), []).append((index, -1))

        for meeting in ends.values():
            live = [(i, e) for i, e in meeting if pieces[i] is not None]
            if len(live) != 2:
                # Three or more runs meeting is a T or a cross. Which two carry
                # on cannot be decided from a count, and joining the wrong two
                # would run a wall round a corner - so nothing is joined here.
                continue
            (first_index, first_end), (second_index, second_end) = live
            if first_index == second_index:
                continue
            if not _carries_on(pieces[first_index], first_end, pieces[second_index], second_end,
                               tolerance):
                continue
            one = pieces[first_index] if first_end == -1 else list(reversed(pieces[first_index]))
            two = pieces[second_index] if second_end == 0 else list(reversed(pieces[second_index]))
            pieces[first_index] = one + two
            pieces[second_index] = None
            joined = True
            break

    return [np.array(piece, dtype=np.int32) for piece in pieces if piece is not None]


def _node(point, snap: float):
    return (round(float(point[0]) / snap), round(float(point[1]) / snap))


def _run_length(run) -> float:
    return sum(
        math.hypot(float(run[i + 1][0] - run[i][0]), float(run[i + 1][1] - run[i][1]))
        for i in range(len(run) - 1)
    )


def _carries_on(first, first_end, second, second_end, tolerance_degrees: float) -> bool:
    """Whether two runs meeting at a point leave it in opposite directions."""
    into = _direction_at(first, first_end)
    out_of = _direction_at(second, second_end)
    if into is None or out_of is None:
        return False
    # Both directions point away from the meeting, so carrying on means they
    # are opposite - a dot product near -1.
    dot = into[0] * out_of[0] + into[1] * out_of[1]
    dot = max(-1.0, min(1.0, dot))
    return math.degrees(math.acos(dot)) >= 180.0 - tolerance_degrees


def _direction_at(run, end):
    """A unit vector pointing away from one end of a run, along the run."""
    look = min(len(run) - 1, 5)
    if look < 1:
        return None
    if end == 0:
        tip, inner = run[0], run[look]
    else:
        tip, inner = run[-1], run[-1 - look]
    dx, dy = float(tip[0] - inner[0]), float(tip[1] - inner[1])
    size = math.hypot(dx, dy)
    if size <= 0:
        return None
    return (dx / size, dy / size)


def _walk(pixels: set) -> list:
    """Puts one run's pixels in order, from one end to the other."""
    def neighbours_of(pixel):
        y, x = pixel
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dy == 0 and dx == 0:
                    continue
                other = (y + dy, x + dx)
                if other in pixels:
                    yield other

    start = None
    for pixel in pixels:
        if sum(1 for _ in neighbours_of(pixel)) <= 1:
            start = pixel
            break
    if start is None:
        # A closed loop has no end to start from - a wall drawn right round a
        # lightwell or a chimney. Any pixel will do.
        start = next(iter(pixels))

    ordered = [start]
    seen = {start}
    current = start
    while True:
        step = None
        for other in neighbours_of(current):
            if other not in seen:
                step = other
                break
        if step is None:
            break
        ordered.append(step)
        seen.add(step)
        current = step
    return ordered


def _thin_without_opencv_contrib(piece):
    """Zhang-Suen thinning, for a build of OpenCV that has no ``ximgproc``.

    ``opencv-python-headless`` carries ``ximgproc`` on some platforms and not
    on others, and which one a server has is a fact about the server rather
    than about the drawing. So the same skeleton is available either way rather
    than the sheet coming back with no walls on a machine that happens to have
    the smaller build.
    """
    import numpy as np

    image = (piece > 0).astype(np.uint8)
    changed = True
    passes = 0
    while changed and passes < 100:
        changed = False
        passes += 1
        for step in (0, 1):
            padded = np.pad(image, 1, mode="constant")
            p2 = padded[:-2, 1:-1]
            p3 = padded[:-2, 2:]
            p4 = padded[1:-1, 2:]
            p5 = padded[2:, 2:]
            p6 = padded[2:, 1:-1]
            p7 = padded[2:, :-2]
            p8 = padded[1:-1, :-2]
            p9 = padded[:-2, :-2]
            neighbours = p2 + p3 + p4 + p5 + p6 + p7 + p8 + p9
            sequence = [p2, p3, p4, p5, p6, p7, p8, p9, p2]
            transitions = sum(
                ((sequence[i] == 0) & (sequence[i + 1] == 1)).astype(np.uint8)
                for i in range(8)
            )
            if step == 0:
                first, second = p2 * p4 * p6, p4 * p6 * p8
            else:
                first, second = p2 * p4 * p8, p2 * p6 * p8
            remove = (
                (image == 1)
                & (neighbours >= 2)
                & (neighbours <= 6)
                & (transitions == 1)
                & (first == 0)
                & (second == 0)
            )
            if remove.any():
                image[remove] = 0
                changed = True
    return (image * 255).astype(np.uint8)
