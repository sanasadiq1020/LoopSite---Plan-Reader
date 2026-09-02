"""Pydantic response models for the plan-reading API.

Keeping these explicit (instead of returning raw dicts) is what lets the
frontend render real data instead of guessing shapes, and keeps the API
contract self-documenting in /docs.

Every extracted value carries the same four things, because Week 1's gates ask
for all four on every record: what was found, where on the sheet it came from,
how it was found, and how confident that is. A field with ``value = None`` is
kept in the response rather than omitted, so the interface can say "not
detected" explicitly instead of silently showing nothing (Critical Rule 5).
"""

from typing import Any, Literal, Optional

from pydantic import BaseModel

PageClassification = Literal["vector", "raster", "mixed", "unknown"]
ExtractionStatus = Literal["ok", "partial", "failed"]
ExtractionMethod = Literal["native", "ocr", "native_and_ocr", "none"]
OcrStatus = Literal[
    "skipped", "ok", "timeout", "failed",
    # Character recognition could not run here at all — it is not installed
    # in this deployment, or the machine has too little memory for it.
    "unavailable",
    # This upload’s allowance for reading scanned sheets was already spent.
    "not_attempted_budget_spent",
]
ConfidenceBand = Literal["high", "review", "low"]
ReviewStatus = Literal["confirmed", "needs_review", "unresolved", "auto_confirmed"]
PageType = Literal[
    "cover", "notes", "schedule", "detail", "section", "elevation",
    "site_plan", "roof_plan", "floor_plan", "unknown",
]


class TextBlock(BaseModel):
    text: str
    bbox: list[float]  # [x0, y0, x1, y1] in PDF points


class SheetEntry(BaseModel):
    run_id: str
    page_number: int  # 1-indexed, matches the source PDF
    classification: PageClassification
    extraction_status: ExtractionStatus
    # Defaults keep runs saved before Day 2 (no OCR fields yet) loadable
    # instead of failing validation when their history is reopened.
    extraction_method: ExtractionMethod = "native"
    native_text_char_count: int
    ocr_text_char_count: int = 0
    ocr_status: OcrStatus = "skipped"
    thumbnail_url: str
    width_pt: float
    height_pt: float
    error: Optional[str] = None
    # Why this sheet produced nothing, in the reader’s own words. None on a
    # sheet that read normally: a note is only ever an explanation of an
    # absence, never a label on a result.
    note: Optional[str] = None

    # Week 1 Gate 2: the register must identify each sheet, not only describe
    # how it was extracted. Defaulted so runs from before Day 3 still load.
    sheet_id: str = ""
    sheet_number: str = ""
    sheet_title: str = ""
    discipline: str = ""
    page_type: str = ""
    scale: str = ""
    revision: str = ""
    overlay_url: str = ""
    unresolved_p1: int = 0
    unresolved_p2: int = 0


class UploadResponse(BaseModel):
    run_id: str
    original_filename: str
    file_sha256: str
    page_count: int
    sheets: list[SheetEntry]


# --- Day 3: plan reading -------------------------------------------------


class TitleBlockField(BaseModel):
    """One detected title-block value.

    ``technique`` records how it was found ('label_value_below',
    'inline_label', 'sheet_index', 'derived_from_page_order', ...) and
    ``verified_against_index`` records whether the drawing index agreed with
    it. Together they let the interface show not only what was extracted but
    on what evidence — which is the difference between a reviewable result and
    a number to be taken on trust.
    """

    value: Optional[str] = None
    raw_text: Optional[str] = None
    confidence: float = 0.0
    confidence_band: ConfidenceBand = "low"
    source_bbox: Optional[list[float]] = None
    extraction_method: ExtractionMethod = "none"
    technique: Optional[str] = None
    label_matched: Optional[str] = None
    note: Optional[str] = None
    conflicts: list[str] = []
    verified_against_index: Optional[bool] = None
    review_status: ReviewStatus = "unresolved"


class TitleBlock(BaseModel):
    sheet_number: TitleBlockField
    sheet_title: TitleBlockField
    discipline: TitleBlockField
    revision: TitleBlockField
    scale: TitleBlockField
    sheet_position: TitleBlockField
    project_number: TitleBlockField
    project_name: TitleBlockField
    client: TitleBlockField
    issue_date: TitleBlockField
    drawn_by: TitleBlockField
    checked_by: TitleBlockField


class PageTypeResult(BaseModel):
    value: PageType = "unknown"
    confidence: float = 0.0
    confidence_band: ConfidenceBand = "low"
    technique: Optional[str] = None
    matched_keyword: Optional[str] = None
    note: Optional[str] = None
    content_agrees_with_title: Optional[bool] = None
    # What the sheet draws, from its contents — a sheet may carry a plan
    # under a title that names something else.
    draws_a_plan: bool = False
    evidence: dict = {}


class RoomLabel(BaseModel):
    room_id: str
    name: str
    normalized_name: Optional[str] = None
    instance: Optional[str] = None
    detection_method: str
    width_mm: Optional[float] = None
    height_mm: Optional[float] = None
    floor_area_m2: Optional[float] = None
    bbox: list[float]
    dimension_bbox: Optional[list[float]] = None
    confidence: float
    confidence_band: ConfidenceBand
    extraction_method: ExtractionMethod
    review_status: ReviewStatus = "needs_review"


class DimensionItem(BaseModel):
    dimension_id: str
    text: str
    kind: Literal["linear", "paired", "level"]
    # Which building axis the figure measures, taken from the writing
    # direction the PDF records — not inferred from the number.
    measures_axis: Literal["x", "y", "z"]
    value_mm: Optional[float] = None
    width_mm: Optional[float] = None
    height_mm: Optional[float] = None
    unit_source: str
    unit_assumption: Optional[str] = None
    is_overall: bool = False
    measured_to: Optional[str] = None
    level_reference: Optional[str] = None
    chain_id: Optional[str] = None
    chain_role: Optional[str] = None
    linked_room_id: Optional[str] = None
    link_method: Optional[str] = None
    link_note: Optional[str] = None
    bbox: list[float]
    confidence: float
    confidence_band: ConfidenceBand
    extraction_method: ExtractionMethod
    review_status: ReviewStatus = "needs_review"


class ChainCheck(BaseModel):
    overall_dimension_id: Optional[str] = None
    overall_mm: Optional[float] = None
    sum_of_running_mm: float
    difference_mm: Optional[float] = None
    variance_pct: Optional[float] = None
    tolerance_pct: float
    result: Literal["pass", "fail", "not_checked"]
    note: Optional[str] = None


class ParallelChainCheck(BaseModel):
    compared_with: str
    this_sum_mm: float
    other_sum_mm: float
    difference_mm: float
    variance_pct: Optional[float] = None
    tolerance_pct: Optional[float] = None
    result: Literal["pass", "fail"]
    note: Optional[str] = None


class DimensionChain(BaseModel):
    chain_id: str
    axis: Literal["x", "y"]
    member_dimension_ids: list[str]
    member_count: int
    sum_mm: float
    bbox: list[float]
    check: ChainCheck
    parallel_check: Optional[ParallelChainCheck] = None


class ScheduleGeometryCheck(BaseModel):
    rule: str
    expected_head_mm: float
    printed_head_mm: float
    difference_mm: float
    result: Literal["pass", "fail"]


class ScheduleRow(BaseModel):
    row_id: str
    mark: Optional[str] = None
    element_type: Optional[str] = None
    width_mm: Optional[float] = None
    height_mm: Optional[float] = None
    values: dict[str, Any] = {}
    geometry_check: Optional[ScheduleGeometryCheck] = None
    flags: list[str] = []
    bbox: Optional[list[float]] = None
    confidence: float
    confidence_band: ConfidenceBand
    review_status: ReviewStatus = "needs_review"


class UnassignedCell(BaseModel):
    attribute: str
    text: str
    bbox: list[float]


class ScheduleTable(BaseModel):
    table_id: str
    caption: str
    caption_source: str
    caption_bbox: Optional[list[float]] = None
    orientation: Literal["row_per_item", "column_per_item"]
    bbox: Optional[list[float]] = None
    columns: list[str]
    row_count: int
    rows: list[ScheduleRow]
    unassigned_cells: list[UnassignedCell] = []


class LegendEntry(BaseModel):
    entry_id: str
    symbol: Optional[str] = None
    description: str
    quantity: Optional[str] = None
    bbox: list[float]
    extraction_method: ExtractionMethod
    confidence: float


class LegendBlock(BaseModel):
    legend_id: str
    caption: str
    caption_bbox: list[float]
    entry_count: int
    entries: list[LegendEntry]


class OpeningMark(BaseModel):
    mark_id: str
    mark: str
    element_type: Optional[str] = None
    bbox: list[float]
    confidence: float
    extraction_method: ExtractionMethod


class ScaleCalibration(BaseModel):
    """Whether the sheet's printed scale survived being checked against the
    sheet's own dimension strings. Nothing is measured from a sheet whose
    scale is contradicted."""

    printed_scale: Optional[str] = None
    scale_denominator: Optional[int] = None
    printed_mm_per_point: Optional[float] = None
    measured_mm_per_point: Optional[float] = None
    variance_pct: Optional[float] = None
    tolerance_pct: float = 0.0
    strings_used: int = 0
    strings_agreeing: int = 0
    usable_for_measurement: bool = False
    result: Literal["confirmed", "contradicted", "inconclusive", "not_checked"] = "not_checked"
    note: Optional[str] = None


class WallCandidate(BaseModel):
    """A pair of parallel drawn faces a plausible wall thickness apart.

    A candidate for review, not a confirmed wall — the measured thickness is
    always kept as measured, with the nearest thickness the office builds
    reported alongside it as context.
    """

    wall_id: str
    # What the wall is: on the outside of the building, inside it, or not
    # established either way. Read from the geometry — a ray cast out from a
    # face that leaves the drawing without crossing another wall is an outside
    # face — never from where the wall happens to sit in a bounding rectangle.
    wall_type: Literal["outer", "inner", "unknown"] = "unknown"
    orientation: Optional[Literal["horizontal", "vertical"]] = None
    # The walls this one meets, and how each meeting is drawn: L at a corner,
    # T where a partition lands on it, + where two cross, collinear where the
    # wall carries on past a doorway.
    connects_to: list[str] = []
    junctions: list[dict] = []
    runs_along: Literal["x", "y"]
    length_mm: float
    thickness_mm: float
    nominal_thickness_mm: Optional[float] = None
    thickness_difference_mm: Optional[float] = None
    matches_nominal_thickness: bool = False
    start_point_pt: list[float]
    end_point_pt: list[float]
    face_positions_pt: list[float]
    line_source: str  # "vector" or "rendered_page" — see CLAUDE.md 4D
    longer_than_sheet_measures: bool = False
    bbox: list[float]
    # The two drawn faces this wall was measured from, and the line down the
    # middle of it — the evidence, kept beside the answer.
    face1: Optional[dict] = None
    face2: Optional[dict] = None
    centerline: Optional[dict] = None
    # Where both faces stop and start again together: a door or a window.
    gaps: list[dict] = []
    meets_another_wall: bool = True
    # Why a candidate was set aside, in a sentence for the person reading the
    # plan. None when the wall is used.
    not_used_because: Optional[str] = None
    # Whether the candidate ran out of the part of the sheet the plan is drawn
    # on and was cut back to it.
    trimmed_to_the_drawing: bool = False
    confidence: float
    confidence_band: ConfidenceBand
    confidence_label: Optional[Literal["high", "medium", "low"]] = None
    review_needed: bool = True
    review_status: ReviewStatus = "needs_review"
    linked_opening_marks: list[str] = []


class Opening(BaseModel):
    """One door or window: its mark on the drawing joined to its schedule row
    and, where the geometry is unambiguous, to a candidate wall."""

    opening_id: str
    mark: str
    # What the opening is called on the marked-up sheet. The printed mark where
    # the drawing prints one; a short made-up one where it does not, so a table
    # row can always be found on the drawing.
    display_mark: Optional[str] = None
    display_mark_is_made_up: bool = False
    element_type: Optional[str] = None
    wall_id: Optional[str] = None
    wall_note: Optional[str] = None
    # Where along the wall the opening sits, as fractions of the wall's own
    # run, and whether that was measured from a break in the drawing or taken
    # from where the mark is printed. Without it an opening cannot be cut.
    position_on_wall: Optional[dict] = None
    width_mm: Optional[float] = None
    height_mm: Optional[float] = None
    sill_height_mm: Optional[float] = None
    head_height_mm: Optional[float] = None
    location_on_plan: Optional[str] = None
    schedule_sheet: Optional[str] = None
    schedule_row_id: Optional[str] = None
    in_schedule: bool = False
    # "mark_on_the_drawing" or "gap_in_the_wall" — a reader must be able to see
    # which openings were labelled and which were measured off the geometry.
    found_by: str = "mark_on_the_drawing"
    # Every way the drawing said this opening is here: the window drawn inside
    # the wall, the door's swing, the mark printed beside it, the break in the
    # wall. Two or more agreeing is what makes an opening confirmed.
    evidence: list[str] = []
    how_it_was_decided: Optional[str] = None
    source_sheet: str
    source_bbox: list[float]
    confidence: float
    confidence_band: ConfidenceBand
    review_status: ReviewStatus = "needs_review"


class SheetIndexEntry(BaseModel):
    sheet_number: Optional[str] = None
    sheet_title: Optional[str] = None
    scale: Optional[str] = None
    revision: Optional[str] = None
    source_page: int
    source_bbox: list[float]


class SheetIndex(BaseModel):
    source_page: int
    header_bbox: list[float]
    columns: list[str]
    entries: list[SheetIndexEntry]


class UnresolvedItem(BaseModel):
    item_id: str = ""
    category: str
    severity: Literal["P0", "P1", "P2"]
    reason: str
    text: Optional[str] = None
    bbox: Optional[list[float]] = None


class PageReading(BaseModel):
    page_number: int
    sheet_id: str
    sheet_id_source: str
    page_type: PageTypeResult
    title_block: TitleBlock
    title_block_region: Optional[list[float]] = None
    # Whether a title block was located on this sheet, and what that means for
    # the reader when it was not. A sheet without one is still read in full.
    title_block_found: bool = False
    title_block_note: Optional[str] = None
    rooms: list[RoomLabel]
    dimensions: list[DimensionItem]
    dimension_chains: list[DimensionChain] = []
    schedules: list[ScheduleTable] = []
    legends: list[LegendBlock] = []
    opening_marks: list[OpeningMark] = []
    scale_calibration: Optional[ScaleCalibration] = None
    walls: list[WallCandidate] = []
    # Why no walls are reported from a sheet that has parallel lines on it but
    # does not look like a building.
    walls_note: Optional[str] = None
    openings: list[Opening] = []
    sheet_index: Optional[SheetIndex] = None
    unresolved_items: list[UnresolvedItem]
    text_evidence: dict = {}
    overlay_url: Optional[str] = None
    error: Optional[str] = None


class CrossCheckFinding(BaseModel):
    page_number: int
    sheet_number: Optional[str] = None
    field: str
    on_sheet: Optional[str] = None
    in_index: Optional[str] = None
    index_page: int


class CrossCheckReport(BaseModel):
    index_source_page: Optional[int] = None
    index_entry_count: int = 0
    compared_pages: int = 0
    agreements: int = 0
    disagreements: int = 0
    filled_from_index: int = 0
    unmatched_index_entries: list[dict] = []
    findings: list[CrossCheckFinding] = []


class OpeningReconciliation(BaseModel):
    """The document-wide join between marks drawn on plans and schedule rows.
    Both kinds of mismatch are preserved rather than resolved."""

    marks_on_drawings: int = 0
    matched_to_a_schedule: int = 0
    placed_on_a_wall: int = 0
    marks_without_a_schedule: list[dict] = []
    scheduled_marks_not_drawn: list[dict] = []


class PlanReadingResponse(BaseModel):
    run_id: str
    format_version: int = 1
    generated_at: str
    sheet_index: Optional[SheetIndex] = None
    cross_check: CrossCheckReport = CrossCheckReport()
    opening_reconciliation: OpeningReconciliation = OpeningReconciliation()
    accuracy: dict = {}
    metrics: dict = {}
    pages: list[PageReading]
