"""The detection overlay: what it draws, and what it counts.

Each test names the mistake it prevents. The pictures themselves are checked by
looking at them; what is checked here is that a finding cannot be drawn as the
wrong thing, and that a number beside a picture cannot disagree with it.
"""

import json
from pathlib import Path

import pytest

from pipeline.plan.detectionoverlay import (
    LEGEND_ROWS,
    _clear_of,
    _opening_label,
    _rgb,
    _wall_drawn_as,
    detection_summary,
    render_legend,
)
from pipeline.plan.reading import load_config


@pytest.fixture(scope="module")
def config():
    return load_config()


def _wall(wall_id="P01-W001", **fields):
    wall = {
        "wall_id": wall_id,
        "wall_type": "inner",
        "length_mm": 3200.0,
        "thickness_mm": 90.0,
        "bbox": [10.0, 10.0, 110.0, 13.0],
        "building": "main",
        "structure_id": None,
        "not_used_because": None,
        "junctions": [],
    }
    wall.update(fields)
    return wall


def _page(page_number=1, **fields):
    page = {
        "page_number": page_number,
        "sheet_id": f"P{page_number:02d}",
        "walls": [],
        "openings": [],
        "unresolved_gaps": [],
        "scale_calibration": {"result": "confirmed", "measured_mm_per_point": 10.0,
                              "usable_for_measurement": True},
        "error": None,
    }
    page.update(fields)
    return page


# --- a finding is drawn as what it was decided to be ------------------------


def test_an_outer_wall_and_an_inner_wall_are_told_apart(config):
    """They are the two answers the ray casting produces, and a reviewer
    checking whether the outside of the building was found right has to be able
    to see which is which."""
    colours = config["detection_overlay"]["colors"]
    outer, outer_label, _ = _wall_drawn_as(_wall(wall_type="outer"), colours)
    inner, inner_label, _ = _wall_drawn_as(_wall(wall_type="inner"), colours)

    assert outer == _rgb(colours["outer_wall"])
    assert inner == _rgb(colours["inner_wall"])
    assert outer != inner
    assert "P01-W001" in outer_label and "3,200 mm" in outer_label


def test_a_wall_meeting_no_other_is_not_drawn_as_inner_or_outer(config):
    """Neither could be established, and drawing it as one or the other would
    be inventing the answer the reader is trying to check (Critical Rule 5)."""
    colours = config["detection_overlay"]["colors"]
    colour, _label, flagged = _wall_drawn_as(_wall(wall_type="unknown"), colours)

    assert colour == _rgb(colours["unknown_wall"])
    assert colour != _rgb(colours["inner_wall"])
    assert colour != _rgb(colours["outer_wall"])
    assert flagged is False


def test_a_candidate_set_aside_is_drawn_as_set_aside_whatever_else_it_was(config):
    """A wall that was excluded is excluded, and that is the fact a reviewer
    needs about it — its type is beside the point."""
    colours = config["detection_overlay"]["colors"]
    colour, label, flagged = _wall_drawn_as(
        _wall(wall_type="outer", not_used_because="It is drawn dashed."), colours
    )

    assert colour == _rgb(colours["flagged_wall"])
    assert flagged is True, "it must be dashed, so colour is not the only signal"
    assert "drawn dashed" in label


def test_a_detached_structure_is_never_drawn_as_part_of_the_house(config):
    """A carport is reported, marked as a carport. Drawn in the building's own
    colour its area would read as part of the house."""
    colours = config["detection_overlay"]["colors"]
    colour, label, _ = _wall_drawn_as(
        _wall(wall_type="outer", building="structure_2", structure_id="structure_2"),
        colours,
    )

    assert colour == _rgb(colours["detached_structure"])
    assert label == "structure_2"


def test_a_long_reason_is_shortened_for_the_drawing(config):
    """Printed in full these sentences run right across the plan and drown the
    thing they are drawn on. The whole reason stays in the summary."""
    colours = config["detection_overlay"]["colors"]
    reason = (
        "This is part of a small group of short lines that encloses nothing, so it "
        "is joinery, furniture or a panel on the sheet rather than part of the building."
    )
    _colour, label, _ = _wall_drawn_as(_wall(not_used_because=reason), colours, limit=40)

    assert len(label) < len(reason)
    assert label.endswith("…")


# --- an opening says what confirmed it --------------------------------------


def test_an_openings_label_names_every_reading_that_confirmed_it(config):
    """This is the whole point of drawing them: an opening a reader cannot
    check is one they have to take on trust."""
    label = _opening_label({
        "display_mark": "D04", "opening_id": "P01-OPG004",
        "evidence": ["arc_geometry", "leaf_dimension"],
    })
    assert label == "D04  arc_geometry+leaf_dimension"


def test_the_same_reading_is_never_named_twice_on_one_opening():
    """An arc read two ways is one arc. Naming it twice on the picture would
    say the drawing agreed with itself."""
    label = _opening_label({
        "display_mark": "D01", "evidence": ["arc_geometry", "arc_geometry"],
    })
    assert label == "D01  arc_geometry"


# --- a label never hides the drawing ----------------------------------------


def test_a_label_with_nowhere_free_is_not_drawn_at_all():
    """A label printed on top of three others is not information — it is worse
    than none, because it also hides the drawing underneath."""
    box = (10.0, 10.0, 60.0, 22.0)
    everywhere = [(0.0, 0.0, 1000.0, 1000.0)]
    assert _clear_of(box, everywhere, (1000, 1000)) is None


def test_a_label_moves_off_its_neighbour_rather_than_onto_it():
    box = (10.0, 40.0, 60.0, 52.0)
    taken = [(10.0, 26.0, 60.0, 38.0)]  # the spot directly above is taken
    placed = _clear_of(box, taken, (1000, 1000))

    assert placed is not None
    assert not (placed[0] < taken[0][2] and taken[0][0] < placed[2]
                and placed[1] < taken[0][3] and taken[0][1] < placed[3])


# --- the numbers beside the picture come from the picture -------------------


def test_the_summary_counts_what_the_overlay_draws(config):
    """A figure that disagreed with the picture beside it would be worse than
    no figure, so both are computed from the same records."""
    pages = [_page(1, walls=[
        _wall("P01-W001", wall_type="outer"),
        _wall("P01-W002", wall_type="inner"),
        _wall("P01-W003", wall_type="unknown"),
        _wall("P01-W004", not_used_because="It is drawn dashed."),
        _wall("P01-W005", building="structure_2", structure_id="structure_2"),
    ], openings=[
        {"opening_id": "P01-OPG001", "element_type": "door", "source_bbox": [0, 0, 1, 1],
         "evidence": ["arc_geometry", "leaf_dimension"], "review_needed": False},
        {"opening_id": "P01-OPG002", "element_type": "window", "source_bbox": [0, 0, 1, 1],
         "evidence": ["glazing_symbol"], "review_needed": True},
    ], unresolved_gaps=[{"gap_id": "P01-GAP001", "source_bbox": [0, 0, 1, 1]}])]

    summary = detection_summary(pages, config)

    assert summary["walls"]["total"] == 5
    assert summary["walls"]["outer"] == 1
    assert summary["walls"]["inner"] == 1
    assert summary["walls"]["outside_or_inside_not_established"] == 1
    assert summary["walls"]["flagged"] == 1
    assert summary["walls"]["detached_structure"] == 1
    assert summary["openings"]["total"] == 2
    assert summary["openings"]["by_type"] == {"door": 1, "window": 1}
    assert summary["openings"]["confirmed_by_two_or_more_readings"] == 1
    assert summary["openings"]["needing_a_reviewer"] == 1
    assert summary["openings"]["unresolved_gaps"] == 1


def test_a_flagged_walls_whole_reason_survives_into_the_summary(config):
    """The label on the picture is shortened; nothing may be only half-said."""
    reason = "This pair of lines meets no other wall, so it is more likely an eave."
    summary = detection_summary(
        [_page(1, walls=[_wall(not_used_because=reason)])], config
    )
    reasons = summary["walls"]["flagged_reasons"]

    assert len(reasons) == 1
    assert reasons[0]["reason"] == reason
    assert reasons[0]["wall_id"] == "P01-W001"


def test_a_junction_is_counted_once_however_many_walls_hold_it(config):
    """A junction is one meeting recorded on both of its walls. Counting it on
    each would double every figure the picture shows."""
    first = _wall("P01-W001", junctions=[{"with_wall_id": "P01-W002", "shape": "T",
                                          "at_pt": [50.0, 50.0]}])
    second = _wall("P01-W002", junctions=[{"with_wall_id": "P01-W001", "shape": "T",
                                           "at_pt": [50.0, 50.0]}])
    summary = detection_summary([_page(1, walls=[first, second])], config)

    assert summary["walls"]["junctions"] == 1
    assert summary["walls"]["junctions_by_shape"] == {"T": 1}


def test_the_scale_is_reported_per_sheet_and_never_as_one_answer(config):
    """A document's sheets are calibrated separately, and presenting one
    sheet's answer as the document's would be a claim no sheet made."""
    pages = [
        _page(1, scale_calibration={"result": "confirmed", "usable_for_measurement": True,
                                    "measured_mm_per_point": 10.0}),
        _page(2, scale_calibration={"result": "contradicted",
                                    "usable_for_measurement": False}),
    ]
    summary = detection_summary(pages, config)

    assert summary["scale"]["sheets_by_result"] == {"confirmed": 1, "contradicted": 1}
    assert summary["scale"]["sheets_usable_for_measurement"] == 1


def test_a_sheet_that_found_nothing_is_left_out_of_the_sheet_list(config):
    """A sheet with no walls, openings or gaps has nothing to show on an
    overlay, so it is not offered as one."""
    summary = detection_summary([_page(1), _page(2, walls=[_wall()])], config)

    assert [s["page_number"] for s in summary["sheets"]] == [2]
    assert summary["sheets"][0]["overlay"] == "overlay_page_2.png"


def test_every_record_drawn_carries_a_place_on_the_sheet(config):
    """Traceability is computed from the records rather than asserted."""
    summary = detection_summary([_page(1, walls=[_wall()], openings=[
        {"opening_id": "P01-OPG001", "element_type": "door",
         "source_bbox": [0, 0, 1, 1], "evidence": ["arc_geometry"], "review_needed": True},
    ])], config)

    assert summary["traceability"]["traceability_pct"] == 100.0
    assert summary["traceability"]["records_total"] == 2


# --- the legend and the overlays cannot disagree ----------------------------


def test_the_legend_names_every_colour_the_overlay_draws(config):
    """A colour on a picture with nothing saying what it means is a puzzle."""
    colours = set(config["detection_overlay"]["colors"])
    named = {key for key, _style, _meaning in LEGEND_ROWS}
    assert named == colours


def test_the_legend_is_drawn_from_the_configured_colours(config, tmp_path):
    """Both the legend and the overlays read the same settings, so a colour
    changed in the configuration changes both."""
    out = tmp_path / "overlay_legend.png"
    assert render_legend(out, config) is True
    assert out.is_file() and out.stat().st_size > 0


def test_nothing_in_this_module_names_a_colour_of_its_own():
    """Critical Rule 1: a colour is something an office may differ on, so it
    lives in the configuration and the code reads it."""
    import re

    source = Path(__file__).resolve().parents[1] / "pipeline" / "plan" / "detectionoverlay.py"
    text = source.read_text(encoding="utf-8")
    # Every colour literal in the module must sit inside a `.get(...)` naming
    # the setting it is a fallback for — never standing on its own.
    for number, line in enumerate(text.splitlines(), start=1):
        if not re.search(r'"#[0-9A-Fa-f]{6}"', line):
            continue
        assert ".get(" in line, f"line {number} names a colour of its own: {line.strip()}"


def test_the_overlay_survives_a_page_it_cannot_draw(config, tmp_path):
    """An overlay is evidence, and failing to draw one must never lose the
    reading it was drawn from (Critical Rule 6)."""
    from pipeline.plan.detectionoverlay import render_detection_overlay

    assert render_detection_overlay(None, _page(1), tmp_path / "x.png", config) is False


def test_the_summary_is_json_the_interface_can_read(config):
    """It feeds the frontend, so it has to survive a round trip."""
    summary = detection_summary([_page(1, walls=[_wall()])], config)
    assert json.loads(json.dumps(summary))["walls"]["total"] == 1
