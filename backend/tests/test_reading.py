"""Day 3 — regression tests for the plan-reading pipeline.

Almost every case here is taken from a real failure measured against the two
supplied Australian plan sets, which use deliberately different title-block
conventions. Each test names the failure it locks down, so a future change
that reintroduces it fails here rather than in a run nobody checks.

Fixtures are built as text lines in PDF points, the same shape
``textmodel.build_page_lines`` produces, so the detectors are tested through
their real interfaces rather than through mocks.
"""

import pytest

from pipeline.plan import accuracy
from pipeline.plan import dimensions as dimensions_module
from pipeline.plan import openings as openings_module
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
    else:
        start_point, end_point = [line, start], [line, end]
    return {
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
