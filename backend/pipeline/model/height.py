"""Day 5 — how tall a wall is.

**A floor plan never says.** It is a horizontal cut through the building, so
height is the one dimension it cannot show. Yet a wall cannot be extruded
without it, and getting it wrong makes every elevation, every area and every
material quantity wrong by the same factor — the exact failure Week 1's
automatic-failure list names first.

So it is not assumed. The drawing set is asked, two independent ways, and the
two are compared:

*   **The figure the sections print.** A section *is* the vertical cut, and it
    dimensions the storey. The figure that recurs most often across the
    section and elevation sheets, within a buildable range, is that dimension.
*   **The distance between the printed levels.** Australian drawings print a
    floor level (FFL, SSL) and the levels above it (CL, FCL, RL) as reduced
    levels in metres. Ceiling level minus floor level is the storey height,
    stated by the drawing in a completely different way.

Where both answer and agree, the height is **confirmed** — two independent
readings of the same building. Where only one answers, it is used and said to
be from that one source. Where neither answers, the office default in
`/config` is used and the model says so on its face.

Nothing here knows anything about a particular plan. The level prefixes, the
buildable range and the tolerance all live in `/config`.
"""

import collections

from app.logging_setup import get_logger

logger = get_logger()


def _settings(config: dict) -> dict:
    return config.get("model", {})


def _plausible_range(config: dict):
    band = _settings(config).get("storey_height_mm", {})
    return float(band.get("min", 2100)), float(band.get("max", 3600))


def height_from_printed_figures(pages: list, config: dict):
    """The storey height as the sections dimension it.

    A section sheet dimensions the same storey several times over — floor to
    ceiling in one room, then the next. The figure that recurs most is that
    height; a one-off figure in the same range is a window head or a door, so
    a figure has to appear more than once to count.
    """
    lowest, highest = _plausible_range(config)
    wanted_types = set(
        _settings(config).get("height_page_types", ["section", "elevation"])
    )
    minimum_repeats = int(_settings(config).get("height_min_repeats", 2))

    counts: collections.Counter = collections.Counter()
    where: dict = {}
    for page in pages:
        if page.get("page_type", {}).get("value") not in wanted_types:
            continue
        for dimension in page.get("dimensions", []):
            value = dimension.get("value_mm")
            if dimension.get("kind") != "linear" or not value:
                continue
            if lowest <= value <= highest:
                counts[round(value)] += 1
                where.setdefault(round(value), []).append(page["sheet_id"])

    if not counts:
        return None

    value, repeats = counts.most_common(1)[0]
    if repeats < minimum_repeats:
        return None
    return {
        "value_mm": float(value),
        "method": "printed_on_a_section",
        "times_printed": repeats,
        "sheets": sorted(set(where[value]))[:6],
    }


def height_from_levels(pages: list, config: dict):
    """The storey height as the difference between two printed levels.

    A reduced level is an absolute height above a datum, printed in metres:
    'FFL 100.40' is the finished floor, 'RL 103.10' a level above it. Their
    difference is the storey height, and it is arrived at without looking at a
    single dimension string.
    """
    lowest, highest = _plausible_range(config)
    floor_prefixes = {
        p.upper() for p in _settings(config).get("floor_level_prefixes", [])
    }

    floors: collections.Counter = collections.Counter()
    others: collections.Counter = collections.Counter()
    for page in pages:
        for dimension in page.get("dimensions", []):
            if dimension.get("kind") != "level":
                continue
            value = dimension.get("value_mm")
            reference = (dimension.get("level_reference") or "").upper()
            if not value:
                continue
            if reference in floor_prefixes:
                floors[round(value)] += 1
            else:
                others[round(value)] += 1

    if not floors or not others:
        return None

    # The floor the building is mostly set out from, and the level above it
    # that most of the drawing agrees on.
    floor_level, _ = floors.most_common(1)[0]
    differences: collections.Counter = collections.Counter()
    for level, times in others.items():
        gap = level - floor_level
        if lowest <= gap <= highest:
            differences[round(gap)] += times

    if not differences:
        return None

    value, times = differences.most_common(1)[0]
    return {
        "value_mm": float(value),
        "method": "difference_between_printed_levels",
        "floor_level_mm": float(floor_level),
        "level_above_mm": float(floor_level + value),
        "times_printed": times,
    }


def resolve_storey_height(pages: list, config: dict) -> dict:
    """The storey height to build with, and where it came from.

    Always returns a usable height. What changes is how much it is trusted and
    what the model says about it — never whether a number appears.
    """
    settings = _settings(config)
    default_mm = float(settings.get("storey_height_mm", {}).get("default", 2700))
    tolerance = float(settings.get("height_agreement_tolerance_mm", 50))

    printed = height_from_printed_figures(pages, config)
    levels = height_from_levels(pages, config)

    if printed and levels:
        difference = abs(printed["value_mm"] - levels["value_mm"])
        if difference <= tolerance:
            # Two readings of the same building, arrived at independently.
            record = {
                "value_mm": printed["value_mm"],
                "source": "confirmed_by_the_drawing",
                "confidence": 0.95,
                "note": (
                    f"The drawing states this height two independent ways and they "
                    f"agree: {printed['value_mm']:.0f} mm is dimensioned "
                    f"{printed['times_printed']} times on the section sheets, and the "
                    f"printed levels are {levels['value_mm']:.0f} mm apart "
                    f"({levels['floor_level_mm'] / 1000:.3f} m floor to "
                    f"{levels['level_above_mm'] / 1000:.3f} m above)."
                ),
            }
        else:
            # Both answered and they disagree. Neither is assumed correct; the
            # dimensioned figure is used because a section dimensions the
            # storey directly, and the disagreement is put on the record.
            record = {
                "value_mm": printed["value_mm"],
                "source": "dimensioned_but_levels_disagree",
                "confidence": 0.6,
                "note": (
                    f"The section sheets dimension {printed['value_mm']:.0f} mm, but the "
                    f"printed levels are {levels['value_mm']:.0f} mm apart — "
                    f"{difference:.0f} mm apart, against a {tolerance:.0f} mm tolerance. "
                    "The dimensioned figure is used because a section dimensions the "
                    "storey directly. Check this before relying on any height."
                ),
            }
    elif printed:
        record = {
            "value_mm": printed["value_mm"],
            "source": "printed_on_a_section",
            "confidence": 0.8,
            "note": (
                f"Taken from the sheets themselves: {printed['value_mm']:.0f} mm is "
                f"dimensioned {printed['times_printed']} times on "
                f"{', '.join(printed['sheets'])}. No printed levels were available to "
                "check it against."
            ),
        }
    elif levels:
        record = {
            "value_mm": levels["value_mm"],
            "source": "difference_between_printed_levels",
            "confidence": 0.75,
            "note": (
                f"Taken from the levels printed on the drawing: "
                f"{levels['floor_level_mm'] / 1000:.3f} m floor to "
                f"{levels['level_above_mm'] / 1000:.3f} m above is "
                f"{levels['value_mm']:.0f} mm. No section dimensioned it directly."
            ),
        }
    else:
        record = {
            "value_mm": default_mm,
            "source": "office_default",
            "confidence": 0.3,
            "note": (
                f"This plan set does not state a storey height anywhere the reader could "
                f"find it — no section dimensions one and no levels are printed. The "
                f"office default of {default_mm:.0f} mm from /config is used instead. "
                "Every height in this model is that assumption, not a measurement."
            ),
        }

    record["default_mm"] = default_mm
    record["printed_evidence"] = printed
    record["level_evidence"] = levels
    logger.info(
        f"storey height {record['value_mm']:.0f} mm ({record['source']}, "
        f"confidence {record['confidence']})"
    )
    return record
