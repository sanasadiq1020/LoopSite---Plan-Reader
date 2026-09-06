"use client";

import { useEffect, useMemo, useState } from "react";
import {
  detectionOverlayUrl,
  fetchSelfCheck,
  type SelfCheckReport,
  type SelfCheckResult,
  type SelfCheckSheet,
} from "@/lib/api";

/**
 * Every sheet in the document, in order, with what was found drawn over it and
 * what the reading says about itself beside it.
 *
 * **Every sheet, including the ones that produced nothing.** A reader cannot
 * tell a drawing with nothing on it from a drawing this tool could not read, so
 * a sheet that produced no walls, rooms or dimensions still appears here, still
 * shows its own picture, and says on its face why it produced nothing. Absence
 * is shown, never omitted.
 *
 * **A check that did not run never reads as a pass.** Four outcomes are kept
 * apart on screen exactly as they are in the data: a check that does not apply
 * to this kind of sheet, a check that had nothing to compare against, a check
 * that ran and passed, and a check that ran and failed.
 */

const CHECK_TITLES: Record<string, string> = {
  scale: "Scale, against the sheet's own dimension strings",
  envelope: "Traced building, against the printed overall dimensions",
  openings: "Doors and windows, against whatever states them twice",
  closure: "How much of what was traced closes into a building",
  attrition: "Where the wall candidates went, stage by stage",
  provenance: "How each wall's thickness was arrived at",
  applicability: "What this kind of sheet can and cannot give",
};

const STATUS_WORDS: Record<string, string> = {
  pass: "Checked",
  fail: "Does not agree",
  not_applicable: "Does not apply to this sheet",
  unverifiable: "Nothing to check against",
};

const STATUS_STYLE: Record<string, string> = {
  pass: "bg-emerald-50 text-emerald-800 border-emerald-200",
  fail: "bg-rose-50 text-rose-800 border-rose-200",
  not_applicable: "bg-slate-100 text-slate-600 border-slate-200",
  unverifiable: "bg-amber-50 text-amber-800 border-amber-200",
};

function StatusChip({ status }: { status: string }) {
  return (
    <span
      className={`inline-block shrink-0 rounded border px-2 py-0.5 text-xs font-medium ${
        STATUS_STYLE[status] ?? STATUS_STYLE.not_applicable
      }`}
    >
      {STATUS_WORDS[status] ?? status}
    </span>
  );
}

/** The few figures worth showing beside a check, in a reader's words. */
function figuresFor(name: string, check: SelfCheckResult): string[] {
  const out: string[] = [];
  const num = (v: unknown) => (typeof v === "number" ? v : null);
  if (name === "scale") {
    const mm = num(check.measured_mm_per_point) ?? num(check.printed_mm_per_point);
    if (mm !== null) out.push(`${mm.toFixed(3)} mm per point`);
    const variance = num(check.variance_pct);
    if (variance !== null) out.push(`${variance.toFixed(1)}% from the printed scale`);
    if (typeof check.strings_used === "number") out.push(`${check.strings_used} strings used`);
  }
  if (name === "envelope") {
    const printed = check.printed_mm as Record<string, number | null> | undefined;
    const detected = check.detected_mm as Record<string, number | null> | undefined;
    const variance = check.variance_pct as Record<string, number | null> | undefined;
    (["x", "y"] as const).forEach((axis) => {
      const p = printed?.[axis];
      const d = detected?.[axis];
      const v = variance?.[axis];
      if (p != null && d != null) {
        out.push(
          `${axis === "x" ? "across" : "down"}: printed ${Math.round(p)} mm, traced ${Math.round(d)} mm${
            v != null ? ` (${v > 0 ? "+" : ""}${v.toFixed(1)}%)` : ""
          }`
        );
      }
    });
  }
  if (name === "openings") {
    if (check.tier_name) out.push(`checked against: ${String(check.tier_name)}`);
    if (typeof check.expected === "number") out.push(`expected ${check.expected}`);
    if (typeof check.found === "number") out.push(`found ${check.found}`);
    if (typeof check.placed === "number") out.push(`placed on a wall ${check.placed}`);
    if (typeof check.inter_evidence_agreement_pct === "number")
      out.push(`${check.inter_evidence_agreement_pct}% confirmed two ways or more`);
  }
  if (name === "closure") {
    if (typeof check.on_a_closed_loop === "number" && typeof check.walls === "number")
      out.push(`${check.on_a_closed_loop} of ${check.walls} walls close into a loop`);
    if (typeof check.dangling === "number") out.push(`${check.dangling} loose ends`);
  }
  if (name === "provenance") {
    if (typeof check.face_derived === "number")
      out.push(`${check.face_derived} measured from their own drawn faces`);
    if (typeof check.band_derived === "number") out.push(`${check.band_derived} from the band`);
    if (typeof check.averaged === "number") out.push(`${check.averaged} averaged across pieces`);
  }
  if (name === "attrition") {
    if (typeof check.traced === "number") out.push(`${check.traced} traced`);
    if (typeof check.kept === "number") out.push(`${check.kept} kept`);
  }
  return out;
}

function AttritionStages({ check }: { check: SelfCheckResult }) {
  const stages = (check.stages as Array<Record<string, number | string>> | undefined) ?? [];
  if (!stages.length) return null;
  return (
    <div className="mt-2 overflow-x-auto">
      <table className="w-full min-w-[420px] text-left text-xs">
        <thead className="text-slate-500">
          <tr>
            <th className="py-1 pr-3 font-medium">Stage</th>
            <th className="py-1 pr-3 text-right font-medium">Kept</th>
            <th className="py-1 pr-3 text-right font-medium">Set aside</th>
            <th className="py-1 text-right font-medium">Change</th>
          </tr>
        </thead>
        <tbody>
          {stages.map((stage, index) => {
            const change = Number(stage.kept_change ?? 0);
            return (
              <tr key={index} className="border-t border-slate-100">
                <td className="py-1 pr-3 text-slate-700">{String(stage.stage)}</td>
                <td className="py-1 pr-3 text-right tabular-nums">{String(stage.kept)}</td>
                <td className="py-1 pr-3 text-right tabular-nums text-slate-500">
                  {String(stage.set_aside)}
                </td>
                <td
                  className={`py-1 text-right tabular-nums ${
                    change < 0 ? "font-medium text-rose-700" : "text-slate-400"
                  }`}
                >
                  {change === 0 ? "—" : change}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function SheetCard({ sheet, runId }: { sheet: SelfCheckSheet; runId: string }) {
  const [open, setOpen] = useState(false);
  const checks = sheet.checks ?? {};
  const failing = Object.values(checks).filter((c) => c.status === "fail").length;
  const empty = Boolean(sheet.nothing_found_because);

  return (
    <article className="rounded-lg border border-slate-200 bg-white">
      <header className="flex flex-wrap items-baseline justify-between gap-2 border-b border-slate-100 px-4 py-3">
        <div>
          <h3 className="text-sm font-semibold text-slate-900">
            {sheet.sheet_id}
            {sheet.title ? ` — ${sheet.title}` : ""}
          </h3>
          <p className="text-xs text-slate-500">
            Read as a {sheet.sheet_type.replace(/_/g, " ")}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {empty && (
            <span className="rounded border border-amber-200 bg-amber-50 px-2 py-0.5 text-xs font-medium text-amber-800">
              Nothing found on this sheet
            </span>
          )}
          {failing > 0 && (
            <span className="rounded border border-rose-200 bg-rose-50 px-2 py-0.5 text-xs font-medium text-rose-800">
              {failing} {failing === 1 ? "check does" : "checks do"} not agree
            </span>
          )}
        </div>
      </header>

      {empty && (
        <p className="border-b border-slate-100 bg-amber-50/60 px-4 py-2 text-xs text-amber-900">
          {sheet.nothing_found_because}
        </p>
      )}

      <div className="px-4 py-3">
        {/* The overlay is the evidence. Every sheet has one, so a sheet that
            produced nothing still shows the drawing it produced nothing from. */}
        <a
          href={detectionOverlayUrl(runId, sheet.page_number)}
          target="_blank"
          rel="noreferrer"
          className="block"
        >
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={detectionOverlayUrl(runId, sheet.page_number)}
            alt={`What was found on ${sheet.sheet_id}, drawn over the sheet`}
            loading="lazy"
            className="w-full rounded border border-slate-200 bg-slate-50"
          />
        </a>

        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          className="mt-3 cursor-pointer text-xs font-medium text-sky-700 hover:underline"
        >
          {open ? "Hide the checks for this sheet" : "Show the checks for this sheet"}
        </button>

        {open && (
          <dl className="mt-3 flex flex-col gap-3">
            {Object.entries(checks).map(([name, check]) => (
              <div key={name} className="rounded border border-slate-100 bg-slate-50/60 p-3">
                <dt className="flex flex-wrap items-baseline justify-between gap-2">
                  <span className="text-xs font-semibold text-slate-800">
                    {CHECK_TITLES[name] ?? name}
                  </span>
                  <StatusChip status={check.status} />
                </dt>
                <dd className="mt-1 text-xs text-slate-600">{check.detail}</dd>
                {figuresFor(name, check).length > 0 && (
                  <dd className="mt-1 text-xs tabular-nums text-slate-500">
                    {figuresFor(name, check).join("  ·  ")}
                  </dd>
                )}
                {name === "attrition" && <AttritionStages check={check} />}
              </div>
            ))}
          </dl>
        )}
      </div>
    </article>
  );
}

export function SheetChecksPanel({ runId }: { runId: string }) {
  const [report, setReport] = useState<SelfCheckReport | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let live = true;
    setLoading(true);
    fetchSelfCheck(runId)
      .then((value) => {
        if (live) setReport(value);
      })
      .finally(() => {
        if (live) setLoading(false);
      });
    return () => {
      live = false;
    };
  }, [runId]);

  const sheets = useMemo(
    () => [...(report?.sheets ?? [])].sort((a, b) => a.page_number - b.page_number),
    [report]
  );

  if (loading) {
    return <p className="text-sm text-slate-500">Reading the checks for this plan…</p>;
  }
  if (!report) {
    return (
      <p className="rounded border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
        The checks could not be read for this plan, so nothing here has been verified. The
        sheets and tables are unaffected.
      </p>
    );
  }

  const document = report.document;
  const unavailable = Object.entries(document.outputs_unavailable ?? {});

  return (
    <div className="flex flex-col gap-5">
      <section className="rounded-lg border border-slate-200 bg-white p-4">
        <h2 className="text-sm font-semibold text-slate-900">
          What this document can and cannot give
        </h2>
        <p className="mt-1 text-xs text-slate-600">
          Every check below is worked out from this PDF alone — the sheet&apos;s own dimension
          strings, its own overall figures, its own schedule. Nothing was compared against an
          outside reference.
        </p>
        <dl className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
          {[
            ["Sheets", document.sheet_count],
            ["Drawing a plan", document.sheets_drawing_a_plan],
            ["With a confirmed scale", document.sheets_with_a_confirmed_scale],
            ["Schedule rows", document.schedule_rows_in_document],
          ].map(([label, value]) => (
            <div key={String(label)} className="rounded border border-slate-100 bg-slate-50 p-2">
              <dt className="text-xs text-slate-500">{label}</dt>
              <dd className="text-lg font-semibold tabular-nums text-slate-900">{value}</dd>
            </div>
          ))}
        </dl>
        {unavailable.length > 0 && (
          <div className="mt-3 rounded border border-amber-200 bg-amber-50 p-3">
            <h3 className="text-xs font-semibold text-amber-900">
              Not available from this document, and what it would take
            </h3>
            <ul className="mt-1 flex list-disc flex-col gap-1 pl-4 text-xs text-amber-900">
              {unavailable.map(([output, why]) => (
                <li key={output}>
                  <span className="font-medium">{output}</span> — {why}
                </li>
              ))}
            </ul>
          </div>
        )}
      </section>

      <div className="flex flex-col gap-4">
        {sheets.map((sheet) => (
          <SheetCard key={sheet.page_number} sheet={sheet} runId={runId} />
        ))}
      </div>
    </div>
  );
}
