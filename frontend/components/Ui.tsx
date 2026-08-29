"use client";

import { useMemo, useState, type ReactNode } from "react";
import type { ConfidenceBand, ExtractionStatus, PageClassification } from "@/lib/api";

// --- Badges ---------------------------------------------------------------

const BAND_STYLES: Record<ConfidenceBand, string> = {
  high: "bg-emerald-50 text-emerald-700 border-emerald-200",
  review: "bg-amber-50 text-amber-700 border-amber-200",
  low: "bg-rose-50 text-rose-700 border-rose-200",
};

const BAND_LABELS: Record<ConfidenceBand, string> = {
  high: "Confirmed",
  review: "Check",
  low: "Uncertain",
};

/** Certainty is shown as a word plus the underlying figure: someone deciding
 *  whether to trust a value needs both, since the word alone hides how close
 *  the reading was to the next band. */
export function ConfidenceBadge({
  band,
  value,
  verified,
}: {
  band: ConfidenceBand;
  value?: number;
  verified?: boolean | null;
}) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span
        className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium ${BAND_STYLES[band]}`}
      >
        {BAND_LABELS[band]}
        {value !== undefined && (
          <span className="tabular-nums opacity-70">{Math.round(value * 100)}%</span>
        )}
      </span>
      {verified === true && (
        <span
          title="Matches the drawing index"
          className="inline-flex items-center rounded-full border border-sky-200 bg-sky-50 px-1.5 py-0.5 text-[10px] font-semibold text-sky-700"
        >
          Matches index
        </span>
      )}
      {verified === false && (
        <span
          title="Differs from the drawing index"
          className="inline-flex items-center rounded-full border border-rose-300 bg-rose-50 px-1.5 py-0.5 text-[10px] font-semibold text-rose-700"
        >
          Differs
        </span>
      )}
    </span>
  );
}

const CLASSIFICATION_STYLES: Record<PageClassification, string> = {
  vector: "bg-blue-50 text-blue-700 border-blue-200",
  raster: "bg-amber-50 text-amber-700 border-amber-200",
  mixed: "bg-purple-50 text-purple-700 border-purple-200",
  unknown: "bg-rose-50 text-rose-700 border-rose-200",
};

export function ClassificationBadge({ classification }: { classification: PageClassification }) {
  return (
    <span
      className={`inline-block rounded-full border px-2 py-0.5 text-[11px] font-medium capitalize ${CLASSIFICATION_STYLES[classification]}`}
    >
      {classification}
    </span>
  );
}

const STATUS_STYLES: Record<ExtractionStatus, string> = {
  ok: "bg-emerald-50 text-emerald-700 border-emerald-200",
  partial: "bg-amber-50 text-amber-700 border-amber-200",
  failed: "bg-rose-50 text-rose-700 border-rose-200",
};

export function StatusBadge({ status }: { status: ExtractionStatus }) {
  return (
    <span
      className={`inline-block rounded-full border px-2 py-0.5 text-[11px] font-medium capitalize ${STATUS_STYLES[status]}`}
    >
      {status}
    </span>
  );
}

const PAGE_TYPE_STYLES: Record<string, string> = {
  floor_plan: "bg-blue-50 text-blue-700 border-blue-200",
  elevation: "bg-indigo-50 text-indigo-700 border-indigo-200",
  section: "bg-violet-50 text-violet-700 border-violet-200",
  schedule: "bg-cyan-50 text-cyan-700 border-cyan-200",
  detail: "bg-teal-50 text-teal-700 border-teal-200",
  site_plan: "bg-lime-50 text-lime-700 border-lime-200",
  cover: "bg-slate-100 text-slate-700 border-slate-300",
  notes: "bg-slate-100 text-slate-700 border-slate-300",
  unknown: "bg-rose-50 text-rose-700 border-rose-200",
};

export function PageTypeBadge({ type, label }: { type: string; label: string }) {
  return (
    <span
      className={`inline-block whitespace-nowrap rounded-full border px-2 py-0.5 text-[11px] font-medium ${
        PAGE_TYPE_STYLES[type] ?? PAGE_TYPE_STYLES.unknown
      }`}
    >
      {label}
    </span>
  );
}

/** A value that is not on the sheet. Shown in words rather than as a blank
 *  cell, so "nothing here" is never mistaken for "nothing to see". */
export function NotFound({ reason, label = "Not on sheet" }: { reason?: string; label?: string }) {
  return (
    <span
      title={reason}
      className="inline-flex items-center gap-1 text-[12px] font-medium text-slate-500"
    >
      <span className="h-1.5 w-1.5 rounded-full bg-slate-300" />
      {label}
    </span>
  );
}

// --- Layout ---------------------------------------------------------------

export function Card({
  title,
  subtitle,
  actions,
  children,
}: {
  title?: string;
  subtitle?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
      {(title || actions) && (
        <header className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 bg-slate-50/70 px-4 py-3">
          <div>
            {title && <h3 className="text-sm font-semibold text-slate-800">{title}</h3>}
            {subtitle && <p className="mt-0.5 text-xs text-slate-500">{subtitle}</p>}
          </div>
          {actions}
        </header>
      )}
      {children}
    </section>
  );
}

export function Stat({
  label,
  value,
  hint,
  tone = "default",
}: {
  label: string;
  value: ReactNode;
  hint?: string;
  tone?: "default" | "good" | "warn" | "bad";
}) {
  const toneClass = {
    default: "text-slate-900",
    good: "text-emerald-700",
    warn: "text-amber-700",
    bad: "text-rose-700",
  }[tone];
  return (
    <div className="rounded-lg border border-slate-200 bg-white px-3.5 py-3">
      <div className="text-[11px] font-medium uppercase tracking-wide text-slate-400">
        {label}
      </div>
      <div className={`mt-1 text-xl font-bold tabular-nums ${toneClass}`}>{value}</div>
      {hint && <div className="mt-0.5 text-[11px] leading-snug text-slate-500">{hint}</div>}
    </div>
  );
}

// --- Table ----------------------------------------------------------------

export interface Column<T> {
  key: string;
  header: string;
  /** Cell content. */
  render: (row: T) => ReactNode;
  /** Plain text used for searching and sorting; falls back to no sorting. */
  sortValue?: (row: T) => string | number;
  className?: string;
  width?: string;
}

/**
 * The one table used everywhere in this interface.
 *
 * Search and sort live here rather than in each screen so every table behaves
 * the same: someone who learns how the rooms table works already knows how the
 * dimensions table works. Sorting uses an explicit `sortValue` rather than the
 * rendered markup, so a numeric column sorts numerically.
 */
export function DataTable<T>({
  columns,
  rows,
  getRowKey,
  searchPlaceholder,
  emptyMessage = "Nothing found here.",
  onRowClick,
  rowClassName,
  initialSort,
  dense = false,
}: {
  columns: Column<T>[];
  rows: T[];
  getRowKey: (row: T) => string;
  searchPlaceholder?: string;
  emptyMessage?: string;
  onRowClick?: (row: T) => void;
  rowClassName?: (row: T) => string;
  initialSort?: { key: string; direction: "asc" | "desc" };
  dense?: boolean;
}) {
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState(initialSort ?? null);

  const searchable = useMemo(
    () =>
      rows.map((row) => ({
        row,
        haystack: columns
          .map((column) => (column.sortValue ? String(column.sortValue(row)) : ""))
          .join(" ")
          .toLowerCase(),
      })),
    [rows, columns]
  );

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    let out = needle
      ? searchable.filter((item) => item.haystack.includes(needle)).map((item) => item.row)
      : rows;
    if (sort) {
      const column = columns.find((c) => c.key === sort.key);
      if (column?.sortValue) {
        const factor = sort.direction === "asc" ? 1 : -1;
        out = [...out].sort((a, b) => {
          const left = column.sortValue!(a);
          const right = column.sortValue!(b);
          if (typeof left === "number" && typeof right === "number") {
            return (left - right) * factor;
          }
          return String(left).localeCompare(String(right)) * factor;
        });
      }
    }
    return out;
  }, [query, rows, searchable, sort, columns]);

  const padding = dense ? "px-3 py-1.5" : "px-3.5 py-2.5";

  return (
    <div>
      {searchPlaceholder && (
        <div className="flex items-center gap-2 border-b border-slate-100 px-3.5 py-2">
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={searchPlaceholder}
            className="w-full max-w-xs rounded-md border border-slate-200 px-2.5 py-1.5 text-sm text-slate-700 outline-none placeholder:text-slate-400 focus:border-blue-400"
          />
          <span className="text-xs tabular-nums text-slate-400">
            {visible.length} of {rows.length}
          </span>
        </div>
      )}
      <div className="overflow-x-auto">
        <table className="w-full min-w-full border-collapse text-left text-sm">
          <thead>
            <tr className="border-b border-slate-200 bg-slate-50/70">
              {columns.map((column) => {
                const isSorted = sort?.key === column.key;
                return (
                  <th
                    key={column.key}
                    style={column.width ? { width: column.width } : undefined}
                    className={`${padding} text-[11px] font-semibold uppercase tracking-wide text-slate-500 ${
                      column.sortValue ? "cursor-pointer select-none hover:text-slate-800" : ""
                    } ${column.className ?? ""}`}
                    onClick={
                      column.sortValue
                        ? () =>
                            setSort(
                              isSorted && sort?.direction === "asc"
                                ? { key: column.key, direction: "desc" }
                                : { key: column.key, direction: "asc" }
                            )
                        : undefined
                    }
                  >
                    {column.header}
                    {isSorted && (
                      <span className="ml-1 text-slate-400">
                        {sort?.direction === "asc" ? "▲" : "▼"}
                      </span>
                    )}
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {visible.length === 0 && (
              <tr>
                <td
                  colSpan={columns.length}
                  className="px-3.5 py-8 text-center text-sm text-slate-400"
                >
                  {emptyMessage}
                </td>
              </tr>
            )}
            {visible.map((row) => (
              <tr
                key={getRowKey(row)}
                onClick={onRowClick ? () => onRowClick(row) : undefined}
                className={`border-b border-slate-100 last:border-0 ${
                  onRowClick ? "cursor-pointer hover:bg-blue-50/50" : "hover:bg-slate-50/60"
                } ${rowClassName?.(row) ?? ""}`}
              >
                {columns.map((column) => (
                  <td
                    key={column.key}
                    className={`${padding} align-top text-slate-700 ${column.className ?? ""}`}
                  >
                    {column.render(row)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function Tabs({
  tabs,
  active,
  onChange,
}: {
  tabs: { id: string; label: string; count?: number; tone?: "default" | "alert" }[];
  active: string;
  onChange: (id: string) => void;
}) {
  return (
    <div className="flex flex-wrap gap-1 border-b border-slate-200">
      {tabs.map((tab) => {
        const isActive = tab.id === active;
        return (
          <button
            key={tab.id}
            type="button"
            onClick={() => onChange(tab.id)}
            className={`-mb-px inline-flex items-center gap-1.5 rounded-t-lg border-b-2 px-3.5 py-2 text-sm font-medium transition-colors ${
              isActive
                ? "border-blue-600 text-blue-700"
                : "border-transparent text-slate-500 hover:text-slate-800"
            }`}
          >
            {tab.label}
            {tab.count !== undefined && (
              <span
                className={`rounded-full px-1.5 py-0.5 text-[10px] font-semibold tabular-nums ${
                  tab.tone === "alert" && tab.count > 0
                    ? "bg-rose-100 text-rose-700"
                    : "bg-slate-100 text-slate-600"
                }`}
              >
                {tab.count}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}

export function DownloadLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <a
      href={href}
      className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 shadow-sm transition-colors hover:border-slate-300 hover:bg-slate-50"
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
      {children}
    </a>
  );
}
