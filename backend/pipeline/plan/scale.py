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

# ISO 216 long edges in millimetres. A standard, not an office's preference, so
# it lives in code beside the fact that a point is 1/72 inch.
_ISO_LONG_EDGE_MM = {
    "A0": 1189.0, "A1": 841.0, "A2": 594.0, "A3": 420.0, "A4": 297.0, "A5": 210.0,
}

# Where the scale determination came from. Reported on every sheet, because a
# ratio measured off the drawing and a ratio read off the title block are not
# the same kind of answer and must never look alike.
SOURCE_MEASURED_AND_PRINTED_AGREE = "the sheet's dimension strings confirm its printed scale"
SOURCE_MEASURED = "the sheet's own dimension strings"
SOURCE_PRINTED = "the printed scale, unchecked"
SOURCE_PRINTED_CORRECTED = "the printed scale, corrected for the sheet size it names"
SOURCE_UNKNOWN = "unknown"

# **"printed_only" is not a pass and must never read as one.** It says the sheet
# states a scale, nothing on the sheet checked it, and every length taken from
# it is therefore unverified. It is kept distinct from "confirmed" everywhere -
# in the record, in the checks and on screen - because the whole purpose of
# calibrating is that a claim and a measurement are different things.
RESULT_PRINTED_ONLY = "printed_only"


def _sheet_size_correction(scale_value, page_size_pt):
    """How far out a printed ratio is because the sheet was plotted to another size.

    ``1:50 @ A3`` says the ratio holds when the drawing is printed at A3. If the
    page really is A3 it stands; if it is A4 the drawing has been reduced and
    every length taken from the printed ratio is out by the ratio of the two
    long edges - 1.414 between one A size and the next, which turns a 3 m wall
    into 2.1 m. ISO 216 sizes are a standard and the page states its own size,
    so the correction is exact.

    Returns ``(factor, named_size, actual_size)``; the factor is 1.0 where there
    is nothing to correct, and ``named_size`` is None where the sheet names no
    size at all - which is most sheets, and is not a fault.
    """
    import re

    if not scale_value or not page_size_pt:
        return 1.0, None, None
    match = re.search(r"@\s*(A[0-5])\b", str(scale_value).upper())
    if not match:
        return 1.0, None, None
    named = match.group(1)
    long_edge_mm = max(page_size_pt) * MM_PER_POINT_AT_FULL_SIZE
    actual = min(
        _ISO_LONG_EDGE_MM,
        key=lambda size: abs(_ISO_LONG_EDGE_MM[size] - long_edge_mm),
    )
    # Only believe the page is a standard size when it really is one; a trimmed
    # or custom sheet is corrected by nothing rather than by a guess.
    if abs(_ISO_LONG_EDGE_MM[actual] - long_edge_mm) > 15.0:
        return 1.0, named, None
    if actual == named:
        return 1.0, named, actual
    return _ISO_LONG_EDGE_MM[named] / _ISO_LONG_EDGE_MM[actual], named, actual


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


def _two_populations(samples: list, agreement_pct: float, min_share: float):
    """Whether these ratios are two scales rather than one, with scatter.

    **A sheet may carry two drawings at two scales** - a floor plan at 1:100
    with an enlarged detail beside it at 1:20 - and pooling their strings gives
    a median that is correct for neither drawing. A median cannot see this: half
    the samples either side of it look exactly like scatter around one value.

    So the sorted ratios are split at their largest relative gap. It is two
    populations when that gap is wider than strings are allowed to disagree by
    **and** each side holds a real share of the samples - one outlier at the end
    of the range is a bad string, not a second drawing.

    Returns ``(is_two, low_median, high_median, gap_pct)``.
    """
    if len(samples) < 4:
        # Below four there is no way to tell a second population from a single
        # stray reading, and calling two strings "two scales" would flag every
        # sheet whose two strings merely disagree - which is already reported.
        return False, None, None, None
    ordered = sorted(samples)
    best_gap, cut = 0.0, None
    for index in range(1, len(ordered)):
        gap = (ordered[index] - ordered[index - 1]) / ordered[index - 1] * 100.0
        if gap > best_gap:
            best_gap, cut = gap, index
    if cut is None or best_gap <= agreement_pct:
        return False, None, None, best_gap
    low, high = ordered[:cut], ordered[cut:]
    share = min(len(low), len(high)) / len(ordered)
    if share < min_share:
        return False, None, None, best_gap
    return True, median(low), median(high), best_gap


def calibrate_page(
    scale_value, dimensions: list, chains: list, config: dict, page_size_pt=None
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
        # **Where the number came from, and whether anything checked it.** A
        # ratio measured off the drawing and a ratio read off the title block
        # are not the same kind of answer, and a sheet that was never checked
        # must never look like one that was. ``verified`` is true only where two
        # independent sources agree.
        "source": SOURCE_UNKNOWN,
        "verified": False,
        "unverified_reason": None,
        "sheet_size_named": None,
        "sheet_size_actual": None,
        "sheet_size_correction": 1.0,
        "note": None,
    }

    # A printed ratio may name the sheet size it holds at. Correct it before it
    # is compared with anything, or the comparison is between a measurement and
    # a claim about a different piece of paper.
    correction, named_size, actual_size = _sheet_size_correction(scale_value, page_size_pt)
    record["sheet_size_named"] = named_size
    record["sheet_size_actual"] = actual_size
    record["sheet_size_correction"] = round(correction, 4)
    if correction != 1.0:
        printed_mm_per_point = printed_mm_per_point / correction
        record["printed_mm_per_point"] = round(printed_mm_per_point, 4)

    if not denominator:
        record["result"] = "unknown"
        record["source"] = SOURCE_UNKNOWN
        record["unverified_reason"] = (
            "This sheet prints no usable scale ratio, so what one point of it represents "
            "is unknown."
        )
        record["note"] = (
            "No usable ratio is printed on this sheet, so nothing on it can be "
            "measured from the drawing."
        )
        return record

    dimensions_by_id = {d["dimension_id"]: d for d in dimensions}
    # **The apparatus that sets a building out is laid clear of it.** So the
    # strings printed in the margin are the evidence, and a run of figures
    # printed among the room names is not - it measures the spacing of whatever
    # it is printed among. But some offices dimension internally throughout, and
    # a sheet drawn that way has no margin strings at all; refusing its only
    # evidence would leave it unmeasurable. So the margin strings are used where
    # the sheet has any, and the sheet falls back to its internal strings where
    # it has none, saying which it did.
    clear_samples, on_plan_samples, refused_on_the_plan = [], [], 0
    for chain in chains:
        measurement = _chain_measurement(chain, dimensions_by_id)
        if measurement is None:
            continue
        expected_mm, drawn_points = measurement
        ratio = expected_mm / drawn_points
        if chain.get("printed_clear_of_the_plan", True):
            clear_samples.append(ratio)
        else:
            on_plan_samples.append(ratio)
            refused_on_the_plan += 1

    samples = clear_samples
    record["strings_from"] = "printed clear of the plan"
    if not clear_samples and on_plan_samples:
        samples = on_plan_samples
        refused_on_the_plan = 0
        record["strings_from"] = (
            "printed on the drawing - this sheet lays no dimension string clear of it"
        )

    record["strings_used"] = len(samples)
    record["strings_refused_printed_on_the_plan"] = refused_on_the_plan

    if len(samples) < minimum_strings:
        # **Second in order: the sheet's own statement about itself, used and
        # labelled as unchecked.** A printed ratio is a claim, and a sheet
        # re-plotted from A3 to A4 still prints "1:100" - so this is a fallback
        # and stays one. It is never called confirmed.
        record["result"] = RESULT_PRINTED_ONLY
        record["source"] = SOURCE_PRINTED_CORRECTED if correction != 1.0 else SOURCE_PRINTED
        record["usable_for_measurement"] = True
        record["unverified_reason"] = (
            f"This sheet prints {len(samples)} dimension string(s) that can be measured against, "
            f"and {minimum_strings} are needed to check a scale. The printed scale is used as "
            "stated and nothing has confirmed it."
        )
        record["note"] = (
            "This sheet does not print enough dimension strings to check its scale "
            "against. The printed scale is used as stated."
        )
        return record

    # **Two drawings at two scales must not be averaged into one answer.**
    two, low_ratio, high_ratio, widest_gap = _two_populations(
        samples,
        float(settings.get("sample_agreement_pct", 10.0)),
        float(settings.get("second_population_min_share", 0.25)),
    )
    if two:
        record["strings_used"] = len(samples)
        record["result"] = "inconclusive"
        record["source"] = SOURCE_UNKNOWN
        record["usable_for_measurement"] = False
        record["two_scale_populations"] = [round(low_ratio, 4), round(high_ratio, 4)]
        record["unverified_reason"] = (
            f"This sheet's dimension strings measure two different scales - one group around "
            f"{low_ratio:.2f} mm per point and another around {high_ratio:.2f} mm, "
            f"{widest_gap:.0f}% apart. That is a sheet carrying two drawings at two scales, and "
            "one value would be correct for neither, so nothing is measured from it."
        )
        record["note"] = record["unverified_reason"]
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

    # **At the minimum there is no majority to appeal to, so every string must
    # agree.** With three strings a bad one is outvoted; with two, one bad
    # string and one good string are indistinguishable, and taking their median
    # would invent a scale neither of them measured. So a sheet at the minimum
    # whose strings disagree falls back to the scale it prints, marked as
    # checked by nothing.
    if len(samples) == minimum_strings and len(consistent) < len(samples):
        record["result"] = RESULT_PRINTED_ONLY
        record["source"] = SOURCE_PRINTED_CORRECTED if correction != 1.0 else SOURCE_PRINTED
        record["measured_mm_per_point"] = None
        record["variance_pct"] = None
        record["usable_for_measurement"] = True
        record["unverified_reason"] = (
            f"This sheet's {len(samples)} dimension strings measure different scales and there is "
            "no third string to settle which is right, so neither is used. The printed scale is "
            "used instead and nothing has confirmed it."
        )
        record["note"] = record["unverified_reason"]
        return record

    if len(consistent) * 2 < len(samples):
        record["result"] = "inconclusive"
        record["source"] = SOURCE_UNKNOWN
        record["usable_for_measurement"] = False
        record["unverified_reason"] = (
            f"This sheet's {len(samples)} dimension strings disagree with each other, so neither "
            "they nor the printed scale can be relied on. Nothing is measured from this sheet."
        )
        record["note"] = (
            f"This sheet's {len(samples)} dimension strings do not agree with each other "
            f"(only {len(consistent)} are within {agreement:.0f}% of the middle value), so "
            "the printed scale could not be checked. Lengths are not measured from this "
            "sheet until the strings are reviewed."
        )
        return record

    if abs(variance) <= tolerance:
        # **Confirmed means two independent sources agree** - the ratio the
        # title block claims and the ratio the sheet's own figures measure.
        # Nothing else earns the word.
        record["result"] = "confirmed"
        record["source"] = SOURCE_MEASURED_AND_PRINTED_AGREE
        record["verified"] = True
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
        # **Contradicted is never verified, whichever value is used.** The
        # drawing may well be internally consistent and simply re-plotted, and
        # measuring from its own figures is the right thing to do - but two
        # sources disagreeing by more than the tolerance is the opposite of
        # confirmation, and the sheet says so on its face rather than having
        # the measured value quietly preferred.
        every_string_agrees = len(consistent) == len(samples)
        record["result"] = "contradicted"
        record["source"] = SOURCE_MEASURED if every_string_agrees else SOURCE_UNKNOWN
        record["verified"] = False
        record["usable_for_measurement"] = every_string_agrees
        record["unverified_reason"] = (
            f"The printed scale and this sheet's own dimension strings disagree by "
            f"{abs(variance):.1f}%, against a {tolerance:.0f}% tolerance. Both are recorded; "
            + ("lengths are measured from the sheet's own figures because all of its strings "
               "agree with each other, and every length from this sheet is unverified."
               if every_string_agrees else
               "the strings do not agree with each other either, so nothing is measured.")
        )
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
