"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ApiError,
  exportCsvUrl,
  getPlanReading,
  issuesCsvUrl,
  markedUpSheetsUrl,
  sheetRegisterCsvUrl,
  uploadPlan,
  ensureSession,
  API_BASE_URL_IN_USE,
  API_BASE_URL_IS_CONFIGURED,
  type PageReading,
  type PlanReadingResponse,
  type SheetEntry,
  type UploadResponse,
  discardRun,
  fetchRegister,
  readUploadProgress,
} from "@/lib/api";
import { Card, Tabs } from "@/components/Ui";
import { RunOverview, SheetIndexCard } from "@/components/RunOverview";
import {
  DimensionsTable,
  OpeningsTable,
  RoomsTable,
  ScheduleTableView,
  SheetRegisterTable,
  LegendTable,
  WallsTable,
  type DimensionRow,
  type OpeningRow,
  type RoomRow,
  type WallRow,
} from "@/components/Tables";
import { SheetDetailModal } from "@/components/SheetDetailModal";
import { UploadPanel } from "@/components/UploadPanel";
import { ReadingProgress } from "@/components/ReadingProgress";
import { DownloadMenu } from "@/components/DownloadMenu";
import { ModelPanel } from "@/components/ModelPanel";
import { AboutPanel } from "@/components/AboutPanel";
import type { UploadProgress } from "@/lib/api";

/** How a sheet is referred to on screen: its printed drawing number when it
 *  has one, otherwise its page in the document. */
function sheetLabelFor(page: PageReading): string {
  return page.title_block.sheet_number.value || "Page " + page.page_number;
}

// Where this tab remembers which plan it is showing, so a refresh picks it up
// again instead of returning an empty upload screen. Per tab, not per browser:
// two tabs may hold two different plans, which is exactly what happened when
// they could not.
const RUN_KEY = "loopsite.run_id";
const NAME_KEY = "loopsite.filename";

// How the bar is divided. Sending the file is real work the reader waits
// through — on a plan set of a few megabytes it is the first several seconds —
// so it gets its own stretch at the front instead of leaving the bar empty.
const SENDING_SHARE = 10;
const READING_SHARE = 0.9;

export default function Home() {
  const [isProcessing, setIsProcessing] = useState(false);
  const [restoring, setRestoring] = useState(false);
  const [progress, setProgress] = useState<UploadProgress | null>(null);
  const [timeLeft, setTimeLeft] = useState<string | null>(null);
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [showAbout, setShowAbout] = useState(false);

  // Only ever true on a deployed site: served from anywhere but localhost, with
  // no API address compiled in. Read once, lazily, so the first render on the
  // server and the first in the browser agree.
  const [misconfigured] = useState(() => {
    if (typeof window === "undefined") return false;
    const local = /^(localhost|127\.0\.0\.1|\[::1\])$/.test(window.location.hostname);
    return !API_BASE_URL_IS_CONFIGURED && !local;
  });
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<UploadResponse | null>(null);
  const [displayName, setDisplayName] = useState<string | null>(null);
  const [selectedPage, setSelectedPage] = useState<SheetEntry | null>(null);

  // An open sheet is its own step in the browser's history, so Back returns to
  // the sheet list instead of leaving the app and losing the run — which meant
  // uploading the plan again. The entry is pushed here, in the handler that
  // opens the sheet, because an effect runs twice in development.
  const openSheet = useCallback((sheet: SheetEntry) => {
    setSelectedPage(sheet);
    try {
      window.history.pushState({ loopsiteSheet: true }, "");
    } catch {
      // A browser that refuses the history entry still opens the sheet; only
      // the Back button behaves as it did before.
    }
  }, []);

  const closeSheet = useCallback(() => {
    if (window.history.state?.loopsiteSheet) window.history.back();
    else setSelectedPage(null);
  }, []);

  // **A refresh must not throw the plan away.** This used to discard the run
  // as the page unloaded, which cannot tell a refresh from a closed tab — so
  // pressing F5 deleted the uploaded plan on the server and returned an empty
  // upload screen, with the file needing to be uploaded all over again.
  //
  // The run id is remembered for this tab instead, and picked up again below.
  // Nothing accumulates on the server: a run is cleared once it is neither
  // recent nor recently touched.
  useEffect(() => {
    const runId = result?.run_id;
    if (!runId) return;
    try {
      sessionStorage.setItem(RUN_KEY, runId);
      if (displayName) sessionStorage.setItem(NAME_KEY, displayName);
    } catch {
      // A browser with storage turned off simply loses the plan on refresh,
      // exactly as before. Nothing else changes.
    }
  }, [result?.run_id, displayName]);

  // Picking the plan back up after a refresh. The server still holds it, so
  // the sheet register and the reading are asked for again — which is the same
  // pair of requests the upload itself finishes with.
  useEffect(() => {
    let cancelled = false;
    const restore = async () => {
      let runId = "";
      try {
        runId = sessionStorage.getItem(RUN_KEY) || "";
      } catch {
        return;
      }
      if (!runId) return;
      setRestoring(true);
      try {
        await ensureSession();
        const register = await fetchRegister(runId);
        if (cancelled) return;
        setResult(register);
        setDisplayName(register.original_filename);
        await loadPlanReading(runId);
      } catch {
        // The plan is no longer on the server — it was cleared, or the server
        // restarted. Say nothing alarming: the upload screen is the right
        // place to be, and the forgotten id must not be tried again.
        try {
          sessionStorage.removeItem(RUN_KEY);
          sessionStorage.removeItem(NAME_KEY);
        } catch {
          /* nothing to clean up */
        }
      } finally {
        if (!cancelled) setRestoring(false);
      }
    };
    void restore();
    return () => {
      cancelled = true;
    };
    // Runs once, when the page is opened.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const closeOnBack = () => setSelectedPage(null);
    window.addEventListener("popstate", closeOnBack);
    return () => window.removeEventListener("popstate", closeOnBack);
  }, []);
  const [tab, setTab] = useState("sheets");

  const [planReading, setPlanReading] = useState<PlanReadingResponse | null>(null);
  const [readingLoading, setReadingLoading] = useState(false);
  const [readingError, setReadingError] = useState<string | null>(null);

  // Sheets that produced nothing, each carrying the reason it produced
  // nothing. Kept together so the whole-plan case can be said once rather
  // than repeated on every row.
  const unreadableSheets = useMemo(
    () => (result?.sheets ?? []).filter((sheet) => Boolean(sheet.note)),
    [result]
  );

  const readingByPage = useMemo(() => {
    const map = new Map<number, PageReading>();
    for (const page of planReading?.pages ?? []) map.set(page.page_number, page);
    return map;
  }, [planReading]);

  // Flattened views across every sheet, so a whole run can be read as one
  // table — the fastest way to spot an outlier is to sort every dimension in
  // the set by value, not to open 23 sheets one at a time.
  const allRooms = useMemo<RoomRow[]>(
    () =>
      (planReading?.pages ?? []).flatMap((page) =>
        page.rooms.map((room) => ({
          ...room,
          sheetId: page.sheet_id,
          pageNumber: page.page_number,
          sheetLabel: sheetLabelFor(page),
        }))
      ),
    [planReading]
  );

  const allDimensions = useMemo<DimensionRow[]>(
    () =>
      (planReading?.pages ?? []).flatMap((page) =>
        page.dimensions.map((dimension) => ({
          ...dimension,
          sheetId: page.sheet_id,
          pageNumber: page.page_number,
          sheetLabel: sheetLabelFor(page),
        }))
      ),
    [planReading]
  );

  const allWalls = useMemo<WallRow[]>(
    () =>
      (planReading?.pages ?? []).flatMap((page) =>
        page.walls.map((wall) => ({
          ...wall,
          sheetId: page.sheet_id,
          pageNumber: page.page_number,
          sheetLabel: sheetLabelFor(page),
        }))
      ),
    [planReading]
  );

  const allOpenings = useMemo<OpeningRow[]>(
    () =>
      (planReading?.pages ?? []).flatMap((page) =>
        page.openings.map((opening) => ({
          ...opening,
          sheetId: page.sheet_id,
          pageNumber: page.page_number,
          sheetLabel: sheetLabelFor(page),
        }))
      ),
    [planReading]
  );

  const roomNames = useMemo(() => {
    const map = new Map<string, string>();
    for (const room of allRooms) map.set(room.room_id, room.name);
    return map;
  }, [allRooms]);

  const scheduleTables = useMemo(
    () =>
      (planReading?.pages ?? []).flatMap((page) =>
        page.schedules.map((table) => ({ page, table }))
      ),
    [planReading]
  );

  const legendBlocks = useMemo(
    () =>
      (planReading?.pages ?? []).flatMap((page) =>
        page.legends.map((legend) => ({ page, legend }))
      ),
    [planReading]
  );

  async function loadPlanReading(runId: string) {
    setReadingLoading(true);
    setReadingError(null);
    setPlanReading(null);
    try {
      setPlanReading(await getPlanReading(runId));
    } catch (err) {
      setReadingError(
        err instanceof ApiError
          ? err.message
          : "The results for this plan could not be loaded."
      );
    } finally {
      setReadingLoading(false);
    }
  }

  // The session is settled before anything is uploaded, so every request that
  // follows — including the images and downloads a browser fetches by URL —
  // carries it. Without this the tool works only in a browser that happens to
  // accept a third-party cookie, which is now the exception rather than the
  // rule.
  useEffect(() => {
    void ensureSession();
  }, []);

  async function handleFileSelected(file: File) {
    // The plan on screen is replaced by this one, so the old run is no longer
    // anything this tab can open. It goes with it — and only it.
    if (result) void discardRun(result.run_id);
    try {
      sessionStorage.removeItem(RUN_KEY);
      sessionStorage.removeItem(NAME_KEY);
    } catch {
      /* storage is optional */
    }

    setIsProcessing(true);
    setError(null);
    setResult(null);
    setPlanReading(null);
    setDisplayName(file.name);
    setTab("sheets");
    setProgress({ known: true, stage: "Sending the plan", percent: 0 });
    setTimeLeft(null);
    const startedAt = Date.now();
    setStartedAt(startedAt);
    await ensureSession();

    // **The bar never goes backwards.** The server reports the reading as
    // 0-100 of its own work, and the send before it is its own stretch; shown
    // raw, the bar reached 100 and then dropped to 96 as the results were laid
    // out. A bar that goes back tells the reader the tool lost its place.
    let highest = 0;
    const show = (update: UploadProgress) => {
      highest = Math.max(highest, update.percent ?? 0);
      setProgress({ ...update, percent: highest });
    };

    // The upload's own token, so the browser can ask how far it has got while
    // the upload request is still in flight.
    const token =
      typeof crypto !== "undefined" && "randomUUID" in crypto
        ? crypto.randomUUID()
        : `${Date.now()}-${Math.random().toString(16).slice(2)}`;

    let polling = true;
    const askHowFar = async () => {
      while (polling) {
        await new Promise((resolve) => setTimeout(resolve, 500));
        if (!polling) break;
        const update = await readUploadProgress(token);
        if (!update.known || !polling) continue;
        // The send is the first tenth of the bar; the reading is the rest.
        show({ ...update, percent: SENDING_SHARE + (update.percent ?? 0) * READING_SHARE });

        // Measured, not guessed: how long the sheets already read took says
        // how long the rest will take. Shown only once two have gone through.
        const done = update.pages_done ?? 0;
        const total = update.pages_total ?? 0;
        if (done >= 2 && total > done) {
          const seconds = Math.round(
            ((Date.now() - startedAt) / done) * (total - done) / 1000
          );
          setTimeLeft(
            seconds < 1
              ? null
              : seconds < 60
                ? `about ${seconds} second${seconds === 1 ? "" : "s"} left`
                : `about ${Math.ceil(seconds / 60)} minute${seconds >= 120 ? "s" : ""} left`
          );
        }
      }
    };
    void askHowFar();

    try {
      const response = await uploadPlan(file, token, (fractionSent) =>
        show({
          known: true,
          stage:
            fractionSent >= 1
              ? "Sent — the server is opening it"
              : `Sending the plan — ${Math.round(fractionSent * 100)}%`,
          percent: fractionSent * SENDING_SHARE,
        })
      );
      setResult(response);
      setDisplayName(response.original_filename);
      show({ known: true, stage: "Laying out the results", percent: 99 });
      await loadPlanReading(response.run_id);
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "This plan could not be processed."
      );
    } finally {
      polling = false;
      setIsProcessing(false);
      setProgress(null);
      setTimeLeft(null);
      setStartedAt(null);
    }
  }

  const tabs = [
    { id: "model", label: "3D model" },
    { id: "sheets", label: "Sheets", count: result?.sheets.length ?? 0 },
    { id: "rooms", label: "Rooms & areas", count: allRooms.length },
    { id: "dimensions", label: "Dimensions", count: allDimensions.length },
    { id: "walls", label: "Walls", count: allWalls.length },
    { id: "openings", label: "Doors & windows", count: allOpenings.length },
    {
      id: "schedules",
      label: "Schedules",
      count: scheduleTables.reduce((total, item) => total + item.table.rows.length, 0),
    },
    ...(planReading?.sheet_index
      ? [
          {
            id: "index",
            label: "Drawing index",
            count: planReading.sheet_index.entries.length,
          },
        ]
      : []),
    {
      id: "legends",
      label: "Legends",
      count: legendBlocks.reduce((total, item) => total + item.legend.entries.length, 0),
    },
  ];

  return (
    <main className="min-h-screen">
      <div className="mx-auto flex w-full max-w-[1500px] flex-col gap-7 px-6 py-9">
        <header className="flex flex-wrap items-center justify-between gap-4 border-b border-slate-200/80 pb-6">
          <div className="flex items-center gap-3.5">
            <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-gradient-to-br from-blue-600 to-indigo-700 text-white shadow-md shadow-blue-900/10">
              <svg
                xmlns="http://www.w3.org/2000/svg"
                className="h-6 w-6"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
                strokeWidth={1.75}
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="m3.75 9 8.25-6 8.25 6M4.5 9.75v9a1.5 1.5 0 0 0 1.5 1.5h3.75v-6h4.5v6H18a1.5 1.5 0 0 0 1.5-1.5v-9"
                />
              </svg>
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h1 className="text-xl font-bold tracking-tight text-slate-900">LoopSite</h1>
                <span className="rounded-full border border-blue-200 bg-blue-50 px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide text-blue-700">
                  Plan reading
                </span>
              </div>
              <p className="text-sm text-slate-500">
                Read an approved construction plan and check every value against the
                sheet it came from.
              </p>
            </div>
          </div>

          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => setShowAbout(true)}
              className="rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-sm font-medium text-slate-600 shadow-sm transition-colors hover:border-slate-400 hover:bg-slate-50"
            >
              About this tool
            </button>

            {result && (
            <button
              type="button"
              onClick={() => {
                // Nothing keeps a run once it is off the screen: the interface
                // shows one plan at a time and has no history to go back to.
                if (result) void discardRun(result.run_id);
                setResult(null);
                setPlanReading(null);
                setDisplayName(null);
                setTab("sheets");
              }}
              className="inline-flex items-center gap-2 rounded-lg bg-blue-600 px-4 py-2.5 text-sm font-semibold text-white shadow-sm transition-colors hover:bg-blue-700"
            >
              <svg
                xmlns="http://www.w3.org/2000/svg"
                className="h-4 w-4"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
                strokeWidth={2}
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M12 16.5V9.75m0 0 3 3m-3-3-3 3M6.75 19.5a4.5 4.5 0 0 1-1.41-8.775 5.25 5.25 0 0 1 10.233-2.33 3 3 0 0 1 3.758 3.848A3.752 3.752 0 0 1 18 19.5H6.75Z"
                />
              </svg>
              Upload another plan
            </button>
            )}
          </div>
        </header>

        {/* A build that was never told where its API is would otherwise fail in a
            way that looks like a broken plan: the upload reaches for a server on
            the visitor's own machine and simply never answers. It says so
            instead, and says what to do about it. */}
        {misconfigured && (
          <div className="rounded-xl border border-rose-200 bg-rose-50 px-4 py-3">
            <p className="text-sm font-semibold text-rose-900">
              This site does not know where its API is
            </p>
            <p className="mt-1 text-xs leading-relaxed text-rose-800">
              It was built without <code className="font-mono">NEXT_PUBLIC_API_BASE_URL</code>,
              so it is trying to reach <code className="font-mono">{API_BASE_URL_IN_USE}</code> —
              a server on your own computer, which is why nothing happens when you upload.
              Set that variable in the hosting platform and <strong>build the site
              again</strong>: setting it without rebuilding changes nothing, because the
              value is compiled in when the site is built.
            </p>
          </div>
        )}

        {/* While a plan is being read the bar goes first, above everything.
            Below the upload panel it only appeared once the reader scrolled,
            which is exactly when they are least likely to. */}
        {isProcessing && (
          <ReadingProgress
            fileName={displayName ?? ""}
            progress={progress}
            timeLeft={timeLeft}
            startedAt={startedAt}
          />
        )}

        {/* Picking the plan back up after a refresh. Shown instead of the
            upload panel, because offering to upload a plan that is already on
            the server is what made a refresh look like the plan was lost. */}
        {restoring && !result && (
          <div className="flex items-center gap-3 rounded-lg border border-blue-200 bg-blue-50 px-4 py-3 text-sm text-blue-800">
            <span className="h-4 w-4 animate-spin rounded-full border-2 border-blue-600 border-t-transparent" />
            Bringing your plan back…
          </div>
        )}

        {!result && !restoring && (
          <UploadPanel onFileSelected={handleFileSelected} isProcessing={isProcessing} />
        )}

        {error && (
          <div className="rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800">
            {error}
          </div>
        )}

        {readingError && (
          <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
            {readingError}
          </div>
        )}

        {/* Every sheet in the document came back with nothing on it. Said once,
            at the top, before the reader works their way through empty tables
            wondering whether the tool ran at all. */}
        {result && unreadableSheets.length === result.sheets.length && result.sheets.length > 0 && (
          <div className="rounded-lg border border-amber-300 bg-amber-50 px-4 py-3">
            <p className="text-sm font-semibold text-amber-900">
              No text could be read from this plan
            </p>
            <p className="mt-1 text-sm leading-relaxed text-amber-800">
              {unreadableSheets[0].note}
            </p>
            <p className="mt-2 text-sm leading-relaxed text-amber-800">
              The pages themselves are shown below, and each sheet can still be opened and
              downloaded. A version of this drawing exported with its text kept as text,
              rather than drawn as line work, would read in full.
            </p>
          </div>
        )}

        {result && (
          <section className="flex flex-col gap-5">
            <div className="flex flex-wrap items-end justify-between gap-3">
              <div>
                <h2 className="text-lg font-semibold text-slate-800">{displayName}</h2>
                <p className="text-xs text-slate-500">
                  {result.sheets.length} sheet{result.sheets.length === 1 ? "" : "s"}
                </p>
              </div>
              <DownloadMenu
                items={[
                  {
                    label: "Sheet list",
                    description: "Every sheet with its number, title, scale and type.",
                    href: sheetRegisterCsvUrl(result.run_id),
                  },
                  {
                    label: "Rooms & areas",
                    description: "Every named space, with its printed size where there is one.",
                    href: exportCsvUrl(result.run_id, "rooms"),
                  },
                  {
                    label: "Dimensions",
                    description: "Every figure printed on the drawings, with what it measures.",
                    href: exportCsvUrl(result.run_id, "dimensions"),
                  },
                  {
                    label: "Walls",
                    description: "Wall lines with their length, thickness and where measured from.",
                    href: exportCsvUrl(result.run_id, "walls"),
                  },
                  {
                    label: "Doors & windows",
                    description: "Every opening, its schedule row and the wall it sits in.",
                    href: exportCsvUrl(result.run_id, "openings"),
                  },
                  {
                    label: "Schedules",
                    description: "Every row of every schedule table on the plan.",
                    href: exportCsvUrl(result.run_id, "schedule-rows"),
                  },
                  {
                    label: "Marked-up sheets",
                    description: "All the drawings with everything found drawn over them.",
                    href: markedUpSheetsUrl(result.run_id),
                  },
                  {
                    label: "Items to check",
                    description: "Everything worth a second look, with the sheet and position.",
                    href: issuesCsvUrl(result.run_id),
                  },
                ]}
              />
            </div>

            {readingLoading && (
              <div className="flex items-center gap-2 text-sm text-slate-500">
                <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-blue-500 border-t-transparent" />
                Loading results…
              </div>
            )}

            {planReading && (
              <RunOverview
                metrics={planReading.metrics}
                crossCheck={planReading.cross_check}
              />
            )}

            <div>
              <Tabs tabs={tabs} active={tab} onChange={setTab} />
            </div>

            {tab === "model" && result && <ModelPanel runId={result.run_id} />}

            {tab === "sheets" && (
              <div className="flex flex-col gap-4">
                <Card
                  title="Sheets in this document"
                  subtitle="Every page, listed once. Select a row to see everything read from that sheet."
                >
                  <SheetRegisterTable sheets={result.sheets} onSelect={openSheet} />
                </Card>
              </div>
            )}

            {tab === "index" && planReading?.sheet_index && (
              <SheetIndexCard index={planReading.sheet_index} />
            )}

            {tab === "rooms" && (
              <Card
                title="Rooms and areas across all sheets"
                subtitle="Every named space on a plan sheet — rooms, and areas such as a garage, carport, verandah or deck. Found by the name printed on the drawing, or by the size printed beneath it; each row shows which."
              >
                <RoomsTable rooms={allRooms} showSheet />
              </Card>
            )}

            {tab === "dimensions" && (
              <Card
                title="Dimensions across all sheets"
                subtitle="Direction comes from the way each figure is printed on the drawing."
              >
                <DimensionsTable dimensions={allDimensions} roomNames={roomNames} showSheet />
              </Card>
            )}

            {tab === "walls" && (
              <Card
                title="Wall candidates"
                subtitle="Pairs of parallel lines a wall's thickness apart, measured through each sheet's checked scale. These are candidates for review, not confirmed walls."
              >
                <WallsTable walls={allWalls} showSheet />
              </Card>
            )}

            {tab === "openings" && (
              <Card
                title="Doors and windows"
                subtitle="Each mark on a drawing joined to its schedule row, and placed on a wall where one is clearly the closest."
              >
                {allOpenings.length === 0 ? (
                  <p className="px-4 py-8 text-center text-sm leading-relaxed text-slate-500">
                    Nothing was found because this plan does not mark its doors and windows.
                    <br />
                    A plan that does gives each opening a code such as D1 or W12 on the drawing
                    and repeats it in a door or window schedule. This one prints neither —
                    {scheduleTables.length === 0
                      ? " it has no schedule table at all,"
                      : " none of its schedules list openings,"}{" "}
                    and it describes its openings in words on the drawing instead.
                    <br />
                    <span className="text-slate-400">
                      Nothing has been missed here and nothing has been guessed.
                    </span>
                  </p>
                ) : (
                  <OpeningsTable openings={allOpenings} showSheet />
                )}
              </Card>
            )}

            {tab === "schedules" && (
              <div className="flex flex-col gap-4">
                {scheduleTables.length === 0 && (
                  <Card>
                    <p className="px-4 py-8 text-center text-sm leading-relaxed text-slate-500">
                      This document prints no schedule table.
                      <br />
                      A schedule is the table that lists every door or window with its size and
                      type, usually on its own sheet. Where a plan has one, every row of it is
                      read here and matched to the marks on the drawings.
                    </p>
                  </Card>
                )}
                {scheduleTables.map(({ page, table }) => (
                  <Card
                    key={table.table_id}
                    title={`${table.caption} — ${sheetLabelFor(page)}`}
                    subtitle={`${table.row_count} items`}
                  >
                    <ScheduleTableView table={table} />
                  </Card>
                ))}
              </div>
            )}

            {tab === "legends" && (
              <div className="flex flex-col gap-4">
                {legendBlocks.length === 0 && (
                  <Card>
                    <p className="px-4 py-8 text-center text-sm text-slate-500">
                      This document has no legends.
                    </p>
                  </Card>
                )}
                {legendBlocks.map(({ page, legend }) => (
                  <Card
                    key={`${page.page_number}-${legend.legend_id}`}
                    title={`${legend.caption} — ${sheetLabelFor(page)}`}
                    subtitle={`${legend.entry_count} entries, exactly as printed on the sheet.`}
                  >
                    <LegendTable legend={legend} />
                  </Card>
                ))}
              </div>
            )}

          </section>
        )}
      </div>

      <footer className="mx-auto mt-10 max-w-7xl px-4 pb-8 sm:px-6 lg:px-8">
        <p className="border-t border-slate-200 pt-4 text-center text-xs leading-relaxed text-slate-400">
          For review, not for construction. Everything here is a draft to be checked by a
          qualified Australian construction professional; no certification, code compliance
          or engineering approval is claimed.{" "}
          <button
            type="button"
            onClick={() => setShowAbout(true)}
            className="font-medium text-slate-500 underline underline-offset-2 hover:text-slate-700"
          >
            What this tool can and cannot do
          </button>
        </p>
      </footer>

      {showAbout && <AboutPanel onClose={() => setShowAbout(false)} />}

      {selectedPage && (
        <SheetDetailModal
          sheet={selectedPage}
          reading={readingByPage.get(selectedPage.page_number) ?? null}
          onClose={closeSheet}
        />
      )}
    </main>
  );
}
