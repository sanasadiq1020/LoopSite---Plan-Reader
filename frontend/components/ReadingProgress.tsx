"use client";

import type { UploadProgress } from "@/lib/api";

/**
 * What the reader sees while a plan is being read.
 *
 * A spinner says only "something is happening". On a twenty-three sheet plan
 * that runs for half a minute, and the reader cannot tell whether it is
 * working or stuck. So the sheets are counted off as they are read, and the
 * bar shows how much of the work is behind it.
 *
 * The time left is worked out where the sheets are counted, from how long the
 * ones already read actually took, and only once enough have gone through to
 * mean anything. A confident wrong number is worse than none.
 */
export function ReadingProgress({
  fileName,
  progress,
  timeLeft,
}: {
  fileName: string;
  progress: UploadProgress | null;
  timeLeft?: string | null;
}) {
  const percent = Math.max(0, Math.min(100, progress?.percent ?? 0));
  const done = progress?.pages_done ?? 0;
  const total = progress?.pages_total ?? 0;
  const stage = progress?.stage ?? "Opening the plan";

  return (
    <section
      aria-live="polite"
      className="rounded-2xl border border-blue-200 bg-gradient-to-b from-blue-50 to-white p-6 shadow-sm"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex items-center gap-3">
          <span className="h-5 w-5 flex-none animate-spin rounded-full border-2 border-blue-600 border-t-transparent" />
          <div>
            <p className="text-sm font-semibold text-slate-800">
              Reading {fileName || "the plan"}
            </p>
            <p className="mt-0.5 text-xs text-slate-500">{stage}</p>
          </div>
        </div>
        <div className="text-right">
          <p className="text-2xl font-bold tabular-nums text-blue-700">{percent}%</p>
          {total > 0 && (
            <p className="text-xs tabular-nums text-slate-500">
              {done} of {total} sheets
            </p>
          )}
        </div>
      </div>

      <div className="mt-4 h-2 w-full overflow-hidden rounded-full bg-blue-100">
        <div
          className="h-full rounded-full bg-blue-600 transition-[width] duration-500 ease-out"
          style={{ width: `${percent}%` }}
        />
      </div>

      <p className="mt-2 text-xs text-slate-500">
        {timeLeft ?? "Every sheet is read, measured and marked up before anything is shown."}
      </p>
    </section>
  );
}
