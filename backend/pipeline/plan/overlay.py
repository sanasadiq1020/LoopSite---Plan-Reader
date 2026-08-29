"""Day 3 — the source overlay.

Week 1's Gate 8 asks for an image that shows what was extracted, drawn over the
original sheet, in distinct colours, with a legend, so that a reviewer can see
false positives, misses and uncertain items directly. That is the only check
that does not depend on trusting the pipeline's own confidence numbers, so it
is generated for every page rather than for a chosen example.

Two drawing rules carry meaning and are not decoration:

*   Colour identifies **what** was detected (title-block field, room,
    dimension, schedule row, legend entry), read from ``config``.
*   A dashed outline identifies **anything not confirmed** — a value below the
    review threshold, a cross-check disagreement, a chain that failed its
    arithmetic. A reviewer scanning the sheet can therefore find every
    uncertain item without reading a single number.
"""

import io

import fitz
from PIL import Image, ImageDraw, ImageFont

from app.logging_setup import get_logger

logger = get_logger()

_LEGEND_PADDING = 10
_LEGEND_SWATCH = 14
_LEGEND_LINE_HEIGHT = 20


def _hex_to_rgb(value: str):
    value = value.lstrip("#")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))


def _load_font(size: int):
    for name in ("arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _dashed_rectangle(draw, box, colour, width: int, dash: int = 6):
    x0, y0, x1, y1 = box
    for x in range(int(x0), int(x1), dash * 2):
        draw.line([(x, y0), (min(x + dash, x1), y0)], fill=colour, width=width)
        draw.line([(x, y1), (min(x + dash, x1), y1)], fill=colour, width=width)
    for y in range(int(y0), int(y1), dash * 2):
        draw.line([(x0, y), (x0, min(y + dash, y1))], fill=colour, width=width)
        draw.line([(x1, y), (x1, min(y + dash, y1))], fill=colour, width=width)


def _collect_marks(page_reading: dict) -> list:
    """Every detected item as (category, bbox, label, confirmed[, shape]).

    ``shape`` defaults to a box. A wall is drawn as a thick line down its
    centre instead: at 150 DPI a 110 mm wall is three pixels deep, and an
    outline that thin reads as nothing at all on the sheet.
    """
    marks: list = []

    for name, field in page_reading["title_block"].items():
        if not field.get("source_bbox"):
            continue
        confirmed = field.get("confidence_band") == "high" and field.get(
            "verified_against_index"
        ) is not False
        marks.append(("title_block", field["source_bbox"], name.replace("_", " "), confirmed))

    for room in page_reading["rooms"]:
        marks.append(
            ("room", room["bbox"], room["room_id"], room["confidence_band"] == "high")
        )
        if room.get("dimension_bbox"):
            marks.append(("room", room["dimension_bbox"], "", room["confidence_band"] == "high"))

    failed_chains = {
        chain["chain_id"]
        for chain in page_reading.get("dimension_chains", [])
        if chain["check"]["result"] == "fail"
    }
    for dimension in page_reading["dimensions"]:
        confirmed = (
            dimension["confidence_band"] == "high"
            and dimension.get("chain_id") not in failed_chains
        )
        marks.append(("dimension", dimension["bbox"], dimension["dimension_id"], confirmed))

    # Week 1 Gate 8 asks the overlay to show wall candidates and opening marks
    # alongside the text, so a reviewer can see a wrong wall or a missed
    # opening without reading a single number.
    for wall in page_reading.get("walls", []):
        confirmed = wall["confidence_band"] == "high" and wall["matches_nominal_thickness"]
        # Only long walls are labelled. A floor plan carries dozens of short
        # internal walls and labelling every one buries the drawing underneath
        # the overlay.
        label = ""
        if wall["length_mm"] >= 3000:
            label = f"{wall['length_mm']:.0f}×{wall['thickness_mm']:.0f}"
            openings_on_wall = wall.get("linked_opening_marks") or []
            if openings_on_wall:
                label = f"{label}  {'/'.join(openings_on_wall)}"
        marks.append(("wall", wall["bbox"], label, confirmed, "line"))

    for opening in page_reading.get("openings", []):
        label = opening["mark"]
        if opening.get("width_mm") and opening.get("height_mm"):
            label = f"{label} {opening['width_mm']:.0f}×{opening['height_mm']:.0f}"
        elif opening.get("width_mm"):
            # An opening the drawing does not label still has a measured width,
            # and that is what identifies it on the sheet.
            label = f"{label} {opening['width_mm']:.0f} wide".strip()
        confirmed = bool(opening.get("in_schedule") and opening.get("wall_id"))
        marks.append(("opening", opening["source_bbox"], label, confirmed))

    for table in page_reading.get("schedules", []):
        for row in table["rows"]:
            if not row.get("bbox"):
                continue
            marks.append(
                ("schedule", row["bbox"], row.get("mark") or row["row_id"], not row["flags"])
            )

    for legend in page_reading.get("legends", []):
        for entry in legend["entries"]:
            marks.append(("legend", entry["bbox"], "", True))

    # The drawing index is real extracted data and is what every sheet's title
    # block is checked against, so it has to be visible on the sheet it was
    # read from. Without this the cover sheet's overlay showed nothing at all.
    index = page_reading.get("sheet_index")
    if index:
        for entry in index.get("entries", []):
            label = entry.get("sheet_number") or ""
            marks.append(("drawing index", entry["source_bbox"], label, True))
        marks.append(("drawing index", index["header_bbox"], "index", True))

    for item in page_reading.get("unresolved_items", []):
        if item.get("bbox"):
            marks.append(("unresolved", item["bbox"], item.get("item_id", ""), False))

    return marks


def render_overlay(page, page_reading: dict, out_path, config: dict, page_pixmap=None) -> bool:
    """Draws the page's detections over its render. Returns True on success.

    Never raises: an overlay is evidence, and failing to produce it must not
    lose the extraction it was going to illustrate (Critical Rule 6).
    """
    try:
        overlay_config = config.get("overlay", {})
        dpi = overlay_config.get("dpi", 150)
        colours = {
            key: _hex_to_rgb(value)
            for key, value in overlay_config.get("colors", {}).items()
        }
        line_width = overlay_config.get("line_width", 2)
        scale = (page_pixmap.width / page.rect.width) if page_pixmap is not None else dpi / 72.0

        # The page is rendered once per run and handed in; rendering it again
        # here doubled the cost of every upload. Encoding that render to PNG
        # only to decode it straight back cost as much again, so the pixels are
        # handed to Pillow directly.
        pixmap = page_pixmap if page_pixmap is not None else page.get_pixmap(
            matrix=fitz.Matrix(scale, scale)
        )
        image = Image.frombytes(
            "RGB" if pixmap.n < 4 else "RGBA", (pixmap.width, pixmap.height), pixmap.samples
        ).convert("RGB")
        draw = ImageDraw.Draw(image)
        label_font = _load_font(max(9, int(dpi / 16)))
        legend_font = _load_font(max(11, int(dpi / 12)))

        used_categories: dict = {}
        for mark in _collect_marks(page_reading):
            category, bbox, label, confirmed = mark[:4]
            shape = mark[4] if len(mark) > 4 else "box"
            colour = colours.get(category, (220, 38, 38))
            box = [
                bbox[0] * scale - 2,
                bbox[1] * scale - 2,
                bbox[2] * scale + 2,
                bbox[3] * scale + 2,
            ]
            if box[2] <= box[0] or box[3] <= box[1]:
                continue
            if shape == "line":
                # Down the middle of the wall's two faces, along its own
                # direction. Kept well inside the wall's real thickness so the
                # drawing underneath stays readable — the overlay is there to
                # be checked against the drawing, not to cover it.
                horizontal = (box[2] - box[0]) >= (box[3] - box[1])
                depth = (box[3] - box[1]) if horizontal else (box[2] - box[0])
                stroke = max(min(int(depth * 0.45), 6), line_width)
                if horizontal:
                    middle = (box[1] + box[3]) / 2
                    draw.line([(box[0], middle), (box[2], middle)], fill=colour, width=stroke)
                else:
                    middle = (box[0] + box[2]) / 2
                    draw.line([(middle, box[1]), (middle, box[3])], fill=colour, width=stroke)
            elif confirmed:
                draw.rectangle(box, outline=colour, width=line_width)
            else:
                _dashed_rectangle(draw, box, colour, line_width)
            used_categories.setdefault(category, colour)
            if label:
                draw.text((box[0], max(0, box[1] - 11)), str(label), fill=colour, font=label_font)

        # On-image legend, so the overlay explains itself when it is opened
        # outside the application.
        entries = [(category.replace("_", " "), colour) for category, colour in used_categories.items()]
        entries.append(("dashed = not confirmed", (90, 90, 90)))
        panel_height = _LEGEND_LINE_HEIGHT * len(entries) + _LEGEND_PADDING * 2
        panel_width = 260
        draw.rectangle(
            [
                _LEGEND_PADDING,
                _LEGEND_PADDING,
                _LEGEND_PADDING + panel_width,
                _LEGEND_PADDING + panel_height,
            ],
            fill=(255, 255, 255),
            outline=(30, 30, 30),
            width=2,
        )
        y = _LEGEND_PADDING * 2
        for text, colour in entries:
            draw.rectangle(
                [
                    _LEGEND_PADDING * 2,
                    y,
                    _LEGEND_PADDING * 2 + _LEGEND_SWATCH,
                    y + _LEGEND_SWATCH,
                ],
                fill=colour,
            )
            draw.text(
                (_LEGEND_PADDING * 2 + _LEGEND_SWATCH + 8, y - 1),
                text,
                fill=(20, 20, 20),
                font=legend_font,
            )
            y += _LEGEND_LINE_HEIGHT

        image.save(str(out_path))
        return True
    except Exception as e:
        logger.exception(f"render_overlay failed for {out_path}: {e}")
        return False
