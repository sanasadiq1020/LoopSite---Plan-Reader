"""Day 5 — regression tests for the canonical model and its 3D exports.

Each test names the mistake it prevents. Two of them lock down failures that
are invisible when they happen: a mirrored building and a model built from a
scale that was never confirmed both look entirely convincing.
"""

import json
import struct

import pytest

from pipeline.model.canonical import build_model, choose_default_sheet, modellable_sheets
from pipeline.model.exporters import wall_pieces, write_glb, write_ifc, write_obj
from pipeline.model.height import resolve_storey_height
from pipeline.plan.reading import load_config


@pytest.fixture(scope="module")
def config():
    return load_config()


def _page(**overrides):
    """A minimal read sheet: one 5 m wall running east, at a confirmed scale."""
    page = {
        "page_number": 1,
        "sheet_id": "A02",
        "title_block": {
            "sheet_number": {"value": "A02"},
            "sheet_title": {"value": "FLOOR PLAN"},
        },
        "page_type": {"value": "floor_plan", "draws_a_plan": True},
        "scale_calibration": {
            "usable_for_measurement": True,
            "measured_mm_per_point": 10.0,
            "result": "confirmed",
        },
        "rooms": [],
        "dimensions": [],
        "openings": [],
        "walls": [
            {
                "wall_id": "A02-W001",
                "runs_along": "x",
                "length_mm": 5000.0,
                "thickness_mm": 90.0,
                "nominal_thickness_mm": 90.0,
                "matches_nominal_thickness": True,
                "start_point_pt": [100.0, 200.0],
                "end_point_pt": [600.0, 200.0],
                "bbox": [100.0, 195.0, 600.0, 205.0],
                "confidence": 0.9,
                "confidence_band": "high",
                "review_status": "needs_review",
                "line_source": "vector",
                "linked_opening_marks": [],
                "longer_than_sheet_measures": False,
                "gaps_pt": [],
            }
        ],
    }
    page.update(overrides)
    return page


_HEIGHT = {
    "value_mm": 2700.0,
    "source": "printed_on_a_section",
    "confidence": 0.8,
    "note": "test",
}


# --- what may be modelled -------------------------------------------------


def test_a_sheet_with_no_confirmed_scale_is_not_offered(config):
    """A model built from an unconfirmed scale is wrong by one constant factor
    and looks perfectly convincing — Week 1's first automatic failure."""
    page = _page(
        scale_calibration={
            "usable_for_measurement": False,
            "result": "contradicted",
            "note": "the strings do not agree.",
        }
    )
    sheet = modellable_sheets([page], config)[0]
    assert sheet["can_be_modelled"] is False
    assert "measured" in sheet["reason"]


def test_a_sheet_that_draws_no_plan_is_not_offered(config):
    page = _page(page_type={"value": "elevation", "draws_a_plan": False})
    sheet = modellable_sheets([page], config)[0]
    assert sheet["can_be_modelled"] is False
    assert "in plan" in sheet["reason"]


def test_the_sheet_with_the_most_walls_is_offered_first(config):
    """A plan set draws the same outline as a floor plan, a ceiling plan and an
    electrical plan. The reader wants the floor plan."""
    floor = _page()
    floor["walls"] = floor["walls"] * 2
    ceiling = _page(page_number=2, sheet_id="A05")
    sheets = modellable_sheets([ceiling, floor], config)
    assert choose_default_sheet(sheets) == floor["page_number"]


# --- the model itself -----------------------------------------------------


def test_a_wall_is_measured_in_millimetres_from_the_buildings_own_corner(config):
    model = build_model(_page(), _HEIGHT, config, "run", "plan.pdf", 800.0)
    assert model["units"] == "millimetres"
    wall = model["walls"][0]
    # The wall starts at the origin because it is the only thing on the sheet.
    assert wall["geometry"]["start_mm"] == [0.0, 0.0]
    assert wall["geometry"]["end_mm"] == [5000.0, 0.0]
    assert wall["dimensions"]["height_mm"] == 2700.0


def test_the_page_downward_y_is_turned_over(config):
    """A PDF's Y grows downward. Left alone, the model is a mirror image of
    the plan — and a mirrored building looks completely convincing."""
    page = _page()
    base = page["walls"][0]
    page["walls"] = [
        dict(
            base,
            wall_id="north",
            start_point_pt=[100.0, 100.0],
            end_point_pt=[600.0, 100.0],
        ),
        dict(
            base,
            wall_id="south",
            start_point_pt=[100.0, 300.0],
            end_point_pt=[600.0, 300.0],
        ),
    ]
    model = build_model(page, _HEIGHT, config, "run", "plan.pdf", 800.0)
    north = next(w for w in model["walls"] if w["from_wall_id"] == "north")
    south = next(w for w in model["walls"] if w["from_wall_id"] == "south")
    # Higher up the page must end up further north, not further south.
    assert north["geometry"]["start_mm"][1] > south["geometry"]["start_mm"][1]


def test_every_element_carries_the_fields_the_rules_require(config):
    """Critical Rule 12 — without these a wall in the 3D model cannot be
    traced back to the lines it was measured from."""
    model = build_model(_page(), _HEIGHT, config, "run", "plan.pdf", 800.0)
    wall = model["walls"][0]
    for field in (
        "element_id",
        "element_type",
        "storey",
        "geometry",
        "dimensions",
        "source_sheet",
        "source_bbox",
        "extraction_method",
        "confidence",
        "review_status",
        "linked_issue_ids",
    ):
        assert field in wall, f"{field} is required on every canonical element"
    assert wall["source_sheet"] == "A02"
    assert wall["source_bbox"] == [100.0, 195.0, 600.0, 205.0]


def test_an_assumed_thickness_is_recorded_as_an_assumption(config):
    page = _page()
    page["walls"][0]["thickness_mm"] = None
    model = build_model(page, _HEIGHT, config, "run", "plan.pdf", 800.0)
    wall = model["walls"][0]
    assert wall["dimensions"]["thickness_is_measured"] is False
    assert any("default" in note for note in wall["assumptions"])


def test_a_sheet_with_no_usable_scale_refuses_to_build(config):
    page = _page(scale_calibration={"usable_for_measurement": False})
    with pytest.raises(ValueError):
        build_model(page, _HEIGHT, config, "run", "plan.pdf", 800.0)


# --- the storey height ----------------------------------------------------


def test_two_agreeing_sources_confirm_the_height(config):
    """The figure the sections dimension, and the gap between the printed
    levels, are two independent readings of the same building."""
    pages = [
        {
            "page_type": {"value": "section"},
            "dimensions": [
                {"kind": "linear", "value_mm": 2700.0},
                {"kind": "linear", "value_mm": 2700.0},
            ],
            "sheet_id": "A08",
        },
        {
            "page_type": {"value": "floor_plan"},
            "dimensions": [
                {"kind": "level", "value_mm": 100400.0, "level_reference": "FFL"},
                {"kind": "level", "value_mm": 103100.0, "level_reference": "RL"},
            ],
            "sheet_id": "A02",
        },
    ]
    height = resolve_storey_height(pages, config)
    assert height["value_mm"] == 2700.0
    assert height["source"] == "confirmed_by_the_drawing"
    assert height["confidence"] > 0.9


def test_a_plan_that_states_no_height_says_so(config):
    """The default must never be presented as if it were measured."""
    height = resolve_storey_height(
        [{"page_type": {"value": "floor_plan"}, "dimensions": [], "sheet_id": "A02"}],
        config,
    )
    assert height["source"] == "office_default"
    assert height["confidence"] < 0.5
    assert "assumption" in height["note"]


def test_a_figure_printed_once_is_not_the_storey_height(config):
    """A single figure in the height range is a window head or a door."""
    pages = [
        {
            "page_type": {"value": "section"},
            "dimensions": [{"kind": "linear", "value_mm": 2340.0}],
            "sheet_id": "A08",
        }
    ]
    assert resolve_storey_height(pages, config)["source"] == "office_default"


# --- the 3D files ---------------------------------------------------------


def test_a_wall_becomes_a_box_of_the_right_size(config):
    model = build_model(_page(), _HEIGHT, config, "run", "plan.pdf", 800.0)
    boxes = wall_pieces(model["walls"][0], [])
    assert len(boxes) == 1, "a wall with no openings in it is one box"
    corners = boxes[0]
    assert len(corners) == 8
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    zs = [c[2] for c in corners]
    assert round(max(xs) - min(xs)) == 5000  # length
    assert round(max(ys) - min(ys)) == 90  # thickness
    assert round(max(zs) - min(zs)) == 2700  # height


def test_the_glb_is_a_valid_file_with_one_named_node_per_wall(tmp_path, config):
    model = build_model(_page(), _HEIGHT, config, "run", "plan.pdf", 800.0)
    path = tmp_path / "m.glb"
    assert write_glb(model, path)

    raw = path.read_bytes()
    assert raw[:4] == b"glTF"
    version, total = struct.unpack("<II", raw[4:12])
    assert version == 2
    assert total == len(raw), "the declared length must match the file"

    json_length = struct.unpack("<I", raw[12:16])[0]
    assert raw[16:20] == b"JSON"
    gltf = json.loads(raw[20 : 20 + json_length])
    assert [node["name"] for node in gltf["nodes"]] == [model["walls"][0]["element_id"]]

    binary_length = struct.unpack("<I", raw[20 + json_length : 24 + json_length])[0]
    assert gltf["buffers"][0]["byteLength"] == binary_length
    for view in gltf["bufferViews"]:
        assert view["byteOffset"] + view["byteLength"] <= binary_length


def test_the_obj_names_every_wall(tmp_path, config):
    model = build_model(_page(), _HEIGHT, config, "run", "plan.pdf", 800.0)
    path = tmp_path / "m.obj"
    assert write_obj(model, path)
    text = path.read_text(encoding="utf-8")
    assert f"g {model['walls'][0]['element_id']}" in text
    assert text.count("\nv ") == 8
    assert text.count("\nf ") == 12


def test_the_ifc_is_a_building_measured_in_millimetres(tmp_path, config):
    ifcopenshell = pytest.importorskip("ifcopenshell")
    model = build_model(_page(), _HEIGHT, config, "run", "plan.pdf", 800.0)
    path = tmp_path / "m.ifc"
    assert write_ifc(model, path)

    ifc = ifcopenshell.open(str(path))
    assert ifc.schema == "IFC4"
    walls = ifc.by_type("IfcWallStandardCase")
    assert [w.Name for w in walls] == [model["walls"][0]["element_id"]]
    length_unit = next(u for u in ifc.by_type("IfcSIUnit") if u.UnitType == "LENGTHUNIT")
    assert (length_unit.Prefix, length_unit.Name) == (
        "MILLI",
        "METRE",
    ), "a silent unit change is the failure Week 1 names first"
    assert ifc.by_type("IfcBuildingStorey"), "walls have to sit on a storey"


# --- doors and windows cut into the walls ---------------------------------


def _opening(**overrides):
    """A door 1 m along the test wall, 900 wide, from a schedule."""
    opening = {
        "opening_id": "A02-OP001",
        "mark": "D1",
        "element_type": "door",
        "wall_id": "A02-W001",
        "wall_note": None,
        "position_on_wall": {
            "start_fraction": 0.1,
            "end_fraction": 0.28,
            "centre_fraction": 0.19,
            "from_wall_start_mm": 950.0,
            "width_mm": 900.0,
            "measured_from": "break_in_the_wall",
        },
        "width_mm": 900.0,
        "height_mm": 2040.0,
        "sill_height_mm": 0.0,
        "head_height_mm": 2040.0,
        "found_by": "text_label",
        "source_sheet": "A02",
        "source_bbox": [150.0, 195.0, 240.0, 205.0],
        "confidence": 0.9,
        "confidence_band": "high",
        "review_status": "needs_review",
        "in_schedule": True,
    }
    opening.update(overrides)
    return opening


def test_a_door_is_cut_as_a_void_and_leaves_three_pieces_of_wall(config):
    """A door reaching the floor leaves wall either side of it and a lintel
    over it — and nothing under it."""
    model = build_model(
        _page(openings=[_opening()]), _HEIGHT, config, "run", "plan.pdf", 800.0
    )
    opening = model["openings"][0]
    assert opening["geometry"]["cut_as_void"] is True
    assert opening["not_cut_because"] is None
    assert opening["dimensions"]["height_source"] == "schedule"

    boxes = wall_pieces(model["walls"][0], model["openings"])
    assert len(boxes) == 3, "wall, lintel, wall"
    heights = sorted(round(max(c[2] for c in box) - min(c[2] for c in box)) for box in boxes)
    assert heights == [660, 2700, 2700], "the lintel is what is left above the door"


def test_a_window_also_leaves_the_wall_under_its_sill(config):
    window = _opening(
        mark="W1", element_type="window", height_mm=1200.0, sill_height_mm=900.0
    )
    model = build_model(
        _page(openings=[window]), _HEIGHT, config, "run", "plan.pdf", 800.0
    )
    boxes = wall_pieces(model["walls"][0], model["openings"])
    assert len(boxes) == 4, "wall, the piece under the sill, the lintel, wall"
    heights = sorted(round(max(c[2] for c in box) - min(c[2] for c in box)) for box in boxes)
    assert heights == [600, 900, 2700, 2700]


def test_the_hole_is_where_the_plan_puts_it_along_the_wall(config):
    model = build_model(
        _page(openings=[_opening()]), _HEIGHT, config, "run", "plan.pdf", 800.0
    )
    opening = model["openings"][0]
    # The wall runs 5 m east from the building's own south-west corner, and
    # the opening sits 19% of the way along it.
    assert round(opening["geometry"]["centre_mm"][0]) == 950
    assert round(opening["geometry"]["offset_along_wall_mm"]) == 950


def test_an_opening_whose_kind_the_drawing_never_states_is_not_cut(config):
    """A plan is a horizontal cut, so it shows no height. Where nothing says
    whether this is a door or a window, no hole is invented."""
    unknown = _opening(
        mark="", element_type=None, height_mm=None, sill_height_mm=None,
        head_height_mm=None, found_by="glazing_symbol", in_schedule=False,
    )
    model = build_model(
        _page(openings=[unknown]), _HEIGHT, config, "run", "plan.pdf", 800.0
    )
    opening = model["openings"][0]
    assert opening["geometry"]["cut_as_void"] is False
    assert "door or a window" in opening["not_cut_because"]
    assert len(wall_pieces(model["walls"][0], model["openings"])) == 1


def test_an_opening_with_no_schedule_uses_the_office_default_and_says_so(config):
    """Where the kind is known but no schedule gives a size, the office default
    is used — and the opening carries it as an assumption, never silently."""
    described = _opening(
        mark="", height_mm=None, sill_height_mm=None, head_height_mm=None,
        found_by="glazing_symbol", in_schedule=False,
    )
    model = build_model(
        _page(openings=[described]), _HEIGHT, config, "run", "plan.pdf", 800.0
    )
    opening = model["openings"][0]
    assert opening["geometry"]["cut_as_void"] is True
    assert opening["dimensions"]["height_source"] == "office_default"
    assert any("office default" in a for a in opening["assumptions"])


def test_an_opening_taller_than_the_storey_is_reported_rather_than_cut(config):
    tall = _opening(height_mm=4000.0, head_height_mm=4000.0)
    model = build_model(_page(openings=[tall]), _HEIGHT, config, "run", "plan.pdf", 800.0)
    opening = model["openings"][0]
    assert opening["geometry"]["cut_as_void"] is False
    assert "storey height" in opening["not_cut_because"]


def test_the_glb_still_parses_once_a_wall_has_a_hole_in_it(tmp_path, config):
    model = build_model(
        _page(openings=[_opening()]), _HEIGHT, config, "run", "plan.pdf", 800.0
    )
    path = tmp_path / "m.glb"
    assert write_glb(model, path)
    raw = path.read_bytes()
    assert raw[:4] == b"glTF"
    _version, total = struct.unpack("<II", raw[4:12])
    assert total == len(raw)

    length = struct.unpack("<I", raw[12:16])[0]
    gltf = json.loads(raw[20 : 20 + length].decode("utf-8"))
    # Three boxes, one wall: still one node, so a click still names one wall.
    assert len(gltf["nodes"]) == 1
    assert gltf["accessors"][0]["count"] == 24


# --- outside, inside, and what is built into what -------------------------


def test_the_model_says_which_walls_face_the_weather(config):
    """Which side of the building a wall is on decides its cladding, its
    insulation and its bracing. Read once from the drawing's geometry, it has
    to reach the model — recomputing it later from the model's own boxes would
    be a second source of truth for the same fact (Critical Rule 2)."""
    page = _page()
    page["walls"][0]["wall_type"] = "outer"
    model = build_model(page, _HEIGHT, config, "run", "plan.pdf", 800.0)

    assert model["walls"][0]["wall_type"] == "outer"


def test_a_junction_reaches_the_model_as_element_ids(config):
    """The plan calls a wall A02-W001 and the model calls it A02-M-W001.
    Leaving the sheet's own ids in the model would make every stage after this
    one hold both vocabularies and join them by guesswork."""
    page = _page()
    base = page["walls"][0]
    page["walls"] = [
        dict(base, wall_id="A02-W001", connects_to=["A02-W002"]),
        dict(
            base,
            wall_id="A02-W002",
            runs_along="y",
            start_point_pt=[600.0, 200.0],
            end_point_pt=[600.0, 500.0],
            length_mm=3000.0,
            connects_to=["A02-W001"],
        ),
    ]
    model = build_model(page, _HEIGHT, config, "run", "plan.pdf", 800.0)
    by_id = {wall["element_id"]: wall for wall in model["walls"]}

    for wall in model["walls"]:
        assert wall["connects_to"], "a junction read on the plan was dropped"
        for other in wall["connects_to"]:
            assert other in by_id, "a wall points at an element that is not in the model"


def test_a_junction_with_a_wall_left_out_of_the_model_is_dropped(config):
    """A wall too short to build with, or one meeting nothing, is not modelled.
    Its neighbours must not be left pointing at a wall that is not there."""
    page = _page()
    page["walls"][0]["connects_to"] = ["A02-W099"]
    model = build_model(page, _HEIGHT, config, "run", "plan.pdf", 800.0)

    assert model["walls"][0]["connects_to"] == []
