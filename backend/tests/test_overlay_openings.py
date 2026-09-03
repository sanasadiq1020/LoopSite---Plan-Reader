"""How an opening is shown on the marked-up sheet.

Display only: nothing here decides what an opening is or where it goes. Each
test names what a reader could not do before it.
"""

import json

import pytest

from app.paths import CONFIG_DIR
from pipeline.plan.overlay import _collect_marks, _what_kind_of_opening


def _sheet(openings, walls=None):
    return {
        "sheet_id": "A02",
        "page_number": 4,
        "title_block": {},
        "rooms": [],
        "dimensions": [],
        "dimension_chains": [],
        "schedules": [],
        "legends": [],
        "walls": walls or [],
        "openings": openings,
        "sheet_index": None,
        "unresolved_items": [],
    }


def _opening(**overrides):
    base = {
        "opening_id": "A02-OP001",
        "mark": "D2",
        "display_mark": "D2",
        "element_type": "door",
        "width_mm": 820.0,
        "height_mm": 2340.0,
        "wall_id": None,
        "position_on_wall": None,
        "source_bbox": [100.0, 200.0, 108.0, 208.0],
        "confidence_band": "high",
    }
    base.update(overrides)
    return base


def _openings_in(marks):
    return [m for m in marks if len(m) > 4 and m[4] == "opening"]


def test_the_sheet_shows_the_opening_id_and_nothing_else():
    """The size used to be on the label — a pair of numbers over a drawing that
    already prints its own dimensions. It is a column in the table; the sheet
    only has to say which opening this is."""
    marks = _openings_in(_collect_marks(_sheet([_opening()])))

    assert len(marks) == 1
    assert marks[0][2] == "D2"
    assert "820" not in marks[0][2] and "×" not in marks[0][2]


def test_an_opening_the_drawing_never_named_still_carries_a_name():
    marks = _openings_in(
        _collect_marks(_sheet([_opening(mark="", display_mark="W01")]))
    )
    assert marks[0][2] == "W01"


def test_a_door_a_window_and_an_unnamed_opening_are_drawn_apart():
    """Which is which can be seen without reading a single label."""
    assert _what_kind_of_opening({"element_type": "door"}) == "door"
    assert _what_kind_of_opening({"element_type": "sliding_window"}) == "window"
    assert _what_kind_of_opening({"element_type": "fixed_window"}) == "window"
    assert _what_kind_of_opening({"element_type": "highlight_window"}) == "window"
    assert _what_kind_of_opening({"element_type": "unknown_opening"}) == "opening"
    assert _what_kind_of_opening({"element_type": None}) == "opening"


def test_the_three_colours_are_the_ones_the_key_shows():
    colours = json.loads(
        (CONFIG_DIR / "plan_reading.json").read_text(encoding="utf-8")
    )["overlay"]["colors"]
    assert colours["door"].upper() == "#FFD700"
    assert colours["window"].upper() == "#6B21A8"
    assert colours["opening"].upper() == "#9CA3AF"
    # And the walls keep the colour they had.
    assert colours["wall"] == "#7c3aed"


def test_the_box_goes_on_the_opening_not_on_its_printed_mark():
    """A mark is printed beside the door, often inside the room on a leader. A
    box round the mark says where the lettering is; what a reviewer is checking
    is whether the hole is in the right place."""
    wall = {
        "wall_id": "A02-W001",
        "runs_along": "x",
        "start_point_pt": [400.0, 300.0],
        "end_point_pt": [900.0, 300.0],
        "thickness_mm": 90.0,
        "bbox": [400.0, 295.0, 900.0, 305.0],
        "face_positions_pt": [295.0, 305.0],
        "meets_another_wall": True,
        "length_mm": 5000.0,
        "confidence_band": "high",
        "matches_nominal_thickness": True,
        "linked_opening_marks": [],
    }
    opening = _opening(
        wall_id="A02-W001",
        position_on_wall={
            "start_fraction": 0.2, "end_fraction": 0.4, "centre_fraction": 0.3,
            "from_wall_start_mm": 1000.0, "width_mm": 820.0,
            "measured_from": "break_in_the_wall",
        },
    )
    marks = _openings_in(_collect_marks(_sheet([opening], walls=[wall])))

    assert len(marks) == 1, "one opening is drawn once, on the place it occupies"
    box = marks[0][1]
    assert 490 <= box[0] <= 510, "it sits a fifth of the way along the wall"
    assert box != opening["source_bbox"], "not on the printed mark"


def test_an_opening_never_placed_still_appears_where_its_mark_is():
    """Somewhere on the sheet is better than nowhere: a reader can still find
    the row it belongs to."""
    marks = _openings_in(_collect_marks(_sheet([_opening()])))
    assert marks[0][1] == [100.0, 200.0, 108.0, 208.0]
