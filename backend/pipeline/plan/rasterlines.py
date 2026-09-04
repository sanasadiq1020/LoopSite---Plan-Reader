"""Straight lines recovered from a sheet whose drawing is stored as a picture.

Not every plan set is vector. One of the supplied sets places its drawing on
the page as **embedded images**, with only text and a few frame lines as real
vector geometry: 400 drawing items against 16,117 on a vector set. Reading its
wall lines from ``page.get_drawings()`` therefore returns almost nothing, and
no amount of tuning the pairing changes that — the lines are pixels.

So when a sheet's own geometry does not trace a building, its lines are
recovered from the rendered page instead and handed to exactly the same
face-merging and pairing code. **The sheet's own line work is always tried
first** — it is exact, and nothing measured off pixels can beat it — so this
only ever runs where the lines are genuinely not in the PDF as lines. The rest
of the pipeline neither knows nor cares which source a segment came from,
except that every wall records it (``line_source``).

**Why a Line Segment Detector rather than morphology or a Hough transform.**
All three answer a different question:

*   A **Hough transform** answers "is there evidence of a line here?" On a
    floor plan — dense with hatching, furniture, door swings and lettering —
    that returns thousands of fragments at every angle, and the real walls are
    not distinguishable among them.
*   **Morphological opening** with a long kernel answers "is there a
    continuous run of dark pixels this long?" That is closer, and it is what
    this module used to do, but the answer it gives is a *blob*: the kernel
    length is a hard floor written into the operator, a line broken by a
    crossing is cut into pieces before anything can join them, and the
    position it reports is the centre of a bounding box rather than of a line.
    Every one of those is a threshold that has to be guessed per drawing.
*   **LSD** answers "where does the image change from light to dark along a
    straight front, and how confident is that?" It grows each line region from
    the image's own gradients and validates it against how often such a region
    would arise by chance, so it needs no length, no threshold and no kernel:
    it is self-tuning by construction. That is what makes it the right
    instrument for a reader that is handed drawings it has never seen.

**LSD finds edges, not lines**, and that difference is the one thing every
caller has to know about. A plotted line half a millimetre wide has two sides,
and LSD returns one segment down each of them. A drawn wall face therefore
arrives as a *pair* of segments — so the two sides of one stroke are put back
together here (``merge_stroke_edges``), before anything downstream sees them.

Every threshold this module uses lives in ``config/wall_config.json`` under
``raster_lines``. The values named in ``_DEFAULTS`` below exist only so that a
missing or unreadable config degrades to a logged, working run rather than
taking the upload down (Critical Rule 6); the file on disk is the source of
truth.
"""

import math

import fitz

from app.logging_setup import get_logger

logger = get_logger()

# Where the office's own values are read from. ``config/wall_config.json`` is
# merged over the "walls" section of ``plan_reading.json``, so a block given
# there arrives as config["walls"]["raster_lines"]; the older top-level block
# is still read so an existing file keeps working.
_SETTINGS_SECTION = "raster_lines"

# Fallbacks only — see the module docstring. Each one is documented beside its
# real entry in config/wall_config.json.
_DEFAULTS = {
    "render_dpi": 200,
    "straightness_tolerance_degrees": 2.0,
    "min_segment_length_mm": 300.0,
    "max_line_width_mm": 320.0,
    "prejoin_lsd_pieces": True,
    "lsd_prejoin_gap_pt": 3.0,
    "lsd_prejoin_offset_pt": 1.8,
    "merge_stroke_edges": True,
    "stroke_edge_width_factor": 2.0,
    "stroke_edge_overlap_share": 0.5,
}


def _settings(config: dict) -> dict:
    """The raster-line settings, with the office's own file laid over them."""
    settings = dict(_DEFAULTS)
    try:
        settings.update(config.get(_SETTINGS_SECTION) or {})
        settings.update((config.get("walls") or {}).get(_SETTINGS_SECTION) or {})
    except AttributeError:
        logger.warning("raster line settings are not a mapping; using the defaults")
    return settings


def _number(settings: dict, name: str) -> float:
    """One setting as a number, falling back rather than raising."""
    try:
        return float(settings.get(name, _DEFAULTS[name]))
    except (TypeError, ValueError):
        logger.warning(f"raster line setting {name!r} is not a number; using the default")
        return float(_DEFAULTS[name])


def _nothing() -> dict:
    """The same shape ``layout.extract_rulings`` returns, holding nothing.

    Returned wherever this reader cannot run at all, so the caller keeps the
    reading its vector geometry gave it instead of failing (Critical Rule 6).
    """
    return {
        "h": [], "v": [],
        "h_widths": [], "v_widths": [],
        "h_dashed": [], "v_dashed": [],
    }


def _detector():
    """OpenCV's Line Segment Detector, or None where this build has none.

    LSD was taken out of OpenCV in 4.1 over a question about the licence of
    the original implementation and restored in 4.8. A deployment may be
    running either, and which one it is is a fact about the server rather than
    about the drawing — so a build without it says so once and the sheet keeps
    the reading its own geometry gave it, rather than the run failing.
    """
    try:
        import cv2
    except ImportError:
        logger.warning("OpenCV is not installed, so sheets drawn as pictures cannot be measured")
        return None
    try:
        return cv2.createLineSegmentDetector()
    except Exception as e:
        logger.warning(
            "this OpenCV build has no Line Segment Detector "
            f"(version {getattr(cv2, '__version__', 'unknown')}): {e}"
        )
        return None


def _rendered_page(page, dpi: float):
    """The page as a greyscale image, with the transform back to PDF points.

    **The same transform the vector geometry goes through.** ``get_pixmap``
    renders the page *as it displays*, which is the space
    ``layout._drawings_in_page_space`` turns the drawn paths into — so a sheet
    carrying its own 90 degree rotation gives text, vector lines and these
    pixels all in one coordinate system, and nothing downstream has to know a
    page was rotated (see CLAUDE.md 4P for what that defect looked like).

    Returned with the origin as well as the scale, because a page whose
    mediabox does not start at the corner of the paper renders from
    ``page.rect``'s own origin, and dropping that offset would put every
    recovered line the same distance away from the drawing it came from.
    """
    import numpy as np

    scale = dpi / 72.0
    pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csGRAY)
    image = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
        pixmap.height, pixmap.width
    )
    # LSD writes nothing, but OpenCV wants an array it owns rather than a
    # read-only view onto the pixmap's buffer.
    return np.ascontiguousarray(image), scale, (page.rect.x0, page.rect.y0)


def _axis_of(x0: float, y0: float, x1: float, y1: float, tolerance_degrees: float):
    """"h", "v", or None for a segment drawn at neither.

    **A plan is drawn on the axes.** Anything at an angle is a door swing, a
    roof diagonal, a stair nosing, a hatch stroke, a note's leader or a piece
    of lettering — and none of those is a wall face, which is found by pairing
    two faces that run *parallel*. So an off-axis segment is not merely
    unusable here, it is evidence of something this reader is not looking for.

    The tolerance is what a scanned sheet's own skew and the sub-pixel jitter
    of an edge detector need; it is not a licence to accept a diagonal.
    """
    dx, dy = abs(x1 - x0), abs(y1 - y0)
    if dx == 0 and dy == 0:
        return None
    # The angle away from the nearer axis, so one tolerance covers both.
    if dx >= dy:
        return "h" if math.degrees(math.atan2(dy, dx)) <= tolerance_degrees else None
    return "v" if math.degrees(math.atan2(dx, dy)) <= tolerance_degrees else None


def _merge_stroke_edges(
    segments: list, factor: float, overlap_share: float, thinnest_wall_pt: float
) -> list:
    """Puts the two sides of one plotted stroke back together as one line.

    **This is a property of edge detection, not of any drawing.** LSD finds
    where the image changes from light to dark along a straight front, and a
    plotted line has two such fronts — one down each side of the stroke. So
    every drawn wall face arrives here as two segments a stroke's width apart,
    and left alone each of them pairs with the far face of the wall
    separately: one wall is then reported two, three or four times over, each
    copy with a thickness half a stroke out — which is enough, on a sheet at
    1:100, to put a 90 mm wall outside the thickness the office builds.

    Two segments are the two sides of one stroke when they run together and
    sit closer than the stroke the detector itself measured for them. That is
    self-tuning in the same way the detector is: a heavily plotted sheet and a
    finely plotted one each supply their own figure.

    **The factor is needed because the width LSD reports is not the distance
    between the two sides it reports.** Measured on plain drawn strokes, the
    separation is 1.3 to 1.7 times the width — a 1-pixel stroke comes back as
    two segments 2.1 pixels apart with a width of 1.25, and a 3-pixel stroke
    as two 5.0 apart with a width of 3.75. That ratio is a property of how the
    detector places an edge on a gradient, not of any drawing, which is why it
    can be a fixed multiple at all.

    **And it may never merge what could be a wall.** Whatever the widths say,
    two lines as far apart as the thinnest wall the office builds are two
    faces, not two sides of one stroke — so that is a hard ceiling. At 1:100 a
    90 mm wall's faces are 7 pixels apart at 200 DPI, against 2 to 5 for the
    sides of the line drawing one of them, so the two cases are not close.

    The width kept is the distance between the two sides, which is the run's
    own measured thickness — the same quantity the PDF states for a vector
    line, and the only one a picture can supply.

    Each entry is (position, start, end, width), in points.
    """
    if len(segments) < 2:
        return segments

    ordered = sorted(range(len(segments)), key=lambda i: segments[i][0])
    # The furthest apart any two segments on this sheet could be and still be
    # one stroke. Segments are looked at in order across the page, so once the
    # gap passes this nothing further along can pair either — which is what
    # keeps this to a handful of comparisons per segment on a sheet carrying
    # thousands of them.
    furthest = max(width for _p, _s, _e, width in segments) * factor
    if thinnest_wall_pt > 0:
        furthest = min(furthest, thinnest_wall_pt)

    taken: set = set()
    merged = []
    for place, index in enumerate(ordered):
        if index in taken:
            continue
        position, start, end, width = segments[index]
        partner = None
        for other in ordered[place + 1:]:
            other_position, other_start, other_end, other_width = segments[other]
            apart = other_position - position
            if apart > furthest:
                break
            if other in taken:
                continue
            if apart > (width + other_width) / 2.0 * factor:
                continue
            # Never merge what could be a wall: two lines as far apart as the
            # thinnest wall the office builds are two faces of one.
            if thinnest_wall_pt > 0 and apart >= thinnest_wall_pt:
                continue
            overlap = min(end, other_end) - max(start, other_start)
            shorter = min(end - start, other_end - other_start)
            if shorter <= 0 or overlap / shorter < overlap_share:
                continue
            partner = other
            break

        if partner is None:
            merged.append((position, start, end, width))
            continue
        other_position, other_start, other_end, _other_width = segments[partner]
        taken.add(index)
        taken.add(partner)
        merged.append(
            (
                (position + other_position) / 2.0,
                min(start, other_start),
                max(end, other_end),
                abs(other_position - position),
            )
        )
    return merged


def _prejoin(segments: list, gap: float, offset: float, ceiling: float) -> list:
    """Joins the pieces of one drawn edge back into a single run.

    **The detector does not return an edge; it returns pieces of one.** On a
    sheet stored as a picture, a wall face that a plan draws as one line comes
    back broken wherever the ink thins, wherever another line crosses it and
    wherever the image was compressed — with each piece sitting a fraction of
    a point off its neighbours, because an edge grown from gradients is placed
    to sub-pixel precision and the pixels disagree. Measured on one back-house
    floor plan, a single 2.9 m internal wall arrived as **nine** segments
    spread over 0.5 of a point.

    That jitter is smaller than the face-merging step downstream can see: it
    buckets by the collinear tolerance, which is deliberately held well under
    the thinnest wall the office builds and so cannot be widened to cover it.
    So the pieces landed in two or three neighbouring buckets, became two or
    three *separate* faces a few tens of millimetres apart, and then competed
    with each other for the far face of the same wall — each pairing taking
    only the stretch it covered. One 2.9 m wall came out as two or three
    stretches too short to be walls at all, and **thirty real internal walls
    on that sheet were lost this way**: the rooms they enclose had no walls on
    the marked-up sheet, and nothing said so, because a wall that was never
    formed cannot carry a reason.

    Joining them here rather than downstream is deliberate. This is a fact
    about *this reader's* output, not about drawings — the sheet's own vector
    geometry states each line once, exactly, and has no jitter to repair — so
    a tolerance wide enough for pixels must never be applied to it.

    Two rules beyond the two tolerances, both to keep a real wall safe:

    *   **The offset may never reach the thinnest wall the office builds.**
        Two lines that far apart are the two faces of a wall, and joining them
        would erase the wall rather than repair it.
    *   **A run's position is the average of its pieces weighted by length.**
        A 63-point face joined to a 13-point fragment is still where the
        63-point face is; a plain mean drags it a quarter of a wall's
        thickness, which is enough to put a 90 mm wall outside anything the
        office builds.

    Each entry is (position, start, end, width), in points.
    """
    if len(segments) < 2 or gap <= 0 or offset <= 0:
        return segments
    if ceiling > 0:
        offset = min(offset, ceiling)

    joined = []
    # Along the run, because that is the order pieces of one edge are in.
    for position, start, end, width in sorted(segments, key=lambda s: (s[1], s[0])):
        best = None
        for index, (chain_position, _s, chain_end, _w, _length) in enumerate(joined):
            # **Consecutive, and that is the whole of it.** This piece has to
            # carry on from where that run reached, within the tolerance,
            # *from either side of it*: the detector's pieces overlap by a
            # fraction of a point as often as they leave a gap. What the
            # tolerance may not do is admit a piece that starts well back
            # inside the run — that is a second line lying alongside the
            # first, and joining the two averages a wall's own two faces into
            # one line and loses the wall. Leaving that out cost eight walls
            # on one floor plan and every one of them was real.
            if abs(start - chain_end) > gap:
                continue
            # And it has to add something. A piece wholly inside a run is
            # already accounted for.
            if end <= chain_end:
                continue
            away = abs(chain_position - position)
            if away > offset:
                continue
            if best is None or away < best[1]:
                best = (index, away)
        if best is None:
            joined.append([position, start, end, width, max(end - start, 0.0)])
            continue
        run = joined[best[0]]
        length = max(end - start, 0.0)
        total = run[4] + length
        if total > 0:
            run[0] = (run[0] * run[4] + position * length) / total
            run[3] = (run[3] * run[4] + width * length) / total
        run[1] = min(run[1], start)
        run[2] = max(run[2], end)
        run[4] = total

    return [(position, start, end, width) for position, start, end, width, _l in joined]


def extract_rulings_from_image(page, config: dict, mm_per_point: float) -> dict:
    """Axis-aligned segments read from the rendered page, in PDF points.

    Returns the same shape as ``layout.extract_rulings`` so the two are
    interchangeable: ``{"h": [(y, x0, x1), ...], "v": [(x, y0, y1), ...]}``
    with ``h_widths``/``v_widths`` and ``h_dashed``/``v_dashed`` beside them,
    index for index. Downstream code cannot tell which reader produced a
    segment, and does not need to.

    ``dashed`` is **False** for every segment, and that is a statement rather
    than a default: a picture of a drawing cannot establish that a line was
    plotted dashed. The dash pattern the PDF states is not there, and the other
    way a dashed line is recognised — a run drawn in many short pieces with
    small regular gaps (``walls._how_it_was_drawn``) — cannot be told apart
    from a solid line that a crossing, a symbol or an artefact of the scan
    happened to break. Claiming a dash from pixels would set aside real walls
    as roof extents and boundaries, so nothing is claimed.
    """
    settings = _settings(config)
    if mm_per_point <= 0:
        logger.warning("no usable scale for this sheet, so its picture cannot be measured")
        return _nothing()

    detector = _detector()
    if detector is None:
        return _nothing()

    dpi = _number(settings, "render_dpi")
    try:
        image, scale, (origin_x, origin_y) = _rendered_page(page, dpi)
    except Exception as e:
        logger.exception(f"could not render the page for line detection: {e}")
        return _nothing()

    try:
        detected = detector.detect(image)
    except Exception as e:
        logger.exception(f"the line segment detector failed on this page: {e}")
        return _nothing()

    lines = detected[0] if detected else None
    # LSD reports the width of the region it grew each line from, which is the
    # stroke it found. Some builds return it, some do not; where it is missing
    # a segment is treated as one pixel wide, which is the thinnest thing this
    # render can hold.
    widths = detected[1] if len(detected) > 1 else None
    if lines is None or len(lines) == 0:
        logger.info("the line segment detector found nothing on this page")
        return _nothing()

    tolerance_degrees = _number(settings, "straightness_tolerance_degrees")
    min_length_pt = _number(settings, "min_segment_length_mm") / mm_per_point
    max_width_pt = _number(settings, "max_line_width_mm") / mm_per_point

    by_axis = {"h": [], "v": []}
    off_axis = short = 0
    for index, line in enumerate(lines):
        try:
            x0, y0, x1, y1 = (float(v) for v in line.reshape(4))
        except Exception:
            continue
        axis = _axis_of(x0, y0, x1, y1, tolerance_degrees)
        if axis is None:
            off_axis += 1
            continue

        try:
            width_px = float(widths[index].item()) if widths is not None else 1.0
        except Exception:
            width_px = 1.0

        if axis == "h":
            position = ((y0 + y1) / 2.0) / scale + origin_y
            low, high = sorted((x0 / scale + origin_x, x1 / scale + origin_x))
        else:
            position = ((x0 + x1) / 2.0) / scale + origin_x
            low, high = sorted((y0 / scale + origin_y, y1 / scale + origin_y))

        # **Shorter than the lettering printed on the sheet.** At drawing
        # scale a stroke this short is a letter, a dimension tick, an
        # arrowhead barb, a hatch stroke or the fragment LSD returns where one
        # line crosses another. A piece of a real wall face shorter than this
        # is not lost: the pieces that remain are joined into one face by
        # ``walls._merge_faces``, which bridges gaps as wide as an opening.
        if (high - low) < min_length_pt:
            short += 1
            continue

        by_axis[axis].append((position, low, high, width_px / scale))

    # The thinnest wall the office builds, read from the wall settings rather
    # than restated here: two lines that far apart are the two faces of a
    # wall, and neither step below may ever join those into one line.
    try:
        thinnest_wall_pt = float(
            (config.get("walls") or {}).get("min_wall_thickness_mm")
            or (config.get("walls") or {}).get("min_thickness_mm")
            or 0.0
        ) / mm_per_point
    except (TypeError, ValueError):
        thinnest_wall_pt = 0.0

    # **The pieces of one edge, before anything else.** It has to run first:
    # the step below pairs the two *sides* of a stroke, and while one side is
    # still in pieces the long side of a wall pairs with a short fragment of
    # the other and is dragged off its own line.
    if bool(settings.get("prejoin_lsd_pieces", _DEFAULTS["prejoin_lsd_pieces"])):
        gap = _number(settings, "lsd_prejoin_gap_pt")
        offset = _number(settings, "lsd_prejoin_offset_pt")
        # **A bridged gap may never be as wide as an opening.** The step that
        # joins faces downstream *records* every break it bridges, because a
        # break interrupting both faces of a wall is a door or a window - that
        # is where openings come from on a plan that prints no marks. This one
        # closes a gap without recording it, so it has to stay below anything
        # that could be an opening or a door would be quietly sealed up. At
        # 1:100 the 3-point default is 106 mm against a 300 mm smallest
        # opening; at a scale where that is no longer true, this is what keeps
        # it true.
        smallest_opening = float(
            (config.get("walls") or {}).get("min_opening_width_mm") or 0.0
        )
        if smallest_opening > 0:
            gap = min(gap, smallest_opening / mm_per_point * 0.9)
        before = len(by_axis["h"]) + len(by_axis["v"])
        for axis in ("h", "v"):
            by_axis[axis] = _prejoin(by_axis[axis], gap, offset, thinnest_wall_pt)
        after = len(by_axis["h"]) + len(by_axis["v"])
        logger.info(
            f"line detection: {before} segments are {after} edges once the pieces "
            "of each edge are joined back into one run"
        )

    if bool(settings.get("merge_stroke_edges", _DEFAULTS["merge_stroke_edges"])):
        factor = _number(settings, "stroke_edge_width_factor")
        overlap_share = _number(settings, "stroke_edge_overlap_share")
        before = len(by_axis["h"]) + len(by_axis["v"])
        for axis in ("h", "v"):
            by_axis[axis] = _merge_stroke_edges(
                by_axis[axis], factor, overlap_share, thinnest_wall_pt
            )
        after = len(by_axis["h"]) + len(by_axis["v"])
        logger.info(
            f"line detection: {before} edges are {after} drawn lines once the two "
            "sides of each stroke are put back together"
        )

    result = _nothing()
    fat = 0
    for axis, key in (("h", "h"), ("v", "v")):
        for position, low, high, width_pt in by_axis[axis]:
            # **A run wider than the thickest wall the office builds is not a
            # line.** It is a filled area, a solid poché, a band of shading or
            # a photographic edge in a scan — and pairing it with anything
            # beside it would report the fill itself as a wall.
            if width_pt > max_width_pt:
                fat += 1
                continue
            result[key].append((round(position, 3), round(low, 3), round(high, 3)))
            result[f"{key}_widths"].append(round(width_pt, 4))
            result[f"{key}_dashed"].append(False)

    logger.info(
        f"line segment detection at {dpi:.0f} DPI: {len(result['h'])} horizontal and "
        f"{len(result['v'])} vertical lines kept from {len(lines)} segments "
        f"({off_axis} off-axis, {short} shorter than a letter, {fat} wider than a wall)"
    )
    return result
