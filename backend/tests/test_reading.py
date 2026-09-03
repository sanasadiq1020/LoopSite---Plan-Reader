"""Day 3 — regression tests for the plan-reading pipeline.

Almost every case here is taken from a real failure measured against the two
supplied Australian plan sets, which use deliberately different title-block
conventions. Each test names the failure it locks down, so a future change
that reintroduces it fails here rather than in a run nobody checks.

Fixtures are built as text lines in PDF points, the same shape
``textmodel.build_page_lines`` produces, so the detectors are tested through
their real interfaces rather than through mocks.
"""

import json

import pytest

from pipeline.plan import accuracy
from pipeline.plan import dimensions as dimensions_module
from pipeline.plan import openings as openings_module
from pipeline.plan.openings import merge_opening_evidence
from pipeline.plan import reading, rooms as rooms_module, schedules, textmodel, validators
from pipeline.plan.layout import extract_rulings, joined_text, value_candidates
from pipeline.plan.sheetindex import cross_check_pages, parse_sheet_index
from pipeline.plan.titleblock import detect_title_block, sheet_id_for
from pipeline.plan.walls import detect_walls

PAGE_W, PAGE_H = 1190.0, 842.0  # A3 landscape, points
NO_RULINGS = {"h": [], "v": []}

# A confirmed 1:100 sheet: one point of paper is 25.4/72 mm of paper, so a
# hundred times that on the building.
_CALIBRATED = {"usable_for_measurement": True, "measured_mm_per_point": 25.4 / 72.0 * 100}


def line(text, bbox, axis="horizontal", size=8.0, method="native", confidence=1.0):
    return textmodel.make_line(text, bbox, method, confidence, axis, size)


@pytest.fixture(scope="module")
def config():
    return reading.load_config()


# --- textmodel ------------------------------------------------------------


def test_overprinted_placeholder_loses_to_the_real_value():
    """These plans print a '-' template then overprint the real revision on
    top. Which one won used to depend on iteration order."""
    kept, conflicts = textmodel.resolve_overprints(
        [line("-", [995, 805, 999, 818]), line("A", [995, 805, 1002, 818])]
    )
    assert [item["text"] for item in kept] == ["A"]
    assert conflicts == []


def test_two_different_real_values_at_one_place_are_both_kept_and_flagged():
    kept, conflicts = textmodel.resolve_overprints(
        [line("15/12/2015", [995, 776, 1034, 787]), line("02/11/2015", [995, 776, 1034, 787])]
    )
    assert len(kept) == 2
    assert conflicts and set(conflicts[0]["values"]) == {"15/12/2015", "02/11/2015"}


def test_ocr_boxes_are_converted_from_image_pixels_to_pdf_points():
    """OCR runs on a 150 DPI render; everything else works in points. Without
    this conversion the two can never be compared."""
    converted = textmodel.convert_ocr_blocks(
        [{"text": "BED 2", "bbox": [150, 300, 250, 320], "confidence": 0.9}], dpi=150
    )
    assert converted[0]["bbox"] == [72.0, 144.0, 120.0, 153.6]


def test_exact_duplicates_are_removed_and_counted():
    kept, removed = textmodel.deduplicate(
        [line("3,600", [10, 10, 40, 18]), line("3,600", [10, 10, 40, 18])]
    )
    assert len(kept) == 1 and removed == 1


# --- validators -----------------------------------------------------------


def test_a_date_is_never_accepted_as_a_scale():
    """The measured Day 3 failure: a blank scale cell reported the drawing
    date from the next column as the sheet's scale."""
    assert validators.validate_scale("15/12/2015") is None


def test_scale_keeps_the_ratio_and_records_the_sheet_size_separately():
    value, note = validators.validate_scale("1:100 @ A3")
    assert value == "1:100"
    assert "A3" in note


def test_not_to_scale_is_a_real_value_not_a_missing_one():
    value, _ = validators.validate_scale("N.T.S.")
    assert value == "NTS"


def test_a_sheet_may_legitimately_be_drawn_at_more_than_one_scale():
    """A plan at 1:100 with an enlarged detail beside it at 1:1 prints both.
    Both are kept, and the first is the one the drawing itself is at."""
    value, note = validators.validate_scale("1:100, 1:1 @ A3")
    assert value == "1:100, 1:1"
    assert validators.scale_ratios(value) == [100, 1]
    assert validators.scale_ratio_denominator(value) == 100
    assert "more than one scale" in note


def test_not_to_scale_has_no_ratio_to_measure_with():
    assert validators.scale_ratios("NTS") == []
    assert validators.scale_ratio_denominator("NTS") is None


def test_a_dash_is_not_a_revision():
    """'-' means no revision has been issued; reporting it as the revision
    presents a placeholder as data."""
    assert validators.validate_revision("-") is None
    assert validators.validate_revision("A") == ("A", None)


def test_sheet_number_strips_the_of_n_sheet_count():
    value, note = validators.validate_sheet_number("A02 of 20")
    assert value == "A02"
    assert "20" in note


def test_sheet_number_accepts_a_hyphenated_job_style_code():
    assert validators.validate_sheet_number("D-00-03")[0] == "D-00-03"


def test_sheet_number_rejects_a_date():
    assert validators.validate_sheet_number("15/12/2015") is None


# --- layout: label to value ----------------------------------------------


def test_value_is_read_from_the_cell_below_its_label():
    label = line("Scale:", [1050, 768, 1067, 776])
    lines = [label, line("1:100 @ A3", [1050, 776, 1091, 787])]
    candidates = value_candidates(label, lines, NO_RULINGS, PAGE_W, PAGE_H, {"SCALE"})
    assert joined_text(candidates[0]["lines"]) == "1:100 @ A3"


def test_an_empty_cell_never_borrows_the_neighbouring_columns_value():
    """A cover sheet leaves 'Scale' blank. The old distance-based match took
    the date from the next column, 44pt away, and reported it as the scale."""
    label = line("Scale:", [1050, 768, 1067, 776])
    lines = [
        label,
        line("Date:", [995, 768, 1009, 776]),
        line("15/12/2015", [995, 776, 1034, 787]),
    ]
    candidates = value_candidates(
        label, lines, NO_RULINGS, PAGE_W, PAGE_H, {"SCALE", "DATE"}
    )
    assert candidates == []


def test_a_value_wrapped_over_two_lines_is_joined():
    label = line("Drawing:", [778, 768, 803, 776])
    lines = [
        label,
        line("REFLECTED CEILING", [778, 784, 878, 798], size=9.7),
        line("PLAN", [778, 795, 804, 809], size=9.7),
    ]
    candidates = value_candidates(label, lines, NO_RULINGS, PAGE_W, PAGE_H, {"DRAWING"})
    assert joined_text(candidates[0]["lines"]) == "REFLECTED CEILING PLAN"


def test_a_wrapped_abbreviation_keeps_its_number_attached():
    assert (
        joined_text(
            [line("WINDOW SCHEDULE SHT.", [778, 784, 900, 798]), line("1", [778, 795, 783, 809])]
        )
        == "WINDOW SCHEDULE SHT.1"
    )


def test_value_is_read_from_the_cell_beside_its_label():
    """The second supplied plan writes label and value side by side."""
    label = line("REV NO", [227, 777, 244, 785])
    lines = [label, line("1", [263, 777, 266, 785])]
    candidates = value_candidates(label, lines, NO_RULINGS, PAGE_W, PAGE_H, {"REV NO"})
    assert candidates[0]["technique"] == "label_value_right"
    assert joined_text(candidates[0]["lines"]) == "1"


# --- title block ----------------------------------------------------------


def _title_block(lines, config, page_number=4, page_count=23):
    return detect_title_block(
        lines, NO_RULINGS, PAGE_W, PAGE_H, config, page_number, page_count
    )["fields"]


def test_a_project_number_is_never_reported_as_a_sheet_number(config):
    """Six sheets sharing 'D-00-03' is a job number, not a sheet number.
    Reporting it as one made every sheet look identically identified."""
    fields = _title_block(
        [
            line("PROJECT NO", [144, 777, 173, 785]),
            line("D-00-03", [180, 777, 199, 785]),
        ],
        config,
    )
    assert fields["project_number"]["value"] == "D-00-03"
    assert fields["sheet_number"]["value"] is None


def test_a_sheet_with_no_drawing_number_is_still_identifiable(config):
    """The user-facing rule: the 3rd sheet is sheet 3, even when the drawing
    prints no number at all."""
    fields = _title_block([], config, page_number=3, page_count=6)
    assert fields["sheet_position"]["value"] == "Page 3 of 6"
    assert fields["sheet_position"]["technique"] == "derived_from_page_order"
    assert sheet_id_for(fields, 3) == ("P03", "page_order")


def test_a_printed_drawing_number_is_preferred_over_the_page_ordinal(config):
    fields = _title_block(
        [line("Drawing No:", [1050, 797, 1083, 805]), line("A02 of 20", [1050, 802, 1102, 818])],
        config,
    )
    assert fields["sheet_number"]["value"] == "A02"
    assert sheet_id_for(fields, 4) == ("A02", "printed_sheet_number")


def test_a_scale_cell_left_blank_reports_not_detected_rather_than_the_date(config):
    fields = _title_block(
        [
            line("Project No:", [926, 768, 955, 776]),
            line("14-66", [926, 776, 946, 787]),
            line("Date:", [995, 768, 1009, 776]),
            line("15/12/2015", [995, 776, 1034, 787]),
            line("Scale:", [1050, 768, 1067, 776]),
        ],
        config,
    )
    assert fields["scale"]["value"] is None
    assert fields["issue_date"]["value"] == "15/12/2015"


def test_a_sheet_titled_door_schedule_keeps_its_title(config):
    """The exclusion list used to reject any title containing 'SCHEDULE',
    losing four sheets' titles on the supplied set."""
    fields = _title_block(
        [line("Drawing:", [778, 768, 803, 776]), line("DOOR SCHEDULE SHT.1", [778, 784, 892, 798])],
        config,
    )
    assert fields["sheet_title"]["value"] == "DOOR SCHEDULE SHT.1"


def test_a_title_with_no_label_is_found_by_its_drawing_type_phrase(config):
    fields = _title_block(
        [line("NORTH WING | FLOOR PLAN", [144, 713, 329, 733], size=14.0)], config
    )
    assert fields["sheet_title"]["value"] == "NORTH WING | FLOOR PLAN"
    assert fields["sheet_title"]["technique"] == "title_keyword"


def test_a_printed_page_number_that_disagrees_with_the_pdf_is_flagged(config):
    """A mismatch means the supplied file is incomplete or out of order —
    something the reader must be told, not smoothed over."""
    fields = _title_block(
        [line("PAGE", [371, 777, 383, 785]), line("2", [402, 777, 405, 785])],
        config,
        page_number=4,
        page_count=6,
    )
    assert fields["sheet_position"]["value"] == "Page 2 of 6"
    assert fields["sheet_position"]["review_status"] == "needs_review"
    assert "may be incomplete" in fields["sheet_position"]["note"]


def test_a_field_is_not_read_from_a_label_outside_the_title_block(config):
    """An unseen cover sheet headed its drawing index 'SCALES' and left its own
    scale blank. The blank was rejected and the field then took the index
    column's value — a scale belonging to five other sheets."""
    title_block_labels = [
        line("PROJECT", [1049, 65, 1090, 73]),
        line("SHEET TITLE", [1049, 143, 1100, 151]),
        line("COVER SHEET", [1049, 154, 1100, 162]),
        line("SCALES", [1049, 195, 1080, 203]),
        line("-", [1049, 206, 1052, 214]),
        line("DRAWN BY", [1049, 273, 1090, 281]),
    ]
    index_headers = [
        line("SCALES", [300, 79, 330, 87]),
        line("1:100", [300, 100, 320, 108]),
        line("1:100", [300, 114, 320, 122]),
    ]
    fields = _title_block(title_block_labels + index_headers, config, page_number=1, page_count=5)
    assert fields["scale"]["value"] is None
    assert fields["sheet_title"]["value"] == "COVER SHEET"


# --- rooms ----------------------------------------------------------------


def test_a_room_is_found_from_its_size_callout_without_any_vocabulary(config):
    """'MULTI-FUNCTION' is a real room on the second supplied plan that no
    room-name list would contain."""
    found = rooms_module.detect_rooms(
        [
            line("MULTI-FUNCTION", [306, 290, 381, 309]),
            line("(3,325 x 5,720)", [305, 309, 358, 319]),
        ],
        config,
        "P01",
    )
    assert len(found) == 1
    assert found[0]["name"] == "MULTI-FUNCTION"
    assert found[0]["detection_method"] == "paired_dimension_below_label"
    assert found[0]["floor_area_m2"] == pytest.approx(19.02, abs=0.01)


def test_room_instances_stay_distinct(config):
    """'BED 2' and 'BED 3' are two rooms; collapsing both to 'BED' destroys
    the identity the canonical model needs."""
    found = rooms_module.detect_rooms(
        [line("BED 2", [10, 10, 40, 18]), line("BED 3", [10, 60, 40, 68])], config, "P04"
    )
    assert [(r["normalized_name"], r["instance"]) for r in found] == [("BED", "2"), ("BED", "3")]
    assert len({r["room_id"] for r in found}) == 2


def test_a_paired_figure_that_is_not_room_sized_is_not_a_room(config):
    """A timber section note (90 x 32) and a ceiling hatch (600 x 600) both sit
    above a paired figure, and both were reported as rooms before this check."""
    lines = [
        line("HARDWOOD TIMBER", [100, 100, 180, 110]),
        line("90 x 32", [100, 112, 140, 122]),
        line("ACCESS HATCH DOOR", [300, 100, 390, 110]),
        line("600 x 600", [300, 112, 350, 122]),
    ]
    assert rooms_module.detect_rooms(lines, config, "A02") == []


def test_a_mixed_case_note_is_not_a_room(config):
    """'Skillion roof to carport' contains a room word but is a note."""
    found = rooms_module.detect_rooms(
        [line("Skillion roof to carport", [825, 143, 902, 153])], config, "P01"
    )
    assert found == []


def test_a_name_wrapped_over_two_lines_becomes_one_room(config):
    found = rooms_module.detect_rooms(
        [
            line("MASTER", [730, 310, 761, 329]),
            line("BEDROOM", [730, 327, 768, 346]),
            line("(3,500 x 3,350)", [723, 345, 777, 356]),
        ],
        config,
        "P01",
    )
    assert len(found) == 1
    assert found[0]["name"] == "MASTER BEDROOM"


def test_one_size_callout_is_claimed_by_only_one_room(config):
    """Two labels above one callout would otherwise both report the same
    floor area, double-counting it downstream."""
    found = rooms_module.detect_rooms(
        [
            line("KITCHEN", [369, 459, 405, 478]),
            line("SCULLERY", [369, 440, 405, 455]),
            line("(3,905 x 3,515)", [364, 476, 417, 486]),
        ],
        config,
        "P01",
    )
    with_area = [r for r in found if r["floor_area_m2"] is not None]
    assert len(with_area) == 1


# --- dimensions -----------------------------------------------------------


def detect_dimensions(lines, config, sheet_id="P04"):
    return dimensions_module.detect_dimensions(lines, config, sheet_id)


def test_a_rotated_figure_measures_the_vertical_axis(config):
    """Direction is what tells a wall which way it runs; deriving it from the
    number is impossible."""
    found = detect_dimensions(
        [
            line("11,030", [134, 358, 144, 414], axis="vertical"),
            line("1,550", [297, 598, 316, 608], axis="horizontal"),
        ],
        config,
    )
    assert [d["measures_axis"] for d in found] == ["y", "x"]


def test_a_number_inside_a_product_note_is_not_a_dimension(config):
    found = detect_dimensions([line("100x100 Steel posts", [10, 10, 90, 18])], config)
    assert found == []


def test_a_bare_number_records_the_millimetre_assumption(config):
    found = detect_dimensions([line("3,600", [10, 10, 40, 18])], config)
    assert found[0]["value_mm"] == 3600
    assert found[0]["unit_source"] == "assumed_mm"
    assert found[0]["unit_assumption"]


def test_an_ocr_misread_thousands_separator_still_gives_the_right_length(config):
    """OCR reads the printed "7,370" as "7.370". Both readings — a misread
    comma, or a genuine 7.370 m — are 7370 mm, so the figure is usable either
    way and is no longer dropped."""
    found = detect_dimensions([line("7.370", [10, 10, 40, 18])], config)
    assert len(found) == 1
    assert found[0]["value_mm"] == 7370
    assert found[0]["unit_source"] == "grouped_or_metres"


def test_a_reduced_level_is_read_in_metres(config):
    found = detect_dimensions([line("RL 12.50", [10, 10, 50, 18])], config)
    assert found[0]["kind"] == "level"
    assert found[0]["value_mm"] == 12500.0


def test_a_chain_that_adds_up_to_its_printed_total_passes(config):
    lines = [
        line("3,000", [100, 200, 130, 208]),
        line("2,000", [200, 200, 230, 208]),
        line("1,000", [300, 200, 330, 208]),
        line("6,000 OVERALL", [180, 230, 260, 238]),
    ]
    found = detect_dimensions(lines, config)
    chains = dimensions_module.build_chains(found, config, "P04")
    assert len(chains) == 1
    assert chains[0]["check"]["result"] == "pass"
    assert chains[0]["check"]["sum_of_running_mm"] == 6000


def test_a_chain_that_does_not_add_up_is_reported_not_accepted(config):
    lines = [
        line("3,000", [100, 200, 130, 208]),
        line("2,000", [200, 200, 230, 208]),
        line("6,000 OVERALL", [150, 230, 230, 238]),
    ]
    chains = dimensions_module.build_chains(detect_dimensions(lines, config), config, "P04")
    assert chains[0]["check"]["result"] == "fail"
    assert chains[0]["check"]["variance_pct"] > 0


def test_one_printed_total_is_claimed_by_only_one_chain(config):
    """A short chain elsewhere on the sheet used to be compared against the
    building's full width, inventing a failure."""
    lines = [
        line("3,000", [100, 200, 130, 208]),
        line("2,000", [200, 200, 230, 208]),
        line("1,000", [300, 200, 330, 208]),
        line("6,000 OVERALL", [180, 230, 260, 238]),
        line("400", [110, 500, 130, 508]),
        line("500", [160, 500, 180, 508]),
    ]
    chains = dimensions_module.build_chains(detect_dimensions(lines, config), config, "P04")
    checked = [c for c in chains if c["check"]["result"] != "not_checked"]
    assert len(checked) == 1
    assert checked[0]["check"]["result"] == "pass"


def test_a_dimension_between_two_equally_close_rooms_is_left_unlinked(config):
    """A wrong link is worse than none: every later stage trusts it."""
    room_lines = [line("KITCHEN", [100, 100, 140, 110]), line("DINING", [100, 200, 140, 210])]
    detected_rooms = rooms_module.detect_rooms(room_lines, config, "P04")
    found = detect_dimensions([line("3,600", [110, 150, 140, 158])], config)
    dimensions_module.link_dimensions_to_rooms(found, detected_rooms, config)
    assert found[0]["linked_room_id"] is None
    assert "equally close" in found[0]["link_note"]


def test_a_room_size_callout_is_linked_to_its_room_with_certainty(config):
    lines = [line("KITCHEN", [369, 459, 405, 478]), line("(3,905 x 3,515)", [364, 476, 417, 486])]
    detected_rooms = rooms_module.detect_rooms(lines, config, "P01")
    found = detect_dimensions(lines, config, "P01")
    dimensions_module.link_dimensions_to_rooms(found, detected_rooms, config)
    paired = [d for d in found if d["kind"] == "paired"][0]
    assert paired["linked_room_id"] == detected_rooms[0]["room_id"]
    assert paired["link_method"] == "room_size_callout"


# --- schedules ------------------------------------------------------------


def _transposed_schedule_lines(marks, start_y):
    """One transposed schedule block: attribute names down the left, one
    column per item — the layout the supplied door/window schedules use."""
    lines = [line("ID", [221, start_y, 240, start_y + 8])]
    columns = [360 + index * 130 for index in range(len(marks))]
    for x, mark in zip(columns, marks):
        lines.append(line(mark, [x, start_y, x + 20, start_y + 8]))
    for offset, (attribute, values) in enumerate(
        [
            ("Location", ["ENTRY", "LAUNDRY"]),
            ("Height", ["2,650", "2,340"]),
            ("Width", ["1,400", "820"]),
        ],
        start=1,
    ):
        y = start_y + offset * 10
        lines.append(line(attribute, [221, y, 250, y + 8]))
        for x, value in zip(columns, values):
            lines.append(line(value, [x, y, x + 40, y + 8]))
    return lines


def test_a_transposed_schedule_is_read_as_real_rows(config):
    tables = schedules.detect_schedules(
        _transposed_schedule_lines(["D1", "D2"], 45), config, "A13", "DOOR SCHEDULE SHT.1"
    )
    assert len(tables) == 1
    rows = tables[0]["rows"]
    assert [row["mark"] for row in rows] == ["D1", "D2"]
    assert rows[0]["width_mm"] == 1400 and rows[0]["height_mm"] == 2650
    assert rows[0]["element_type"] == "door"
    assert rows[0]["values"]["location"] == "ENTRY"


def test_two_schedule_blocks_on_one_sheet_stay_separate(config):
    """Reading both blocks as one table merged D6's values into D2's row."""
    lines = _transposed_schedule_lines(["D1", "D2"], 45) + _transposed_schedule_lines(
        ["D6", "D7"], 380
    )
    tables = schedules.detect_schedules(lines, config, "A13", "DOOR SCHEDULE SHT.1")
    assert len(tables) == 2
    assert [row["mark"] for row in tables[0]["rows"]] == ["D1", "D2"]
    assert [row["mark"] for row in tables[1]["rows"]] == ["D6", "D7"]


def test_a_window_schedule_checks_that_sill_plus_height_equals_head(config):
    lines = [line("ID", [221, 45, 240, 53]), line("W1", [360, 45, 380, 53])]
    lines.append(line("W2", [490, 45, 510, 53]))
    for offset, (attribute, values) in enumerate(
        [("Height", ["600", "600"]), ("Window Sill Height", ["1,050", "1,050"]),
         ("Window Head Height", ["1,650", "9,999"])],
        start=1,
    ):
        y = 45 + offset * 10
        lines.append(line(attribute, [221, y, 280, y + 8]))
        for x, value in zip([360, 490], values):
            lines.append(line(value, [x, y, x + 40, y + 8]))
    rows = schedules.detect_schedules(lines, config, "A15", "WINDOW SCHEDULE")[0]["rows"]
    assert rows[0]["geometry_check"]["result"] == "pass"
    assert rows[1]["geometry_check"]["result"] == "fail"
    assert rows[1]["flags"]


def test_the_sheet_footer_is_not_read_as_schedule_data(config):
    """These sheets print the office address in the same column the schedule
    attributes use."""
    lines = _transposed_schedule_lines(["D1", "D2"], 45) + [
        line("42 Example Street, Newtown, Vic., 3220", [219, 768, 339, 776]),
        line("info@exampledrafting.com.au", [219, 788, 282, 796]),
        line("Project No:", [926, 768, 955, 776]),
        line("Date:", [995, 768, 1009, 776]),
    ]
    columns = schedules.detect_schedules(lines, config, "A13", None)[0]["columns"]
    assert all("example" not in column.lower() for column in columns)
    assert all("newtown" not in column.lower() for column in columns)


def test_a_row_per_item_schedule_covers_every_cell_not_just_its_headers(config):
    """The last column's values run past the last header. A table region drawn
    to the headers alone left those values outside it, and the ROOM column was
    read a second time as rooms."""
    lines = [
        line("MARK", [60, 95, 80, 103]),
        line("WIDTH", [220, 95, 250, 103]),
        line("HEIGHT", [290, 95, 320, 103]),
        line("ROOM", [360, 95, 385, 103]),
        line("SD01", [60, 112, 85, 120]),
        line("1,800", [220, 112, 250, 120]),
        line("2,100", [290, 112, 320, 120]),
        line("LOUNGE", [360, 112, 407, 120]),
        line("D02", [60, 126, 80, 134]),
        line("820", [220, 126, 240, 134]),
        line("2,040", [290, 126, 320, 134]),
        line("MASTER SUITE", [360, 126, 420, 134]),
    ]
    table = schedules.detect_schedules(lines, config, "AR-301", None)[0]
    assert [row["mark"] for row in table["rows"]] == ["SD01", "D02"]
    # The region has to reach the far edge of the last column's values.
    assert table["bbox"][2] >= 420


def test_a_mark_matches_its_schedule_row_whatever_the_punctuation(config):
    """One office prints 'W-01' in the schedule and on the plan; the mark
    reader normalises the drawn one to 'W01'. Comparing without normalising
    both sides reported the same window as undrawn and unscheduled at once."""
    page = reading._empty_page(2, "fixture")
    page["sheet_id"] = "AR-101"
    page["openings"] = [
        {
            "opening_id": "AR-101-OP001", "mark": "W01", "element_type": "window",
            "wall_id": None, "wall_note": None, "width_mm": None, "height_mm": None,
            "sill_height_mm": None, "head_height_mm": None, "location_on_plan": None,
            "schedule_sheet": None, "schedule_row_id": None, "in_schedule": False,
            "source_sheet": "AR-101", "source_bbox": [0, 0, 1, 1],
            "confidence": 0.5, "confidence_band": "review", "review_status": "needs_review",
        }
    ]
    schedule_page = reading._empty_page(5, "fixture")
    schedule_page["sheet_id"] = "AR-301"
    schedule_page["schedules"] = [
        {
            "table_id": "AR-301-TBL01", "caption": "SCHEDULE", "caption_source": "x",
            "caption_bbox": None, "orientation": "row_per_item", "bbox": None,
            "columns": [], "row_count": 1, "unassigned_cells": [],
            "rows": [
                {
                    "row_id": "R1", "mark": "W-01", "element_type": "window",
                    "width_mm": 1200.0, "height_mm": 600.0, "values": {},
                    "geometry_check": None, "flags": [], "bbox": None,
                    "confidence": 0.9, "confidence_band": "high",
                    "review_status": "confirmed",
                }
            ],
        }
    ]
    report = openings_module.reconcile_openings_with_schedules([page, schedule_page])
    assert report["matched_to_a_schedule"] == 1
    assert report["scheduled_marks_not_drawn"] == []
    assert page["openings"][0]["width_mm"] == 1200.0


def test_level_prefixes_come_from_config_not_a_fixed_list(config):
    """'F.C.L.' and 'S.S.L.' are listed in config but were missed because the
    pattern was hardcoded."""
    found = detect_dimensions(
        [line("F.C.L. 27.300", [10, 10, 70, 18]), line("S.S.L. 24.100", [10, 30, 70, 38])],
        config,
    )
    assert [d["level_reference"] for d in found] == ["FCL", "SSL"]
    assert [d["value_mm"] for d in found] == [27300.0, 24100.0]


def test_a_legend_is_captured_as_symbol_and_meaning(config):
    lines = [
        line("ABBREVIATIONS:", [35, 393, 90, 401]),
        line("CSD", [35, 404, 48, 412]),
        line("CAVITY SLIDING DOOR", [56, 404, 130, 412]),
        line("CG", [35, 410, 45, 418]),
        line("CLEAR GLAZING", [56, 410, 110, 418]),
    ]
    legends = schedules.detect_legends(lines, config, "A13")
    assert legends[0]["entry_count"] == 2
    assert legends[0]["entries"][0]["symbol"] == "CSD"
    assert legends[0]["entries"][0]["description"] == "CAVITY SLIDING DOOR"


def test_a_legend_quantity_column_is_kept_out_of_the_meaning(config):
    """'1 GANG SWITCH 5' was one string; the 5 is a count, not part of the
    meaning."""
    lines = [
        line("ELECTRICAL LEGEND", [39, 281, 135, 292]),
        line("1G", [60, 303, 70, 311]),
        line("1 GANG SWITCH", [88, 303, 150, 311]),
        line("5", [180, 303, 185, 311]),
        line("2G", [60, 317, 70, 325]),
        line("2 GANG SWITCH", [88, 317, 150, 325]),
        line("3", [180, 317, 185, 325]),
    ]
    entries = schedules.detect_legends(lines, config, "A06")[0]["entries"]
    assert entries[0]["description"] == "1 GANG SWITCH"
    assert entries[0]["quantity"] == "5"


def test_a_wrapped_legend_meaning_stays_with_its_symbol(config):
    lines = [
        line("ABBREVIATIONS:", [35, 229, 98, 239]),
        line("1050", [35, 239, 55, 247]),
        line("DENOTES HEIGHT TO CENTRELINE", [56, 239, 200, 247]),
        line("FROM FFL", [56, 245, 100, 253]),
        line("2W", [35, 252, 48, 260]),
        line("2 WAY LIGHT SWITCH", [56, 252, 150, 260]),
    ]
    entries = schedules.detect_legends(lines, config, "A06")[0]["entries"]
    assert len(entries) == 2
    assert entries[0]["description"] == "DENOTES HEIGHT TO CENTRELINE FROM FFL"


def test_an_adjacent_legend_is_not_absorbed_into_the_one_above_it(config):
    """A shadow key printed under the abbreviations list shares its left
    margin; only the second column separates them."""
    lines = [
        line("ABBREVIATIONS:", [35, 470, 98, 480]),
        line("DP", [35, 487, 45, 495]),
        line("DOWNPIPE", [56, 487, 100, 495]),
        line("RL", [35, 500, 44, 508]),
        line("RELATIVE LEVEL", [56, 500, 120, 508]),
        line("EQUINOX SHADOW", [35, 550, 110, 558]),
        line("9AM SHADOW @ EQUINOX", [92, 568, 200, 576]),
    ]
    entries = schedules.detect_legends(lines, config, "A01")[0]["entries"]
    assert [entry["symbol"] for entry in entries] == ["DP", "RL"]


# --- drawing index and cross-check ---------------------------------------


def _index_lines():
    lines = [
        line("#", [38, 128, 42, 136]),
        line("DRAWING NAME:", [54, 128, 102, 136]),
        line("SCALE", [147, 128, 167, 136]),
        line("REV.", [182, 128, 195, 136]),
    ]
    rows = [
        ("A01", "SITE PLAN", "1:200", "-", 165),
        ("A02", "FLOOR PLAN", "1:100", "A", 182),
        ("A03", "ELEVATIONS SHT.1", "1:100", "A", 199),
    ]
    for number, title, scale, revision, y in rows:
        lines.append(line(number, [32, y, 41, y + 7]))
        lines.append(line(title, [54, y, 120, y + 7]))
        lines.append(line(scale, [151, y, 163, y + 7]))
        lines.append(line(revision, [181, y, 184, y + 7]))
    return lines


def test_the_cover_sheet_drawing_index_is_parsed(config):
    index = parse_sheet_index(_index_lines(), 1, PAGE_W, config)
    assert index is not None
    assert [entry["sheet_number"] for entry in index["entries"]] == ["A01", "A02", "A03"]
    assert index["entries"][1]["sheet_title"] == "FLOOR PLAN"
    assert index["entries"][1]["revision"] == "A"


def test_a_wrapped_index_title_is_joined_to_its_own_row(config):
    lines = _index_lines() + [line("BED ENS. & WIR", [54, 206, 120, 213])]
    index = parse_sheet_index(lines, 1, PAGE_W, config)
    assert index["entries"][-1]["sheet_title"] == "ELEVATIONS SHT.1 BED ENS. & WIR"


def test_a_sheet_agreeing_with_the_index_is_marked_verified(config):
    index = parse_sheet_index(_index_lines(), 1, PAGE_W, config)
    page = reading._empty_page(4, "fixture")
    page["title_block"]["sheet_number"]["value"] = "A02"
    page["title_block"]["sheet_title"]["value"] = "FLOOR PLAN"
    page["title_block"]["sheet_title"]["confidence"] = 0.9
    report = cross_check_pages([page], index, config)
    assert report["agreements"] >= 1
    assert page["title_block"]["sheet_title"]["verified_against_index"] is True


def test_a_sheet_disagreeing_with_the_index_raises_a_finding(config):
    index = parse_sheet_index(_index_lines(), 1, PAGE_W, config)
    page = reading._empty_page(4, "fixture")
    page["unresolved_items"] = []
    page["title_block"]["sheet_number"]["value"] = "A02"
    page["title_block"]["scale"]["value"] = "1:50"
    page["title_block"]["scale"]["confidence"] = 0.9
    report = cross_check_pages([page], index, config)
    assert report["disagreements"] == 1
    assert page["title_block"]["scale"]["verified_against_index"] is False
    assert any(item["category"] == "cross_check.scale" for item in page["unresolved_items"])


def test_a_field_missing_from_a_sheet_is_filled_from_the_index_and_attributed(config):
    index = parse_sheet_index(_index_lines(), 1, PAGE_W, config)
    page = reading._empty_page(4, "fixture")
    page["unresolved_items"] = []
    page["title_block"]["sheet_number"]["value"] = "A02"
    cross_check_pages([page], index, config)
    scale = page["title_block"]["scale"]
    assert scale["value"] == "1:100"
    assert scale["technique"] == "sheet_index"
    assert "drawing index" in scale["note"]


# --- orchestration --------------------------------------------------------


def test_a_full_page_reading_produces_traceable_records(config):
    lines = [
        line("Drawing:", [778, 768, 803, 776]),
        line("FLOOR PLAN", [778, 784, 841, 798], size=9.7),
        line("Drawing No:", [1050, 797, 1083, 805]),
        line("A02 of 20", [1050, 802, 1102, 818], size=11.7),
        line("Scale:", [1050, 768, 1067, 776]),
        line("1:100 @ A3", [1050, 776, 1091, 787]),
        line("KITCHEN", [369, 459, 405, 478]),
        line("(3,905 x 3,515)", [364, 476, 417, 486]),
        line("3,600", [200, 600, 240, 608]),
    ]
    page = reading.analyze_page(4, 23, PAGE_W, PAGE_H, lines, {}, NO_RULINGS)
    assert page["error"] is None
    assert page["sheet_id"] == "A02"
    assert page["page_type"]["value"] == "floor_plan"
    assert page["title_block"]["scale"]["value"] == "1:100"
    assert any(room["name"] == "KITCHEN" for room in page["rooms"])
    for room in page["rooms"]:
        assert room["bbox"]
    for dimension in page["dimensions"]:
        assert dimension["bbox"]


def test_a_page_that_fails_degrades_to_a_flagged_reading_instead_of_crashing():
    """Critical Rule 6: the app never hard-crashes on one bad page."""
    page = reading.analyze_page(7, 23, PAGE_W, PAGE_H, None, {}, NO_RULINGS)
    assert page["error"] is not None
    assert page["page_number"] == 7
    assert page["unresolved_items"][0]["severity"] == "P1"


def test_a_missing_value_is_listed_as_unresolved_rather_than_hidden(config):
    page = reading.analyze_page(9, 23, PAGE_W, PAGE_H, [], {}, NO_RULINGS)
    categories = {item["category"] for item in page["unresolved_items"]}
    assert "title_block.sheet_title" in categories
    assert "title_block.scale" in categories
    assert page["title_block"]["sheet_title"]["value"] is None


def test_metrics_are_computed_from_the_records_not_asserted(config):
    lines = [
        line("Drawing:", [778, 768, 803, 776]),
        line("FLOOR PLAN", [778, 784, 841, 798], size=9.7),
        line("KITCHEN", [369, 459, 405, 478]),
    ]
    page = reading.analyze_page(1, 1, PAGE_W, PAGE_H, lines, {}, NO_RULINGS)
    metrics = reading.compute_metrics([page], {})
    assert metrics["page_count"] == 1
    assert metrics["records"]["rooms"] == 1
    assert metrics["traceability"]["traceability_pct"] == 100.0


# --- Sheets that draw their plan as an image ------------------------------


def _image_drawn_page(tmp_path):
    """A page whose only geometry is a picture of two walls.

    Drawn, rendered to pixels, and re-inserted as an image, so the resulting
    page has no vector line work at all — the shape one supplied plan set has.
    """
    import fitz

    drawing = fitz.open()
    canvas = drawing.new_page(width=600, height=400)
    for offset in (0, 6.52):  # a 230 mm wall at 1:100 is 6.52 pt of paper
        canvas.draw_line(fitz.Point(100, 150 + offset), fitz.Point(500, 150 + offset), width=0.7)
        canvas.draw_line(fitz.Point(100, 300 + offset), fitz.Point(500, 300 + offset), width=0.7)
    pixels = canvas.get_pixmap(matrix=fitz.Matrix(4, 4))
    picture = tmp_path / "plan.png"
    pixels.save(str(picture))

    document = fitz.open()
    page = document.new_page(width=600, height=400)
    page.insert_image(fitz.Rect(0, 0, 600, 400), filename=str(picture))
    return document, page


def test_walls_are_recovered_from_a_sheet_drawn_as_an_image(tmp_path):
    document, page = _image_drawn_page(tmp_path)
    try:
        vector_rulings = extract_rulings(page)
        # The premise: the wall lines are not in the PDF as lines.
        assert not detect_walls(vector_rulings, _CALIBRATED, reading.load_config(), "P01")

        walls = detect_walls(vector_rulings, _CALIBRATED, reading.load_config(), "P01", page=page)
        assert walls, "an image-drawn wall should still be measurable"
        assert all(wall["line_source"] == "rendered_page" for wall in walls)
        longest = max(walls, key=lambda wall: wall["length_mm"])
        # 400 pt at 1:100 is 14.1 m; measured off pixels, within a percent.
        assert 13900 < longest["length_mm"] < 14400
        assert longest["nominal_thickness_mm"] == 230
    finally:
        document.close()


def test_vector_geometry_is_preferred_when_it_works(tmp_path):
    """A sheet with real line work is never re-read as pixels."""
    import fitz

    document = fitz.open()
    page = document.new_page(width=600, height=400)
    for offset in (0, 6.52):
        page.draw_line(fitz.Point(100, 150 + offset), fitz.Point(500, 150 + offset), width=0.7)
    try:
        walls = detect_walls(extract_rulings(page), _CALIBRATED, reading.load_config(), "P01", page=page)
        assert walls
        assert all(wall["line_source"] == "vector" for wall in walls)
    finally:
        document.close()


# --- A row is text printed in one direction -------------------------------


def test_a_rotated_figure_does_not_chain_separate_rows():
    """A floor plan prints its vertical dimensions beside its abbreviations
    list. A rotated figure is as tall on the page as it is long, so it used to
    overlap every legend row next to it and merge seven of them into one."""
    lines = [
        line("SHR", (35, 425, 48, 433)),
        line("SHOWER", (56, 425, 81, 433)),
        line("ST", (35, 432, 44, 440)),
        line("STOVE", (56, 432, 75, 440)),
        # One figure printed rotated, spanning all of the rows above.
        line("1,895", (297, 425, 305, 474), axis="vertical"),
    ]
    rows = textmodel.group_into_rows(lines)
    horizontal = [r for r in rows if r[0]["axis"] == "horizontal"]
    assert len(horizontal) == 2, "each printed line of the legend is its own row"
    assert all(len(row) == 2 for row in horizontal)


def test_a_legend_entry_is_bounded_by_the_cells_it_used(config):
    """The legend's area is excluded from dimension detection, so an entry that
    claims the width of the sheet hides every dimension printed beside it."""
    lines = [line("ABBREVIATIONS:", (35, 309, 98, 319))]
    for index, (symbol, meaning) in enumerate(
        [("BAL", "BALUSTRADE"), ("BG", "BOX GUTTER"), ("DP", "DOWNPIPE"), ("FL", "FLOOR LEVEL")]
    ):
        top = 320 + index * 7
        lines.append(line(symbol, (35, top, 48, top + 7)))
        lines.append(line(meaning, (56, top, 100, top + 7)))
    # A dimension the drawing prints well to the right of the legend.
    lines.append(line("4,590", (297, 330, 305, 345), axis="vertical"))

    legends = schedules.detect_legends(lines, config, "A02")
    assert legends and len(legends[0]["entries"]) == 4
    right_edge = max(entry["bbox"][2] for entry in legends[0]["entries"])
    assert right_edge < 200, "the legend must not claim the drawing beside it"


def test_a_wall_longer_than_the_sheet_measures_is_flagged_not_dropped():
    """The block boundary pairs into a wall longer than the building. It is
    reported with a warning rather than deleted, because a sheet may also
    dimension only part of what it draws."""
    rulings = {"h": [(100.0, 100.0, 500.0), (106.52, 100.0, 500.0)], "v": []}
    walls = detect_walls(
        rulings, _CALIBRATED, reading.load_config(), "A02", sheet_span_mm=5000
    )
    assert len(walls) == 1, "the candidate is kept"
    assert walls[0]["longer_than_sheet_measures"] is True
    assert walls[0]["confidence_band"] == "review"


# --- The checking sheet a person fills in ---------------------------------


def _checking_sheet(tmp_path, rows):
    path = tmp_path / "ground_truth.csv"
    header = (
        "what,on_sheet,which_one,computer_read,where_to_look,"
        "is_it_correct,if_no_what_is_correct,your_name,notes"
    )
    path.write_text(
        "# instructions a person reads, kept above the header\n"
        "#\n" + header + "\n" + "\n".join(rows) + "\n",
        encoding="utf-8",
    )
    return path


def test_yes_means_the_drawing_agrees_with_what_was_read(tmp_path):
    path = _checking_sheet(
        tmp_path, ["room,Page 1,KITCHEN,13.73,middle left,YES,,S. Sadiq,"]
    )
    rows = accuracy.load_ground_truth(path)
    assert len(rows) == 1
    assert rows[0]["expected_value"] == "13.73"
    assert rows[0]["checked_by"] == "S. Sadiq"


def test_no_records_the_correction_not_what_was_read(tmp_path):
    """The most valuable row in the file: the one that proves the reader wrong."""
    path = _checking_sheet(
        tmp_path, ["dimension,Page 1,1910,1910,bottom centre,NO,1190,S. Sadiq,"]
    )
    rows = accuracy.load_ground_truth(path)
    assert rows[0]["expected_value"] == "1190", "the drawing wins, not the reader"
    assert rows[0]["reader_proposed"] == "1910"


def test_an_unanswered_row_is_not_counted(tmp_path):
    """A row seeded from the run and left blank must never score anything —
    comparing a run against its own output proves nothing."""
    path = _checking_sheet(
        tmp_path, ["room,Page 1,KITCHEN,13.73,middle left,,,,"]
    )
    rows = accuracy.load_ground_truth(path)
    assert rows[0]["checked_by"] == ""
    assert rows[0]["expected_value"] == ""


def test_the_original_column_names_still_load(tmp_path):
    """An older reference file keeps working."""
    path = tmp_path / "old.csv"
    path.write_text(
        "item_type,sheet,identifier,expected_value,checked_by\n"
        "room,Page 1,KITCHEN,13.73,S. Sadiq\n",
        encoding="utf-8",
    )
    rows = accuracy.load_ground_truth(path)
    assert rows[0]["expected_value"] == "13.73"


def test_a_checking_sheet_names_the_plan_it_belongs_to(tmp_path):
    """Two plans both have a "Page 1". Scoring one plan against the other's
    checking sheet reported every room on it as missing — 28 invented misses
    and a number that meant nothing."""
    path = tmp_path / "ground_truth.csv"
    path.write_text(
        "# FOR PLAN: one_plan.pdf\n"
        "#\n"
        "what,on_sheet,which_one,computer_read,is_it_correct,your_name\n"
        "room,Page 1,KITCHEN,13.73 m2,YES,S. Sadiq\n",
        encoding="utf-8",
    )
    assert accuracy.plan_file_named_in(path) == "one_plan.pdf"

    result = accuracy.written_for_another_plan("one_plan.pdf", "another_plan.pdf")
    assert result["measured"] is False
    assert result["per_item_type"] == {}
    assert "one_plan.pdf" in result["note"] and "another_plan.pdf" in result["note"]


def test_a_measurement_reads_back_with_or_without_its_unit():
    """The checking sheet prints "6,225 mm" so it can be compared with the
    drawing at a glance; whoever fills it in may type it either way."""
    assert accuracy._as_number("6,225 mm") == 6225.0
    assert accuracy._as_number("6225") == 6225.0
    assert accuracy._as_number("13.73 m2") == 13.73
    assert accuracy._as_number("1:100") is None


def test_a_figure_keeps_the_note_saying_what_it_measures_to(config):
    """One office prints '10260 TO WALL'. The figure is the measurement and
    the note says which face it runs to; both are kept."""
    reading = dimensions_module._classify("10260 TO WALL", config)
    assert reading is not None and reading["value_mm"] == 10260.0
    assert reading["measured_to"] == "TO WALL"
    # A product callout is still not a dimension.
    assert dimensions_module._classify("100x100 Steel posts", config) is None


# --- A page that carries its own rotation ---------------------------------


def test_text_on_a_rotated_page_lands_where_the_page_displays_it():
    """CAD exports very often store a landscape sheet as portrait with a
    90-degree page rotation. PyMuPDF then returns text in the unrotated space
    while the page renders rotated, so every box lands somewhere else on the
    marked-up sheet and horizontal text is measured as vertical."""
    import fitz

    document = fitz.open()
    page = document.new_page(width=595, height=842)  # stored portrait
    # Drafted so that it reads across the sheet once the page is turned —
    # which is exactly how a CAD export stores a landscape drawing.
    page.insert_text(fitz.Point(100, 200), "KITCHEN", fontsize=10, rotate=90)
    page.set_rotation(90)
    try:
        assert page.rect.width > page.rect.height, "the page displays as landscape"
        lines, _ = textmodel.build_page_lines(page, [], 150)
        kitchen = next(line for line in lines if "KITCHEN" in line["text"])

        # The box must sit inside the page as it is displayed...
        x0, y0, x1, y1 = kitchen["bbox"]
        assert 0 <= x0 < x1 <= page.rect.width
        assert 0 <= y0 < y1 <= page.rect.height
        # ...and the text must read across the sheet, not up it.
        assert kitchen["axis"] == "horizontal"
        assert (x1 - x0) > (y1 - y0)
    finally:
        document.close()


def test_drawn_lines_on_a_rotated_page_follow_the_page_too():
    """Walls are found from the line work, so it has to be turned with the
    text — otherwise the horizontal and vertical faces are swapped."""
    import fitz

    document = fitz.open()
    page = document.new_page(width=595, height=842)
    page.draw_line(fitz.Point(100, 300), fitz.Point(400, 300), width=0.7)  # horizontal
    page.set_rotation(90)
    try:
        rulings = extract_rulings(page)
        # A line drawn across a portrait page runs *up* the landscape one.
        assert rulings["v"], "the line should now be vertical on the displayed page"
        assert not rulings["h"]
    finally:
        document.close()


# --- A room label is a name, not a sentence -------------------------------


def test_an_annotation_that_mentions_a_room_is_not_a_room(config):
    """An elevation prints 'PROPOSED DECK ROOF SHEETING' and a specification
    sheet 'PLATE TO VERANDAH BEAM'. Both contain a real room word inside them
    and neither is a room."""
    lines = [
        line("PROPOSED DECK ROOF SHEETING", (100, 100, 260, 110)),
        line("PLATE TO VERANDAH BEAM", (100, 130, 240, 140)),
        line("DECK ROOFING", (100, 160, 180, 170)),
        line("REAR DECK", (100, 190, 160, 200)),
        line("BEDROOM No. 2", (100, 220, 170, 230)),
        line("LIVING / LOUNGE", (100, 250, 190, 260)),
    ]
    names = {room["name"] for room in rooms_module.detect_rooms(lines, config, "P03")}
    assert names == {"REAR DECK", "BEDROOM No. 2", "LIVING / LOUNGE"}


# --- A sheet may draw a plan under another title --------------------------


def test_a_sheet_titled_as_notes_that_draws_a_plan_is_treated_as_one(config):
    """One office titles a sheet 'FRAMING SPECIFICATIONS' and prints a
    complete proposed floor plan on it. Judging by the title alone meant no
    walls were ever looked for on the only floor plan in the document."""
    result = reading.detect_page_type("FRAMING SPECIFICATIONS", [], 13, 19, 0, config)
    assert result["value"] == "notes", "the sheet still reports what it calls itself"
    assert result["draws_a_plan"] is True, "but its contents say it draws a plan"


def test_a_section_is_never_promoted_to_a_plan_by_its_contents(config):
    """A section prints room names and dimensions too. Promoting those found
    202 'walls' across three sheets that draw no plan at all."""
    result = reading.detect_page_type("SECTION 1", [], 5, 14, 0, config)
    assert result["value"] == "section"
    assert result["draws_a_plan"] is False


# --- one wall, reported once ---------------------------------------------


def _candidate(wall_id, axis, line, start, end, thickness, breaks=None, nominal=True):
    """A wall candidate as the pairing step produces one, in points."""
    half = thickness / 10.0 / 2.0  # the tests below run at 10 mm per point
    if axis == "x":
        start_point, end_point = [start, line], [end, line]
        bbox = [start, line - half, end, line + half]
    else:
        start_point, end_point = [line, start], [line, end]
        bbox = [line - half, start, line + half, end]
    return {
        "bbox": bbox,
        "wall_id": wall_id,
        "runs_along": axis,
        "length_mm": (end - start) * 10.0,
        "thickness_mm": thickness,
        "matches_nominal_thickness": nominal,
        "start_point_pt": start_point,
        "end_point_pt": end_point,
        "face_positions_pt": [line - half, line + half],
        "gaps_pt": list(breaks or []),
        "longer_than_sheet_measures": False,
        "confidence": 0.8,
        "confidence_band": "high",
        "linked_opening_marks": [],
    }


def test_two_readings_of_the_same_wall_are_reported_once(config):
    """Two solids cannot occupy the same space. An external wall is drawn with
    more lines than its own two faces, so the pairing step produces several
    overlapping candidates for one wall — which doubled the wall count and left
    every opening mark with two equally close walls to choose between."""
    from pipeline.plan.walls import merge_overlapping_walls

    walls = [
        _candidate("W1", "x", 200.0, 100.0, 600.0, 90.0),
        _candidate("W2", "x", 200.4, 120.0, 580.0, 95.0),
    ]
    merged = merge_overlapping_walls(walls, 10.0, config)
    assert len(merged) == 1
    assert merged[0]["merged_from"] == 2


def test_a_wall_keeps_every_opening_its_copies_recorded(config):
    """Each copy holds only some of the wall's breaks, so a door in the wall is
    invisible to whichever copy did not record it."""
    from pipeline.plan.walls import merge_overlapping_walls

    walls = [
        _candidate("W1", "x", 200.0, 100.0, 600.0, 90.0, breaks=[(150.0, 240.0)]),
        _candidate("W2", "x", 200.4, 100.0, 600.0, 95.0, breaks=[(400.0, 490.0)]),
    ]
    merged = merge_overlapping_walls(walls, 10.0, config)
    assert len(merged) == 1
    assert merged[0]["gaps_pt"] == [[150.0, 240.0], [400.0, 490.0]]


def test_two_real_walls_side_by_side_are_not_merged(config):
    """A brick skin and a frame drawn beside each other are two walls. Merging
    them on band overlap alone lost five of one plan set's ten openings."""
    from pipeline.plan.walls import merge_overlapping_walls

    walls = [
        _candidate("W1", "x", 200.0, 100.0, 600.0, 80.0),
        _candidate("W2", "x", 206.0, 100.0, 600.0, 180.0),
    ]
    assert len(merge_overlapping_walls(walls, 10.0, config)) == 2


# --- where an opening sits on its wall ------------------------------------


def _sheet_with(walls, openings, schedules=None):
    return {
        "page_number": 1,
        "sheet_id": "A02",
        "walls": walls,
        "openings": openings,
        "schedules": schedules or [],
    }


def _mark(mark_id, mark, bbox):
    return {"mark_id": mark_id, "mark": mark, "element_type": "door", "bbox": bbox}


def test_a_mark_is_placed_on_the_break_that_measures_what_the_schedule_says(config):
    """A break beside the mark measuring the schedule's width *is* the opening:
    both its position and its width are then measured off the drawing."""
    calibration = {"measured_mm_per_point": 10.0, "usable_for_measurement": True}
    walls = [
        # The nearer wall has a break of the wrong width; the further one has
        # the right one. The schedule settles it.
        _candidate("A02-W001", "x", 205.0, 100.0, 600.0, 90.0, breaks=[(280.0, 300.0)]),
        _candidate("A02-W002", "x", 230.0, 100.0, 600.0, 90.0, breaks=[(280.0, 362.0)]),
    ]
    marks = [_mark("m1", "D1", [290.0, 210.0, 310.0, 220.0])]
    openings = openings_module.place_openings_on_walls(
        marks, walls, calibration, config, "A02"
    )
    openings[0]["width_mm"] = 820.0
    page = _sheet_with(walls, openings)
    openings_module.settle_opening_placement([page], config)

    assert openings[0]["wall_id"] == "A02-W002"
    assert openings[0]["position_on_wall"]["measured_from"] == "break_in_the_wall"


def test_two_doors_cannot_claim_the_same_break(config):
    """A break holds one opening. Without this, two 820 mm doors both took the
    same hole and one of them was cut in the wrong place."""
    calibration = {"measured_mm_per_point": 10.0, "usable_for_measurement": True}
    walls = [_candidate("A02-W001", "x", 200.0, 100.0, 600.0, 90.0, breaks=[(280.0, 362.0)])]
    marks = [
        _mark("m1", "D1", [300.0, 205.0, 320.0, 215.0]),
        _mark("m2", "D2", [420.0, 205.0, 440.0, 215.0]),
    ]
    openings = openings_module.place_openings_on_walls(
        marks, walls, calibration, config, "A02"
    )
    for opening in openings:
        opening["width_mm"] = 820.0
    openings_module.settle_opening_placement([_sheet_with(walls, openings)], config)

    measured = [
        o for o in openings
        if (o["position_on_wall"] or {}).get("measured_from") == "break_in_the_wall"
    ]
    assert len(measured) == 1, "only one of them is the door in that hole"


def test_a_mark_with_no_break_is_placed_from_where_it_is_printed(config):
    """A wall drawn with its opening hatched has no break to measure. The mark
    still says which wall and roughly where — and the record says exactly that
    rather than implying it was measured."""
    calibration = {"measured_mm_per_point": 10.0, "usable_for_measurement": True}
    walls = [_candidate("A02-W001", "x", 200.0, 100.0, 600.0, 90.0)]
    marks = [_mark("m1", "D1", [300.0, 215.0, 320.0, 225.0])]
    openings = openings_module.place_openings_on_walls(
        marks, walls, calibration, config, "A02"
    )
    openings[0]["width_mm"] = 820.0
    openings_module.settle_opening_placement([_sheet_with(walls, openings)], config)

    assert openings[0]["wall_id"] == "A02-W001"
    assert openings[0]["position_on_wall"]["measured_from"] == "the_mark_on_the_drawing"
    assert "no break" in openings[0]["wall_note"]


def test_the_words_beside_an_unmarked_opening_name_its_kind(config):
    """A plan set that prints no marks still describes its openings in words,
    and the kind is what makes a height possible at all."""
    lines = [{"text": "Sliding door", "bbox": [300.0, 180.0, 360.0, 190.0]}]
    kind, described = openings_module.kind_from_words_beside_it(
        [300.0, 195.0, 340.0, 205.0], lines, 10.0, config["openings"]
    )
    assert kind == "door"
    assert described == "Sliding door"


def test_a_word_too_far_from_the_opening_does_not_name_it(config):
    lines = [{"text": "Sliding door", "bbox": [900.0, 900.0, 960.0, 910.0]}]
    kind, _ = openings_module.kind_from_words_beside_it(
        [300.0, 195.0, 340.0, 205.0], lines, 10.0, config["openings"]
    )
    assert kind is None


def test_a_plan_that_labels_nothing_still_reports_its_doors_and_windows():
    """Counting only distinct marks reported "0 doors and windows" on a plan
    set that prints none — a headline contradicted by the line under it, which
    said the same openings were "marked 10 times"."""
    page = {
        "page_number": 1,
        "sheet_id": "P01",
        "title_block": {k: {"value": None, "confidence": 0, "extraction_method": "none"}
                        for k in ("sheet_number", "sheet_title", "discipline", "revision",
                                  "scale", "sheet_position", "project_number")},
        "page_type": {"value": "floor_plan", "draws_a_plan": True},
        "rooms": [], "dimensions": [], "dimension_chains": [], "schedules": [],
        "legends": [], "opening_marks": [], "walls": [], "unresolved_items": [],
        "scale_calibration": {"result": "confirmed"},
        "openings": [
            {"opening_id": f"P01-OPG{n:03d}", "mark": "", "element_type": None,
             "wall_id": None, "in_schedule": False, "found_by": "gap_in_the_wall",
             "source_bbox": [0, 0, 1, 1], "confidence": 0.6,
             "confidence_band": "review", "position_on_wall": None}
            for n in range(1, 11)
        ],
    }
    openings = reading.compute_metrics([page], {})["openings"]
    assert openings["distinct_openings"] == 10, "the plan's ten openings were reported as none"
    assert openings["openings_with_no_mark"] == 10


def test_marks_still_decide_the_count_where_a_plan_prints_them():
    """Where marks are printed they identify one opening across the sheets it
    appears on. The unmarked ones are then the same doors seen again on a
    reflected-ceiling or electrical plan, and counting those too doubles them."""
    def sheet(sheet_id, openings):
        return {
            "page_number": 1, "sheet_id": sheet_id,
            "title_block": {k: {"value": None, "confidence": 0, "extraction_method": "none"}
                            for k in ("sheet_number", "sheet_title", "discipline",
                                      "revision", "scale", "sheet_position",
                                      "project_number")},
            "page_type": {"value": "floor_plan", "draws_a_plan": True},
            "rooms": [], "dimensions": [], "dimension_chains": [], "schedules": [],
            "legends": [], "opening_marks": [], "walls": [], "unresolved_items": [],
            "scale_calibration": {"result": "confirmed"}, "openings": openings,
        }

    marked = [
        {"opening_id": f"A02-OP{n:03d}", "mark": m, "element_type": "door",
         "wall_id": None, "in_schedule": True, "found_by": "mark_on_the_drawing",
         "source_bbox": [0, 0, 1, 1], "confidence": 0.9, "confidence_band": "high",
         "position_on_wall": None}
        for n, m in enumerate(["D1", "D2", "D1"], start=1)  # D1 marked twice
    ]
    unmarked = [
        {"opening_id": "A05-OPG001", "mark": "", "element_type": None,
         "wall_id": None, "in_schedule": False, "found_by": "gap_in_the_wall",
         "source_bbox": [0, 0, 1, 1], "confidence": 0.6, "confidence_band": "review",
         "position_on_wall": None}
    ]
    openings = reading.compute_metrics(
        [sheet("A02", marked), sheet("A05", unmarked)], {}
    )["openings"]
    assert openings["distinct_openings"] == 2, "D1 and D2 — the unmarked one is D1 again"


def test_a_pair_of_lines_meeting_nothing_is_not_a_wall_of_this_building(config):
    """A building's walls form one connected outline - they meet at corners
    and junctions. A pair of parallel lines touching nothing else is an eave, a
    roof extent, a fence or a bench, and drawing it on the marked-up sheet puts
    a wall where the drawing has none."""
    from pipeline.plan.walls import mark_walls_that_stand_alone

    walls = [
        _candidate("W1", "x", 200.0, 100.0, 600.0, 90.0),
        _candidate("W2", "y", 100.0, 200.0, 700.0, 90.0),   # meets W1 at a corner
        _candidate("W3", "x", 700.0, 100.0, 600.0, 90.0),   # meets W2 at the far end
        _candidate("W4", "y", 600.0, 200.0, 700.0, 90.0),   # closes the outline
        _candidate("W5", "x", 2000.0, 2000.0, 2600.0, 90.0),  # away on its own
    ]
    alone = mark_walls_that_stand_alone(walls, 10.0, config)
    assert alone == 1
    assert walls[-1]["meets_another_wall"] is False
    assert all(w["meets_another_wall"] for w in walls[:4])


def test_a_drawing_with_too_few_candidates_is_not_judged_that_way(config):
    """With two or three candidates there is no network to be part of, and
    calling them all strays would report a small extension as having no walls."""
    from pipeline.plan.walls import mark_walls_that_stand_alone

    walls = [
        _candidate("W1", "x", 200.0, 100.0, 600.0, 90.0),
        _candidate("W2", "x", 900.0, 100.0, 600.0, 90.0),
    ]
    assert mark_walls_that_stand_alone(walls, 10.0, config) == 0
    assert all(w["meets_another_wall"] for w in walls)


def test_a_wall_that_meets_nothing_is_kept_out_of_the_model(config):
    """Extruded into a model it becomes a wall standing on its own in mid-air,
    and every quantity taken from the model counts it."""
    from pipeline.model.canonical import buildable_walls

    page = {"walls": [
        {"wall_id": "A-W1", "length_mm": 5000.0, "meets_another_wall": True},
        {"wall_id": "A-W2", "length_mm": 5000.0, "meets_another_wall": False},
        {"wall_id": "A-W3", "length_mm": 100.0, "meets_another_wall": True},
    ]}
    kept = [w["wall_id"] for w in buildable_walls(page, 300.0)]
    assert kept == ["A-W1"]


def test_one_long_face_can_pair_along_several_stretches(config):
    """A long external wall is one continuous face on the outside and several
    shorter ones on the inside, because the rooms behind it break the inner
    face up. Marking the whole outer face as used at the first pairing left
    every later stretch with nothing to pair against - which is why a wall was
    marked up for only half its length, with the rest of it bare."""
    from pipeline.plan.walls import _pair_faces

    # (position across, start along, end along, breaks). 10 mm per point.
    faces = [
        (0.0, 0.0, 1000.0, [], {}),      # the outside of the wall, all of it
        (9.0, 0.0, 400.0, [], {}),       # the inside behind the first room
        (9.0, 600.0, 1000.0, [], {}),    # and behind the second
    ]
    pairs = _pair_faces(faces, 10.0, config, config["walls"]["nominal_thickness_mm"])

    assert len(pairs) == 2, "the outer face was used up by the first room"
    covered = sorted((round(p["start"]), round(p["end"])) for p in pairs)
    assert covered == [(0, 400), (600, 1000)]
    assert all(round(p["thickness_mm"]) == 90 for p in pairs)


def test_a_stretch_is_never_given_to_two_walls(config):
    """Each pairing takes only what is still free, so the same run of wall is
    never reported twice."""
    from pipeline.plan.walls import _pair_faces

    faces = [
        (0.0, 0.0, 1000.0, [], {}),
        (9.0, 0.0, 1000.0, [], {}),
        (9.4, 0.0, 1000.0, [], {}),   # a third line along the same wall
    ]
    pairs = _pair_faces(faces, 10.0, config, config["walls"]["nominal_thickness_mm"])
    spans = [(p["start"], p["end"]) for p in pairs]
    for i, (a_start, a_end) in enumerate(spans):
        for b_start, b_end in spans[i + 1 :]:
            overlap = min(a_end, b_end) - max(a_start, b_start)
            assert overlap <= 0, "two walls were reported over the same stretch"


# --- where the walls meet each other --------------------------------------
#
# Every test below names the mistake it prevents. All of them run at 10 mm per
# point, which is the scale ``_candidate`` above is written for.


def _junction_shapes(walls, config):
    from pipeline.plan.walls import detect_junctions

    detect_junctions(walls, config)
    return {
        (junction["with_wall_id"], junction["shape"])
        for wall in walls
        for junction in wall["junctions"]
    }


def test_a_partition_landing_on_a_wall_is_a_t_junction(config):
    """The whole point of reading junctions: an inner wall is drawn running up
    to an outer wall and stopping. Without this the two are separate lines on a
    page and the building has no shape."""
    from pipeline.plan.walls import detect_junctions

    outer = _candidate("W1", "x", 200.0, 100.0, 600.0, 90.0)
    partition = _candidate("W2", "y", 350.0, 200.0, 400.0, 90.0)
    detect_junctions([outer, partition], config)

    assert outer["connects_to"] == ["W2"]
    assert partition["connects_to"] == ["W1"]
    assert partition["junctions"][0]["shape"] == "T"


def test_two_walls_meeting_at_a_corner_are_an_l_junction(config):
    from pipeline.plan.walls import detect_junctions

    across = _candidate("W1", "x", 200.0, 100.0, 600.0, 90.0)
    down = _candidate("W2", "y", 600.0, 200.0, 500.0, 90.0)
    detect_junctions([across, down], config)

    assert across["junctions"][0]["shape"] == "L"


def test_two_walls_crossing_are_a_plus_junction(config):
    from pipeline.plan.walls import detect_junctions

    across = _candidate("W1", "x", 300.0, 100.0, 600.0, 90.0)
    down = _candidate("W2", "y", 350.0, 100.0, 500.0, 90.0)
    detect_junctions([across, down], config)

    assert across["junctions"][0]["shape"] == "+"


def test_a_wall_carrying_on_past_a_doorway_stays_one_building(config):
    """A run of wall broken by two doors is drawn as three pieces. Read as three
    unconnected walls it is three buildings, and every partition hanging off the
    middle piece loses its way back to the outside."""
    from pipeline.plan.walls import detect_junctions

    left = _candidate("W1", "x", 200.0, 100.0, 300.0, 90.0)
    right = _candidate("W2", "x", 200.0, 308.0, 600.0, 90.0)
    detect_junctions([left, right], config)

    assert left["junctions"][0]["shape"] == "collinear"


def test_two_walls_nowhere_near_each_other_do_not_meet(config):
    from pipeline.plan.walls import detect_junctions

    one = _candidate("W1", "x", 200.0, 100.0, 300.0, 90.0)
    other = _candidate("W2", "y", 900.0, 700.0, 800.0, 90.0)
    assert detect_junctions([one, other], config) == 0
    assert one["connects_to"] == []


def test_a_junction_says_where_on_the_sheet_it_happens(config):
    """Critical Rule 4: nothing this reader produces may be anonymous. A
    junction a reviewer cannot find on the drawing cannot be checked."""
    from pipeline.plan.walls import detect_junctions

    across = _candidate("W1", "x", 200.0, 100.0, 600.0, 90.0)
    down = _candidate("W2", "y", 350.0, 200.0, 400.0, 90.0)
    detect_junctions([across, down], config)

    x, y = across["junctions"][0]["at_pt"]
    assert abs(x - 350.0) < 10.0 and abs(y - 200.0) < 10.0


# --- a short stretch is judged on what it is joined to ---------------------


def test_a_short_wall_joined_to_the_building_is_kept(config):
    """A pier, a return and the nib beside a doorway are all real walls and all
    short. A plain length floor loses every one of them."""
    from pipeline.plan.walls import _drop_short_walls_that_meet_nothing

    outer = _candidate("W1", "x", 200.0, 100.0, 600.0, 90.0)
    nib = _candidate("W2", "y", 300.0, 200.0, 230.0, 90.0)
    kept = _drop_short_walls_that_meet_nothing([outer, nib], config)

    assert {wall["wall_id"] for wall in kept} == {"W1", "W2"}


def test_a_short_stretch_joined_to_nothing_is_not_a_wall(config):
    """A bench top, a wardrobe and a step draw as two parallel lines a plausible
    thickness apart, and are joined to nothing."""
    from pipeline.plan.walls import _drop_short_walls_that_meet_nothing

    outer = _candidate("W1", "x", 200.0, 100.0, 600.0, 90.0)
    joinery = _candidate("W2", "y", 900.0, 700.0, 730.0, 90.0)
    kept = _drop_short_walls_that_meet_nothing([outer, joinery], config)

    assert [wall["wall_id"] for wall in kept] == ["W1"]


# --- outside and inside ----------------------------------------------------


def _closed_room():
    """Four walls round a room, as the pairing step would report them."""
    return [
        _candidate("N", "x", 100.0, 100.0, 600.0, 90.0),
        _candidate("S", "x", 500.0, 100.0, 600.0, 90.0),
        _candidate("W", "y", 100.0, 100.0, 500.0, 90.0),
        _candidate("E", "y", 600.0, 100.0, 500.0, 90.0),
    ]


def test_the_walls_round_the_outside_are_outer_walls(config):
    from pipeline.plan.walls import classify_outer_inner, detect_junctions

    walls = _closed_room()
    detect_junctions(walls, config)
    classify_outer_inner(walls, config)

    assert {wall["wall_type"] for wall in walls} == {"outer"}


def test_a_wall_with_building_on_both_sides_is_an_inner_wall(config):
    from pipeline.plan.walls import classify_outer_inner, detect_junctions

    walls = _closed_room()
    walls.append(_candidate("P", "y", 350.0, 100.0, 500.0, 90.0))
    detect_junctions(walls, config)
    classify_outer_inner(walls, config)

    assert {wall["wall_id"] for wall in walls if wall["wall_type"] == "inner"} == {"P"}


def test_an_outer_wall_in_the_notch_of_an_l_shaped_plan_is_still_outer(config):
    """**Why this is a ray and not a rectangle.** Most detached houses are an L
    or a U once the garage and the alfresco are on. The walls in the notch are
    external and nowhere near the corners of a bounding rectangle, so any test
    that asks 'is this wall on the edge of the extent' calls them internal —
    and then every quantity that depends on which walls face the weather is
    wrong."""
    from pipeline.plan.walls import classify_outer_inner, detect_junctions

    # An L: the big rectangle with its top-right quarter cut away. The two
    # walls of the notch sit in the middle of the extent.
    walls = [
        _candidate("S", "x", 500.0, 100.0, 600.0, 90.0),
        _candidate("W", "y", 100.0, 100.0, 500.0, 90.0),
        _candidate("N_left", "x", 100.0, 100.0, 350.0, 90.0),
        _candidate("notch_down", "y", 350.0, 100.0, 300.0, 90.0),
        _candidate("notch_across", "x", 300.0, 350.0, 600.0, 90.0),
        _candidate("E", "y", 600.0, 300.0, 500.0, 90.0),
    ]
    detect_junctions(walls, config)
    classify_outer_inner(walls, config)

    notch = {w["wall_id"]: w["wall_type"] for w in walls if w["wall_id"].startswith("notch")}
    assert notch == {"notch_down": "outer", "notch_across": "outer"}


def test_a_wall_that_meets_nothing_is_not_guessed_at(config):
    """Critical Rule 5. A pair of lines joined to nothing has not been
    established as part of this building, so which side of it is outside is not
    something the drawing has said."""
    from pipeline.plan.walls import classify_outer_inner, detect_junctions

    walls = _closed_room()
    walls.append(_candidate("alone", "x", 2000.0, 2000.0, 2400.0, 90.0))
    detect_junctions(walls, config)
    classify_outer_inner(walls, config)

    assert walls[-1]["wall_type"] == "unknown"


# --- the graph and the record ---------------------------------------------


def test_a_junction_is_one_edge_in_the_graph_not_two(config):
    """It is recorded on both walls, because either one is where a reader may
    start. It is one place on the drawing."""
    from pipeline.plan.walls import detect_junctions, wall_graph_for

    walls = _closed_room()
    detect_junctions(walls, config)
    graph = wall_graph_for(walls, "A02", 1)

    assert graph["junction_count"] == 4
    assert len(graph["nodes"]) == 4
    assert all(edge["from_wall_id"] < edge["to_wall_id"] for edge in graph["edges"])


def test_every_wall_record_says_what_where_how_and_how_certain(config):
    """The fields a later stage and a reviewer both read. A record missing any
    of them is a measurement nobody can check."""
    from pipeline.plan.walls import (
        classify_outer_inner,
        describe_walls,
        detect_junctions,
        walls_as_records,
    )

    walls = _closed_room()
    walls[0]["gaps_pt"] = [[200.0, 282.0]]
    detect_junctions(walls, config)
    classify_outer_inner(walls, config)
    describe_walls(walls, 10.0, config, "A02", 1)
    record = walls_as_records(walls)[0]

    for field in (
        "wall_id", "wall_type", "orientation", "face1", "face2", "centerline",
        "gaps", "length_mm", "thickness_mm", "connects_to", "source_sheet",
        "source_page", "confidence", "review_needed",
    ):
        assert field in record, f"{field} is missing from the wall record"
    assert record["orientation"] == "horizontal"
    assert record["confidence"] in ("high", "medium", "low")
    assert record["gaps"][0]["gap_mm"] == 820.0
    assert record["source_sheet"] == "A02" and record["source_page"] == 1


def test_a_wall_reports_the_two_faces_it_was_measured_from(config):
    """The evidence, kept beside the answer: a reviewer checks a thickness by
    looking at the two lines it was taken between."""
    from pipeline.plan.walls import describe_walls, walls_as_records

    walls = [_candidate("W1", "x", 200.0, 100.0, 600.0, 90.0)]
    describe_walls(walls, 10.0, config, "A02", 1)
    # Derived where the record is written rather than stored on every wall:
    # they are the face positions and the run written a second way, and
    # carrying both made the reading the browser waits for a third larger.
    record = walls_as_records(walls)[0]

    assert record["face1"]["y0"] != record["face2"]["y0"]
    assert record["centerline"]["y0"] == 200.0
    assert record["centerline"]["x0"] == 100.0


# --- what the settings must not be allowed to do --------------------------


def test_a_fragment_never_replaces_the_wall_it_is_part_of(config):
    """Once a stretch as short as a nib could be a candidate, a cluster could
    hold an 8 m wall and a 0.3 m piece of the same wall — and the ranking, which
    prefers a thickness the office builds over length, kept the piece. On two
    floor plans that cost about 30 m of traced wall each while the wall count
    went up, which is the worst shape a change can take: it looks like more and
    is less."""
    from pipeline.plan.walls import merge_overlapping_walls

    wall = _candidate("W1", "x", 200.0, 100.0, 900.0, 95.0, nominal=False)
    fragment = _candidate("W2", "x", 200.4, 400.0, 430.0, 90.0, nominal=True)
    merged = merge_overlapping_walls([wall, fragment], 10.0, config)

    assert len(merged) == 1
    assert merged[0]["length_mm"] == 8000.0


def test_the_collinear_tolerance_stays_under_the_thinnest_wall(config):
    """A wall **is** two lines a thickness apart. A tolerance for 'the same
    line' that is wider than the thinnest wall the office builds merges a
    wall's own two faces into one, and the wall disappears — measured, going
    from 0.6 to 3 points took the walls at a buildable thickness on one floor
    plan from 38 of 40 down to 21 of 27."""
    settings = config["walls"]
    tolerance_mm = float(settings["collinear_tolerance_points"]) * 35.28  # 1:100
    assert tolerance_mm < min(float(t) for t in settings["nominal_thickness_mm"])


def test_a_line_with_no_stated_width_is_never_dropped_as_too_thin(config):
    """A filled shape states no stroke width, and neither does a line recovered
    from a page read as a picture. Reading zero as thin threw away every wall
    on an image-drawn sheet."""
    from pipeline.plan.walls import _drop_lines_that_are_not_wall_faces

    segments = [(200.0, 100.0, 600.0), (210.0, 100.0, 600.0)]
    # How a line was drawn travels with it as (stroke width, dashed).
    kept, drawn = _drop_lines_that_are_not_wall_faces(
        segments, [(0.0, False), (0.2, False)], {"reject_lines_thinner_than_pt": 0.5}
    )
    assert kept == [segments[0]] and drawn == [(0.0, False)]


def test_the_office_wall_settings_are_read_from_their_own_file():
    """``config/wall_config.json`` is where an office changes what a wall is.
    A setting given there has to win, or the file is decoration."""
    from app.paths import CONFIG_DIR

    assert (CONFIG_DIR / "wall_config.json").exists()
    settings = reading.load_config()["walls"]
    override = json.loads((CONFIG_DIR / "wall_config.json").read_text(encoding="utf-8"))
    for name, value in override.items():
        if not name.startswith("_"):
            assert settings[name] == value


# --- what is not a wall ----------------------------------------------------
#
# Every test below names a false wall that was appearing on the marked-up
# sheet. They run at 10 mm per point, which is what ``_candidate`` is written
# for; a text box here is a printed line of words, as `textmodel` reports one.


def _room(x0, y0, x1, y1, name="ROOM"):
    return {"room_id": name, "name": name, "bbox": [x0, y0, x1, y1]}


def _chain(axis, x0, y0, x1, y1, members=4):
    return {"chain_id": "CH", "axis": axis, "member_count": members,
            "bbox": [x0, y0, x1, y1], "sum_mm": 10000.0}


def test_a_printed_word_is_not_a_wall(config):
    """**The largest source of false walls there was.** A sheet stored as a
    picture has its lines recovered as continuous runs of dark pixels, and a
    room name set in capitals is one — so the word became a line, its top and
    bottom became two parallel lines a wall thickness apart, and every room
    label on the plan was reported as a wall lying across its own room. On one
    floor plan 32 of 157 walls were printed words."""
    from pipeline.plan.walls import _drop_lettering

    label = [200.0, 100.0, 260.0, 108.0]  # the word "KITCHEN"
    segments = [(100.5, 200.0, 260.0), (107.5, 200.0, 260.0)]
    assert _drop_lettering(segments, None, "x", [label], config["walls"])[0] == []


def test_a_wall_running_under_a_room_label_is_kept(config):
    """A plan prints its room names on top of its rooms, so every wall of that
    room passes near one. The test is containment, not overlap: a real wall is
    many times longer than the label and only a fraction of it is inside."""
    from pipeline.plan.walls import _drop_lettering

    label = [200.0, 100.0, 260.0, 108.0]
    wall_face = [(104.0, 60.0, 700.0)]
    assert _drop_lettering(wall_face, None, "x", [label], config["walls"])[0] == wall_face


def test_a_short_line_inside_a_long_note_is_not_lettering(config):
    """A slab setout plan prints its notes right across the slab. Treating any
    line inside a printed line of text as lettering threw away 20 of that
    sheet's 25 walls — the drawn line has to be about the size of the word."""
    from pipeline.plan.walls import _drop_lettering

    note = [100.0, 100.0, 500.0, 108.0]  # a sentence printed over the plan
    short = [(104.0, 300.0, 322.0)]
    assert _drop_lettering(short, None, "x", [note], config["walls"])[0] == short


def test_a_square_is_not_a_wall(config):
    """A dining chair is 440 mm across with its back drawn 76 mm behind it, and
    that passed every thickness test there was. A wall is longer than it is
    thick, at any scale and on any size of building."""
    from pipeline.plan.walls import _pair_faces

    # two faces 230 mm apart running together for 230 mm
    faces = [(100.0, 500.0, 523.0, [], {}), (123.0, 500.0, 523.0, [], {})]
    settings = {**config["walls"], "min_wall_length_mm": 200, "min_length_to_thickness": 3.0}
    assert _pair_faces(faces, 10.0, {"walls": settings}, []) == []


# --- the plan is drawn between its dimension strings -----------------------


def test_the_plan_is_bounded_by_the_strings_printed_outside_it(config):
    """A dimension string is printed clear of the plan, and the witness line
    running back from each figure lies on the same line as the wall face it
    measures to — so the two merge into one candidate running out of the
    building. On one floor plan that produced a 17.9 m wall on a house 11 m
    deep."""
    from pipeline.plan.walls import drawing_region

    rooms = [_room(400.0, 300.0, 460.0, 310.0), _room(500.0, 400.0, 560.0, 410.0)]
    chains = [_chain("x", 380.0, 200.0, 700.0, 210.0),   # printed above the plan
              _chain("x", 380.0, 600.0, 700.0, 610.0)]   # and below it
    x0, y0, x1, y1 = drawing_region(rooms, chains, 1000.0, 800.0)
    assert (y0, y1) == (210.0, 600.0)


def test_a_string_printed_over_the_plan_never_cuts_the_building_in_half(config):
    """A plan may dimension itself internally. Using that string would put the
    edge of the drawing through the middle of the building."""
    from pipeline.plan.walls import drawing_region

    rooms = [_room(400.0, 300.0, 460.0, 310.0), _room(500.0, 400.0, 560.0, 410.0)]
    inside_string = [_chain("x", 380.0, 350.0, 700.0, 360.0)]
    assert drawing_region(rooms, inside_string, 1000.0, 800.0) == (0.0, 0.0, 1000.0, 800.0)


def test_where_there_is_nothing_to_work_from_the_whole_page_is_the_plan(config):
    """A sheet with no room labels, or none of its strings outside them, must
    lose nothing: the rule then removes nothing rather than guessing."""
    from pipeline.plan.walls import drawing_region

    assert drawing_region([], [], 1000.0, 800.0) == (0.0, 0.0, 1000.0, 800.0)


def test_a_wall_running_out_of_the_plan_is_cut_back_not_thrown_away(config):
    """Half of such a candidate is a real wall face. Rejecting it outright lost
    4.2 m of genuine wall on each of five candidates on one floor plan."""
    from pipeline.plan.walls import trim_walls_to_the_drawing

    wall = _candidate("W1", "y", 300.0, 200.0, 600.0, 90.0)
    trimmed, dropped = trim_walls_to_the_drawing(
        [wall], (0.0, 0.0, 1000.0, 400.0), 10.0, config
    )
    assert (trimmed, dropped) == (1, 0)
    assert wall["end_point_pt"][1] == 400.0
    assert wall["length_mm"] == 2000.0


def test_a_candidate_wholly_outside_the_plan_is_set_aside(config):
    from pipeline.plan.walls import trim_walls_to_the_drawing

    wall = _candidate("W1", "x", 900.0, 100.0, 600.0, 90.0)
    trimmed, dropped = trim_walls_to_the_drawing(
        [wall], (0.0, 0.0, 1000.0, 400.0), 10.0, config
    )
    assert (trimmed, dropped) == (0, 1)
    assert wall["meets_another_wall"] is False


# --- a wall does not end in the margin -------------------------------------


def test_a_tail_running_past_the_building_into_the_margin_is_cut_off(config):
    """A wall ends where it meets another wall, or at the outside of the
    building. It never ends in the middle of empty paper outside the plan."""
    from pipeline.plan.walls import detect_junctions, trim_free_tails

    # The building's outside wall, and a line running from inside the plan,
    # through it, and on out to where the dimension figures are printed.
    across = _candidate("W1", "x", 600.0, 100.0, 800.0, 90.0)
    witness = _candidate("W2", "y", 400.0, 250.0, 700.0, 90.0)
    rooms = [_room(150.0, 150.0, 200.0, 160.0), _room(500.0, 250.0, 550.0, 260.0)]
    detect_junctions([across, witness], config)

    assert trim_free_tails([across, witness], rooms, 10.0, config) == 1
    assert witness["end_point_pt"][1] == 600.0  # cut back to the wall it meets


def test_a_wall_stopping_free_inside_the_building_is_left_alone(config):
    """Inside the building a wall genuinely stops free — at a doorway, at the
    end of a nib, at a return. That is the distinction that makes the rule
    above safe, and without it every partition would be cut at its last
    junction."""
    from pipeline.plan.walls import detect_junctions, trim_free_tails

    across = _candidate("W1", "x", 300.0, 100.0, 600.0, 90.0)
    partition = _candidate("W2", "y", 400.0, 300.0, 500.0, 90.0)
    rooms = [_room(150.0, 200.0, 200.0, 210.0), _room(500.0, 550.0, 550.0, 560.0)]
    detect_junctions([across, partition], config)

    assert trim_free_tails([across, partition], rooms, 10.0, config) == 0
    assert partition["end_point_pt"][1] == 500.0


# --- what a group of walls has to be ---------------------------------------


def test_a_small_group_of_short_lines_is_not_part_of_the_building(config):
    """Touching one other line is not being part of a building. The two lines
    of a legend row touch each other, and so do the sides of a car drawn in a
    garage — and every one of those was drawn on the marked-up sheet."""
    from pipeline.plan.walls import mark_walls_that_stand_alone

    walls = [
        _candidate("W1", "x", 200.0, 100.0, 600.0, 90.0),
        _candidate("W2", "y", 100.0, 200.0, 700.0, 90.0),
        _candidate("W3", "x", 700.0, 100.0, 600.0, 90.0),
        _candidate("W4", "y", 600.0, 200.0, 700.0, 90.0),
        # a legend row away on its own: two short lines meeting each other
        _candidate("L1", "x", 2000.0, 2000.0, 2030.0, 90.0),
        _candidate("L2", "y", 2000.0, 2000.0, 2030.0, 90.0),
    ]
    assert mark_walls_that_stand_alone(walls, 10.0, config) == 2
    assert [w["wall_id"] for w in walls if not w["meets_another_wall"]] == ["L1", "L2"]


def test_two_long_walls_meeting_each_other_are_a_building(config):
    """On a sheet whose drawing is stored as a picture the tracing recovers the
    walls in pieces rather than as one connected outline. Counting alone threw
    away 32 m of real wall."""
    from pipeline.plan.walls import mark_walls_that_stand_alone

    walls = [
        _candidate("W1", "x", 200.0, 100.0, 600.0, 90.0),
        _candidate("W2", "y", 100.0, 200.0, 700.0, 90.0),
        _candidate("W3", "x", 700.0, 100.0, 600.0, 90.0),
        _candidate("W4", "y", 600.0, 200.0, 700.0, 90.0),
        _candidate("F1", "x", 2000.0, 2000.0, 2600.0, 90.0),
        _candidate("F2", "y", 2000.0, 2000.0, 2600.0, 90.0),
    ]
    assert mark_walls_that_stand_alone(walls, 10.0, config) == 0


def test_a_sheet_with_no_connected_building_is_not_judged_this_way(config):
    """On a slab setout plan the walls are drawn sparsely and none reaches
    another. The rule says "this group is too small to be part of the
    building", which means nothing when no group on the sheet is a building —
    and applying it anyway threw away all 19 of that sheet's walls."""
    from pipeline.plan.walls import mark_walls_that_stand_alone

    walls = [
        _candidate(f"W{n}", "x", 200.0 * n, 100.0, 200.0, 90.0) for n in range(1, 6)
    ]
    assert mark_walls_that_stand_alone(walls, 10.0, config) == 0
    assert all(wall["meets_another_wall"] for wall in walls)


# --- the drawing says what its openings are --------------------------------


def test_three_lines_drawn_inside_a_wall_are_a_sliding_window(config):
    """**The feature this whole reading turns on.** A wall is solid, so nothing
    is drawn inside one. A window is not, and the drawing says so by putting
    the glass, the frame and the sashes between the wall's two faces. Three or
    more of them are the overlapping sashes of a sliding window."""
    from pipeline.plan.symbols import window_symbols_in

    wall = _candidate("W1", "x", 200.0, 100.0, 600.0, 90.0)  # faces at 195.5 / 204.5
    rulings = {"h": [(198.0, 300.0, 400.0), (200.0, 300.0, 400.0), (202.0, 300.0, 400.0)],
               "v": []}
    found = window_symbols_in(wall, rulings, 10.0, config["walls"])

    assert len(found) == 1
    assert found[0]["kind"] == "sliding_window"
    assert found[0]["width_mm"] == 1000.0


def test_two_lines_drawn_inside_a_wall_are_a_fixed_pane(config):
    from pipeline.plan.symbols import window_symbols_in

    wall = _candidate("W1", "x", 200.0, 100.0, 600.0, 90.0)
    rulings = {"h": [(198.5, 300.0, 400.0), (201.5, 300.0, 400.0)], "v": []}
    found = window_symbols_in(wall, rulings, 10.0, config["walls"])

    assert [f["kind"] for f in found] == ["fixed_window"]


def test_a_wall_with_nothing_drawn_inside_it_carries_no_window(config):
    from pipeline.plan.symbols import window_symbols_in

    wall = _candidate("W1", "x", 200.0, 100.0, 600.0, 90.0)
    assert window_symbols_in(wall, {"h": [], "v": []}, 10.0, config["walls"]) == []


def test_a_wall_hatched_end_to_end_is_not_one_long_window(config):
    """A wall drawn in section is hatched right through. Reading that as a
    window reported a 5.3 m "fixed window" covering nine tenths of a wall."""
    from pipeline.plan.symbols import window_symbols_in

    wall = _candidate("W1", "x", 200.0, 100.0, 600.0, 90.0)
    rulings = {"h": [(198.0, 100.0, 600.0), (200.0, 100.0, 600.0), (202.0, 100.0, 600.0)],
               "v": []}
    assert window_symbols_in(wall, rulings, 10.0, config["walls"]) == []


# --- a door states its own width in its swing ------------------------------


def test_a_quarter_circle_the_size_of_a_door_leaf_is_a_door(config):
    """A hinged door is drawn as its swing, and the arc's radius is the leaf —
    which is why a plan set is full of square-ish curved paths exactly 820 mm
    and 1,100 mm across. Those *are* the doors, stated more precisely in the
    geometry than in any label."""
    from pipeline.plan.symbols import door_swings

    class _Rect:
        x0, y0, x1, y1 = 100.0, 100.0, 182.0, 182.0

    class _Page:
        _loopsite_drawings = [
            {"items": [("c", None, None, None, None)], "rect": _Rect()},
            # a leader: curved, but nothing like square
            {"items": [("c", None, None, None, None)],
             "rect": type("R", (), {"x0": 0.0, "y0": 0.0, "x1": 300.0, "y1": 8.0})()},
        ]

    found = door_swings(_Page(), 10.0, config["walls"])
    assert [f["width_mm"] for f in found] == [820.0]


# --- four readings of one opening ------------------------------------------


def _opening(found_by, wall_id, start, end, kind=None, mark="", width=None):
    return {
        "opening_id": "", "mark": mark, "element_type": kind,
        "element_type_source": found_by, "wall_id": wall_id,
        "position_on_wall": {
            "start_fraction": start, "end_fraction": end,
            "centre_fraction": (start + end) / 2,
            "from_wall_start_mm": 0.0, "width_mm": width or 0.0,
            "measured_from": found_by,
        },
        "width_mm": width, "height_mm": None, "sill_height_mm": None,
        "head_height_mm": None, "in_schedule": False, "found_by": found_by,
        "source_sheet": "A02", "source_bbox": [0.0, 0.0, 1.0, 1.0],
        "confidence": 0.6, "confidence_band": "review",
        "review_status": "auto_confirmed",
    }


def _page_with(openings):
    return {
        "sheet_id": "A02",
        "walls": [_candidate("W1", "x", 200.0, 100.0, 600.0, 90.0)],
        "scale_calibration": {"measured_mm_per_point": 10.0},
        "openings": openings,
    }


def test_three_readings_of_one_opening_are_one_opening(config):
    """A plan states an opening in up to four ways and a good plan set states
    it twice or more. Three readings of the same window are one window."""
    page = _page_with([
        _opening("mark_on_the_drawing", "W1", 0.3, 0.5, "window", mark="W4", width=1800),
        _opening("window_symbol", "W1", 0.31, 0.51, "sliding_window", width=1804),
        _opening("gap_in_the_wall", "W1", 0.3, 0.5, None, width=1790),
    ])
    result = merge_opening_evidence(page, config)

    assert result["openings"] == 1
    kept = page["openings"][0]
    assert result["merged"] == 2
    assert len(set(kept["evidence"])) == 3
    assert kept["confidence_band"] == "high"
    assert kept["review_status"] == "auto_confirmed"


def test_a_drawn_symbol_outranks_a_printed_label(config):
    """The symbol is the thing itself, drawn to size; the label is a reference
    to a schedule row that may have been typed against the wrong mark."""
    page = _page_with([
        _opening("mark_on_the_drawing", "W1", 0.3, 0.5, "window", mark="W4", width=1800),
        _opening("window_symbol", "W1", 0.31, 0.51, "sliding_window", width=1804),
    ])
    merge_opening_evidence(page, config)

    assert page["openings"][0]["element_type"] == "sliding_window"
    assert page["openings"][0]["mark"] == "W4"


def test_two_openings_in_different_places_stay_separate(config):
    page = _page_with([
        _opening("window_symbol", "W1", 0.05, 0.2, "fixed_window", width=800),
        _opening("window_symbol", "W1", 0.6, 0.8, "sliding_window", width=1200),
    ])
    assert merge_opening_evidence(page, config)["openings"] == 2


def test_a_break_with_nothing_else_saying_what_it_is_is_an_opening_of_unknown_kind(config):
    """Nothing is set aside for a person to decide. A gap the drawing never
    explained is reported as what it is."""
    page = _page_with([_opening("gap_in_the_wall", "W1", 0.3, 0.5, None, width=900)])
    merge_opening_evidence(page, config)
    kept = page["openings"][0]

    assert kept["element_type"] == "unknown_opening"
    assert kept["confidence_band"] == "low"
    assert kept["review_status"] == "auto_confirmed"
    assert "unknown kind" in kept["how_it_was_decided"]


def test_nothing_is_left_waiting_for_a_person(config):
    """Every outcome is decided here. A reading that stops to ask is a reading
    that cannot run on its own."""
    page = _page_with([
        _opening("gap_in_the_wall", "W1", 0.1, 0.2, None, width=900),
        _opening("window_symbol", "W1", 0.6, 0.8, "sliding_window", width=1200),
    ])
    merge_opening_evidence(page, config)

    assert all(o["review_status"] == "auto_confirmed" for o in page["openings"])
    assert all(o.get("how_it_was_decided") for o in page["openings"])


def test_every_opening_is_named_even_where_the_drawing_names_none(config):
    """An opening a reader cannot name is one they cannot check. A great many
    plans print no marks at all."""
    from pipeline.plan.openings import name_openings

    page = _page_with([
        _opening("window_symbol", "W1", 0.1, 0.2, "sliding_window", width=1200),
        _opening("door_swing", "W1", 0.6, 0.8, "door", width=820),
        _opening("mark_on_the_drawing", "W1", 0.85, 0.95, "window", mark="W7", width=900),
    ])
    name_openings([page], config)
    names = [o["display_mark"] for o in page["openings"]]

    assert names == ["W01", "D01", "W7"]
    assert page["openings"][0]["display_mark_is_made_up"] is True
    assert "display_mark_is_made_up" not in page["openings"][2]


# --- a break where a wall lands is not a door ------------------------------


def test_a_break_where_a_partition_lands_is_a_junction_not_a_door(config):
    """Both stop the wall's two faces at the same place and they mean opposite
    things: one is a hole to cut and count, the other is solid wall with a wall
    attached."""
    from pipeline.plan.walls import break_is_a_junction

    wall = _candidate("W1", "x", 200.0, 100.0, 600.0, 90.0)
    partition = _candidate("W2", "y", 300.0, 200.0, 500.0, 90.0)
    # the break is exactly as wide as the partition is thick
    assert break_is_a_junction(wall, 295.5, 304.5, [wall, partition], 10.0)


def test_a_door_beside_a_partition_is_still_a_door(config):
    """A junction break is about as wide as the wall that made it. A partition
    arriving at one jamb of an 820 mm door does not make the door a junction —
    and without that rule a plan drawn as a picture lost every opening it had."""
    from pipeline.plan.walls import break_is_a_junction

    wall = _candidate("W1", "x", 200.0, 100.0, 600.0, 90.0)
    partition = _candidate("W2", "y", 300.0, 200.0, 500.0, 90.0)
    assert not break_is_a_junction(wall, 295.5, 377.5, [wall, partition], 10.0)


# --- a face whose partner was never found ---------------------------------


def test_the_second_look_widens_the_thickness_and_nothing_else(config):
    """Every other test in the pairing is there for its own reason. Skipping
    them on the second look let a 230 mm square back in as a wall."""
    from pipeline.plan.walls import _pair_faces

    faces = [(100.0, 500.0, 523.0, [], {}), (123.0, 500.0, 523.0, [], {})]
    settings = {
        **config["walls"], "min_wall_length_mm": 200,
        "min_length_to_thickness": 3.0, "second_look_widening": 0.2,
    }
    assert _pair_faces(faces, 10.0, {"walls": settings}, []) == []


# --- a carport is not part of the house ------------------------------------


def test_a_structure_standing_apart_is_reported_as_its_own(config):
    """**A carport, a pergola and a detached garage are drawn on the same sheet
    and are not the same building.** They are not joined to it, so the house is
    one connected group of walls and the outbuilding is another — which is what
    the junctions already said, and it was being thrown away as "outside the
    building", which is true and useless."""
    from pipeline.plan.walls import mark_detached_structures

    house = [
        _candidate("H1", "x", 100.0, 100.0, 900.0, 90.0),
        _candidate("H2", "x", 600.0, 100.0, 900.0, 90.0),
        _candidate("H3", "y", 100.0, 100.0, 600.0, 90.0),
        _candidate("H4", "y", 900.0, 100.0, 600.0, 90.0),
    ]
    # A carport well clear of it, 4 m by 6 m at this scale.
    carport = [
        _candidate("C1", "x", 1200.0, 1200.0, 1600.0, 90.0),
        _candidate("C2", "x", 1800.0, 1200.0, 1600.0, 90.0),
        _candidate("C3", "y", 1200.0, 1200.0, 1800.0, 90.0),
        _candidate("C4", "y", 1600.0, 1200.0, 1800.0, 90.0),
    ]
    walls = house + carport
    assert mark_detached_structures(walls, config) == 4

    assert {w["wall_id"] for w in walls if w["building"] == "detached"} == {
        "C1", "C2", "C3", "C4"
    }
    assert all(w["building"] == "main" for w in house)
    # It is a wall, and it does meet other walls — it is simply not the house.
    assert all(w["meets_another_wall"] for w in carport)
    assert {w["structure_id"] for w in carport} == {"S01"}


def test_a_car_in_the_garage_is_not_a_detached_structure(config):
    """The first thing this rule found on a real plan was the car drawn in the
    garage: its outline and the dashed door swing make a closed group of four
    standing apart from the walls around it. So does a shower recess and a
    robe. A structure is metres across in both directions."""
    from pipeline.plan.walls import mark_detached_structures

    house = [
        _candidate("H1", "x", 100.0, 100.0, 900.0, 90.0),
        _candidate("H2", "x", 600.0, 100.0, 900.0, 90.0),
        _candidate("H3", "y", 100.0, 100.0, 600.0, 90.0),
        _candidate("H4", "y", 900.0, 100.0, 600.0, 90.0),
    ]
    # A closed box 1.5 m by 1.2 m: a shower recess, not a building.
    fitting = [
        _candidate("F1", "x", 1200.0, 1200.0, 1350.0, 90.0),
        _candidate("F2", "x", 1320.0, 1200.0, 1350.0, 90.0),
        _candidate("F3", "y", 1200.0, 1200.0, 1320.0, 90.0),
        _candidate("F4", "y", 1350.0, 1200.0, 1320.0, 90.0),
    ]
    walls = house + fitting
    assert mark_detached_structures(walls, config) == 0
    assert all(not w["meets_another_wall"] for w in fitting)
    assert "furniture or a fitting" in fitting[0]["not_used_because"]


def test_nothing_is_called_detached_where_there_is_no_building_to_detach_from(config):
    """On a sheet whose drawing is stored as a picture the tracing recovers the
    walls in pieces. Calling every piece but the biggest "detached" would be an
    invention."""
    from pipeline.plan.walls import mark_detached_structures

    scattered = [
        _candidate(f"W{n}", "x", 200.0 * n, 100.0, 400.0, 90.0) for n in range(1, 7)
    ]
    assert mark_detached_structures(scattered, config) == 0
    assert all(w["building"] == "main" for w in scattered)


# --- what is not a wall, read from the drawing and not from the words ------


def test_a_dashed_line_is_not_a_wall(config):
    """**A roof extent, an eave, a setback and a boundary are all drawn
    dashed**, and every office draws them that way whatever it calls them.
    Reading the words printed nearby only worked where the office wrote a word
    this reader knew, and it set aside real walls that ran under the note."""
    from pipeline.plan.walls import mark_walls_in_dead_ground

    wall = _candidate("W1", "x", 200.0, 100.0, 600.0, 90.0)
    eave = _candidate("W2", "x", 260.0, 100.0, 600.0, 90.0)
    eave["drawn_dashed"] = True

    assert mark_walls_in_dead_ground([wall, eave], None, []) == 1
    assert wall.get("meets_another_wall", True) is True
    assert eave["meets_another_wall"] is False
    assert "dashed" in eave["not_used_because"]


def test_the_pdfs_own_dash_pattern_is_read_where_it_gives_one(config):
    """PyMuPDF reports it in the PostScript form. These plan sets mostly export
    their dashes as separate short segments and leave the pattern empty, so
    this is one of two ways a dashed line is recognised — but where the file
    does say so outright, it is taken."""
    from pipeline.plan.layout import _states_a_dash_pattern

    assert _states_a_dash_pattern("[ 2.02 2.02 ] 0") is True
    assert _states_a_dash_pattern("[] 0") is False
    assert _states_a_dash_pattern(None) is False


def test_a_line_finishing_in_an_arrowhead_is_a_leader(config):
    """A leader is drawn from a note to the thing it describes and finishes in
    an arrowhead. Paired with any line beside it, it is a wall by every test
    that looks only at the lines — the arrowhead is what says otherwise, in the
    drawing rather than in words."""
    from pipeline.plan.walls import mark_walls_with_an_arrow_at_the_end

    leader = _candidate("W1", "x", 200.0, 100.0, 600.0, 90.0)
    wall = _candidate("W2", "y", 900.0, 100.0, 600.0, 90.0)
    heads = [(600.0, 200.0)]  # at the leader's far end

    assert mark_walls_with_an_arrow_at_the_end(
        [leader, wall], heads, {"arrow_reach_pt": 4.0}
    ) == 1
    assert leader["meets_another_wall"] is False
    assert wall.get("meets_another_wall", True) is True


def test_a_wall_running_past_an_arrowhead_is_still_a_wall(config):
    """A wall runs past the arrowheads of the dimensions that measure it all
    day long. What a leader does is *stop* at one."""
    from pipeline.plan.walls import mark_walls_with_an_arrow_at_the_end

    wall = _candidate("W1", "x", 200.0, 100.0, 600.0, 90.0)
    beside_it = [(350.0, 200.0)]  # partway along, not at an end

    assert mark_walls_with_an_arrow_at_the_end(
        [wall], beside_it, {"arrow_reach_pt": 4.0}
    ) == 0


def test_a_panel_of_printed_matter_is_found_from_where_the_text_is(config):
    """A title block, a legend and a notes column are the same thing: a dense
    rectangle of text pushed against an edge so it stays clear of the drawing.
    Its ruled rows are parallel lines a few millimetres apart at drawing
    scale — walls, to anything that looks only at the lines."""
    from pipeline.plan.walls import printed_panels

    # A notes column down the left edge: forty lines, one under the other.
    column = [
        {"bbox": [20.0, 40.0 + n * 9.0, 150.0, 48.0 + n * 9.0], "text": "note"}
        for n in range(40)
    ]
    # And the drawing's own labels, scattered across the middle.
    scattered = [
        {"bbox": [300.0 + n * 70.0, 300.0 + n * 40.0, 360.0 + n * 70.0, 310.0 + n * 40.0],
         "text": "BED"}
        for n in range(6)
    ]
    panels = printed_panels(column + scattered, 842.0, 595.0, config["walls"])

    assert len(panels) == 1
    x0, y0, x1, y1 = panels[0]
    assert x0 < 60 and x1 < 300, "the panel is the column down the edge"
    assert y1 > 300, "and it runs the depth of the column"


def test_the_drawings_own_labels_are_not_a_panel(config):
    """A dense patch in the middle of the paper is the drawing being busy — a
    stack of dimensions, a room with its finishes listed."""
    from pipeline.plan.walls import printed_panels

    middle = [
        {"bbox": [400.0, 200.0 + n * 9.0, 480.0, 208.0 + n * 9.0], "text": "x"}
        for n in range(20)
    ]
    assert printed_panels(middle, 842.0, 595.0, config["walls"]) == []


# --- two readings a door's width apart are one door ------------------------


def test_two_readings_alongside_each_other_on_one_wall_are_one_opening(config):
    """A door's swing is drawn from the hinge and a break is measured between
    the jambs, so the two sit alongside rather than on top of each other.
    Nearer than a door is wide, on one wall, there is not room for two."""
    page = _page_with([
        _opening("door_swing", "W1", 0.30, 0.42, "door", width=820),
        _opening("gap_in_the_wall", "W1", 0.44, 0.56, None, width=830),
    ])
    result = merge_opening_evidence(page, config)

    assert result["openings"] == 1
    assert page["openings"][0]["element_type"] == "door"


def test_a_swing_outranks_the_window_drawn_inside_the_wall(config):
    """The swing is the door leaf itself, swept out to its own width. The
    window drawn inside a wall is the opening, but its width is read off the
    ends rather than swept."""
    page = _page_with([
        _opening("window_symbol", "W1", 0.30, 0.45, "sliding_window", width=900),
        _opening("door_swing", "W1", 0.31, 0.46, "door", width=820),
    ])
    merge_opening_evidence(page, config)

    assert page["openings"][0]["element_type"] == "door"


def test_two_openings_a_room_apart_stay_two(config):
    """The rule is a door's width, not a room's."""
    page = _page_with([
        _opening("window_symbol", "W1", 0.05, 0.18, "fixed_window", width=800),
        _opening("window_symbol", "W1", 0.70, 0.85, "sliding_window", width=900),
    ])
    assert merge_opening_evidence(page, config)["openings"] == 2


def test_an_opening_with_no_mark_is_never_an_unmatched_mark(config):
    """It was measured from a break, or read from the window drawn inside the
    wall, or from a door's swing. It can neither match a schedule row nor be
    missing from one."""
    from pipeline.plan.openings import reconcile_openings_with_schedules

    page = _page_with([
        _opening("window_symbol", "W1", 0.30, 0.45, "sliding_window", width=900),
        _opening("door_swing", "W1", 0.60, 0.72, "door", width=820),
    ])
    page["schedules"] = []
    result = reconcile_openings_with_schedules([page])

    assert result["marks_without_a_schedule"] == []
