"use client";

import {
  fieldLabel,
  pageTypeLabel,
  NOT_AVAILABLE,
  type CrossCheckReport,
  type ReadingMetrics,
  type SheetIndex,
} from "@/lib/api";
import { Card, DataTable, Stat, type Column } from "@/components/Ui";

/**
 * The summary of what was read from the document.
 *
 * Every figure here is worked out from the extracted values themselves:
 * "traced to source" counts the values that can be pointed back to a place on
 * a sheet, the index figures come from comparing each sheet against the
 * drawing index, and the dimension figure comes only from strings whose
 * arithmetic could actually be tested. Where a check could not run, it says
 * so rather than showing a flattering blank.
 */
export function RunOverview({
  metrics,
  crossCheck,
}: {
  metrics: ReadingMetrics;
  crossCheck: CrossCheckReport;
}) {
  const traceability = metrics.traceability.traceability_pct;

  return (
    <div className="flex flex-col gap-4">
      {/* What a plan reader came here for. The checks that used to sit here —
          index agreement, dimension arithmetic, scale confirmation — are not
          gone: they are on the sheet they belong to and in the issues log.
          Eight cards of them across the top told nobody anything. */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <Stat
          label="Sheets read"
          value={metrics.page_count}
          hint={
            metrics.pages_with_errors > 0
              ? `${metrics.pages_with_errors} could not be read`
              : "Every page processed"
          }
          tone={metrics.pages_with_errors > 0 ? "warn" : "good"}
        />
        <Stat
          label="Rooms & areas"
          value={metrics.records.rooms}
          hint="Named spaces found on the plan sheets"
        />
        <Stat
          label="Dimensions"
          value={metrics.records.dimensions.toLocaleString()}
          hint="Figures printed on the drawings"
        />
        <Stat
          label="Walls"
          value={metrics.walls ? metrics.walls.candidates : 0}
          hint={
            metrics.walls && metrics.walls.at_nominal_thickness_pct !== null
              ? `${metrics.walls.at_nominal_thickness} at a thickness normally built`
              : "Pairs of lines to review"
          }
        />
        <Stat
          label="Doors & windows"
          value={metrics.openings ? metrics.openings.distinct_openings : 0}
          hint={
            metrics.openings && metrics.openings.marks_on_drawings
              ? `marked ${metrics.openings.marks_on_drawings} times across the sheets`
              : "None marked on this plan"
          }
        />
        <Stat
          label="Traced to source"
          value={traceability === null ? NOT_AVAILABLE : `${traceability}%`}
          hint="Every value points back to a place on a sheet"
          tone={traceability !== null && traceability >= 90 ? "good" : "warn"}
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card
          title="Sheet details found"
          subtitle="Which details were read from each sheet, and how many the drawing index confirmed."
        >
          <DataTable
            dense
            columns={
              [
                {
                  key: "field",
                  header: "Detail",
                  render: (row) => (
                    <span className="font-medium text-slate-800">{fieldLabel(row.name)}</span>
                  ),
                  sortValue: (row) => row.name,
                },
                {
                  key: "found",
                  header: "Found on",
                  render: (row) => (
                    <span className="tabular-nums">
                      {row.found} of {row.pages} sheets
                    </span>
                  ),
                  sortValue: (row) => row.found,
                },
                {
                  key: "pct",
                  header: "Share",
                  render: (row) => (
                    <span
                      className={`tabular-nums font-semibold ${
                        (row.found_pct ?? 0) >= 90
                          ? "text-emerald-700"
                          : (row.found_pct ?? 0) >= 50
                            ? "text-amber-700"
                            : "text-slate-500"
                      }`}
                    >
                      {row.found_pct === null ? NOT_AVAILABLE : `${row.found_pct}%`}
                    </span>
                  ),
                  sortValue: (row) => row.found_pct ?? -1,
                },
                {
                  key: "verified",
                  header: "Confirmed by index",
                  render: (row) =>
                    row.verified + row.disagreed === 0 ? (
                      <span className="text-slate-400">Not compared</span>
                    ) : (
                      <span className="tabular-nums">
                        {row.verified}
                        {row.disagreed > 0 && (
                          <span className="ml-1 font-semibold text-rose-600">
                            ({row.disagreed} differ)
                          </span>
                        )}
                      </span>
                    ),
                  sortValue: (row) => row.verified,
                },
              ] as Column<PerFieldRow>[]
            }
            rows={Object.entries(metrics.title_block.per_field ?? {}).map(([name, stats]) => ({
              name,
              ...stats,
            }))}
            getRowKey={(row) => row.name}
          />
        </Card>

        <Card
          title="Sheet types"
          subtitle="Taken from each sheet's own title, then checked against what the sheet actually contains."
        >
          <div className="flex flex-wrap gap-2 p-4">
            {Object.entries(metrics.page_types ?? {}).map(([type, count]) => (
              <span
                key={type}
                className="inline-flex items-center gap-2 rounded-lg border border-slate-200 bg-slate-50 px-3 py-1.5 text-sm"
              >
                <span className="font-medium text-slate-700">{pageTypeLabel(type)}</span>
                <span className="rounded-full bg-white px-1.5 text-xs font-bold tabular-nums text-slate-600">
                  {count}
                </span>
              </span>
            ))}
          </div>
          <div className="border-t border-slate-100 px-4 py-3 text-xs leading-relaxed text-slate-500">
            {metrics.traceability.note}
          </div>
        </Card>
      </div>

      {crossCheck.findings.length > 0 && (
        <Card
          title="Differences from the drawing index"
          subtitle="Both values are kept and shown. Neither has been assumed correct."
        >
          <DataTable
            dense
            columns={
              [
                {
                  key: "sheet",
                  header: "Sheet",
                  render: (row) => (
                    <span className="font-mono text-xs font-semibold">{row.sheet_number}</span>
                  ),
                  sortValue: (row) => row.sheet_number ?? "",
                },
                {
                  key: "field",
                  header: "Detail",
                  render: (row) => fieldLabel(row.field),
                  sortValue: (row) => row.field,
                },
                {
                  key: "sheetvalue",
                  header: "On the sheet",
                  render: (row) => <span className="font-medium">{row.on_sheet}</span>,
                  sortValue: (row) => row.on_sheet ?? "",
                },
                {
                  key: "indexvalue",
                  header: "In the drawing index",
                  render: (row) => <span className="font-medium">{row.in_index}</span>,
                  sortValue: (row) => row.in_index ?? "",
                },
              ] as Column<CrossCheckReport["findings"][number]>[]
            }
            rows={crossCheck.findings}
            getRowKey={(row) => `${row.page_number}-${row.field}`}
          />
        </Card>
      )}
    </div>
  );
}


interface PerFieldRow {
  name: string;
  pages: number;
  found: number;
  verified: number;
  disagreed: number;
  found_pct: number | null;
}

/** The drawing index printed on the cover sheet — the list every sheet's own
 *  details were checked against. */
export function SheetIndexCard({ index }: { index: SheetIndex }) {
  return (
    <Card
      title="Drawing index"
      subtitle={`${index.entries.length} sheets listed on page ${index.source_page}. Each sheet's details were checked against this list.`}
    >
      <DataTable
        dense
        searchPlaceholder="Search the drawing index"
        columns={
          [
            {
              key: "number",
              header: "Drawing no.",
              width: "110px",
              render: (row) => (
                <span className="font-mono text-xs font-semibold text-slate-800">
                  {row.sheet_number ?? NOT_AVAILABLE}
                </span>
              ),
              sortValue: (row) => row.sheet_number ?? "",
            },
            {
              key: "title",
              header: "Drawing name",
              render: (row) => row.sheet_title ?? NOT_AVAILABLE,
              sortValue: (row) => row.sheet_title ?? "",
            },
            {
              key: "scale",
              header: "Scale",
              width: "100px",
              render: (row) => row.scale ?? NOT_AVAILABLE,
              sortValue: (row) => row.scale ?? "",
            },
            {
              key: "rev",
              header: "Revision",
              width: "90px",
              render: (row) => row.revision ?? NOT_AVAILABLE,
              sortValue: (row) => row.revision ?? "",
            },
          ] as Column<SheetIndex["entries"][number]>[]
        }
        rows={index.entries}
        getRowKey={(row) => `${row.sheet_number}-${row.source_bbox[1]}`}
      />
    </Card>
  );
}
