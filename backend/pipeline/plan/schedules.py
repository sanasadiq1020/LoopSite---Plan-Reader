"""Day 3 — schedules and legends, read as real tables.

The earlier version searched for the word 'SCHEDULE' and recorded that it had
seen it. That satisfies a checklist but produces nothing usable: the supplied
plan set devotes four full sheets to door and window schedules, and every
opening's ID, location, height, width, frame, glazing and operation type sits
in those tables. Day 6 needs the width and height to cut an opening void, Day 9
builds its master opening schedule from them. So this module extracts the
contents.

Both real-world layouts are handled, because the supplied plans use both:

*   **Row-oriented** — a header row names the columns ('MARK', 'WIDTH',
    'HEIGHT', ...) and each subsequent row is one item.
*   **Column-oriented (transposed)** — the attribute names run down the left
    ('ID', 'Location', 'Height', 'Width', 'Frame Type', ...) and each *column*
    is one item. This is what the supplied door and window schedules use, and
    a row-only reader extracts nothing at all from them.

Which layout was found is recorded on the table, along with the header
positions it was read from, so the extraction stays checkable against the
sheet.

Column and row boundaries come from the printed header positions, not from
fixed offsets, and any cell that cannot be assigned to a column is reported
rather than dropped.
"""

import re

from app.logging_setup import get_logger
from pipeline.plan.textmodel import (
    bbox_center,
    bbox_height,
    group_into_rows,
    is_placeholder,
    normalize_label,
)

logger = get_logger()

# An attribute column in a transposed table must name at least this many known
# roles before the table is read that way — fewer is coincidence.
_MIN_TRANSPOSED_ROLES = 3
# Same idea for a conventional header row.
_MIN_ROW_HEADER_ROLES = 3

# Labels in a transposed table's attribute column share an x within this many
# points of each other.
_ATTRIBUTE_COLUMN_TOLERANCE_PT = 4.0


def _role_of(text: str, header_keywords: dict):
    normalized = normalize_label(text)
    for role, words in header_keywords.items():
        for word in words:
            if normalized == normalize_label(word):
                return role
    return None


_MAX_ATTRIBUTE_NAME_LENGTH = 30


_CONTACT_DETAIL_RE = re.compile(r"@|WWW\.|HTTPS?://|\.COM|\.AU\b|\bABN\b|\bACN\b", re.IGNORECASE)


def _looks_like_attribute_name(text: str) -> bool:
    """A schedule's attribute name is a short noun phrase ('Frame Finish',
    'Window Sill Height'), never an address, a contact detail, a sentence or a
    bare number. These sheets print the office's address, email and web
    address down the same column the schedule uses, so shape is what
    separates a real attribute row from the footer."""
    stripped = text.strip()
    if not stripped or len(stripped) > _MAX_ATTRIBUTE_NAME_LENGTH:
        return False
    if _CONTACT_DETAIL_RE.search(stripped):
        return False
    letters = sum(1 for c in stripped if c.isalpha())
    if letters < 2 or letters / len(stripped) < 0.6:
        return False
    return not any(ch in stripped for ch in ",;")


def _canonical_key(text: str) -> str:
    """A printed attribute name reduced to a stable key ('Door Leaf Type' ->
    'door_leaf_type'), so a table keeps every column the drawing printed even
    when it is not one of the roles we know by name."""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", text.strip().lower()).strip("_")
    return slug or "column"


# --- Transposed (attribute rows, item columns) ----------------------------


def _find_transposed_tables(lines: list, header_keywords: dict) -> list:
    """Every transposed schedule block on the sheet, in printed order.

    A sheet routinely carries more than one block — the supplied door schedule
    prints D1-D5 in an upper block and D6-D9 in a lower one, both using the
    same attribute column. Reading them as a single table silently merges D6's
    values into D2's row, so each block is bounded by the next block's mark row
    and read separately.
    """
    role_lines = []
    for line in lines:
        role = _role_of(line["text"], header_keywords)
        if role:
            role_lines.append((role, line))
    if len(role_lines) < _MIN_TRANSPOSED_ROLES:
        return []

    # Group the role labels by their left edge — a transposed table's attribute
    # names form one column.
    columns: dict = {}
    for role, line in role_lines:
        key = round(line["bbox"][0] / _ATTRIBUTE_COLUMN_TOLERANCE_PT)
        columns.setdefault(key, []).append((role, line))
    best = max(columns.values(), key=len)
    if len(best) < _MIN_TRANSPOSED_ROLES:
        return []

    attribute_x = min(line["bbox"][0] for _, line in best)
    rows = group_into_rows(lines)

    def attribute_cell(row):
        for line in row:
            if abs(line["bbox"][0] - attribute_x) <= _ATTRIBUTE_COLUMN_TOLERANCE_PT:
                return line
        return None

    # Rows that open a block: the attribute cell names the item identifier.
    block_starts = []
    for index, row in enumerate(rows):
        cell = attribute_cell(row)
        if cell is not None and _role_of(cell["text"], header_keywords) == "mark":
            block_starts.append(index)
    if not block_starts:
        return []

    tables: list = []
    for block_number, start in enumerate(block_starts):
        end = block_starts[block_number + 1] if block_number + 1 < len(block_starts) else len(rows)
        mark_row = rows[start]
        mark_cell = attribute_cell(mark_row)
        item_cells = [
            ln
            for ln in mark_row
            if ln["bbox"][0] > mark_cell["bbox"][2] + 1.0 and not is_placeholder(ln["text"])
        ]
        if len(item_cells) < 2:
            continue

        item_centres = sorted(
            (bbox_center(ln["bbox"])[0], ln["text"].strip()) for ln in item_cells
        )
        pitch = min(
            (b[0] - a[0] for a, b in zip(item_centres, item_centres[1:])),
            default=120.0,
        )
        max_offset = max(pitch * 0.6, 30.0)

        items = [{"_mark": text, "_bbox": None} for _, text in item_centres]
        attribute_names: list = []
        unassigned: list = []
        role_by_key: dict = {}

        previous_bottom = mark_row[0]["bbox"][3]

        for row in rows[start:end]:
            cell = attribute_cell(row)
            if cell is None:
                continue

            attribute_label = cell["text"].strip().rstrip(":")
            # Attribute rows are separated by the drawn elevation panels on
            # these sheets, so a vertical gap cannot be used to find the end of
            # the block. What distinguishes a real attribute name is its shape:
            # a short mostly-alphabetic phrase. This is what keeps the sheet's
            # footer address block — which sits in the same column — out of the
            # schedule.
            if not _looks_like_attribute_name(attribute_label):
                continue
            previous_bottom = max(previous_bottom, max(ln["bbox"][3] for ln in row))
            value_cells = [ln for ln in row if ln["bbox"][0] > cell["bbox"][2] + 1.0]
            if len(value_cells) < 2:
                # A label with no values on its row is a drawn panel (the
                # elevation of each door), not a data row.
                continue

            key = _canonical_key(attribute_label)
            role = _role_of(attribute_label, header_keywords)
            if key not in attribute_names:
                attribute_names.append(key)
            if role and role not in role_by_key.values():
                role_by_key[key] = role

            for value_cell in value_cells:
                centre = bbox_center(value_cell["bbox"])[0]
                distance, position = min(
                    (abs(centre - item_centre), index)
                    for index, (item_centre, _) in enumerate(item_centres)
                )
                if distance > max_offset:
                    unassigned.append(
                        {"attribute": key, "text": value_cell["text"], "bbox": value_cell["bbox"]}
                    )
                    continue
                item = items[position]
                value = value_cell["text"].strip()
                item[key] = f"{item[key]} {value}".strip() if item.get(key) else value
                existing = item.get("_bbox")
                item["_bbox"] = (
                    value_cell["bbox"]
                    if existing is None
                    else [
                        min(existing[0], value_cell["bbox"][0]),
                        min(existing[1], value_cell["bbox"][1]),
                        max(existing[2], value_cell["bbox"][2]),
                        max(existing[3], value_cell["bbox"][3]),
                    ]
                )

        # Expose each column under its canonical role as well as its printed
        # name, so a consumer can ask for 'width' without knowing the drawing
        # happened to label it 'Width'.
        for key, role in role_by_key.items():
            for item in items:
                if key in item and role not in item:
                    item[role] = item[key]

        if attribute_names:
            tables.append(
                {
                    "orientation": "column_per_item",
                    "attribute_column_x": round(attribute_x, 2),
                    "attributes": attribute_names,
                    "items": items,
                    "unassigned_cells": unassigned,
                    "bbox": [
                        min(ln["bbox"][0] for ln in mark_row),
                        mark_row[0]["bbox"][1],
                        max(ln["bbox"][2] for ln in mark_row),
                        previous_bottom,
                    ],
                }
            )
    return tables


# --- Row-oriented ---------------------------------------------------------


def _find_row_table(lines: list, header_keywords: dict):
    rows = group_into_rows(lines)
    for index, row in enumerate(rows):
        roles = {}
        for line in row:
            role = _role_of(line["text"], header_keywords)
            if role and role not in roles:
                roles[role] = line
        if len(roles) < _MIN_ROW_HEADER_ROLES:
            continue

        headers = sorted(
            [(role, line) for role, line in roles.items()],
            key=lambda item: bbox_center(item[1]["bbox"])[0],
        )
        centres = [(role, bbox_center(line["bbox"])[0]) for role, line in headers]
        bands = {}
        for position, (role, centre) in enumerate(centres):
            left = float("-inf") if position == 0 else (centres[position - 1][1] + centre) / 2.0
            right = (
                float("inf")
                if position == len(centres) - 1
                else (centre + centres[position + 1][1]) / 2.0
            )
            bands[role] = (left, right)

        header_bottom = max(line["bbox"][3] for _, line in headers)
        pitch = max(bbox_height(headers[0][1]["bbox"]), 6.0)
        items: list = []
        previous_bottom = header_bottom
        for data_row in rows[index + 1 :]:
            top = min(ln["bbox"][1] for ln in data_row)
            if top - previous_bottom > pitch * 3.0:
                break
            item = {"_mark": None, "_bbox": None}
            filled = 0
            for line in data_row:
                centre = bbox_center(line["bbox"])[0]
                for role, (left, right) in bands.items():
                    if left <= centre < right:
                        value = line["text"].strip()
                        if not value or is_placeholder(value):
                            break
                        item[role] = f"{item[role]} {value}".strip() if item.get(role) else value
                        filled += 1
                        existing = item.get("_bbox")
                        item["_bbox"] = (
                            line["bbox"]
                            if existing is None
                            else [
                                min(existing[0], line["bbox"][0]),
                                min(existing[1], line["bbox"][1]),
                                max(existing[2], line["bbox"][2]),
                                max(existing[3], line["bbox"][3]),
                            ]
                        )
                        break
            if filled >= 2:
                item["_mark"] = item.get("mark")
                items.append(item)
                previous_bottom = max(ln["bbox"][3] for ln in data_row)

        if len(items) >= 2:
            # The table's extent comes from every cell in it, not from the
            # header row: the last column's values run past the last header,
            # and a region drawn to the headers alone leaves those values
            # outside it — where they were read a second time as rooms.
            cell_boxes = [item["_bbox"] for item in items if item.get("_bbox")]
            cell_boxes += [line["bbox"] for _, line in headers]
            return [
                {
                    "orientation": "row_per_item",
                    "attribute_column_x": None,
                    "attributes": [role for role, _ in headers],
                    "items": items,
                    "unassigned_cells": [],
                    "bbox": [
                        min(box[0] for box in cell_boxes),
                        min(box[1] for box in cell_boxes),
                        max(box[2] for box in cell_boxes),
                        max(previous_bottom, max(box[3] for box in cell_boxes)),
                    ],
                }
            ]
    return []


# --- Public API -----------------------------------------------------------

_SIZE_RE = re.compile(r"(\d{1,3}(?:,\d{3})+|\d{2,6})")


def _numeric_mm(value):
    if value is None:
        return None
    match = _SIZE_RE.search(str(value))
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def detect_schedules(lines: list, config: dict, sheet_id: str, sheet_title) -> list:
    """Extracts every schedule table found on a sheet, with its rows."""
    schedule_config = config.get("schedules", {})
    header_keywords = schedule_config.get("column_header_keywords", {})
    title_keywords = schedule_config.get("table_title_keywords", [])
    minimum_rows = schedule_config.get("min_rows_for_table", 2)
    thresholds = config["confidence_thresholds"]
    opening_config = config.get("openings", {})

    if not header_keywords:
        return []

    tables = _find_transposed_tables(lines, header_keywords) or _find_row_table(
        lines, header_keywords
    )
    tables = [t for t in tables if len(t["items"]) >= minimum_rows]
    if not tables:
        return []

    # Captions printed on the sheet, so each block can be named by the one
    # directly above it rather than by whichever appears first.
    captions = [
        line
        for line in lines
        if any(keyword in line["text"].upper() for keyword in title_keywords)
    ]

    def caption_for(table_bbox):
        if not table_bbox:
            return None
        above = [c for c in captions if c["bbox"][3] <= table_bbox[1] + 4.0]
        if above:
            return max(above, key=lambda c: c["bbox"][1])
        return captions[0] if captions else None

    output: list = []
    for table_number, table in enumerate(tables, start=1):
        caption_line = caption_for(table.get("bbox"))
        caption = caption_line["text"].strip() if caption_line else None
        caption_bbox = caption_line["bbox"] if caption_line else None
        output.append(
            _build_table(
                table, table_number, caption, caption_bbox, sheet_id, sheet_title,
                thresholds, opening_config,
            )
        )
    return output


def _build_table(
    table, table_number, caption, caption_bbox, sheet_id, sheet_title,
    thresholds, opening_config,
):
    rows: list = []
    for position, item in enumerate(table["items"], start=1):
        mark = item.get("mark") or item.get("_mark")
        values = {
            key: value
            for key, value in item.items()
            if not key.startswith("_") and value not in (None, "")
        }
        width_mm = _numeric_mm(values.get("width"))
        height_mm = _numeric_mm(values.get("height"))
        if width_mm is None and height_mm is None and values.get("size"):
            parts = _SIZE_RE.findall(str(values["size"]))
            if len(parts) >= 2:
                width_mm = float(parts[0].replace(",", ""))
                height_mm = float(parts[1].replace(",", ""))

        element_type = None
        flags: list = []
        if mark:
            prefix_match = re.match(r"^([A-Z]{1,3})", str(mark).upper())
            if prefix_match:
                element_type = opening_config.get("mark_prefixes", {}).get(
                    prefix_match.group(1)
                )
        if element_type == "door" and width_mm is not None:
            limits = opening_config.get("plausible_door_width_mm", {})
            if limits and not (limits["min"] <= width_mm <= limits["max"]):
                flags.append(
                    f"width {width_mm:.0f} mm is outside the plausible door range "
                    f"({limits['min']}-{limits['max']} mm)"
                )
        if element_type == "window" and width_mm is not None:
            limits = opening_config.get("plausible_window_width_mm", {})
            if limits and not (limits["min"] <= width_mm <= limits["max"]):
                flags.append(
                    f"width {width_mm:.0f} mm is outside the plausible window range "
                    f"({limits['min']}-{limits['max']} mm)"
                )

        # A window schedule that prints sill height, head height and the
        # opening height states the same thing twice: head = sill + height.
        # Checking it is a real arithmetic test of the extraction, using only
        # what the drawing itself printed, and it catches a cell read from the
        # wrong column immediately.
        sill_mm = _numeric_mm(values.get("window_sill_height") or values.get("sill_height"))
        head_mm = _numeric_mm(values.get("window_head_height") or values.get("head_height"))
        geometry_check = None
        if sill_mm is not None and head_mm is not None and height_mm is not None:
            difference = (sill_mm + height_mm) - head_mm
            geometry_check = {
                "rule": "sill height + opening height = head height",
                "expected_head_mm": round(sill_mm + height_mm, 1),
                "printed_head_mm": round(head_mm, 1),
                "difference_mm": round(difference, 1),
                "result": "pass" if abs(difference) <= 5.0 else "fail",
            }
            if geometry_check["result"] == "fail":
                flags.append(
                    f"sill ({sill_mm:.0f}) + height ({height_mm:.0f}) = "
                    f"{sill_mm + height_mm:.0f} mm, but the head height is printed as "
                    f"{head_mm:.0f} mm"
                )

        confidence = 0.9 if mark and (width_mm or height_mm) else 0.7
        if geometry_check and geometry_check["result"] == "pass":
            confidence = min(confidence + 0.05, 1.0)
        if flags:
            confidence = min(confidence, 0.6)
        band = (
            "high"
            if confidence >= thresholds["review"]
            else ("review" if confidence >= thresholds["low"] else "low")
        )
        rows.append(
            {
                "row_id": f"{sheet_id}-T{table_number:02d}-SCH{position:02d}",
                "mark": str(mark) if mark else None,
                "element_type": element_type,
                "width_mm": width_mm,
                "height_mm": height_mm,
                "values": values,
                "geometry_check": geometry_check,
                "flags": flags,
                "bbox": item.get("_bbox"),
                "confidence": confidence,
                "confidence_band": band,
                "review_status": "confirmed" if band == "high" and not flags else "needs_review",
            }
        )

    logger.info(
        f"{sheet_id}: schedule table {table_number} ({table['orientation']}) with "
        f"{len(rows)} rows, columns={table['attributes']}"
    )
    return {
        "table_id": f"{sheet_id}-TBL{table_number:02d}",
        "caption": caption or (sheet_title or "Schedule"),
        "caption_source": "printed_caption" if caption else "sheet_title",
        "caption_bbox": caption_bbox,
        "orientation": table["orientation"],
        "bbox": table.get("bbox"),
        "columns": table["attributes"],
        "row_count": len(rows),
        "rows": rows,
        "unassigned_cells": table["unassigned_cells"],
    }


# --- Legends --------------------------------------------------------------




# A legend caption is a short heading, not a sentence.
_MAX_CAPTION_LENGTH = 40
# Two cells belong to the same legend column when their left edges agree to
# within this many points.
_LEGEND_COLUMN_TOLERANCE_PT = 4.0
# A symbol is a short printed code ('CSD', '2W', '1050'), never a phrase.
_MAX_SYMBOL_LENGTH = 8


def _legend_captions(lines: list, title_keywords: list) -> list:
    """Every legend heading on the sheet, in printed order.

    A heading is matched loosely on purpose: these sheets print both
    'ABBREVIATIONS:' and 'ELECTRICAL LEGEND GF', and the second one has to be
    recognised or its entries are swallowed into the legend above it.
    """
    captions = []
    for line in lines:
        text = line["text"].strip()
        if not text or len(text) > _MAX_CAPTION_LENGTH:
            continue
        upper = normalize_label(text)
        if upper in title_keywords or any(
            keyword in upper for keyword in title_keywords if len(keyword) >= 5
        ):
            captions.append(line)
    captions.sort(key=lambda ln: (ln["bbox"][1], ln["bbox"][0]))
    return captions


def _column_positions(rows: list) -> list:
    """The left edges of a legend's columns, taken from the rows that actually
    have more than one cell.

    Locking the columns is what keeps a neighbouring legend out: on the
    supplied site plan a shadow key sits directly below the abbreviations list
    and shares its left margin, and only the second column tells the two apart.
    """
    counts: dict = {}
    multi_cell_rows = 0
    for row in rows:
        if len(row) < 2:
            continue
        multi_cell_rows += 1
        for index, cell in enumerate(row[:3]):
            key = (index, round(cell["bbox"][0] / _LEGEND_COLUMN_TOLERANCE_PT))
            counts[key] = counts.get(key, 0) + 1

    # A column of a table is somewhere most of that table's rows print
    # something. Accepting a position that appeared twice invented a third
    # column out of two survey figures the drawing prints 250 points to the
    # right of an abbreviations list — and every legend row without a cell
    # there then looked incomplete.
    required = max(2, multi_cell_rows // 2)
    positions: list = []
    for index in range(3):
        candidates = [(count, key) for key, count in counts.items() if key[0] == index]
        if not candidates:
            break
        count, key = max(candidates)
        if count < (2 if index < 2 else required):
            break
        positions.append(key[1] * _LEGEND_COLUMN_TOLERANCE_PT)
    return positions


def _column_of(cell: dict, positions: list):
    for index, position in enumerate(positions):
        if abs(cell["bbox"][0] - position) <= _LEGEND_COLUMN_TOLERANCE_PT:
            return index
    return None


def _legend_entry(sheet_id, legend_number, entry_number, symbol, description, quantity, row):
    """One legend entry.

    ``row`` must contain only the cells this entry was actually read from.
    Passing the whole printed line stretched the entry's box across whatever
    the drawing happened to print beside the legend, and the legend's area is
    what rooms and dimensions are excluded from — so a legend on the edge of a
    floor plan hid the dimensions printed next to it.
    """
    return {
        "entry_id": f"{sheet_id}-LEG{legend_number:02d}-{entry_number:02d}",
        "symbol": symbol,
        "description": description,
        "quantity": quantity,
        "bbox": [
            min(cell["bbox"][0] for cell in row),
            min(cell["bbox"][1] for cell in row),
            max(cell["bbox"][2] for cell in row),
            max(cell["bbox"][3] for cell in row),
        ],
        "extraction_method": row[0]["extraction_method"],
        "confidence": round(row[0]["confidence"] * 0.85, 3),
    }


def detect_legends(lines: list, config: dict, sheet_id: str) -> list:
    """Captures a legend's printed entries, not just the word 'LEGEND'.

    A legend is read as the small table it is: a column of symbols or
    abbreviations, a column of meanings, and sometimes a third column of
    quantities. Reading it as loose text instead produced three measured
    faults on the supplied set - a quantity appended to the meaning
    ('1 GANG SWITCH 5'), a wrapped meaning split off into a second entry with
    no symbol, and the entries of an adjacent legend absorbed into the one
    above it.

    Only what this drawing prints is recorded; no universal construction
    symbol library is assumed, which is explicitly out of scope for Week 1.
    """
    legend_config = config.get("legends", {})
    title_keywords = [normalize_label(k) for k in legend_config.get("title_keywords", [])]
    max_entry_length = legend_config.get("max_entry_length", 120)
    header_keywords = config.get("schedules", {}).get("column_header_keywords", {})
    if not title_keywords:
        return []

    captions = _legend_captions(lines, title_keywords)
    if not captions:
        return []

    legends: list = []
    for caption_index, caption in enumerate(captions):
        caption_bbox = caption["bbox"]
        caption_height = bbox_height(caption_bbox) or 10.0
        # A legend runs from its own heading down to the next heading.
        next_caption_y = (
            captions[caption_index + 1]["bbox"][1]
            if caption_index + 1 < len(captions)
            else None
        )

        band_left = caption_bbox[0] - caption_height * 2.0
        band_right = caption_bbox[0] + max(caption_height * 30, 240.0)
        candidates = [
            other
            for other in lines
            if other is not caption
            and other["bbox"][1] >= caption_bbox[3] - caption_height * 0.5
            and (next_caption_y is None or other["bbox"][1] < next_caption_y - 0.5)
            and band_left <= other["bbox"][0] <= band_right
            and other["text"].strip()
            and len(other["text"].strip()) <= max_entry_length
        ]
        if not candidates:
            continue

        rows = group_into_rows(candidates)
        positions = _column_positions(rows)
        legend_number = len(legends) + 1

        entries: list = []
        last_row_bottom = None
        for row in rows:
            cells = [cell for cell in row if cell["text"].strip()]
            if not cells:
                continue
            row_top = min(cell["bbox"][1] for cell in cells)
            row_height = max(bbox_height(cell["bbox"]) for cell in cells) or 8.0
            if last_row_bottom is not None and row_top - last_row_bottom > row_height * 4:
                break  # the list has ended

            if not positions:
                # A legend with no symbol column: the swatches are drawn, and
                # only the meanings are printed as text.
                text = " ".join(cell["text"].strip() for cell in cells)
                if entries and (text.startswith("(") or text[:1].islower()):
                    entries[-1]["description"] = f"{entries[-1]['description']} {text}".strip()
                    continue
                entries.append(
                    _legend_entry(sheet_id, legend_number, len(entries) + 1, None, text, None, row)
                )
                last_row_bottom = max(cell["bbox"][3] for cell in cells)
                if len(entries) >= 80:
                    break
                continue

            # Cells outside the legend's own columns belong to the drawing
            # behind it, not to the legend — these sheets print survey levels
            # and callouts across the same rows. They are dropped rather than
            # ending the list, which is what previously cut three entries off
            # the site plan's abbreviation list.
            assigned: dict = {}
            for cell in cells:
                column = _column_of(cell, positions)
                if column is not None:
                    assigned.setdefault(column, []).append(cell)
            if not assigned:
                continue  # nothing of this row belongs to the legend

            # A legend table may print its own header row ('ID | ITEM | No.').
            # Every cell matching a known column-header word identifies it.
            if all(
                _role_of(cell["text"], header_keywords) is not None
                for cells_in_column in assigned.values()
                for cell in cells_in_column
            ):
                continue

            symbol_cells = assigned.get(0, [])
            description_cells = assigned.get(1, [])
            extra_cells = assigned.get(2, [])

            if not description_cells:
                if not symbol_cells:
                    # Nothing of this row is in the legend's own symbol or
                    # meaning column, so it is the drawing showing through the
                    # same band — a dimension, a level, a callout. It is passed
                    # over, not treated as the end of the list, which is what
                    # cut an abbreviations list off at 13 of its 28 entries.
                    continue
                # A cell in the symbol column with no meaning beside it starts a
                # different list rather than continuing this one.
                if entries:
                    break
                continue

            description = " ".join(cell["text"].strip() for cell in description_cells)
            if not symbol_cells:
                # A meaning that wrapped onto a second line belongs to the entry
                # above it.
                if entries:
                    entries[-1]["description"] = (
                        f"{entries[-1]['description']} {description}".strip()
                    )
                    box = entries[-1]["bbox"]
                    box[2] = max(box[2], max(c["bbox"][2] for c in description_cells))
                    box[3] = max(box[3], max(c["bbox"][3] for c in description_cells))
                    last_row_bottom = max(cell["bbox"][3] for cell in description_cells)
                continue

            symbol = " ".join(cell["text"].strip() for cell in symbol_cells)
            if len(symbol) > _MAX_SYMBOL_LENGTH:
                symbol, description = None, f"{symbol} {description}".strip()

            quantity = None
            if extra_cells:
                extra = " ".join(cell["text"].strip() for cell in extra_cells)
                if re.fullmatch(r"\d{1,4}", extra):
                    quantity = extra
                else:
                    description = f"{description} {extra}".strip()

            used = symbol_cells + description_cells + extra_cells
            entries.append(
                _legend_entry(
                    sheet_id, legend_number, len(entries) + 1, symbol, description, quantity, used
                )
            )
            last_row_bottom = max(cell["bbox"][3] for cell in used)
            if len(entries) >= 80:
                break

        if entries:
            legends.append(
                {
                    "legend_id": f"{sheet_id}-LEG{legend_number:02d}",
                    "caption": caption["text"].strip().rstrip(":"),
                    "caption_bbox": caption_bbox,
                    "entry_count": len(entries),
                    "entries": entries,
                }
            )
    return legends
