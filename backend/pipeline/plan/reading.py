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
from pipeline.plan import cvwalls
from pipeline.plan.layout import extract_rulings
from pipeline.plan.openingevidence import read_openings_from_the_drawing
from pipeline.plan.openings import (
    openings_from_symbols,
    place_openings_on_walls,
)
from pipeline.plan.pagetype import detect_page_type
from pipeline.plan.scale import calibrate_page
from pipeline.plan.walls import (
    building_outline,
    detect_walls,
    drawing_region,
    arrow_heads,
    mark_walls_in_dead_ground,
    mark_walls_with_an_arrow_at_the_end,
    printed_panels,
    trim_walls_to_the_drawing,
    wall_graph_for,
    walls_as_records,
)
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

# The wall reader's own settings live in their own file, because they are the
# ones an office is most likely to change: the thicknesses it builds, how short
# a stretch is still a wall, how far apart two lines may be and still be one
# face. Merged over the "walls" section of the file above, so a setting given
# there wins and anything left out keeps the value it already had. A missing
# file is not an error - it means the office has not overridden anything.
WALL_CONFIG_PATH = CONFIG_DIR / "wall_config.json"

# The opening reader's own settings, for the same reason and merged the same
# way over the "openings" section: what makes a break in a wall a door or a
# window is the judgement an office is most likely to want to retune, and it is
# now made from four separate readings of the drawing rather than from the
# break alone.
OPENING_CONFIG_PATH = CONFIG_DIR / "opening_config.json"

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

# The three files the plan config is built from, and what they looked like when
# it was last built. See ``load_config``.
_CONFIG_FILES = (CONFIG_PATH, WALL_CONFIG_PATH, OPENING_CONFIG_PATH)
_config_cache = None
_config_stamp = None


def _config_files_stamp() -> tuple:
    """What the config files look like right now: name, modified time, size.

    Size as well as modified time, because two edits within the same clock tick
    are not impossible and a settings file is exactly the sort of thing that
    gets edited twice in a second.
    """
    stamp = []
    for path in _CONFIG_FILES:
        try:
            state = path.stat()
            stamp.append((path.name, state.st_mtime_ns, state.st_size))
        except OSError:
            stamp.append((path.name, None, None))
    return tuple(stamp)


def load_config() -> dict:
    """The plan-reading config, rebuilt whenever one of its files changes.

    **A setting has to be changeable on a server that is already running.** The
    config was read once and cached for the life of the process, so changing
    which wall reader runs, or a tolerance, or a room name, meant a restart -
    and on a hosted Space a restart wipes the disk and every plan being read.
    That is the same reason ``OCR_ENABLED`` is read from the environment rather
    than baked into the image (CLAUDE.md 4AJ): the thing you want to change is
    usually discovered *after* deploying.

    The cache is kept - this is called once per sheet and a few times per run,
    and re-reading three JSON files each time would be real work - but it is
    dropped as soon as any of the files is touched. Checking costs three
    ``stat`` calls, which is nothing beside reading a page.

    The same dict is returned while the files are untouched, so anything
    holding a reference to it keeps working.
    """
    global _config_cache, _config_stamp
    stamp = _config_files_stamp()
    if _config_cache is not None and stamp == _config_stamp:
        return _config_cache

    if _config_cache is not None:
        logger.info("a config file changed on disk; the settings are being re-read")
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.exception(f"Could not read {CONFIG_PATH}, running reduced: {e}")
        config = dict(_MINIMAL_CONFIG)
    config["walls"] = _merged_wall_settings(config.get("walls", {}))
    config["openings"] = _merged_from(
        config.get("openings", {}), OPENING_CONFIG_PATH, "opening"
    )
    # Only published once it is complete, so a reader that arrives mid-rebuild
    # never sees a half-merged config.
    _config_cache, _config_stamp = config, stamp
    return _config_cache


def _merged_from(existing: dict, path, what: str) -> dict:
    """One settings section with its own file laid over it.

    Kept separate from the file itself so that an unreadable override degrades
    to the settings already in hand rather than taking the run down
    (Critical Rule 6), and so a reader can see in the log which file a setting
    came from.
    """
    merged = dict(existing)
    try:
        override = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return merged
    except Exception as e:
        logger.exception(f"Could not read {path}, using the settings already loaded: {e}")
        return merged
    merged.update(override)
    logger.info(f"{what} settings: {len(override)} entries read from {path.name}")
    return merged


def _merged_wall_settings(existing: dict) -> dict:
    """The wall settings, with ``config/wall_config.json`` laid over them.

    Kept separate from the file itself so that an unreadable override degrades
    to the settings already in hand rather than taking the run down
    (Critical Rule 6), and so a reader can see in the log which file a wall
    setting came from.
    """
    merged = dict(existing)
    try:
        override = json.loads(WALL_CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return merged
    except Exception as e:
        logger.exception(f"Could not read {WALL_CONFIG_PATH}, using the settings already loaded: {e}")
        return merged
    merged.update(override)
    logger.info(f"wall settings: {len(override)} entries read from {WALL_CONFIG_PATH.name}")
    return merged


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
        "title_block_found": False,
        "title_block_note": None,
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
        "walls_note": None,
        "openings": [],
        "unresolved_gaps": [],
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
        "detection_overlay_url": None,
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

    # **A break in a wall that nothing confirmed is not an opening**, and it is
    # not thrown away either. It is a real break in a real wall, so it goes to
    # the issues log with its width and where on the sheet to find it — the
    # screen shows what was read, and the log says what to check.
    for gap in page.get("unresolved_gaps", []):
        add(
            "opening.gap_with_nothing_to_confirm_it",
            "P2",
            gap["reason"],
            gap["gap_id"],
            gap["source_bbox"],
        )

    for opening in page.get("openings", []):
        if opening.get("review_needed"):
            add(
                "opening.one_reading_only",
                "P1",
                (
                    f"{opening.get('display_mark') or opening['opening_id']}: "
                    f"{opening.get('how_it_was_decided') or opening.get('wall_note') or ''}"
                ),
                opening["opening_id"],
                opening["source_bbox"],
            )
        if opening.get("schedule_width_agrees") is False or (
            opening.get("measured_width_mm")
            and opening.get("schedule_width_mm")
            and opening["measured_width_mm"] != opening["schedule_width_mm"]
        ):
            # Both values are preserved and neither is assumed correct.
            add(
                "opening.width_disagrees_with_the_schedule",
                "P1",
                (
                    f"{opening.get('mark') or opening['opening_id']} measures "
                    f"{float(opening['measured_width_mm']):.0f} mm across the break in "
                    f"the wall, and the schedule states "
                    f"{float(opening['schedule_width_mm']):.0f} mm. Both are kept and "
                    "neither has been assumed correct."
                ),
                opening.get("mark") or opening["opening_id"],
                opening["source_bbox"],
            )
        if not opening.get("mark"):
            continue
        if not opening["in_schedule"]:
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

        # **A sheet with no title block still has to be usable.** Some sheets
        # simply do not carry one - a continuation sheet, a sketch, a page from
        # a report bound into the set - and some carry one this reader could not
        # find. Either way the sheet is still read in full: its rooms, its
        # dimensions, its walls and its openings are all found from the drawing
        # itself, and it is identified by its position in the document. Saying
        # which of those happened is the difference between a reader trusting
        # the reading and doubting all of it.
        title_block_found = region is not None
        title_block_note = None
        if not title_block_found:
            printed = [
                name
                for name in fields
                if fields[name]["value"]
                and fields[name]["technique"] != "derived_from_page_order"
            ]
            if printed:
                title_block_note = (
                    "No title block could be located on this sheet, so its details were "
                    "read from wherever they are printed on it. Everything else on the "
                    "sheet was read as usual."
                )
            else:
                title_block_note = (
                    "This sheet carries no title block that could be found, so it has no "
                    "printed drawing number, title or scale to report. It is identified "
                    "by its position in the document. Everything drawn on it was still "
                    "read, and without a scale no lengths are taken from it."
                )

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

        # **A table is a panel on a sheet; the drawing occupies the rest of it.**
        # The area a table covers is excluded from room and dimension
        # detection, so a table read wrongly does not merely add a wrong row -
        # it hides the drawing behind it. On one sheet a mis-read table claimed
        # most of the page and every room on that plan disappeared. A "table"
        # covering more of the sheet than a table ever does is therefore not
        # believed at all.
        max_table_share = float(
            config.get("tables", {}).get("max_sheet_share", 0.6)
        )
        sheet_area = max(page_width * page_height, 1.0)

        def _covers_the_sheet(box) -> bool:
            return ((box[2] - box[0]) * (box[3] - box[1])) / sheet_area > max_table_share

        def _bounds(boxes):
            return [
                min(b[0] for b in boxes),
                min(b[1] for b in boxes),
                max(b[2] for b in boxes),
                max(b[3] for b in boxes),
            ]

        kept_schedules = []
        table_regions = []
        for table in detected_schedules:
            box = table.get("bbox")
            if box and _covers_the_sheet(box):
                logger.info(
                    f"page {page_number}: a schedule table was read across most of the "
                    "sheet, which a schedule never is, so it was not used"
                )
                continue
            kept_schedules.append(table)
            if box:
                table_regions.append(box)
        detected_schedules = kept_schedules

        kept_legends = []
        for legend in detected_legends:
            entries = [e["bbox"] for e in legend["entries"]]
            box = _bounds(entries) if entries else None
            if box and _covers_the_sheet(box):
                logger.info(
                    f"page {page_number}: a legend was read across most of the sheet, "
                    "which a legend never is, so it was not used"
                )
                continue
            kept_legends.append(legend)
            if box:
                table_regions.append(box)
        detected_legends = kept_legends

        if page_sheet_index:
            index_box = _bounds([e["source_bbox"] for e in page_sheet_index["entries"]])
            if _covers_the_sheet(index_box):
                logger.info(
                    f"page {page_number}: a drawing index was read across most of the "
                    "sheet, which an index never is, so it was not used"
                )
                page_sheet_index = None
            else:
                table_regions.append(index_box)

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
            legend_count=len(detected_legends),
            has_drawing_index=bool(page_sheet_index),
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
        # A title block is a band across one edge of the sheet, and it is ruled
        # into cells whose rules are parallel lines a few millimetres apart at
        # drawing scale. Only the labels inside it were located, so the band is
        # extended along its own long axis to the sheet edges before walls are
        # looked for — otherwise the revisions table is reported as a wall.
        # The longest distance this sheet actually measures, from its own
        # printed figures — the only honest upper bound on a wall's length.
        # Measured separately for each direction. A house is commonly twice as
        # wide as it is deep, and one limit taken from the wider direction lets
        # a "wall" through in the other that is half as long again as the
        # building — which is how a dimension string's witness lines came to be
        # reported as walls running down the whole sheet.
        sheet_span_mm = {}
        for axis in ("x", "y"):
            spans = [
                dimension["value_mm"]
                for dimension in detected_dimensions
                if dimension["kind"] == "linear"
                and dimension["value_mm"]
                and dimension["measures_axis"] == axis
            ] + [
                chain["sum_mm"]
                for chain in chains
                if chain.get("sum_mm") and chain.get("axis") == axis
            ]
            sheet_span_mm[axis] = max(spans) if spans else None
        # A sheet that dimensions only one direction still bounds the other:
        # nothing on it is longer than the longest thing it measures.
        widest = max((v for v in sheet_span_mm.values() if v), default=None)
        for axis in ("x", "y"):
            if not sheet_span_mm[axis]:
                sheet_span_mm[axis] = widest

        # A site plan draws the block, not the building's walls. Its parallel
        # lines are boundaries, setbacks, easements, fences and driveways, and
        # at site-plan scale a wall is about one point thick, so nothing
        # measured from it would be a wall thickness anyway. Reporting those as
        # walls would bury the real ones from the floor plan.
        never_trace_on = set(wall_settings.get("never_trace_walls_on", ["site_plan"]))
        trace_walls_here = (
            page_type["value"] in wall_page_types or page_type.get("draws_a_plan")
        ) and page_type["value"] not in never_trace_on

        # **How many rooms a sheet names is not what makes it a plan.** Walls
        # used to be traced only on a sheet naming at least four rooms, which
        # is a guess about the size of the building rather than a fact about
        # the drawing: an extension, a granny flat, a studio or a shed names
        # two or three, and a plan drawn without room names at all names none.
        # Those sheets produced no walls, and so no model and no quantities.
        #
        # So the walls are looked for first and the result is judged, which is
        # evidence rather than a guess. Where the sheet says it is a plan, the
        # walls found are the walls. Where the kind was only inferred from what
        # is printed, the drawing has to look like a building before they are
        # reported: a building is a closed shape, so it takes at least four
        # walls, and enough of them at a thickness the office actually builds.
        # **Which reader measures the walls is a setting, not a rebuild.** The
        # computer-vision reader (``cvwalls`` over ``cvdetect``) closes the
        # drawing into solid bands and skeletonises them, so a wall is reported
        # once rather than once per pair of drawn faces; the face-pairing
        # reader (``walls``) is kept and can be switched back to on a deployed
        # server. Both hand back the same canonical record and both go through
        # the same junction, outer/inner and description pass, so everything
        # downstream - the overlay, the model, the CSVs - is unaffected by the
        # choice.
        read_walls = (
            cvwalls.detect_walls
            if cvwalls.reader_name(config) == "cvdetect"
            else detect_walls
        )
        detected_walls = (
            read_walls(
                rulings,
                calibration,
                config,
                sheet_id,
                exclude_region=_title_block_band(region, page_width, page_height),
                page=page,
                sheet_span_mm=sheet_span_mm,
                page_number=page_number,
                # Every word printed on the sheet. A word set in capitals is a
                # continuous run of dark pixels, which is exactly what a wall
                # face looks like to a page read as a picture.
                text_boxes=[line["bbox"] for line in lines],
                # Where the room names are printed is the inside of the
                # building, which is what tells a wall stopping free at a
                # doorway from one running out into the margin.
                rooms=detected_rooms,
            )
            if trace_walls_here
            else []
        )
        walls_note = None
        if detected_walls and not page_type.get("named_as_a_plan"):
            min_walls = int(wall_settings.get("min_walls_for_an_unnamed_plan", 4))
            min_nominal_share = float(
                wall_settings.get("min_nominal_thickness_share_for_an_unnamed_plan", 0.4)
            )
            at_nominal = sum(1 for w in detected_walls if w.get("matches_nominal_thickness"))
            share = at_nominal / len(detected_walls)
            if len(detected_walls) < min_walls or share < min_nominal_share:
                walls_note = (
                    f"This sheet does not say it is a plan, and the {len(detected_walls)} "
                    f"pairs of parallel lines found on it do not look like a building "
                    f"({at_nominal} of them are at a thickness a wall is built to), so no "
                    "walls are reported from it."
                )
                logger.info(f"{sheet_id}: {walls_note}")
                detected_walls = []
        if not page_type.get("draws_a_plan"):
            detected_rooms = []

        # A building is drawn between its dimension strings, never through
        # them. The strings printed outside the rooms bound the part of the
        # sheet the plan is on, and a pair of parallel lines outside that is a
        # dimension string's own witness lines, not a wall.
        region = drawing_region(
            detected_rooms, chains, page_width, page_height
        )
        mm_per_point = calibration.get("measured_mm_per_point") or calibration.get(
            "printed_mm_per_point"
        )
        if detected_walls and mm_per_point:
            trimmed, dropped = trim_walls_to_the_drawing(
                detected_walls, region, mm_per_point, config
            )
            if trimmed or dropped:
                logger.info(
                    f"{sheet_id}: {trimmed} candidate(s) ran out of the part of the "
                    f"sheet the plan is drawn on and were cut back to it; {dropped} "
                    "lay outside it altogether"
                )

        # **A drawing says an opening is here in up to four ways, and all four
        # are read.** The mark printed beside it carries a schedule row and so
        # a real size and type; the window drawn inside the wall says what kind
        # of window it is and how wide; a door's swing says it is a door and
        # how wide; and a break in the wall says where the wall stops. Reading
        # only the marks meant a plan set that labels nothing returned nothing,
        # and reading only the breaks meant a hole of unknown kind where the
        # drawing had drawn a sliding window plainly.
        # **Three more places a pair of parallel lines is not a wall, and all
        # three are read from the drawing rather than from what is written on
        # it.** Outside the outline the building's own connected walls make;
        # drawn as a dashed line, which is what a roof extent, an eave, a
        # setback and a boundary all are; and inside a panel of printed matter,
        # whose ruled rows are parallel lines a few millimetres apart at
        # drawing scale.
        wall_settings = config.get("walls", {})
        if detected_walls:
            dead = mark_walls_in_dead_ground(
                detected_walls,
                building_outline(detected_walls, config),
                printed_panels(lines, page_width, page_height, wall_settings),
                wall_settings,
            )
            if dead:
                logger.info(
                    f"{sheet_id}: {dead} candidate(s) are outside the building, drawn "
                    "dashed, or inside a panel printed on the sheet"
                )
            # A line finishing in an arrowhead points at something, which a
            # wall never does.
            leaders = mark_walls_with_an_arrow_at_the_end(
                detected_walls, arrow_heads(page, wall_settings), wall_settings
            )
            if leaders:
                logger.info(
                    f"{sheet_id}: {leaders} candidate(s) finish in an arrowhead, so "
                    "they are notes' leaders rather than walls"
                )

        detected_openings = place_openings_on_walls(
            opening_marks, detected_walls, calibration, config, sheet_id
        )
        detected_openings += openings_from_symbols(
            detected_walls, rulings, page, calibration, config, sheet_id
        )
        # **A break in a wall is a candidate, not an opening.** Every gap is
        # put to the drawing and kept only where the drawing says independently
        # that something goes in it — a door's arc about one of its jambs, a
        # mark printed beside it, or glazing drawn between the wall's faces. A
        # gap none of those confirm is written to the issues log as a gap to
        # check rather than reported as a door nobody can verify. The schedule,
        # the fourth reading, is added once the whole document has been read,
        # because it is printed on its own sheet.
        gap_openings, unresolved_gaps = read_openings_from_the_drawing(
            detected_walls, rulings, page, opening_marks, calibration, config, sheet_id,
            detected_dimensions,
        )
        detected_openings += gap_openings
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
            "title_block_found": title_block_found,
            "title_block_note": title_block_note,
            "rooms": detected_rooms,
            "dimensions": detected_dimensions,
            "dimension_chains": chains,
            "schedules": detected_schedules,
            "legends": detected_legends,
            "opening_marks": opening_marks,
            "scale_calibration": calibration,
            "walls": detected_walls,
            "walls_note": walls_note,
            "openings": detected_openings,
            "unresolved_gaps": unresolved_gaps,
            "sheet_index": page_sheet_index,
            "unresolved_items": [],
            "text_evidence": text_evidence,
            "overlay_url": None,
            "detection_overlay_url": None,
            "error": None,
        }
        # The several readings of one opening are joined together later, once
        # every sheet has been read: a mark is only placed on its wall after
        # the schedule that gives it a width has been found, and the schedule
        # is printed on a different sheet.
        page["unresolved_items"] = _collect_unresolved(page, text_evidence)
        return page
    except Exception as e:
        logger.exception(f"analyze_page failed for page={page_number}: {e}")
        return _empty_page(page_number, f"Plan-reading analysis failed for this page: {e}")


def page_lines_and_rulings(page, ocr_blocks: list, dpi: int, use_native_text: bool = True):
    """Convenience wrapper so intake has one call for the page's text model."""
    from pipeline.plan.textmodel import build_page_lines

    lines, evidence = build_page_lines(page, ocr_blocks, dpi, use_native_text)
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
    opening_on_a_plan = opening_position_measured = unmarked_openings = 0
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
            else:
                unmarked_openings += 1
            if opening["in_schedule"]:
                opening_in_schedule += 1
            # An opening can only be placed on a wall of a sheet that has
            # walls. The same door is marked again on an elevation and on a
            # section, and there is no wall there to place it on — counting
            # those in the denominator reported one in three placed on a plan
            # where nine in ten actually were.
            if page.get("walls"):
                opening_on_a_plan += 1
                if opening["wall_id"]:
                    opening_on_wall += 1
                position = opening.get("position_on_wall") or {}
                if position.get("measured_from") == "break_in_the_wall":
                    opening_position_measured += 1

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
            # **A plan set that labels nothing still has doors and windows.**
            # Counting only distinct marks reported "0 doors and windows" on a
            # set that prints none, beside "marked 10 times" — a headline
            # contradicted by the line under it.
            #
            # Where the drawings do print marks, they are what identifies one
            # opening across the sheets it appears on, and the unmarked ones
            # are the same doors seen again on a reflected-ceiling or
            # electrical plan — counting those too would double them. Where
            # the drawings print no marks at all, what was measured is all
            # there is, and that is the count.
            "distinct_openings": (
                len(distinct_openings) if distinct_openings else unmarked_openings
            ),
            "openings_with_no_mark": unmarked_openings,
            "marks_on_drawings": opening_total,
            "matched_to_a_schedule": opening_in_schedule,
            "matched_to_a_schedule_pct": percentage(opening_in_schedule, opening_total),
            "on_a_sheet_that_draws_a_plan": opening_on_a_plan,
            "placed_on_a_wall": opening_on_wall,
            "placed_on_a_wall_pct": percentage(opening_on_wall, opening_on_a_plan),
            "position_measured_from_the_drawing": opening_position_measured,
            "position_measured_pct": percentage(opening_position_measured, opening_on_a_plan),
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
                "run_id", "page_number", "sheet_id", "wall_id", "wall_type", "orientation",
                "runs_along", "length_mm",
                "thickness_mm", "nominal_thickness_mm", "matches_nominal_thickness",
                "connects_to", "junction_count", "openings_on_this_wall",
                "breaks_in_the_wall_mm", "measured_from", "longer_than_sheet_measures",
                "not_used_because", "cut_back_to_the_plan",
                "confidence", "confidence_band", "review_needed", "bbox",
            ]
        )
        for page in pages:
            for wall in page.get("walls", []):
                writer.writerow(
                    [
                        run_id, page["page_number"], page["sheet_id"], wall["wall_id"],
                        wall.get("wall_type", "unknown"), wall.get("orientation", ""),
                        wall["runs_along"], wall["length_mm"], wall["thickness_mm"],
                        wall["nominal_thickness_mm"] or "", wall["matches_nominal_thickness"],
                        "; ".join(wall.get("connects_to", [])),
                        len(wall.get("connects_to", [])),
                        "; ".join(wall["linked_opening_marks"]),
                        "; ".join(str(gap["gap_mm"]) for gap in wall.get("gaps", [])),
                        wall["line_source"],
                        wall.get("longer_than_sheet_measures", False),
                        wall.get("not_used_because") or "",
                        bool(wall.get("trimmed_to_the_drawing")),
                        wall["confidence"], wall["confidence_band"],
                        wall.get("review_needed", True),
                        json.dumps(wall["bbox"]),
                    ]
                )

    # **The walls and their junctions, as data rather than as a table.** A
    # spreadsheet row can carry a wall; it cannot carry the two faces the wall
    # was measured from, the breaks in it and the graph it sits in. Those go to
    # JSON, which is what every later stage reads (Critical Rule 2) and what a
    # reviewer opens to check one wall against the sheet it came from.
    write_walls_json(run_dir, run_id, pages)

    openings_path = run_dir / "openings.csv"
    with openings_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "run_id", "page_number", "sheet_id", "opening_id", "mark",
                "shown_on_the_sheet_as", "name_was_made_up", "element_type",
                "how_the_drawing_said_so", "how_many_readings_agree",
                "a_reviewer_should_check_this", "how_it_was_decided",
                "wall_id", "width_mm", "height_mm", "sill_height_mm", "head_height_mm",
                "location_on_plan", "schedule_sheet", "schedule_row_id", "in_schedule",
                "found_by", "position_along_the_wall_mm", "position_measured_from",
                "how_it_was_placed", "confidence", "confidence_band", "source_bbox",
            ]
        )
        for page in pages:
            for opening in page.get("openings", []):
                writer.writerow(
                    [
                        run_id, page["page_number"], page["sheet_id"], opening["opening_id"],
                        opening["mark"],
                        opening.get("display_mark") or opening["mark"],
                        bool(opening.get("display_mark_is_made_up")),
                        opening["element_type"] or "",
                        "; ".join(opening.get("evidence") or []),
                        opening.get("evidence_count", 0),
                        bool(opening.get("review_needed", True)),
                        opening.get("how_it_was_decided") or "",
                        opening["wall_id"] or "",
                        opening["width_mm"] if opening["width_mm"] is not None else "",
                        opening["height_mm"] if opening["height_mm"] is not None else "",
                        opening["sill_height_mm"] if opening["sill_height_mm"] is not None else "",
                        opening["head_height_mm"] if opening["head_height_mm"] is not None else "",
                        opening["location_on_plan"] or "", opening["schedule_sheet"] or "",
                        opening["schedule_row_id"] or "", opening["in_schedule"],
                        opening.get("found_by", ""),
                        (opening.get("position_on_wall") or {}).get("from_wall_start_mm", ""),
                        _plain_position_source(opening),
                        opening.get("wall_note") or "",
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


def write_walls_json(run_dir: Path, run_id: str, pages: list) -> None:
    """``walls.json`` and ``wall_graph.json`` for this run.

    Both are written for every run, even when no sheet in the document draws a
    plan: a file that says plainly that no walls were found is a result, and a
    missing file is a reader wondering whether the run finished.
    """
    walls = []
    graphs = []
    for page in pages:
        page_walls = page.get("walls", [])
        walls.extend(walls_as_records(page_walls))
        # **The graph is built here rather than carried on every page.** It is
        # the same junctions the walls already hold, written a second way, and
        # keeping both put 84 KB of duplicate into the reading the browser has
        # to download before it can show anything.
        graph = wall_graph_for(page_walls, page["sheet_id"], page["page_number"])
        if graph.get("nodes"):
            graphs.append(graph)

    payload = {
        "run_id": run_id,
        "units": "millimetres for lengths, PDF points for positions on the sheet",
        "wall_count": len(walls),
        "outer_wall_count": sum(1 for w in walls if w["wall_type"] == "outer"),
        "inner_wall_count": sum(1 for w in walls if w["wall_type"] == "inner"),
        "unknown_wall_count": sum(1 for w in walls if w["wall_type"] == "unknown"),
        "walls": walls,
    }
    try:
        (run_dir / "walls.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
    except Exception as e:
        logger.exception(f"could not write walls.json for run={run_id}: {e}")

    graph_payload = {
        "run_id": run_id,
        "note": (
            "Walls are the nodes and the places they meet are the edges. Each edge "
            "says which shape the meeting makes on the drawing - L at a corner, T "
            "where a wall lands partway along another, + where two cross, collinear "
            "where one wall carries on past a doorway - and where on the sheet it "
            "happens, in PDF points."
        ),
        "sheets": graphs,
        "junction_count": sum(graph["junction_count"] for graph in graphs),
    }
    try:
        (run_dir / "wall_graph.json").write_text(
            json.dumps(graph_payload, indent=2), encoding="utf-8"
        )
    except Exception as e:
        logger.exception(f"could not write wall_graph.json for run={run_id}: {e}")


def _plain_position_source(opening: dict) -> str:
    """How an opening's place along its wall was arrived at, in a reader's words."""
    position = opening.get("position_on_wall") or {}
    if position.get("measured_from") == "break_in_the_wall":
        return "Measured from the break in the wall"
    if position.get("measured_from") == "the_mark_on_the_drawing":
        return "Taken from where the mark is printed"
    return ""
