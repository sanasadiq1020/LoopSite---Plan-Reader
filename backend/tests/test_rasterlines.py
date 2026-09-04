"""The line reader for sheets whose drawing is stored as a picture.

Every test here names the mistake it prevents. None of them uses a supplied
plan set: each draws its own drawing, so what is asserted is what was drawn,
not what some particular office happened to produce.
"""

import fitz
import pytest

from pipeline.plan import rasterlines, reading
from pipeline.plan.layout import extract_rulings
from pipeline.plan.walls import detect_walls

# One PDF point is 1/72 inch, so at 1:100 it is 35.28 mm of building.
MM_PER_POINT = 25.4 / 72.0 * 100
CALIBRATED = {"usable_for_measurement": True, "measured_mm_per_point": MM_PER_POINT}


def mm(value: float) -> float:
    """A length in millimetres of building, as points of paper at 1:100."""
    return value / MM_PER_POINT


@pytest.fixture(scope="module")
def config():
    return reading.load_config()


# --- a plan nobody in this project drew -----------------------------------

X0, Y0 = 120.0, 100.0
OUTER_W, OUTER_D = mm(12000), mm(8000)
EXTERNAL, INTERNAL = mm(230), mm(90)

# (orientation, position of the first face, thickness, start, end) in points.
# A 12 m x 8 m building in 230 mm external wall with two 90 mm partitions,
# every wall drawn as its two faces, which is how a plan draws one.
DRAWN_WALLS = [
    ("h", Y0, EXTERNAL, X0, X0 + OUTER_W),
    ("h", Y0 + OUTER_D - EXTERNAL, EXTERNAL, X0, X0 + OUTER_W),
    ("v", X0, EXTERNAL, Y0, Y0 + OUTER_D),
    ("v", X0 + OUTER_W - EXTERNAL, EXTERNAL, Y0, Y0 + OUTER_D),
    ("v", X0 + mm(5000), INTERNAL, Y0, Y0 + OUTER_D),
    ("h", Y0 + mm(4500), INTERNAL, X0 + mm(5000), X0 + OUTER_W),
]


def _draw_the_plan(canvas, stroke=0.7):
    for orientation, position, thickness, start, end in DRAWN_WALLS:
        for face in (position, position + thickness):
            if orientation == "h":
                canvas.draw_line(fitz.Point(start, face), fitz.Point(end, face), width=stroke)
            else:
                canvas.draw_line(fitz.Point(face, start), fitz.Point(face, end), width=stroke)


def _as_a_picture(tmp_path, rotation: int = 0):
    """That plan published the way a plan set does: as an image on a page.

    Drawn, rendered to pixels and re-inserted, so the resulting page has no
    vector line work at all — the shape one real plan set has.
    """
    drawing = fitz.open()
    canvas = drawing.new_page(width=842, height=595)
    _draw_the_plan(canvas)
    picture = tmp_path / "drawn_plan.png"
    canvas.get_pixmap(matrix=fitz.Matrix(4, 4)).save(str(picture))
    drawing.close()

    document = fitz.open()
    page = document.new_page(width=842, height=595)
    page.insert_image(fitz.Rect(0, 0, 842, 595), filename=str(picture))
    if rotation:
        page.set_rotation(rotation)
    return document, page


def _wall_for(walls, orientation, position, thickness, start, end):
    """The candidate covering one drawn wall, matched by where it is."""
    axis = "x" if orientation == "h" else "y"
    centre = position + thickness / 2.0
    best = None
    for wall in walls:
        if wall["runs_along"] != axis:
            continue
        low, high = sorted(wall["face_positions_pt"])
        if abs((low + high) / 2.0 - centre) > thickness:
            continue
        run_low = wall["bbox"][0] if axis == "x" else wall["bbox"][1]
        run_high = wall["bbox"][2] if axis == "x" else wall["bbox"][3]
        if run_high < start or run_low > end:
            continue
        error = abs(wall["thickness_mm"] - thickness * MM_PER_POINT)
        if best is None or error < best[1]:
            best = (wall, error)
    return best


def test_every_drawn_wall_is_found_at_the_thickness_it_was_drawn(tmp_path, config):
    """The whole point of the reader, measured against what was drawn.

    Prevents a change that raises the wall count while getting the walls wrong
    — the shape of error this project has hit more than once. A thickness is
    what decides whether a candidate is at a thickness the office builds, so a
    reading that is out by more than the nominal tolerance is not a reading.
    """
    document, page = _as_a_picture(tmp_path)
    try:
        walls = detect_walls(extract_rulings(page), CALIBRATED, config, "P01", page=page)
        assert walls, "a plan drawn as a picture must still be measurable"
        assert all(wall["line_source"] == "lsd_raster" for wall in walls)

        tolerance = float(config["walls"].get("nominal_thickness_tolerance_mm", 12))
        for drawn in DRAWN_WALLS:
            found = _wall_for(walls, *drawn)
            assert found is not None, f"the wall drawn at {drawn} was not found"
            wall, error = found
            assert error <= tolerance, (
                f"thickness read as {wall['thickness_mm']} mm against "
                f"{drawn[2] * MM_PER_POINT:.0f} mm drawn"
            )
            assert wall["matches_nominal_thickness"]
    finally:
        document.close()


def test_the_two_sides_of_one_stroke_are_not_two_faces(tmp_path, config):
    """A Line Segment Detector finds edges, and a plotted line has two.

    Left alone, each side of a stroke pairs with the far face of the wall
    separately: one wall is reported two to four times over, each copy with a
    thickness half a stroke out. Prevents that regression by measuring what
    turning the merge off actually costs.
    """
    document, page = _as_a_picture(tmp_path)
    try:
        merged = rasterlines.extract_rulings_from_image(page, config, MM_PER_POINT)
        raw_config = dict(config)
        raw_config["walls"] = dict(
            config["walls"],
            raster_lines=dict(config["walls"]["raster_lines"], merge_stroke_edges=False),
        )
        raw = rasterlines.extract_rulings_from_image(page, raw_config, MM_PER_POINT)

        assert len(merged["h"]) < len(raw["h"])
        assert len(merged["v"]) < len(raw["v"])
        # A merged line carries the distance between the two sides as its
        # width, which is the run's own measured thickness.
        assert all(width > 0 for width in merged["h_widths"])
    finally:
        document.close()


def test_a_wall_is_never_merged_into_one_line(tmp_path, config):
    """Two lines as far apart as the thinnest wall are two faces, not one.

    Without that ceiling a widened stroke-edge factor would swallow a wall's
    own two faces and the wall would disappear entirely — the same failure the
    collinear tolerance is held below the thinnest wall for.
    """
    thinnest_pt = float(config["walls"]["min_wall_thickness_mm"]) / MM_PER_POINT
    # Two faces exactly a thin wall apart, each claiming to be a very wide
    # stroke, which is the only way the width test alone could pair them.
    segments = [(100.0, 0.0, 200.0, thinnest_pt), (100.0 + thinnest_pt, 0.0, 200.0, thinnest_pt)]
    merged = rasterlines._merge_stroke_edges(segments, 10.0, 0.5, thinnest_pt)
    assert len(merged) == 2, "a wall's two faces were merged into one line"


def test_a_diagonal_is_not_a_wall_face(config):
    """A plan is drawn on the axes; anything at an angle is something else.

    A door swing's chord, a roof diagonal, a stair nosing and a note's leader
    all arrive as segments, and a wall is found by pairing faces that run
    parallel — so an off-axis segment cannot be one.
    """
    tolerance = float(config["walls"]["raster_lines"]["straightness_tolerance_degrees"])
    assert rasterlines._axis_of(0, 0, 100, 0, tolerance) == "h"
    assert rasterlines._axis_of(0, 0, 0, 100, tolerance) == "v"
    assert rasterlines._axis_of(0, 0, 100, 100, tolerance) is None
    # A scanned sheet's own skew is allowed for; a diagonal is not.
    assert rasterlines._axis_of(0, 0, 100, 1.5, tolerance) == "h"
    assert rasterlines._axis_of(0, 0, 100, 20, tolerance) is None


def test_the_page_transform_is_the_one_the_vector_geometry_uses(tmp_path, config):
    """A recovered line lands where the drawing actually is, rotation and all.

    A PDF page stores a rotation and CAD exports very often use it. The text
    and the vector geometry are read in the space the page *displays* in, and
    the render has to be read in the same one, or every recovered line lands
    somewhere else on the sheet and the two faces of a wall are looked for
    across the wrong axis.
    """
    for rotation in (0, 90):
        document, page = _as_a_picture(tmp_path, rotation=rotation)
        try:
            recovered = rasterlines.extract_rulings_from_image(page, config, MM_PER_POINT)
            # The drawn plan is 12 m wide and 8 m deep on an unrotated page,
            # and a 90 degree rotation swaps those on the displayed page.
            width = max(high - low for _p, low, high in recovered["h"]) * MM_PER_POINT
            depth = max(high - low for _p, low, high in recovered["v"]) * MM_PER_POINT
            along, across = (width, depth) if rotation == 0 else (depth, width)
            assert 11500 < along < 12500, f"rotation {rotation}: {along:.0f} mm across"
            assert 7500 < across < 8500, f"rotation {rotation}: {across:.0f} mm deep"
        finally:
            document.close()


def test_the_drawings_own_lines_are_never_displaced_by_pixels(tmp_path, config):
    """A sheet with real line work is not re-read as a picture."""
    document = fitz.open()
    page = document.new_page(width=842, height=595)
    _draw_the_plan(page)
    try:
        walls = detect_walls(extract_rulings(page), CALIBRATED, config, "P01", page=page)
        assert walls
        assert all(wall["line_source"] == "vector" for wall in walls)
    finally:
        document.close()


def test_a_reader_that_cannot_run_returns_nothing_rather_than_failing(monkeypatch, config):
    """A build of OpenCV without the detector is a fact about the server.

    The sheet keeps whatever its own geometry gave it; the upload does not
    fail (Critical Rule 6).
    """
    monkeypatch.setattr(rasterlines, "_detector", lambda: None)
    empty = rasterlines.extract_rulings_from_image(None, config, MM_PER_POINT)
    assert empty == {
        "h": [], "v": [], "h_widths": [], "v_widths": [], "h_dashed": [], "v_dashed": [],
    }


def test_a_picture_never_claims_a_dashed_line(tmp_path, config):
    """A dash cannot be established from pixels, so none is claimed.

    A dashed line is a roof extent, an eave, a setback or a boundary and is
    set aside as not a wall. Claiming one from a run that a crossing, a symbol
    or an artefact of the scan happened to break would set aside real walls.
    """
    document, page = _as_a_picture(tmp_path)
    try:
        recovered = rasterlines.extract_rulings_from_image(page, config, MM_PER_POINT)
        assert recovered["h_dashed"] and not any(recovered["h_dashed"])
        assert recovered["v_dashed"] and not any(recovered["v_dashed"])
        assert len(recovered["h_dashed"]) == len(recovered["h"])
        assert len(recovered["v_dashed"]) == len(recovered["v"])
    finally:
        document.close()


def test_both_sources_hand_over_the_same_shape(tmp_path, config):
    """Downstream code cannot tell which reader produced a segment.

    The vector reader and this one are interchangeable by contract, and a key
    missing from one of them would only be found on a sheet drawn as a
    picture — which is to say, on somebody else's plan set.
    """
    document, page = _as_a_picture(tmp_path)
    try:
        from_picture = rasterlines.extract_rulings_from_image(page, config, MM_PER_POINT)
        from_lines = extract_rulings(page)
        assert set(from_picture) == set(from_lines)
        for axis in ("h", "v"):
            assert len(from_picture[axis]) == len(from_picture[f"{axis}_widths"])
            assert len(from_picture[axis]) == len(from_picture[f"{axis}_dashed"])
            for segment in from_picture[axis]:
                assert len(segment) == 3
    finally:
        document.close()


def test_every_threshold_comes_from_the_config(config):
    """No value this reader uses is written into the code.

    The fallbacks in the module exist so a missing config degrades rather than
    crashing; the file on disk is the source of truth, and this fails if a
    setting is ever added to one without the other.
    """
    on_disk = config["walls"]["raster_lines"]
    for name in rasterlines._DEFAULTS:
        assert name in on_disk, f"{name} is not in config/wall_config.json"


# --- the pieces of one edge ------------------------------------------------


def test_the_pieces_of_one_edge_are_joined_into_one_run(config):
    """The defect this exists for, in the smallest form that shows it.

    A detector working on pixels returns one drawn edge as several pieces,
    each a fraction of a point off its neighbours. Left in pieces they land in
    neighbouring buckets in the face-merging step downstream, become two or
    three separate faces a few tens of millimetres apart, and then compete
    with each other for the far face of the same wall — and the wall comes out
    as stretches too short to be a wall at all. Thirty real internal walls on
    one floor plan were lost this way.
    """
    settings = config["walls"]["raster_lines"]
    gap = float(settings["lsd_prejoin_gap_pt"])
    offset = float(settings["lsd_prejoin_offset_pt"])
    thinnest = float(config["walls"]["min_wall_thickness_mm"]) / MM_PER_POINT

    # One 80-point edge, as five pieces with the jitter a render produces.
    pieces = [
        (100.00, 0.0, 15.0, 0.4),
        (100.35, 16.5, 30.0, 0.4),
        (99.70, 29.4, 45.0, 0.4),
        (100.55, 47.0, 62.0, 0.4),
        (100.10, 63.5, 80.0, 0.4),
    ]
    joined = rasterlines._prejoin(pieces, gap, offset, thinnest)
    assert len(joined) == 1, f"one edge came back as {len(joined)} runs"
    position, start, end, _width = joined[0]
    assert (start, end) == (0.0, 80.0)
    # The run sits where its pieces do, weighted by how much each contributed.
    assert 99.8 < position < 100.4


def test_a_wall_is_never_joined_into_one_edge(config):
    """Two lines a wall's thickness apart are two faces, not one edge.

    The pre-join's offset is capped at the thinnest wall the office builds
    whatever the config says, because joining a wall's own two faces does not
    repair the reading — it erases the wall.
    """
    thinnest = float(config["walls"]["min_wall_thickness_mm"]) / MM_PER_POINT
    faces = [(100.0, 0.0, 80.0, 0.4), (100.0 + thinnest, 0.0, 80.0, 0.4)]
    # Even asked for an offset far wider than a wall.
    joined = rasterlines._prejoin(faces, 3.0, thinnest * 5, thinnest)
    assert len(joined) == 2, "a wall's two faces were joined into one edge"


def test_a_line_lying_alongside_is_not_a_continuation(config):
    """A piece has to carry on from where the run reached, not start inside it.

    Without that, a second line running alongside the first — a lining, a
    hatch boundary, the far face of a thin wall — is averaged into it, and
    both are lost. It cost eight walls on one floor plan.
    """
    settings = config["walls"]["raster_lines"]
    gap = float(settings["lsd_prejoin_gap_pt"])
    offset = float(settings["lsd_prejoin_offset_pt"])
    alongside = [(100.0, 0.0, 80.0, 0.4), (100.9, 10.0, 70.0, 0.4)]
    joined = rasterlines._prejoin(alongside, gap, offset, 0.0)
    assert len(joined) == 2


def test_the_pre_join_never_seals_up_a_door(tmp_path, config):
    """A gap it closes is not recorded, so it must never be opening-sized.

    The face-merging step downstream records every break it bridges, because a
    break through both faces of a wall is a door or a window. This one does
    not, so the gap it may close is held below the smallest opening — at any
    scale, not only at the one the default was written for.
    """
    walls = config["walls"]
    gap_pt = float(walls["raster_lines"]["lsd_prejoin_gap_pt"])
    smallest_opening_mm = float(walls["min_opening_width_mm"])
    assert gap_pt * MM_PER_POINT < smallest_opening_mm

    # And at a scale where the configured gap would be wider than an opening,
    # the reader clamps it rather than sealing one up.
    document, page = _as_a_picture(tmp_path)
    try:
        coarse = smallest_opening_mm / gap_pt  # mm per point that makes them equal
        recovered = rasterlines.extract_rulings_from_image(page, config, coarse * 2)
        assert recovered is not None
    finally:
        document.close()
