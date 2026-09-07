"""The reader and the API must agree about what a reading contains.

**The failure this exists to stop.** A scale status was added to the reader and
to the browser's TypeScript, and not to the API's response schema, which
declares that field as a closed set of strings. The reader then emitted a value
the schema did not list, and Pydantic does not degrade a field it cannot
validate - it rejects the **entire response**. Every sheet on that plan came
back empty, every tab read zero, and the interface said only that the results
could not be loaded. Nothing in the type checker, the unit tests or the frontend
build could see it, because each side was individually correct.

Two ways for the two sides to disagree, and a test for each:

*   **A value the schema does not list**, which fails the whole reading. Caught
    by comparing the reader's own list of what it can emit against the schema's.
*   **A field the schema does not declare**, which is quietly dropped from the
    response and never reaches the interface at all - the same defect, silent
    rather than loud. Caught by comparing the record the reader builds against
    the schema's fields, with the deliberately internal ones named.
"""

import typing

from app import schemas
from pipeline.plan import scale as scale_module


def _literal_values(annotation) -> set:
    """Every string a Literal field will accept, through Optional and unions."""
    found = set()
    if typing.get_origin(annotation) is typing.Literal:
        found |= {v for v in typing.get_args(annotation) if isinstance(v, str)}
    for argument in typing.get_args(annotation) or ():
        found |= _literal_values(argument)
    return found


def test_every_scale_result_the_reader_can_report_is_in_the_response_schema():
    """A status the schema has never heard of empties the whole plan.

    ``scale.RESULTS`` is the reader's own statement of what it can produce, so
    this compares the two sides directly rather than restating either.
    """
    declared = _literal_values(schemas.ScaleCalibration.model_fields["result"].annotation)
    missing = sorted(set(scale_module.RESULTS) - declared)
    assert not missing, (
        f"the reader can report {missing} and the response schema does not list "
        f"{'it' if len(missing) == 1 else 'them'}, so any plan with such a sheet would "
        "fail validation and reach the browser as an empty reading"
    )


def test_the_scale_record_carries_no_field_the_response_would_drop():
    """A field the schema does not declare never reaches the interface."""
    record = scale_module.calibrate_page("1:100", [], [], {"scale_calibration": {}})
    undeclared = sorted(set(record) - set(schemas.ScaleCalibration.model_fields))
    assert not undeclared, (
        f"the scale record carries {undeclared}, which the response schema does not "
        "declare, so the interface would never see them"
    )


# Wall fields that are deliberately not part of the API's answer: each is used
# to build another output rather than to be shown. Named here so that adding a
# field is a decision rather than an accident - a new one fails this test until
# it is either declared in the schema or added to this list on purpose.
WALL_FIELDS_KEPT_OUT_OF_THE_RESPONSE = {
    "gaps_pt",            # the breaks in points; the openings carry these on screen
    "junction_count",     # summarised for the table by connects_to
    "merged_from",        # how many traced pieces became this wall
    "source_page",        # the page number, already on the sheet
    "source_sheet",       # the sheet id, already on the sheet
    "stroke_pt",          # how heavily the line was plotted
    "wall_group_size",    # how many walls its connected group holds
    "inside_the_drawing", # whether it sits in the part of the sheet the plan is on
    "interior_drawn_as",  # solid, hatched or outline
    "thickness_is_assumed",
    "found_on_a_second_look",
}


def test_a_wall_record_carries_no_field_the_response_would_silently_drop():
    """Every field a wall gains must be declared or deliberately withheld.

    Built from the record-making function itself rather than from a run, so it
    is deterministic and needs no PDF.
    """
    from pipeline.plan import cvwalls

    piece = {
        "runs_along": "x",
        "position_pt": 100.0,
        "start_pt": 0.0,
        "end_pt": 100.0,
        "thickness_mm": 230.0,
        "length_mm": 3500.0,
        "start_point_pt": [0.0, 100.0],
        "end_point_pt": [100.0, 100.0],
        "face_positions_pt": [96.0, 104.0],
        "confidence": 0.8,
        "measured_from": "test",
        "drawn_as": "outline",
        "thickness_from": "page_faces",
        "thickness_uncertainty_mm": 0.4,
    }
    record = cvwalls._one_wall([piece], 35.278, 300.0, 12.0)
    undeclared = (
        set(record)
        - set(schemas.WallCandidate.model_fields)
        - WALL_FIELDS_KEPT_OUT_OF_THE_RESPONSE
    )
    assert not undeclared, (
        f"a wall record carries {sorted(undeclared)}, which the response schema does not "
        "declare and which is not in the list of fields deliberately kept out of it. "
        "Declare it in schemas.WallCandidate so the interface can show it, or name it "
        "in WALL_FIELDS_KEPT_OUT_OF_THE_RESPONSE to say it is internal on purpose"
    )


def test_a_wall_record_validates_against_the_response_schema():
    """The record the reader builds is a thing the API can actually answer with."""
    from pipeline.plan import cvwalls

    piece = {
        "runs_along": "y",
        "position_pt": 50.0,
        "start_pt": 0.0,
        "end_pt": 80.0,
        "thickness_mm": 90.0,
        "length_mm": 2800.0,
        "start_point_pt": [50.0, 0.0],
        "end_point_pt": [50.0, 80.0],
        "face_positions_pt": [48.7, 51.3],
        "confidence": 0.7,
        "measured_from": "test",
        "drawn_as": "outline",
        "thickness_from": "band_less_stroke",
        "thickness_uncertainty_mm": 1.2,
    }
    record = cvwalls._one_wall([piece], 35.278, 300.0, 12.0)
    record.setdefault("wall_id", "P01-W001")
    record.setdefault("line_source", "cv_raster")
    # Raises if any declared field is the wrong shape.
    schemas.WallCandidate.model_validate(record)
