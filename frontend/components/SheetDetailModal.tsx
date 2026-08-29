"use client";

import { useEffect, useMemo, useState } from "react";
import { fileUrl, pageTypeLabel, type PageReading, type SheetEntry } from "@/lib/api";
import { Card, PageTypeBadge, Tabs } from "@/components/Ui";
import {
  DimensionsTable,
  LegendTable,
  OpeningsTable,
  RoomsTable,
  ScheduleTableView,
  TitleBlockTable,
  WallsTable,
} from "@/components/Tables";

/**
 * Everything read from one sheet, alongside the sheet itself.
 *
 * The marked-up sheet opens first on purpose: it is the one view where
 * something missed or wrongly picked up can be seen directly, without relying
 * on any figure the reader produced. The tables behind it answer "what exactly
 * was read here".
 */
export function SheetDetailModal({
  sheet,
  reading,
  onClose,
}: {
  sheet: SheetEntry;
  reading: PageReading | null;
  onClose: () => void;
}) {
  const [tab, setTab] = useState("overlay");

  // Escape closes the sheet, the same way the Close button and the browser's
  // Back button do. The history entry that makes Back work is pushed by the
  // page when a sheet is opened, not here: an effect is invoked twice in
  // development, which pushed one entry and immediately unwound another, and
  // the sheet closed the instant it opened.
  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [onClose]);

  const roomNames = useMemo(() => {
    const map = new Map<string, string>();
    for (const room of reading?.rooms ?? []) map.set(room.room_id, room.name);
    return map;
  }, [reading]);

  const sheetLabel = sheet.sheet_number || `Page ${sheet.page_number}`;
  const decorate = <T,>(items: T[]) =>
    items.map((item) => ({
      ...item,
      sheetId: reading?.sheet_id ?? sheetLabel,
      pageNumber: sheet.page_number,
      sheetLabel,
    }));

  const scheduleRowCount = (reading?.schedules ?? []).reduce(
    (total, table) => total + table.rows.length,
    0
  );
  const legendEntryCount = (reading?.legends ?? []).reduce(
    (total, legend) => total + legend.entries.length,
    0
  );

  const tabs = [
    { id: "overlay", label: "Marked-up sheet" },
    { id: "titleblock", label: "Sheet details" },
    { id: "rooms", label: "Rooms & areas", count: reading?.rooms.length ?? 0 },
    { id: "dimensions", label: "Dimensions", count: reading?.dimensions.length ?? 0 },
    { id: "walls", label: "Walls", count: reading?.walls.length ?? 0 },
    { id: "schedules", label: "Schedules", count: scheduleRowCount },
    { id: "legends", label: "Legends", count: legendEntryCount },
    { id: "openings", label: "Doors & windows", count: reading?.openings.length ?? 0 },
  ];

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-slate-900/50 p-4 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="my-4 w-full max-w-7xl overflow-hidden rounded-2xl bg-slate-50 shadow-2xl"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="flex flex-wrap items-start justify-between gap-4 border-b border-slate-200 bg-white px-5 py-4">
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <span className="rounded-md bg-slate-900 px-2 py-0.5 font-mono text-xs font-bold text-white">
                {sheetLabel}
              </span>
              <h2 className="text-lg font-semibold text-slate-900">
                {sheet.sheet_title || "No sheet title printed"}
              </h2>
              {reading && (
                <PageTypeBadge
                  type={reading.page_type.value}
                  label={pageTypeLabel(reading.page_type.value)}
                />
              )}
            </div>
            <p className="mt-1 text-xs text-slate-500">
              Page {sheet.page_number} of the document
              {sheet.scale && (
                <>
                  {" "}
                  · scale {sheet.scale}
                  {reading?.scale_calibration?.result === "confirmed" && (
                    <span className="text-emerald-700"> (checked)</span>
                  )}
                  {reading?.scale_calibration?.result === "contradicted" && (
                    <span className="font-medium text-rose-700"> (does not match this sheet)</span>
                  )}
                  {reading?.scale_calibration?.result === "inconclusive" && (
                    <span className="font-medium text-amber-700"> (could not be checked)</span>
                  )}
                </>
              )}
              {sheet.revision && <> · revision {sheet.revision}</>}
              {reading?.page_type.note && (
                <span className="ml-1 text-amber-700">· {reading.page_type.note}</span>
              )}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm font-medium text-slate-600 hover:bg-slate-50"
          >
            Close
          </button>
        </header>

        <div className="bg-white px-5">
          <Tabs tabs={tabs} active={tab} onChange={setTab} />
        </div>

        <div className="max-h-[70vh] overflow-y-auto p-5">
          {!reading && <p className="text-sm text-slate-500">Nothing was read from this sheet.</p>}

          {reading && tab === "overlay" && (
            <div className="flex flex-col gap-3">
              <p className="text-xs leading-relaxed text-slate-500">
                Everything found on this sheet, marked over the original drawing. A solid
                outline is a confirmed value; a dashed outline is one worth checking. The key
                in the corner shows what each colour means.
              </p>
              {reading.overlay_url ? (
                <>
                  <a
                    href={`${fileUrl(reading.overlay_url)}?download=true`}
                    download={`marked-up-sheet-${sheetLabel.replace(/[^\w.-]+/g, "-")}.png`}
                    className="inline-flex w-fit items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 shadow-sm transition-colors hover:border-slate-300 hover:bg-slate-50"
                  >
                    <svg
                      xmlns="http://www.w3.org/2000/svg"
                      className="h-3.5 w-3.5 text-slate-500"
                      fill="none"
                      viewBox="0 0 24 24"
                      stroke="currentColor"
                      strokeWidth={2}
                    >
                      <path
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        d="M3 16.5v2.25A2.25 2.25 0 0 0 5.25 21h13.5A2.25 2.25 0 0 0 21 18.75V16.5M16.5 12 12 16.5m0 0L7.5 12m4.5 4.5V3"
                      />
                    </svg>
                    Download this sheet
                  </a>
                  <img
                    src={fileUrl(reading.overlay_url)}
                    alt={`Marked-up view of sheet ${sheetLabel}`}
                    className="w-full rounded-lg border border-slate-200 bg-white shadow-sm"
                  />
                </>
              ) : (
                <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
                  The marked-up view could not be produced for this sheet. The original page
                  is shown below.
                  <img
                    src={fileUrl(sheet.thumbnail_url)}
                    alt={`Page ${sheet.page_number}`}
                    className="mt-3 w-full rounded border border-slate-200 bg-white"
                  />
                </div>
              )}
            </div>
          )}

          {reading && tab === "titleblock" && (
            <Card
              title="Sheet details"
              subtitle="Each detail, where on the sheet it was read from, and whether the drawing index agreed."
            >
              <TitleBlockTable titleBlock={reading.title_block} />
            </Card>
          )}

          {reading && tab === "rooms" && (
            <Card
              title="Rooms and areas"
              subtitle="Found either by a recognised room name or by the size printed beneath the label. Each room shows which."
            >
              <RoomsTable rooms={decorate(reading.rooms)} />
            </Card>
          )}

          {reading && tab === "dimensions" && (
            <Card
              title="Dimensions"
              subtitle="Direction comes from the way each figure is printed — a figure turned on its side measures up the sheet."
            >
              <DimensionsTable dimensions={decorate(reading.dimensions)} roomNames={roomNames} />
            </Card>
          )}

          {reading && tab === "schedules" && (
            <div className="flex flex-col gap-4">
              {reading.schedules.length === 0 && (
                <p className="text-sm text-slate-500">
                  This sheet has no schedule table.
                </p>
              )}
              {reading.schedules.map((table) => (
                <Card
                  key={table.table_id}
                  title={table.caption}
                  subtitle={`${table.row_count} items${
                    table.caption_source === "sheet_title"
                      ? " · no heading printed, named from the sheet title"
                      : ""
                  }`}
                >
                  <ScheduleTableView table={table} />
                </Card>
              ))}
            </div>
          )}

          {reading && tab === "legends" && (
            <div className="flex flex-col gap-4">
              {reading.legends.length === 0 && (
                <p className="text-sm text-slate-500">This sheet has no legend.</p>
              )}
              {reading.legends.map((legend) => (
                <Card
                  key={legend.legend_id}
                  title={legend.caption}
                  subtitle={`${legend.entry_count} entries, exactly as printed on this sheet.`}
                >
                  <LegendTable legend={legend} />
                </Card>
              ))}
            </div>
          )}

          {reading && tab === "walls" && (
            <Card
              title="Wall candidates"
              subtitle="Pairs of parallel lines a wall's thickness apart, measured through this sheet's checked scale. Candidates for review, not confirmed walls."
            >
              <WallsTable walls={decorate(reading.walls)} />
            </Card>
          )}

          {reading && tab === "openings" && (
            <Card
              title="Doors and windows"
              subtitle="Each mark on this drawing joined to its schedule row, and placed on a wall where one is clearly the closest."
            >
              <OpeningsTable openings={decorate(reading.openings)} />
            </Card>
          )}

        </div>
      </div>
    </div>
  );
}
