"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ApiError,
  buildSheetModel,
  fetchModellableSheets,
  formatMm,
  heightSourceLabel,
  modelFileUrl,
  type ModellableSheet,
  type ProjectModel,
} from "@/lib/api";
import { Card, ConfidenceBadge } from "@/components/Ui";
import { ModelViewer } from "@/components/ModelViewer";

/**
 * Day 5 — the 3D model screen.
 *
 * A plan set usually draws the same outline more than once: the floor plan,
 * the reflected-ceiling plan and the electrical plan are the same walls with
 * different things written on them. Only the reader knows which one they want
 * built, so the sheet is chosen here and the model is built on request.
 *
 * The left column is what the model is: which sheet, how tall, what it rests
 * on. The right column is the model itself. Clicking a wall in either one
 * highlights it in the other, because the point of the checkpoint is being
 * able to take one wall in the 3D view and trace it back to the sheet it was
 * measured from.
 */
export function ModelPanel({ runId }: { runId: string }) {
  const [sheets, setSheets] = useState<ModellableSheet[] | null>(null);
  const [chosenPage, setChosenPage] = useState<number | null>(null);
  const [model, setModel] = useState<ProjectModel | null>(null);
  const [building, setBuilding] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const usable = useMemo(
    () => (sheets ?? []).filter((s) => s.can_be_modelled),
    [sheets]
  );
  const notUsable = useMemo(
    () => (sheets ?? []).filter((s) => !s.can_be_modelled),
    [sheets]
  );

  const build = useCallback(
    async (pageNumber: number) => {
      setBuilding(true);
      setError(null);
      setSelectedId(null);
      try {
        setModel(await buildSheetModel(runId, pageNumber));
        setChosenPage(pageNumber);
      } catch (err) {
        setModel(null);
        setError(
          err instanceof ApiError ? err.message : "This sheet's model could not be built."
        );
      } finally {
        setBuilding(false);
      }
    },
    [runId]
  );

  // Which sheets can be modelled, and build the most likely one so the reader
  // sees a model rather than a form.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const result = await fetchModellableSheets(runId);
        if (cancelled) return;
        setSheets(result.sheets);
        if (result.default_page_number !== null) {
          await build(result.default_page_number);
        }
      } catch (err) {
        if (!cancelled) {
          setError(
            err instanceof ApiError ? err.message : "The sheet list could not be loaded."
          );
          setSheets([]);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [runId, build]);

  const storey = model?.storeys[0];
  const selectedWall = model?.walls.find((w) => w.element_id === selectedId) ?? null;

  // The openings that were not cut, grouped by the reason. One sentence
  // repeated twenty times is noise; the same sentence with a count beside it
  // is what a reader can act on.
  const notCut = useMemo(() => {
    const reasons = new Map<string, number>();
    for (const opening of model?.openings ?? []) {
      if (opening.geometry?.cut_as_void) continue;
      const reason = opening.not_cut_because;
      if (!reason) continue;
      reasons.set(reason, (reasons.get(reason) ?? 0) + 1);
    }
    return [...reasons.entries()].sort((a, b) => b[1] - a[1]);
  }, [model]);

  if (sheets === null) {
    return (
      <Card>
        <p className="flex items-center gap-2 px-4 py-8 text-sm text-slate-500">
          <span className="h-4 w-4 animate-spin rounded-full border-2 border-blue-600 border-t-transparent" />
          Looking at which sheets can be modelled…
        </p>
      </Card>
    );
  }

  if (usable.length === 0) {
    return (
      <Card title="No sheet in this plan can be built into a 3D model">
        <div className="px-4 py-5 text-sm leading-relaxed text-slate-600">
          <p>
            A sheet can be modelled when it draws the building in plan, its scale has been
            established, and wall lines were found on it. None of this plan&rsquo;s sheets
            meets all three. Each one says why:
          </p>
          <ul className="mt-3 space-y-1.5">
            {notUsable.slice(0, 8).map((sheet) => (
              <li key={sheet.page_number} className="text-xs text-slate-500">
                <span className="font-mono font-medium text-slate-700">{sheet.sheet_id}</span>{" "}
                {sheet.sheet_title ? `— ${sheet.sheet_title} ` : ""}
                <span className="text-slate-400">· {sheet.reason}</span>
              </li>
            ))}
          </ul>
        </div>
      </Card>
    );
  }

  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,20rem)_minmax(0,1fr)]">
      {/* --- left: which sheet, and what the model rests on --------------- */}
      <div className="flex flex-col gap-4">
        <Card
          title="Which sheet to build"
          subtitle="A plan set often draws the same walls more than once. Choose the one you want."
        >
          <div className="flex flex-col gap-2 p-3">
            {usable.map((sheet) => {
              const active = sheet.page_number === chosenPage;
              return (
                <button
                  key={sheet.page_number}
                  type="button"
                  disabled={building}
                  onClick={() => build(sheet.page_number)}
                  className={`rounded-lg border px-3 py-2.5 text-left transition-colors disabled:cursor-not-allowed ${
                    active
                      ? "border-blue-500 bg-blue-50 ring-1 ring-blue-500"
                      : "border-slate-200 bg-white hover:border-blue-300 hover:bg-blue-50/40"
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-mono text-xs font-bold text-slate-900">
                      {sheet.sheet_number || sheet.sheet_id}
                    </span>
                    <span className="text-xs text-slate-400">
                      page {sheet.page_number}
                    </span>
                  </div>
                  <p className="mt-0.5 truncate text-sm text-slate-700">
                    {sheet.sheet_title || "No title printed"}
                  </p>
                  <p className="mt-0.5 text-xs text-slate-500">
                    {sheet.wall_count} walls · {sheet.opening_count} doors &amp; windows ·{" "}
                    {sheet.room_count} rooms
                  </p>
                </button>
              );
            })}
          </div>

          {notUsable.length > 0 && (
            <details className="border-t border-slate-100 px-4 py-3">
              <summary className="cursor-pointer text-xs font-medium text-slate-500 hover:text-slate-700">
                {notUsable.length} sheet{notUsable.length === 1 ? "" : "s"} cannot be
                modelled — why
              </summary>
              <ul className="mt-2 space-y-1">
                {notUsable.map((sheet) => (
                  <li key={sheet.page_number} className="text-xs leading-snug text-slate-500">
                    <span className="font-mono text-slate-600">{sheet.sheet_id}</span>{" "}
                    {sheet.reason}
                  </li>
                ))}
              </ul>
            </details>
          )}
        </Card>

        {storey && (
          <Card title="How tall the walls are">
            <div className="px-4 py-4">
              <p className="text-3xl font-bold tabular-nums text-slate-900">
                {formatMm(storey.height_mm)}
              </p>
              <p className="mt-1 text-sm font-medium text-slate-700">
                {heightSourceLabel(storey.height_source)}
              </p>
              <p className="mt-2 text-xs leading-relaxed text-slate-500">
                {storey.height_note}
              </p>
              <p className="mt-3 text-xs leading-relaxed text-slate-400">
                A floor plan cannot show height — it is a horizontal cut. So the drawing set
                is asked for it two independent ways and the answers are compared.
              </p>
            </div>
          </Card>
        )}

        {model && model.openings_summary && (
          <Card title="Doors and windows in this model">
            <div className="px-4 py-4">
              <p className="text-3xl font-bold tabular-nums text-slate-900">
                {model.openings_summary.cut_as_voids}
                <span className="ml-1 text-base font-medium text-slate-500">
                  of {model.openings_summary.total} cut as real openings
                </span>
              </p>
              <p className="mt-2 text-xs leading-relaxed text-slate-500">
                {model.openings_summary.height_from_a_schedule > 0 && (
                  <>
                    {model.openings_summary.height_from_a_schedule} take their height from
                    this plan set&rsquo;s own schedule.{" "}
                  </>
                )}
                {model.openings_summary.height_from_the_office_default > 0 && (
                  <>
                    {model.openings_summary.height_from_the_office_default} use a standard
                    height because no schedule gives one — check those before measuring
                    anything from them.{" "}
                  </>
                )}
              </p>
              {notCut.length > 0 && (
                <div className="mt-3 border-t border-slate-100 pt-3">
                  <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">
                    Not cut, and why
                  </p>
                  <ul className="mt-2 space-y-1.5">
                    {notCut.map(([reason, count]) => (
                      <li key={reason} className="text-xs leading-relaxed text-slate-600">
                        <span className="font-semibold tabular-nums">{count}</span> — {reason}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              <p className="mt-3 text-xs leading-relaxed text-slate-400">
                A hole needs four things: which wall, where along it, how wide and how tall.
                A plan is a horizontal cut, so it can never show the last one — a schedule
                can. Anything the drawings do not establish is carried on the model with its
                wall and left uncut rather than guessed.
              </p>
            </div>
          </Card>
        )}

        {model && (
          <Card title="What this model rests on">
            <ul className="divide-y divide-slate-100">
              {model.assumptions.map((assumption) => (
                <li key={assumption.about} className="px-4 py-3">
                  <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">
                    {assumption.about}
                  </p>
                  <p className="mt-1 text-xs leading-relaxed text-slate-600">
                    {assumption.statement}
                  </p>
                </li>
              ))}
            </ul>
          </Card>
        )}
      </div>

      {/* --- right: the model itself ------------------------------------- */}
      <div className="flex flex-col gap-4">
        {error && (
          <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
            {error}
          </div>
        )}

        {building && (
          <div className="flex items-center gap-3 rounded-lg border border-blue-200 bg-blue-50 px-4 py-3 text-sm text-blue-800">
            <span className="h-4 w-4 animate-spin rounded-full border-2 border-blue-600 border-t-transparent" />
            Building the model from this sheet…
          </div>
        )}

        {model && chosenPage !== null && !building && (
          <>
            <Card
              title={`${model.modelled_sheet.sheet_number || model.modelled_sheet.sheet_id} — ${
                model.modelled_sheet.sheet_title || "3D model"
              }`}
              subtitle={`${model.walls.length} walls · ${
                model.openings_summary?.cut_as_voids ?? 0
              } doors and windows cut into them · ${(model.extent_mm.x / 1000).toFixed(1)} m × ${(
                model.extent_mm.y / 1000
              ).toFixed(1)} m × ${(model.extent_mm.z / 1000).toFixed(2)} m tall · millimetres`}
            >
              <div className="p-3">
                <ModelViewer
                  url={modelFileUrl(runId, chosenPage, "glb")}
                  onWallSelected={setSelectedId}
                  selectedId={selectedId}
                />
              </div>

              <div className="flex flex-wrap items-center gap-2 border-t border-slate-100 px-4 py-3">
                <span className="text-xs font-medium text-slate-500">Download the model</span>
                {(
                  [
                    ["ifc", "IFC", "The construction industry's exchange format"],
                    ["glb", "GLB", "Opens in any 3D viewer"],
                    ["obj", "OBJ", "Plain text, opens anywhere"],
                    ["json", "project_model.json", "The canonical data itself"],
                  ] as const
                ).map(([kind, label, hint]) => (
                  <a
                    key={kind}
                    href={modelFileUrl(runId, chosenPage, kind)}
                    title={hint}
                    className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 shadow-sm transition-colors hover:border-slate-300 hover:bg-slate-50"
                  >
                    {label}
                  </a>
                ))}
              </div>
            </Card>

            <Card
              title={selectedWall ? "The wall you selected" : "Every wall in this model"}
              subtitle={
                selectedWall
                  ? "Where it came from on the sheet, and what was measured rather than assumed."
                  : "Select a row, or click a wall in the model, to trace it back to the drawing."
              }
            >
              {selectedWall ? (
                <div className="px-4 py-4">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="rounded-md bg-slate-900 px-2 py-0.5 font-mono text-xs font-bold text-white">
                      {selectedWall.element_id}
                    </span>
                    <ConfidenceBadge
                      band={selectedWall.confidence_band}
                      value={selectedWall.confidence}
                    />
                    <button
                      type="button"
                      onClick={() => setSelectedId(null)}
                      className="ml-auto text-xs font-medium text-blue-700 hover:underline"
                    >
                      Show all walls
                    </button>
                  </div>

                  <dl className="mt-4 grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-3">
                    {(
                      [
                        ["Length", formatMm(selectedWall.dimensions.length_mm)],
                        [
                          "Thickness",
                          `${formatMm(selectedWall.dimensions.thickness_mm)}${
                            selectedWall.dimensions.thickness_is_measured ? "" : " (assumed)"
                          }`,
                        ],
                        ["Height", formatMm(selectedWall.dimensions.height_mm)],
                        ["Read from sheet", selectedWall.source_sheet],
                        [
                          "Position",
                          `${(selectedWall.geometry.start_mm[0] / 1000).toFixed(2)} m, ${(
                            selectedWall.geometry.start_mm[1] / 1000
                          ).toFixed(2)} m`,
                        ],
                        [
                          "Doors & windows",
                          selectedWall.linked_opening_ids.length
                            ? `${selectedWall.linked_opening_ids.length} in this wall`
                            : "none placed",
                        ],
                      ] as const
                    ).map(([label, value]) => (
                      <div key={label}>
                        <dt className="text-xs font-medium uppercase tracking-wide text-slate-400">
                          {label}
                        </dt>
                        <dd className="mt-0.5 text-sm font-medium tabular-nums text-slate-800">
                          {value}
                        </dd>
                      </div>
                    ))}
                  </dl>

                  {selectedWall.assumptions.length > 0 && (
                    <ul className="mt-4 space-y-1 border-t border-slate-100 pt-3">
                      {selectedWall.assumptions.map((note) => (
                        <li key={note} className="text-xs leading-relaxed text-amber-700">
                          {note}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              ) : (
                <div className="max-h-80 overflow-y-auto">
                  <table className="w-full text-sm">
                    <thead className="sticky top-0 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                      <tr>
                        <th className="px-4 py-2 text-left font-medium">Wall</th>
                        <th className="px-4 py-2 text-right font-medium">Length</th>
                        <th className="px-4 py-2 text-right font-medium">Thickness</th>
                        <th className="px-4 py-2 text-left font-medium">Certainty</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100">
                      {model.walls.map((wall) => (
                        <tr
                          key={wall.element_id}
                          onClick={() => setSelectedId(wall.element_id)}
                          className="cursor-pointer hover:bg-blue-50/60"
                        >
                          <td className="px-4 py-2 font-mono text-xs text-slate-700">
                            {wall.element_id}
                          </td>
                          <td className="px-4 py-2 text-right tabular-nums">
                            {formatMm(wall.dimensions.length_mm)}
                          </td>
                          <td className="px-4 py-2 text-right tabular-nums text-slate-600">
                            {formatMm(wall.dimensions.thickness_mm)}
                          </td>
                          <td className="px-4 py-2">
                            <ConfidenceBadge
                              band={wall.confidence_band}
                              value={wall.confidence}
                            />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Card>
          </>
        )}
      </div>
    </div>
  );
}
