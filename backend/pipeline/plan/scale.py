"""Day 4 — scale calibration.

Everything measured from a drawing depends on one number: how many real
millimetres one PDF point represents. A printed scale gives that number
directly — at 1:100, one point (1/72 inch) of paper is 35.28 mm of building.

But a printed scale is a claim, not a measurement. A sheet can be re-plotted at
a different size, or the title block can be wrong, and every length taken from
it would then be wrong by the same factor without anything looking odd. Week 1's
automatic-failure list names this exactly: "Any wrong or unknown unit/scale is
used as if it were confirmed, causing downstream geometry to be materially
wrong."

So the printed scale is **verified against the drawing's own dimension
strings** before anything is measured with it.

The check works because of how a dimension string is drawn: each figure is
centred on the segment it describes. So for a run of figures f1..fn, the
distance on paper between the centre of f1 and the centre of fn is exactly

    f1/2 + f2 + ... + f(n-1) + fn/2   =   sum - f1/2 - fn/2

millimetres of building. That quantity is known from the printed numbers alone,
and the distance between the two centres is measurable on the page. Their ratio
is the real millimetres-per-point, derived from the drawing rather than assumed
from the title block. Taking the median across every usable string on the sheet
makes it robust to a string that is missing a figure.

If the derived figure agrees with the printed scale, the scale is confirmed and
measurements may proceed. If it does not, both are reported and the sheet is
flagged — never silently reconciled.
"""

from statistics import median

from app.logging_setup import get_logger
from pipeline.plan.validators import scale_ratio_denominator

logger = get_logger()

# One PDF point is 1/72 inch of paper.
MM_PER_POINT_AT_FULL_SIZE = 25.4 / 72.0

# A string is only usable for calibration when it has enough figures that the
# two end segments are a small part of the total, and when it spans a real
# distance. Short strings are dominated by rounding and text placement.
_MIN_MEMBERS = 3
_MIN_SPAN_MM = 1000.0


def millimetres_per_point(scale_denominator) -> float:
    """Real millimetres represented by one PDF point at a given scale."""
    if not scale_denominator or scale_denominator <= 0:
        return 0.0
    return MM_PER_POINT_AT_FULL_SIZE * float(scale_denominator)


def _chain_measurement(chain: dict, dimensions_by_id: dict):
    """(expected_mm, drawn_points) for one dimension string, or None."""
    members = [dimensions_by_id[i] for i in chain["member_dimension_ids"] if i in dimensions_by_id]
    members = [m for m in members if m.get("value_mm")]
    if len(members) < _MIN_MEMBERS:
        return None

    axis_index = 0 if chain["axis"] == "x" else 1

    def centre(dimension):
        box = dimension["bbox"]
        return (box[axis_index] + box[axis_index + 2]) / 2.0

    members.sort(key=centre)
    expected_mm = (
        sum(m["value_mm"] for m in members)
        - members[0]["value_mm"] / 2.0
        - members[-1]["value_mm"] / 2.0
    )
    drawn_points = centre(members[-1]) - centre(members[0])
    if expected_mm < _MIN_SPAN_MM or drawn_points <= 0:
        return None
    return expected_mm, drawn_points


def calibrate_page(
    scale_value, dimensions: list, chains: list, config: dict
) -> dict:
    """Confirms (or contradicts) a sheet's printed scale from its own dimensions.

    Returns a record that always states what is known and what is not, so a
    sheet with nothing to check against says so rather than appearing verified.
    """
    settings = config.get("scale_calibration", {})
    tolerance = float(settings.get("tolerance_pct", 5.0))
    minimum_strings = int(settings.get("min_strings_for_check", 2))

    denominator = scale_ratio_denominator(scale_value)
    printed_mm_per_point = millimetres_per_point(denominator)

    record = {
        "printed_scale": scale_value,
        "scale_denominator": denominator,
        "printed_mm_per_point": round(printed_mm_per_point, 4) if printed_mm_per_point else None,
        "measured_mm_per_point": None,
        "variance_pct": None,
        "tolerance_pct": tolerance,
        "strings_used": 0,
        "strings_agreeing": 0,
        "usable_for_measurement": False,
        "result": "not_checked",
        "note": None,
    }

    if not denominator:
        record["note"] = (
            "No usable ratio is printed on this sheet, so nothing on it can be "
            "measured from the drawing."
        )
        return record

    dimensions_by_id = {d["dimension_id"]: d for d in dimensions}
    samples = []
    for chain in chains:
        measurement = _chain_measurement(chain, dimensions_by_id)
        if measurement is None:
            continue
        expected_mm, drawn_points = measurement
        samples.append(expected_mm / drawn_points)

    record["strings_used"] = len(samples)

    if len(samples) < minimum_strings:
        record["note"] = (
            "This sheet does not print enough dimension strings to check its scale "
            "against. The printed scale is used as stated."
        )
        record["usable_for_measurement"] = True
        return record

    measured = median(samples)
    variance = (measured - printed_mm_per_point) / printed_mm_per_point * 100.0
    record["measured_mm_per_point"] = round(measured, 4)
    record["variance_pct"] = round(variance, 2)

    # The strings have to agree with each other before any conclusion is drawn
    # from them. A string that is missing a figure, or that grouped two runs
    # into one, produces a wildly different ratio; on its own that is evidence
    # about the string, not about the sheet's scale. Saying "the scale is
    # wrong" on that basis would be exactly the kind of confident-but-baseless
    # claim the accuracy rules exist to prevent.
    agreement = float(settings.get("sample_agreement_pct", 10.0))
    consistent = [s for s in samples if abs(s - measured) / measured * 100.0 <= agreement]
    record["strings_agreeing"] = len(consistent)
    if len(consistent) * 2 < len(samples):
        record["result"] = "inconclusive"
        record["usable_for_measurement"] = False
        record["note"] = (
            f"This sheet's {len(samples)} dimension strings do not agree with each other "
            f"(only {len(consistent)} are within {agreement:.0f}% of the middle value), so "
            "the printed scale could not be checked. Lengths are not measured from this "
            "sheet until the strings are reviewed."
        )
        return record

    if abs(variance) <= tolerance:
        record["result"] = "confirmed"
        record["usable_for_measurement"] = True
        record["note"] = (
            f"The printed scale was checked against {len(samples)} dimension strings on "
            f"this sheet and agrees to within {abs(variance):.1f}%."
        )
    else:
        # The printed scale is wrong for this sheet — but the sheet's own
        # figures still say what it is. When every string agrees with every
        # other, the drawing is internally consistent and simply not at the
        # scale its title block claims: a sheet drafted at A3 and printed to
        # A4 is exactly this, and it is common. The measured ratio is then the
        # real one and measuring may go ahead, saying plainly that the printed
        # scale was not used.
        #
        # Where the strings do not all agree, nothing is measured. The
        # difference is then evidence about the strings, not about the sheet.
        every_string_agrees = len(consistent) == len(samples)
        record["result"] = "contradicted"
        record["usable_for_measurement"] = every_string_agrees
        if every_string_agrees:
            record["note"] = (
                f"The title block says {record['printed_scale'] or 'this sheet'} is at one "
                f"point to {printed_mm_per_point:.2f} mm, but all {len(samples)} of the "
                f"sheet's own dimension strings measure {measured:.2f} mm — "
                f"{abs(variance):.1f}% apart. The drawing agrees with itself, so it has "
                "most likely been printed at a reduced size. Lengths are measured from the "
                "sheet's own dimensions, not from the printed scale; check the printed "
                "scale before relying on them."
            )
        else:
            record["note"] = (
                f"The printed scale says one point is {printed_mm_per_point:.2f} mm, but this "
                f"sheet's own {len(samples)} dimension strings measure "
                f"{measured:.2f} mm — {abs(variance):.1f}% apart, against a {tolerance}% "
                f"tolerance, and only {len(consistent)} of them agree with each other. "
                "Lengths measured from this sheet cannot be relied on until the "
                "difference is explained."
            )
        logger.warning(
            f"scale contradicted: printed={printed_mm_per_point:.3f} "
            f"measured={measured:.3f} variance={variance:.2f}%"
        )
    return record
