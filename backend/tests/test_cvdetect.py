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
