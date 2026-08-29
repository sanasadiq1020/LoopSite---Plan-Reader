"""Day 3 — plan-reading orchestrator.

Turns one page of text into a source-linked reading: title block, page type,
room labels, dimensions with their axis and arithmetic checks, schedule tables,
legends, and an explicit list of everything unresolved.

The detection work lives in focused modules — ``textmodel`` (what a line of
text is), ``layout`` (label/value geometry), ``titleblock``, ``pagetype``,
``rooms``, ``dimensions``, ``schedules``, ``sheetindex``, ``overlay``. This
file only decides the order they run in and assembles the result, so each
detector can be read, reasoned about and tested on its own.

Order matters and is deliberate:

1.  Title block first — it identifies the sheet, and it marks out the region
    that must be excluded from every other detector, so a drawing date is
    never read as a dimension and a footer is never read as schedule data.
2.  Rooms, then dimensions — a dimension can only be attributed to a room that
    has already been found.
3.  Schedules and legends — tables, read from their printed headers.
4.  Page type — decided from the title and confirmed against what the page
    actually contains.
5.  Discipline — which needs the sheet number and the page type.

Every stage is wrapped so that one failing detector degrades that section to
empty-and-flagged rather than losing the page (Critical Rule 6).
"""

import csv
import json
from pathlib import Path

from app.logging_setup import get_logger
from app.paths import CONFIG_DIR
from pipeline.plan import dimensions as dimensions_module
from pipeline.plan import rooms as rooms_module
from pipeline.plan import schedules as schedules_module
from pipeline.plan.layout import extract_rulings
from pipeline.plan.openings import openings_from_wall_gaps, place_openings_on_walls
from pipeline.plan.pagetype import detect_page_type
from pipeline.plan.scale import calibrate_page
from pipeline.plan.walls import detect_walls
from pipeline.plan.sheetindex import parse_sheet_index
from pipeline.plan.textmodel import bbox_center
from pipeline.plan.titleblock import (
    FIELD_NAMES,
    detect_title_block,
    empty_field,
    resolve_discipline,
    sheet_id_for,
)
from pipeline.plan.validators import scale_ratios

logger = get_logger()

# Typographic characters used in the messages this module produces for
# people reading the results.
LDQ, RDQ, DASH = "\u201c", "\u201d", "\u2014"

CONFIG_PATH = CONFIG_DIR / "plan_reading.json"

# Used only if config/plan_reading.json is missing or unreadable, so a missing
# config degrades to a logged warning and a reduced run rather than a crash
# (Critical Rule 6). The file on disk is the source of truth.
_MINIMAL_CONFIG = {
    "confidence_thresholds": {"review": 0.75, "low": 0.5},
    "title_block": {"field_labels": {}, "sheet_title_keywords": [], "title_exclusion_keywords": []},
    "sheet_index": {"column_header_keywords": {}},
    "page_types": {"keywords": {}},
    "discipline": {"prefix_map": {}, "text_keywords": {}, "page_type_map": {}},
    "rooms": {"keywords": [], "exclusion_keywords": []},
    "dimensions": {"bare_number_range_mm": {"min": 100, "max": 100000}},
    "openings": {"mark_prefixes": {}},
    "schedules": {"column_header_keywords": {}},
    "legends": {"title_keywords": []},
    "overlay": {"colors": {}},
}

_config_cache = None


def load_config() -> dict:
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    try:
        _config_cache = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.exception(f"Could not read {CONFIG_PATH}, running reduced: {e}")
        _config_cache = _MINIMAL_CONFIG
    return _config_cache


def _empty_title_block() -> dict:
    return {name: empty_field() for name in FIELD_NAMES}


def _empty_page(page_number: int, reason: str) -> dict:
    return {
        "page_number": page_number,
        "sheet_id": f"P{page_number:02d}",
        "sheet_id_source": "page_order",
        "page_type": {
            "value": "unknown",
            "confidence": 0.0,
            "confidence_band": "low",
            "technique": None,
            "matched_keyword": None,
            "note": reason,
            "content_agrees_with_title": None,
            "evidence": {},
        },
        "title_block": _empty_title_block(),
        "title_block_region": None,
        "rooms": [],
        "dimensions": [],
        "dimension_chains": [],
        "schedules": [],
        "legends": [],
        "opening_marks": [],
        "scale_calibration": {
            "printed_scale": None,
            "scale_denominator": None,
            "printed_mm_per_point": None,
            "measured_mm_per_point": None,
            "variance_pct": None,
            "tolerance_pct": 0.0,
            "strings_used": 0,
            "usable_for_measurement": False,
            "result": "not_checked",
            "note": reason,
        },
        "walls": [],
        "openings": [],
        "sheet_index": None,
        "unresolved_items": [
            {
                "item_id": f"P{page_number:02d}-ERR01",
                "category": "page_analysis",
                "severity": "P1",
                "reason": reason,
                "text": None,
                "bbox": None,
            }
        ],
        "text_evidence": {},
        "overlay_url": None,
        "error": reason,
    }


def _detect_opening_marks(lines, config: dict, sheet_id: str) -> list:
    """Door and window marks printed on the drawing ('D3', 'W12').

    Day 3 records where they are and what type they are; linking each one to a
    wall is Day 4's job. Capturing them now is what lets the opening schedule
    rows be matched to marks on the plan later.
    """
    prefixes = config.get("openings", {}).get("mark_prefixes", {})
    if not prefixes:
        return []
    import re

    pattern = re.compile(
        r"^(" + "|".join(sorted(prefixes, key=len, reverse=True)) + r")[-\s]?(\d{1,3}[A-Z]?)$",
        re.IGNORECASE,
    )
    marks = []
    for line in lines:
        match = pattern.match(line["text"].strip())
        if not match:
            continue
        prefix = match.group(1).upper()
        marks.append(
            {
                "mark": f"{prefix}{match.group(2).upper()}",
                "element_type": prefixes.get(prefix),
                "bbox": line["bbox"],
                "confidence": round(line["confidence"] * 0.9, 3),
                "extraction_method": line["extraction_method"],
            }
        )
    marks.sort(key=lambda m: (round(m["bbox"][1], 1), round(m["bbox"][0], 1)))
    for position, mark in enumerate(marks, start=1):
        mark["mark_id"] = f"{sheet_id}-MK{position:02d}"
    return marks


def _title_block_band(region, page_width: float, page_height: float):
    """The full band a title block occupies, from the cluster of labels found.

    Widened along the band's own long axis only: a title block that runs across
    the foot of a sheet runs the whole way across, but one printed as a strip
    down the right-hand edge runs the whole way down instead.
    """
    if region is None:
        return None
    x0, y0, x1, y1 = region
    if (x1 - x0) >= (y1 - y0):
        return [0.0, y0, page_width, y1]
    return [x0, 0.0, x1, page_height]


def _region_contains(region, bbox) -> bool:
    if region is None:
        return False
    cx, cy = bbox_center(bbox)
    return region[0] <= cx <= region[2] and region[1] <= cy <= region[3]


def _collect_unresolved(page: dict, text_evidence: dict) -> list:
    """Everything a reviewer must look at, with a stable ID per item."""
    items: list = []
    page_number = page["page_number"]

    def add(category, severity, reason, text=None, bbox=None):
        items.append(
            {
                "item_id": f"P{page_number:02d}-{len(items) + 1:03d}",
                "category": category,
                "severity": severity,
                "reason": reason,
                "text": text,
                "bbox": bbox,
            }
        )

    identifiable = page["title_block"]["sheet_position"]["value"] is not None
    for name, field in page["title_block"].items():
        if field["value"] is None:
            # A project-wide field missing from one sheet is normal; a field
            # that identifies the sheet is not. A missing drawing number is
            # only serious if the sheet cannot be identified at all — where the
            # set prints a page position instead, the sheet is still
            # identifiable and this is a note, not a defect.
            severity = "P1" if name in ("sheet_title", "scale") else "P2"
            reason = f"No {name.replace('_', ' ')} was found on this sheet."
            if name == "sheet_number":
                if identifiable:
                    reason = (
                        "No drawing number is printed on this sheet. It is identified as "
                        f"{page['title_block']['sheet_position']['value']}."
                    )
                else:
                    severity = "P1"
            add(f"title_block.{name}", severity, reason)
        elif field["confidence_band"] == "low":
            add(
                f"title_block.{name}",
                "P2",
                "A value was found, but the reading is uncertain. Check it against the sheet.",
                field["value"],
                field["source_bbox"],
            )
        if field.get("conflicts"):
            add(
                f"title_block.{name}",
                "P1",
                (
                    f"The sheet prints more than one {name.replace('_', ' ')}. "
                    f"{LDQ}{field['value']}{RDQ} is shown; also printed: "
                    + ", ".join(f"{LDQ}{c}{RDQ}" for c in field["conflicts"])
                    + "."
                ),
                field["value"],
                field["source_bbox"],
            )

    if page["page_type"]["value"] == "unknown":
        add(
            "page_type",
            "P2",
            page["page_type"].get("note") or "The type of this sheet could not be determined.",
        )
    elif page["page_type"].get("content_agrees_with_title") is False:
        add("page_type", "P1", page["page_type"]["note"])

    # A sheet printing several scales is ordinary practice and is not raised as
    # something to review. A scale that carries no ratio at all ('NTS') is,
    # because nothing on that sheet can be measured.
    scale_value = page["title_block"]["scale"]["value"]
    if scale_value and not scale_ratios(scale_value):
        add(
            "scale.no_ratio",
            "P2",
            (
                f"This sheet's scale is given as \u201c{scale_value}\u201d, so nothing on it "
                "can be measured from the drawing."
            ),
            scale_value,
            page["title_block"]["scale"]["source_bbox"],
        )

    for chain in page["dimension_chains"]:
        check = chain["check"]
        if check["result"] == "fail":
            add(
                "dimension_chain",
                "P1",
                (
                    f"Dimension string {chain['chain_id']} does not add up. Its "
                    f"{chain['member_count']} figures total "
                    f"{check['sum_of_running_mm']:,.0f} mm, but the drawing prints a total of "
                    f"{check['overall_mm']:,.0f} mm {DASH} a difference of "
                    f"{check['variance_pct']}%, against a {check['tolerance_pct']}% tolerance."
                ),
                None,
                chain["bbox"],
            )
        parallel = chain.get("parallel_check")
        if parallel and parallel["result"] == "fail":
            add(
                "dimension_chain",
                "P1",
                (
                    f"Dimension string {chain['chain_id']} totals "
                    f"{parallel['this_sum_mm']:,.0f} mm, but string "
                    f"{parallel['compared_with']} covers the same distance and totals "
                    f"{parallel['other_sum_mm']:,.0f} mm {DASH} a difference of "
                    f"{parallel['variance_pct']}%. At least one is incomplete or misread."
                ),
                None,
                chain["bbox"],
            )

    calibration = page.get("scale_calibration") or {}
    if calibration.get("result") == "contradicted":
        add("scale.contradicted", "P1", calibration.get("note") or "")
    elif calibration.get("result") == "inconclusive":
        add("scale.inconclusive", "P1", calibration.get("note") or "")
    elif calibration.get("result") == "not_checked" and calibration.get("note"):
        add("scale.not_checked", "P2", calibration["note"])

    for opening in page.get("openings", []):
        if opening.get("found_by") == "gap_in_the_wall":
            add(
                "opening.read_from_the_drawing",
                "P1",
                (
                    f"An opening {opening['width_mm']:.0f} mm wide was measured in "
                    f"{opening['wall_id']}, where the wall stops and starts again. This "
                    "drawing prints no door or window code, so whether it is a door or a "
                    "window, and how tall it is, are not stated and have not been assumed."
                ),
                opening["opening_id"],
                opening["source_bbox"],
            )
        elif not opening["in_schedule"]:
            add(
                "opening.not_in_schedule",
                "P1",
                (
                    f"{opening['mark']} is marked on this sheet but does not appear in any "
                    "schedule, so its size is unknown."
                ),
                opening["mark"],
                opening["source_bbox"],
            )
        elif not opening["wall_id"]:
            add(
                "opening.not_placed_on_a_wall",
                "P2",
                f"{opening['mark']}: {opening['wall_note']}",
                opening["mark"],
                opening["source_bbox"],
            )

    for wall in page.get("walls", []):
        if not wall["matches_nominal_thickness"]:
            add(
                "wall.unusual_thickness",
                "P2",
                (
                    f"Candidate wall {wall['wall_id']} measures "
                    f"{wall['thickness_mm']:.0f} mm thick, which is not one of the "
                    "thicknesses this office normally builds."
                ),
                None,
                wall["bbox"],
            )
        if wall.get("longer_than_sheet_measures"):
            add(
                "wall.longer_than_the_sheet_measures",
                "P1",
                (
                    f"Candidate wall {wall['wall_id']} is "
                    f"{wall['length_mm'] / 1000:.1f} m long, which is longer than any "
                    "distance this sheet dimensions. It is more likely a boundary or "
                    "an eave line than a wall, and needs a look."
                ),
                None,
                wall["bbox"],
            )

    for room in page["rooms"]:
        if room["confidence_band"] == "low":
            add(
                "room_label",
                "P2",
                "This room label was read with low certainty.",
                room["name"],
                room["bbox"],
            )

    for dimension in page["dimensions"]:
        if dimension["confidence_band"] == "low":
            add(
                "dimension",
                "P2",
                "This dimension was read with low certainty.",
                dimension["text"],
                dimension["bbox"],
            )

    for table in page["schedules"]:
        for row in table["rows"]:
            for flag in row["flags"]:
                add("schedule_row", "P1", flag, row.get("mark"), row.get("bbox"))
        for cell in table["unassigned_cells"]:
            add(
                "schedule_cell",
                "P2",
                (
                    f"A value in the {LDQ}{cell['attribute'].replace('_', ' ')}{RDQ} row of "
                    f"{table['caption']} could not be matched to an item, so it was not used."
                ),
                cell["text"],
                cell["bbox"],
            )

    for conflict in text_evidence.get("overprint_conflicts", []):
        add(
            "overprinted_text",
            "P2",
            (
                "Two different values are printed in the same place on this sheet: "
                + ", ".join(f"{LDQ}{v}{RDQ}" for v in conflict["values"])
                + ". Both were kept."
            ),
            None,
            conflict["bbox"],
        )

    return items


def analyze_page(
    page_number: int,
    page_count: int,
    page_width: float,
    page_height: float,
    lines: list,
    text_evidence: dict,
    rulings: dict,
    page=None,
) -> dict:
    """Full plan reading for one page. Never raises."""
    try:
        config = load_config()

        title = detect_title_block(
            lines, rulings, page_width, page_height, config, page_number, page_count
        )
        fields = title["fields"]
        region = title["region"]
        sheet_id, sheet_id_source = sheet_id_for(fields, page_number)

        # Everything the title block already accounted for, plus everything
        # inside the title-block region, is excluded from the content
        # detectors. The region matters as much as the individual values: a
        # sheet's footer, its issue date and its drawing-number cell all sit
        # there, and each was previously being re-read as a dimension, a room
        # or a schedule row.
        content_lines = [
            line
            for line in lines
            if tuple(line["bbox"]) not in title["consumed_bboxes"]
            and not _region_contains(region, line["bbox"])
        ]

        sheet_title_value = fields["sheet_title"]["value"]

        # Tables are read before the loose content on the page, and the areas
        # they occupy are then excluded from room and dimension detection.
        # Without that, the same fact is extracted twice under two different
        # meanings: a door schedule's 'Location' column reads as a page full of
        # room labels, and a drawing index's titles read as rooms on the cover
        # sheet. One printed value should produce one record.
        detected_schedules = schedules_module.detect_schedules(
            content_lines, config, sheet_id, sheet_title_value
        )
        detected_legends = schedules_module.detect_legends(content_lines, config, sheet_id)
        page_sheet_index = parse_sheet_index(content_lines, page_number, page_width, config)

        table_regions = [t["bbox"] for t in detected_schedules if t.get("bbox")]
        for legend in detected_legends:
            entries = [e["bbox"] for e in legend["entries"]]
            if entries:
                table_regions.append(
                    [
                        min(b[0] for b in entries),
                        min(b[1] for b in entries),
                        max(b[2] for b in entries),
                        max(b[3] for b in entries),
                    ]
                )
        if page_sheet_index:
            index_boxes = [e["source_bbox"] for e in page_sheet_index["entries"]]
            table_regions.append(
                [
                    min(b[0] for b in index_boxes),
                    min(b[1] for b in index_boxes),
                    max(b[2] for b in index_boxes),
                    max(b[3] for b in index_boxes),
                ]
            )

        drawing_lines = [
            line
            for line in content_lines
            if not any(_region_contains(box, line["bbox"]) for box in table_regions)
        ]

        # Rooms are read from every sheet first, because the page type is
        # decided partly by how many there are. They are only *kept* on a sheet
        # that draws a plan: a section prints the names of the spaces it cuts
        # through, an interior elevation prints the room it belongs to, and a
        # notes sheet mentions them in sentences. Those are references to a
        # room, not the room — keeping them listed the same kitchen five times
        # and turned drawing captions into areas of the building.
        detected_rooms = rooms_module.detect_rooms(drawing_lines, config, sheet_id)
        detected_dimensions = dimensions_module.detect_dimensions(
            drawing_lines, config, sheet_id
        )
        chains = dimensions_module.build_chains(detected_dimensions, config, sheet_id)
        dimensions_module.link_dimensions_to_rooms(detected_dimensions, detected_rooms, config)
        opening_marks = _detect_opening_marks(drawing_lines, config, sheet_id)

        page_type = detect_page_type(
            sheet_title_value,
            drawing_lines,
            len(detected_rooms),
            len(detected_dimensions),
            len(detected_schedules),
            config,
        )

        # Day 4. Scale is calibrated before anything is measured with it, and
        # walls are only produced when it holds up — a length taken from an
        # unconfirmed scale is worse than no length at all.
        calibration = calibrate_page(
            fields["scale"]["value"], detected_dimensions, chains, config
        )
        # Walls are looked for only on sheets that draw the building in plan.
        # Two parallel lines a wall thickness apart mean something on a floor
        # plan; on an elevation or a section they are a window frame or a
        # hatch, and reporting those as walls would bury the real ones.
        wall_settings = config.get("walls", {})
        wall_page_types = wall_settings.get("detect_on_page_types", ["floor_plan"])
        draws_rooms = len(detected_rooms) >= int(wall_settings.get("min_rooms_on_sheet", 4))
        # A title block is a band across one edge of the sheet, and it is ruled
        # into cells whose rules are parallel lines a few millimetres apart at
        # drawing scale. Only the labels inside it were located, so the band is
        # extended along its own long axis to the sheet edges before walls are
        # looked for — otherwise the revisions table is reported as a wall.
        # The longest distance this sheet actually measures, from its own
        # printed figures — the only honest upper bound on a wall's length.
        measured_spans = [
            dimension["value_mm"]
            for dimension in detected_dimensions
            if dimension["kind"] == "linear" and dimension["value_mm"]
        ] + [chain["sum_mm"] for chain in chains if chain.get("sum_mm")]
        sheet_span_mm = max(measured_spans) if measured_spans else None

        detected_walls = (
            detect_walls(
                rulings,
                calibration,
                config,
                sheet_id,
                exclude_region=_title_block_band(region, page_width, page_height),
                page=page,
                sheet_span_mm=sheet_span_mm,
            )
            if (page_type["value"] in wall_page_types or page_type.get("draws_a_plan"))
            and draws_rooms
            else []
        )
        if not page_type.get("draws_a_plan"):
            detected_rooms = []

        detected_openings = place_openings_on_walls(
            opening_marks, detected_walls, calibration, config, sheet_id
        )
        # A plan that prints no marks still draws its doors and windows. Where
        # no mark was found on this sheet, the openings are read from the
        # breaks in the walls themselves. Marks are always preferred when they
        # exist, because a mark carries a schedule row and therefore a real
        # size and type; a break carries only what can be measured.
        if not detected_openings:
            detected_openings = openings_from_wall_gaps(
                detected_walls, calibration, config, sheet_id
            )
        fields["discipline"] = resolve_discipline(
            fields, page_type["value"], lines, region, config
        )

        page = {
            "page_number": page_number,
            "sheet_id": sheet_id,
            "sheet_id_source": sheet_id_source,
            "page_type": page_type,
            "title_block": fields,
            "title_block_region": [round(v, 2) for v in region] if region else None,
            "rooms": detected_rooms,
            "dimensions": detected_dimensions,
            "dimension_chains": chains,
            "schedules": detected_schedules,
            "legends": detected_legends,
            "opening_marks": opening_marks,
            "scale_calibration": calibration,
            "walls": detected_walls,
            "openings": detected_openings,
            "sheet_index": page_sheet_index,
            "unresolved_items": [],
            "text_evidence": text_evidence,
            "overlay_url": None,
            "error": None,
        }
        page["unresolved_items"] = _collect_unresolved(page, text_evidence)
        return page
    except Exception as e:
        logger.exception(f"analyze_page failed for page={page_number}: {e}")
        return _empty_page(page_number, f"Plan-reading analysis failed for this page: {e}")


def page_lines_and_rulings(page, ocr_blocks: list, dpi: int):
    """Convenience wrapper so intake has one call for the page's text model."""
    from pipeline.plan.textmodel import build_page_lines

    lines, evidence = build_page_lines(page, ocr_blocks, dpi)
    return lines, evidence, extract_rulings(page)


# --- Run-level metrics ----------------------------------------------------


def compute_metrics(pages: list, cross_check: dict, opening_reconciliation=None) -> dict:
    """The numbers shown in the interface, each computed from the records.

    Traceability is Week 1 Gate 5 and is measured, not asserted: it is the
    share of extracted records that carry a source bounding box. A derived
    value (the sheet position taken from page order) has no box on the sheet
    and is counted separately rather than being quietly included.
    """
    field_total = field_found = field_high = field_traced = 0
    traceable_fields = derived_fields = 0
    per_field: dict = {}
    record_total = record_traced = 0
    room_total = dimension_total = schedule_row_total = legend_entry_total = 0
    wall_total = wall_nominal = 0
    opening_total = opening_in_schedule = opening_on_wall = 0
    distinct_openings: set = set()
    scale_confirmed = scale_contradicted = scale_unchecked = 0
    chain_checked = chain_passed = 0
    severity_counts = {"P0": 0, "P1": 0, "P2": 0}
    page_type_counts: dict = {}

    for page in pages:
        for name, field in page["title_block"].items():
            field_total += 1
            stats = per_field.setdefault(
                name, {"pages": 0, "found": 0, "verified": 0, "disagreed": 0}
            )
            stats["pages"] += 1
            if field.get("verified_against_index") is True:
                stats["verified"] += 1
            elif field.get("verified_against_index") is False:
                stats["disagreed"] += 1
            if field["value"] is None:
                continue
            stats["found"] += 1
            field_found += 1
            if field["confidence_band"] == "high":
                field_high += 1
            if field.get("technique") == "derived_from_page_order":
                # Derived from the PDF's page order, not read off the drawing:
                # it has no location on the sheet, so counting it as untraced
                # would understate traceability and counting it as traced would
                # overstate it. It is excluded from the ratio entirely.
                derived_fields += 1
                continue
            traceable_fields += 1
            if field["source_bbox"]:
                field_traced += 1

        for record in page["rooms"]:
            room_total += 1
            record_total += 1
            record_traced += 1 if record.get("bbox") else 0
        for record in page["dimensions"]:
            dimension_total += 1
            record_total += 1
            record_traced += 1 if record.get("bbox") else 0
        for table in page["schedules"]:
            for row in table["rows"]:
                schedule_row_total += 1
                record_total += 1
                record_traced += 1 if row.get("bbox") else 0
        for legend in page["legends"]:
            for entry in legend["entries"]:
                legend_entry_total += 1
                record_total += 1
                record_traced += 1 if entry.get("bbox") else 0

        for wall in page.get("walls", []):
            wall_total += 1
            record_total += 1
            record_traced += 1 if wall.get("bbox") else 0
            if wall["matches_nominal_thickness"]:
                wall_nominal += 1
        for opening in page.get("openings", []):
            opening_total += 1
            record_total += 1
            record_traced += 1 if opening.get("source_bbox") else 0
            # One opening is marked on several sheets — on the plan, again on
            # an elevation, again on a section. Those are the same door. The
            # headline figure counts the doors and windows; the number of
            # marks is reported beside it as the evidence behind them.
            if opening["mark"]:
                distinct_openings.add(opening["mark"].upper())
            if opening["in_schedule"]:
                opening_in_schedule += 1
            if opening["wall_id"]:
                opening_on_wall += 1

        outcome_scale = (page.get("scale_calibration") or {}).get("result")
        if outcome_scale == "confirmed":
            scale_confirmed += 1
        elif outcome_scale in ("contradicted", "inconclusive"):
            scale_contradicted += 1
        elif page["title_block"]["scale"]["value"]:
            scale_unchecked += 1

        for chain in page["dimension_chains"]:
            outcome = chain["check"]["result"]
            if outcome not in ("pass", "fail") and chain.get("parallel_check"):
                outcome = chain["parallel_check"]["result"]
            if outcome in ("pass", "fail"):
                chain_checked += 1
                if outcome == "pass":
                    chain_passed += 1

        for item in page["unresolved_items"]:
            severity_counts[item["severity"]] = severity_counts.get(item["severity"], 0) + 1

        page_type = page["page_type"]["value"]
        page_type_counts[page_type] = page_type_counts.get(page_type, 0) + 1

    def percentage(part, whole):
        return round(part / whole * 100.0, 1) if whole else None

    traceable_total = record_total + traceable_fields
    traceable_traced = record_traced + field_traced

    return {
        "page_count": len(pages),
        "pages_with_errors": sum(1 for p in pages if p.get("error")),
        "sheet_coverage_pct": 100.0 if pages else None,
        "title_block": {
            "fields_expected": field_total,
            "fields_found": field_found,
            "fields_found_pct": percentage(field_found, field_total),
            "fields_high_confidence_pct": percentage(field_high, field_found),
            "per_field": {
                name: {**stats, "found_pct": percentage(stats["found"], stats["pages"])}
                for name, stats in per_field.items()
            },
        },
        "cross_check": {
            "index_source_page": cross_check.get("index_source_page"),
            "compared_pages": cross_check.get("compared_pages", 0),
            "agreements": cross_check.get("agreements", 0),
            "disagreements": cross_check.get("disagreements", 0),
            "filled_from_index": cross_check.get("filled_from_index", 0),
            "agreement_pct": percentage(
                cross_check.get("agreements", 0),
                cross_check.get("agreements", 0) + cross_check.get("disagreements", 0),
            ),
        },
        "records": {
            "rooms": room_total,
            "dimensions": dimension_total,
            "schedule_rows": schedule_row_total,
            "legend_entries": legend_entry_total,
            "candidate_walls": wall_total,
            "openings": opening_total,
        },
        "scale_calibration": {
            "sheets_confirmed": scale_confirmed,
            "sheets_contradicted": scale_contradicted,
            "sheets_not_checked": scale_unchecked,
            "confirmed_pct": percentage(
                scale_confirmed, scale_confirmed + scale_contradicted + scale_unchecked
            ),
        },
        "walls": {
            "candidates": wall_total,
            "at_nominal_thickness": wall_nominal,
            "at_nominal_thickness_pct": percentage(wall_nominal, wall_total),
        },
        "openings": {
            "distinct_openings": len(distinct_openings),
            "marks_on_drawings": opening_total,
            "matched_to_a_schedule": opening_in_schedule,
            "matched_to_a_schedule_pct": percentage(opening_in_schedule, opening_total),
            "placed_on_a_wall": opening_on_wall,
            "placed_on_a_wall_pct": percentage(opening_on_wall, opening_total),
            "scheduled_marks_not_drawn": len(
                (opening_reconciliation or {}).get("scheduled_marks_not_drawn", [])
            ),
        },
        "dimension_chain_check": {
            "chains_checked": chain_checked,
            "chains_passed": chain_passed,
            "pass_pct": percentage(chain_passed, chain_checked),
        },
        "traceability": {
            "records_with_source": traceable_traced,
            "records_total": traceable_total,
            "traceability_pct": percentage(traceable_traced, traceable_total),
            "values_derived_not_read": derived_fields,
            "note": (
                "The share of extracted values that can be pointed back to a place on the "
                f"sheet. {derived_fields} value(s) taken from the document's page order "
                "rather than read from a drawing are not counted, because they do not "
                "appear on the sheet."
            ),
        },
        "unresolved_items": severity_counts,
        "page_types": page_type_counts,
    }


# --- CSV outputs ----------------------------------------------------------


# What each internal severity means, spelled out in the downloadable log so
# the file makes sense to someone who has never seen the code. The severities
# themselves are a tracing tool and are deliberately never shown on screen.
_SEVERITY_MEANING = {
    "P0": "Blocks the run",
    "P1": "Needs attention",
    "P2": "For information",
}


def write_unresolved_items_csv(csv_path: Path, run_id: str, pages: list) -> None:
    """The full issues log, written for every run and offered as a download.

    It carries more than the interface shows: the sheet it belongs to, the
    internal category and severity, the exact wording, and the position on the
    sheet — everything needed to trace an item back to the drawing.
    """
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "run_id",
                "page_number",
                "sheet_number",
                "sheet_title",
                "item_id",
                "area",
                "category",
                "severity",
                "severity_meaning",
                "description",
                "value_on_sheet",
                "position_on_sheet",
            ]
        )
        for page in pages:
            fields = page.get("title_block", {})
            sheet_number = (fields.get("sheet_number") or {}).get("value") or ""
            sheet_title = (fields.get("sheet_title") or {}).get("value") or ""
            for item in page["unresolved_items"]:
                writer.writerow(
                    [
                        run_id,
                        page["page_number"],
                        sheet_number,
                        sheet_title,
                        item.get("item_id", ""),
                        _issue_area(item["category"]),
                        item["category"],
                        item["severity"],
                        _SEVERITY_MEANING.get(item["severity"], ""),
                        item["reason"],
                        item.get("text") or "",
                        json.dumps(item["bbox"]) if item.get("bbox") else "",
                    ]
                )


# Groups an item by the part of the sheet it concerns, matching the grouping
# the interface used to show.
_ISSUE_AREAS = (
    ("cross_check.", "Drawing index"),
    ("title_block.", "Sheet details"),
    ("scale.", "Scale"),
    ("page_type", "Sheet type"),
    ("page_analysis", "Sheet"),
    ("dimension_chain", "Dimensions"),
    ("dimension", "Dimensions"),
    ("room_label", "Rooms"),
    ("schedule_row", "Schedules"),
    ("schedule_cell", "Schedules"),
    ("overprinted_text", "Overlapping text"),
)


def _issue_area(category: str) -> str:
    for prefix, label in _ISSUE_AREAS:
        if category.startswith(prefix):
            return label
    return "Sheet"


def write_plan_reading_csvs(run_dir: Path, run_id: str, pages: list) -> None:
    """Flat CSVs alongside the JSON, so every table shown in the interface can
    also be opened in a spreadsheet without writing any code."""
    rooms_path = run_dir / "rooms.csv"
    with rooms_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "run_id", "page_number", "sheet_id", "room_id", "name", "normalized_name",
                "instance", "detection_method", "width_mm", "height_mm", "floor_area_m2",
                "confidence", "confidence_band", "review_status", "bbox",
            ]
        )
        for page in pages:
            for room in page["rooms"]:
                writer.writerow(
                    [
                        run_id, page["page_number"], page["sheet_id"], room["room_id"],
                        room["name"], room["normalized_name"] or "", room["instance"] or "",
                        room["detection_method"], room["width_mm"] or "", room["height_mm"] or "",
                        room["floor_area_m2"] or "", room["confidence"], room["confidence_band"],
                        room["review_status"], json.dumps(room["bbox"]),
                    ]
                )

    dimensions_path = run_dir / "dimensions.csv"
    with dimensions_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "run_id", "page_number", "sheet_id", "dimension_id", "text", "kind",
                "measures_axis", "value_mm", "width_mm", "height_mm", "unit_source",
                "is_overall", "measured_to", "chain_id", "chain_role", "linked_room_id",
                "link_method",
                "confidence", "confidence_band", "bbox",
            ]
        )
        for page in pages:
            for dimension in page["dimensions"]:
                writer.writerow(
                    [
                        run_id, page["page_number"], page["sheet_id"], dimension["dimension_id"],
                        dimension["text"], dimension["kind"], dimension["measures_axis"],
                        dimension["value_mm"] if dimension["value_mm"] is not None else "",
                        dimension["width_mm"] if dimension["width_mm"] is not None else "",
                        dimension["height_mm"] if dimension["height_mm"] is not None else "",
                        dimension["unit_source"], dimension["is_overall"],
                        dimension.get("measured_to") or "",
                        dimension["chain_id"] or "", dimension["chain_role"] or "",
                        dimension["linked_room_id"] or "", dimension["link_method"] or "",
                        dimension["confidence"], dimension["confidence_band"],
                        json.dumps(dimension["bbox"]),
                    ]
                )

    walls_path = run_dir / "walls.csv"
    with walls_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "run_id", "page_number", "sheet_id", "wall_id", "runs_along", "length_mm",
                "thickness_mm", "nominal_thickness_mm", "matches_nominal_thickness",
                "openings_on_this_wall", "measured_from", "longer_than_sheet_measures",
                "confidence", "confidence_band", "bbox",
            ]
        )
        for page in pages:
            for wall in page.get("walls", []):
                writer.writerow(
                    [
                        run_id, page["page_number"], page["sheet_id"], wall["wall_id"],
                        wall["runs_along"], wall["length_mm"], wall["thickness_mm"],
                        wall["nominal_thickness_mm"] or "", wall["matches_nominal_thickness"],
                        "; ".join(wall["linked_opening_marks"]), wall["line_source"],
                        wall.get("longer_than_sheet_measures", False),
                        wall["confidence"], wall["confidence_band"], json.dumps(wall["bbox"]),
                    ]
                )

    openings_path = run_dir / "openings.csv"
    with openings_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "run_id", "page_number", "sheet_id", "opening_id", "mark", "element_type",
                "wall_id", "width_mm", "height_mm", "sill_height_mm", "head_height_mm",
                "location_on_plan", "schedule_sheet", "schedule_row_id", "in_schedule",
                "found_by", "confidence", "confidence_band", "source_bbox",
            ]
        )
        for page in pages:
            for opening in page.get("openings", []):
                writer.writerow(
                    [
                        run_id, page["page_number"], page["sheet_id"], opening["opening_id"],
                        opening["mark"], opening["element_type"] or "", opening["wall_id"] or "",
                        opening["width_mm"] if opening["width_mm"] is not None else "",
                        opening["height_mm"] if opening["height_mm"] is not None else "",
                        opening["sill_height_mm"] if opening["sill_height_mm"] is not None else "",
                        opening["head_height_mm"] if opening["head_height_mm"] is not None else "",
                        opening["location_on_plan"] or "", opening["schedule_sheet"] or "",
                        opening["schedule_row_id"] or "", opening["in_schedule"],
                        opening.get("found_by", "mark_on_the_drawing"),
                        opening["confidence"], opening["confidence_band"],
                        json.dumps(opening["source_bbox"]),
                    ]
                )

    schedule_path = run_dir / "schedule_rows.csv"
    columns: list = []
    for page in pages:
        for table in page["schedules"]:
            for row in table["rows"]:
                for key in row["values"]:
                    if key not in columns:
                        columns.append(key)
    with schedule_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["run_id", "page_number", "sheet_id", "table_id", "caption", "row_id", "mark",
             "element_type", "width_mm", "height_mm", "confidence_band", "flags"] + columns
        )
        for page in pages:
            for table in page["schedules"]:
                for row in table["rows"]:
                    writer.writerow(
                        [
                            run_id, page["page_number"], page["sheet_id"], table["table_id"],
                            table["caption"], row["row_id"], row["mark"] or "",
                            row["element_type"] or "",
                            row["width_mm"] if row["width_mm"] is not None else "",
                            row["height_mm"] if row["height_mm"] is not None else "",
                            row["confidence_band"], "; ".join(row["flags"]),
                        ]
                        + [row["values"].get(column, "") for column in columns]
                    )
