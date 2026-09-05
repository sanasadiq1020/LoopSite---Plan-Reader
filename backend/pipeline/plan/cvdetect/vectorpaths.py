"""Step 1 - the drawing's own line work, with what is not a wall taken out of it.

A PDF plan does not have to be guessed at from pixels: it states its geometry.
``page.get_drawings()`` returns every path the drafter plotted, with the width
it was plotted at and the dash pattern it was plotted with. Reading that
directly is exact where anything recovered from an image is an estimate, so it
is always tried first.

**The noise has to come out before the image is made, not after.** A dimension
line, a roof overhang and a wall face all become the same black pixels once
they are drawn, and no amount of morphology afterwards can tell them apart -
a 90 mm dimension's two witness lines are two parallel lines 90 mm apart, which
is a wall by every geometric test there is. So this module classifies each path
while it still has its stated width and dash pattern, and the binary image
handed to Step 4 is drawn from the structural paths **only**.

Two ways a path is set aside, and one measured lesson about each:

**Dashed.** A roof extent, an eave, a boundary, a setback and a carport are all
plotted dashed, and none of them is a wall. The obvious test is the dash
pattern the PDF states - and on the plan sets in use it finds *nothing*:
measured, all 5,595 paths on one floor plan report ``[] 0``, which is solid,
including the roof extent that is plainly dashed on the page. Exporters very
commonly emit dashes as many separate short segments instead. So the shape of
the run is read as well: **many short collinear pieces, separated by regular
gaps, is a dashed line whatever the file calls it.** Regularity is the part
that matters - a wall face broken by a doorway has one big irregular gap, while
a dashed line's gaps are all the same size, and that difference is scale-free.

**Stroke weight.** AS 1100.301 has offices plot a structural outline heavier
than an annotation line, so separating them by weight is sound in principle.
In practice no fixed number of points can do it, and neither can the obvious
statistical answer. Measured by drawn length on the real sheets:

| sheet | weights present, by drawn length |
|---|---|
| one office's floor plan | 0.28 pt 48%, 0.37 pt 24%, 0.51 pt 18%, 0.71 pt 10% |
| another office's floor plan | **0.17 pt 66%**, 0.42 pt 2%, **1.36 pt 29%** |

A "structural is >= 0.35 pt" rule throws away nearly half of the first sheet.
Otsu's method over the same histogram is worse on the second: it puts the cut
at 1.36 pt, which is the **drawing frame and title block**, and deletes the
whole building - which is drawn at 0.17 pt - while looking entirely reasonable.

So the rule is turned around. The weight class carrying the most drawn length
*is* the drawing, and only what is plotted lighter than the drawing is an
annotation line (``_dominant_weight_split``). It cannot delete the drawing,
because the drawing is what sets the threshold. *Measured, this removes nothing
on any of the three plan sets in use* - which is the honest finding: on
Australian residential plan sets stroke weight does not separate structure from
annotation, and what actually does is the thickness of the two paired faces,
which is Step 4's job. Otsu is kept as a configurable option, with the
measurement above recorded beside it, for the office whose drawings do carry
the distinction.
"""

import math
from dataclasses import dataclass, field
from statistics import median, pstdev

from app.logging_setup import get_logger
from pipeline.plan import layout
from pipeline.plan.cvdetect.settings import number, setting

logger = get_logger()

# Two segments are on the same infinite line when they agree in direction to
# within this, and sit within the tolerance below of each other across it.
# Both are drafting allowances on the paper, not measurements of a building.
_SAME_DIRECTION_DEGREES = 2.0
_SAME_LINE_TOLERANCE_PT = 0.6

# Below this a "segment" is a plotting artefact rather than a drawn line.
_SHORTEST_SEGMENT_PT = 0.5


@dataclass
class Segment:
    """One straight piece of drawn line, in the page's own space."""

    x0: float
    y0: float
    x1: float
    y1: float
    width: float
    stated_dashed: bool
    path_index: int
    role: str = "structural"
    reason: str = ""

    @property
    def length(self) -> float:
        return math.hypot(self.x1 - self.x0, self.y1 - self.y0)

    @property
    def angle_degrees(self) -> float:
        """Direction of the line, folded onto 0-180: a line has no arrowhead."""
        return math.degrees(math.atan2(self.y1 - self.y0, self.x1 - self.x0)) % 180.0


@dataclass
class VectorPaths:
    """Everything Step 1 recovered, sorted into what it is."""

    segments: list = field(default_factory=list)
    curves: list = field(default_factory=list)
    fills: list = field(default_factory=list)
    heavy_threshold_pt: float = 0.0
    notes: list = field(default_factory=list)

    @property
    def structural(self) -> list:
        return [s for s in self.segments if s.role == "structural"]

    @property
    def noise(self) -> list:
        return [s for s in self.segments if s.role != "structural"]

    def counts(self) -> dict:
        tally = {"structural": 0, "dashed": 0, "thin": 0}
        for segment in self.segments:
            tally[segment.role] = tally.get(segment.role, 0) + 1
        return tally


def parse_paths(page, settings: dict) -> VectorPaths:
    """Reads the page's drawn paths and marks each one for what it is.

    Never raises. A page whose geometry cannot be read comes back empty, and
    Step 4 then reads it as a picture instead - which is what a plan set
    published as images needs anyway (Critical Rule 6).
    """
    result = VectorPaths()
    try:
        drawings = layout.page_drawings(page)
    except Exception as e:
        logger.exception(f"parse_paths: could not read the drawn paths: {e}")
        result.notes.append("This sheet's drawn paths could not be read.")
        return result

    for index, path in enumerate(drawings):
        try:
            _read_path(index, path, result)
        except Exception as e:
            # One malformed path must never cost the other sixteen thousand.
            logger.debug(f"parse_paths: path {index} skipped: {e}")
            continue

    _mark_dashed(result, settings)
    _mark_thin(result, settings)
    logger.info(
        f"vector paths: {len(result.segments)} segments, {len(result.curves)} curved, "
        f"{len(result.fills)} filled; {result.counts()}"
    )
    return result


def _read_path(index: int, path: dict, into: VectorPaths) -> None:
    try:
        width = float(path.get("width") or 0.0)
    except (TypeError, ValueError):
        width = 0.0
    stated_dashed = _states_a_dash_pattern(path.get("dashes"))
    filled = path.get("fill") is not None
    has_curve = False

    for item in path.get("items", []):
        kind = item[0]
        if kind == "l":
            _add(into, item[1].x, item[1].y, item[2].x, item[2].y, width, stated_dashed, index)
        elif kind == "re":
            rect = item[1]
            corners = [
                (rect.x0, rect.y0, rect.x1, rect.y0),
                (rect.x1, rect.y0, rect.x1, rect.y1),
                (rect.x1, rect.y1, rect.x0, rect.y1),
                (rect.x0, rect.y1, rect.x0, rect.y0),
            ]
            for x0, y0, x1, y1 in corners:
                _add(into, x0, y0, x1, y1, width, stated_dashed, index)
            if filled:
                into.fills.append(
                    {"bbox": [rect.x0, rect.y0, rect.x1, rect.y1], "path_index": index}
                )
        elif kind == "qu":
            quad = item[1]
            points = [quad.ul, quad.ur, quad.lr, quad.ll]
            for first, second in zip(points, points[1:] + points[:1]):
                _add(into, first.x, first.y, second.x, second.y, width, stated_dashed, index)
        elif kind == "c":
            has_curve = True

    if has_curve:
        rect = path.get("rect")
        if rect is not None:
            into.curves.append(
                {
                    "bbox": [rect.x0, rect.y0, rect.x1, rect.y1],
                    "width": width,
                    "path_index": index,
                    "items": [i for i in path.get("items", []) if i[0] == "c"],
                }
            )
    if filled and not has_curve:
        rect = path.get("rect")
        if rect is not None and not any(f["path_index"] == index for f in into.fills):
            into.fills.append(
                {"bbox": [rect.x0, rect.y0, rect.x1, rect.y1], "path_index": index}
            )


def _add(into: VectorPaths, x0, y0, x1, y1, width, dashed, index) -> None:
    if math.hypot(x1 - x0, y1 - y0) < _SHORTEST_SEGMENT_PT:
        return
    into.segments.append(Segment(x0, y0, x1, y1, width, dashed, index))


def _states_a_dash_pattern(dashes) -> bool:
    """Whether the PDF itself says this path is drawn dashed.

    PyMuPDF gives ``"[] 0"`` for a solid line and ``"[ 2.02 2.02 ] 0"`` for a
    dashed one, so what matters is whether there is a number *inside* the
    brackets. Testing the whole string for a digit calls every solid line
    dashed, because of the zero after them.
    """
    if not dashes:
        return False
    try:
        inside = str(dashes).split("[", 1)[1].split("]", 1)[0]
    except IndexError:
        return False
    return any(character.isdigit() and character != "0" for character in inside) or bool(
        inside.strip()
    )


def _line_key(segment: Segment):
    """Which infinite line a segment lies on, as a bucket key.

    Angle folded onto 0-180 and the perpendicular distance from the origin -
    the ordinary way to say "these two pieces are on the same line", and it
    works for a diagonal, which matters because a roof hip and a boundary are
    both drawn dashed and neither is on an axis.
    """
    angle = segment.angle_degrees
    radians = math.radians(angle)
    # Perpendicular offset of the line from the origin.
    rho = segment.x0 * math.sin(radians) - segment.y0 * math.cos(radians)
    return (
        round(angle / _SAME_DIRECTION_DEGREES),
        round(rho / _SAME_LINE_TOLERANCE_PT),
    )


def _along(segment: Segment, radians: float):
    """Where a segment starts and ends measured along its own line."""
    first = segment.x0 * math.cos(radians) + segment.y0 * math.sin(radians)
    second = segment.x1 * math.cos(radians) + segment.y1 * math.sin(radians)
    return (min(first, second), max(first, second))


def _mark_dashed(paths: VectorPaths, settings: dict) -> None:
    """Sets aside every path drawn as a dashed line.

    Both readings are used and either is enough. Where the PDF states a dash
    pattern that settles it; where it does not - which is every plan set tried
    here - the run of short, regularly spaced collinear pieces is the evidence.
    """
    if setting(settings, "noise.use_stated_dash_pattern", True):
        stated = 0
        for segment in paths.segments:
            if segment.stated_dashed:
                segment.role = "dashed"
                segment.reason = "the file states this line is drawn dashed"
                stated += 1
        if stated:
            paths.notes.append(f"{stated} segments carry a dash pattern in the file.")

    if not setting(settings, "noise.detect_dashes_geometrically", True):
        return

    min_pieces = int(number(settings, "noise.dash_min_pieces", 4))
    max_piece = number(settings, "noise.dash_max_piece_length_pt", 12.0)
    max_gap_ratio = number(settings, "noise.dash_max_gap_to_piece", 3.0)
    regularity = number(settings, "noise.dash_gap_regularity", 0.6)

    lines = {}
    for segment in paths.segments:
        if segment.role != "structural" or segment.length > max_piece:
            continue
        lines.setdefault(_line_key(segment), []).append(segment)

    found = 0
    for members in lines.values():
        if len(members) < min_pieces:
            continue
        radians = math.radians(members[0].angle_degrees)
        spans = sorted(((_along(s, radians), s) for s in members), key=lambda pair: pair[0])
        run = [spans[0]]
        for span in spans[1:]:
            gap = span[0][0] - run[-1][0][1]
            piece = max(run[-1][0][1] - run[-1][0][0], 1e-6)
            if 0 <= gap <= max_gap_ratio * piece:
                run.append(span)
                continue
            found += _accept_dash_run(run, min_pieces, regularity)
            run = [span]
        found += _accept_dash_run(run, min_pieces, regularity)

    if found:
        paths.notes.append(
            f"{found} segments are drawn as dashed lines - a roof extent, an eave, a "
            "boundary or a setback - and are not wall faces."
        )
        logger.info(f"vector paths: {found} segments read as dashed from their shape")


def _accept_dash_run(run: list, min_pieces: int, regularity: float) -> int:
    """Marks a run of pieces as a dashed line, where its gaps are regular.

    **Regularity is the whole test.** A wall face broken by a doorway is also a
    line in pieces - but it is two long pieces with one big gap, not eight short
    pieces with seven equal ones. Without this a wall with two doors in it would
    be discarded as a dashed line, which is the worst outcome available here.
    """
    if len(run) < min_pieces:
        return 0
    gaps = [run[i + 1][0][0] - run[i][0][1] for i in range(len(run) - 1)]
    positive = [g for g in gaps if g > 0]
    if len(positive) < min_pieces - 1:
        return 0
    average = sum(positive) / len(positive)
    if average <= 0:
        return 0
    if pstdev(positive) / average > regularity:
        return 0
    for _span, segment in run:
        segment.role = "dashed"
        segment.reason = "drawn as a dashed line - many short pieces with regular gaps"
    return len(run)


def _mark_thin(paths: VectorPaths, settings: dict) -> None:
    """Sets aside line work plotted more lightly than this sheet's own structure.

    The threshold is measured off the sheet, never written down. See the module
    docstring for why a fixed "0.35 pt" would delete more than a third of a real
    floor plan.
    """
    mode = str(setting(settings, "noise.stroke_weight_split", "dominant") or "off").lower()
    absolute = number(settings, "noise.thin_line_absolute_pt", 0.0)

    threshold = 0.0
    if absolute > 0:
        threshold = absolute
        paths.notes.append(f"Lines lighter than {absolute} pt are set aside, as configured.")
    elif mode == "dominant":
        threshold = _dominant_weight_split(paths)
    elif mode == "otsu":
        threshold = _otsu_weight_split(paths, settings)

    paths.heavy_threshold_pt = threshold
    if threshold <= 0:
        return

    thin = 0
    for segment in paths.segments:
        # A width of zero means the PDF states none - a fill boundary, most
        # often, which is the outline of a solid-filled wall. Never set those
        # aside on weight: nothing was claimed about their weight.
        if segment.role == "structural" and 0 < segment.width < threshold:
            segment.role = "thin"
            segment.reason = (
                f"plotted at {segment.width:.2f} pt, lighter than this sheet's own "
                f"structural line work ({threshold:.2f} pt)"
            )
            thin += 1
    if thin:
        logger.info(f"vector paths: {thin} segments set aside as lighter than {threshold:.2f} pt")


def _weight_lengths(paths: VectorPaths) -> dict:
    """How much drawn length this sheet carries at each plotted weight.

    Weighted by length rather than by count, because a sheet's 4,000 short
    hatch strokes must not outvote its 200 long wall faces - and length is
    what a reader sees.
    """
    lengths = {}
    for segment in paths.segments:
        if segment.role != "structural" or segment.width <= 0:
            continue
        key = round(segment.width, 2)
        lengths[key] = lengths.get(key, 0.0) + segment.length
    return lengths


def _dominant_weight_split(paths: VectorPaths) -> float:
    """The weight this sheet draws most of its line work at. Nothing lighter is kept.

    **This rule exists because the obvious one is dangerous**, and the danger
    was measured rather than imagined. Otsu's method over the same histogram
    asks "where is the natural break in this sheet's weights?", and on a real
    plan set the answer is the sheet **border**:

    | sheet | weights present, by drawn length |
    |---|---|
    | one office's floor plan | 0.28 pt 48%, 0.37 pt 24%, 0.51 pt 18%, 0.71 pt 10% |
    | another office's floor plan | **0.17 pt 66%**, 0.42 pt 2%, **1.36 pt 29%** |

    On the second, the 1.36 pt lines are the drawing frame and title block and
    the building itself is drawn at 0.17 pt. An Otsu cut lands at 1.36 and
    deletes the entire plan while looking perfectly reasonable, and the share
    guard does not catch it because the frame carries 29% of the sheet.

    So the rule is turned around: the class carrying the most drawn length **is**
    the drawing, and only what is plotted lighter than the drawing is an
    annotation line. It cannot delete the drawing, because the drawing is what
    defines the threshold.

    *Measured on all three plan sets in use, this removes nothing at all* -
    every one of them plots its walls at the lightest or the dominant weight.
    That is the honest finding: on Australian residential plan sets stroke
    weight does not separate structure from annotation, and what actually does
    is the thickness of the two paired faces, which is Step 4's job. The
    capability is here, dynamic and configurable, for the office whose drawings
    do carry that distinction.
    """
    lengths = _weight_lengths(paths)
    if len(lengths) < 2:
        return 0.0
    dominant = max(lengths, key=lambda weight: lengths[weight])
    lighter = [w for w in lengths if w < dominant]
    if not lighter:
        return 0.0
    return dominant


def _otsu_weight_split(paths: VectorPaths, settings: dict) -> float:
    """The weight that best separates this sheet's heavy line work from its light.

    Otsu's method over a histogram weighted by *drawn length* rather than by
    segment count: a sheet's 4,000 short hatch strokes must not outvote its 200
    long wall faces, and length is what a reader sees.

    Returns 0.0 - meaning no split, keep everything - wherever the split would
    be a guess: one weight on the sheet, or a heavy class too small to be the
    structure of a building.
    """
    lengths = _weight_lengths(paths)
    if len(lengths) < 2:
        return 0.0

    weights = sorted(lengths)
    total = sum(lengths.values())
    if total <= 0:
        return 0.0

    best_split, best_variance = 0.0, -1.0
    for cut in range(1, len(weights)):
        light = weights[:cut]
        heavy = weights[cut:]
        light_mass = sum(lengths[w] for w in light) / total
        heavy_mass = 1.0 - light_mass
        if light_mass <= 0 or heavy_mass <= 0:
            continue
        light_mean = sum(w * lengths[w] for w in light) / (light_mass * total)
        heavy_mean = sum(w * lengths[w] for w in heavy) / (heavy_mass * total)
        variance = light_mass * heavy_mass * (heavy_mean - light_mean) ** 2
        if variance > best_variance:
            best_variance, best_split = variance, weights[cut]

    if best_split <= 0:
        return 0.0

    heavy_share = sum(lengths[w] for w in weights if w >= best_split) / total
    floor = number(settings, "noise.min_heavy_share_of_length", 0.15)
    if heavy_share < floor:
        # **The guard, and it earns its place.** A sheet plotted at one weight
        # throughout has no split to find, and Otsu will hand one back anyway
        # because that is what it does. Acting on it deletes the drawing.
        logger.info(
            f"vector paths: no stroke-weight split taken - a cut at {best_split:.2f} pt would "
            f"leave only {heavy_share:.0%} of this sheet's line work, under the {floor:.0%} floor"
        )
        paths.notes.append(
            "This sheet is plotted at one weight throughout, so its lines were not "
            "separated by weight."
        )
        return 0.0
    return best_split
