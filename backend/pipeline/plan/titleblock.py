"""Day 3 — title-block field extraction.

A title block is a grid of label/value pairs. This module finds the grid,
reads each field from it, and refuses any value that does not survive its
field's validator.

Order of evidence, strongest first, recorded on every field as ``technique``
so the interface can show *how* each value was obtained:

1.  ``label_value_below`` / ``label_value_right`` — the sheet printed a label
    ('Scale:', 'REV NO') and the value sits in the adjacent cell. This is the
    drawing telling us directly what the value means, so it is preferred
    everywhere it exists.
2.  ``inline_label`` — label and value share one printed line
    ('SCALE: 1:100 @ A3'), which the second supplied plan uses.
3.  ``title_keyword`` — no title label is printed, but a line matches a known
    drawing-type phrase ('FLOOR PLAN', 'BUILDING ELEVATIONS').
4.  ``largest_text_in_title_block`` — last resort for the sheet title only: a
    title block's largest type is its title. Confined to the detected title-
    block region, so a construction note in the drawing area can never win.
5.  ``derived_from_page_order`` — the sheet's position in the supplied file,
    used for the sheet position/identifier. Deterministic and always
    available, and marked as derived rather than printed.

Nothing here gates on a fixed page position. The two supplied plan sets place
their title blocks differently and arrange label and value differently
(stacked versus side by side); the region is found from where the labels
actually are on the page being read.
"""

import re
from functools import lru_cache

from app.logging_setup import get_logger
from pipeline.plan import validators
from pipeline.plan.layout import (
    drawn_box_around,
    enclosing_cell,
    find_label_lines,
    joined_text,
    value_candidates,
)
from pipeline.plan.textmodel import (
    bbox_center,
    bbox_height,
    is_placeholder,
    normalize_label,
)

logger = get_logger()

# Two title-block labels belong to the same block if they sit within this
# fraction of the page's smaller dimension of each other. Expressed relative
# to the page so it holds for A1 and A3 sheets alike.
_LABEL_CLUSTER_RADIUS_RATIO = 0.14

# Confidence assigned per technique. A printed label beside the value is the
# only signal the drawing gives explicitly, so it scores highest; a value that
# is additionally confirmed by the cover-sheet drawing index is raised to
# certainty by the cross-check stage, not here.
_TECHNIQUE_CONFIDENCE = {
    "label_value_below": 0.95,
    "label_value_right": 0.95,
    "inline_label": 0.9,
    "title_keyword": 0.85,
    "largest_text_in_title_block": 0.6,
    # Read from where it sits on the sheet rather than from anything printed
    # to say what it is. Deliberately below the "review" threshold, so it can
    # never present itself as a confirmed value.
    "largest_text_in_sheet_edge_band": 0.45,
    "derived_from_page_order": 0.99,
}

# The bands a title block can be drawn in. Offices put it up the right edge,
# along the bottom, up the left edge or across the top, so all four are
# looked in. Fractions of the sheet, so they hold for A0 and A4 alike. Used
# only when the sheet prints no title-block labels at all.
_EDGE_BAND_WIDTH_RATIO = 0.30
_EDGE_BAND_HEIGHT_RATIO = 0.20

FIELD_NAMES = (
    "sheet_number",
    "sheet_title",
    "discipline",
    "revision",
    "scale",
    "sheet_position",
    "project_number",
    "project_name",
    "client",
    "issue_date",
    "drawn_by",
    "checked_by",
)


def empty_field() -> dict:
    return {
        "value": None,
        "raw_text": None,
        "confidence": 0.0,
        "confidence_band": "low",
        "source_bbox": None,
        "extraction_method": "none",
        "technique": None,
        "label_matched": None,
        "note": None,
        "conflicts": [],
        "verified_against_index": None,
        "review_status": "unresolved",
    }


def confidence_band(confidence: float, thresholds: dict) -> str:
    if confidence >= thresholds.get("review", 0.75):
        return "high"
    if confidence >= thresholds.get("low", 0.5):
        return "review"
    return "low"


def _build_field(
    value: str,
    raw_text: str,
    technique: str,
    source_line_or_bbox,
    extraction_method: str,
    base_confidence: float,
    thresholds: dict,
    label_matched=None,
    note=None,
    conflicts=None,
) -> dict:
    confidence = round(min(base_confidence * _TECHNIQUE_CONFIDENCE.get(technique, 0.5), 1.0), 3)
    band = confidence_band(confidence, thresholds)
    bbox = source_line_or_bbox
    if isinstance(source_line_or_bbox, dict):
        bbox = source_line_or_bbox["bbox"]
    return {
        "value": value,
        "raw_text": raw_text,
        "confidence": confidence,
        "confidence_band": band,
        "source_bbox": [round(float(v), 2) for v in bbox] if bbox else None,
        "extraction_method": extraction_method,
        "technique": technique,
        "label_matched": label_matched,
        "note": note,
        "conflicts": list(conflicts or []),
        "verified_against_index": None,
        "review_status": "confirmed" if band == "high" else "needs_review",
    }


# --- Title-block region ---------------------------------------------------


def _all_configured_labels(field_labels: dict) -> set:
    labels = set()
    for spec in field_labels.values():
        for group in ("page_specific", "project_wide"):
            for label in spec.get(group, []):
                labels.add(normalize_label(label))
    return labels


# A ruled frame bigger than this share of the sheet is the drawing's own
# border, not the title block inside it.
_MAX_FRAME_SHEET_SHARE = 0.45


def find_title_block_region(
    lines: list,
    field_labels: dict,
    page_width: float,
    page_height: float,
    rulings: dict | None = None,
):
    """The rectangle covering the densest cluster of printed title-block labels.

    Real drawings put their title block in different corners, and a cover
    sheet's drawing index reuses the same words ('SCALE', 'REV') as column
    headers far away from it. Clustering the labels that actually appear finds
    the block wherever it is and separates it from those reused headers,
    without assuming any page position.
    """
    known = _all_configured_labels(field_labels)
    label_lines = [ln for ln in lines if normalize_label(ln["text"]) in known]
    if not label_lines:
        return None

    radius = min(page_width, page_height) * _LABEL_CLUSTER_RADIUS_RATIO
    clusters: list = []
    for line in label_lines:
        cx, cy = bbox_center(line["bbox"])
        joined = None
        for cluster in clusters:
            for member in cluster:
                mx, my = bbox_center(member["bbox"])
                if abs(mx - cx) <= radius and abs(my - cy) <= radius:
                    joined = cluster
                    break
            if joined is not None:
                break
        if joined is None:
            clusters.append([line])
        else:
            joined.append(line)

    # Merge clusters that ended up adjacent through a chain of members.
    merged = True
    while merged:
        merged = False
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                if any(
                    abs(bbox_center(a["bbox"])[0] - bbox_center(b["bbox"])[0]) <= radius
                    and abs(bbox_center(a["bbox"])[1] - bbox_center(b["bbox"])[1]) <= radius
                    for a in clusters[i]
                    for b in clusters[j]
                ):
                    clusters[i].extend(clusters[j])
                    del clusters[j]
                    merged = True
                    break
            if merged:
                break

    best = max(
        clusters,
        key=lambda c: (len({normalize_label(ln["text"]) for ln in c}), -min(ln["bbox"][1] for ln in c)),
    )

    # **The office drew where its title block ends; use that.** A rectangle
    # padded around the labels is a guess, and on a sheet whose title block
    # sits partway along an edge - with the drawing wrapped around it rather
    # than only beside it - the guess spills into the plan and a field's value
    # comes back with a room name attached. The ruled frame enclosing the
    # labels is the office's own answer, and it is right wherever on the sheet
    # the block was placed.
    rulings = rulings or {"h": [], "v": []}
    sheet_area = max(page_width * page_height, 1.0)

    # The box the office actually drew around these labels. Tried for every
    # group of labels worth trying - all of them together, then each cluster
    # on its own - because a set may print the same words in two places, and
    # the box holding the most of them is the title block.
    groups = [label_lines] + sorted(clusters, key=len, reverse=True)
    best_frame = None
    best_inside = 0
    for group in groups:
        frame = drawn_box_around(
            [ln["bbox"] for ln in group],
            rulings,
            page_width,
            page_height,
            _MAX_FRAME_SHEET_SHARE,
        )
        if frame is None:
            continue
        inside = sum(1 for ln in label_lines if _inside(ln["bbox"], frame))
        if inside > best_inside:
            best_frame, best_inside = frame, inside
    if best_frame is not None:
        return best_frame

    x0 = min(ln["bbox"][0] for ln in best)
    y0 = min(ln["bbox"][1] for ln in best)
    x1 = max(ln["bbox"][2] for ln in best)
    y1 = max(ln["bbox"][3] for ln in best)
    # No frame was drawn around these labels. Pad instead, so the values that
    # belong to them fall inside the region too.
    pad_x = min(page_width, page_height) * 0.06
    pad_y = min(page_width, page_height) * 0.06
    return [
        max(0.0, x0 - pad_x),
        max(0.0, y0 - pad_y),
        min(page_width, x1 + pad_x),
        min(page_height, y1 + pad_y),
    ]


def _inside(bbox, region) -> bool:
    if region is None:
        return True
    cx, cy = bbox_center(bbox)
    return region[0] <= cx <= region[2] and region[1] <= cy <= region[3]


def _value_from_one_line(candidate: dict, label_line: dict, validator):
    """The first single line inside a cell that is a valid value for this field.

    Lines are tried nearest the label first, because that is the order a cell
    is read in. Returns (validator result, the raw text used, a note) or
    (None, "", None).
    """
    lines = candidate.get("lines") or []
    if len(lines) < 2:
        return None, "", None

    lx0, ly0, lx1, ly1 = label_line["bbox"]

    def distance(line):
        x0, y0, x1, y1 = line["bbox"]
        gap_x = max(lx0 - x1, x0 - lx1, 0.0)
        gap_y = max(ly0 - y1, y0 - ly1, 0.0)
        return (gap_x * gap_x + gap_y * gap_y) ** 0.5

    for line in sorted(lines, key=distance):
        text = (line.get("text") or "").strip()
        if not text:
            continue
        result = validator(text)
        if result is not None:
            return result, text, "read from one line of the cell, as its edge is not ruled"
    return None, "", None


def _near_its_label(label_line: dict, candidate: dict, max_gap_label_heights: float) -> bool:
    """Whether every line of a candidate was printed next to the label.

    Every line, not just the candidate's overall box: a candidate that starts
    in the title block and runs out into the drawing has swept up something
    that is not part of the field, and keeping only its first words would be
    guessing at where the real value stopped.

    The allowance is in label heights rather than points, so it means the same
    thing on an A4 sheet and on an A0 one.
    """
    lines = candidate.get("lines") or []
    if not lines:
        return False
    # The label's printed type size, not the height of its box. A label
    # printed sideways is as *tall* on the page as it is long, so its box
    # height is its length - which made the allowance five times too generous
    # on exactly the sideways title blocks this rule exists to fix.
    x0, y0, x1, y1 = label_line["bbox"]
    label_height = max(
        float(label_line.get("size") or 0.0) or min(abs(x1 - x0), abs(y1 - y0)),
        1.0,
    )
    allowed = label_height * max_gap_label_heights
    lx0, ly0, lx1, ly1 = label_line["bbox"]
    for line in lines:
        x0, y0, x1, y1 = line["bbox"]
        gap_x = max(lx0 - x1, x0 - lx1, 0.0)
        gap_y = max(ly0 - y1, y0 - ly1, 0.0)
        if (gap_x * gap_x + gap_y * gap_y) ** 0.5 > allowed:
            return False
    return True


# --- Inline "LABEL: value" on one printed line ----------------------------


@lru_cache(maxsize=2048)
def _inline_pattern(label: str, separator: str):
    return re.compile(
        r"^\s*" + re.escape(label) + r"\s*" + re.escape(separator) + r"\s*(.+)$",
        re.IGNORECASE,
    )


def _inline_label_match(line_text: str, label: str, separators: list):
    """Finds 'SCALE: 1:100 @ A3' — a label and its value on one printed line.

    Requires a separator between them, so a note sentence that merely starts
    with the word is not mistaken for a labelled field.
    """
    stripped = line_text.strip()
    # Every label on the sheet is tried against every line, which is tens of
    # thousands of comparisons per page. Almost all of them fail on the first
    # character, so the cheap test comes first and the pattern is only built
    # for a line that actually starts with the label.
    if not stripped[: len(label)].casefold() == label.casefold():
        return None
    for separator in separators or [":"]:
        pattern = _inline_pattern(label, separator)
        match = pattern.match(stripped)
        if match:
            remainder = match.group(1).strip()
            if remainder and not is_placeholder(remainder):
                return remainder
    return None


# --- Main extraction ------------------------------------------------------


def _field_from_labels(
    field_name: str,
    label_list: list,
    lines: list,
    rulings: dict,
    page_width: float,
    page_height: float,
    all_labels: set,
    region,
    thresholds: dict,
    separators: list,
    validator,
    max_value_gap_label_heights: float = 12.0,
):
    """Tries every configured label for one field and returns the first value
    that passes the field's validator, plus every other distinct value the
    other labels produced (reported as conflicts, never silently dropped)."""
    accepted = None
    other_values: list = []
    max_value_gap = float(max_value_gap_label_heights)

    for raw_label in label_list:
        wanted = normalize_label(raw_label)

        # A title-block field is read only from a label inside the title
        # block. Elsewhere on the sheet the same word means something else: a
        # drawing index heads its columns 'SCALES' and 'AMENDMENT', and those
        # describe the other sheets in the set. Falling through to them filled
        # a cover sheet's blank scale with a value belonging to five other
        # drawings. Where no title block was located at all, the whole sheet is
        # searched, because a label somewhere beats no value.
        instances = find_label_lines(lines, wanted)
        if region is not None:
            inside = [ln for ln in instances if _inside(ln["bbox"], region)]
            if inside:
                instances = inside
        instances.sort(key=lambda ln: -ln["bbox"][1])

        for label_line in instances:
            # One label offers several candidate cells (the one beside it, the
            # one beneath it). Those are ranked alternatives for the same
            # field, not competing claims: once one is accepted the rest are
            # simply weaker readings of the same label and must not be
            # reported as conflicting values. A genuine conflict is two
            # different *labels*, or the same label printed twice, giving
            # different answers — that is what the caller is told about.
            label_accepted_here = False
            for candidate in value_candidates(
                label_line, lines, rulings, page_width, page_height, all_labels
            ):
                if label_accepted_here:
                    break
                # **A value is printed next to its own label.** The value sits
                # in the cell beside or beneath the label - but "beside" runs
                # on until something stops it, and where the block is not
                # ruled into cells the search runs straight out into the
                # drawing. On a title strip printed up the edge of a sheet
                # that is exactly what happened: the drawing number came back
                # empty while the "checked by" field returned a run of legend
                # text from the middle of the plan.
                #
                # How far is "next to" cannot be a number of points, because a
                # sheet may be A4 or A0. It is measured in the label's own
                # printed height, which scales with the drawing.
                if not _near_its_label(label_line, candidate, max_value_gap):
                    continue
                raw = joined_text(candidate["lines"])
                result = validator(raw)
                extra_note = None
                if result is None:
                    # **Where a cell is not ruled, the search cannot see where
                    # it ends**, and two neighbouring cells arrive joined:
                    # a drawing number came back as "AR-104 GROUND FLOOR PLAN"
                    # and was rejected, leaving the field empty even though the
                    # drawing number was sitting in it. When the joined text is
                    # not a valid value, each line inside the cell is offered on
                    # its own, nearest the label first. The field's validator
                    # still decides, so this can only ever recover a value that
                    # was really printed - it cannot invent one.
                    result, raw, extra_note = _value_from_one_line(
                        candidate, label_line, validator
                    )
                if result is None:
                    continue
                value, note = result
                note = "; ".join(filter(None, [note, extra_note]))or None
                label_accepted_here = True
                if accepted is None:
                    accepted = _build_field(
                        value=value,
                        raw_text=raw,
                        technique=candidate["technique"],
                        source_line_or_bbox=candidate["bbox"],
                        extraction_method=candidate["lines"][0]["extraction_method"],
                        base_confidence=min(ln["confidence"] for ln in candidate["lines"]),
                        thresholds=thresholds,
                        label_matched=raw_label,
                        note=note,
                    )
                elif value != accepted["value"]:
                    other_values.append(value)

        # Inline form: the label and the value share one line.
        for line in lines:
            remainder = _inline_label_match(line["text"], wanted, separators)
            if remainder is None:
                continue
            result = validator(remainder)
            if result is None:
                continue
            value, note = result
            if accepted is None:
                accepted = _build_field(
                    value=value,
                    raw_text=line["text"],
                    technique="inline_label",
                    source_line_or_bbox=line,
                    extraction_method=line["extraction_method"],
                    base_confidence=line["confidence"],
                    thresholds=thresholds,
                    label_matched=raw_label,
                    note=note,
                )
            elif value != accepted["value"]:
                other_values.append(value)

    if accepted is not None and other_values:
        distinct = [v for v in dict.fromkeys(other_values) if v != accepted["value"]]
        if distinct:
            accepted["conflicts"] = distinct
            accepted["review_status"] = "needs_review"
            accepted["note"] = "; ".join(
                filter(None, [accepted.get("note"), f"other candidates on this sheet: {', '.join(distinct)}"])
            )
    return accepted


def _detect_sheet_title(
    lines: list,
    rulings: dict,
    page_width: float,
    page_height: float,
    all_labels: set,
    region,
    thresholds: dict,
    config: dict,
    consumed_bboxes: set,
):
    tb_config = config["title_block"]
    exclusions = tb_config.get("title_exclusion_keywords", [])
    separators = tb_config.get("inline_label_separators", [":"])

    def validator(text):
        return validators.validate_sheet_title(text, exclusion_keywords=[])

    labels = tb_config["field_labels"]["sheet_title"]["page_specific"]
    field = _field_from_labels(
        "sheet_title", labels, lines, rulings, page_width, page_height,
        all_labels, region, thresholds, separators, validator,
    )
    if field is not None:
        return field

    # No title label printed: match a known drawing-type phrase. Prefer the
    # largest type, then the candidate nearest the title-block region — a
    # title is set larger than the notes around it on every drawing checked.
    keywords = tb_config.get("sheet_title_keywords", [])
    candidates = []
    for line in lines:
        if tuple(line["bbox"]) in consumed_bboxes:
            continue
        text = line["text"].strip()
        result = validators.validate_sheet_title(text, exclusion_keywords=exclusions)
        if result is None:
            continue
        upper = text.upper()
        if not any(kw in upper for kw in keywords):
            continue
        candidates.append(line)
    if candidates:
        # **A sheet can carry more than one drawing, each with its own
        # caption.** One supplied sheet prints an existing sub-floor plan and
        # an existing floor plan side by side, in identical type. Picking
        # between them by which is nearer the title block is arbitrary - it
        # flips whenever the title block is located a little differently - so
        # the largest type wins and, among equals, the one printed first in
        # reading order. The others are reported as further drawings on the
        # sheet rather than quietly discarded.
        region_centre = None
        if region:
            region_centre = ((region[0] + region[2]) / 2.0, (region[1] + region[3]) / 2.0)

        def rank(line):
            # Largest type first, because a title is set larger than the notes
            # around it. Then nearest the title block, because a title printed
            # inside it is the sheet's own title while one out on the paper is
            # a caption for one of the drawings. Reading order last, so two
            # captions that are equal in every other way still resolve the
            # same way on every run.
            distance = 0.0
            if region_centre:
                cx, cy = bbox_center(line["bbox"])
                distance = ((cx - region_centre[0]) ** 2 + (cy - region_centre[1]) ** 2) ** 0.5
            return (
                -round(line["size"], 1),
                round(distance, 1),
                round(line["bbox"][1], 1),
                round(line["bbox"][0], 1),
            )

        candidates.sort(key=rank)
        best = candidates[0]
        largest = round(best["size"], 1)
        others = []
        for line in candidates[1:]:
            if round(line["size"], 1) < largest:
                break
            result = validators.validate_sheet_title(line["text"], exclusion_keywords=[])
            if result and result[0] != best["text"]:
                others.append(result[0])

        value, note = validators.validate_sheet_title(best["text"], exclusion_keywords=[])
        if others:
            note = "; ".join(
                filter(
                    None,
                    [note, f"this sheet also carries: {', '.join(dict.fromkeys(others))}"],
                )
            )
        field = _build_field(
            value=value,
            raw_text=best["text"],
            technique="title_keyword",
            source_line_or_bbox=best,
            extraction_method=best["extraction_method"],
            base_confidence=best["confidence"],
            thresholds=thresholds,
            note=note,
        )
        if others:
            field["conflicts"] = list(dict.fromkeys(others))
        return field

    # Last resort: the largest type inside the title-block region. Confined to
    # that region so a callout inside the drawing can never be picked, and
    # given the lowest confidence of any technique here.
    if region:
        in_region = [
            ln
            for ln in lines
            if _inside(ln["bbox"], region)
            and tuple(ln["bbox"]) not in consumed_bboxes
            and normalize_label(ln["text"]) not in all_labels
            and validators.validate_sheet_title(ln["text"], exclusion_keywords=exclusions) is not None
        ]
        if in_region:
            best = max(in_region, key=lambda ln: (round(ln["size"], 1), -ln["bbox"][1]))
            value, note = validators.validate_sheet_title(best["text"], exclusion_keywords=[])
            return _build_field(
                value=value,
                raw_text=best["text"],
                technique="largest_text_in_title_block",
                source_line_or_bbox=best,
                extraction_method=best["extraction_method"],
                base_confidence=best["confidence"],
                thresholds=thresholds,
                note="no title label printed on this sheet; largest type in the title block used",
            )

    # **A sheet may print no title-block labels at all.** The block is found
    # from the labels printed in it, so a drawing whose title strip carries
    # only values - or carries wording no office in /config uses - has no
    # region, and every technique above needs one. That sheet had no title,
    # which is the difference between a reader seeing what their drawing is
    # and seeing a blank.
    #
    # Where a title block sits is a drafting convention rather than an
    # opinion: it runs along one edge of the sheet. Which edge differs by
    # office - up the right, along the bottom, up the left, across the top -
    # so all four are looked in. The largest type inside those bands is taken,
    # at the lowest confidence of any technique here, with a note saying it
    # was read from where it sits rather than from a printed label, because
    # position is weaker evidence than a label and is shown as such.
    if region is None:
        left_band = page_width * _EDGE_BAND_WIDTH_RATIO
        right_band = page_width * (1.0 - _EDGE_BAND_WIDTH_RATIO)
        top_band = page_height * _EDGE_BAND_HEIGHT_RATIO
        bottom_band = page_height * (1.0 - _EDGE_BAND_HEIGHT_RATIO)
        in_band = []
        for line in lines:
            if tuple(line["bbox"]) in consumed_bboxes:
                continue
            if normalize_label(line["text"]) in all_labels:
                continue
            if validators.validate_sheet_title(line["text"], exclusion_keywords=exclusions) is None:
                continue
            cx, cy = bbox_center(line["bbox"])
            if cx <= left_band or cx >= right_band or cy <= top_band or cy >= bottom_band:
                in_band.append(line)
        if in_band:
            best = max(in_band, key=lambda ln: (round(ln["size"], 1), -ln["bbox"][1]))
            value, note = validators.validate_sheet_title(best["text"], exclusion_keywords=[])
            return _build_field(
                value=value,
                raw_text=best["text"],
                technique="largest_text_in_sheet_edge_band",
                source_line_or_bbox=best,
                extraction_method=best["extraction_method"],
                base_confidence=best["confidence"],
                thresholds=thresholds,
                note=(
                    "this sheet prints no title-block labels, so no title block could be "
                    "located; this is the largest type along one of the sheet's edges, "
                    "where a title block is drawn - worth checking"
                ),
            )
    return None


def detect_title_block(
    lines: list,
    rulings: dict,
    page_width: float,
    page_height: float,
    config: dict,
    page_number: int,
    page_count: int,
) -> dict:
    """Reads every configured title-block field for one page.

    Returns {"fields": {...}, "region": [...], "consumed_bboxes": set}. Fields
    that were not found are present with value=None so the interface always
    shows the full set and can say "not detected" explicitly (Critical Rule 5).
    """
    thresholds = config["confidence_thresholds"]
    tb_config = config["title_block"]
    field_labels = tb_config["field_labels"]
    separators = tb_config.get("inline_label_separators", [":"])
    all_labels = _all_configured_labels(field_labels)
    region = find_title_block_region(lines, field_labels, page_width, page_height, rulings)

    fields = {name: empty_field() for name in FIELD_NAMES}
    consumed_bboxes: set = set()

    def default_validator(text):
        return validators.validate_text_field(text)

    for field_name in (
        "sheet_number",
        "revision",
        "scale",
        "sheet_position",
        "project_number",
        "project_name",
        "client",
        "issue_date",
        "drawn_by",
        "checked_by",
    ):
        spec = field_labels.get(field_name, {})
        validator = validators.VALIDATORS.get(field_name, default_validator)
        # Page-specific labels identify this sheet and are always preferred.
        # Project-wide labels are read into their own field and never allowed
        # to stand in for a sheet identifier.
        label_list = list(spec.get("page_specific", [])) + list(spec.get("project_wide", []))
        if not label_list:
            continue
        found = _field_from_labels(
            field_name, label_list, lines, rulings, page_width, page_height,
            all_labels, region, thresholds, separators, validator,
        )
        if found is not None:
            fields[field_name] = found
            if found["source_bbox"]:
                consumed_bboxes.add(tuple(found["source_bbox"]))

    title = _detect_sheet_title(
        lines, rulings, page_width, page_height, all_labels, region,
        thresholds, config, consumed_bboxes,
    )
    if title is not None:
        fields["sheet_title"] = title
        if title["source_bbox"]:
            consumed_bboxes.add(tuple(title["source_bbox"]))

    # --- Issue date with no printed label ----------------------------------
    # Some title blocks print the date without labelling it ('DECEMBER 2012').
    # Searching for a date shape is only safe inside the detected title-block
    # region — a date in the drawing area is a revision note or a survey date,
    # not this sheet's issue date — so this fallback is confined to it and
    # scored below a labelled value.
    if fields["issue_date"]["value"] is None and region is not None:
        for line in sorted(lines, key=lambda ln: -ln["bbox"][1]):
            if not _inside(line["bbox"], region):
                continue
            if normalize_label(line["text"]) in all_labels:
                continue
            result = validators.validate_date(line["text"])
            if result is None:
                continue
            value, note = result
            fields["issue_date"] = _build_field(
                value=value,
                raw_text=line["text"],
                technique="title_keyword",
                source_line_or_bbox=line,
                extraction_method=line["extraction_method"],
                base_confidence=line["confidence"] * 0.8,
                thresholds=thresholds,
                note="; ".join(filter(None, [note, "no date label printed; date shape found inside the title block"])),
            )
            break

    # --- Sheet position: always available, always honest -------------------
    # A sheet must always be identifiable by its position in the supplied file
    # ("the 3rd sheet is sheet 3"), even when the drawing prints no sheet
    # number at all. That derivation is deterministic, so it is recorded as a
    # real value and marked as derived from page order rather than read off
    # the sheet.
    printed_position = fields["sheet_position"]
    ordinal = f"Page {page_number} of {page_count}"
    if printed_position["value"] is None:
        fields["sheet_position"] = _build_field(
            value=ordinal,
            raw_text=None,
            technique="derived_from_page_order",
            source_line_or_bbox=None,
            extraction_method="none",
            base_confidence=1.0,
            thresholds=thresholds,
            note="Based on this sheet's position in the uploaded document; not printed on the sheet.",
        )
    else:
        printed_value = printed_position["value"]
        if " of " not in printed_value:
            # The sheet printed its own page number but not the set total.
            # Comparing that printed number against the page's position in the
            # file is a genuine check on the input: a mismatch means the
            # supplied PDF is not the complete set, or its pages are out of
            # order, and that must be visible rather than smoothed over.
            digits = re.search(r"(\d+)", printed_value)
            printed_ordinal = int(digits.group(1)) if digits else None
            note = "Page number printed on the sheet; total taken from the uploaded document."
            if printed_ordinal is not None and printed_ordinal != page_number:
                note = (
                    f"The sheet prints page {printed_ordinal}, but it is page {page_number} "
                    "of the uploaded document. The document may be incomplete or out of order."
                )
                printed_position["review_status"] = "needs_review"
                printed_position["conflicts"] = [ordinal]
            printed_position["value"] = f"Page {printed_ordinal or page_number} of {page_count}"
            printed_position["note"] = "; ".join(
                filter(None, [printed_position.get("note"), note])
            )

    return {"fields": fields, "region": region, "consumed_bboxes": consumed_bboxes}


def sheet_id_for(fields: dict, page_number: int) -> tuple:
    """A stable identifier for the sheet, and where it came from.

    Uses the printed drawing number when the sheet prints one, otherwise the
    page ordinal. Every downstream record (rooms, dimensions, schedules) is
    keyed on this, so it must exist for every page — a page with no printed
    number still gets 'P07' rather than nothing.
    """
    printed = fields.get("sheet_number", {}).get("value")
    if printed:
        return printed, "printed_sheet_number"
    # No drawing number is printed, so the sheet is identified by its page in
    # the uploaded document. 'P04' keeps every derived record id short and
    # stable while still reading as a page reference.
    return f"P{page_number:02d}", "page_order"


def resolve_discipline(fields: dict, page_type: str, lines: list, region, config: dict) -> dict:
    """Discipline, from the strongest available evidence.

    Preference order matters here: a discipline word printed somewhere on a
    sheet frequently belongs to a consultant's letterhead or to another sheet
    listed in a drawing index, so it is the *last* source consulted, not the
    first, and it is only read from inside the title-block region.
    """
    thresholds = config["confidence_thresholds"]
    discipline_config = config["discipline"]

    sheet_number = fields.get("sheet_number", {}).get("value")
    if sheet_number:
        prefix_match = re.match(r"^([A-Z]{1,3})", sheet_number.upper())
        if prefix_match:
            mapped = discipline_config["prefix_map"].get(prefix_match.group(1))
            if mapped:
                confidence = round(fields["sheet_number"]["confidence"] * 0.95, 3)
                return {
                    **empty_field(),
                    "value": mapped,
                    "raw_text": sheet_number,
                    "confidence": confidence,
                    "confidence_band": confidence_band(confidence, thresholds),
                    "source_bbox": fields["sheet_number"]["source_bbox"],
                    "extraction_method": fields["sheet_number"]["extraction_method"],
                    "technique": "sheet_number_prefix",
                    "note": f"from the '{prefix_match.group(1)}' prefix of drawing number {sheet_number}",
                    "review_status": "confirmed" if confidence >= thresholds["review"] else "needs_review",
                }

    mapped = discipline_config.get("page_type_map", {}).get(page_type)
    if mapped and page_type not in ("unknown",):
        confidence = 0.7
        return {
            **empty_field(),
            "value": mapped,
            "raw_text": page_type,
            "confidence": confidence,
            "confidence_band": confidence_band(confidence, thresholds),
            "source_bbox": fields.get("sheet_title", {}).get("source_bbox"),
            "extraction_method": fields.get("sheet_title", {}).get("extraction_method", "none"),
            "technique": "page_type",
            "note": f"inferred from the sheet being a {page_type.replace('_', ' ')}; no discipline printed",
            "review_status": "needs_review",
        }

    for line in lines:
        if not _inside(line["bbox"], region):
            continue
        upper = line["text"].upper()
        for keyword, discipline in discipline_config["text_keywords"].items():
            if keyword in upper:
                confidence = round(line["confidence"] * 0.8, 3)
                return {
                    **empty_field(),
                    "value": discipline,
                    "raw_text": line["text"],
                    "confidence": confidence,
                    "confidence_band": confidence_band(confidence, thresholds),
                    "source_bbox": line["bbox"],
                    "extraction_method": line["extraction_method"],
                    "technique": "discipline_keyword_in_title_block",
                    "review_status": "needs_review",
                }

    return empty_field()
