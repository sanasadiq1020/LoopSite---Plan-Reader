"""Reading sheets drawn the way a different office draws them.

A plan set from an office nobody here has seen will put its title block on a
different edge, print it sideways, name its drawings differently, and caption
the drawing rather than the title block. Each case here is one of those, and
each locks down a rule that was wrong before it was written.

Fixtures are built as text lines in PDF points, the same shape
``textmodel.build_page_lines`` produces, so the detectors are tested through
their real interfaces rather than through mocks.
"""

import pytest

from pipeline.plan import reading
from pipeline.plan.layout import value_candidates, joined_text
from pipeline.plan.pagetype import detect_page_type
from pipeline.plan.textmodel import make_line


@pytest.fixture(scope="module")
def config():
    return reading.load_config()


def line(text, x0, y0, x1, y1, size=8.0, axis="horizontal", direction=(1.0, 0.0)):
    return make_line(
        text=text,
        bbox=[x0, y0, x1, y1],
        extraction_method="native",
        confidence=1.0,
        axis=axis,
        size=size,
        direction=direction,
    )


def room_and_dimension_lines(rooms=6, dimensions=8):
    lines = []
    names = ["KITCHEN", "LOUNGE", "BED 1", "BED 2", "BATH", "LAUNDRY", "GARAGE", "ENTRY"]
    for i in range(rooms):
        lines.append(line(names[i % len(names)], 100, 100 + i * 40, 160, 110 + i * 40))
    for i in range(dimensions):
        lines.append(line(f"{3000 + i * 110:,}", 300 + i * 60, 90, 340 + i * 60, 100, size=6.0))
    return lines


# --- What kind of sheet is this -------------------------------------------


def test_the_sheet_title_decides_when_it_names_a_drawing(config):
    result = detect_page_type("GROUND FLOOR PLAN", [], 6, 8, 0, config)
    assert result["value"] == "floor_plan"
    assert result["technique"] == "sheet_title"
    assert result["named_as_a_plan"] is True


def test_a_drawing_captioned_on_the_sheet_names_it_when_the_title_block_does_not(config):
    """Many offices put a generic project title in the title block and name
    the drawing in large type under the drawing itself. Before this, such a
    sheet was reported as an unclassified page with nothing done to it."""
    lines = room_and_dimension_lines() + [
        line("PROPOSED GROUND FLOOR PLAN", 60, 40, 400, 66, size=20.0)
    ]
    result = detect_page_type("LOT 88 HARDING ROAD", lines, 6, 8, 0, config)
    assert result["value"] == "floor_plan"
    assert result["technique"] == "drawing_caption"
    assert result["named_as_a_plan"] is True
    assert "PROPOSED GROUND FLOOR PLAN" in (result["note"] or "")


def test_a_small_section_marker_does_not_decide_what_a_sheet_is(config):
    """A cutting-line marker refers to another sheet. Only the largest caption
    on the sheet counts, which is what keeps a floor plan from being called a
    section because it is marked where the sections were cut."""
    lines = room_and_dimension_lines() + [
        line("PROPOSED GROUND FLOOR PLAN", 60, 40, 400, 66, size=20.0),
        line("SECTION A-A", 500, 300, 560, 307, size=5.0),
    ]
    result = detect_page_type(None, lines, 6, 8, 0, config)
    assert result["value"] == "floor_plan"


def test_a_small_buildings_plan_is_still_a_plan(config):
    """An extension, a granny flat or a shed names two or three rooms. Needing
    four meant its only floor plan was never recognised as one."""
    result = detect_page_type("PROPOSED PLAN", [], 2, 12, 0, config)
    assert result["value"] == "floor_plan"
    assert result["draws_a_plan"] is True


def test_printed_height_levels_with_no_rooms_are_a_vertical_drawing(config):
    """Reduced levels are how a drawing states a height. A sheet full of them
    with no room names is the building seen from the side, never a plan."""
    lines = [
        line("RL 100.400", 100, 100, 160, 110),
        line("FFL 100.550", 100, 140, 160, 150),
        line("NGL 99.800", 100, 180, 160, 190),
        line("CL 103.100", 100, 220, 160, 230),
    ]
    result = detect_page_type(None, lines, 0, 0, 0, config)
    assert result["value"] == "elevation"
    assert result["draws_a_plan"] is False


def test_a_sheet_that_names_nothing_says_so_plainly(config):
    result = detect_page_type(None, [line("A", 10, 10, 20, 20)], 0, 0, 0, config)
    assert result["value"] == "unknown"
    assert result["note"] and "does not name a kind of drawing" in result["note"]
    assert result["draws_a_plan"] is False


def test_a_sheet_named_as_a_plan_with_nothing_on_it_is_flagged_not_trusted(config):
    result = detect_page_type("FLOOR PLAN", [], 0, 0, 0, config)
    assert result["value"] == "floor_plan"
    assert result["content_agrees_with_title"] is False
    assert result["note"] and "reading may be incomplete" in result["note"]


def test_a_section_is_never_promoted_to_a_plan_by_its_contents(config):
    """A section prints the names of the spaces it cuts through and dimensions
    them. Letting contents promote those found walls on sheets that draw no
    plan at all."""
    result = detect_page_type("SECTION 1", [], 8, 20, 0, config)
    assert result["value"] == "section"
    assert result["draws_a_plan"] is False


# --- A title block printed sideways ---------------------------------------


def _sideways_title_block(reading_downward: bool):
    """A label and its value printed one above the other, both turned 90
    degrees, the way a title strip up the edge of a sheet is set."""
    direction = (0.0, 1.0) if reading_downward else (0.0, -1.0)
    label = line("SCALE", 24, 200, 32, 240, size=6.0, axis="vertical", direction=direction)
    value = line("1:100", 54, 200, 65, 240, size=8.0, axis="vertical", direction=direction)
    return label, [label, value]


@pytest.mark.parametrize("reading_downward", [True, False])
def test_a_title_block_printed_sideways_is_read_either_way_up(reading_downward):
    """Sideways text runs two ways and the two are opposites: a strip up the
    right edge reads bottom to top, one up the left edge reads top to bottom.
    Turning the page only one way read one of them and reported the other as
    blank."""
    label, lines = _sideways_title_block(reading_downward)
    candidates = value_candidates(label, lines, {"h": [], "v": []}, 1200.0, 842.0, {"SCALE"})
    assert candidates, "no value was offered for a sideways label"
    assert any("1:100" in joined_text(c["lines"]) for c in candidates)


def test_text_at_right_angles_to_a_label_is_not_its_value():
    """A title strip up the edge of a sheet has whatever the drawing prints
    running past it. A note printed across the sheet is not the strip's value,
    and reading it as one reported a paragraph of general notes as a drawing
    number."""
    label = line("SCALE", 24, 200, 32, 240, size=6.0, axis="vertical", direction=(0.0, 1.0))
    across = line("ALL WORK TO COMPLY WITH THE NCC", 40, 205, 300, 213, size=7.0)
    candidates = value_candidates(label, [label, across], {"h": [], "v": []}, 1200.0, 842.0, {"SCALE"})
    assert all(
        "COMPLY" not in joined_text(c["lines"]) for c in candidates
    ), "text printed across the sheet was offered as a sideways label's value"


# --- A table is a panel, not the whole sheet ------------------------------


def test_a_table_read_across_the_whole_sheet_is_not_believed(config):
    """The area a table covers is excluded from room and dimension detection,
    so a table read wrongly hides the drawing behind it. On one sheet a title
    block printed as a row of labels across the top was read as a drawing
    index, and every room on that plan disappeared."""
    page_width, page_height = 1191.0, 842.0
    lines = room_and_dimension_lines(rooms=6, dimensions=8) + [
        line("PROJECT", 20, 20, 60, 27, size=6.0),
        line("DRAWING", 200, 20, 250, 27, size=6.0),
        line("SCALE", 420, 20, 450, 27, size=6.0),
        line("REVISION", 560, 20, 610, 27, size=6.0),
        line("RIVERBEND STAGE 2", 20, 34, 140, 44),
        line("GROUND FLOOR PLAN", 200, 34, 330, 44),
        line("1:100", 420, 34, 450, 44),
        line("C", 560, 34, 568, 44),
    ]
    page = reading.analyze_page(
        page_number=1,
        page_count=1,
        page_width=page_width,
        page_height=page_height,
        lines=lines,
        text_evidence={},
        rulings={"h": [], "v": []},
    )
    assert page["error"] is None
    # The rooms printed on the drawing survive: no table swallowed them.
    assert len(page["rooms"]) >= 1


# --- The title block is wherever the office put it -------------------------


def _ruled_block(x, y, cols=4, cell_w=165.0, cell_h=27.0):
    """A title block of eight ruled label/value cells at a given corner."""
    fields = [
        ("PROJECT", "RIVERBEND STAGE 2"), ("DRAWING TITLE", "GROUND FLOOR PLAN"),
        ("DRAWING NO", "AR-104"), ("SCALE", "1:100"), ("REVISION", "C"),
        ("DATE", "14.03.2025"), ("DRAWN BY", "TMK"), ("CHECKED BY", "RJP"),
    ]
    rows_count = len(fields) // cols
    lines, horizontals, verticals = [], [], []
    for i, (label, value) in enumerate(fields):
        cx = x + (i % cols) * cell_w
        cy = y + (i // cols) * cell_h
        lines.append(line(label, cx + 2, cy + 4, cx + 40, cy + 10, size=6.0))
        lines.append(line(value, cx + 2, cy + 14, cx + 90, cy + 23, size=9.0))
    width, height = cols * cell_w, rows_count * cell_h
    # The block's own frame, which is what says where it ends.
    horizontals += [(y, x, x + width), (y + height, x, x + width)]
    verticals += [(x, y, y + height), (x + width, y, y + height)]
    return lines, {"h": horizontals, "v": verticals}, [x, y, x + width, y + height]


@pytest.mark.parametrize(
    "corner",
    ["top_left", "top_middle", "top_right", "left_middle", "right_middle",
     "bottom_left", "bottom_middle", "bottom_right"],
)
def test_a_title_block_is_found_wherever_the_office_put_it(corner):
    """Offices put the block on any edge and at any end of it. Finding it from
    the box the office drew works at every one of those positions; a rectangle
    padded around wherever its labels sit does not."""
    from pipeline.plan.titleblock import find_title_block_region
    from pipeline.plan.reading import load_config

    page_width, page_height = 1191.0, 842.0
    width, height = 660.0, 54.0
    xs = {"left": 20.0, "middle": (page_width - width) / 2, "right": page_width - width - 20}
    ys = {"top": 20.0, "middle": (page_height - height) / 2, "bottom": page_height - height - 20}
    vertical, horizontal = corner.split("_")
    if vertical in ("left", "right"):        # a block partway up an edge
        x, y = xs[vertical], ys["middle"]
    else:
        x, y = xs[horizontal], ys[vertical]

    lines, rulings, expected = _ruled_block(x, y)
    region = find_title_block_region(
        lines, load_config()["title_block"]["field_labels"], page_width, page_height, rulings
    )
    assert region is not None, f"no title block found at {corner}"
    assert region == pytest.approx(expected, abs=1.0), f"wrong box at {corner}"


def test_a_sheet_with_no_title_block_is_still_read_and_says_so():
    """Some sheets simply do not carry a title block. That sheet is still read
    in full and identified by its position in the document - what it must not
    do is come back blank with no explanation."""
    lines = room_and_dimension_lines(rooms=6, dimensions=8)
    page = reading.analyze_page(
        page_number=3, page_count=6, page_width=1191.0, page_height=842.0,
        lines=lines, text_evidence={}, rulings={"h": [], "v": []},
    )
    assert page["error"] is None
    assert page["title_block_found"] is False
    assert page["title_block_note"] and "No title block" in page["title_block_note"]
    assert page["sheet_id"], "a sheet must always be identifiable"
    assert len(page["rooms"]) >= 1, "the drawing is still read without a title block"


def test_a_sheet_with_nothing_printed_on_it_still_gets_an_identity():
    """The plainest case of all: no title block and nothing to read. The sheet
    is still listed, still numbered by its place in the document, and says
    what it could not find."""
    page = reading.analyze_page(
        page_number=4, page_count=6, page_width=1191.0, page_height=842.0,
        lines=[], text_evidence={}, rulings={"h": [], "v": []},
    )
    assert page["error"] is None
    assert page["title_block_found"] is False
    assert page["title_block_note"] and "position in the document" in page["title_block_note"]
    assert page["sheet_id"]
    assert page["page_type"]["value"] == "unknown"


# --- A roof plan is not a floor plan ---------------------------------------


def test_a_roof_plan_is_its_own_kind_of_sheet(config):
    """A roof plan is drawn looking down like a floor plan, but its parallel
    lines are battens, ridges and gutters. Treating it as a floor plan
    reported 202 of them as walls of the building."""
    result = detect_page_type("ROOF PLAN", [], 0, 0, 0, config)
    assert result["value"] == "roof_plan"
    assert result["draws_a_plan"] is False


def test_walls_are_never_traced_on_a_roof_plan_or_a_site_plan(config):
    never = set(config["walls"]["never_trace_walls_on"])
    assert {"roof_plan", "site_plan"} <= never
