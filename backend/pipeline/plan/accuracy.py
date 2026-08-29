"""Day 4 — measured accuracy against a manually checked reference.

The Handbook is explicit that this file is the only thing that can turn output
into an accuracy claim:

> Accuracy is not declared by confidence or visual quality. It is calculated
> against approved ground truth and separated by output type.

The in-document checks built on Day 3 — the drawing index, the dimension
arithmetic, the schedule geometry — are real, but they can only test what a
drawing states twice. They cannot tell anyone that a room was missed entirely,
that an opening was never found, or that a wall length is wrong, because the
drawing does not repeat those. That is what ``tests/ground_truth.csv`` is for,
and it is the reason a reviewer's memory of having checked the screen is not a
substitute: **recall needs a list of what should have been found**, and the
output can only ever show what was.

The file records one expected item per row. Every row is compared with what the
run produced and lands in exactly one of four buckets:

* **matched**    — found, and the value agrees within tolerance.
* **wrong**      — found, but the value disagrees.
* **missed**     — expected and not found. This is what drives recall, and it
                   is the number no amount of looking at the screen can give.
* **unexpected** — found but not in the reference. This drives precision.

Metrics follow the Handbook's own formulas (Section 7) so the numbers here mean
the same thing as the numbers in the acceptance gates.
"""

import csv
import re
from pathlib import Path

from app.logging_setup import get_logger

logger = get_logger()

# The kinds of thing a ground-truth row can describe.
ITEM_TYPES = ("sheet_field", "room", "opening", "wall", "dimension")


def _normalise(value) -> str:
    return " ".join(str(value or "").strip().upper().split())


_UNIT_SUFFIX = re.compile(r"\s*(MM|M2|M²|SQM|M)\s*$", re.IGNORECASE)


def _as_number(value):
    """A figure, however a person wrote it.

    The checking sheet shows measurements the way a drawing prints them —
    "6,225 mm", "13.73 m2" — and whoever fills it in will copy that style, or
    type a bare number, or add their own unit. All of them mean the same
    measurement, so all of them parse.
    """
    text = _UNIT_SUFFIX.sub("", str(value or "").strip())
    try:
        return float(text.replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def load_ground_truth(path: Path) -> list:
    """Reads the reference file.

    The file opens with instructions for whoever fills it in, so the header is
    found by name rather than assumed to be the first line — a reference file a
    person cannot read is a reference file nobody maintains.
    """
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except Exception as e:
        logger.exception(f"could not read ground truth at {path}: {e}")
        return []

    header_index = next(
        (
            i
            for i, line in enumerate(lines)
            if line.lstrip().lower().startswith(("item_type,", "what,"))
        ),
        None,
    )
    if header_index is None:
        logger.warning(f"ground truth at {path} has no header row")
        return []

    rows = []
    for row in csv.DictReader(lines[header_index:]):
        if not row:
            continue
        row = {key: (value or "").strip() for key, value in row.items() if key}
        row = _in_plain_words(row)
        item_type = row.get("item_type", "").lower()
        if not item_type or item_type.startswith("#") or item_type not in ITEM_TYPES:
            continue
        rows.append(row)
    logger.info(f"ground truth: {len(rows)} reference rows loaded from {path.name}")
    return rows


# The file a person fills in asks one question — "is this right?" — instead of
# asking them to understand expected values, units and tolerances. These are
# the columns it uses, and this is how they become the fields the comparison
# needs. The original column names still load, so an older file keeps working.
_PLAIN_WORDS = {
    "what": "item_type",
    "on_sheet": "sheet",
    "which_one": "identifier",
    "your_name": "checked_by",
}


def _in_plain_words(row: dict) -> dict:
    """Accepts the plain-words file as well as the original column names."""
    if "item_type" in row:
        return row

    translated = {_PLAIN_WORDS.get(key, key): value for key, value in row.items()}
    answer = translated.pop("is_it_correct", "").strip().upper()
    computer_read = translated.get("computer_read", "")
    correction = translated.pop("if_no_what_is_correct", "").strip()

    if answer.startswith("Y"):
        # Confirmed as printed. An item with nothing to measure — a room with
        # no size printed beside it — is confirmed by its presence alone.
        translated["expected_value"] = computer_read
    elif answer.startswith("N"):
        # A correction is the most valuable row in the file: it is the only
        # thing that can prove the reader wrong.
        translated["expected_value"] = correction
    else:
        translated["expected_value"] = ""
        translated["checked_by"] = ""  # unanswered rows are not counted

    translated["reader_proposed"] = computer_read
    return translated


_FOR_PLAN_RE = re.compile(r"^#\s*FOR PLAN:\s*(.+?)\s*$", re.IGNORECASE)


def plan_file_named_in(path: Path):
    """Which uploaded plan this checking sheet was written for.

    A checking sheet describes one plan's sheets. Applied to a different plan
    it is not merely useless, it is actively wrong: both documents have a
    "Page 1", so every room on the first plan's page 1 was reported as
    *missing* from the second plan's page 1 — 28 invented misses and a score
    that meant nothing. So the sheet records the plan it belongs to, and is
    only ever used for that plan.
    """
    if not path.is_file():
        return None
    try:
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            match = _FOR_PLAN_RE.match(line.strip().strip('"'))
            if match:
                return match.group(1).strip()
            if not line.lstrip().startswith("#") and line.strip():
                break  # past the header comments
    except Exception as e:
        logger.exception(f"could not read the plan name from {path}: {e}")
    return None


def written_for_another_plan(named: str, document: str) -> dict:
    """The result when the checking sheet belongs to a different plan."""
    return {
        "reference_rows": 0,
        "verified_rows": 0,
        "unverified_rows": 0,
        "rows_for_other_documents": 0,
        "measured": False,
        "note": (
            f"No accuracy has been measured for this plan. The checking sheet on file was "
            f"written for “{named}”, and a checking sheet only describes the plan it "
            f"was written for. Download the Checking sheet for “{document}”, answer its "
            f"rows, and save it as tests/ground_truth.csv to measure this plan."
        ),
        "per_item_type": {},
        "wall_length_variance_pct": {"worst": None, "average": None},
        "rows": [],
        "unexpected": [],
    }


def _page_for(pages: list, sheet: str):
    """A ground-truth row names a sheet by its drawing number or its page."""
    wanted = _normalise(sheet)
    for page in pages:
        printed = _normalise(page["title_block"]["sheet_number"]["value"])
        if printed and printed == wanted:
            return page
        if _normalise(page["sheet_id"]) == wanted:
            return page
        if wanted == f"PAGE {page['page_number']}" or wanted == str(page["page_number"]):
            return page
    return None


def _extracted_items(page: dict, item_type: str) -> list:
    """(key, value, record) for everything of one kind found on a sheet."""
    if item_type == "room":
        return [(_normalise(r["name"]), r.get("floor_area_m2"), r) for r in page["rooms"]]
    if item_type == "opening":
        # An opening the drawing does not label has no mark, so its own
        # reference identifies it. Without this every unlabelled opening on a
        # sheet had the same empty key and they all matched the first one.
        return [
            (_normalise(o["mark"] or o["opening_id"]), o.get("width_mm"), o)
            for o in page.get("openings", [])
        ]
    if item_type == "wall":
        return [(w["wall_id"], w.get("length_mm"), w) for w in page.get("walls", [])]
    if item_type == "dimension":
        return [(_normalise(d["text"]), d.get("value_mm"), d) for d in page["dimensions"]]
    if item_type == "sheet_field":
        return [
            (_normalise(name), field.get("value"), field)
            for name, field in page["title_block"].items()
        ]
    return []


def _wall_match(expected_length, walls: list, tolerance_pct: float):
    """Walls have no printed name, so a reference row gives a length and the
    closest candidate of that length is the match."""
    target = _as_number(expected_length)
    if target is None or not walls:
        return None, None
    best = min(walls, key=lambda w: abs((w.get("length_mm") or 0) - target))
    actual = best.get("length_mm") or 0
    variance = abs(actual - target) / target * 100.0 if target else None
    if variance is not None and variance <= tolerance_pct:
        return best, variance
    return None, variance


def compare(pages: list, ground_truth: list, config: dict) -> dict:
    """Compares one run against the reference file, row by row."""
    settings = config.get("accuracy", {})
    default_tolerance = float(settings.get("default_tolerance_pct", 2.0))
    wall_tolerance = float(settings.get("wall_length_tolerance_pct", 5.0))

    results = []
    matched_records: dict = {}

    for row in ground_truth:
        item_type = row["item_type"].lower()
        sheet = row.get("sheet", "")
        page = _page_for(pages, sheet)
        expected = row.get("expected_value", "")
        identifier = row.get("identifier", "")
        tolerance = _as_number(row.get("tolerance_pct")) or (
            wall_tolerance if item_type == "wall" else default_tolerance
        )

        outcome = {
            "item_type": item_type,
            "sheet": sheet,
            "identifier": identifier,
            "expected_value": expected,
            "found_value": None,
            "variance_pct": None,
            "tolerance_pct": tolerance,
            "result": "missed",
            "note": "",
            "checked_by": row.get("checked_by", ""),
        }

        if page is None:
            # The reference describes a sheet this document does not contain —
            # a different plan set, or a sheet not supplied. That says nothing
            # about how well this document was read, so it is set aside rather
            # than counted as something the reader missed.
            outcome["result"] = "not_applicable"
            outcome["note"] = f"Sheet '{sheet}' is not in this document."
            results.append(outcome)
            continue

        items = _extracted_items(page, item_type)
        matched_records.setdefault(id(page), {}).setdefault(item_type, set())

        if item_type == "wall":
            walls = [record for _, _, record in items]
            best, variance = _wall_match(expected, walls, tolerance)
            if best is not None:
                outcome.update(
                    {
                        "found_value": best["length_mm"],
                        "variance_pct": round(variance, 2) if variance is not None else None,
                        "result": "matched",
                        "note": f"Matched candidate wall {best['wall_id']}.",
                    }
                )
                matched_records[id(page)]["wall"].add(best["wall_id"])
            else:
                outcome["note"] = (
                    "No candidate wall of this length was found"
                    + (f"; closest was {variance:.1f}% away." if variance is not None else ".")
                )
            results.append(outcome)
            continue

        found = next((item for item in items if item[0] == _normalise(identifier)), None)
        if found is None:
            outcome["note"] = f"'{identifier}' was not found on this sheet."
            results.append(outcome)
            continue

        key, value, record = found
        matched_records[id(page)][item_type].add(key)
        outcome["found_value"] = value

        expected_number = _as_number(expected)
        found_number = _as_number(value)
        if expected == "":
            outcome["result"] = "matched"
            outcome["note"] = "Presence only — no value was given to check."
        elif expected_number is not None and found_number is not None:
            variance = (
                abs(found_number - expected_number) / expected_number * 100.0
                if expected_number
                else (0.0 if found_number == 0 else 100.0)
            )
            outcome["variance_pct"] = round(variance, 2)
            outcome["result"] = "matched" if variance <= tolerance else "wrong"
            if outcome["result"] == "wrong":
                outcome["note"] = (
                    f"Expected {expected}, found {value} — {variance:.1f}% apart against a "
                    f"{tolerance}% tolerance."
                )
        else:
            same = _normalise(expected) == _normalise(value)
            outcome["result"] = "matched" if same else "wrong"
            if not same:
                outcome["note"] = f"Expected '{expected}', found '{value}'."
        results.append(outcome)

    # Anything found that the reference does not list — this is what precision
    # is calculated from, and it only exists because the reference exists.
    unexpected = []
    referenced_types = {row["item_type"].lower() for row in ground_truth}
    referenced_sheets = {_normalise(row.get("sheet", "")) for row in ground_truth}
    for page in pages:
        printed = _normalise(page["title_block"]["sheet_number"]["value"])
        if printed not in referenced_sheets and _normalise(page["sheet_id"]) not in referenced_sheets:
            continue
        for item_type in referenced_types:
            if item_type in ("sheet_field", "wall", "dimension"):
                continue  # only counted for the named item kinds
            seen = matched_records.get(id(page), {}).get(item_type, set())
            for key, value, _record in _extracted_items(page, item_type):
                if key not in seen:
                    unexpected.append(
                        {"item_type": item_type, "sheet": page["sheet_id"], "identifier": key}
                    )

    return _summarise(results, unexpected)


def _summarise(results: list, unexpected: list) -> dict:
    """Accuracy is only claimed for rows a person has actually checked.

    A reference file can be seeded from a run to save transcription, which is
    convenient and also circular: comparing a run against numbers taken from
    that same run will always score 100% and prove nothing. So each row is
    counted as verified only once someone has put their name in `checked_by`,
    and the headline figures are computed from those rows alone. Until then the
    report says plainly that nothing has been verified rather than showing a
    flattering score.
    """
    applicable = [r for r in results if r["result"] != "not_applicable"]
    not_applicable = len(results) - len(applicable)
    verified_rows = [r for r in applicable if r["checked_by"]]
    unverified = len(applicable) - len(verified_rows)
    results = verified_rows

    def count(item_type, outcome):
        return sum(1 for r in results if r["item_type"] == item_type and r["result"] == outcome)

    per_type = {}
    for item_type in sorted({r["item_type"] for r in results}):
        expected_total = sum(1 for r in results if r["item_type"] == item_type)
        matched = count(item_type, "matched")
        wrong = count(item_type, "wrong")
        missed = count(item_type, "missed")
        extra = sum(1 for u in unexpected if u["item_type"] == item_type)
        predicted = matched + wrong + extra
        per_type[item_type] = {
            "expected": expected_total,
            "matched": matched,
            "wrong": wrong,
            "missed": missed,
            "unexpected": extra,
            # Handbook Section 7 formulas.
            "recall_pct": round(matched / expected_total * 100.0, 1) if expected_total else None,
            "precision_pct": round(matched / predicted * 100.0, 1) if predicted else None,
        }

    variances = [
        r["variance_pct"]
        for r in results
        if r["item_type"] == "wall" and r["variance_pct"] is not None
    ]
    return {
        "reference_rows": len(results) + unverified + not_applicable,
        "verified_rows": len(results),
        "unverified_rows": unverified,
        "rows_for_other_documents": not_applicable,
        "measured": bool(results),
        "note": (
            None
            if results
            else (
                f"{unverified} reference row(s) apply to this document but none has been "
                "checked by a person yet, so no accuracy has been measured. Confirm each "
                "row against the drawing and put your name in the checked_by column."
                if unverified
                else (
                    f"The reference file describes a different plan set "
                    f"({not_applicable} row(s)), so there is nothing to measure here."
                )
            )
        ),
        "per_item_type": per_type,
        "wall_length_variance_pct": {
            "worst": round(max(variances), 2) if variances else None,
            "average": round(sum(variances) / len(variances), 2) if variances else None,
        },
        "rows": results,
        "unexpected": unexpected,
    }


def write_accuracy_report(path: Path, run_id: str, report: dict) -> None:
    """One row per reference item, so every number in the summary can be traced
    back to the exact comparison that produced it."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "run_id", "item_type", "sheet", "identifier", "expected_value",
                "found_value", "variance_pct", "tolerance_pct", "result",
                "counted_towards_accuracy", "note", "checked_by",
            ]
        )
        for row in report.get("rows", []):
            writer.writerow(
                [
                    run_id, row["item_type"], row["sheet"], row["identifier"],
                    row["expected_value"],
                    "" if row["found_value"] is None else row["found_value"],
                    "" if row["variance_pct"] is None else row["variance_pct"],
                    row["tolerance_pct"], row["result"],
                    "yes" if row["checked_by"] else "no - not checked by a person yet",
                    row["note"], row["checked_by"],
                ]
            )
        for row in report.get("unexpected", []):
            writer.writerow(
                [
                    run_id, row["item_type"], row["sheet"], row["identifier"], "", "", "", "",
                    "unexpected", "yes",
                    "Found by the reader but not listed in the reference file.", "",
                ]
            )

# --- The template a reviewer fills in -------------------------------------

TEMPLATE_COLUMNS = [
    "what",
    "on_sheet",
    "which_one",
    "computer_read",
    "where_to_look",
    "is_it_correct",
    "if_no_what_is_correct",
    "your_name",
    "notes",
]

_TEMPLATE_HELP = [
    ["# CHECKING SHEET - open the drawing beside this file and answer one question per row."],
    ["#"],
    ["# YOU FILL IN TWO COLUMNS:  is_it_correct   and   your_name"],
    ["#   Type YES if the drawing agrees with what the computer read."],
    ["#   Type NO  if it does not, and put the right answer in if_no_what_is_correct."],
    ["#   Put your name in your_name. A row with no name is not counted at all."],
    ["#"],
    ["# EXAMPLE"],
    ["#   what   on_sheet  which_one  computer_read  where_to_look        is_it_correct  if_no  your_name"],
    ["#   room   Page 1    KITCHEN    13.73          middle left          YES                   S. Sadiq"],
    ["#     -> You looked at the kitchen on the drawing. It says 3,905 x 3,515, which is"],
    ["#        13.73 square metres. It matches, so YES."],
    ["#"],
    ["#   dim    Page 1    1,910      1910           bottom centre        NO             1,190  S. Sadiq"],
    ["#     -> The drawing actually prints 1,190. The computer read it wrong. That is a"],
    ["#        real catch, and it is exactly what this file is for."],
    ["#"],
    ["# TWO THINGS ONLY YOU CAN DO"],
    ["#   ADD a row for anything on the drawing the computer did not find. Leave"],
    ["#     computer_read empty and put the right answer in if_no_what_is_correct."],
    ["#     The screen can only show what WAS found, never what was missed - this is the"],
    ["#     only way a missed room or door can ever be counted."],
    ["#   DELETE a row for anything the computer invented that is not on the drawing."],
    ["#"],
    ["# 'what' must be one of: sheet_field, room, opening, wall, dimension"],
    ["#"],
]


def _as_printed(value, unit: str) -> str:
    """The value written the way a person reads it off a drawing.

    A dimension printed "6,225" was being shown as "6225", and a floor area as
    a bare "13.73". Whoever is checking then has to work out whether they are
    even the same kind of thing. A measurement is written with its thousands
    separator and its unit, so that comparing it with the drawing is a glance
    rather than a translation.
    """
    if value is None or value == "":
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if unit == "mm":
        return f"{number:,.0f} mm"
    if unit == "m2":
        return f"{number:,.2f} m2"
    return str(value)


def _proposal(item_type, identifier, value, unit, where, read_from, tolerance="", note=""):
    return {
        "what": item_type,
        "which_one": identifier,
        "computer_read": _as_printed(value, unit),
        "where_to_look": where,
        "is_it_correct": "",
        "if_no_what_is_correct": "",
        "your_name": "",
        "notes": note,
    }


def _corner(bbox, page_width, page_height):
    """Where on the sheet something sits, in words a person can act on."""
    if not bbox:
        return "not on the sheet"
    x = (bbox[0] + bbox[2]) / 2.0 / max(page_width, 1)
    y = (bbox[1] + bbox[3]) / 2.0 / max(page_height, 1)
    vertical = "top" if y < 0.33 else ("middle" if y < 0.66 else "bottom")
    horizontal = "left" if x < 0.33 else ("centre" if x < 0.66 else "right")
    return f"{vertical} {horizontal} of the sheet"


def build_ground_truth_template(page: dict, page_width: float, page_height: float) -> list:
    """Proposed reference rows for one sheet, each saying where it came from."""
    fields = page["title_block"]
    _ = fields["revision"]["value"]  # kept for context; not a column any more
    sheet = fields["sheet_number"]["value"] or f"Page {page['page_number']}"
    rows = []

    for name in ("sheet_number", "sheet_title", "scale", "revision", "discipline"):
        field = fields[name]
        rows.append(
            _proposal(
                "sheet_field",
                name,
                field["value"],
                "",
                _corner(field["source_bbox"], page_width, page_height),
                (field["technique"] or "not found").replace("_", " "),
            )
        )

    for room in sorted(page["rooms"], key=lambda r: r["name"]):
        rows.append(
            _proposal(
                "room",
                room["name"],
                room["floor_area_m2"],
                "m2" if room["floor_area_m2"] is not None else "",
                _corner(room["bbox"], page_width, page_height),
                room["detection_method"].replace("_", " "),
                note="" if room["floor_area_m2"] is not None
                else "No size printed beside this room, so just check the name is right.",
            )
        )

    for opening in sorted(
        page.get("openings", []), key=lambda o: o["mark"] or o["opening_id"]
    ):
        rows.append(
            _proposal(
                "opening",
                opening["mark"] or opening["opening_id"],
                opening["width_mm"],
                "mm" if opening["width_mm"] is not None else "",
                _corner(opening["source_bbox"], page_width, page_height),
                f"schedule on sheet {opening['schedule_sheet']}"
                if opening["schedule_sheet"]
                else "mark on the drawing; no schedule row found",
                note="Check the width against the schedule or the drawing." if opening["width_mm"] else "",
            )
        )

    for wall in sorted(page.get("walls", []), key=lambda w: -w["length_mm"])[:8]:
        rows.append(
            _proposal(
                "wall",
                f"{'horizontal' if wall['runs_along'] == 'x' else 'vertical'} wall, "
                f"about {wall['length_mm']:.0f} mm",
                round(wall["length_mm"]),
                "mm",
                _corner(wall["bbox"], page_width, page_height),
                f"two lines {wall['thickness_mm']:.0f} mm apart",
                tolerance="5",
                note="Scale this wall off the drawing, or check it against the dimension printed along it.",
            )
        )

    seen = set()
    for dimension in page["dimensions"]:
        if dimension["kind"] != "linear" or dimension["value_mm"] is None:
            continue
        if dimension["text"] in seen:
            continue
        seen.add(dimension["text"])
        rows.append(
            _proposal(
                "dimension",
                dimension["text"],
                round(dimension["value_mm"]),
                "mm",
                _corner(dimension["bbox"], page_width, page_height),
                "printed on the drawing",
                tolerance="0",
                note="Should be exactly the figure printed on the drawing.",
            )
        )
        if len(seen) >= 12:
            break

    for row in rows:
        row["on_sheet"] = sheet
    return rows


def write_ground_truth_template(path: Path, rows: list, plan_file: str = "") -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if plan_file:
            # Names the plan this sheet describes, so it can never be scored
            # against a different upload.
            writer.writerow([f"# FOR PLAN: {plan_file}"])
            writer.writerow(["#"])
        for line in _TEMPLATE_HELP:
            writer.writerow(line)
        writer.writerow(TEMPLATE_COLUMNS)
        for row in rows:
            writer.writerow([row.get(column, "") for column in TEMPLATE_COLUMNS])
