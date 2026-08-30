"""What kind of sheet is this?

Every stage after plan reading depends on the answer: the scale is calibrated
on a plan, walls are looked for on a plan, elevations are compared against the
drawn ones, and opening schedules are read from schedule sheets. A sheet that
cannot be classified is a sheet nothing else can be done with, so this has to
work on drawings from an office nobody here has seen.

Four sources of evidence are used, strongest first, and the one that decided
is always recorded so a reader can see *why* a sheet was called what it was:

1.  **The sheet's own title.** A drawing that titles itself FLOOR PLAN is a
    floor plan. This is the drawing stating its own kind, and nothing
    overrules it.
2.  **A drawing caption printed on the sheet.** Many offices put a generic
    project title in the title block and name the drawing in large type under
    it. Only the largest such caption counts, because a small section marker
    beside a cutting line is a reference to another sheet, not this sheet's
    own kind.
3.  **Tables printed on the sheet.** A door or window schedule makes a sheet a
    schedule; a drawing index makes it a cover sheet.
4.  **What the sheet contains.** Room names with dimensions are a plan.
    Printed height levels with no room names are a drawing of the building
    seen from the side or cut through. Mostly written text with no figures is
    a notes sheet.

Where the title and the contents disagree, both are reported and neither is
quietly discarded: a sheet titled as a plan with nothing on it is what a
failed extraction looks like, and it must not pass as a successfully read
plan.
"""

import re

from pipeline.plan.textmodel import normalize_label

# Checked in this order, so a more specific type wins over a more general one:
# a door schedule is a schedule even though it contains no plan word, and a
# sheet of interior elevation details is a detail sheet rather than an
# elevation or a plan.
_TYPE_PRIORITY = (
    "cover",
    "notes",
    "schedule",
    "detail",
    "section",
    "elevation",
    "site_plan",
    # Checked before floor_plan: a roof plan is drawn looking down like one,
    # but it shows the roof rather than the building, and its parallel lines
    # are battens and ridges rather than walls.
    "roof_plan",
    "floor_plan",
)

# A printed height level: RL 100.400, FFL 25.60, NGL, AHD. These are how a
# drawing states a height, so a sheet covered in them is a vertical drawing -
# an elevation or a section - and never a plan.
_LEVEL_MARKER_RE = re.compile(
    r"\b(?:R\.?L|F\.?F\.?L|F\.?C\.?L|C\.?L|N\.?G\.?L|A\.?H\.?D|T\.?O\.?W|S\.?S\.?L)\b\.?\s*[-+]?\d",
    re.IGNORECASE,
)

# A line that reads as prose rather than as a drawing label: several words,
# mostly lower case. A notes or specification sheet is made of these.
_SENTENCE_RE = re.compile(r"[a-z]{3,}\s+[a-z]{3,}")

_MAX_CAPTION_LENGTH = 80


def _type_from_text(text: str, keywords: dict):
    upper = normalize_label(text)
    for page_type in _TYPE_PRIORITY:
        for keyword in keywords.get(page_type, []):
            if normalize_label(keyword) in upper:
                return page_type, keyword
    return None, None


def _largest_drawing_caption(lines: list, keywords: dict, min_size_ratio: float):
    """The most prominent line on the sheet that names a kind of drawing.

    Size is the test, not position. A drawing's caption is set large under the
    drawing it names; a section marker beside a cutting line, and a note
    saying "see elevation", are set small. Taking the largest keeps the first
    and ignores the other two.
    """
    matches = []
    for line in lines:
        text = (line.get("text") or "").strip()
        if not text or len(text) > _MAX_CAPTION_LENGTH:
            continue
        page_type, keyword = _type_from_text(text, keywords)
        if page_type is None:
            continue
        matches.append((float(line.get("size") or 0.0), page_type, keyword, text))
    if not matches:
        return None, None, None

    largest = max(size for size, _, _, _ in matches)
    if largest <= 0:
        # No type sizes are available, which is the case for text recovered
        # from a page image. Fall back to the first match in reading order.
        _, page_type, keyword, text = matches[0]
        return page_type, keyword, text

    prominent = [m for m in matches if m[0] >= largest * min_size_ratio]
    prominent.sort(key=lambda m: (-m[0], _TYPE_PRIORITY.index(m[1])))
    _, page_type, keyword, text = prominent[0]
    return page_type, keyword, text


def _content_signals(lines: list) -> dict:
    """Plain counts of what is printed on the sheet, with no interpretation."""
    level_markers = 0
    sentences = 0
    counted = 0
    for line in lines:
        text = (line.get("text") or "").strip()
        if not text:
            continue
        counted += 1
        if _LEVEL_MARKER_RE.search(text):
            level_markers += 1
        if len(text) >= 25 and _SENTENCE_RE.search(text):
            sentences += 1
    return {
        "line_count": counted,
        "level_marker_count": level_markers,
        "sentence_line_count": sentences,
        "sentence_share": round(sentences / counted, 3) if counted else 0.0,
    }


def detect_page_type(
    sheet_title,
    lines: list,
    room_count: int,
    dimension_count: int,
    schedule_count: int,
    config: dict,
    legend_count: int = 0,
    has_drawing_index: bool = False,
) -> dict:
    """Returns a page-type record with its evidence and any disagreement."""
    page_config = config.get("page_types", {})
    keywords = page_config.get("keywords", {})
    thresholds = config["confidence_thresholds"]
    min_rooms = page_config.get("min_rooms_for_floor_plan_evidence", 4)
    min_dimensions = page_config.get("min_dimensions_for_floor_plan_evidence", 6)
    # A small building - an extension, a granny flat, a shed - prints only a
    # handful of room names. Requiring four of them meant a small building's
    # only floor plan was never recognised as one, so a drawing with fewer
    # rooms but dimensions beside them is accepted as well.
    min_rooms_with_dimensions = page_config.get("min_rooms_with_dimensions", 1)
    min_caption_size_ratio = float(page_config.get("caption_min_size_ratio", 0.75))
    min_level_markers = page_config.get("min_level_markers_for_vertical_drawing", 3)
    min_sentence_share = float(page_config.get("min_sentence_share_for_notes", 0.5))
    plan_types = set(page_config.get("plan_page_types", ["floor_plan", "site_plan"]))
    # A sheet whose title names one of these is not a plan, whatever else it
    # carries. A section prints room names and dimensions too, and an interior
    # elevation sheet prints dozens of them; letting content promote those to
    # plans found 202 walls on three sheets that draw no plan at all.
    not_a_plan = set(page_config.get("never_a_plan_page_types", []))

    signals = _content_signals(lines)
    has_plan_content = (room_count >= min_rooms and dimension_count >= min_dimensions) or (
        room_count >= min_rooms_with_dimensions and dimension_count >= min_dimensions
    )

    value = None
    technique = None
    matched = None
    caption_text = None
    confidence = 0.0
    note = None

    # 1. The drawing says what it is.
    if sheet_title:
        value, matched = _type_from_text(sheet_title, keywords)
        if value:
            technique = "sheet_title"
            confidence = 0.9

    # 2. The drawing is captioned on the sheet even though the title block is
    #    generic - common wherever one title block serves a whole set.
    if value is None:
        caption_type, caption_keyword, caption_text = _largest_drawing_caption(
            lines, keywords, min_caption_size_ratio
        )
        if caption_type:
            value = caption_type
            matched = caption_keyword
            technique = "drawing_caption"
            confidence = 0.8
            note = (
                "The title block names no kind of drawing. Taken from the caption "
                f"printed on the sheet: {caption_text}."
            )

    # 3. Tables printed on the sheet.
    if value is None and has_drawing_index:
        value = "cover"
        technique = "drawing_index_found"
        confidence = 0.75
        note = "The title names no kind of drawing; this sheet prints the drawing index."

    if value is None and schedule_count > 0:
        value = "schedule"
        technique = "schedule_table_found"
        confidence = 0.7
        note = "The title names no kind of drawing; a schedule table was found on the sheet."

    # 4. What the sheet contains.
    if value is None and has_plan_content:
        value = "floor_plan"
        technique = "page_content"
        confidence = 0.6
        note = (
            "The title names no kind of drawing. Taken from what is printed on the "
            f"sheet: {room_count} room names and {dimension_count} dimensions."
        )

    if value is None and signals["level_marker_count"] >= min_level_markers and room_count == 0:
        value = "elevation"
        technique = "page_content"
        confidence = 0.5
        note = (
            "The title names no kind of drawing. This sheet prints "
            f"{signals['level_marker_count']} height levels and no room names, which is "
            "how a drawing of the building seen from the side is marked."
        )

    if (
        value is None
        and signals["sentence_share"] >= min_sentence_share
        and dimension_count == 0
        and signals["line_count"] >= 10
    ):
        value = "notes"
        technique = "page_content"
        confidence = 0.5
        note = (
            "The title names no kind of drawing. This sheet is mostly written text "
            "with no dimensions on it."
        )

    if value is None:
        value = "unknown"
        technique = None
        confidence = 0.0
        note = (
            "This sheet does not name a kind of drawing anywhere on it, and there is "
            "too little printed on it to tell what it is. Everything found on it is "
            "still listed, and still traceable to where it was printed."
        )

    # Content check on a floor plan taken from what the sheet calls itself.
    # Reported, never used to overrule the drawing's own words.
    content_agrees = None
    if technique in ("sheet_title", "drawing_caption") and value == "floor_plan":
        content_agrees = has_plan_content
        if not has_plan_content:
            note = (
                f"This sheet is named as a plan, but only {room_count} room names and "
                f"{dimension_count} dimensions were found on it. The reading may be "
                "incomplete."
            )
            confidence = round(confidence * 0.7, 3)

    band = (
        "high"
        if confidence >= thresholds["review"]
        else ("review" if confidence >= thresholds["low"] else "low")
    )
    return {
        "value": value,
        # What the sheet actually draws, decided from its contents and kept
        # separate from what it calls itself.
        #
        # A sheet may carry more than one drawing, and its title names only
        # one of them. One plan set titles a sheet FRAMING SPECIFICATIONS and
        # prints a complete proposed floor plan on it - thirteen room names
        # and nineteen dimensions. Classifying it from the title alone meant
        # no walls were ever looked for on the only floor plan in the
        # document. The title is a claim; the content is evidence, and both
        # are reported rather than one overruling the other.
        "draws_a_plan": (
            value in plan_types or (bool(has_plan_content) and value not in not_a_plan)
        ),
        # Whether the sheet itself said it is a plan, rather than the type
        # being inferred from what is printed on it. A sheet that says so is
        # trusted with less content on it before its walls are traced.
        "named_as_a_plan": bool(
            value in plan_types and technique in ("sheet_title", "drawing_caption")
        ),
        "confidence": round(confidence, 3),
        "confidence_band": band,
        "technique": technique,
        "matched_keyword": matched,
        "note": note,
        "content_agrees_with_title": content_agrees,
        "evidence": {
            "room_label_count": room_count,
            "dimension_count": dimension_count,
            "schedule_table_count": schedule_count,
            "legend_count": legend_count,
            "drawing_index_on_sheet": bool(has_drawing_index),
            "caption_on_sheet": caption_text,
            "height_level_count": signals["level_marker_count"],
            "written_text_share": signals["sentence_share"],
        },
    }
