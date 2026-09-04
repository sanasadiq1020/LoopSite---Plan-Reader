"""What the wall and opening readers produced, drawn over the sheet itself.

**A table says what was found; a picture says whether it is right.** A wall
lying across the middle of a room, a door on a cupboard, a wall that is really
a dimension line - every one of those is obvious on the drawing and invisible
in a column of numbers. Nearly every defect this reader has had was found by
looking at one of these and not by reading a count. So one is produced for
every sheet that was read, automatically, with nothing to fill in and nothing
to compare against.

Each finding is drawn in the colour of what it was decided to be:

| drawn | what it is |
|---|---|
| solid blue | a wall with open ground on one side of it - an outer wall |
| solid green | a wall with building on both sides - an inner wall |
| solid violet | a wall that meets no other, so neither could be established |
| solid grey | a structure standing apart from the house: a carport, a shed |
| dashed red | a candidate set aside, labelled with the reason it was |
| magenta | an opening the drawing confirmed, labelled with what confirmed it |
| yellow outline | a break in a wall nothing confirmed - a gap to check |
| orange dot | where two walls meet, labelled with the shape of the meeting |

Every colour and size lives in ``config/plan_reading.json`` under
``detection_overlay``. Nothing in this module holds a colour, a coordinate or a
name taken from any particular drawing (Critical Rule 1).

Never raises. An overlay is evidence, and failing to draw one must not lose the
reading it was drawn from (Critical Rule 6).
"""

import json

from app.logging_setup import get_logger

logger = get_logger()

# A point is 1/72 inch. This is the only number in this file that is not a
# setting, because it is the definition of a PDF point rather than an opinion.
POINTS_PER_INCH = 72.0

# What the reader is told a junction is, when the record says L, T, + or that
# two walls run on into each other.
JUNCTION_IN_WORDS = {
    "L": "L",
    "T": "T",
    "+": "+",
    "collinear": "collinear",
}


def _settings(config: dict) -> dict:
    return config.get("detection_overlay", {}) or {}


def _colours(config: dict) -> dict:
    return _settings(config).get("colors", {}) or {}


def _rgb(value: str, fallback=(0, 0, 0)):
    """A colour written the way a designer writes one, as the drawing wants it."""
    try:
        text = str(value).lstrip("#")
        return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        return fallback


def _font(size: int):
    from PIL import ImageFont

    for name in ("arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _dashed_rectangle(draw, box, colour, width: int, dash: int = 7):
    """A rectangle drawn in dashes, for a candidate that was set aside.

    Dashed rather than a different solid colour, because a reader who is
    colour-blind still has to be able to tell a wall that was used from one
    that was not.
    """
    x0, y0, x1, y1 = box
    for start in range(int(x0), int(x1), dash * 2):
        draw.line([start, y0, min(start + dash, x1), y0], fill=colour, width=width)
        draw.line([start, y1, min(start + dash, x1), y1], fill=colour, width=width)
    for start in range(int(y0), int(y1), dash * 2):
        draw.line([x0, start, x0, min(start + dash, y1)], fill=colour, width=width)
        draw.line([x1, start, x1, min(start + dash, y1)], fill=colour, width=width)


def _clear_of(box, taken: list, bounds):
    """Somewhere to put a label that is not on top of another label.

    A plan is dense and the findings sit close together, so a label dropped
    where its finding is lands on the next one and neither can be read. Tried
    above the finding first, because that is where a reader looks for it, then
    below, then either side, then stepped along the wall it belongs to.

    **Returns None when there is nowhere free**, and the label is not drawn. A
    label printed on top of three others is not information — it is worse than
    no label, because it also hides the drawing underneath. Every label is in
    the summary and the issues log whether or not it fitted on the picture.
    """
    width = box[2] - box[0]
    height = box[3] - box[1]
    places = [
        (box[0], box[1] - height - 2),
        (box[0], box[3] + 2),
        (box[2] + 3, box[1]),
        (box[0] - width - 3, box[1]),
    ]
    # Stepped along and away, so a run of walls beside each other can all be
    # labelled instead of the first one taking the only free spot.
    for step in range(1, 6):
        places.append((box[0] + step * width * 0.4, box[1] - height - 2))
        places.append((box[0] - step * width * 0.4, box[3] + 2))
        places.append((box[0], box[1] - (height + 2) * (step + 1)))
        places.append((box[0], box[3] + (height + 2) * step))

    for x, y in places:
        placed = (x, y, x + width, y + height)
        if placed[0] < 0 or placed[1] < 0 or placed[2] > bounds[0] or placed[3] > bounds[1]:
            continue
        if any(
            placed[0] < other[2] and other[0] < placed[2]
            and placed[1] < other[3] and other[1] < placed[3]
            for other in taken
        ):
            continue
        return placed
    return None


def _wall_drawn_as(wall: dict, colours: dict, limit: int = 0):
    """The colour a wall is drawn in, and what its label says.

    The order is the order the decisions were taken in, and it matters: a
    candidate that was set aside is drawn as set aside whatever else it might
    have been, because that is the fact a reviewer needs about it.
    """
    identifier = wall.get("wall_id", "")
    length = wall.get("length_mm") or 0.0

    reason = wall.get("not_used_because")
    if reason:
        # **Shortened for the drawing, and only for the drawing.** These
        # sentences are written for the issues log; printed in full they run
        # right across the plan and drown the thing they are drawn on. The
        # whole reason is in the summary and in the issues log.
        short = str(reason)
        if limit and len(short) > limit:
            short = short[: max(limit - 1, 1)].rstrip() + "…"
        return _rgb(colours.get("flagged_wall", "#DC2626")), f"{identifier}  {short}", True

    if wall.get("structure_id") or (wall.get("building") or "main") != "main":
        name = wall.get("structure_id") or wall.get("building")
        return _rgb(colours.get("detached_structure", "#6B7280")), f"{name}", False

    kind = wall.get("wall_type") or "unknown"
    if kind == "outer":
        colour = _rgb(colours.get("outer_wall", "#1D4ED8"))
    elif kind == "inner":
        colour = _rgb(colours.get("inner_wall", "#15803D"))
    else:
        # Neither could be established, and saying which would be inventing an
        # answer. It is drawn in its own colour rather than as one or the other.
        colour = _rgb(colours.get("unknown_wall", "#7C3AED"))
    return colour, f"{identifier}  {length:,.0f} mm", False


def _opening_label(opening: dict) -> str:
    """An opening's name and what confirmed it, which is what makes it checkable."""
    name = opening.get("display_mark") or opening.get("mark") or opening.get("opening_id", "")
    sources = [s for s in (opening.get("evidence") or []) if s]
    # De-duplicated but kept in the order they were read, so the strongest
    # reading is named first.
    seen, ordered = set(), []
    for source in sources:
        if source not in seen:
            seen.add(source)
            ordered.append(source)
    return f"{name}  {'+'.join(ordered)}" if ordered else str(name)


def render_detection_overlay(page, page_reading: dict, out_path, config: dict) -> bool:
    """Draws one sheet's walls, openings, gaps and junctions over the sheet.

    Returns whether an image was written. Never raises.
    """
    try:
        from PIL import Image, ImageDraw
    except Exception as e:
        logger.exception(f"detection overlay needs an imaging library: {e}")
        return False

    try:
        import fitz

        settings = _settings(config)
        colours = _colours(config)
        dpi = float(settings.get("overlay_dpi", 150))
        width = int(settings.get("line_width", 3))
        label_points = float(settings.get("label_points", 7))
        dot_points = float(settings.get("junction_dot_radius_points", 2.5))
        with_labels = bool(settings.get("draw_labels", True))
        reason_limit = int(settings.get("flagged_label_max_chars", 40))

        scale = dpi / POINTS_PER_INCH
        pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale))
        image = Image.frombytes(
            "RGB", (pixmap.width, pixmap.height), pixmap.samples
        ).convert("RGB")
        draw = ImageDraw.Draw(image, "RGBA")
        font = _font(max(int(label_points * scale), 9))
        origin = (page.rect.x0, page.rect.y0)
        bounds = image.size

        def to_pixels(box):
            return [
                (box[0] - origin[0]) * scale, (box[1] - origin[1]) * scale,
                (box[2] - origin[0]) * scale, (box[3] - origin[1]) * scale,
            ]

        taken: list = []

        def label(text, box, colour):
            if not with_labels or not text:
                return
            try:
                size = draw.textbbox((0, 0), str(text), font=font)
            except Exception:
                return
            wanted = (box[0], box[1], box[0] + (size[2] - size[0]) + 4,
                      box[1] + (size[3] - size[1]) + 4)
            where = _clear_of(wanted, taken, bounds)
            if where is None:
                return
            taken.append(where)
            draw.rectangle(
                [where[0] - 1, where[1] - 1, where[2] + 1, where[3] + 1],
                fill=(255, 255, 255, 215),
            )
            draw.text((where[0] + 2, where[1] + 1), str(text), fill=colour, font=font)

        # --- the walls -----------------------------------------------------
        for wall in page_reading.get("walls") or []:
            box = wall.get("source_bbox") or wall.get("bbox")
            if not box or len(box) != 4:
                continue
            colour, text, flagged = _wall_drawn_as(wall, colours, reason_limit)
            drawn = to_pixels(box)
            # A wall traced as a hair-thin band still has to be visible.
            if drawn[2] - drawn[0] < 2:
                drawn[0] -= 1
                drawn[2] += 1
            if drawn[3] - drawn[1] < 2:
                drawn[1] -= 1
                drawn[3] += 1
            if flagged:
                _dashed_rectangle(draw, drawn, colour, width)
            else:
                draw.rectangle(drawn, outline=colour, width=width)
            label(text, drawn, colour)

        # --- the openings the drawing confirmed ----------------------------
        opening_colour = _rgb(colours.get("confirmed_opening", "#C026D3"))
        for opening in page_reading.get("openings") or []:
            box = opening.get("source_bbox")
            if not box or len(box) != 4:
                continue
            drawn = to_pixels(box)
            pad = width + 1
            draw.rectangle(
                [drawn[0] - pad, drawn[1] - pad, drawn[2] + pad, drawn[3] + pad],
                outline=opening_colour, width=width + 1,
            )
            label(_opening_label(opening), drawn, opening_colour)

        # --- the breaks nothing confirmed ----------------------------------
        gap_colour = _rgb(colours.get("unresolved_gap", "#EAB308"))
        for gap in page_reading.get("unresolved_gaps") or []:
            box = gap.get("source_bbox")
            if not box or len(box) != 4:
                continue
            drawn = to_pixels(box)
            pad = width + 1
            draw.rectangle(
                [drawn[0] - pad, drawn[1] - pad, drawn[2] + pad, drawn[3] + pad],
                outline=gap_colour, width=width + 1,
            )
            draw.rectangle(
                [drawn[0] - pad, drawn[1] - pad, drawn[2] + pad, drawn[3] + pad],
                fill=gap_colour + (45,),
            )
            label("UNRESOLVED", drawn, gap_colour)

        # --- where the walls meet ------------------------------------------
        junction_colour = _rgb(colours.get("junction", "#EA580C"))
        radius = max(dot_points * scale, 2.0)
        seen_junctions: set = set()
        for wall in page_reading.get("walls") or []:
            for junction in wall.get("junctions") or []:
                at = junction.get("at_pt")
                if not at or len(at) != 2:
                    continue
                pair = tuple(sorted((wall.get("wall_id", ""),
                                     junction.get("with_wall_id", ""))))
                if pair in seen_junctions:
                    continue
                seen_junctions.add(pair)
                x = (at[0] - origin[0]) * scale
                y = (at[1] - origin[1]) * scale
                draw.ellipse(
                    [x - radius, y - radius, x + radius, y + radius],
                    fill=junction_colour, outline=junction_colour,
                )
                shape = JUNCTION_IN_WORDS.get(junction.get("shape"), junction.get("shape"))
                label(shape, [x + radius, y - radius, x + radius, y + radius],
                      junction_colour)

        out_path = str(out_path)
        image.save(out_path)
        return True
    except Exception as e:
        logger.exception(f"could not draw the detection overlay for {out_path}: {e}")
        return False


# --- what the colours mean --------------------------------------------------

# What each entry of the legend says, in the words a plan reader would use. A
# reader is never shown a field name.
LEGEND_ROWS = [
    ("outer_wall", "solid", "Outer wall — open ground on one side"),
    ("inner_wall", "solid", "Inner wall — building on both sides"),
    ("unknown_wall", "solid", "Wall meeting no other — outside or inside not established"),
    ("detached_structure", "solid", "Detached structure — carport, shed, pergola"),
    ("flagged_wall", "dashed", "Set aside — the label says why"),
    ("confirmed_opening", "solid", "Door or window — the label says what confirmed it"),
    ("unresolved_gap", "solid", "Break in a wall nothing confirmed — check this"),
    ("junction", "dot", "Where two walls meet — L, T, + or running on"),
]


def render_legend(out_path, config: dict) -> bool:
    """One picture saying what every colour on the overlays means.

    Drawn from the same settings the overlays are drawn from, so a colour
    changed in the configuration changes both and they can never disagree.
    """
    try:
        from PIL import Image, ImageDraw
    except Exception as e:
        logger.exception(f"the legend needs an imaging library: {e}")
        return False
    try:
        colours = _colours(config)
        font = _font(15)
        heading = _font(19)

        row_height, swatch, padding = 30, 26, 18
        width = 620
        height = padding * 2 + 34 + row_height * len(LEGEND_ROWS)
        image = Image.new("RGB", (width, height), (255, 255, 255))
        draw = ImageDraw.Draw(image)
        draw.rectangle([0, 0, width - 1, height - 1], outline=(203, 213, 225), width=1)
        draw.text((padding, padding), "What the colours mean", fill=(15, 23, 42), font=heading)

        y = padding + 34
        for key, style, meaning in LEGEND_ROWS:
            colour = _rgb(colours.get(key, "#000000"))
            box = [padding, y + 4, padding + swatch, y + 4 + swatch - 10]
            if style == "dashed":
                _dashed_rectangle(draw, box, colour, 2, dash=4)
            elif style == "dot":
                middle = ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)
                draw.ellipse(
                    [middle[0] - 6, middle[1] - 6, middle[0] + 6, middle[1] + 6],
                    fill=colour, outline=colour,
                )
            else:
                draw.rectangle(box, outline=colour, width=3)
            draw.text((padding + swatch + 14, y + 2), meaning, fill=(30, 41, 59), font=font)
            y += row_height

        image.save(str(out_path))
        return True
    except Exception as e:
        logger.exception(f"could not draw the overlay legend: {e}")
        return False


# --- what was found, as numbers the interface can read ----------------------


def _percentage(part: int, whole: int):
    return round(100.0 * part / whole, 1) if whole else None


def detection_summary(pages: list, config: dict) -> dict:
    """Everything the overlays show, counted, for the interface to read.

    Computed from the records themselves rather than asserted anywhere, so a
    figure here and the picture beside it can never disagree.
    """
    walls = outer = inner = unknown = flagged = detached = 0
    junction_shapes: dict = {}
    openings = unresolved = 0
    opening_types: dict = {}
    evidence_counts: dict = {}
    confirmed = needing_review = 0
    traced = traceable = 0
    per_sheet = []
    # The reason each candidate was set aside, in full. The label on the
    # picture is shortened to stay readable; this is where the whole sentence
    # lives, so nothing is only ever half-said.
    flagged_reasons: list = []

    for page in pages:
        if page.get("error"):
            continue
        sheet_walls = page.get("walls") or []
        sheet_openings = page.get("openings") or []
        sheet_gaps = page.get("unresolved_gaps") or []

        sheet_outer = sheet_inner = sheet_unknown = sheet_flagged = sheet_detached = 0
        seen_junctions: set = set()
        for wall in sheet_walls:
            walls += 1
            if wall.get("source_bbox") or wall.get("bbox"):
                traced += 1
            traceable += 1
            if wall.get("not_used_because"):
                sheet_flagged += 1
                flagged_reasons.append({
                    "sheet_id": page.get("sheet_id"),
                    "page_number": page.get("page_number"),
                    "wall_id": wall.get("wall_id"),
                    "reason": wall.get("not_used_because"),
                    "source_bbox": wall.get("source_bbox") or wall.get("bbox"),
                })
            elif wall.get("structure_id") or (wall.get("building") or "main") != "main":
                sheet_detached += 1
            elif wall.get("wall_type") == "outer":
                sheet_outer += 1
            elif wall.get("wall_type") == "inner":
                sheet_inner += 1
            else:
                sheet_unknown += 1
            for junction in wall.get("junctions") or []:
                pair = tuple(sorted((wall.get("wall_id", ""),
                                     junction.get("with_wall_id", ""))))
                if pair in seen_junctions:
                    continue
                seen_junctions.add(pair)
                shape = junction.get("shape") or "unknown"
                junction_shapes[shape] = junction_shapes.get(shape, 0) + 1

        for opening in sheet_openings:
            openings += 1
            traceable += 1
            if opening.get("source_bbox"):
                traced += 1
            kind = opening.get("element_type") or "unknown_opening"
            opening_types[kind] = opening_types.get(kind, 0) + 1
            for source in set(opening.get("evidence") or []):
                evidence_counts[source] = evidence_counts.get(source, 0) + 1
            if opening.get("review_needed"):
                needing_review += 1
            else:
                confirmed += 1

        unresolved += len(sheet_gaps)
        outer += sheet_outer
        inner += sheet_inner
        unknown += sheet_unknown
        flagged += sheet_flagged
        detached += sheet_detached

        calibration = page.get("scale_calibration") or {}
        if sheet_walls or sheet_openings or sheet_gaps:
            per_sheet.append({
                "page_number": page.get("page_number"),
                "sheet_id": page.get("sheet_id"),
                "overlay": f"overlay_page_{page.get('page_number')}.png",
                "walls": len(sheet_walls),
                "outer_walls": sheet_outer,
                "inner_walls": sheet_inner,
                "unknown_walls": sheet_unknown,
                "flagged_walls": sheet_flagged,
                "detached_structure_walls": sheet_detached,
                "openings": len(sheet_openings),
                "unresolved_gaps": len(sheet_gaps),
                "scale_status": calibration.get("result"),
                "mm_per_point": (
                    calibration.get("measured_mm_per_point")
                    or calibration.get("printed_mm_per_point")
                ),
                "usable_for_measurement": bool(calibration.get("usable_for_measurement")),
            })

    # The scale is a per-sheet fact, so the document-level answer is what its
    # sheets said - never one sheet's answer presented as the document's.
    scale_results: dict = {}
    for page in pages:
        result = ((page.get("scale_calibration") or {}).get("result")) or "not_checked"
        scale_results[result] = scale_results.get(result, 0) + 1

    return {
        "format_version": 1,
        "walls": {
            "total": walls,
            "outer": outer,
            "inner": inner,
            "outside_or_inside_not_established": unknown,
            "flagged": flagged,
            "detached_structure": detached,
            "junctions": sum(junction_shapes.values()),
            "junctions_by_shape": junction_shapes,
            "flagged_reasons": flagged_reasons,
        },
        "openings": {
            "total": openings,
            "by_type": opening_types,
            "by_reading": evidence_counts,
            "confirmed_by_two_or_more_readings": confirmed,
            "needing_a_reviewer": needing_review,
            "unresolved_gaps": unresolved,
        },
        "scale": {
            "sheets_by_result": scale_results,
            "sheets_usable_for_measurement": sum(
                1 for page in pages
                if (page.get("scale_calibration") or {}).get("usable_for_measurement")
            ),
        },
        "traceability": {
            "records_with_a_place_on_the_sheet": traced,
            "records_total": traceable,
            "traceability_pct": _percentage(traced, traceable),
        },
        "legend_image": "overlay_legend.png",
        "sheets": per_sheet,
    }


def write_detection_outputs(doc, pages: list, out_dir, config: dict) -> dict:
    """Draws every sheet's overlay, the legend, and writes the summary.

    One picture per sheet that was read, whatever is on it: a sheet with
    nothing found still produces an overlay, because "nothing was found here"
    is a result a reviewer has to be able to see.
    """
    from pathlib import Path

    out_dir = Path(out_dir)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.exception(f"could not make the overlay folder {out_dir}: {e}")
        return {}

    drawn = 0
    for page_reading in pages:
        number = page_reading.get("page_number")
        if number is None or page_reading.get("error"):
            continue
        try:
            page = doc[number - 1]
        except Exception as e:
            logger.exception(f"page {number} could not be opened for its overlay: {e}")
            continue
        if render_detection_overlay(
            page, page_reading, out_dir / f"overlay_page_{number}.png", config
        ):
            drawn += 1

    render_legend(out_dir / "overlay_legend.png", config)

    summary = detection_summary(pages, config)
    summary["overlays_drawn"] = drawn
    try:
        (out_dir / "detection_summary.json").write_text(
            json.dumps(summary, indent=2), encoding="utf-8"
        )
    except Exception as e:
        logger.exception(f"could not write the detection summary: {e}")

    logger.info(
        f"detection overlays: {drawn} sheet(s) drawn, "
        f"{summary['walls']['total']} walls "
        f"({summary['walls']['outer']} outer, {summary['walls']['inner']} inner, "
        f"{summary['walls']['flagged']} set aside), "
        f"{summary['openings']['total']} openings, "
        f"{summary['openings']['unresolved_gaps']} gaps to check"
    )
    return summary
