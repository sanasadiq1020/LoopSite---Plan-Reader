// Thin client for the LoopSite backend API. All URLs are built from
// NEXT_PUBLIC_API_BASE_URL — never hardcode a host here.
//
// The types below mirror backend/app/schemas.py exactly. Every extracted
// value carries the same four things — what was found, where on the sheet,
// how it was found, and how confident — because the interface is required to
// show all four rather than presenting bare values as facts.

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export type PageClassification = "vector" | "raster" | "mixed" | "unknown";
export type ExtractionStatus = "ok" | "partial" | "failed";
export type ExtractionMethod = "native" | "ocr" | "native_and_ocr" | "none";
export type OcrStatus = "skipped" | "ok" | "timeout" | "failed";
export type ConfidenceBand = "high" | "review" | "low";
export type ReviewStatus = "confirmed" | "needs_review" | "unresolved";
export type Severity = "P0" | "P1" | "P2";

export type PageType =
  | "cover"
  | "notes"
  | "schedule"
  | "detail"
  | "section"
  | "elevation"
  | "site_plan"
  | "floor_plan"
  | "unknown";

export interface SheetEntry {
  run_id: string;
  page_number: number;
  classification: PageClassification;
  extraction_status: ExtractionStatus;
  extraction_method: ExtractionMethod;
  native_text_char_count: number;
  ocr_text_char_count: number;
  ocr_status: OcrStatus;
  thumbnail_url: string;
  width_pt: number;
  height_pt: number;
  error: string | null;
  sheet_id: string;
  sheet_number: string;
  sheet_title: string;
  discipline: string;
  page_type: string;
  scale: string;
  revision: string;
  overlay_url: string;
  unresolved_p1: number;
  unresolved_p2: number;
}

export interface UploadResponse {
  run_id: string;
  original_filename: string;
  file_sha256: string;
  page_count: number;
  sheets: SheetEntry[];
}

export interface TitleBlockField {
  value: string | null;
  raw_text: string | null;
  confidence: number;
  confidence_band: ConfidenceBand;
  source_bbox: number[] | null;
  extraction_method: ExtractionMethod;
  technique: string | null;
  label_matched: string | null;
  note: string | null;
  conflicts: string[];
  verified_against_index: boolean | null;
  review_status: ReviewStatus;
}

export const TITLE_BLOCK_FIELDS = [
  "sheet_number",
  "sheet_title",
  "sheet_position",
  "discipline",
  "revision",
  "scale",
  "issue_date",
  "project_number",
  "project_name",
  "client",
  "drawn_by",
  "checked_by",
] as const;

export type TitleBlockFieldName = (typeof TITLE_BLOCK_FIELDS)[number];
export type TitleBlock = Record<TitleBlockFieldName, TitleBlockField>;

export interface PageTypeResult {
  value: PageType;
  confidence: number;
  confidence_band: ConfidenceBand;
  technique: string | null;
  matched_keyword: string | null;
  note: string | null;
  content_agrees_with_title: boolean | null;
  evidence: Record<string, number>;
}

export interface RoomLabel {
  room_id: string;
  name: string;
  normalized_name: string | null;
  instance: string | null;
  detection_method: string;
  width_mm: number | null;
  height_mm: number | null;
  floor_area_m2: number | null;
  bbox: number[];
  dimension_bbox: number[] | null;
  confidence: number;
  confidence_band: ConfidenceBand;
  extraction_method: ExtractionMethod;
  review_status: ReviewStatus;
}

export interface DimensionItem {
  dimension_id: string;
  text: string;
  kind: "linear" | "paired" | "level";
  measures_axis: "x" | "y" | "z";
  value_mm: number | null;
  width_mm: number | null;
  height_mm: number | null;
  unit_source: string;
  unit_assumption: string | null;
  is_overall: boolean;
  level_reference: string | null;
  chain_id: string | null;
  chain_role: string | null;
  linked_room_id: string | null;
  link_method: string | null;
  link_note: string | null;
  bbox: number[];
  confidence: number;
  confidence_band: ConfidenceBand;
  extraction_method: ExtractionMethod;
  review_status: ReviewStatus;
}

export interface ChainCheck {
  overall_dimension_id: string | null;
  overall_mm: number | null;
  sum_of_running_mm: number;
  difference_mm: number | null;
  variance_pct: number | null;
  tolerance_pct: number;
  result: "pass" | "fail" | "not_checked";
  note: string | null;
}

export interface ParallelChainCheck {
  compared_with: string;
  this_sum_mm: number;
  other_sum_mm: number;
  difference_mm: number;
  variance_pct: number | null;
  tolerance_pct: number | null;
  result: "pass" | "fail";
  note: string | null;
}

export interface DimensionChain {
  chain_id: string;
  axis: "x" | "y";
  member_dimension_ids: string[];
  member_count: number;
  sum_mm: number;
  bbox: number[];
  check: ChainCheck;
  parallel_check: ParallelChainCheck | null;
}

export interface ScheduleGeometryCheck {
  rule: string;
  expected_head_mm: number;
  printed_head_mm: number;
  difference_mm: number;
  result: "pass" | "fail";
}

export interface ScheduleRow {
  row_id: string;
  mark: string | null;
  element_type: string | null;
  width_mm: number | null;
  height_mm: number | null;
  values: Record<string, string>;
  geometry_check: ScheduleGeometryCheck | null;
  flags: string[];
  bbox: number[] | null;
  confidence: number;
  confidence_band: ConfidenceBand;
  review_status: ReviewStatus;
}

export interface ScheduleTable {
  table_id: string;
  caption: string;
  caption_source: string;
  caption_bbox: number[] | null;
  orientation: "row_per_item" | "column_per_item";
  bbox: number[] | null;
  columns: string[];
  row_count: number;
  rows: ScheduleRow[];
  unassigned_cells: { attribute: string; text: string; bbox: number[] }[];
}

export interface LegendEntry {
  entry_id: string;
  symbol: string | null;
  description: string;
  quantity: string | null;
  bbox: number[];
  extraction_method: ExtractionMethod;
  confidence: number;
}

export interface LegendBlock {
  legend_id: string;
  caption: string;
  caption_bbox: number[];
  entry_count: number;
  entries: LegendEntry[];
}

export interface OpeningMark {
  mark_id: string;
  mark: string;
  element_type: string | null;
  bbox: number[];
  confidence: number;
  extraction_method: ExtractionMethod;
}

export interface ScaleCalibration {
  printed_scale: string | null;
  scale_denominator: number | null;
  printed_mm_per_point: number | null;
  measured_mm_per_point: number | null;
  variance_pct: number | null;
  tolerance_pct: number;
  strings_used: number;
  strings_agreeing: number;
  usable_for_measurement: boolean;
  result: "confirmed" | "contradicted" | "inconclusive" | "not_checked";
  note: string | null;
}

export interface WallCandidate {
  wall_id: string;
  runs_along: "x" | "y";
  length_mm: number;
  thickness_mm: number;
  nominal_thickness_mm: number | null;
  thickness_difference_mm: number | null;
  matches_nominal_thickness: boolean;
  start_point_pt: number[];
  end_point_pt: number[];
  face_positions_pt: number[];
  bbox: number[];
  line_source: string;
  longer_than_sheet_measures: boolean;
  confidence: number;
  confidence_band: ConfidenceBand;
  review_status: ReviewStatus;
  linked_opening_marks: string[];
}

export interface Opening {
  opening_id: string;
  mark: string;
  element_type: string | null;
  wall_id: string | null;
  wall_note: string | null;
  width_mm: number | null;
  height_mm: number | null;
  sill_height_mm: number | null;
  head_height_mm: number | null;
  location_on_plan: string | null;
  schedule_sheet: string | null;
  schedule_row_id: string | null;
  in_schedule: boolean;
  found_by: string;
  source_sheet: string;
  source_bbox: number[];
  confidence: number;
  confidence_band: ConfidenceBand;
  review_status: ReviewStatus;
}

export interface OpeningReconciliation {
  marks_on_drawings: number;
  matched_to_a_schedule: number;
  placed_on_a_wall: number;
  marks_without_a_schedule: { sheet_id: string; mark: string }[];
  scheduled_marks_not_drawn: { mark: string; schedule_sheet: string; table_id: string }[];
}

export interface AccuracyPerType {
  expected: number;
  matched: number;
  wrong: number;
  missed: number;
  unexpected: number;
  recall_pct: number | null;
  precision_pct: number | null;
}

export interface AccuracyReport {
  reference_rows: number;
  verified_rows: number;
  unverified_rows: number;
  measured: boolean;
  note: string | null;
  per_item_type: Record<string, AccuracyPerType>;
  wall_length_variance_pct?: { worst: number | null; average: number | null };
}

export interface SheetIndexEntry {
  sheet_number: string | null;
  sheet_title: string | null;
  scale: string | null;
  revision: string | null;
  source_page: number;
  source_bbox: number[];
}

export interface SheetIndex {
  source_page: number;
  header_bbox: number[];
  columns: string[];
  entries: SheetIndexEntry[];
}

export interface UnresolvedItem {
  item_id: string;
  category: string;
  severity: Severity;
  reason: string;
  text: string | null;
  bbox: number[] | null;
}

export interface PageReading {
  page_number: number;
  sheet_id: string;
  sheet_id_source: string;
  page_type: PageTypeResult;
  title_block: TitleBlock;
  title_block_region: number[] | null;
  rooms: RoomLabel[];
  dimensions: DimensionItem[];
  dimension_chains: DimensionChain[];
  schedules: ScheduleTable[];
  legends: LegendBlock[];
  opening_marks: OpeningMark[];
  scale_calibration: ScaleCalibration | null;
  walls: WallCandidate[];
  openings: Opening[];
  sheet_index: SheetIndex | null;
  unresolved_items: UnresolvedItem[];
  text_evidence: Record<string, unknown>;
  overlay_url: string | null;
  error: string | null;
}

export interface CrossCheckFinding {
  page_number: number;
  sheet_number: string | null;
  field: string;
  on_sheet: string | null;
  in_index: string | null;
  index_page: number;
}

export interface CrossCheckReport {
  index_source_page: number | null;
  index_entry_count: number;
  compared_pages: number;
  agreements: number;
  disagreements: number;
  filled_from_index: number;
  unmatched_index_entries: { sheet_number: string; sheet_title: string }[];
  findings: CrossCheckFinding[];
}

export interface ReadingMetrics {
  page_count: number;
  pages_with_errors: number;
  sheet_coverage_pct: number | null;
  title_block: {
    fields_expected: number;
    fields_found: number;
    fields_found_pct: number | null;
    fields_high_confidence_pct: number | null;
    per_field: Record<
      string,
      { pages: number; found: number; verified: number; disagreed: number; found_pct: number | null }
    >;
  };
  cross_check: {
    index_source_page: number | null;
    compared_pages: number;
    agreements: number;
    disagreements: number;
    filled_from_index: number;
    agreement_pct: number | null;
  };
  records: {
    rooms: number;
    dimensions: number;
    schedule_rows: number;
    legend_entries: number;
    candidate_walls: number;
    openings: number;
  };
  scale_calibration: {
    sheets_confirmed: number;
    sheets_contradicted: number;
    sheets_not_checked: number;
    confirmed_pct: number | null;
  };
  walls: {
    candidates: number;
    at_nominal_thickness: number;
    at_nominal_thickness_pct: number | null;
  };
  openings: {
    distinct_openings: number;
    marks_on_drawings: number;
    matched_to_a_schedule: number;
    matched_to_a_schedule_pct: number | null;
    placed_on_a_wall: number;
    placed_on_a_wall_pct: number | null;
    scheduled_marks_not_drawn: number;
  };
  dimension_chain_check: {
    chains_checked: number;
    chains_passed: number;
    pass_pct: number | null;
  };
  traceability: {
    records_with_source: number;
    records_total: number;
    traceability_pct: number | null;
    note: string;
  };
  unresolved_items: Record<Severity, number>;
  page_types: Record<string, number>;
}

export interface PlanReadingResponse {
  run_id: string;
  generated_at: string;
  sheet_index: SheetIndex | null;
  cross_check: CrossCheckReport;
  opening_reconciliation: OpeningReconciliation;
  accuracy: AccuracyReport;
  metrics: ReadingMetrics;
  pages: PageReading[];
}

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

export function fileUrl(path: string): string {
  if (!path) return "";
  return `${API_BASE_URL}${path}`;
}

/** What an upload is doing right now, while it is still being read. */
export interface UploadProgress {
  known: boolean;
  stage?: string;
  percent?: number;
  pages_done?: number;
  pages_total?: number;
  finished?: boolean;
  failed?: boolean;
}

/**
 * Asks how far an upload has got.
 *
 * The browser brings its own token to the upload and asks about that, because
 * the run id does not exist until the upload has been accepted — several
 * seconds in, by which time the reader is already waiting.
 */
export async function readUploadProgress(token: string): Promise<UploadProgress> {
  try {
    const res = await fetch(`${API_BASE_URL}/api/plan/progress/${token}`, {
      credentials: "include",
    });
    if (!res.ok) return { known: false };
    return (await res.json()) as UploadProgress;
  } catch {
    // A missed poll is not a failure — the next one will answer.
    return { known: false };
  }
}

/** Removes a run the reader has finished with, so one plan is on disk at a time. */
export async function discardRun(runId: string): Promise<void> {
  try {
    await fetch(`${API_BASE_URL}/api/plan/${runId}/discard`, {
      method: "POST",
      credentials: "include",
      keepalive: true, // still sent when the page is closing
    });
  } catch {
    // Best effort: the next upload clears earlier runs anyway.
  }
}

export async function uploadPlan(file: File, token = ""): Promise<UploadResponse> {
  const formData = new FormData();
  formData.append("file", file);

  const query = token ? `?token=${encodeURIComponent(token)}` : "";
  const res = await fetch(`${API_BASE_URL}/api/plan/upload${query}`, {
    method: "POST",
    body: formData,
    credentials: "include", // send/receive the session cookie cross-port in dev
  });

  if (!res.ok) {
    let detail = `Upload failed (HTTP ${res.status}).`;
    try {
      const body = await res.json();
      if (body?.detail) detail = body.detail;
    } catch {
      // response wasn't JSON — keep the generic message
    }
    throw new ApiError(detail, res.status);
  }

  return res.json();
}

async function requestJson<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE_URL}${path}`, { credentials: "include" });
  if (!res.ok) {
    let detail = `Request failed (HTTP ${res.status}).`;
    try {
      const body = await res.json();
      if (body?.detail) detail = body.detail;
    } catch {
      // response wasn't JSON — keep the generic message
    }
    throw new ApiError(detail, res.status);
  }
  return res.json();
}

export async function getPlanReading(runId: string): Promise<PlanReadingResponse> {
  return requestJson<PlanReadingResponse>(`/api/plan/${runId}/reading`);
}

// --- Download URLs --------------------------------------------------------

export function sheetRegisterCsvUrl(runId: string): string {
  return fileUrl(`/api/plan/${runId}/sheet-register.csv`);
}

export function exportCsvUrl(
  runId: string,
  name: "rooms" | "dimensions" | "schedule-rows" | "walls" | "openings"
): string {
  return fileUrl(`/api/plan/${runId}/export/${name}.csv`);
}

/** The full issues log: every value that needs checking, with the sheet and
 *  position it came from. Offered as a download rather than shown on screen. */
/** Every marked-up sheet for this plan, as one download. */
export function markedUpSheetsUrl(runId: string): string {
  return fileUrl(`/api/plan/${runId}/marked-up-sheets.zip`);
}

/** A reference file for this plan, pre-filled from the run: what the reader
 *  read, where on the sheet to look for it, and how it was read. */
export function groundTruthTemplateUrl(runId: string): string {
  return fileUrl(`/api/plan/${runId}/ground-truth-template.csv`);
}

/** The row-by-row comparison against the manually checked reference. */
export function accuracyCsvUrl(runId: string): string {
  return fileUrl(`/api/plan/${runId}/accuracy-report.csv`);
}

export function issuesCsvUrl(runId: string): string {
  return fileUrl(`/api/plan/${runId}/issues-log.csv`);
}

// --- Display helpers ------------------------------------------------------

export const NOT_AVAILABLE = "N/A";

export function formatMm(value: number | null | undefined): string {
  if (value === null || value === undefined) return NOT_AVAILABLE;
  return `${value.toLocaleString(undefined, { maximumFractionDigits: 0 })} mm`;
}

export function formatDimensionValue(dim: DimensionItem): string {
  if (dim.kind === "paired" && dim.width_mm != null && dim.height_mm != null) {
    return `${dim.width_mm.toLocaleString()} × ${dim.height_mm.toLocaleString()} mm`;
  }
  if (dim.kind === "level" && dim.value_mm != null) {
    return `${(dim.value_mm / 1000).toFixed(3)} m ${dim.level_reference ?? ""}`.trim();
  }
  return formatMm(dim.value_mm);
}

const UNIT_SOURCE_LABELS: Record<string, string> = {
  explicit_mm: "Printed in mm",
  explicit_metres: "Printed in metres",
  explicit_level_metres: "Printed as a level",
  assumed_mm: "Assumed mm",
  grouped_or_metres: "mm (either reading)",
  explicit: "Printed with units",
};

export function unitSourceLabel(source: string): string {
  return UNIT_SOURCE_LABELS[source] ?? source.replace(/_/g, " ");
}

/** Which way a wall runs, described the way someone looking at the sheet
 *  would describe it rather than by axis letter. */
export function wallDirectionLabel(runsAlong: string): string {
  return runsAlong === "x" ? "Across the sheet" : "Up the sheet";
}

const AXIS_LABELS: Record<string, string> = {
  x: "Across",
  y: "Up the sheet",
  z: "Level",
};

export function axisLabel(axis: string): string {
  return AXIS_LABELS[axis] ?? axis;
}

const PAGE_TYPE_LABELS: Record<string, string> = {
  cover: "Cover sheet",
  notes: "General notes",
  schedule: "Schedule",
  detail: "Details",
  section: "Section",
  elevation: "Elevation",
  site_plan: "Site plan",
  floor_plan: "Floor plan",
  unknown: "Unclassified",
};

export function pageTypeLabel(type: string): string {
  return PAGE_TYPE_LABELS[type] ?? type;
}

const TECHNIQUE_LABELS: Record<string, string> = {
  label_value_below: "Printed under its label on the sheet",
  label_value_right: "Printed beside its label on the sheet",
  inline_label: "Printed on the same line as its label",
  title_keyword: "Recognised from the drawing type in the title",
  largest_text_in_title_block: "Largest text in the title block, as no label was printed",
  derived_from_page_order: "Based on the sheet's position in the document",
  sheet_index: "Taken from the drawing index, not this sheet",
  sheet_number_prefix: "From the letter at the start of the drawing number",
  page_type: "Based on what kind of sheet this is",
  discipline_keyword_in_title_block: "Printed in the title block",
};

export function techniqueLabel(technique: string | null): string {
  if (!technique) return "Not found on this sheet";
  return TECHNIQUE_LABELS[technique] ?? technique.replace(/_/g, " ");
}

export function fieldLabel(name: string): string {
  return name
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase())
    .replace(/\bNo\b/, "No.");
}

/** Where a wall's two lines were measured from, in plain words. */
export function wallLineSourceLabel(source: string): string {
  if (source === "rendered_page") return "The sheet as a picture";
  if (source === "vector") return "The drawing's own lines";
  return "UNKNOWN";
}

/** How an opening came to be known, in plain words. */
export function openingFoundByLabel(source: string): string {
  if (source === "gap_in_the_wall") return "Measured from the break in the wall";
  return "Labelled on the drawing";
}

// --- Day 5: the canonical model and its 3D files -------------------------

/** One sheet, and whether a 3D model can be built from it. */
export interface ModellableSheet {
  page_number: number;
  sheet_id: string;
  sheet_number: string | null;
  sheet_title: string | null;
  page_type: string;
  wall_count: number;
  opening_count: number;
  room_count: number;
  can_be_modelled: boolean;
  reason: string | null;
}

export interface ModelStorey {
  storey_id: string;
  name: string;
  elevation_mm: number;
  height_mm: number;
  height_source: string;
  height_confidence: number;
  height_note: string;
}

export interface ModelWall {
  element_id: string;
  element_type: string;
  storey: string;
  geometry: {
    start_mm: number[];
    end_mm: number[];
    base_elevation_mm: number;
    runs_along: string;
  };
  dimensions: {
    length_mm: number;
    thickness_mm: number;
    height_mm: number;
    thickness_is_measured: boolean;
    nominal_thickness_mm: number | null;
  };
  source_sheet: string;
  source_bbox: number[] | null;
  extraction_method: string;
  confidence: number;
  confidence_band: ConfidenceBand;
  review_status: ReviewStatus;
  linked_opening_ids: string[];
  assumptions: string[];
  from_wall_id: string;
}

export interface ProjectModel {
  format_version: number;
  generated_at: string;
  run_id: string;
  source_file: string;
  modelled_sheet: {
    page_number: number;
    sheet_id: string;
    sheet_number: string | null;
    sheet_title: string | null;
  };
  units: string;
  coordinate_system: {
    x: string;
    y: string;
    z: string;
    note: string;
    millimetres_per_page_point: number;
    scale_result: string | null;
  };
  extent_mm: { x: number; y: number; z: number };
  storeys: ModelStorey[];
  walls: ModelWall[];
  openings: {
    element_id: string;
    element_type: string;
    mark: string | null;
    in_wall: string | null;
    dimensions: { width_mm: number | null; height_mm: number | null };
  }[];
  assumptions: { about: string; statement: string; confidence: number }[];
  files?: Record<string, boolean>;
}

export async function fetchModellableSheets(
  runId: string
): Promise<{ sheets: ModellableSheet[]; default_page_number: number | null }> {
  return requestJson(`/api/plan/${runId}/model/sheets`);
}

/** Builds the model for one sheet. This is the work; it can take a moment. */
export async function buildSheetModel(
  runId: string,
  pageNumber: number
): Promise<ProjectModel> {
  const res = await fetch(`${API_BASE_URL}/api/plan/${runId}/model/${pageNumber}`, {
    method: "POST",
    credentials: "include",
  });
  if (!res.ok) {
    let detail = `The model could not be built (HTTP ${res.status}).`;
    try {
      const body = await res.json();
      if (body?.detail) detail = body.detail;
    } catch {
      // not JSON — keep the generic message
    }
    throw new ApiError(detail, res.status);
  }
  return (await res.json()) as ProjectModel;
}

export function modelFileUrl(runId: string, pageNumber: number, kind: string): string {
  const download = kind === "glb" ? "" : "?download=true";
  return `${API_BASE_URL}/api/plan/${runId}/model/${pageNumber}/file/${kind}${download}`;
}

/** Where a storey height came from, in words a reader can act on. */
export function heightSourceLabel(source: string): string {
  switch (source) {
    case "confirmed_by_the_drawing":
      return "Confirmed by the drawing, two ways";
    case "printed_on_a_section":
      return "Dimensioned on a section sheet";
    case "difference_between_printed_levels":
      return "From the levels printed on the drawing";
    case "dimensioned_but_levels_disagree":
      return "Dimensioned, but the levels disagree";
    case "office_default":
      return "Office default — the drawing states none";
    default:
      return "Not established";
  }
}

// --- What this release is, and what it cannot do -------------------------

export interface ReleaseInfo {
  product: string;
  version: string;
  released: string;
  stage: string;
  what_it_does: string[];
  known_limitations: string[];
  not_in_this_release: string[];
}

/** Read from the product's own configuration, never written into the screen. */
export async function fetchRelease(): Promise<ReleaseInfo> {
  return requestJson<ReleaseInfo>("/api/plan/release");
}

