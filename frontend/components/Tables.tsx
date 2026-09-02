"use client";

import {
  axisLabel,
  fieldLabel,
  formatDimensionValue,
  formatMm,
  pageTypeLabel,
  techniqueLabel,
  unitSourceLabel,
  junctionShapeLabel,
  wallDirectionLabel,
  NOT_AVAILABLE,
  type DimensionItem,
  type LegendBlock,
  type Opening,
  type RoomLabel,
  type ScheduleTable,
  type SheetEntry,
  type TitleBlock,
  type WallCandidate,
  type TitleBlockFieldName,
  TITLE_BLOCK_FIELDS,
  wallLineSourceLabel,
  openingFoundByLabel,
  positionSourceLabel,
} from "@/lib/api";
import {
  ClassificationBadge,
  ConfidenceBadge,
  DataTable,
  NotFound,
  PageTypeBadge,
  StatusBadge,
  type Column,
} from "@/components/Ui";

// --- Sheet register -------------------------------------------------------

export function SheetRegisterTable({
  sheets,
  onSelect,
}: {
  sheets: SheetEntry[];
  onSelect: (sheet: SheetEntry) => void;
}) {
  const columns: Column<SheetEntry>[] = [
    {
      key: "page",
      header: "Page",
      width: "62px",
      render: (row) => <span className="tabular-nums font-semibold">{row.page_number}</span>,
      sortValue: (row) => row.page_number,
    },
    {
      key: "sheet",
      header: "Drawing no.",
      width: "112px",
      render: (row) =>
        row.sheet_number ? (
          <span className="font-mono text-xs font-semibold text-slate-800">
            {row.sheet_number}
          </span>
        ) : (
          <span className="text-xs text-slate-500">Page {row.page_number}</span>
        ),
      sortValue: (row) => row.sheet_number || `Page ${row.page_number}`,
    },
    {
      key: "title",
      header: "Sheet title",
      render: (row) => (
        <div>
          {row.sheet_title ? (
            <span className="font-medium text-slate-800">{row.sheet_title}</span>
          ) : (
            <NotFound />
          )}
          {/* A sheet that produced nothing says why here. Without this the
              row is indistinguishable from a drawing that genuinely has
              nothing on it. */}
          {row.note && (
            <p className="mt-1 max-w-xl text-xs leading-snug text-amber-700">{row.note}</p>
          )}
        </div>
      ),
      sortValue: (row) => row.sheet_title,
    },
    {
      key: "type",
      header: "Type",
      width: "120px",
      render: (row) => <PageTypeBadge type={row.page_type} label={pageTypeLabel(row.page_type)} />,
      sortValue: (row) => row.page_type,
    },
    {
      key: "discipline",
      header: "Discipline",
      width: "120px",
      render: (row) => row.discipline || <NotFound />,
      sortValue: (row) => row.discipline,
    },
    {
      key: "scale",
      header: "Scale",
      width: "110px",
      render: (row) =>
        row.scale ? <span className="tabular-nums">{row.scale}</span> : <NotFound />,
      sortValue: (row) => row.scale,
    },
    {
      key: "rev",
      header: "Revision",
      width: "84px",
      render: (row) =>
        row.revision || <span className="text-slate-400">{NOT_AVAILABLE}</span>,
      sortValue: (row) => row.revision,
    },
    {
      key: "source",
      header: "Page source",
      width: "150px",
      render: (row) => (
        <div className="flex flex-wrap items-center gap-1">
          <ClassificationBadge classification={row.classification} />
          <StatusBadge status={row.extraction_status} />
        </div>
      ),
      sortValue: (row) => `${row.classification} ${row.extraction_status}`,
    },
  ];

  return (
    <DataTable
      columns={columns}
      rows={sheets}
      getRowKey={(row) => String(row.page_number)}
      searchPlaceholder="Search by drawing number, title or type"
      onRowClick={onSelect}
      initialSort={{ key: "page", direction: "asc" }}
    />
  );
}

// --- Sheet details (title block) -----------------------------------------

export function TitleBlockTable({ titleBlock }: { titleBlock: TitleBlock }) {
  const rows = TITLE_BLOCK_FIELDS.map((name) => ({ name, field: titleBlock[name] }));
  type Row = (typeof rows)[number];

  const columns: Column<Row>[] = [
    {
      key: "field",
      header: "Detail",
      width: "150px",
      render: (row) => (
        <span className="font-medium text-slate-700">{fieldLabel(row.name)}</span>
      ),
      sortValue: (row) => row.name,
    },
    {
      key: "value",
      header: "Value",
      render: (row) =>
        row.field.value === null ? (
          <NotFound reason="This detail is not printed on the sheet, or the text found did not match the expected format." />
        ) : (
          <span className="font-semibold text-slate-900">{row.field.value}</span>
        ),
      sortValue: (row) => row.field.value ?? "",
    },
    {
      key: "how",
      header: "Where it came from",
      render: (row) => (
        <div className="text-xs leading-relaxed text-slate-600">
          <div>{techniqueLabel(row.field.technique)}</div>
          {row.field.label_matched && (
            <div className="text-slate-400">Label on the sheet: “{row.field.label_matched}”</div>
          )}
          {row.field.raw_text && row.field.raw_text !== row.field.value && (
            <div className="text-slate-400">Printed as: “{row.field.raw_text}”</div>
          )}
          {row.field.note && <div className="mt-0.5 text-amber-700">{row.field.note}</div>}
          {row.field.conflicts.length > 0 && (
            <div className="mt-0.5 font-medium text-rose-600">
              Also printed: {row.field.conflicts.join(", ")}
            </div>
          )}
        </div>
      ),
    },
    {
      key: "confidence",
      header: "Certainty",
      width: "195px",
      render: (row) =>
        row.field.value === null ? (
          <span className="text-slate-400">{NOT_AVAILABLE}</span>
        ) : (
          <ConfidenceBadge
            band={row.field.confidence_band}
            value={row.field.confidence}
            verified={row.field.verified_against_index}
          />
        ),
      sortValue: (row) => row.field.confidence,
    },
    {
      key: "bbox",
      header: "Position on sheet",
      width: "150px",
      render: (row) =>
        row.field.source_bbox ? (
          <span className="font-mono text-[11px] text-slate-400">
            {row.field.source_bbox.map((v) => Math.round(v)).join(", ")}
          </span>
        ) : (
          <span className="text-[11px] text-slate-400">Not on the sheet</span>
        ),
    },
  ];

  return (
    <DataTable
      dense
      columns={columns}
      rows={rows}
      getRowKey={(row) => row.name as TitleBlockFieldName}
    />
  );
}

// --- Rooms ----------------------------------------------------------------

const ROOM_METHOD_LABELS: Record<string, string> = {
  vocabulary: "Recognised room name",
  paired_dimension_below_label: "Size printed beneath the label",
  vocabulary_and_paired_dimension: "Recognised room name with a size beneath it",
};

export type RoomRow = RoomLabel & { sheetId: string; pageNumber: number; sheetLabel: string };

export function RoomsTable({ rooms, showSheet }: { rooms: RoomRow[]; showSheet?: boolean }) {
  const columns: Column<RoomRow>[] = [
    ...(showSheet ? [sheetColumn<RoomRow>()] : []),
    {
      key: "id",
      header: "Room ref.",
      width: "110px",
      render: (row) => <span className="font-mono text-xs font-semibold">{row.room_id}</span>,
      sortValue: (row) => row.room_id,
    },
    {
      key: "name",
      header: "Name on the drawing",
      render: (row) => <span className="font-semibold text-slate-900">{row.name}</span>,
      sortValue: (row) => row.name,
    },
    {
      key: "type",
      header: "Room type",
      width: "150px",
      render: (row) =>
        row.normalized_name ? (
          <span>
            {row.normalized_name}
            {row.instance && (
              <span className="ml-1 rounded bg-slate-100 px-1 text-[11px] font-semibold text-slate-600">
                #{row.instance}
              </span>
            )}
          </span>
        ) : (
          <span className="text-slate-400">Not a standard name</span>
        ),
      sortValue: (row) => row.normalized_name ?? "",
    },
    {
      key: "size",
      header: "Size",
      width: "170px",
      render: (row) =>
        row.width_mm != null && row.height_mm != null ? (
          <span className="tabular-nums">
            {row.width_mm.toLocaleString()} × {row.height_mm.toLocaleString()} mm
          </span>
        ) : (
          <span className="text-slate-400">Not printed</span>
        ),
      sortValue: (row) => row.width_mm ?? 0,
    },
    {
      key: "area",
      header: "Floor area",
      width: "110px",
      render: (row) =>
        row.floor_area_m2 != null ? (
          <span className="tabular-nums font-medium">{row.floor_area_m2} m²</span>
        ) : (
          <span className="text-slate-400">{NOT_AVAILABLE}</span>
        ),
      sortValue: (row) => row.floor_area_m2 ?? 0,
    },
    {
      key: "method",
      header: "How it was found",
      render: (row) => (
        <span className="text-xs text-slate-600">
          {ROOM_METHOD_LABELS[row.detection_method] ?? row.detection_method}
        </span>
      ),
      sortValue: (row) => row.detection_method,
    },
    {
      key: "confidence",
      header: "Certainty",
      width: "140px",
      render: (row) => <ConfidenceBadge band={row.confidence_band} value={row.confidence} />,
      sortValue: (row) => row.confidence,
    },
  ];

  return (
    <DataTable
      columns={columns}
      rows={rooms}
      getRowKey={(row) => row.room_id}
      searchPlaceholder="Search rooms"
      emptyMessage="No room labels were found on this sheet."
    />
  );
}

function sheetColumn<T extends { sheetLabel: string; pageNumber: number }>(): Column<T> {
  return {
    key: "sheet",
    header: "Sheet",
    width: "104px",
    render: (row) => <span className="font-mono text-xs text-slate-500">{row.sheetLabel}</span>,
    sortValue: (row) => row.pageNumber,
  };
}

// --- Dimensions -----------------------------------------------------------

export type DimensionRow = DimensionItem & {
  sheetId: string;
  pageNumber: number;
  sheetLabel: string;
};

export function DimensionsTable({
  dimensions,
  roomNames,
  showSheet,
}: {
  dimensions: DimensionRow[];
  roomNames: Map<string, string>;
  showSheet?: boolean;
}) {
  const columns: Column<DimensionRow>[] = [
    ...(showSheet ? [sheetColumn<DimensionRow>()] : []),
    {
      key: "id",
      header: "Ref.",
      width: "120px",
      render: (row) => (
        <span className="font-mono text-[11px] text-slate-500">{row.dimension_id}</span>
      ),
      sortValue: (row) => row.dimension_id,
    },
    {
      key: "text",
      header: "As printed",
      width: "130px",
      render: (row) => <span className="font-mono text-xs text-slate-800">{row.text}</span>,
      sortValue: (row) => row.text,
    },
    {
      key: "value",
      header: "Measurement",
      width: "150px",
      render: (row) => (
        <span className="tabular-nums font-semibold text-slate-900">
          {formatDimensionValue(row)}
        </span>
      ),
      sortValue: (row) => row.value_mm ?? row.width_mm ?? 0,
    },
    {
      key: "axis",
      header: "Direction",
      width: "120px",
      render: (row) => (
        <span
          className={`inline-block rounded border px-1.5 py-0.5 text-[11px] font-medium ${
            row.measures_axis === "x"
              ? "border-orange-200 bg-orange-50 text-orange-700"
              : row.measures_axis === "y"
                ? "border-sky-200 bg-sky-50 text-sky-700"
                : "border-slate-200 bg-slate-50 text-slate-600"
          }`}
        >
          {axisLabel(row.measures_axis)}
        </span>
      ),
      sortValue: (row) => row.measures_axis,
    },
    {
      key: "unit",
      header: "Units",
      width: "130px",
      render: (row) => (
        <span className="text-[11px] text-slate-500" title={row.unit_assumption ?? undefined}>
          {unitSourceLabel(row.unit_source)}
        </span>
      ),
      sortValue: (row) => row.unit_source,
    },
    {
      key: "room",
      header: "Room or area",
      render: (row) =>
        row.linked_room_id ? (
          <span className="text-xs">
            <span className="font-medium text-slate-700">
              {roomNames.get(row.linked_room_id) ?? row.linked_room_id}
            </span>
            <span className="block text-slate-400">{row.link_note}</span>
          </span>
        ) : (
          <span className="text-xs text-slate-400">{row.link_note ?? "Not linked to a room."}</span>
        ),
      sortValue: (row) => row.linked_room_id ?? "",
    },
    {
      key: "confidence",
      header: "Certainty",
      width: "140px",
      render: (row) => <ConfidenceBadge band={row.confidence_band} value={row.confidence} />,
      sortValue: (row) => row.confidence,
    },
  ];

  return (
    <DataTable
      dense
      columns={columns}
      rows={dimensions}
      getRowKey={(row) => row.dimension_id}
      searchPlaceholder="Search dimensions"
      emptyMessage="No dimensions were found on this sheet."
    />
  );
}

// --- Schedules ------------------------------------------------------------

export function ScheduleTableView({ table }: { table: ScheduleTable }) {
  const columns: Column<ScheduleTable["rows"][number]>[] = [
    {
      key: "mark",
      header: "Mark",
      width: "80px",
      render: (row) =>
        row.mark ? (
          <span className="font-mono text-xs font-bold text-slate-800">{row.mark}</span>
        ) : (
          <NotFound />
        ),
      sortValue: (row) => row.mark ?? "",
    },
    {
      key: "type",
      header: "Type",
      width: "90px",
      render: (row) =>
        row.element_type ? (
          <span className="capitalize">{row.element_type}</span>
        ) : (
          <span className="text-slate-400">{NOT_AVAILABLE}</span>
        ),
      sortValue: (row) => row.element_type ?? "",
    },
    {
      key: "width",
      header: "Width",
      width: "100px",
      render: (row) => <span className="tabular-nums">{formatMm(row.width_mm)}</span>,
      sortValue: (row) => row.width_mm ?? 0,
    },
    {
      key: "height",
      header: "Height",
      width: "100px",
      render: (row) => <span className="tabular-nums">{formatMm(row.height_mm)}</span>,
      sortValue: (row) => row.height_mm ?? 0,
    },
    ...table.columns
      .filter((column) => !["id", "mark", "width", "height"].includes(column))
      .map((column) => ({
        key: column,
        header: fieldLabel(column),
        render: (row: ScheduleTable["rows"][number]) =>
          row.values[column] ? (
            <span className="text-xs">{row.values[column]}</span>
          ) : (
            <span className="text-slate-400">{NOT_AVAILABLE}</span>
          ),
        sortValue: (row: ScheduleTable["rows"][number]) => row.values[column] ?? "",
      })),
    {
      key: "check",
      header: "Checks",
      width: "190px",
      render: (row) => (
        <div className="flex flex-col gap-1 text-xs">
          {row.geometry_check && (
            <span
              className={
                row.geometry_check.result === "pass" ? "text-emerald-700" : "text-rose-700"
              }
              title={row.geometry_check.rule}
            >
              {row.geometry_check.result === "pass"
                ? "Sill + height matches head height"
                : `Sill + height is ${Math.abs(row.geometry_check.difference_mm)} mm off the head height`}
            </span>
          )}
          {row.flags.map((flag) => (
            <span key={flag} className="text-amber-700">
              {flag}
            </span>
          ))}
          {!row.geometry_check && row.flags.length === 0 && (
            <span className="text-slate-400">{NOT_AVAILABLE}</span>
          )}
        </div>
      ),
    },
  ];

  return (
    <DataTable
      dense
      columns={columns}
      rows={table.rows}
      getRowKey={(row) => row.row_id}
      searchPlaceholder="Search this schedule"
    />
  );
}

// --- Legends --------------------------------------------------------------

export function LegendTable({ legend }: { legend: LegendBlock }) {
  const hasQuantity = legend.entries.some((entry) => entry.quantity);
  const columns: Column<LegendBlock["entries"][number]>[] = [
    {
      key: "symbol",
      header: "Symbol",
      width: "160px",
      render: (row) =>
        row.symbol ? (
          <span className="font-mono text-xs font-bold text-slate-800">{row.symbol}</span>
        ) : (
          <span className="text-slate-400">Drawn symbol</span>
        ),
      sortValue: (row) => row.symbol ?? "",
    },
    {
      key: "description",
      header: "What it means",
      render: (row) => row.description,
      sortValue: (row) => row.description,
    },
    ...(hasQuantity
      ? [
          {
            key: "quantity",
            header: "Count on sheet",
            width: "130px",
            render: (row: LegendBlock["entries"][number]) =>
              row.quantity ? (
                <span className="tabular-nums font-medium">{row.quantity}</span>
              ) : (
                <span className="text-slate-400">{NOT_AVAILABLE}</span>
              ),
            sortValue: (row: LegendBlock["entries"][number]) => Number(row.quantity ?? 0),
          },
        ]
      : []),
  ];
  return (
    <DataTable
      dense
      columns={columns}
      rows={legend.entries}
      getRowKey={(row) => row.entry_id}
      searchPlaceholder="Search this legend"
    />
  );
}

// --- Walls ----------------------------------------------------------------

export type WallRow = WallCandidate & {
  sheetId: string;
  pageNumber: number;
  sheetLabel: string;
};

export function WallsTable({ walls, showSheet }: { walls: WallRow[]; showSheet?: boolean }) {
  const columns: Column<WallRow>[] = [
    ...(showSheet ? [sheetColumn<WallRow>()] : []),
    {
      key: "id",
      header: "Wall ref.",
      width: "120px",
      render: (row) => <span className="font-mono text-xs font-semibold">{row.wall_id}</span>,
      sortValue: (row) => row.wall_id,
    },
    {
      key: "length",
      header: "Length",
      width: "150px",
      render: (row) => (
        <div>
          <span className="tabular-nums font-semibold">{formatMm(row.length_mm)}</span>
          {row.longer_than_sheet_measures && (
            <span
              className="mt-0.5 block text-xs text-amber-700"
              title="No dimension on this sheet measures a distance this long, so this pair of lines is more likely a boundary or an eave line than a wall."
            >
              longer than this sheet measures
            </span>
          )}
          {row.meets_another_wall === false && (
            <span
              className="mt-0.5 block text-xs text-amber-700"
              title="A building's walls form one connected outline — they meet at corners and junctions. This pair of lines meets none of the others, so it is more likely an eave, a roof extent, a fence or a bench. It is listed here, but it is left off the marked-up sheet and out of the 3D model."
            >
              meets no other wall — not used
            </span>
          )}
        </div>
      ),
      sortValue: (row) => row.length_mm,
    },
    {
      key: "thickness",
      header: "Thickness",
      width: "200px",
      render: (row) => (
        <span className="tabular-nums">
          {formatMm(row.thickness_mm)}
          {row.nominal_thickness_mm != null && (
            <span
              className={`ml-1.5 text-[11px] ${
                row.matches_nominal_thickness ? "text-slate-500" : "text-amber-700"
              }`}
            >
              {row.matches_nominal_thickness
                ? `(a ${row.nominal_thickness_mm} mm wall)`
                : "(not a thickness normally built)"}
            </span>
          )}
        </span>
      ),
      sortValue: (row) => row.thickness_mm,
    },
    {
      // Which side of the building a wall is on is the first thing a reader
      // wants from a wall list, and it is the difference between a wall that
      // faces the weather and one that does not.
      key: "type",
      header: "Outside or inside",
      width: "170px",
      render: (row) => (
        <span
          className={
            row.wall_type === "unknown" ? "text-xs text-slate-400" : "text-xs text-slate-700"
          }
          title={
            row.wall_type === "outer"
              ? "Nothing but open paper on one side of it, so it is on the outside of the building."
              : row.wall_type === "inner"
                ? "There is building on both sides of it, so it is a wall inside the plan."
                : "This pair of lines meets no other wall, so it has not been established as part of this building and neither answer would be honest."
          }
        >
          {row.wall_type === "outer"
            ? "Outside wall"
            : row.wall_type === "inner"
              ? "Inside wall"
              : "Not established"}
        </span>
      ),
      sortValue: (row) => row.wall_type,
    },
    {
      key: "direction",
      header: "Runs",
      width: "150px",
      render: (row) => wallDirectionLabel(row.runs_along),
      sortValue: (row) => row.runs_along,
    },
    {
      // A wall on its own is a pair of lines; a wall joined to other walls is
      // part of a building. Showing what it meets is what lets a reviewer
      // follow the plan round from one wall to the next.
      key: "junctions",
      header: "Meets",
      width: "190px",
      render: (row) =>
        row.connects_to?.length ? (
          <span
            className="text-xs text-slate-600"
            title={row.junctions
              ?.map((j) => `${j.with_wall_id} (${junctionShapeLabel(j.shape)})`)
              .join(", ")}
          >
            {row.connects_to.length} wall{row.connects_to.length === 1 ? "" : "s"}
            <span className="ml-1 font-mono text-[11px] text-slate-400">
              {row.connects_to.slice(0, 2).join(", ")}
              {row.connects_to.length > 2 ? "…" : ""}
            </span>
          </span>
        ) : (
          <span className="text-xs text-slate-400">Meets no other wall</span>
        ),
      sortValue: (row) => row.connects_to?.length ?? 0,
    },
    {
      key: "source",
      header: "Measured from",
      width: "170px",
      render: (row) => (
        <span
          className="text-xs text-slate-500"
          title={
            row.line_source === "rendered_page"
              ? "This sheet stores its drawing as a picture, so its lines were read from the page as it prints rather than from the PDF's own line work."
              : "Read from the drawing's own line work, which is exact."
          }
        >
          {wallLineSourceLabel(row.line_source)}
        </span>
      ),
      sortValue: (row) => row.line_source,
    },
    {
      key: "openings",
      header: "Doors & windows on it",
      render: (row) =>
        row.linked_opening_marks.length > 0 ? (
          <span className="font-mono text-xs">{row.linked_opening_marks.join(", ")}</span>
        ) : (
          <span className="text-slate-400">None placed on this wall</span>
        ),
      sortValue: (row) => row.linked_opening_marks.join(","),
    },
    {
      key: "confidence",
      header: "Certainty",
      width: "140px",
      render: (row) => <ConfidenceBadge band={row.confidence_band} value={row.confidence} />,
      sortValue: (row) => row.confidence,
    },
  ];

  return (
    <DataTable
      columns={columns}
      rows={walls}
      getRowKey={(row) => row.wall_id}
      searchPlaceholder="Search walls"
      emptyMessage="No wall candidates were found on this sheet."
      initialSort={{ key: "length", direction: "desc" }}
    />
  );
}

// --- Doors and windows ----------------------------------------------------

export type OpeningRow = Opening & {
  sheetId: string;
  pageNumber: number;
  sheetLabel: string;
};

export function OpeningsTable({
  openings,
  showSheet,
}: {
  openings: OpeningRow[];
  showSheet?: boolean;
}) {
  const columns: Column<OpeningRow>[] = [
    ...(showSheet ? [sheetColumn<OpeningRow>()] : []),
    {
      key: "mark",
      header: "Mark",
      width: "80px",
      render: (row) =>
        row.mark ? (
          <span className="font-mono text-xs font-bold">{row.mark}</span>
        ) : (
          // The drawing prints no code for this one, so it is referred to by
          // the reference drawn beside it on the marked-up sheet.
          <span
            className="font-mono text-xs text-slate-500"
            title="This drawing prints no code for its openings. This is the reference shown beside it on the marked-up sheet."
          >
            {row.opening_id}
          </span>
        ),
      sortValue: (row) => row.mark || row.opening_id,
    },
    {
      key: "type",
      header: "Type",
      width: "90px",
      render: (row) =>
        row.element_type ? (
          <span className="capitalize">{row.element_type}</span>
        ) : (
          <span className="text-slate-400">{NOT_AVAILABLE}</span>
        ),
      sortValue: (row) => row.element_type ?? "",
    },
    {
      key: "found",
      header: "How it was found",
      width: "210px",
      render: (row) => (
        <span
          className="text-xs text-slate-500"
          title={
            row.found_by === "gap_in_the_wall"
              ? "This drawing prints no code for its doors and windows, so the opening was measured where the wall stops and starts again. Its width is measured; its type and height are not stated because the drawing does not state them."
              : "The drawing prints a code such as D1 or W12, which is matched to the schedule row of the same code."
          }
        >
          {openingFoundByLabel(row.found_by)}
        </span>
      ),
      sortValue: (row) => row.found_by,
    },
    {
      key: "size",
      header: "Size",
      width: "170px",
      render: (row) =>
        row.width_mm != null && row.height_mm != null ? (
          <span className="tabular-nums">
            {row.width_mm.toLocaleString()} × {row.height_mm.toLocaleString()} mm
          </span>
        ) : row.width_mm != null ? (
          // An opening measured off the drawing has a real width. A plan does
          // not show a height, so that stays blank rather than being invented.
          <div>
            <span className="tabular-nums">{row.width_mm.toLocaleString()} mm wide</span>
            <span className="mt-0.5 block text-xs text-slate-400">
              height not shown on a plan
            </span>
          </div>
        ) : (
          <NotFound label="Not in a schedule" />
        ),
      sortValue: (row) => row.width_mm ?? 0,
    },
    {
      key: "sill",
      header: "Sill height",
      width: "110px",
      render: (row) =>
        row.sill_height_mm != null ? (
          <span className="tabular-nums">{formatMm(row.sill_height_mm)}</span>
        ) : (
          <span className="text-slate-400">{NOT_AVAILABLE}</span>
        ),
      sortValue: (row) => row.sill_height_mm ?? -1,
    },
    {
      key: "room",
      header: "Room it serves",
      render: (row) =>
        row.location_on_plan ?? <span className="text-slate-400">{NOT_AVAILABLE}</span>,
      sortValue: (row) => row.location_on_plan ?? "",
    },
    {
      key: "wall",
      header: "Wall it is in",
      render: (row) =>
        row.wall_id ? (
          <span className="font-mono text-xs text-slate-700" title={row.wall_note ?? undefined}>
            {row.wall_id}
          </span>
        ) : (
          <span className="text-xs text-slate-400">{row.wall_note}</span>
        ),
      sortValue: (row) => row.wall_id ?? "",
    },
    {
      // Which wall a door is in is not enough to cut it: the hole has to go
      // somewhere along that wall. This says where, and whether that was
      // measured off the drawing or taken from where the mark is printed.
      key: "position",
      header: "Where on the wall",
      width: "200px",
      render: (row) =>
        row.position_on_wall ? (
          <div>
            <span className="tabular-nums">
              {formatMm(row.position_on_wall.from_wall_start_mm)} from the wall&rsquo;s start
            </span>
            <span className="mt-0.5 block text-xs text-slate-400">
              {positionSourceLabel(row.position_on_wall.measured_from)}
            </span>
          </div>
        ) : (
          <span className="text-xs text-slate-400">Not established</span>
        ),
      sortValue: (row) => row.position_on_wall?.from_wall_start_mm ?? -1,
    },
    {
      key: "schedule",
      header: "From schedule",
      width: "130px",
      render: (row) =>
        row.schedule_sheet ? (
          <span className="text-xs text-slate-600">Sheet {row.schedule_sheet}</span>
        ) : (
          <span className="text-xs text-amber-700">Not scheduled</span>
        ),
      sortValue: (row) => row.schedule_sheet ?? "",
    },
    {
      key: "confidence",
      header: "Certainty",
      width: "140px",
      render: (row) => <ConfidenceBadge band={row.confidence_band} value={row.confidence} />,
      sortValue: (row) => row.confidence,
    },
  ];

  return (
    <DataTable
      columns={columns}
      rows={openings}
      getRowKey={(row) => row.opening_id}
      searchPlaceholder="Search doors and windows"
      emptyMessage="No door or window marks were found on this sheet."
    />
  );
}
