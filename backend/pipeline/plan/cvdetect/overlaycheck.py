"""Reading the overlay back off the picture, pixel by pixel.

**Nearly every defect this reader has had was found by looking at an overlay,
not by reading a count** - printed words traced as walls, a 17.9 m "wall" that
was two dimension strings, the car drawn in the garage, a door on a cooktop.
Twice the counts actively hid the problem. They cannot do otherwise: every
number beside the picture is computed from the same records the picture is
drawn from, so a figure and its drawing can never disagree, however wrong both
are.

So this looks at the picture, and asks the two questions a reviewer asks when
they open one:

*   **Is anything drawn as a wall out where the building is not?** Wall ink in
    the margin, above the roof line, out past the carport, is a rafter, a
    boundary or a setting-out tick reported as a wall of the house. Where the
    building is drawn is taken from the sheet's own room labels and dimension
    strings (``walls.drawing_region``) - deliberately **not** from the walls
    that were kept, because a box drawn round the kept walls contains them by
    definition, and a test its subject cannot fail is not a test.

*   **Is every opening actually broken through?** A door is where the wall
    stops and starts again, so along the middle of an opening there should be
    no wall ink at all. If there is, the centreline was drawn straight through
    its own doorway.

**A run of ink, not a pixel of a colour.** A wall is drawn as a rectangle
outline several pixels wide and at least as long as the shortest wall the
reader will report; a label is drawn in the same colour and is a few glyphs.
Counting coloured pixels calls the labels a failure - measured, 118 runs of
eight to ten pixels each on one sheet, every one of them lettering. So the ink
is grouped into connected runs and only a run long enough to be a wall is
judged.

Set-aside candidates are drawn dashed red and detached structures grey, and
neither is looked at here: being drawn *as something else* is the whole point
of drawing them differently.
"""

import json
from pathlib import Path

from app.logging_setup import get_logger

logger = get_logger()

# Drawn as a wall of this building - the three colours the overlay uses for a
# wall it kept. Grey (a detached structure) and red (a candidate set aside) are
# deliberately absent.
WALL_COLOURS = ("#1D4ED8", "#15803D", "#7C3AED")

# How near a pixel has to be to a colour to count as that colour. The overlay is
# anti-aliased, so an edge pixel is a blend, but a line three pixels wide has a
# core drawn in the colour exactly - so this only has to be wide enough for the
# PNG round trip. It is deliberately tight: one plan set prints its own text in
# a pure blue that sits 39 apart from the outer-wall blue, and at 40 the sheet's
# own lettering read as walls.
_NEAR = 24


def _rgb(colour: str):
    colour = colour.lstrip("#")
    return tuple(int(colour[i:i + 2], 16) for i in (0, 2, 4))


def check_sheet(png_path, page_reading: dict, page_rect, config: dict,
                plain_page=None) -> dict:
    """Both acceptance criteria, read off one rendered overlay.

    ``page_rect`` is the source page's own rectangle, which is what the overlay
    was rendered from - its width gives the pixels per point and its corner the
    origin, exactly as the renderer used them. Never raises: a picture that
    cannot be read is reported as not checked, which is a result rather than a
    failure of the run (Critical Rule 6).
    """
    result = {
        "sheet": page_reading.get("sheet_id") or page_reading.get("page_number"),
        "page": page_reading.get("page_number"),
        "png": Path(png_path).name,
        "checked": False,
        "note": "",
        "wall_runs_outside_the_drawing": None,
        "outside_at": [],
        "openings_checked": 0,
        "openings_drawn_through": 0,
        "drawn_through": [],
    }
    try:
        import numpy as np
        from PIL import Image
    except Exception as e:
        result["note"] = f"The picture cannot be read here: {e}"
        return result

    try:
        image = np.asarray(Image.open(png_path).convert("RGB")).astype(int)
    except Exception as e:
        logger.exception(f"overlay check: {png_path} could not be opened: {e}")
        result["note"] = "This picture could not be opened."
        return result

    try:
        scale = image.shape[1] / float(page_rect[2] - page_rect[0])
        origin = (float(page_rect[0]), float(page_rect[1]))
    except Exception:
        result["note"] = "This sheet has no size to measure the picture against."
        return result

    wall_ink = _wall_ink(image, plain_page, np)
    result["checked"] = True

    shortest = _shortest_wall_points(page_reading, config)
    region, region_note = _where_the_building_is_drawn(page_reading, page_rect)
    if region is None:
        result["note"] = region_note
    else:
        runs = _runs_outside(wall_ink, region, origin, scale, shortest * scale, np)
        result["wall_runs_outside_the_drawing"] = len(runs)
        result["outside_at"] = runs[:6]

    checked, through, where = _openings_drawn_through(
        wall_ink, page_reading, origin, scale, np
    )
    result["openings_checked"] = checked
    result["openings_drawn_through"] = through
    result["drawn_through"] = where
    return result


def _wall_ink(image, plain_page, np):
    """Every pixel the overlay drew in one of a kept wall's three colours.

    **The overlay drew, not the sheet.** A drawing is not printed in black: one
    plan set rules its frame and its title strip in a blue close enough to the
    outer-wall colour to match it, and reading those as walls put four "walls"
    out in the margin of a sheet that had none. So the sheet is rendered plain
    at the same size and only pixels the overlay changed are counted - which is
    the difference between what the reader drew and what the drawing already
    said.
    """
    mask = np.zeros(image.shape[:2], dtype=bool)
    for colour in WALL_COLOURS:
        target = np.array(_rgb(colour))
        mask |= np.abs(image - target).max(axis=2) <= _NEAR
    if plain_page is not None and plain_page.shape == image.shape:
        mask &= np.abs(image - plain_page).max(axis=2) > _NEAR
    return mask


def _shortest_wall_points(page_reading: dict, config: dict) -> float:
    """The shortest wall this reader will report on this sheet, in points.

    Below this a run of ink cannot be a wall line, so it is lettering, the edge
    of a junction dot, or a stray blend. Taken through the sheet's own scale, so
    it means the same on a 1:50 detail and a 1:200 site plan.
    """
    calibration = page_reading.get("scale_calibration") or {}
    mm_per_point = (
        calibration.get("measured_mm_per_point")
        or calibration.get("printed_mm_per_point")
    )
    try:
        from pipeline.plan.cvdetect.settings import load_settings, number

        minimum_mm = number(load_settings(), "wall.min_length_mm", 600.0)
    except Exception:
        minimum_mm = 600.0
    try:
        return float(minimum_mm) / float(mm_per_point)
    except (TypeError, ValueError, ZeroDivisionError):
        # No scale on this sheet. A wall line is still a line, and the shortest
        # one the overlay can draw is longer on the paper than a printed word.
        return 20.0


def _where_the_building_is_drawn(page_reading: dict, page_rect):
    """The region of the sheet the plan occupies, from the sheet's own text.

    Reuses the reader's own definition - the room labels bound it from inside
    and the dimension strings printed outside them bound it from outside,
    because a building is drawn *between* its dimension strings and never
    through them. Independent of which candidates were kept, which is what makes
    it able to fail.
    """
    from pipeline.plan import walls as legacy

    width = float(page_rect[2] - page_rect[0])
    height = float(page_rect[3] - page_rect[1])
    try:
        region = legacy.drawing_region(
            page_reading.get("rooms") or [],
            page_reading.get("dimension_chains") or [],
            width, height,
        )
    except Exception as e:
        logger.exception(f"overlay check: the drawing region failed: {e}")
        return None, "Where the building is drawn could not be worked out on this sheet."

    if (region[2] - region[0]) >= width - 1 and (region[3] - region[1]) >= height - 1:
        return None, (
            "This sheet prints no room labels with dimension strings outside them, so "
            "there is nothing on it that says where the building is drawn."
        )
    return region, ""


def _runs_outside(wall_ink, region, origin, scale, shortest_px, np):
    """Runs of wall ink lying outside the drawing, longest first.

    A few pixels of margin are allowed on each side, because the outermost wall
    sits on the edge of the drawing by definition and a line drawn three pixels
    wide must not read as ink beyond it.
    """
    margin = 4
    x0 = int((region[0] - origin[0]) * scale) - margin
    y0 = int((region[1] - origin[1]) * scale) - margin
    x1 = int((region[2] - origin[0]) * scale) + margin
    y1 = int((region[3] - origin[1]) * scale) + margin

    outside = wall_ink.copy()
    outside[max(0, y0):max(0, y1), max(0, x0):max(0, x1)] = False
    if not outside.any():
        return []

    found = []
    for box, size in _connected_runs(outside, np):
        run = max(box[2] - box[0], box[3] - box[1])
        if run < shortest_px:
            continue
        found.append({
            "run_pt": round(run / scale, 1),
            "ink_px": int(size),
            "at_pt": [round(box[0] / scale + origin[0], 1),
                      round(box[1] / scale + origin[1], 1)],
        })
    found.sort(key=lambda run: -run["run_pt"])
    return found


def _connected_runs(mask, np):
    """Each connected run of ink as (x0, y0, x1, y1) in pixels, with its size."""
    try:
        import cv2

        count, _labels, stats, _centres = cv2.connectedComponentsWithStats(
            mask.astype("uint8"), connectivity=8
        )
        return [
            ((int(stats[i, 0]), int(stats[i, 1]),
              int(stats[i, 0] + stats[i, 2]), int(stats[i, 1] + stats[i, 3])),
             int(stats[i, 4]))
            for i in range(1, count)
        ]
    except Exception as e:
        logger.exception(f"overlay check: the ink could not be grouped into runs: {e}")
        rows, columns = np.nonzero(mask)
        if not rows.size:
            return []
        return [((int(columns.min()), int(rows.min()),
                  int(columns.max()), int(rows.max())), int(rows.size))]


def _openings_drawn_through(wall_ink, page_reading, origin, scale, np):
    """Whether any opening has its own wall drawn straight across it.

    **Ink in the doorway is not the question; ink running *along the wall*
    through the doorway is.** A partition landing on the far jamb of a door
    draws a short stripe across the opening, and that is a different wall
    correctly drawn - reading any coloured pixel as a failure calls three
    correctly severed openings on one sheet a fault, which is what the first
    version of this did. So what is looked for is a line of ink spanning the
    *whole* middle of the opening in the host wall's own direction, which is
    what a centreline drawn through its own doorway looks like and what nothing
    else does.

    The middle half, not the whole box: the overlay draws the opening's own
    magenta rectangle just outside the box and a severed wall's two stretches
    end at the jambs, so ink at the very edges of the box is expected.
    """
    walls = {
        wall.get("wall_id"): wall
        for wall in (page_reading.get("walls") or [])
        if wall.get("wall_id")
    }
    checked = through = 0
    where = []
    for opening in page_reading.get("openings") or []:
        wall = walls.get(opening.get("wall_id"))
        box = _where_the_opening_sits(opening, wall)
        host = (wall or {}).get("source_bbox") or (wall or {}).get("bbox")
        if not box or len(box) != 4 or not host or len(host) != 4:
            continue
        checked += 1
        along_x = (host[2] - host[0]) >= (host[3] - host[1])
        window = _the_middle_of(box, host, along_x)

        x0 = int(max(0, (window[0] - origin[0]) * scale))
        y0 = int(max(0, (window[1] - origin[1]) * scale))
        x1 = int(min(wall_ink.shape[1], (window[2] - origin[0]) * scale)) + 1
        y1 = int(min(wall_ink.shape[0], (window[3] - origin[1]) * scale)) + 1
        if x1 - x0 < 2 or y1 - y0 < 1:
            continue
        patch = wall_ink[y0:y1, x0:x1]
        if not patch.size:
            continue
        # Along the wall's own direction: is any line of the band inked from one
        # end of the opening to the other? A tenth is allowed for the blend at
        # each end of an anti-aliased line.
        share = patch.mean(axis=1 if along_x else 0)
        if float(share.max()) >= 0.9:
            through += 1
            if len(where) < 8:
                where.append({
                    "opening_id": opening.get("opening_id") or opening.get("mark"),
                    "wall_id": opening.get("wall_id"),
                    "at_pt": [round(box[0], 1), round(box[1], 1)],
                    "unbroken_share": round(float(share.max()), 2),
                })
    return checked, through, where


def _where_the_opening_sits(opening: dict, wall):
    """The stretch of wall the opening occupies - the same one the overlay draws.

    Asked of the reader itself rather than worked out again here, so that the
    picture, the cut and this check can never disagree about where a doorway is
    (Critical Rule 2).
    """
    try:
        from pipeline.plan.cvwalls import opening_span

        box = opening_span(opening, wall)
        if box and len(box) == 4:
            return box
    except Exception as e:
        logger.exception(f"overlay check: an opening's place could not be read: {e}")
    box = opening.get("source_bbox") or opening.get("bbox")
    return box if box and len(box) == 4 else None


def _the_middle_of(box, host, along_x):
    """The central half of the opening, across the host wall's own band."""
    if along_x:
        quarter = (box[2] - box[0]) / 4.0
        return (box[0] + quarter, host[1], box[2] - quarter, host[3])
    quarter = (box[3] - box[1]) / 4.0
    return (host[0], box[1] + quarter, host[2], box[3] - quarter)


def _the_sheet_itself(page, config: dict):
    """The plain sheet, rendered exactly as the overlay rendered it.

    **Exactly**: the same resolution, taken from the same setting. Rendering it
    at the overlay's pixel width instead is a scale a fraction different, which
    lands every line half a pixel off - and then every anti-aliased edge on the
    sheet differs from itself and reads as ink the overlay added.
    """
    try:
        import numpy as np
        import fitz
        from PIL import Image

        dpi = float((config.get("detection_overlay") or {}).get("overlay_dpi", 150))
        scale = dpi / 72.0
        pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale))
        plain = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
        return np.asarray(plain.convert("RGB")).astype(int)
    except Exception as e:
        logger.exception(f"overlay check: the plain sheet could not be rendered: {e}")
        return None


def check_pages(document, run_dir, pages: list, config: dict) -> dict:
    """Every overlay just drawn, checked against both criteria.

    Runs as part of the reading, on the document already open, so a sheet whose
    picture disagrees with its own record is found by the run that produced it
    rather than by somebody opening the file later. **Only the sheets that drew
    something are looked at**: a sheet with no walls and no openings has nothing
    for either criterion to be about, and rendering it plain again to prove that
    would put seconds on every upload for no answer (Section 4AF).
    """
    run_dir = Path(run_dir)
    sheets = []
    for page_reading in pages or []:
        number = page_reading.get("page_number")
        if not number:
            continue
        if not page_reading.get("walls"):
            continue
        png = run_dir / f"overlay_page_{number}.png"
        if not png.exists():
            continue
        try:
            page = document[number - 1]
            rect = page.rect
            sheets.append(check_sheet(
                png, page_reading, (rect.x0, rect.y0, rect.x1, rect.y1), config,
                _the_sheet_itself(page, config),
            ))
        except Exception as e:
            logger.exception(f"overlay check: page {number} could not be checked: {e}")

    outside = sum(s.get("wall_runs_outside_the_drawing") or 0 for s in sheets)
    through = sum(s.get("openings_drawn_through") or 0 for s in sheets)
    checked = sum(s.get("openings_checked") or 0 for s in sheets)
    if sheets:
        logger.info(
            f"overlay check: {len(sheets)} picture(s) read back - {outside} wall line(s) "
            f"drawn outside the plan, {through} of {checked} opening(s) drawn through"
        )
    return {
        "sheets_checked": len(sheets),
        "wall_runs_outside_the_drawing": outside,
        "openings_checked": checked,
        "openings_drawn_through": through,
        "passes": outside == 0 and through == 0,
        "sheets": sheets,
    }


def check_run(run_dir, config: dict, pages: list = None) -> dict:
    """Every overlay in a finished run, checked against both criteria.

    The source PDF saved beside them is opened for each page's own rectangle,
    which is what the overlay was rendered from.
    """
    run_dir = Path(run_dir)
    try:
        reading = json.loads(
            (run_dir / "plan_reading.json").read_text(encoding="utf-8")
        )
    except Exception as e:
        logger.exception(f"overlay check: {run_dir} could not be read: {e}")
        return {"sheets_checked": 0, "sheets": [], "note": "This run could not be read."}

    try:
        import fitz

        document = fitz.open(run_dir / "source.pdf")
    except Exception as e:
        logger.exception(f"overlay check: {run_dir} source could not be opened: {e}")
        return {"sheets_checked": 0, "sheets": [],
                "note": "The sheet itself could not be opened."}

    wanted = [
        page for page in (reading.get("pages") or [])
        if not pages or page.get("page_number") in pages
    ]
    try:
        return check_pages(document, run_dir, wanted, config)
    finally:
        try:
            document.close()
        except Exception:
            pass
