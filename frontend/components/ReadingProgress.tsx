"use client";

import { useEffect, useState } from "react";
import type { UploadProgress } from "@/lib/api";

/**
 * What the reader sees while a plan is being read.
 *
 * A spinner says only "something is happening". A twenty-three sheet plan runs
 * for the better part of a minute, and the reader cannot tell working from
 * stuck. So the sheets are counted off as they are read, the bar shows how
 * much of the work is behind it, and the clock shows how long it has taken.
 *
 * **The bar slides between reports.** The server is asked every half second,
 * so left alone the bar would jump, wait, jump again. The sliding is done by
 * the browser's own transition rather than by a script: an animation driven by
 * `requestAnimationFrame` stops dead in a tab that is not being looked at, so
 * a reader who switched away and came back would find the bar frozen at
 * whatever it showed when they left. A transition keeps its promise whether
 * anyone is watching or not.
 *
 * The time left is measured, not guessed: how long the sheets already read
 * actually took says how long the rest will take, and it is shown only once
 * enough have gone through to mean anything. A confident wrong number is worse
 * than none.
 */
export function ReadingProgress({
  fileName,
  progress,
  timeLeft,
  startedAt,
}: {
  fileName: string;
  progress: UploadProgress | null;
  timeLeft?: string | null;
  startedAt?: number | null;
}) {
  // Counted here, once a second, rather than whenever the server happens to
  // answer — a clock that only moves when something else does looks stopped.
  const [elapsedSeconds, setElapsed] = useState(0);
  useEffect(() => {
    if (!startedAt) return;
    const tick = () => setElapsed(Math.round((Date.now() - startedAt) / 1000));
    tick();
    const timer = window.setInterval(tick, 1000);
    return () => window.clearInterval(timer);
  }, [startedAt]);
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
          <p className="text-2xl font-bold tabular-nums text-blue-700">
            {Math.round(percent)}%
          </p>
          {total > 0 && (
            <p className="text-xs tabular-nums text-slate-500">
              {done} of {total} sheets
            </p>
          )}
        </div>
      </div>

      <div className="mt-4 h-2 w-full overflow-hidden rounded-full bg-blue-100">
        <div
          className="h-full rounded-full bg-blue-600 transition-[width] duration-700 ease-linear"
          style={{ width: `${percent}%` }}
        />
      </div>

      <div className="mt-2 flex flex-wrap items-center justify-between gap-x-4 gap-y-1 text-xs text-slate-500">
        <span>
          {timeLeft ?? "Every sheet is read, measured and checked before anything is shown."}
        </span>
        <span className="tabular-nums">{formatElapsed(elapsedSeconds)}</span>
      </div>
    </section>
  );
}

function formatElapsed(seconds: number): string {
  if (seconds <= 0) return "";
  if (seconds < 60) return `${seconds}s elapsed`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${String(seconds % 60).padStart(2, "0")}s elapsed`;
}
