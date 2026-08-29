"""Day 3 — per-field value validation.

Critical Rule 5 says nothing is guessed silently. The practical form of that
rule is this module: a value found next to the right label is still only a
*candidate*, and it is accepted only if it actually looks like the thing the
field is supposed to hold.

This exists because of a measured failure. The previous code, when the
pattern it was looking for did not match the cell it had picked, fell back to
returning the cell's raw last line — so a sheet whose title block leaves the
scale cell blank reported ``scale = "15/12/2015"``, the drawing date from the
neighbouring column, presented as a confirmed value. A field that reports
"not detected" costs a reviewer one look at the sheet; a field that reports a
date as a scale corrupts every downstream measurement that trusts it.

Every validator returns ``(normalized_value, note)`` or ``None``:

*   ``None``  — this text is not a valid value for the field. The caller must
    treat the field as unresolved and try its next candidate.
*   a value   — the cleaned, canonical form actually worth storing, plus an
    optional note recording anything dropped or assumed during cleaning (for
    example the paper size in "1:100 @ A3"), so normalisation is never silent
    either.
"""

import re

from pipeline.plan.textmodel import is_placeholder

# --- Scale ---------------------------------------------------------------

# "1:100", "1 : 100", "1/100" — the ratio form used on every metric drawing.
_RATIO_RE = re.compile(r"\b1\s*[:/]\s*(\d{1,5})\b")
# "NTS" / "N.T.S." / "NOT TO SCALE" — a real, meaningful scale value that must
# be preserved rather than reported as a missing scale.
_NTS_RE = re.compile(r"\bN\.?\s?T\.?\s?S\.?\b|\bNOT\s+TO\s+SCALE\b", re.IGNORECASE)
# "@ A3" / "AT A1" — the sheet size the ratio applies at. Kept as a note
# because printing a plan at a different size changes what the ratio means.
_PAPER_SIZE_RE = re.compile(r"[@]\s*(A[0-4])\b|\bAT\s+(A[0-4])\b", re.IGNORECASE)
# A date must never be accepted as a scale (the measured Day 3 failure).
_DATE_RE = re.compile(
    r"^\s*\d{1,4}\s*[/.\-]\s*\d{1,2}\s*[/.\-]\s*\d{1,4}\s*$"
)


def validate_scale(text: str):
    """Accepts a drawing scale in ratio or not-to-scale form.

    Multiple ratios on one line ("1:100, 1:50") are a legitimate multi-scale
    sheet: all of them are kept, in printed order, and the note says so.
    """
    raw = (text or "").strip()
    if not raw or is_placeholder(raw):
        return None
    if _DATE_RE.match(raw):
        return None

    notes = []
    paper = _PAPER_SIZE_RE.search(raw)
    if paper:
        notes.append(f"applies at sheet size {(paper.group(1) or paper.group(2)).upper()}")

    ratios = _RATIO_RE.findall(raw)
    if ratios:
        seen = []
        for r in ratios:
            value = f"1:{int(r)}"
            if value not in seen:
                seen.append(value)
        if len(seen) > 1:
            # Printing more than one scale on a sheet is ordinary practice: the
            # larger ratio covers the plan itself and the smaller one covers an
            # enlarged detail alongside it. It is recorded as printed, and each
            # measurement taken from the sheet has to say which one it used.
            notes.append(
                "This sheet is drawn at more than one scale \u2014 normally one for the "
                "main drawing and another for enlarged details."
            )
        return ", ".join(seen), "; ".join(notes) or None

    if _NTS_RE.search(raw):
        return "NTS", "; ".join(notes) or None

    return None


def scale_ratios(scale_value: str) -> list:
    """Every ratio denominator printed in a scale value, in printed order.

    A sheet may legitimately carry several: '1:100, 1:1' means the drawing is
    at 1:100 and an enlarged detail on the same sheet is at 1:1.
    """
    if not scale_value:
        return []
    out = []
    for ratio in _RATIO_RE.findall(scale_value):
        try:
            denominator = int(ratio)
        except ValueError:
            continue
        if denominator > 0 and denominator not in out:
            out.append(denominator)
    return out


def scale_ratio_denominator(scale_value: str):
    """The ratio to measure the main drawing with.

    A sheet printing several scales lists the drawing's own scale first and its
    enlarged details after it, so the first ratio is the one that applies to
    the drawing as a whole. 'NTS' honestly has no ratio and returns None.
    """
    ratios = scale_ratios(scale_value)
    return ratios[0] if ratios else None


# --- Sheet number ---------------------------------------------------------

# Drawing numbers seen in practice: "A02", "A02A", "A-102", "S1.02",
# "D-00-03", "03". A trailing "of 20" is a sheet count, not part of the number.
_SHEET_NUMBER_RE = re.compile(
    r"^(?P<num>[A-Z]{0,3}[-.]?\d{1,4}(?:[-.]\d{1,4})*[A-Z]?)"
    r"(?:\s+(?:OF|/)\s*\d{1,4})?$",
    re.IGNORECASE,
)
# No leading word boundary: in "A02 of 20" the digits follow a letter, so \b
# never matches there and the printed sheet count was silently missed.
_OF_COUNT_RE = re.compile(r"(\d{1,4})\s*(?:OF|/)\s*(\d{1,4})\b", re.IGNORECASE)


def validate_sheet_number(text: str):
    """Accepts a printed drawing number and strips any 'of N' sheet count.

    A date is rejected outright: 15/12/2015 would otherwise satisfy the
    digits-and-separators shape of a drawing number.
    """
    raw = (text or "").strip().rstrip(".")
    if not raw or is_placeholder(raw):
        return None
    if _DATE_RE.match(raw):
        return None
    if len(raw) > 16:
        return None

    note = None
    of_match = _OF_COUNT_RE.search(raw)
    if of_match:
        note = f"sheet {of_match.group(1)} of {of_match.group(2)}"

    match = _SHEET_NUMBER_RE.match(raw)
    if not match:
        return None
    value = match.group("num").upper().strip("-.")
    if not value or not any(c.isdigit() for c in value):
        return None
    return value, note


def validate_sheet_position(text: str):
    """Accepts 'PAGE 4 OF 20' style values, returning 'Page 4 of 20'.

    Phrased as a page reference because that is how a reader identifies a
    sheet that carries no drawing number of its own.
    """
    raw = (text or "").strip()
    if not raw or is_placeholder(raw):
        return None
    match = _OF_COUNT_RE.search(raw)
    if match:
        return f"Page {int(match.group(1))} of {int(match.group(2))}", None
    if re.fullmatch(r"\d{1,4}", raw):
        return f"Page {int(raw)}", "page number printed without a total"
    return None


# --- Revision -------------------------------------------------------------

# A revision is a short code: "A", "B1", "01", "P2". Anything longer is a
# revision *description*, not the revision itself.
_REVISION_RE = re.compile(r"^[A-Z]{0,2}\d{0,3}[A-Z]?$", re.IGNORECASE)


def validate_revision(text: str):
    """Accepts a short revision code. A dash placeholder is rejected here, so
    a sheet with no revision issued reports 'not detected' with that reason
    rather than a literal '-' presented as the revision."""
    raw = (text or "").strip().rstrip(".")
    if not raw:
        return None
    if is_placeholder(raw):
        return None
    if len(raw) > 4:
        return None
    if not _REVISION_RE.match(raw):
        return None
    if not any(c.isalnum() for c in raw):
        return None
    return raw.upper(), None


# --- Dates ----------------------------------------------------------------

_DATE_ANY_RE = re.compile(
    r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})\b|"
    r"\b(\d{4})[/.\-](\d{1,2})[/.\-](\d{1,2})\b|"
    r"\b([A-Z]{3,9})\s+(\d{4})\b",
    re.IGNORECASE,
)


def validate_date(text: str):
    """Accepts a printed date in any of the common orderings, returned as
    printed. It is deliberately not reformatted: Australian drawings are
    day-first, but a 03/04/2015 cannot be proven to be either ordering from
    the sheet alone, so re-writing it would invent certainty."""
    raw = (text or "").strip()
    if not raw or is_placeholder(raw):
        return None
    match = _DATE_ANY_RE.search(raw)
    if not match:
        return None
    value = match.group(0).strip()
    note = None
    if re.match(r"^\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}$", value):
        note = "printed as-is; day/month order not verifiable from the sheet"
    return value, note


# --- Free-text fields -----------------------------------------------------

_URL_RE = re.compile(r"WWW\.|HTTPS?://|\.COM|\.AU\b", re.IGNORECASE)


def validate_text_field(text: str, max_length: int = 120):
    """Accepts a general printed value (project name, drawn-by, client).

    Rejects placeholders, web addresses and anything long enough to be a
    paragraph of notes rather than a field value.
    """
    raw = " ".join((text or "").split())
    if not raw or is_placeholder(raw):
        return None
    if len(raw) > max_length:
        return None
    if _URL_RE.search(raw):
        return None
    if not any(c.isalnum() for c in raw):
        return None
    return raw, None


def validate_sheet_title(text: str, exclusion_keywords, max_length: int = 90):
    """Accepts a sheet title.

    The exclusion list is applied here and nowhere else. It exists to reject
    page furniture ('DO NOT SCALE', a copyright line) when a title has to be
    guessed. It is deliberately *not* applied to a title read from a printed
    title label, because a sheet is genuinely allowed to be called
    'DOOR SCHEDULE SHT.1' — an earlier version rejected exactly those titles
    for containing the word 'SCHEDULE', losing four sheets' titles on the
    supplied plan set.
    """
    raw = " ".join((text or "").split())
    if not raw or is_placeholder(raw):
        return None
    if len(raw) > max_length:
        return None
    if not any(c.isalpha() for c in raw):
        return None
    if _URL_RE.search(raw):
        return None
    upper = raw.upper()
    for keyword in exclusion_keywords or []:
        if keyword.upper() in upper:
            return None
    return raw, None


VALIDATORS = {
    "sheet_number": validate_sheet_number,
    "sheet_position": validate_sheet_position,
    "revision": validate_revision,
    "scale": validate_scale,
    "issue_date": validate_date,
    "project_number": validate_sheet_number,
}
