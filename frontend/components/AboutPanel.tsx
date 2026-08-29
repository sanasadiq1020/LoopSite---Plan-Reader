"use client";

import { useEffect, useState } from "react";
import { fetchRelease, type ReleaseInfo } from "@/lib/api";

/**
 * What this tool is, and what it cannot do.
 *
 * Every word here is read from the product's own configuration rather than
 * written into the screen, so what the tool claims about itself lives in one
 * file. A limitation that is only in someone's head is not a limitation the
 * reader can act on.
 */
export function AboutPanel({ onClose }: { onClose: () => void }) {
  const [release, setRelease] = useState<ReleaseInfo | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetchRelease()
      .then((info) => !cancelled && setRelease(info))
      .catch(() => !cancelled && setFailed(true));
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-slate-900/50 p-4 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="my-6 w-full max-w-2xl overflow-hidden rounded-2xl bg-white shadow-2xl"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="flex items-start justify-between gap-4 border-b border-slate-200 px-6 py-4">
          <div>
            <h2 className="text-lg font-semibold text-slate-900">
              {release?.product ?? "About this tool"}
            </h2>
            {release && (
              <p className="mt-0.5 text-xs text-slate-500">
                Version {release.version} · released {release.released}
                {release.stage ? ` · ${release.stage}` : ""}
              </p>
            )}
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg border border-slate-200 px-3 py-1.5 text-sm font-medium text-slate-600 hover:bg-slate-50"
          >
            Close
          </button>
        </header>

        <div className="max-h-[70vh] overflow-y-auto px-6 py-5">
          {failed && (
            <p className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
              This build could not read its own release information, so it cannot say which
              version it is. Treat anything it produces with corresponding caution.
            </p>
          )}

          {!release && !failed && (
            <p className="text-sm text-slate-500">Loading…</p>
          )}

          {release && (
            <div className="flex flex-col gap-6">
              {release.what_it_does?.length > 0 && (
                <section>
                  <h3 className="text-xs font-semibold uppercase tracking-wider text-slate-400">
                    What it does
                  </h3>
                  <ul className="mt-2 space-y-1.5">
                    {release.what_it_does.map((item) => (
                      <li key={item} className="flex gap-2 text-sm leading-relaxed text-slate-700">
                        <span className="mt-1.5 h-1.5 w-1.5 flex-none rounded-full bg-blue-500" />
                        {item}
                      </li>
                    ))}
                  </ul>
                </section>
              )}

              {release.known_limitations?.length > 0 && (
                <section>
                  <h3 className="text-xs font-semibold uppercase tracking-wider text-slate-400">
                    Known limitations
                  </h3>
                  <p className="mt-1 text-xs text-slate-500">
                    Read these before relying on anything the tool produces.
                  </p>
                  <ul className="mt-2 space-y-2">
                    {release.known_limitations.map((item) => (
                      <li
                        key={item}
                        className="rounded-lg border border-amber-100 bg-amber-50/60 px-3 py-2 text-sm leading-relaxed text-slate-700"
                      >
                        {item}
                      </li>
                    ))}
                  </ul>
                </section>
              )}

              {release.not_in_this_release?.length > 0 && (
                <section>
                  <h3 className="text-xs font-semibold uppercase tracking-wider text-slate-400">
                    Not in this release
                  </h3>
                  <ul className="mt-2 flex flex-wrap gap-2">
                    {release.not_in_this_release.map((item) => (
                      <li
                        key={item}
                        className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1 text-xs text-slate-600"
                      >
                        {item}
                      </li>
                    ))}
                  </ul>
                </section>
              )}
            </div>
          )}
        </div>

        <footer className="border-t border-slate-200 bg-slate-50 px-6 py-4">
          <p className="text-xs leading-relaxed text-slate-600">
            <strong className="text-slate-800">For review, not for construction.</strong>{" "}
            Everything this tool produces is a draft to be checked by a qualified Australian
            construction professional. No certification, code compliance, engineering approval
            or structural adequacy is claimed or implied. Anything the tool could not establish
            is reported as not found — never guessed.
          </p>
        </footer>
      </div>
    </div>
  );
}
