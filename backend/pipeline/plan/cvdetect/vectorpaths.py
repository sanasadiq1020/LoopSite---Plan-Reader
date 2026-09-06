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

# One PDF point is 1/72 inch of paper. A unit definition, not a tuning value.
MM_PER_POINT = 25.4 / 72.0


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
        tally = {"structural": 0, "dashed": 0, "thin": 0, "structure": 0}
        for segment in self.segments:
            tally[segment.role] = tally.get(segment.role, 0) + 1
        return tally


def parse_paths(page, settings: dict, scale=None) -> VectorPaths:
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
    _mark_inside_labelled_structures(page, result, settings, scale)
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


def _mark_inside_labelled_structures(page, paths: VectorPaths, settings: dict,
                                     scale=None) -> None:
    """Sets aside the line work inside a region the sheet names as not a room.

    **A pergola's rafters are two parallel lines a wall thickness apart**, and
    they are joined to the house, so neither the thickness test nor the
    detached-structure test can see them - they arrive at Step 4 as walls and
    are skeletonised into walls. What the drawing does say is the word printed
    over them: PERGOLA, CARPORT, EXTENT OF ROOF, VERANDAH.

    **Reading a word is a weaker instrument than reading geometry, and this
    project has recorded that twice** (Sections 4AR and 4AV): a word is an
    office's habit, so a rule built on one works on the drawings whose
    vocabulary is in the list and silently does nothing on the rest. It is
    therefore paired with a geometric rule that needs no vocabulary - the
    open-grid test in ``cvwalls`` - and every word lives in
    ``noise.structure_labels`` in ``/config``, never here (Critical Rule 1).

    **The region is the box the office drew, not a radius round the label.**
    A radius is the rule that was measured at its worst on this project: at
    150 points it set aside 99 real walls on one floor plan. So the label's own
    enclosing rectangle is taken from the sheet's own ruling lines - the same
    ``drawn_box_around`` that finds a title block, one definition rather than
    two (Critical Rule 2) - and where the drawing rules no such box, nothing is
    set aside unless a reach is configured explicitly.

    A segment is only set aside when **both** of its ends are inside the
    region: a wall of the house running out under a pergola crosses the
    boundary, and cutting it would take a real wall with the rafters.
    """
    words = setting(settings, "noise.structure_labels", None) or []
    if not words or page is None:
        return
    try:
        from pipeline.plan import textmodel

        lines = textmodel.extract_native_lines(page)
    except Exception as e:
        logger.exception(f"the sheet's own labels could not be read: {e}")
        return

    wanted = [str(word).strip().upper() for word in words if str(word).strip()]
    extra = int(number(settings, "noise.structure_label_max_extra_words", 2))
    labels = [
        line for line in lines
        if line.get("bbox") and _names_a_structure(line.get("text", ""), wanted, extra)
    ]
    if not labels:
        return

    # Bounded to the line work around each label, for the reason recorded in
    # ``grids_under_labels``: the pairwise search over a whole vector floor
    # plan's 45,661 segments cost three minutes a plan set and found the same
    # grids.
    members = grids_under_labels(paths.structural, labels, settings)
    if not members:
        logger.info(
            f"vector paths: {len(labels)} structure label(s) printed, but no open grid "
            "of line work under any of them, so nothing was set aside"
        )
        return
    grids = [(_box_of(members), members)]

    set_aside, named = 0, []
    for region, members in grids:
        over_it = [
            line for line in labels if _boxes_meet(line["bbox"], region)
        ]
        if not over_it:
            continue
        text = str(over_it[0].get("text", "")).strip()
        marked = 0
        for segment in members:
            if segment.role != "structural":
                continue
            segment.role = "structure"
            segment.reason = (
                f"This is one of a row of evenly spaced parallel lines under the "
                f"label '{text}', which is a roof or a grid over the house rather "
                "than a wall of it."
            )
            marked += 1
        if marked:
            set_aside += marked
            named.append(f"{text} ({marked})")

    if set_aside:
        paths.notes.append(
            f"{set_aside} segment(s) are the open grid of a structure the sheet "
            f"names rather than a room: {', '.join(named)}."
        )
        logger.info(
            f"vector paths: {set_aside} segment(s) set aside as the grid under "
            f"{', '.join(named)}"
        )


def _names_a_structure(text: str, wanted: list, max_extra_words: int = 0) -> bool:
    """Whether a printed line *names* one of the configured structures.

    **A structure label is a name, not a sentence**, and this is the same rule
    room names already live under (Section 4R). Matching the word anywhere in a
    line was measured on the plan sets in use and is unusable: it catches
    ``200x50 Salvaged timber rafters on pergola`` (a construction note),
    ``TYPICAL DETAIL - RAFTER TO VERANDAH BEAM`` (a drawing caption),
    ``ALLOWABLE WATTAGE - INTERNAL 5W/M2, VERANDAH OR BALCONY`` (an energy
    note) and ``patio and carport systems this section`` (a sentence from a
    specification). Every one of those would have set aside line work somewhere
    it has no business being.

    So the printed line may carry at most a couple of words beyond the phrase
    it matched: ``PERGOLA``, ``CARPORT`` and ``PROPOSED CARPORT OVER`` are
    labels; a sentence is not.
    """
    said = " ".join(str(text).upper().replace(".", " ").replace(",", " ").split())
    padded = f" {said} "
    for word in wanted:
        if f" {word} " not in padded:
            continue
        if max_extra_words <= 0:
            return True
        if len(said.split()) - len(word.split()) <= max_extra_words:
            return True
    return False


def _open_grids(paths: VectorPaths, settings: dict) -> list:
    """Runs of parallel, evenly spaced lines - a roof grid rather than walls.

    **This is the half of the rule that needs no vocabulary.** A pergola, a
    carport, a set of roof rafters and a joist layout are all drawn the same
    way whatever an office calls them: several parallel lines, spanning the
    same stretch, at the same spacing. A wall has none of those properties -
    two walls of a house are not evenly spaced with four more like them.

    Measured on the sheets in use, the office rules no rectangle around its
    pergola at all, so there is no drawn box to read: the grid *is* the region.
    Each group is returned with the box it occupies and the segments in it.
    """
    least = int(number(settings, "noise.grid_min_lines", 4))
    regularity = number(settings, "noise.grid_spacing_regularity", 0.25)
    share = number(settings, "noise.grid_min_overlap_share", 0.6)
    if least < 3:
        return []

    grids = []
    try:
        for axis in ("h", "v"):
            members = [
                segment for segment in paths.segments
                if segment.role == "structural" and _grid_axis(segment) == axis
            ]
            for group in _parallel_runs(members, axis, share):
                if len(group) < least:
                    continue
                grid = _regular_stretch(group, axis, regularity, least)
                if grid:
                    grids.append((_box_of(grid), grid))
    except Exception as e:
        logger.exception(f"the open grids could not be worked out: {e}")
        return []
    return grids


def structure_labels_on(page, settings: dict) -> list:
    """Every printed line on this sheet that names a structure, with its box."""
    words = setting(settings, "noise.structure_labels", None) or []
    if not words or page is None:
        return []
    wanted = [str(word).strip().upper() for word in words if str(word).strip()]
    extra = int(number(settings, "noise.structure_label_max_extra_words", 2))
    try:
        from pipeline.plan import textmodel

        return [
            line for line in (textmodel.extract_native_lines(page) or [])
            if line.get("bbox") and _names_a_structure(line.get("text", ""), wanted, extra)
        ]
    except Exception as e:
        logger.exception(f"the sheet's own labels could not be read: {e}")
        return []


def grids_under_labels(segments: list, labels: list, settings: dict) -> list:
    """The **lines** of every open grid a structure label is printed on.

    Shared by both readers, so a sheet whose drawing is line work and one whose
    drawing is a picture are judged by the same rule (Critical Rule 2). The
    picture reader hands in the runs LSD recovered; the vector reader hands in
    the paths the drafter plotted.

    **The grid's own lines, never the rectangle round them**, and that
    distinction cost a floor plan when it was got wrong. Masking the bounding
    box of a rafter run removes everything standing inside it - which is the
    room the pergola is drawn over, and its walls. Measured: one sheet went from
    31 walls to 9, and its openings from 5 to 1. A rafter is a line; only the
    line is taken out.

    **The label has to sit inside the grid, not merely touch its box.** A box
    that reaches a label printed a centimetre away is a box that reaches most of
    the drawing.
    """
    if not labels or not segments:
        return []

    # **Only the line work a label could possibly be standing on.** Finding
    # grids is a pairwise search, and one vector floor plan carries 45,661
    # structural segments - run over all of them it took a 23-sheet plan set
    # from 41 seconds to 237. The label has to lie inside the grid for the grid
    # to count at all, so the search is bounded to the segments around each
    # label before it starts, and the answer is unchanged.
    reach = number(settings, "noise.structure_label_reach_pt", 300.0)
    near = [
        segment for segment in segments
        if any(_near_the_label(segment, line["bbox"], reach) for line in labels)
    ]
    if not near:
        return []

    found = []
    holder = VectorPaths(segments=near)
    for region, members in _open_grids(holder, settings):
        if any(_centre_inside(line["bbox"], region) for line in labels):
            found.extend(members)
    return found


def _near_the_label(segment: "Segment", box, reach: float) -> bool:
    low_x, high_x = sorted((segment.x0, segment.x1))
    low_y, high_y = sorted((segment.y0, segment.y1))
    return not (
        high_x < box[0] - reach or low_x > box[2] + reach
        or high_y < box[1] - reach or low_y > box[3] + reach
    )


def _centre_inside(box, region) -> bool:
    x = (box[0] + box[2]) / 2.0
    y = (box[1] + box[3]) / 2.0
    return region[0] <= x <= region[2] and region[1] <= y <= region[3]


def segments_from_rulings(rulings: dict) -> list:
    """Axis-aligned ruling runs as segments, so the grid rule can read them.

    ``h`` entries are ``(across, low, high)`` and ``v`` entries the same, which
    is the shape both ``layout.extract_rulings`` and the picture reader return.
    """
    made = []
    for across, low, high in rulings.get("h", []) or []:
        made.append(Segment(float(low), float(across), float(high), float(across),
                            0.0, False, -1))
    for across, low, high in rulings.get("v", []) or []:
        made.append(Segment(float(across), float(low), float(across), float(high),
                            0.0, False, -1))
    return made


def _grid_axis(segment: "Segment"):
    angle = segment.angle_degrees
    if angle <= _SAME_DIRECTION_DEGREES or angle >= 180.0 - _SAME_DIRECTION_DEGREES:
        return "h"
    if abs(angle - 90.0) <= _SAME_DIRECTION_DEGREES:
        return "v"
    return None


def _across(segment: "Segment", axis: str) -> float:
    return (segment.y0 + segment.y1) / 2.0 if axis == "h" else (segment.x0 + segment.x1) / 2.0


def _extent(segment: "Segment", axis: str):
    if axis == "h":
        return (min(segment.x0, segment.x1), max(segment.x0, segment.x1))
    return (min(segment.y0, segment.y1), max(segment.y0, segment.y1))


def _parallel_runs(members: list, axis: str, share: float) -> list:
    """Every set of parallel lines that spans the same stretch as one of them.

    **Grouped by what they overlap, not by where they fall in a sorted list.**
    Sorting the sheet's lines across the page and cutting the list wherever two
    neighbours do not overlap was the first attempt, and on a floor plan it
    finds nothing: the rafters of a pergola are interleaved in that order with
    every wall, fitting and dimension line at the same range of positions, so
    the run is broken before it starts. Measured, it found one group of six
    lines thirty points across on a sheet whose pergola is a metre wide.
    """
    groups, seen = [], set()
    for seed in members:
        low, high = _extent(seed, axis)
        if high - low <= 0:
            continue
        group = [
            segment for segment in members
            if _shares_the_run(segment, seed, axis, share)
        ]
        if len(group) < 3:
            continue
        key = tuple(sorted(round(_across(segment, axis), 2) for segment in group))
        if key in seen:
            continue
        seen.add(key)
        groups.append(group)
    return groups


def _shares_the_run(one: "Segment", other: "Segment", axis: str, share: float) -> bool:
    """Whether two parallel lines run alongside each other for most of their length."""
    first_low, first_high = _extent(one, axis)
    second_low, second_high = _extent(other, axis)
    shorter = min(first_high - first_low, second_high - second_low)
    if shorter <= 0:
        return False
    return (min(first_high, second_high) - max(first_low, second_low)) >= share * shorter


def _regular_stretch(group: list, axis: str, regularity: float, least: int) -> list:
    """The longest run of these lines that sits at one repeated spacing.

    **A sub-run, not the whole group**, because the lines that span the same
    stretch as a rafter include the walls the rafters run between. What makes a
    grid a grid is that its members are evenly spaced, and that is scale-free:
    a rafter layout at 450 mm centres and one at 900 mm both pass, and two
    walls of a house do not.
    """
    ordered = sorted(group, key=lambda segment: _across(segment, axis))
    positions = [_across(segment, axis) for segment in ordered]

    best = []
    start = 0
    for index in range(1, len(positions)):
        gap = positions[index] - positions[index - 1]
        run = positions[start:index + 1]
        gaps = [b - a for a, b in zip(run, run[1:])]
        middle = median(gaps) if gaps else 0.0
        if gap <= 0.01 or middle <= 0 or pstdev(gaps) / middle > regularity:
            if index - start >= len(best):
                best = list(range(start, index))
            start = index - 1
    if len(positions) - start > len(best):
        best = list(range(start, len(positions)))

    if len(best) < least:
        return []
    return [ordered[i] for i in best]


def _box_of(group: list):
    return (
        min(min(s.x0, s.x1) for s in group), min(min(s.y0, s.y1) for s in group),
        max(max(s.x0, s.x1) for s in group), max(max(s.y0, s.y1) for s in group),
    )


def _boxes_meet(first, second) -> bool:
    return not (
        first[2] < second[0] or first[0] > second[2]
        or first[3] < second[1] or first[1] > second[3]
    )


def _mark_thin(paths: VectorPaths, settings: dict) -> None:
    """Sets aside line work plotted more lightly than this sheet's own structure.

    The threshold is measured off the sheet, never written down. See the module
    docstring for why a fixed "0.35 pt" would delete more than a third of a real
    floor plan.
    """
    mode = str(setting(settings, "noise.stroke_weight_split", "dominant") or "off").lower()
    absolute = number(settings, "noise.thin_line_absolute_pt", 0.0)
    # **AS 1100 states line widths in millimetres of paper**, so an office
    # thinking in those terms sets the figure that way and it is converted
    # here. One point is 1/72 inch, which is 0.3528 mm.
    in_mm = number(settings, "noise.thin_line_min_mm", 0.0)
    if in_mm > 0:
        absolute = max(absolute, in_mm / MM_PER_POINT)

    threshold = 0.0
    if absolute > 0:
        threshold = absolute
        paths.notes.append(
            f"Lines lighter than {absolute:.2f} pt "
            f"({absolute * MM_PER_POINT:.2f} mm on the paper) are set aside, as configured."
        )
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
