"""Day 3 — the drawing index printed on the cover sheet, and the cross-check.

Australian plan sets almost always print a drawing index somewhere in the set
(usually the cover): one row per sheet giving its number, title, scale and
revision. That table is an independent statement, made by the same drawing
office, about what every sheet in the set is.

That makes it the most valuable thing on the cover sheet and it was previously
being thrown away. Used properly it does two jobs:

*   **Verification.** A title-block value that also appears in the index is
    confirmed by a second source, so it can be reported as verified instead of
    merely detected. A value that *disagrees* with the index is a real finding
    and is raised as an unresolved item — this is the only way Day 3 can claim
    accuracy from evidence rather than from confidence heuristics.
*   **Filling genuine blanks.** Some sheets leave a title-block cell empty (a
    scale printed in the index but not on the sheet itself). The index value
    is then used, recorded with ``technique = "sheet_index"`` and a note
    saying it came from the index rather than from this sheet, so it is never
    mistaken for something read off the drawing.

The table is found by its header row rather than by a caption, because the
header words ('#', 'DRAWING NAME', 'SCALE', 'REV') are what actually define
the columns, and a set that omits the caption still has them.
"""

from app.logging_setup import get_logger
from pipeline.plan.textmodel import (
    bbox_center,
    bbox_height,
    group_into_rows,
    is_placeholder,
    normalize_label,
)
from pipeline.plan.validators import (
    scale_ratios,
    validate_revision,
    validate_scale,
    validate_sheet_number,
    validate_sheet_title,
)

logger = get_logger()

# Typographic characters used in the messages this module produces for
# people reading the results.
LDQ, RDQ, DASH = "\u201c", "\u201d", "\u2014"

# A header row must identify at least this many of the index columns before
# the rows beneath it are read as an index — one matching word is far too
# easy to hit by accident on a busy sheet.
_MIN_HEADER_ROLES = 2

# Rows are read downwards until the vertical gap to the next row exceeds this
# multiple of the typical row pitch, which is how a table's end is detected
# without knowing how many sheets the set contains.
_MAX_ROW_GAP_FACTOR = 3.0


def _match_header_role(text: str, header_keywords: dict):
    normalized = normalize_label(text)
    for role, words in header_keywords.items():
        for word in words:
            if normalized == normalize_label(word):
                return role
    return None


def _find_header_row(rows: list, header_keywords: dict):
    """The first row that names at least two index columns, one of which must
    be the sheet number or the sheet title — a row naming only 'SCALE' and
    'REV' is a title-block fragment, not an index header."""
    for index, row in enumerate(rows):
        roles = {}
        for line in row:
            role = _match_header_role(line["text"], header_keywords)
            if role and role not in roles:
                roles[role] = line
        if len(roles) < _MIN_HEADER_ROLES:
            continue
        if "sheet_number" not in roles and "sheet_title" not in roles:
            continue
        return index, roles
    return None, None


def _column_bands(roles: dict, page_width: float):
    """Column boundaries taken as the midpoints between adjacent headers.

    Midpoints rather than header edges, because a column's values are not
    always aligned with its header: on the supplied cover sheet the '#'
    header sits at x=38 while its values start at x=32.
    """
    ordered = sorted(roles.items(), key=lambda item: bbox_center(item[1]["bbox"])[0])
    centres = [(role, bbox_center(line["bbox"])[0]) for role, line in ordered]
    bands = {}
    for position, (role, centre) in enumerate(centres):
        left = 0.0 if position == 0 else (centres[position - 1][1] + centre) / 2.0
        right = (
            page_width
            if position == len(centres) - 1
            else (centre + centres[position + 1][1]) / 2.0
        )
        bands[role] = (left, right)
    return bands


def _cells_for_row(row: list, bands: dict):
    cells = {role: [] for role in bands}
    for line in row:
        centre_x = bbox_center(line["bbox"])[0]
        for role, (left, right) in bands.items():
            if left <= centre_x < right:
                cells[role].append(line)
                break
    for role in cells:
        cells[role].sort(key=lambda ln: (ln["bbox"][1], ln["bbox"][0]))
    return cells


def _cell_text(lines: list) -> str:
    return " ".join(ln["text"].strip() for ln in lines if ln["text"].strip()).strip()


def parse_sheet_index(lines: list, page_number: int, page_width: float, config: dict):
    """Reads a drawing index table from one page, or returns None.

    Returns {"source_page", "header_bbox", "entries": [...]} where each entry
    carries the printed sheet number, title, scale and revision plus the bbox
    of the row it was read from, so every cross-check finding stays traceable
    to the exact place on the cover sheet that supports it.
    """
    index_config = config.get("sheet_index", {})
    header_keywords = index_config.get("column_header_keywords", {})
    if not header_keywords:
        return None

    rows = group_into_rows(lines)
    header_index, roles = _find_header_row(rows, header_keywords)
    if header_index is None:
        return None

    bands = _column_bands(roles, page_width)
    header_row = rows[header_index]
    header_bbox = [
        min(ln["bbox"][0] for ln in header_row),
        min(ln["bbox"][1] for ln in header_row),
        max(ln["bbox"][2] for ln in header_row),
        max(ln["bbox"][3] for ln in header_row),
    ]

    # The gap that ends the table is measured against the table's own row
    # pitch, which is only known once two rows have been read. Until then a
    # generous allowance is used, because a header row is normally padded away
    # from the first entry by more than one row height.
    header_height = max(bbox_height(header_bbox), 6.0)
    row_pitch = None
    entries: list = []
    previous_y = header_bbox[3]

    for row in rows[header_index + 1 :]:
        row_top = min(ln["bbox"][1] for ln in row)
        gap = row_top - previous_y
        allowed = (row_pitch * _MAX_ROW_GAP_FACTOR) if row_pitch else header_height * 6.0
        if gap > allowed:
            break  # the table has ended
        if row_pitch is None and entries and gap > 0:
            row_pitch = max(gap, header_height)

        cells = _cells_for_row(row, bands)
        number_text = _cell_text(cells.get("sheet_number", []))
        title_text = _cell_text(cells.get("sheet_title", []))
        scale_text = _cell_text(cells.get("scale", []))
        revision_text = _cell_text(cells.get("revision", []))
        if not number_text and not title_text:
            continue

        # A long title wraps onto a second line, which arrives as a row that
        # has text in the title column and nothing at all in any other column
        # ('PLAN DET. & INT. ELEV - MASTER' / 'BED ENS. & WIR'). That is a
        # continuation of the row above, not a sheet of its own. A row that
        # merely leaves its number blank but still prints its other cells (a
        # cover sheet with no drawing number) is a real row and is kept.
        if entries and title_text and not number_text and not scale_text and not revision_text:
            previous = entries[-1]
            if previous.get("sheet_title"):
                previous["sheet_title"] = f"{previous['sheet_title']} {title_text}".strip()
                previous["source_bbox"] = [
                    min(previous["source_bbox"][0], min(ln["bbox"][0] for ln in row)),
                    min(previous["source_bbox"][1], row_top),
                    max(previous["source_bbox"][2], max(ln["bbox"][2] for ln in row)),
                    max(previous["source_bbox"][3], max(ln["bbox"][3] for ln in row)),
                ]
                previous_y = previous["source_bbox"][3]
                continue

        number = None
        if number_text and not is_placeholder(number_text):
            result = validate_sheet_number(number_text)
            if result:
                number = result[0]

        title = None
        if title_text and not is_placeholder(title_text):
            result = validate_sheet_title(title_text, exclusion_keywords=[], max_length=120)
            if result:
                title = result[0]

        if number is None and title is None:
            continue

        scale = None
        if scale_text:
            result = validate_scale(scale_text)
            if result:
                scale = result[0]

        revision = None
        if revision_text:
            result = validate_revision(revision_text)
            if result:
                revision = result[0]

        row_bbox = [
            min(ln["bbox"][0] for ln in row),
            row_top,
            max(ln["bbox"][2] for ln in row),
            max(ln["bbox"][3] for ln in row),
        ]
        entries.append(
            {
                "sheet_number": number,
                "sheet_title": title,
                "scale": scale,
                "revision": revision,
                "source_page": page_number,
                "source_bbox": [round(v, 2) for v in row_bbox],
            }
        )
        previous_y = row_bbox[3]

    if len(entries) < 2:
        return None  # one row is not a table

    logger.info(
        f"sheet index found on page {page_number}: {len(entries)} entries, "
        f"columns={sorted(bands)}"
    )
    return {
        "source_page": page_number,
        "header_bbox": [round(v, 2) for v in header_bbox],
        "columns": sorted(bands),
        "entries": entries,
    }


# --- Cross-check ----------------------------------------------------------

_CROSS_CHECKED_FIELDS = ("sheet_title", "scale", "revision")


def _normalize_for_comparison(value) -> str:
    if value is None:
        return ""
    return " ".join(str(value).upper().replace(".", " ").split())


def _values_agree(field_name: str, on_sheet, in_index) -> bool:
    """Whether the sheet and the drawing index are saying the same thing.

    Scale needs more than string equality. A sheet is often drawn at one scale
    with an enlarged detail beside it at another, and prints both ('1:100,
    1:1'), while the index lists only the scale of the drawing itself
    ('1:100'). That is agreement, not a discrepancy, so the index is treated as
    agreeing when every scale it lists is also printed on the sheet.
    """
    if _normalize_for_comparison(on_sheet) == _normalize_for_comparison(in_index):
        return True
    if field_name == "scale":
        sheet_ratios = scale_ratios(on_sheet or "")
        index_ratios = scale_ratios(in_index or "")
        if sheet_ratios and index_ratios:
            return set(index_ratios).issubset(set(sheet_ratios))
    return False


def cross_check_pages(pages: list, index: dict, config: dict) -> dict:
    """Compares every page's title block against the drawing index.

    Mutates each page's title-block fields in place to record the result:
    ``verified_against_index`` becomes True (agrees), False (disagrees) or
    stays None (no index row to compare with). Where the sheet left a field
    blank and the index has it, the value is filled in and clearly attributed.

    Returns a report with the counts and every disagreement, which is what the
    accuracy figures shown in the interface are computed from — a measured
    comparison against a second source in the document, not an opinion.
    """
    report = {
        "index_source_page": index.get("source_page") if index else None,
        "index_entry_count": len(index.get("entries", [])) if index else 0,
        "compared_pages": 0,
        "agreements": 0,
        "disagreements": 0,
        "filled_from_index": 0,
        "unmatched_index_entries": [],
        "findings": [],
    }
    if not index or not index.get("entries"):
        return report

    thresholds = config["confidence_thresholds"]
    by_number = {
        entry["sheet_number"]: entry
        for entry in index["entries"]
        if entry.get("sheet_number")
    }
    matched_numbers = set()

    for page in pages:
        fields = page["title_block"]
        number = fields.get("sheet_number", {}).get("value")
        entry = by_number.get(number) if number else None
        if entry is None:
            continue
        matched_numbers.add(number)
        report["compared_pages"] += 1

        for field_name in _CROSS_CHECKED_FIELDS:
            expected = entry.get(field_name)
            if not expected:
                continue
            field = fields.get(field_name)
            if field is None:
                continue

            if field["value"] is None:
                # The sheet leaves this blank but the index states it. Using
                # it is legitimate and useful, provided the record says plainly
                # that it came from the index and not from this sheet.
                confidence = round(0.8, 3)
                field.update(
                    {
                        "value": expected,
                        "raw_text": expected,
                        "confidence": confidence,
                        "confidence_band": "high" if confidence >= thresholds["review"] else "review",
                        "source_bbox": entry["source_bbox"],
                        "extraction_method": "native",
                        "technique": "sheet_index",
                        "note": (
                            "Not printed on this sheet; taken from the drawing index on "
                            f"page {entry['source_page']}."
                        ),
                        "verified_against_index": None,
                        "review_status": "needs_review",
                    }
                )
                report["filled_from_index"] += 1
                # The page's unresolved list was built before the index was
                # consulted, so the "not found" entry for this field is now
                # stale. Leaving it would report a value and a complaint about
                # its absence at the same time.
                page["unresolved_items"] = [
                    item
                    for item in page["unresolved_items"]
                    if item["category"] != f"title_block.{field_name}"
                ]
                page["unresolved_items"].append(
                    {
                        "item_id": f"P{page['page_number']:02d}-IDX-{field_name}",
                        "category": f"title_block.{field_name}",
                        "severity": "P2",
                        "reason": (
                            f"Not printed on this sheet. The value shown "
                            f"({LDQ}{expected}{RDQ}) comes from the drawing index on page "
                            f"{entry['source_page']}."
                        ),
                        "text": expected,
                        "bbox": entry["source_bbox"],
                    }
                )
                continue

            if _values_agree(field_name, field["value"], expected):
                field["verified_against_index"] = True
                field["review_status"] = "confirmed"
                field["confidence"] = round(min(field["confidence"] + 0.05, 1.0), 3)
                field["confidence_band"] = (
                    "high" if field["confidence"] >= thresholds["review"] else field["confidence_band"]
                )
                report["agreements"] += 1
            else:
                field["verified_against_index"] = False
                field["review_status"] = "needs_review"
                finding = {
                    "page_number": page["page_number"],
                    "sheet_number": number,
                    "field": field_name,
                    "on_sheet": field["value"],
                    "in_index": expected,
                    "index_page": entry["source_page"],
                }
                report["findings"].append(finding)
                report["disagreements"] += 1
                page["unresolved_items"].append(
                    {
                        "item_id": f"P{page['page_number']:02d}-XCHK-{field_name}",
                        "category": f"cross_check.{field_name}",
                        "severity": "P1",
                        "reason": (
                            f"This sheet shows {LDQ}{field['value']}{RDQ}, but the drawing "
                            f"index on page {entry['source_page']} shows "
                            f"{LDQ}{expected}{RDQ}. Both are kept; neither has been assumed "
                            "correct."
                        ),
                        "text": field["value"],
                        "bbox": field["source_bbox"],
                    }
                )

    for entry in index["entries"]:
        number = entry.get("sheet_number")
        if number and number not in matched_numbers:
            report["unmatched_index_entries"].append(
                {"sheet_number": number, "sheet_title": entry.get("sheet_title")}
            )

    return report
