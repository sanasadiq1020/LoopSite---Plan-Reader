"use client";

import { useRef, useState } from "react";

interface UploadPanelProps {
  onFileSelected: (file: File) => void;
  isProcessing: boolean;
}

/**
 * The first screen: one plan in, and what the reader gets back.
 *
 * The whole product is a single action, so the screen is built around it —
 * a target big enough to drop a file onto from anywhere, and, beneath it, a
 * plain statement of what will come back. The list is not decoration: someone
 * handed a link with no explanation needs to know what this does before they
 * hand it a drawing set.
 */

const WHAT_YOU_GET = [
  {
    title: "Every sheet, read",
    body: "Drawing number, title, scale, revision and what each sheet draws.",
    icon: "M9 12h6m-6 4h6m2 5H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5.586a1 1 0 0 1 .707.293l5.414 5.414a1 1 0 0 1 .293.707V19a2 2 0 0 1-2 2Z",
  },
  {
    title: "Rooms and dimensions",
    body: "Room names with their printed sizes, and every dimension figure on the sheet.",
    icon: "M3.75 3.75v4.5m0-4.5h4.5m-4.5 0L9 9M3.75 20.25v-4.5m0 4.5h4.5m-4.5 0L9 15M20.25 3.75h-4.5m4.5 0v4.5m0-4.5L15 9m5.25 11.25h-4.5m4.5 0v-4.5m0 4.5L15 15",
  },
  {
    title: "Walls, doors and windows",
    body: "Wall lines measured through the checked scale, with their thickness and length.",
    icon: "M2.25 21h19.5m-18-18v18m10.5-18v18m6-13.5V21M6.75 6.75h.75m-.75 3h.75m-.75 3h.75",
  },
  {
    title: "The sheet, marked up",
    body: "Everything found drawn over the original drawing, so you can check it by eye.",
    icon: "m2.25 15.75 5.159-5.159a2.25 2.25 0 0 1 3.182 0l5.159 5.159m-1.5-1.5 1.409-1.409a2.25 2.25 0 0 1 3.182 0l2.909 2.909M18 9h.008v.008H18V9Zm2.25 9A2.25 2.25 0 0 1 18 20.25H6A2.25 2.25 0 0 1 3.75 18V6A2.25 2.25 0 0 1 6 3.75h12A2.25 2.25 0 0 1 20.25 6v12Z",
  },
];

export function UploadPanel({
  onFileSelected,
  isProcessing,
}: UploadPanelProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [isDragOver, setIsDragOver] = useState(false);
  const [rejected, setRejected] = useState<string | null>(null);

  function handleFiles(files: FileList | null) {
    if (!files || files.length === 0) return;
    const file = files[0];
    if (!file.name.toLowerCase().endsWith(".pdf")) {
      // Said on the page rather than in a browser alert, which a reader has to
      // dismiss before they can try again.
      setRejected(
        `“${file.name}” is not a PDF. A plan set has to be a PDF file.`,
      );
      return;
    }
    setRejected(null);
    onFileSelected(file);
  }

  return (
    <section className="flex flex-col gap-8">
      <div className="text-center">
        <h2 className="text-2xl font-bold tracking-tight text-slate-900 sm:text-3xl">
          Read a construction plan
        </h2>
        <p className="mx-auto mt-2 max-w-xl text-sm leading-relaxed text-slate-600">
          Upload one approved Australian residential plan set. Every value comes
          back with the sheet and the place on it that it was read from, and how
          certain it is.
        </p>
      </div>

      <div className="grid items-start gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)]">
        <div className="order-2 lg:order-1">
          <p className="mb-3 text-xs font-semibold uppercase tracking-wider text-slate-400">
            What comes back
          </p>
          <div className="flex flex-col gap-3">
            {WHAT_YOU_GET.map((item) => (
              <div
                key={item.title}
                className="flex gap-3 rounded-xl border border-slate-200 bg-white p-4 shadow-sm"
              >
                <svg
                  xmlns="http://www.w3.org/2000/svg"
                  className="mt-0.5 h-5 w-5 flex-none text-blue-600"
                  fill="none"
                  viewBox="0 0 24 24"
                  stroke="currentColor"
                  strokeWidth={1.5}
                  aria-hidden="true"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    d={item.icon}
                  />
                </svg>
                <div>
                  <p className="text-sm font-semibold text-slate-800">
                    {item.title}
                  </p>
                  <p className="mt-0.5 text-xs leading-relaxed text-slate-500">
                    {item.body}
                  </p>
                </div>
              </div>
            ))}
          </div>
        </div>

        <div
          onDragOver={(e) => {
            e.preventDefault();
            setIsDragOver(true);
          }}
          onDragLeave={() => setIsDragOver(false)}
          onDrop={(e) => {
            e.preventDefault();
            setIsDragOver(false);
            handleFiles(e.dataTransfer.files);
          }}
          onClick={() => !isProcessing && inputRef.current?.click()}
          role="button"
          tabIndex={0}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") inputRef.current?.click();
          }}
          aria-label="Choose a plan PDF, or drop one here"
          className={`group order-1 flex h-full cursor-pointer flex-col items-center justify-center gap-5 rounded-2xl border-2 border-dashed px-6 py-14 text-center transition-all duration-200 lg:order-2 ${
            isDragOver
              ? "scale-[1.01] border-blue-500 bg-blue-50 shadow-md"
              : "border-slate-300 bg-gradient-to-b from-slate-50 to-white hover:border-blue-400 hover:bg-blue-50/40"
          }`}
        >
          <div
            className={`rounded-2xl p-4 transition-colors ${
              isDragOver ? "bg-blue-600" : "bg-blue-100 group-hover:bg-blue-200"
            }`}
          >
            <svg
              xmlns="http://www.w3.org/2000/svg"
              className={`h-9 w-9 transition-colors ${
                isDragOver ? "text-white" : "text-blue-600"
              }`}
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={1.5}
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M12 16.5V9.75m0 0 3 3m-3-3-3 3M6.75 19.5a4.5 4.5 0 0 1-1.41-8.775 5.25 5.25 0 0 1 10.233-2.33 3 3 0 0 1 3.758 3.848A3.752 3.752 0 0 1 18 19.5H6.75Z"
              />
            </svg>
          </div>

          <div>
            <p className="text-lg font-semibold text-slate-800">
              {isDragOver ? "Drop it here" : "Drop your plan PDF here"}
            </p>
            <p className="mt-1 text-sm text-slate-500">
              or click anywhere in this box to choose a file
            </p>
          </div>

          <input
            ref={inputRef}
            type="file"
            accept="application/pdf"
            className="hidden"
            onChange={(e) => handleFiles(e.target.files)}
          />

          <button
            type="button"
            disabled={isProcessing}
            onClick={(e) => {
              e.stopPropagation();
              inputRef.current?.click();
            }}
            className="rounded-lg bg-blue-600 px-6 py-2.5 text-sm font-semibold text-white shadow-sm transition-colors hover:bg-blue-700 disabled:cursor-not-allowed disabled:bg-slate-300"
          >
            {isProcessing ? "Reading…" : "Choose PDF"}
          </button>

          <p className="text-xs text-slate-400">
            PDF only · one plan set at a time · nothing is kept after you upload
            another
          </p>
        </div>
      </div>

      {rejected && (
        <p className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
          {rejected}
        </p>
      )}

      <p className="text-center text-xs leading-relaxed text-slate-400">
        Anything the reader could not work out is shown as not found, never
        guessed. Drafts are for review by a qualified Australian construction
        professional; no certification, code compliance or engineering approval
        is claimed.
      </p>
    </section>
  );
}
