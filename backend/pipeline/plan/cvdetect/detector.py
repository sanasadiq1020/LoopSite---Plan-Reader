"""The five steps, run in order, over one sheet or one document.

Everything the package does in the order it has to happen:

1.  the drawing's own paths, with dashed and lightly plotted line work set
    aside (``vectorpaths``);
2.  what one point of this sheet measures, taken off the sheet's own dimension
    figures (``calibration``);
3.  the openings, found and painted white before anything is closed
    (``openings``);
4.  the wall bands, their outlines and their centrelines (``wallgeometry``);
5.  which wall each opening is in (``crosslink``).

**The order is the design, not an implementation detail.** Calibration comes
before every threshold because a threshold in millimetres is meaningless until
a millimetre has a size on this sheet. Openings come before walls because
closing a wall's gaps would seal a door shut, and once it is sealed nothing
downstream can know it was there.

**Nothing here raises.** Every stage is wrapped, every failure is logged, and a
stage that fails leaves the run holding whatever the earlier stages produced
(Critical Rule 6). A sheet that cannot be read comes back with its reason
attached, which is a result; an exception reaching the caller would not be.

Every stage is also timed, and the timings come back on the result. A reader
waiting on a 23-sheet upload is owed an answer about where the time went, and
guessing at it is how a third of an upload came to be spent drawing pictures
nobody had asked for.
"""

import time
from dataclasses import dataclass, field

import fitz

from app.logging_setup import get_logger
from pipeline.plan.cvdetect import calibration, crosslink, imaging, openings as openings_step
from pipeline.plan.cvdetect import vectorpaths, wallgeometry
from pipeline.plan.cvdetect.settings import load_settings

logger = get_logger()


@dataclass
class SheetReading:
    """Everything the five steps found on one sheet."""

    page_number: int
    sheet_name: str
    scale: object
    walls: list = field(default_factory=list)
    openings: list = field(default_factory=list)
    links: dict = field(default_factory=dict)
    path_counts: dict = field(default_factory=dict)
    diagnostics: dict = field(default_factory=dict)
    timings: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)

    def as_record(self) -> dict:
        return {
            "page_number": self.page_number,
            "sheet_name": self.sheet_name,
            "scale": self.scale.as_record() if self.scale else None,
            "walls": [w.as_record() for w in self.walls],
            "openings": [o.as_record() for o in self.openings],
            "openings_by_wall": {k: v for k, v in self.links.items() if v},
            "vector_paths": self.path_counts,
            "diagnostics": self.diagnostics,
            "seconds": {k: round(v, 3) for k, v in self.timings.items()},
            "notes": list(self.notes),
            "summary": self.summary(),
        }

    def summary(self) -> dict:
        placed = sum(1 for o in self.openings if o.wall_id)
        return {
            "walls": len(self.walls),
            "wall_metres": round(sum(w.length_mm for w in self.walls) / 1000.0, 1),
            "openings": len(self.openings),
            "openings_placed_on_a_wall": placed,
            "doors": sum(1 for o in self.openings if o.kind == "door"),
            "windows": sum(1 for o in self.openings if o.kind == "window"),
            "scale_established": bool(self.scale and self.scale.usable),
        }


def read_sheet(page, settings: dict = None, sheet_name: str = "", printed_scale: str = None,
               ocr_results: list = None) -> SheetReading:
    """Runs all five steps over one page and reports what they found."""
    settings = settings or load_settings()
    page_number = getattr(page, "number", 0) + 1
    reading = SheetReading(
        page_number=page_number,
        sheet_name=sheet_name or f"Page {page_number}",
        scale=None,
    )
    clock = {}

    def timed(name, work, fallback):
        started = time.perf_counter()
        try:
            return work()
        except Exception as e:
            logger.exception(f"read_sheet: {name} failed on page {page_number}: {e}")
            reading.notes.append(f"This sheet's {name} could not be completed; the failure is logged.")
            return fallback
        finally:
            clock[name] = time.perf_counter() - started

    dpi = timed("resolution", lambda: imaging.choose_dpi(page, settings), 300.0)

    paths = timed("vector paths", lambda: vectorpaths.parse_paths(page, settings),
                  vectorpaths.VectorPaths())
    reading.path_counts = paths.counts()
    reading.notes.extend(paths.notes)

    scale = timed(
        "scale calibration",
        lambda: calibration.calibrate_scale(
            page, ocr_results=ocr_results, settings=settings,
            printed_scale=printed_scale, dpi=dpi,
        ),
        None,
    )
    reading.scale = scale
    if scale is not None and scale.note:
        reading.notes.append(scale.note)
    if scale is None or not scale.usable:
        reading.timings = clock
        return reading

    ink = timed(
        "plan image",
        lambda: wallgeometry.build_ink(page, scale, paths, settings),
        (None, None),
    )
    ink_image, line_source = ink if isinstance(ink, tuple) else (None, None)

    found = timed(
        "openings",
        lambda: openings_step.detect_openings(page, scale, paths, settings),
        [],
    )
    reading.openings = found

    mask = timed(
        "openings mask",
        lambda: openings_step.openings_mask(page, scale, found, settings),
        None,
    )

    walls_and_notes = timed(
        "walls",
        lambda: wallgeometry.detect_walls(
            page, scale, paths, settings, openings_mask=mask,
            sheet_name=reading.sheet_name, ink=ink_image,
        ),
        ([], {}),
    )
    reading.walls, reading.diagnostics = walls_and_notes
    if line_source:
        reading.diagnostics.setdefault("line_source", line_source)
    reading.notes.extend(reading.diagnostics.get("notes", []))

    reading.links = timed(
        "cross-linking",
        lambda: crosslink.link_openings_to_walls(reading.walls, reading.openings, scale, settings),
        {},
    )

    reading.timings = clock
    logger.info(
        f"page {page_number}: {reading.summary()} in "
        f"{sum(clock.values()):.1f}s"
    )
    return reading


def read_document(pdf_path, settings: dict = None, pages: list = None,
                  password: str = "") -> dict:
    """Runs the five steps over a whole PDF and reports every sheet.

    ``pages`` is an optional list of 1-based page numbers; without it every
    page is read. A page that fails is reported as a page that failed, and the
    other twenty-two still come back.
    """
    settings = settings or load_settings()
    started = time.perf_counter()
    result = {
        "source": str(pdf_path),
        "sheets": [],
        "seconds": 0.0,
        "note": "",
    }

    document = None
    try:
        document = fitz.open(str(pdf_path))
        if document.needs_pass:
            # An owner password with an empty user password is very common on
            # drawings issued for tender, so that is tried before giving up.
            if not document.authenticate(password or ""):
                result["note"] = (
                    "This PDF is password protected and could not be opened. Supply the "
                    "password, or a copy of the drawing that is not locked."
                )
                return result
        if document.page_count == 0:
            result["note"] = "This PDF has no pages in it."
            return result

        wanted = pages or range(1, document.page_count + 1)
        for page_number in wanted:
            if not (1 <= page_number <= document.page_count):
                continue
            try:
                reading = read_sheet(document[page_number - 1], settings)
                result["sheets"].append(reading.as_record())
            except Exception as e:
                logger.exception(f"read_document: page {page_number} failed: {e}")
                result["sheets"].append(
                    {
                        "page_number": page_number,
                        "sheet_name": f"Page {page_number}",
                        "note": "This sheet could not be read; the failure is logged.",
                        "walls": [],
                        "openings": [],
                    }
                )
    except Exception as e:
        logger.exception(f"read_document: {pdf_path} could not be opened: {e}")
        result["note"] = "This file could not be opened as a PDF."
    finally:
        if document is not None:
            try:
                document.close()
            except Exception:
                pass

    result["seconds"] = round(time.perf_counter() - started, 2)
    result["totals"] = _totals(result["sheets"])
    return result


def _totals(sheets: list) -> dict:
    walls = sum(len(s.get("walls") or []) for s in sheets)
    found = sum(len(s.get("openings") or []) for s in sheets)
    placed = sum(
        1
        for s in sheets
        for o in (s.get("openings") or [])
        if o.get("wall_id")
    )
    return {
        "sheets": len(sheets),
        "walls": walls,
        "openings": found,
        "openings_placed_on_a_wall": placed,
        "sheets_with_a_measured_scale": sum(
            1 for s in sheets if (s.get("scale") or {}).get("usable_for_measurement")
        ),
    }
