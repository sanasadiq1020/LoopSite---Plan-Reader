"""Day 5 — the canonical building model.

This is the contract between every stage that follows. The 3D model, the four
elevations, the take-off and the crew work package all read from here, and none
of them reads the plan again or redraws what this holds (Critical Rule 2).

Three things define it:

*   **Millimetres, everywhere.** The plan is read in PDF points, which mean
    nothing on their own. Every length here has been through the sheet's own
    calibrated scale, and a sheet whose scale could not be trusted produces no
    model at all rather than a plausible wrong one.

*   **One coordinate system for the building, not for the page.** A PDF's Y
    grows downward and its origin is the page corner; a building's does not.
    The model puts X to the east, Y to the north and Z up, with the origin at
    the building's own south-west corner, so the numbers are about the
    building and not about where it happened to sit on the sheet.

*   **Every element says where it came from.** `element_id`, `element_type`,
    `storey`, `geometry`, `dimensions`, `source_sheet`, `source_bbox`,
    `extraction_method`, `confidence`, `review_status` and `linked_issue_ids`
    on every record (Critical Rule 12), so one wall in the 3D model can be
    traced back to the two lines on the sheet it was measured from.

Nothing here is specific to any plan. Which sheet is modelled is chosen by the
reader; the height comes from the drawing (see `height.py`); thicknesses are
measured; and everything that could not be established is carried as an
assumption on the record rather than quietly filled in.
"""

from datetime import datetime, timezone

from app.logging_setup import get_logger

logger = get_logger()

# The shape of project_model.json. An older model read by newer code would be
# silently wrong rather than obviously broken, so it is versioned like the
# plan reading is.
MODEL_FORMAT = 1


def modellable_sheets(pages: list, config: dict) -> list:
    """Every sheet, and whether a model can be built from it.

    A sheet that cannot be modelled is listed with the reason rather than
    hidden, so the reader can see why their floor plan is not on the list.
    """
    minimum_length = float(config.get("model", {}).get("min_wall_length_mm", 300))
    out = []
    for page in pages:
        calibration = page.get("scale_calibration") or {}
        walls = [
            w for w in page.get("walls", []) if w.get("length_mm", 0) >= minimum_length
        ]
        if not page.get("page_type", {}).get("draws_a_plan"):
            reason = "This sheet does not draw the building in plan."
        elif not calibration.get("usable_for_measurement"):
            reason = (
                "Nothing can be measured from this sheet: "
                + (calibration.get("note") or "its scale could not be established.")
            )
        elif not walls:
            reason = "No wall lines long enough to build with were found on this sheet."
        else:
            reason = None

        out.append(
            {
                "page_number": page["page_number"],
                "sheet_id": page["sheet_id"],
                "sheet_number": page["title_block"]["sheet_number"]["value"],
                "sheet_title": page["title_block"]["sheet_title"]["value"],
                "page_type": page["page_type"]["value"],
                "wall_count": len(walls),
                "opening_count": len(page.get("openings", [])),
                "room_count": len(page.get("rooms", [])),
                "can_be_modelled": reason is None,
                "reason": reason,
            }
        )
    return out


def choose_default_sheet(sheets: list):
    """The sheet a reader most likely wants modelled.

    The one that can be modelled and carries the most walls: on a set with a
    floor plan, a reflected-ceiling plan and an electrical plan drawn over the
    same outline, that is the floor plan.
    """
    usable = [s for s in sheets if s["can_be_modelled"]]
    if not usable:
        return None
    return max(usable, key=lambda s: (s["wall_count"], s["room_count"]))["page_number"]


def _millimetres_per_point(page: dict):
    calibration = page.get("scale_calibration") or {}
    return calibration.get("measured_mm_per_point") or calibration.get(
        "printed_mm_per_point"
    )


def build_model(
    page: dict,
    height: dict,
    config: dict,
    run_id: str,
    source_file: str,
    page_height_pt: float,
) -> dict:
    """The canonical model for one sheet, in millimetres.

    ``page_height_pt`` is the sheet's height in PDF points, needed to turn the
    page's downward Y into the building's northward Y.
    """
    settings = config.get("model", {})
    minimum_length = float(settings.get("min_wall_length_mm", 300))
    default_thickness = float(settings.get("default_wall_thickness_mm", 90))

    mm_per_point = _millimetres_per_point(page)
    if not mm_per_point:
        raise ValueError("This sheet has no usable scale, so nothing can be measured.")

    walls_in = [
        w for w in page.get("walls", []) if w.get("length_mm", 0) >= minimum_length
    ]
    if not walls_in:
        raise ValueError("This sheet has no wall lines long enough to build with.")

    # --- the building's own origin ---------------------------------------
    # Everything is shifted so the building's south-west corner is (0, 0).
    # Until that is done the numbers describe a position on a piece of paper.
    xs = [p for w in walls_in for p in (w["start_point_pt"][0], w["end_point_pt"][0])]
    ys = [p for w in walls_in for p in (w["start_point_pt"][1], w["end_point_pt"][1])]
    left_pt, top_pt, bottom_pt = min(xs), min(ys), max(ys)

    def to_mm(point):
        """A point on the page, as a position in the building, in millimetres.

        X runs east and Y runs north. A PDF's Y grows *downward*, so it is
        turned over here — without that the model is a mirror image of the
        plan, which looks entirely convincing and is wrong.
        """
        x_pt, y_pt = point
        return [
            round((x_pt - left_pt) * mm_per_point, 1),
            round((bottom_pt - y_pt) * mm_per_point, 1),
        ]

    storey = {
        "storey_id": "S01",
        "name": page["title_block"]["sheet_title"]["value"] or page["sheet_id"],
        "elevation_mm": 0.0,
        "height_mm": height["value_mm"],
        "height_source": height["source"],
        "height_confidence": height["confidence"],
        "height_note": height["note"],
    }

    elements = []
    thickness_assumed = 0
    for position, wall in enumerate(sorted(walls_in, key=lambda w: -w["length_mm"]), 1):
        measured_thickness = wall.get("thickness_mm")
        thickness_is_measured = bool(measured_thickness)
        if not thickness_is_measured:
            measured_thickness = default_thickness
            thickness_assumed += 1

        assumptions = []
        if not thickness_is_measured:
            assumptions.append(
                f"Thickness not measured on the sheet; the office default of "
                f"{default_thickness:.0f} mm was used."
            )
        if height["source"] == "office_default":
            assumptions.append(
                "Height is the office default — this plan set states none anywhere."
            )
        if wall.get("longer_than_sheet_measures"):
            assumptions.append(
                "This wall is longer than any distance the sheet dimensions, so it may "
                "be a boundary or an eave line rather than a wall."
            )
        if wall.get("line_source") == "rendered_page":
            assumptions.append(
                "Measured from the sheet as a picture, because its drawing is stored as "
                "an image rather than as lines."
            )

        elements.append(
            {
                # --- Critical Rule 12: every field, on every element -------
                "element_id": f"{page['sheet_id']}-M-W{position:03d}",
                "element_type": "wall",
                "storey": storey["storey_id"],
                "geometry": {
                    # The wall's centreline on the floor, and how far up it
                    # goes. A solid is these two facts plus the thickness.
                    "start_mm": to_mm(wall["start_point_pt"]),
                    "end_mm": to_mm(wall["end_point_pt"]),
                    "base_elevation_mm": 0.0,
                    "runs_along": wall["runs_along"],
                },
                "dimensions": {
                    "length_mm": round(wall["length_mm"], 1),
                    "thickness_mm": round(measured_thickness, 1),
                    "height_mm": storey["height_mm"],
                    "thickness_is_measured": thickness_is_measured,
                    "nominal_thickness_mm": wall.get("nominal_thickness_mm"),
                },
                "material": None,  # not stated on a plan; Week 3 reads it from the take-off
                "source_sheet": page["sheet_id"],
                "source_bbox": wall.get("bbox"),
                "extraction_method": (
                    "paired_parallel_lines_"
                    + ("rendered_page" if wall.get("line_source") == "rendered_page" else "vector")
                ),
                "confidence": wall.get("confidence", 0.5),
                "confidence_band": wall.get("confidence_band", "review"),
                "review_status": wall.get("review_status", "needs_review"),
                "linked_issue_ids": [],
                "linked_opening_ids": [],
                "assumptions": assumptions,
                "from_wall_id": wall["wall_id"],
            }
        )

    # --- openings are carried, not yet cut -------------------------------
    # Day 6 cuts these as voids in the same walls. They are in the canonical
    # model now because the model is the contract: Day 6 reads from here, not
    # from the plan again.
    openings = []
    by_wall = {e["from_wall_id"]: e for e in elements}
    for position, opening in enumerate(page.get("openings", []), 1):
        host = by_wall.get(opening.get("wall_id"))
        openings.append(
            {
                "element_id": f"{page['sheet_id']}-M-O{position:03d}",
                "element_type": opening.get("element_type") or "opening",
                "storey": storey["storey_id"],
                "geometry": {"centre_mm": None, "cut_as_void": False},
                "dimensions": {
                    "width_mm": opening.get("width_mm"),
                    "height_mm": opening.get("height_mm"),
                    "sill_height_mm": opening.get("sill_height_mm"),
                },
                "material": None,
                "mark": opening.get("mark"),
                "in_wall": host["element_id"] if host else None,
                "source_sheet": opening.get("source_sheet"),
                "source_bbox": opening.get("source_bbox"),
                "extraction_method": opening.get("found_by", "mark_on_the_drawing"),
                "confidence": opening.get("confidence", 0.5),
                "confidence_band": opening.get("confidence_band", "review"),
                "review_status": opening.get("review_status", "needs_review"),
                "linked_issue_ids": [],
                "assumptions": [
                    "Carried into the model with the wall it belongs to, but not yet cut as an "
            "opening in it."
                ],
            }
        )
        if host:
            host["linked_opening_ids"].append(openings[-1]["element_id"])

    extent_x = max(max(e["geometry"]["start_mm"][0], e["geometry"]["end_mm"][0]) for e in elements)
    extent_y = max(max(e["geometry"]["start_mm"][1], e["geometry"]["end_mm"][1]) for e in elements)

    model = {
        "format_version": MODEL_FORMAT,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "source_file": source_file,
        "modelled_sheet": {
            "page_number": page["page_number"],
            "sheet_id": page["sheet_id"],
            "sheet_number": page["title_block"]["sheet_number"]["value"],
            "sheet_title": page["title_block"]["sheet_title"]["value"],
        },
        "units": "millimetres",
        "coordinate_system": {
            "x": "east, from the building's south-west corner",
            "y": "north, from the building's south-west corner",
            "z": "up, from the floor of this storey",
            "note": (
                "The page's downward Y is turned over so that north is up. Without that "
                "the model is a convincing mirror image of the plan."
            ),
            "millimetres_per_page_point": round(mm_per_point, 4),
            "scale_result": (page.get("scale_calibration") or {}).get("result"),
        },
        "extent_mm": {"x": round(extent_x, 1), "y": round(extent_y, 1), "z": storey["height_mm"]},
        "storeys": [storey],
        "walls": elements,
        "openings": openings,
        "assumptions": _collect_assumptions(height, thickness_assumed, len(elements), page),
    }
    logger.info(
        f"canonical model for {page['sheet_id']}: {len(elements)} walls, "
        f"{len(openings)} openings, storey {storey['height_mm']:.0f} mm "
        f"({storey['height_source']})"
    )
    return model


def _collect_assumptions(height: dict, thickness_assumed: int, wall_count: int, page: dict) -> list:
    """Everything this model takes on trust, in one place.

    Week 1's exit gate asks for assumptions to be visible, and a reader is
    entitled to see what a model is resting on before they measure anything
    off it.
    """
    out = [
        {
            "about": "storey height",
            "statement": height["note"],
            "confidence": height["confidence"],
        },
        {
            "about": "one storey",
            "statement": (
                "This model is the one sheet named above, built as a single storey. "
                "Sheets are not combined into a multi-storey building."
            ),
            "confidence": 1.0,
        },
        {
            "about": "walls are candidates",
            "statement": (
                f"All {wall_count} walls are pairs of parallel lines measured from the "
                "sheet. They are candidates for review, not confirmed walls."
            ),
            "confidence": 1.0,
        },
    ]
    if thickness_assumed:
        out.append(
            {
                "about": "wall thickness",
                "statement": (
                    f"{thickness_assumed} of {wall_count} walls had no measurable "
                    "thickness and use the office default."
                ),
                "confidence": 0.5,
            }
        )
    calibration = page.get("scale_calibration") or {}
    if calibration.get("result") == "contradicted":
        out.append(
            {
                "about": "scale",
                "statement": (
                    "The scale printed on this sheet is wrong for it; every length here "
                    "is measured from the sheet's own dimension strings instead. "
                    + (calibration.get("note") or "")
                ),
                "confidence": 0.7,
            }
        )
    return out
