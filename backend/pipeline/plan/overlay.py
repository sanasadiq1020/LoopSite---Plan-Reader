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

**What is deliberately not drawn.** Flagged items are not marked on the sheet.
Everything flagged goes to the downloadable issues log, with its sheet, area,
category, severity, wording and position; drawing every one of them here as
well covered most of the drawing in red boxes, and the marks saying what was
actually *read* could barely be seen underneath. The sheet shows what was read;
the log says what to check.

**A name is left off rather than printed over another.** Marks cluster where a
drawing is busiest, and their names were landing on top of one another into a
smear — worse than a missing name, because a smear cannot be read *and* hides
the drawing beneath it.
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


# An opening's name, in points on the sheet, and how thick its outline is.
_OPENING_LABEL_POINTS = 9
_OPENING_BORDER = 2


def _load_font(size: int):
    for name in ("arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _overlaps(one, other) -> bool:
    return not (
        one[2] <= other[0] or other[2] <= one[0] or one[3] <= other[1] or other[3] <= one[1]
    )


# How tall a drawn name is, and roughly how wide one character is, at the size
# the labels are drawn. Used only to keep names from landing on one another.
_LABEL_HEIGHT = 11
_LABEL_CHARACTER_WIDTH = 5.5


def _room_for_a_label(
    text: str, box, already_drawn: list, image_size, font=None, draw=None
) -> tuple:
    """Somewhere clear to put this name, as near its own mark as possible.

    Tried in order: just above the mark, just below it, then to either side,
    then stepped further above and below. The first place clear of every name
    already drawn wins. If a drawing is so crowded that nothing is clear, the
    name still goes just above its mark — a name in the wrong place is a
    nuisance, a name missing altogether is a reader unable to find the row it
    belongs to.
    """
    # Measured where a font is given, so the white panel behind a name is the
    # size of the name rather than a guess at it.
    height = _LABEL_HEIGHT
    if font is not None and draw is not None:
        try:
            left_x, top_y, right_x, bottom_y = draw.textbbox((0, 0), text, font=font)
            width = (right_x - left_x) + 5
            height = (bottom_y - top_y) + 4
        except Exception:
            width = len(text) * _LABEL_CHARACTER_WIDTH + 4
    else:
        width = len(text) * _LABEL_CHARACTER_WIDTH + 4
    page_width, page_height = image_size
    left = min(max(0.0, box[0]), max(0.0, page_width - width))
    above, below = box[1] - height, box[3] + 1

    places = [(left, above), (left, below), (box[2] + 3, above), (left - width - 3, above)]
    for step in range(1, 7):
        places.append((left, above - step * height))
        places.append((left, below + step * height))

    for x, y in places:
        if x < 0 or y < 0 or x + width > page_width or y + height > page_height:
            continue
        room = (x, y, x + width, y + height)
        if not any(_overlaps(room, taken) for taken in already_drawn):
            return room
    return (left, max(0.0, above), left + width, max(0.0, above) + height)


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
    walls_by_id = {w["wall_id"]: w for w in page_reading.get("walls", [])}
    for wall in page_reading.get("walls", []):
        # A candidate that meets no other wall is an eave, a roof extent, a
        # fence or a bench — not a wall of this building. It stays in the
        # sheet's table with the reason, but drawing it here would put a wall
        # on the sheet where the drawing has none, which is the one thing a
        # marked-up sheet must never do.
        if not wall.get("meets_another_wall", True):
            continue
        confirmed = wall["confidence_band"] == "high" and wall["matches_nominal_thickness"]
        # **Every wall is named.** Its short number is what lets a reader take
        # a row of the walls table — its length, its thickness, where it was
        # measured from — and find that exact wall on the drawing. Without it
        # the table and the sheet cannot be put side by side at all.
        label = wall["wall_id"].rsplit("-", 1)[-1]
        # Only the long ones carry their size as well; a floor plan has dozens
        # of short internal walls and putting a measurement on every one buries
        # the drawing under the overlay.
        if wall["length_mm"] >= 3000:
            label = f"{label} {wall['length_mm']:.0f}×{wall['thickness_mm']:.0f}"
        # **A carport is drawn, and drawn as a carport.** A structure standing
        # apart from the house is a real thing on the sheet and belongs on the
        # marked-up sheet, but drawing it in the same colour as the building
        # says it is part of the building, which is the thing this is for
        # telling apart. Its own colour, and its structure's number on it.
        if wall.get("building") == "detached":
            marks.append((
                "detached structure",
                wall["bbox"],
                f"{wall.get('structure_id') or ''} {label}".strip(),
                confirmed,
                "line",
            ))
        else:
            marks.append(("wall", wall["bbox"], label, confirmed, "line"))

    for opening in page_reading.get("openings", []):
        # **Every opening is named on the sheet, and only named.** Where the
        # drawing prints a mark that is the name; where it prints none — and a
        # great many plans print none — a short one is made from what the
        # opening is, so a row of the doors-and-windows table can always be
        # found on the drawing.
        #
        # The size is deliberately not on it. Every opening used to carry its
        # width and height here, which is a pair of numbers on a sheet that
        # already prints its own dimensions, sitting over the drawing a
        # reviewer is trying to read. The size is a column in the table; the
        # sheet only has to say *which* opening this is.
        label = opening.get("display_mark") or opening.get("mark") or "O?"
        confirmed = opening.get("confidence_band") == "high"

        # **Drawn where the opening is, not where its label is printed.** A
        # mark is printed beside the door, often inside the room on a leader,
        # so a box round the mark says where the lettering is. What a reviewer
        # is checking is whether the hole is in the right place, so the box
        # goes on the place the opening was given on its wall — the same
        # evidence the 3D model is cut from — and falls back to the mark's own
        # box only where it was never placed.
        marks.append((
            _what_kind_of_opening(opening),
            _opening_on_its_wall(opening, walls_by_id) or opening["source_bbox"],
            label,
            confirmed,
            "opening",
        ))

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

    # **Flagged items are deliberately not drawn here.** Everything flagged —
    # the sheet, the area, the category, the severity, the wording and the
    # position — goes to the downloadable issues log, which is where a reader
    # is meant to look for what to check. Drawing every one of them on the
    # sheet as well put a red box over most of the drawing, so the marks that
    # say what *was* read could barely be seen underneath. The sheet shows what
    # was read; the log says what to check.
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
        # An opening's name is set at 9 point on the sheet — a real type size,
        # so it reads the same whether the page is rendered at 150 dots an inch
        # or at 300. The other names keep the size they had.
        opening_font = _load_font(max(int(_OPENING_LABEL_POINTS * scale), 9))

        used_categories: dict = {}
        label_boxes: list = []
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
            elif shape == "opening":
                # The opening itself: an outline on the place it occupies, two
                # pixels thick, never filled — a filled box would hide the very
                # thing a reviewer opened the sheet to look at. Still dashed
                # where the reading is not confirmed, which is what the key in
                # the corner says a dashed outline means.
                if confirmed:
                    draw.rectangle(box, outline=colour, width=_OPENING_BORDER)
                else:
                    _dashed_rectangle(draw, box, colour, _OPENING_BORDER)
            elif confirmed:
                draw.rectangle(box, outline=colour, width=line_width)
            else:
                _dashed_rectangle(draw, box, colour, line_width)
            used_categories.setdefault(category, colour)
            if label and shape == "opening":
                # **Above the opening, on white.** A name in the drawing's own
                # colours disappears into the line work under it; a name over a
                # patch of white is legible on top of anything. It is still
                # moved out of the way of a name already drawn, because on a
                # busy plan the openings are close enough together that fixing
                # every name directly above its own box printed them over one
                # another into a smear.
                where = _room_for_a_label(
                    str(label), box, label_boxes, image.size, opening_font, draw
                )
                draw.rectangle(where, fill=(255, 255, 255))
                draw.text((where[0] + 2, where[1]), str(label), fill=colour, font=opening_font)
                label_boxes.append(where)
            elif label:
                # **Every name is drawn. A name that would land on one already
                # there is moved, not dropped.** Marks cluster where a drawing
                # is busiest, and their names were printing on top of one
                # another into a smear that could not be read and hid the
                # drawing underneath. A place is looked for around the mark
                # instead — above it, below it, then stepped further out — and
                # only somewhere clear is used.
                where = _room_for_a_label(str(label), box, label_boxes, image.size)
                draw.text(where[:2], str(label), fill=colour, font=label_font)
                label_boxes.append(where)

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


def _what_kind_of_opening(opening: dict) -> str:
    """Which colour this opening is drawn in: a door, a window, or neither.

    Read off the kind the reading already settled — nothing is decided here.
    An opening the drawing never named keeps its own colour rather than being
    coloured as a guess.
    """
    kind = (opening.get("element_type") or "").lower()
    if "door" in kind and "window" not in kind:
        return "door"
    if "window" in kind:
        return "window"
    return "opening"


def _opening_on_its_wall(opening: dict, walls_by_id: dict):
    """The box the opening occupies on its wall, in page points.

    The fractions are measured from the wall's own start point, so turning
    them back into a place on the page needs nothing but that wall.
    """
    position = opening.get("position_on_wall") or {}
    wall = walls_by_id.get(opening.get("wall_id"))
    if not position or wall is None:
        return None
    try:
        start, end = wall["start_point_pt"], wall["end_point_pt"]
        first, last = float(position["start_fraction"]), float(position["end_fraction"])
        low = [start[i] + (end[i] - start[i]) * first for i in (0, 1)]
        high = [start[i] + (end[i] - start[i]) * last for i in (0, 1)]
        faces = sorted(wall.get("face_positions_pt") or [])
        if len(faces) != 2:
            return None
        if wall["runs_along"] == "x":
            return [min(low[0], high[0]), faces[0], max(low[0], high[0]), faces[1]]
        return [faces[0], min(low[1], high[1]), faces[1], max(low[1], high[1])]
    except Exception:
        # An overlay is evidence, not a calculation — a mark that cannot be
        # placed is left off rather than failing the whole picture.
        return None
