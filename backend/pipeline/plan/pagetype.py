"""Day 3 — what kind of sheet is this?

The Week 1 brief requires plan and elevation pages to be identified, and Gate 2
requires the sheet register to carry a discipline/type per page. Everything
after Week 1 depends on it too: Day 4 calibrates scale on a floor plan, Day 8
compares generated elevations against the drawn ones, Day 9 reads opening
schedules — each needs to know which sheets to look at.

Two independent sources are used and both are reported:

*   **What the sheet calls itself** — its detected title. A drawing that says
    'FLOOR PLAN' is a floor plan; this is the drawing's own statement and is
    the primary source.
*   **What the sheet contains** — how many room labels and dimension strings
    were actually found on it. A real floor plan has both in quantity.

Where the two disagree (a sheet titled 'FLOOR PLAN' with no rooms and no
dimensions, which is what a mostly-blank or failed page looks like) the
disagreement is recorded rather than resolved, so a page that failed to
extract cannot quietly pass as a successfully read floor plan.
"""

from pipeline.plan.textmodel import normalize_label

# Checked in this order, so a more specific type wins over a more general one:
# 'DOOR SCHEDULE SHT.1' is a schedule even though it contains no plan word,
# and 'PLAN DET. & INT. ELEV - KITCHEN' is a detail sheet rather than an
# elevation or a plan.
_TYPE_PRIORITY = (
    "cover",
    "notes",
    "schedule",
    "detail",
    "section",
    "elevation",
    "site_plan",
    "floor_plan",
)



def _type_from_text(text: str, keywords: dict):
    upper = normalize_label(text)
    for page_type in _TYPE_PRIORITY:
        for keyword in keywords.get(page_type, []):
            if normalize_label(keyword) in upper:
                return page_type, keyword
    return None, None


def detect_page_type(
    sheet_title,
    lines: list,
    room_count: int,
    dimension_count: int,
    schedule_count: int,
    config: dict,
) -> dict:
    """Returns a page-type record with its evidence and any disagreement."""
    page_config = config.get("page_types", {})
    keywords = page_config.get("keywords", {})
    thresholds = config["confidence_thresholds"]
    min_rooms = page_config.get("min_rooms_for_floor_plan_evidence", 4)
    min_dimensions = page_config.get("min_dimensions_for_floor_plan_evidence", 6)
    plan_types = set(page_config.get("plan_page_types", ["floor_plan", "site_plan"]))
    # A sheet whose title names one of these is not a plan, whatever else it
    # carries. A section prints room names and dimensions too, and an interior
    # elevation sheet prints dozens of them; letting content promote those to
    # plans found 202 "walls" on three sheets that draw no plan at all.
    not_a_plan = set(page_config.get("never_a_plan_page_types", []))

    has_plan_content = room_count >= min_rooms and dimension_count >= min_dimensions

    value = None
    technique = None
    matched = None
    confidence = 0.0
    note = None

    if sheet_title:
        value, matched = _type_from_text(sheet_title, keywords)
        if value:
            technique = "sheet_title"
            confidence = 0.9

    if value is None and schedule_count > 0:
        value = "schedule"
        technique = "schedule_table_found"
        confidence = 0.7
        note = "The title names no drawing type; a schedule table was found on the sheet."

    if value is None and has_plan_content:
        value = "floor_plan"
        technique = "page_content"
        confidence = 0.6
        note = (
            f"The title names no drawing type. Classified from the sheet's contents: "
            f"{room_count} room labels and {dimension_count} dimensions."
        )

    if value is None:
        value = "unknown"
        technique = None
        confidence = 0.0
        note = "The title names no drawing type, and the sheet has too little content to classify it."

    # Content check on a title-derived floor plan. Reported, never used to
    # overrule the drawing's own title.
    content_agrees = None
    if technique == "sheet_title" and value == "floor_plan":
        content_agrees = has_plan_content
        if not has_plan_content:
            note = (
                f"This sheet is titled as a plan, but only {room_count} room labels and "
                f"{dimension_count} dimensions were found. The reading may be incomplete."
            )
            confidence = round(confidence * 0.7, 3)

    band = "high" if confidence >= thresholds["review"] else (
        "review" if confidence >= thresholds["low"] else "low"
    )
    return {
        "value": value,
        # What the sheet actually draws, decided from its contents and kept
        # separate from what it calls itself.
        #
        # A sheet may carry more than one drawing, and its title names only
        # one of them. One plan set titles a sheet "FRAMING SPECIFICATIONS"
        # and prints a complete proposed floor plan on it — thirteen room
        # labels and nineteen dimensions. Classifying it from the title alone
        # meant no walls were ever looked for on the only floor plan in the
        # document. The title is a claim; the content is evidence, and both
        # are reported rather than one overruling the other.
        "draws_a_plan": (
            value in plan_types
            or (bool(has_plan_content) and value not in not_a_plan)
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
        },
    }
