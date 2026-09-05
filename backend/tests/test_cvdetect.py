"""Regression tests for the computer-vision plan reader.

Every test here names a mistake that was actually made while building the
module and that produced output which looked plausible and was not. A test
that only restates what the code does prevents nothing; these each pin a
specific wrong answer.
"""

import math

import fitz
import numpy as np
import pytest

from pipeline.plan.cvdetect import calibration, crosslink, openings as openings_step
from pipeline.plan.cvdetect import settings as cv_settings
from pipeline.plan.cvdetect import vectorpaths, wallgeometry
from pipeline.plan.cvdetect.settings import Scale, load_settings

# One point of a 1:100 drawing is this many millimetres of building.
AT_1_TO_100 = 25.4 / 72.0 * 100


@pytest.fixture
def config():
    return load_settings()


@pytest.fixture
def scale():
    return Scale(mm_per_point=AT_1_TO_100, dpi=300.0, source="test", confidence=1.0)


# --------------------------------------------------------------------------
# Settings and the scale that turns them into pixels
# --------------------------------------------------------------------------

def test_a_config_naming_one_wall_setting_keeps_the_others():
    """A shallow update would silently discard nine settings to change one."""
    merged = cv_settings._deep_merge(
        {"wall": {"min_thickness_mm": 70.0, "max_thickness_mm": 320.0}},
        {"wall": {"min_thickness_mm": 90.0}},
    )
    assert merged["wall"]["min_thickness_mm"] == 90.0
    assert merged["wall"]["max_thickness_mm"] == 320.0


def test_a_setting_that_is_not_a_number_is_logged_not_raised(config):
    broken = cv_settings._deep_merge(config, {"wall": {"min_thickness_mm": "ninety"}})
    assert cv_settings.number(broken, "wall.min_thickness_mm", 70.0) == 70.0


def test_millimetres_and_pixels_round_trip(scale):
    assert scale.px_from_mm(scale.mm_from_px(123.0)) == pytest.approx(123.0)
    # A 90 mm wall at 1:100 and 300 DPI is about ten pixels. If this ever comes
    # out as one, nothing can be skeletonised.
    assert 8 <= scale.px_from_mm(90.0) <= 13


def test_a_page_whose_paper_does_not_start_at_the_corner_is_not_offset():
    """Dropping the page origin puts every line the same distance from its drawing."""
    offset = Scale(mm_per_point=AT_1_TO_100, dpi=72.0, origin=(20.0, 30.0))
    assert offset.point_to_pixel(20.0, 30.0) == (0.0, 0.0)
    assert offset.pixel_to_point(*offset.point_to_pixel(100.0, 200.0)) == pytest.approx(
        (100.0, 200.0)
    )


def test_a_sheet_with_no_established_scale_measures_nothing():
    assert not Scale(mm_per_point=0.0, dpi=300.0).usable


# --------------------------------------------------------------------------
# Step 2 - reading a dimension figure
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "printed, expected",
    [
        ("11 030", 11030.0),
        ("11,030", 11030.0),
        ("3325", 3325.0),
        ("10260 TO WALL", 10260.0),
        ("2600 TO EAVE", 2600.0),
        ("19920 OVERALL", 19920.0),
        # Character recognition reads the thousands comma as a full stop. Both
        # readings are the same number of millimetres, so no decision is needed.
        ("7.370", 7370.0),
    ],
)
def test_a_printed_figure_is_read_as_millimetres(printed, expected, config):
    assert calibration.dimension_value_mm(printed, config)[0] == expected


@pytest.mark.parametrize("printed", ["KITCHEN", "1:100", "", "5", "100x100 Steel posts"])
def test_what_is_not_a_dimension_is_not_read_as_one(printed, config):
    assert calibration.dimension_value_mm(printed, config) is None


def test_a_figure_printed_rotated_dimensions_the_other_axis():
    """Without this a sheet's vertical dimensions all go to the wrong axis."""
    upright = {"text": "1200", "bbox": [0, 0, 30, 8], "dir": (1.0, 0.0)}
    turned = {"text": "1200", "bbox": [0, 0, 8, 30], "dir": (0.0, -1.0)}
    assert calibration._axis_of_text(upright) == "h"
    assert calibration._axis_of_text(turned) == "v"


def test_character_recognition_gives_no_direction_so_the_box_says(config):
    """Recognition returns no writing direction; a tall box holds a turned figure."""
    assert calibration._axis_of_text({"text": "1200", "bbox": [0, 0, 8, 40], "dir": None}) == "v"
    assert calibration._axis_of_text({"text": "1200", "bbox": [0, 0, 40, 8], "dir": None}) == "h"


# --------------------------------------------------------------------------
# Step 1 - what is not a wall
# --------------------------------------------------------------------------

def test_a_solid_line_is_not_called_dashed():
    """PyMuPDF writes "[] 0" for a solid line. Testing the whole string for a
    digit finds the zero and calls every solid line on the sheet dashed."""
    assert vectorpaths._states_a_dash_pattern("[] 0") is False
    assert vectorpaths._states_a_dash_pattern(None) is False
    assert vectorpaths._states_a_dash_pattern("[ 2.02 2.02 ] 0") is True


def test_a_dashed_line_is_recognised_from_its_shape(config):
    """Every plan set in use exports its dashes as separate short segments with
    the pattern left empty, so the file's own answer finds nothing."""
    paths = vectorpaths.VectorPaths()
    for step in range(8):
        start = step * 10.0
        paths.segments.append(
            vectorpaths.Segment(start, 100.0, start + 5.0, 100.0, 0.3, False, step)
        )
    vectorpaths._mark_dashed(paths, config)
    assert all(s.role == "dashed" for s in paths.segments)


def test_a_wall_broken_by_a_doorway_is_not_a_dashed_line(config):
    """The worst outcome available here. A wall face with two doors in it is
    also a line in pieces - but the pieces are long and the gaps irregular."""
    paths = vectorpaths.VectorPaths()
    for start, end in [(0.0, 120.0), (145.0, 300.0), (330.0, 500.0), (505.0, 900.0)]:
        paths.segments.append(
            vectorpaths.Segment(start, 100.0, end, 100.0, 0.3, False, 0)
        )
    vectorpaths._mark_dashed(paths, config)
    assert all(s.role == "structural" for s in paths.segments)


def test_the_weight_split_can_never_delete_the_drawing():
    """Otsu put the cut at the sheet border (1.36 pt) on a real plan whose
    building is drawn at 0.17 pt, which would have deleted the whole plan."""
    paths = vectorpaths.VectorPaths()
    # The drawing: many long lines at 0.17 pt. The frame: a few at 1.36 pt.
    for index in range(50):
        paths.segments.append(vectorpaths.Segment(0, index, 400, index, 0.17, False, index))
    for index in range(4):
        paths.segments.append(vectorpaths.Segment(0, 500 + index, 800, 500 + index, 1.36, False, index))
    split = vectorpaths._dominant_weight_split(paths)
    assert split <= 0.17, "the class carrying the most drawn length must never be set aside"


def test_the_weight_split_does_set_aside_lighter_line_work():
    paths = vectorpaths.VectorPaths()
    for index in range(30):
        paths.segments.append(vectorpaths.Segment(0, index, 900, index, 0.50, False, index))
    for index in range(10):
        paths.segments.append(vectorpaths.Segment(0, 300 + index, 100, 300 + index, 0.13, False, index))
    assert vectorpaths._dominant_weight_split(paths) == 0.50


# --------------------------------------------------------------------------
# Step 3 - openings
# --------------------------------------------------------------------------

def test_a_walls_own_two_faces_are_not_a_window(config, scale):
    """Without requiring an outer pair with something drawn between them, "two
    parallel lines closer together than a wall is thick" is the definition of a
    wall - and 112 windows were reported on a floor plan that has about thirty."""
    paths = vectorpaths.VectorPaths()
    thickness_pt = 90.0 / scale.mm_per_point
    paths.segments.append(vectorpaths.Segment(0, 100.0, 60.0, 100.0, 0.3, False, 0))
    paths.segments.append(
        vectorpaths.Segment(0, 100.0 + thickness_pt, 60.0, 100.0 + thickness_pt, 0.3, False, 1)
    )
    assert openings_step._windows_from_glazing(None, scale, paths, config) == []


def test_glazing_drawn_inside_a_wall_is_a_window(config, scale):
    paths = vectorpaths.VectorPaths()
    thickness_pt = 90.0 / scale.mm_per_point
    for share in (0.0, 0.35, 0.65, 1.0):
        y = 100.0 + thickness_pt * share
        paths.segments.append(vectorpaths.Segment(0.0, y, 60.0, y, 0.3, False, int(share * 10)))
    found = openings_step._windows_from_glazing(None, scale, paths, config)
    assert len(found) == 1
    assert found[0].kind == "window"


def test_a_basin_drawn_round_is_not_a_door_swing():
    """Hough proposes every circle on the sheet - 4,767 on one floor plan. What
    separates a door from a basin is how far round the ink actually goes."""
    image = np.zeros((200, 200), dtype=np.uint8)
    import cv2

    cv2.circle(image, (100, 100), 50, 255, 2)
    full = openings_step._sweep_of_ink(image, 100, 100, 50, 200, 200)
    assert full > 350, "a full circle sweeps the whole way round"

    quarter = np.zeros((200, 200), dtype=np.uint8)
    cv2.ellipse(quarter, (100, 100), (50, 50), 0, 0, 90, 255, 2)
    assert 70 < openings_step._sweep_of_ink(quarter, 100, 100, 50, 200, 200) < 110


def test_two_readings_of_one_opening_are_one_opening(config, scale):
    """A door's swing and the D07 printed beside it are the same door."""
    swing = openings_step.Opening("", "door", [100.0, 100.0, 123.0, 123.0], "arc_geometry", 0.8)
    mark = openings_step.Opening("", "door", [110.0, 108.0, 122.0, 114.0], "printed_mark", 0.7,
                                 mark="D07")
    kept = openings_step._one_opening_per_place([swing, mark], scale, config)
    assert len(kept) == 1
    # The drawn symbol wins, because it is the thing itself drawn to size.
    assert kept[0].found_by == "arc_geometry"
    assert kept[0].mark == "D07"


# --------------------------------------------------------------------------
# Step 4 - walls
# --------------------------------------------------------------------------

def test_a_wall_that_carries_on_past_a_junction_is_one_wall(scale, config):
    """A skeleton is cut at every junction, so a 12 m external wall arrives as
    five stubs unless the pieces that carry on are put back together."""
    left = np.array([(0, 100), (50, 100), (100, 100)], dtype=np.int32)
    right = np.array([(100, 100), (150, 100), (200, 100)], dtype=np.int32)
    joined = wallgeometry._join_runs_that_carry_on([left, right], scale, config)
    assert len(joined) == 1
    assert wallgeometry._run_length(joined[0]) == pytest.approx(200.0, abs=1.0)


def test_a_corner_is_two_walls_not_one(scale, config):
    """A wall running north and a wall running east are two walls."""
    across = np.array([(0, 100), (50, 100), (100, 100)], dtype=np.int32)
    down = np.array([(100, 100), (100, 150), (100, 200)], dtype=np.int32)
    assert len(wallgeometry._join_runs_that_carry_on([across, down], scale, config)) == 2


def test_a_whisker_off_a_corner_is_not_a_wall(scale, config):
    """Closing rounds a corner and thinning grows a whisker off it. Left in,
    each whisker is counted as a wall and breaks the wall it hangs off."""
    wall = np.array([(0, 100), (200, 100), (400, 100)], dtype=np.int32)
    whisker = np.array([(400, 100), (403, 103), (406, 106)], dtype=np.int32)
    kept = wallgeometry._prune_spurs([wall, whisker], scale, config)
    assert len(kept) == 1
    assert wallgeometry._run_length(kept[0]) > 100


def test_a_short_run_joined_at_both_ends_is_kept(scale, config):
    """A nib, a pier and the stub between two doorways are short and real."""
    left = np.array([(0, 100), (100, 100)], dtype=np.int32)
    nib = np.array([(100, 100), (104, 100)], dtype=np.int32)
    right = np.array([(104, 100), (300, 100)], dtype=np.int32)
    kept = wallgeometry._prune_spurs([left, nib, right], scale, config)
    assert len(kept) == 3


def test_a_junction_does_not_make_a_wall_too_thick(scale):
    """A corner where two 230 mm walls meet is 325 mm across the diagonal. Take
    the mean and one junction puts the whole wall outside every thickness the
    office builds."""
    distance = np.full((10, 100), 5.0, dtype=np.float32)
    distance[:, 50] = 40.0
    points = np.array([(x, 5) for x in range(100)], dtype=np.int32)
    assert wallgeometry._thickness_along(distance, points, (0, 0)) == pytest.approx(5.0)


def test_a_solid_wall_is_not_reported_as_hatched(config):
    """A diagonal kernel fits inside a blacked-in wall as happily as inside a
    hatched one, which reported 45 of 66 outlined walls as hatched."""
    band = np.full((40, 200), 255, dtype=np.uint8)
    solid = np.full((40, 200), 255, dtype=np.uint8)
    share, drawn_as = wallgeometry._how_the_interior_is_drawn(solid, band, config)
    assert drawn_as == "solid"
    assert share > 0.9


def test_a_wall_drawn_as_an_outline_says_so(config):
    band = np.full((40, 200), 255, dtype=np.uint8)
    outline = np.zeros((40, 200), dtype=np.uint8)
    outline[0:2, :] = 255
    outline[-2:, :] = 255
    _share, drawn_as = wallgeometry._how_the_interior_is_drawn(outline, band, config)
    assert drawn_as == "outline"


def test_a_wall_carries_every_field_the_model_requires(scale):
    """Critical Rule 12: twelve fields on every canonical element record."""
    from shapely.geometry import LineString

    wall = wallgeometry.Wall(
        element_id="W-001",
        centreline=LineString([(0, 0), (100, 0)]),
        outline=None,
        thickness_mm=90.0,
        length_mm=3528.0,
        source_sheet="Page 4",
        source_bbox=[0, 0, 100, 3],
        extraction_method="test",
        confidence=0.8,
    )
    record = wall.as_record()
    for field in (
        "element_id", "element_type", "storey", "geometry", "dimensions", "material",
        "source_sheet", "source_bbox", "extraction_method", "confidence",
        "review_status", "linked_issue_ids",
    ):
        assert field in record, f"a canonical wall record must carry {field}"


# --------------------------------------------------------------------------
# Step 5 - cross-linking
# --------------------------------------------------------------------------

def _wall(identifier, x0, y0, x1, y1, thickness_pt):
    from shapely.geometry import LineString

    line = LineString([(x0, y0), (x1, y1)])
    return wallgeometry.Wall(
        element_id=identifier,
        centreline=line,
        outline=line.buffer(thickness_pt / 2.0, cap_style=2, join_style=2),
        thickness_mm=90.0,
        length_mm=line.length,
        source_sheet="test",
        source_bbox=list(line.bounds),
        extraction_method="test",
        confidence=0.8,
    )


def test_the_reader_allows_for_the_gap_it_made_itself(config, scale):
    """Step 3 paints every opening white before the walls are closed, so a wall
    band has a hole exactly where the opening is. Measuring without allowing
    for that reported real, marked doors as having no wall within 200 mm."""
    thickness_pt = 90.0 / scale.mm_per_point
    padding_pt = 40.0 / scale.mm_per_point
    wall = _wall("W-1", 0.0, 100.0, 400.0, 100.0, thickness_pt)
    # A swing whose hinge sits just past the hole the mask cut in the wall.
    edge = 100.0 + thickness_pt / 2.0 + padding_pt * 0.9
    door = openings_step.Opening(
        "D01", "door", [50.0, edge, 73.0, edge + 23.0], "arc_geometry", 0.8
    )
    links = crosslink.link_openings_to_walls([wall], [door], scale, config)
    assert door.wall_id == "W-1"
    assert links["W-1"] == ["D01"]


def test_two_walls_equally_close_leave_the_opening_unplaced(config, scale):
    """A wrong link is worse than none: every later stage trusts it and cuts
    the void into the wrong wall."""
    thickness_pt = 90.0 / scale.mm_per_point
    one = _wall("W-1", 0.0, 100.0, 400.0, 100.0, thickness_pt)
    two = _wall("W-2", 0.0, 100.4, 400.0, 100.4, thickness_pt)
    door = openings_step.Opening(
        "D01", "door", [200.0, 99.0, 210.0, 101.5], "arc_geometry", 0.8
    )
    crosslink.link_openings_to_walls([one, two], [door], scale, config)
    assert door.wall_id is None
    assert any("guess" in note for note in door.evidence)


def test_an_opening_records_how_far_along_its_wall_it_sits(config, scale):
    """Fractions, not millimetres: they survive the turn from the page's
    downward Y into the building's northward Y."""
    thickness_pt = 90.0 / scale.mm_per_point
    wall = _wall("W-1", 0.0, 100.0, 400.0, 100.0, thickness_pt)
    door = openings_step.Opening(
        "D01", "door", [190.0, 99.0, 210.0, 101.0], "arc_geometry", 0.8
    )
    crosslink.link_openings_to_walls([wall], [door], scale, config)
    assert door.wall_id == "W-1"
    assert any("of the way along" in note for note in door.evidence)


def test_nothing_is_placed_when_the_scale_was_never_established(config):
    wall = _wall("W-1", 0.0, 100.0, 400.0, 100.0, 3.0)
    door = openings_step.Opening("D01", "door", [190.0, 99.0, 210.0, 101.0], "arc_geometry", 0.8)
    unmeasured = Scale(mm_per_point=0.0, dpi=300.0)
    crosslink.link_openings_to_walls([wall], [door], unmeasured, config)
    assert door.wall_id is None


# --------------------------------------------------------------------------
# End to end, on a building drawn for the purpose
# --------------------------------------------------------------------------

def _drawn_building(tmp_path, thickness_mm=230.0, width_mm=12000.0, height_mm=8000.0):
    """A plain rectangular building at 1:100, drawn as a PDF.

    Scoring a reader against a plan set it was built on is how a threshold gets
    tuned to that plan set. A building drawn to known dimensions can be scored
    against what was actually drawn.
    """
    document = fitz.open()
    page = document.new_page(width=595, height=842)
    mm_per_point = AT_1_TO_100
    outer_w, outer_h = width_mm / mm_per_point, height_mm / mm_per_point
    thickness = thickness_mm / mm_per_point
    left, top = 100.0, 200.0

    shape = page.new_shape()
    shape.draw_rect(fitz.Rect(left, top, left + outer_w, top + outer_h))
    shape.draw_rect(
        fitz.Rect(
            left + thickness, top + thickness,
            left + outer_w - thickness, top + outer_h - thickness,
        )
    )
    shape.finish(color=(0, 0, 0), width=0.5)
    shape.commit()

    path = tmp_path / "drawn_building.pdf"
    document.save(str(path))
    document.close()
    return path


def test_a_building_drawn_to_known_dimensions_measures_back(tmp_path):
    """The whole module, end to end, against a building whose real thickness is
    known because it was drawn to it."""
    from pipeline.plan.cvdetect import read_sheet

    path = _drawn_building(tmp_path)
    document = fitz.open(str(path))
    try:
        reading = read_sheet(document[0], sheet_name="Drawn building", printed_scale="1:100")
    finally:
        document.close()

    assert reading.scale.usable
    assert reading.scale.mm_per_point == pytest.approx(AT_1_TO_100, rel=0.01)
    assert reading.walls, "a rectangle in 230 mm wall must report walls"

    thicknesses = [w.thickness_mm for w in reading.walls]
    worst = max(abs(t - 230.0) for t in thicknesses)
    assert worst < 25.0, f"thickness measured {thicknesses}, drawn 230 mm"

    total_m = sum(w.length_mm for w in reading.walls) / 1000.0
    # The centrelines of a 12 x 8 m building in 230 mm wall run
    # 2 x (12 - 0.23) + 2 x (8 - 0.23) = 39.1 m.
    assert 33.0 < total_m < 45.0, f"measured {total_m:.1f} m of centreline"


def test_a_file_that_is_not_a_pdf_is_reported_not_raised(tmp_path):
    """Critical Rule 6: the app degrades to a logged, visible error state."""
    from pipeline.plan.cvdetect import read_document

    broken = tmp_path / "not_a_plan.pdf"
    broken.write_bytes(b"this is not a PDF at all")
    result = read_document(broken)
    assert result["sheets"] == []
    assert result["note"]


def test_a_pdf_with_no_pages_says_so(tmp_path):
    """PyMuPDF will not write a zero-page file, so one is spelled out by hand -
    which is what a real one that arrives this way looks like anyway."""
    from pipeline.plan.cvdetect import read_document

    empty = tmp_path / "empty.pdf"
    empty.write_bytes(
        b"\n".join(
            [
                b"%PDF-1.4",
                b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj",
                b"2 0 obj<</Type/Pages/Kids[]/Count 0>>endobj",
                b"trailer<</Root 1 0 R>>",
                b"%%EOF",
            ]
        )
    )
    result = read_document(empty)
    assert result["sheets"] == []
    assert result["note"], "a file with nothing in it must say so, not come back blank"


# --------------------------------------------------------------------------
# Step 2, fallback - the scale a sheet prints about itself
# --------------------------------------------------------------------------

def _sheet_with(tmp_path, items, size="a3", name="sheet.pdf"):
    """A blank sheet carrying the given text at the given places.

    ``items`` are ``(x, y, text)`` or ``(x, y, text, fontsize)`` in points, with
    ``y`` the text baseline - which is how a title block's own cells arrive.

    Landscape, because that is how a construction drawing is plotted -
    ``fitz.paper_size`` hands back portrait, and a title block placed by
    portrait coordinates lands off the side of the sheet.
    """
    width, height = sorted(fitz.paper_size(size), reverse=True)
    document = fitz.open()
    page = document.new_page(width=width, height=height)
    for item in items:
        x, y, text = item[0], item[1], item[2]
        page.insert_text((x, y), text, fontsize=item[3] if len(item) > 3 else 8)
    path = tmp_path / name
    document.save(str(path))
    document.close()
    return path


def _title_block_scale(tmp_path, items, size="a3", config=None):
    from pipeline.plan.cvdetect import titleblockscale
    from pipeline.plan.cvdetect.calibration import _text_lines

    path = _sheet_with(tmp_path, items, size)
    document = fitz.open(str(path))
    try:
        page = document[0]
        return titleblockscale.scale_from_title_block(
            page, _text_lines(page, None), config or load_settings()
        )
    finally:
        document.close()


def test_a_scale_printed_in_the_title_block_is_read(tmp_path, config):
    found = _title_block_scale(tmp_path, [(60, 800, "SCALE: 1:100 @ A3")], config=config)
    assert found is not None
    assert found.ratio == "1:100"
    assert found.mm_per_point == pytest.approx(AT_1_TO_100, rel=1e-6)
    assert found.rank == 4


def test_a_label_above_its_value_is_a_title_block_cell(tmp_path, config):
    """One real plan set prints 'Scale:' on one line and '1:100 @ A3' on the next."""
    found = _title_block_scale(
        tmp_path, [(1050, 790, "Scale:", 6), (1050, 800, "1:100 @ A3")], config=config
    )
    assert found is not None and found.ratio == "1:100" and found.rank == 4


def test_a_label_beside_its_value_is_a_title_block_cell(tmp_path, config):
    """Another prints 'SCALE:' with '1 : 200' next to it, spaces and all."""
    found = _title_block_scale(
        tmp_path, [(700, 800, "SCALE:", 6), (740, 800, "1 : 200")], config=config
    )
    assert found is not None and found.ratio == "1:200"


def test_a_fall_is_not_a_scale(tmp_path, config):
    """'1:100MM FALL ON SPANDECK' is printed on a real floor plan, in the strip
    where a title block sits. A ratio followed straight by a letter is not one."""
    found = _title_block_scale(
        tmp_path, [(60, 800, "SCALE:", 6), (200, 800, "1:100MM FALL ON SPANDECK")], config=config
    )
    assert found is None


def test_do_not_scale_drawing_is_not_a_scale_label(tmp_path, config):
    """Printed across the bottom strip of real sheets, right where a scale is."""
    found = _title_block_scale(
        tmp_path,
        [(60, 800, "DO NOT SCALE DRAWING - REFER ONLY TO FIGURED DIMENSIONS"),
         (60, 812, "1:250")],
        config=config,
    )
    assert found is None, "a ratio under this wording is not the sheet's scale"


def test_a_sheet_marked_not_to_scale_measures_nothing(tmp_path, config):
    found = _title_block_scale(
        tmp_path, [(60, 800, "SCALE:", 6), (200, 800, "N.T.S.")], config=config
    )
    assert found is None


def test_a_drawing_index_column_is_not_this_sheets_scale(tmp_path, config):
    """A cover sheet's index has SCALE as a column HEADER with a row for every
    sheet in the set - measured on one real cover, 22 ratios stacked beneath it.
    Read as a cell label, the first became 'this sheet's scale' with the highest
    confidence the reader can give, on a sheet that draws nothing at all."""
    index = [(147, 128, "SCALE", 6)]
    for step, ratio in enumerate(["1:200", "1:100", "1:100", "1:50", "1:50", "1:50"]):
        index.append((151, 182 + step * 17, ratio))
    assert _title_block_scale(tmp_path, index, config=config) is None


def test_an_unlabelled_ratio_by_the_left_edge_is_a_caption(tmp_path, config):
    """On a real details sheet a '1:20' printed under one detail, on a sheet whose
    other drawings are marked NTS, would otherwise become the whole sheet's scale."""
    assert _title_block_scale(tmp_path, [(64, 344, "1:20")], config=config) is None


def test_a_title_block_listing_two_scales_takes_the_first(tmp_path, config):
    """A plan at 1:100 with an enlarged detail beside it prints both, and the
    first applies to the drawing itself. A drafting convention, not a guess."""
    found = _title_block_scale(
        tmp_path, [(60, 800, "SCALE: 1:100, 1:1")], config=config
    )
    assert found is not None and found.ratio == "1:100"


def test_two_weakly_printed_scales_that_disagree_answer_nothing(tmp_path, config):
    """Neither is tied to the sheet more strongly than the other, and picking one
    would put every length on it out by the ratio between them."""
    found = _title_block_scale(
        tmp_path, [(300, 800, "1:100"), (600, 800, "1:50")], config=config
    )
    assert found is None


def test_the_page_says_which_iso_sheet_it_is(tmp_path):
    from pipeline.plan.cvdetect import titleblockscale

    for size, expected in (("a3", "A3"), ("a4", "A4"), ("a1", "A1")):
        path = _sheet_with(tmp_path, [(50, 50, "x")], size, name=f"{size}.pdf")
        document = fitz.open(str(path))
        try:
            assert titleblockscale.page_sheet_size(document[0])[0] == expected
        finally:
            document.close()


def test_a_drawing_reprinted_at_another_size_is_corrected(tmp_path, config):
    """'1:50 @ A3' printed on an A4 page means the drawing was reduced. Left
    uncorrected, every length is out by 1.414 - a 3 m wall reported as 2.1 m."""
    found = _title_block_scale(
        tmp_path, [(60, 560, "SCALE: 1:50 @ A3")], size="a4", config=config
    )
    assert found is not None
    assert found.stated_sheet == "A3"
    assert found.sheet_correction == pytest.approx(420.0 / 297.0, rel=0.01)
    assert found.mm_per_point == pytest.approx(AT_1_TO_100 / 2 * 420.0 / 297.0, rel=0.01)
    assert "re-printed at a different size" in found.note


def test_a_drawing_printed_at_the_size_it_states_is_not_corrected(tmp_path, config):
    found = _title_block_scale(tmp_path, [(60, 800, "SCALE: 1:50 @ A3")], config=config)
    assert found is not None
    assert found.sheet_correction == 1.0
    assert found.mm_per_point == pytest.approx(AT_1_TO_100 / 2, rel=1e-6)


def test_a_sheet_that_cannot_measure_itself_falls_back_and_says_so(tmp_path, config):
    """The whole point of the fallback: a plan set published as pictures has no
    dimension lines to measure against, and without this reports no lengths."""
    from pipeline.plan.cvdetect.calibration import calibrate_scale

    path = _sheet_with(tmp_path, [(60, 800, "SCALE: 1:100 @ A3")])
    document = fitz.open(str(path))
    try:
        scale = calibrate_scale(document[0])
    finally:
        document.close()
    assert scale.usable
    assert scale.source == "printed_scale"
    assert scale.mm_per_point == pytest.approx(AT_1_TO_100, rel=1e-6)
    assert scale.confidence < 0.5, "a printed claim is never as good as a measurement"
    assert "not been verified" in scale.note


def test_a_sheet_stating_nothing_measures_nothing(tmp_path):
    """Critical Rule 5: never guess silently. No scale means no lengths."""
    from pipeline.plan.cvdetect.calibration import calibrate_scale

    path = _sheet_with(tmp_path, [(60, 800, "GENERAL NOTES"), (60, 812, "REFER TO ENGINEER")])
    document = fitz.open(str(path))
    try:
        scale = calibrate_scale(document[0])
    finally:
        document.close()
    assert not scale.usable
    assert scale.mm_per_point == 0.0
    assert scale.source == "not_established"


# --------------------------------------------------------------------------
# Step 4 - sheets whose drawing has no walls in it
# --------------------------------------------------------------------------

def test_no_walls_are_traced_on_a_site_plan(tmp_path, config):
    """Measured on a real site-plan-and-roof-plan sheet, tracing it produced
    124 walls and 400 metres of them - every one a boundary or a batten."""
    path = _sheet_with(tmp_path, [(60, 100, "PROPOSED SITE PLAN"), (60, 800, "SCALE: 1:200 @ A3")])
    document = fitz.open(str(path))
    try:
        assert "Site Plan" in wallgeometry.why_this_sheet_has_no_walls(document[0], config)
    finally:
        document.close()


def test_no_walls_are_traced_on_a_roof_plan(tmp_path, config):
    path = _sheet_with(tmp_path, [(60, 100, "ROOF PLAN"), (60, 800, "SCALE: 1:100 @ A3")])
    document = fitz.open(str(path))
    try:
        assert wallgeometry.why_this_sheet_has_no_walls(document[0], config)
    finally:
        document.close()


def test_a_floor_plan_is_traced(tmp_path, config):
    path = _sheet_with(tmp_path, [(60, 100, "GROUND FLOOR PLAN"), (60, 800, "SCALE: 1:100 @ A3")])
    document = fitz.open(str(path))
    try:
        assert wallgeometry.why_this_sheet_has_no_walls(document[0], config) == ""
    finally:
        document.close()


def test_a_floor_plan_with_a_roof_plan_beside_it_is_still_traced(tmp_path, config):
    """The safe direction: read it and let the geometry decide."""
    path = _sheet_with(
        tmp_path, [(60, 100, "GROUND FLOOR PLAN"), (600, 100, "ROOF PLAN"),
                   (60, 800, "SCALE: 1:100 @ A3")]
    )
    document = fitz.open(str(path))
    try:
        assert wallgeometry.why_this_sheet_has_no_walls(document[0], config) == ""
    finally:
        document.close()


def test_a_level_named_in_a_table_does_not_rescue_a_site_plan(tmp_path, config):
    """A bare 'GROUND FLOOR' matched a site-coverage table on a real site plan -
    'BUILDING SITE COVERAGE (AREA M2) | GROUND FLOOR | HOUSE: 182.66 M2' - and
    rescued the very sheet the rule exists to stop."""
    path = _sheet_with(
        tmp_path,
        [(60, 100, "PROPOSED SITE PLAN"),
         (60, 300, "BUILDING SITE COVERAGE (AREA M2)"),
         (60, 320, "GROUND FLOOR   HOUSE: 182.66 M2"),
         (60, 800, "SCALE: 1:200 @ A3")],
    )
    document = fitz.open(str(path))
    try:
        assert wallgeometry.why_this_sheet_has_no_walls(document[0], config)
    finally:
        document.close()


# --------------------------------------------------------------------------
# The adapter that puts the reader into the pipeline
# --------------------------------------------------------------------------

def test_the_wall_reader_is_a_setting(config):
    from pipeline.plan import cvwalls

    assert cvwalls.reader_name({"walls": {"reader": "cvdetect"}}) == "cvdetect"
    assert cvwalls.reader_name({"walls": {"reader": "legacy"}}) == "legacy"
    # A config that says nothing, or is not a mapping, must not take a run down.
    assert cvwalls.reader_name({}) == "cvdetect"
    assert cvwalls.reader_name({"walls": None}) == "cvdetect"


def test_the_adapter_measures_nothing_without_a_confirmed_scale():
    """A length that cannot be trusted is worse than no length at all."""
    from pipeline.plan import cvwalls

    assert cvwalls.detect_walls({}, {"usable_for_measurement": False}, {}, "S1") == []
    assert cvwalls.detect_walls({}, {"usable_for_measurement": True}, {}, "S1", page=None) == []


def test_collinear_pieces_are_rejoined_with_the_break_between_them(config):
    """The inverse of Step 3. A wall with a door in it arrives as two collinear
    pieces of the same thickness, and the space between them is where the
    opening was - so joining them and recording the space restores the record
    the opening evidence reader works from."""
    from pipeline.plan import cvwalls

    mm_per_point = AT_1_TO_100
    door_pt = 820.0 / mm_per_point
    spans = [
        {"runs_along": "x", "position_pt": 100.0, "start_pt": 0.0, "end_pt": 80.0,
         "thickness_mm": 90.0, "length_mm": 80.0 * mm_per_point, "confidence": 0.8,
         "measured_from": "test", "drawn_as": "outline"},
        {"runs_along": "x", "position_pt": 100.2, "start_pt": 80.0 + door_pt, "end_pt": 200.0,
         "thickness_mm": 94.0, "length_mm": 50.0 * mm_per_point, "confidence": 0.8,
         "measured_from": "test", "drawn_as": "outline"},
    ]
    walls = cvwalls._rejoin_across_openings(spans, mm_per_point, config)
    assert len(walls) == 1, "two pieces of one wall are one wall"
    assert len(walls[0]["gaps_pt"]) == 1, "and the door between them is a break"


def test_two_different_walls_on_one_line_are_not_joined(config):
    """A 90 mm partition and a 230 mm external wall running along the same line
    are two walls, and merging them would report a break where the building
    simply changes thickness."""
    from pipeline.plan import cvwalls

    mm_per_point = AT_1_TO_100
    spans = [
        {"runs_along": "x", "position_pt": 100.0, "start_pt": 0.0, "end_pt": 80.0,
         "thickness_mm": 90.0, "length_mm": 80.0 * mm_per_point, "confidence": 0.8,
         "measured_from": "test", "drawn_as": "outline"},
        {"runs_along": "x", "position_pt": 100.0, "start_pt": 100.0, "end_pt": 200.0,
         "thickness_mm": 230.0, "length_mm": 100.0 * mm_per_point, "confidence": 0.8,
         "measured_from": "test", "drawn_as": "outline"},
    ]
    assert len(cvwalls._rejoin_across_openings(spans, mm_per_point, config)) == 2


def test_a_centreline_round_a_corner_becomes_two_walls(config, scale):
    """The canonical record says a wall runs along x or y, because that is what
    every later stage measures against. A skeleton traced round a corner is one
    polyline with a bend in it."""
    from shapely.geometry import LineString

    from pipeline.plan import cvwalls

    wall = wallgeometry.Wall(
        element_id="W-001",
        centreline=LineString([(0, 100), (200, 100), (200, 300)]),
        outline=None, thickness_mm=90.0, length_mm=1000.0,
        source_sheet="t", source_bbox=[0, 0, 1, 1],
        extraction_method="test", confidence=0.8,
    )
    spans = cvwalls._straight_spans(wall, scale)
    assert len(spans) == 2
    assert {s["runs_along"] for s in spans} == {"x", "y"}


def test_a_canonical_wall_carries_what_the_pipeline_reads(config):
    """The adapter's whole job: everything downstream - the overlay, the model,
    the CSVs, the junction graph - reads these fields."""
    from pipeline.plan import cvwalls

    mm_per_point = AT_1_TO_100
    spans = [{
        "runs_along": "x", "position_pt": 100.0, "start_pt": 0.0, "end_pt": 200.0,
        "thickness_mm": 90.0, "length_mm": 200.0 * mm_per_point, "confidence": 0.8,
        "measured_from": "test", "drawn_as": "outline",
    }]
    wall = cvwalls._rejoin_across_openings(spans, mm_per_point, config)[0]
    for field in (
        "wall_id", "runs_along", "length_mm", "thickness_mm", "start_point_pt",
        "end_point_pt", "face_positions_pt", "bbox", "gaps_pt", "confidence",
        "confidence_band", "review_status", "meets_another_wall", "linked_opening_marks",
    ):
        assert field in wall, f"the pipeline reads {field} off every wall"


# --------------------------------------------------------------------------
# Face-level breaks, and the cost of thinning
# --------------------------------------------------------------------------

def test_a_break_is_read_from_the_faces_of_a_wall(config, scale):
    """Closing joins a wall to the jambs, the leaf and the swing arc drawn
    inside its own doorway, so the band comes out continuous and the break is
    gone before anything can look for it. Both faces stopping together is still
    readable in the line work at this point."""
    from pipeline.plan.cvdetect import breaks

    thickness_pt = 230.0 / scale.mm_per_point
    door_pt = 900.0 / scale.mm_per_point
    rulings = {"h": [], "v": [], "h_widths": [], "v_widths": []}
    for position in (100.0, 100.0 + thickness_pt):
        rulings["h"].append((position, 0.0, 300.0))
        rulings["h"].append((position, 300.0 + door_pt, 800.0))
        rulings["h_widths"].extend([0.5, 0.5])

    found = breaks.shared_gaps(rulings, scale, config)
    assert len(found) == 1, "one pair of faces"
    gaps = found[0]["gaps"]
    assert len(gaps) == 1, "both faces stop together, so that is one break"
    low, high = gaps[0]
    assert (high - low) * scale.mm_per_point == pytest.approx(900.0, abs=200.0)
    assert found[0]["thickness_mm"] == pytest.approx(230.0, abs=30.0)


def test_a_break_in_one_face_alone_is_not_an_opening(config, scale):
    """A break in one face is a cupboard line, a bench, a change of material.
    A door goes through the wall, so both faces stop."""
    from pipeline.plan.cvdetect import breaks

    thickness_pt = 230.0 / scale.mm_per_point
    door_pt = 900.0 / scale.mm_per_point
    rulings = {"h": [], "v": [], "h_widths": [], "v_widths": []}
    rulings["h"].append((100.0, 0.0, 300.0))
    rulings["h"].append((100.0, 300.0 + door_pt, 800.0))
    rulings["h"].append((100.0 + thickness_pt, 0.0, 800.0))
    rulings["h_widths"].extend([0.5, 0.5, 0.5])

    assert breaks.shared_gaps(rulings, scale, config) == []


def test_a_break_needs_wall_on_both_sides_of_it(config, scale):
    """A gap running to the end of a wall is where the drawing stopped, or
    where this wall meets another - not a door."""
    from pipeline.plan.cvdetect import breaks

    thickness_pt = 230.0 / scale.mm_per_point
    rulings = {"h": [], "v": [], "h_widths": [], "v_widths": []}
    for position in (100.0, 100.0 + thickness_pt):
        # A 1,058 mm gap, but only 176 mm of wall left beyond it - less than
        # the 300 mm an opening has to have on both sides of it.
        rulings["h"].append((position, 0.0, 300.0))
        rulings["h"].append((position, 330.0, 335.0))
        rulings["h_widths"].extend([0.5, 0.5])

    assert breaks.shared_gaps(rulings, scale, config) == []


def test_nothing_is_read_without_a_scale(config):
    from pipeline.plan.cvdetect import breaks

    unmeasured = Scale(mm_per_point=0.0, dpi=300.0)
    assert breaks.shared_gaps({"h": [(1, 0, 9)], "v": []}, unmeasured, config) == []


def test_a_sprawling_component_is_thinned_at_a_reduced_resolution(config):
    """The walls of a building are one connected network, so its component
    sprawls: measured on one real sheet, all its components' boxes added to
    18.7 megapixels on an 8.7 megapixel sheet, and thinning took 14.1 of that
    stage's 15 seconds."""
    thinnest_px = 16.0
    assert wallgeometry._thinning_divisor((500, 500), thinnest_px, config) == 1
    big = wallgeometry._thinning_divisor((4000, 4000), thinnest_px, config)
    assert big > 1, "a box far over the budget must be reduced"


def test_the_reduction_never_takes_a_wall_below_a_few_pixels(config):
    """Thinning a line rather than a band gives a skeleton that wanders instead
    of running down the middle of the wall."""
    # A wall only 4 pixels across cannot be reduced at all, however large the box.
    assert wallgeometry._thinning_divisor((9000, 9000), 4.0, config) == 1


def test_a_face_pairs_gaps_are_injected_onto_the_matching_centreline(config):
    """Direct injection: the morphology cannot be relied on to leave a doorway
    open, so the gaps are read off the two drawn faces and written straight onto
    the wall that runs along the same line."""
    from pipeline.plan import cvwalls

    mm_per_point = AT_1_TO_100
    half = (230.0 / mm_per_point) / 2.0
    wall = {
        "runs_along": "x", "thickness_mm": 230.0, "gaps_pt": [],
        "start_point_pt": [0.0, 100.0], "end_point_pt": [800.0, 100.0],
        "face_positions_pt": [100.0 - half, 100.0 + half],
    }
    pair = {
        "axis": "h", "position": 100.0, "thickness_mm": 230.0,
        "face_positions": [100.0 - half, 100.0 + half],
        "start": 0.0, "end": 800.0,
        "gaps": [(300.0, 330.0)],
    }
    cvwalls._inject_face_gaps([wall], [pair], mm_per_point, config)
    assert wall["gaps_pt"] == [[300.0, 330.0]]


def test_a_gap_is_clipped_to_the_wall_it_is_written_on(config):
    """A wall traced shorter than its faces must not claim a gap beyond its end."""
    from pipeline.plan import cvwalls

    mm_per_point = AT_1_TO_100
    half = (230.0 / mm_per_point) / 2.0
    wall = {
        "runs_along": "x", "thickness_mm": 230.0, "gaps_pt": [],
        "start_point_pt": [0.0, 100.0], "end_point_pt": [305.0, 100.0],
        "face_positions_pt": [100.0 - half, 100.0 + half],
    }
    pair = {
        "axis": "h", "position": 100.0, "thickness_mm": 230.0,
        "face_positions": [100.0 - half, 100.0 + half],
        "start": 0.0, "end": 800.0, "gaps": [(300.0, 400.0)],
    }
    cvwalls._inject_face_gaps([wall], [pair], mm_per_point, config)
    assert wall["gaps_pt"] == [], "clipped to 176 mm, under the smallest opening"


def test_a_pair_on_a_different_line_is_not_injected(config):
    """Two walls of a building are a room apart, not a thickness."""
    from pipeline.plan import cvwalls

    mm_per_point = AT_1_TO_100
    half = (230.0 / mm_per_point) / 2.0
    wall = {
        "runs_along": "x", "thickness_mm": 230.0, "gaps_pt": [],
        "start_point_pt": [0.0, 100.0], "end_point_pt": [800.0, 100.0],
        "face_positions_pt": [100.0 - half, 100.0 + half],
    }
    far = {
        "axis": "h", "position": 400.0, "thickness_mm": 230.0,
        "face_positions": [400.0 - half, 400.0 + half],
        "start": 0.0, "end": 800.0, "gaps": [(300.0, 330.0)],
    }
    cvwalls._inject_face_gaps([wall], [far], mm_per_point, config)
    assert wall["gaps_pt"] == []


def test_a_pair_whose_run_misses_the_wall_is_not_injected(config):
    from pipeline.plan import cvwalls

    mm_per_point = AT_1_TO_100
    half = (230.0 / mm_per_point) / 2.0
    wall = {
        "runs_along": "x", "thickness_mm": 230.0, "gaps_pt": [],
        "start_point_pt": [0.0, 100.0], "end_point_pt": [200.0, 100.0],
        "face_positions_pt": [100.0 - half, 100.0 + half],
    }
    elsewhere = {
        "axis": "h", "position": 100.0, "thickness_mm": 230.0,
        "face_positions": [100.0 - half, 100.0 + half],
        "start": 500.0, "end": 900.0, "gaps": [(600.0, 640.0)],
    }
    cvwalls._inject_face_gaps([wall], [elsewhere], mm_per_point, config)
    assert wall["gaps_pt"] == []


# --------------------------------------------------------------------------
# Collinear merging of traced runs
# --------------------------------------------------------------------------

def _run(points, thickness=None):
    import numpy as np

    entry = {
        "points": np.array(points, dtype=np.int32),
        "fill_share": 0.5,
        "drawn_as": "outline",
    }
    if thickness is not None:
        entry["thickness_mm"] = thickness
    return entry


def test_collinear_pieces_of_one_wall_become_one_run(config, scale):
    """A 14.7 m wall on a sheet read as a picture arrives as a dozen pieces,
    and each one dies at the length floor before anything can join them."""
    import numpy as np

    distance = np.full((400, 4000), 6.0, dtype=np.float32)
    pieces = [_run([[0, 200], [400, 200]]), _run([[600, 201], [1200, 201]])]
    merged = wallgeometry._merge_collinear_runs(pieces, distance, scale, config)
    assert len(merged) == 1
    points = merged[0]["points"]
    assert int(points[:, 0].min()) == 0 and int(points[:, 0].max()) == 1200


def test_two_walls_on_different_lines_are_not_merged(config, scale):
    """Two walls of a building are a room apart, not a few points."""
    import numpy as np

    distance = np.full((900, 4000), 6.0, dtype=np.float32)
    pieces = [_run([[0, 200], [400, 200]]), _run([[600, 800], [1200, 800]])]
    assert len(wallgeometry._merge_collinear_runs(pieces, distance, scale, config)) == 2


def test_a_run_that_bends_is_left_alone(config, scale):
    """It is already more than one straight run, and joining it to anything
    would invent a corner."""
    import numpy as np

    distance = np.full((900, 900), 6.0, dtype=np.float32)
    bent = _run([[0, 100], [400, 100], [400, 500]])
    merged = wallgeometry._merge_collinear_runs([bent], distance, scale, config)
    assert len(merged) == 1
    assert len(merged[0]["points"]) == 3, "the bend is preserved"


def test_merging_can_be_switched_off(config, scale):
    import numpy as np

    off = cv_settings._deep_merge(config, {"wall": {"merge_collinear_runs": False}})
    distance = np.full((400, 4000), 6.0, dtype=np.float32)
    pieces = [_run([[0, 200], [400, 200]]), _run([[600, 201], [1200, 201]])]
    assert len(wallgeometry._merge_collinear_runs(pieces, distance, scale, off)) == 2


def test_thickness_comes_from_the_paired_faces_where_there_is_one(config, scale):
    """Closing joins a wall to whatever ink is drawn against it, so the band is
    wider than the wall - eleven of one sheet's longest walls were thrown away
    as 322 to 779 mm thick. The faces measure the real thickness."""
    run = {"axis": "h", "position": scale.px_from_pt(100.0), "start": 0.0, "end": 1000.0}
    pairs = [{
        "axis": "h", "position": 100.0, "thickness_mm": 230.0,
        "start": 0.0, "end": 300.0, "face_positions": [97.0, 103.0], "gaps": [],
    }]
    assert wallgeometry._thickness_from_the_faces(run, pairs, scale, config) == 230.0


def test_no_pair_means_the_band_keeps_its_own_measurement(config, scale):
    run = {"axis": "h", "position": scale.px_from_pt(100.0), "start": 0.0, "end": 1000.0}
    far = [{
        "axis": "h", "position": 400.0, "thickness_mm": 230.0,
        "start": 0.0, "end": 300.0, "face_positions": [397.0, 403.0], "gaps": [],
    }]
    assert wallgeometry._thickness_from_the_faces(run, far, scale, config) is None
    assert wallgeometry._thickness_from_the_faces(run, [], scale, config) is None


# --------------------------------------------------------------------------
# A setting has to be changeable on a server that is already running
# --------------------------------------------------------------------------

def test_the_wall_reader_can_be_changed_without_a_restart(tmp_path, monkeypatch):
    """The config was read once and cached for the life of the process, so
    switching the wall reader meant restarting - and on a hosted Space a restart
    wipes the disk and every plan being read."""
    import json

    from pipeline.plan import cvwalls, reading

    original = reading.WALL_CONFIG_PATH.read_text(encoding="utf-8")
    try:
        assert cvwalls.reader_name(reading.load_config()) in ("legacy", "cvdetect")
        for wanted in ("cvdetect", "legacy", "cvdetect"):
            settings = json.loads(reading.WALL_CONFIG_PATH.read_text(encoding="utf-8"))
            settings["reader"] = wanted
            reading.WALL_CONFIG_PATH.write_text(
                json.dumps(settings, indent=2) + "\n", encoding="utf-8"
            )
            assert cvwalls.reader_name(reading.load_config()) == wanted
    finally:
        reading.WALL_CONFIG_PATH.write_text(original, encoding="utf-8")
        reading.load_config()


def test_an_untouched_config_is_not_re_read(tmp_path):
    """It is called once per sheet, so re-reading three JSON files every time
    would be real work. The same dict comes back while nothing has changed, so
    anything holding a reference to it keeps working."""
    from pipeline.plan import reading

    first = reading.load_config()
    assert reading.load_config() is first


def test_the_detection_settings_reload_too(tmp_path):
    import json

    from pipeline.plan.cvdetect import settings as cv

    original = cv.CONFIG_PATH.read_text(encoding="utf-8")
    try:
        before = cv.number(cv.load_settings(), "wall.min_length_mm", 600.0)
        changed = json.loads(original)
        changed.setdefault("wall", {})["min_length_mm"] = before + 123.0
        cv.CONFIG_PATH.write_text(json.dumps(changed, indent=2) + "\n", encoding="utf-8")
        assert cv.number(cv.load_settings(), "wall.min_length_mm", 0.0) == before + 123.0
    finally:
        cv.CONFIG_PATH.write_text(original, encoding="utf-8")
        cv.load_settings()


# --------------------------------------------------------------------------
# Pre-processing: what never reaches the wall tracing
# --------------------------------------------------------------------------

def test_printed_text_is_kept_out_of_the_wall_tracing(tmp_path, config):
    """A room name set in capitals is a continuous run of dark pixels, so the
    top and bottom of the word are two parallel lines a plausible wall
    thickness apart - 32 of one floor plan's 157 walls were printed words."""
    from pipeline.plan.cvdetect import vectorpaths as vp
    from pipeline.plan.cvdetect import wallgeometry as wg

    path = _sheet_with(tmp_path, [(200, 400, "KITCHEN", 14), (200, 500, "BED 2", 14)])
    document = fitz.open(str(path))
    try:
        page = document[0]
        scale = Scale(mm_per_point=AT_1_TO_100, dpi=150.0, origin=(page.rect.x0, page.rect.y0))
        mask = wg._noise_to_strip(page, scale, vp.parse_paths(page, config), config)
        assert mask is not None, "the sheet's own words must be masked out"
        assert mask.any(), "and the mask must actually cover them"
    finally:
        document.close()


def test_stripping_can_be_switched_off(tmp_path, config):
    from pipeline.plan.cvdetect import vectorpaths as vp
    from pipeline.plan.cvdetect import wallgeometry as wg

    off = cv_settings._deep_merge(config, {"noise": {"strip_text_and_swings": False}})
    path = _sheet_with(tmp_path, [(200, 400, "KITCHEN", 14)])
    document = fitz.open(str(path))
    try:
        page = document[0]
        scale = Scale(mm_per_point=AT_1_TO_100, dpi=150.0, origin=(page.rect.x0, page.rect.y0))
        assert wg._noise_to_strip(page, scale, vp.parse_paths(page, off), off) is None
    finally:
        document.close()


def test_a_thin_line_threshold_can_be_stated_in_paper_millimetres(config):
    """AS 1100 states line widths in millimetres of paper, so an office
    thinking in those terms sets the figure that way."""
    paths = vectorpaths.VectorPaths()
    # 0.25 mm on the paper is 0.71 pt. A 0.5 pt line is under it; a 1.0 pt line is not.
    for index in range(20):
        paths.segments.append(vectorpaths.Segment(0, index, 900, index, 0.50, False, index))
    for index in range(20):
        paths.segments.append(vectorpaths.Segment(0, 100 + index, 900, 100 + index, 1.00, False, index))
    in_mm = cv_settings._deep_merge(config, {"noise": {"thin_line_min_mm": 0.25}})
    vectorpaths._mark_thin(paths, in_mm)
    assert all(s.role == "thin" for s in paths.segments if s.width == 0.50)
    assert all(s.role == "structural" for s in paths.segments if s.width == 1.00)


def test_an_australian_wall_is_not_thicker_than_three_hundred(config):
    """A brick-veneer external wall is 230 to 270 mm and a cavity wall about
    290; anything wider is a band the closing joined to something else."""
    assert cv_settings.number(config, "wall.max_thickness_mm", 0.0) == 300.0
