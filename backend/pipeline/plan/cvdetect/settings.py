"""What this reader is allowed to differ on, and the scale that turns it into pixels.

**Nothing in this package may carry a distance.** A wall is 90 mm or 230 mm
thick in the building; on the paper it is whatever the drawing's scale makes
it, and at 300 DPI that is a different number of pixels on a 1:50 detail and a
1:200 site plan. So every threshold this reader uses is stated once, in
millimetres of building or in points of paper, in ``config/cv_detection.json``
- and is turned into pixels through the scale measured off the sheet itself
(``calibration.py``). A pixel figure written into the code would be a promise
about one drawing at one scale, and the first sheet drawn differently would
break it silently.

Two kinds of quantity live here and the difference matters:

*   **Millimetres of building** - how thick a wall is, how wide a door is.
    These pass through the calibrated scale.
*   **Points of paper** - how long a dash is, how heavily a line is plotted.
    AS 1100.301 states these on the *paper*, so they are the same on a 1:50
    detail and a 1:200 site plan and must never be scaled.

The values in ``_DEFAULTS`` exist so that a missing or unreadable config file
degrades to a logged, working run rather than taking the reading down
(Critical Rule 6). The file on disk is the source of truth.
"""

import json
from dataclasses import dataclass

from app.logging_setup import get_logger
from app.paths import CONFIG_DIR

logger = get_logger()

CONFIG_PATH = CONFIG_DIR / "cv_detection.json"

# One PDF point is 1/72 inch of paper. A unit definition, not a tuning value,
# which is why it is in code and not in the config file.
POINTS_PER_INCH = 72.0

_DEFAULTS = {
    "render_dpi": 300,
    "max_megapixels": 40.0,
    "noise": {
        "use_stated_dash_pattern": True,
        "detect_dashes_geometrically": True,
        "dash_min_pieces": 4,
        "dash_max_piece_length_pt": 12.0,
        "dash_max_gap_to_piece": 3.0,
        "dash_gap_regularity": 0.6,
        "stroke_weight_split": "dominant",
        "thin_line_absolute_pt": 0.0,
        "min_heavy_share_of_length": 0.15,
    },
    "scale": {
        "dimension_min_mm": 100.0,
        "dimension_max_mm": 100000.0,
        "search_radius_text_heights": 4.0,
        "min_samples": 6,
        "agreement_pct": 5.0,
        "consensus_share": 0.5,
        "min_samples_to_contradict_printed": 12,
        "contradiction_agreement_share": 0.9,
        "fall_back_to_printed_scale": True,
        "title_block_band_share": 0.25,
        "label_reach_text_heights": 6.0,
        "correct_for_stated_sheet_size": True,
        "sheet_size_tolerance_pct": 2.0,
    },
    "openings": {
        "door_min_width_mm": 600.0,
        "door_max_width_mm": 2400.0,
        "window_min_width_mm": 300.0,
        "window_max_width_mm": 6000.0,
        "arc_squareness": 1.6,
        "arc_min_sweep_degrees": 55.0,
        "arc_search_radius_mm": 60.0,
        "hough_when_no_vector_arcs": True,
        "hough_accumulator_ratio": 1.5,
        "hough_proposal_radius_px": 18.0,
        "picture_share_of_sheet": 0.1,
        "mark_prefixes": {
            "door": ["D", "SD", "BD", "GD", "CD", "FD"],
            "window": ["W", "AW", "DH", "FW", "SW", "HW"],
        },
        "glazing_min_lines": 2,
        "glazing_max_lines": 6,
        "mask_padding_mm": 40.0,
    },
    "wall": {
        "min_thickness_mm": 70.0,
        "max_thickness_mm": 320.0,
        "min_length_mm": 600.0,
        "spur_mm": 600.0,
        "carry_on_angle_degrees": 25.0,
        "simplify_mm": 30.0,
        "merge_collinear_runs": True,
        "thickness_from_the_faces": True,
        "collinear_lateral_tolerance_pt": 3.5,
        "collinear_max_gap_mm": 6000.0,
        "thinning_pixel_budget": 2000000.0,
        "thinning_min_wall_px": 4.0,
        "closing_share_of_thickest_wall": 1.0,
        "solid_fill_share": 0.85,
        "hatch_share_of_ink": 0.5,
        "hatch_angle_degrees": [45.0, 135.0],
        "min_length_to_thickness": 1.5,
        "centreline_tolerance_share_of_thickness": 0.5,
        "thickness_agreement_share": 0.5,
        "min_walls_for_vector": 4,
    },
    "page": {
        "never_trace_walls_on": [
            "SITE PLAN", "ROOF PLAN", "LANDSCAPE PLAN", "DRAINAGE PLAN",
            "STORMWATER PLAN", "SEDIMENT CONTROL", "EROSION CONTROL",
            "LOCALITY PLAN", "SURVEY PLAN", "SHADOW DIAGRAM", "SITE ANALYSIS",
            "DEMOLITION PLAN", "LOCATION PLAN",
        ],
        "traces_walls_on": [
            "FLOOR PLAN", "SLAB PLAN", "SLAB SETOUT", "SETOUT PLAN",
            "FRAMING PLAN", "LAYOUT PLAN", "REFLECTED CEILING",
            "ELECTRICAL PLAN",
        ],
    },
    "breaks": {
        "punch_before_closing": False,
        "band_match_share_of_thickness": 1.5,
        "min_face_overlap_share": 0.5,
        "min_wall_each_side_mm": 300.0,
        "collinear_tolerance_pt": 0.6,
        "clearance_mm": 40.0,
        "clearance_share_of_thickness": 0.0,
    },
    "crosslink": {
        "max_offset_mm": 200.0,
        "mark_reach_mm": 1000.0,
    },
}

_cache = None


def load_settings(overrides: dict = None) -> dict:
    """The reader's settings, with ``config/cv_detection.json`` laid over them.

    An unreadable file is logged and the defaults are used, because a run that
    reads a plan slightly less well is worth more than a run that does not
    happen at all (Critical Rule 6).
    """
    global _cache
    if _cache is None:
        merged = _deep_merge(_DEFAULTS, {})
        try:
            on_disk = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            merged = _deep_merge(merged, on_disk)
            logger.info(f"cv detection settings read from {CONFIG_PATH.name}")
        except FileNotFoundError:
            logger.info(
                f"{CONFIG_PATH.name} is not present; the built-in detection settings are used"
            )
        except Exception as e:
            logger.exception(f"Could not read {CONFIG_PATH}, using the built-in settings: {e}")
        _cache = merged
    if not overrides:
        return _cache
    return _deep_merge(_cache, overrides)


def forget_settings() -> None:
    """Drops the cached settings, so a test can change the file and re-read it."""
    global _cache
    _cache = None


def _deep_merge(base: dict, over: dict) -> dict:
    """``over`` laid on ``base``, section by section rather than wholesale.

    A config file naming one wall setting must not silently discard the other
    nine, which is what a shallow update would do.
    """
    result = dict(base)
    for key, value in (over or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def setting(settings: dict, dotted: str, default=None):
    """One setting by its dotted name, falling back rather than raising."""
    node = settings
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            fallback = _from_defaults(dotted)
            return default if fallback is None else fallback
        node = node[part]
    return node


def _from_defaults(dotted: str):
    node = _DEFAULTS
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def number(settings: dict, dotted: str, default: float = 0.0) -> float:
    """One setting as a number. A value that is not one is logged, not raised."""
    value = setting(settings, dotted, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        logger.warning(f"detection setting {dotted!r} is not a number; using {default}")
        return float(default)


@dataclass(frozen=True)
class Scale:
    """How a millimetre of building, a point of paper and a pixel relate.

    Three spaces are in play and confusing any two of them puts a wall in the
    wrong place:

    *   **Building millimetres** - what a reader and a take-off care about.
    *   **PDF points** - the page's own coordinates, 1/72 inch of paper. The
        drawing's *scale* is the only thing connecting these to the first, and
        it is measured, never assumed (see ``calibration.py``).
    *   **Pixels** of the rendered image, which is where OpenCV works. This
        one is exact: it is the render resolution and nothing else.

    ``origin`` is the page rectangle's own corner. A page whose mediabox does
    not start at the corner of the paper renders from ``page.rect``, so
    dropping the offset would put every recovered line the same distance away
    from the drawing it came from.
    """

    mm_per_point: float
    dpi: float
    origin: tuple = (0.0, 0.0)
    source: str = "unknown"
    confidence: float = 0.0
    samples: int = 0
    note: str = ""
    printed_mm_per_point: float = 0.0
    measured_mm_per_point: float = 0.0
    variance_pct: float = None

    @property
    def pixels_per_point(self) -> float:
        return self.dpi / POINTS_PER_INCH

    @property
    def mm_per_pixel(self) -> float:
        return self.mm_per_point / self.pixels_per_point if self.pixels_per_point else 0.0

    @property
    def usable(self) -> bool:
        """Whether any length may be taken from this sheet at all.

        A sheet whose scale could not be established is still read for what it
        draws, but no length is reported from it - a wrong scale makes every
        length wrong by the same factor with nothing looking odd, which is the
        one failure this product must never produce silently.
        """
        return self.mm_per_point > 0.0

    def px_from_mm(self, mm: float) -> float:
        """A building distance as a number of pixels on the rendered page."""
        if self.mm_per_pixel <= 0:
            return 0.0
        return float(mm) / self.mm_per_pixel

    def mm_from_px(self, pixels: float) -> float:
        return float(pixels) * self.mm_per_pixel

    def px_from_pt(self, points: float) -> float:
        """A paper distance as pixels. Never passes through the drawing scale."""
        return float(points) * self.pixels_per_point

    def point_to_pixel(self, x: float, y: float):
        """A point in the page's own space as a pixel in the rendered image."""
        zoom = self.pixels_per_point
        return ((x - self.origin[0]) * zoom, (y - self.origin[1]) * zoom)

    def pixel_to_point(self, x: float, y: float):
        zoom = self.pixels_per_point
        if zoom <= 0:
            return (0.0, 0.0)
        return (x / zoom + self.origin[0], y / zoom + self.origin[1])

    def as_record(self) -> dict:
        """What the sheet says about its own scale, for the run's output."""
        return {
            "mm_per_point": round(self.mm_per_point, 5),
            "mm_per_pixel": round(self.mm_per_pixel, 5),
            "render_dpi": self.dpi,
            "source": self.source,
            "confidence": round(self.confidence, 3),
            "samples_used": self.samples,
            "printed_mm_per_point": round(self.printed_mm_per_point, 5) or None,
            "measured_mm_per_point": round(self.measured_mm_per_point, 5) or None,
            "variance_pct": self.variance_pct,
            "usable_for_measurement": self.usable,
            "note": self.note,
        }
