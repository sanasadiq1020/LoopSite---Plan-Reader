"""Reading a plan set nobody has seen before.

Every case here comes from a way a real construction PDF can differ from the
ones this reader was built against, and each one produced — or would have
produced — a sheet with nothing on it and no explanation. A blank sheet with
no reason given is the one result a reader cannot act on, so these lock the
behaviour down.
"""

import fitz
import pytest

from pipeline.plan.intake import _why_nothing_was_read, classify_page, render_page
from pipeline.plan.ocr import should_run_ocr, text_layer_is_usable


# --- Which pages are offered to character recognition ---------------------


def test_a_page_with_no_text_is_read_by_ocr_even_when_it_is_drawn_line_work():
    """The failure this file exists for.

    A sheet whose lettering was converted to outlines on export carries no
    text at all, and yet classifies as drawn line work — because line work is
    exactly what outlined letters are. The classification used to decide
    whether recognition ran, so nothing was read and every field on the sheet
    came back blank.
    """
    assert should_run_ocr(native_char_count=0, page_has_marks=True) is True


def test_a_scan_placed_inside_a_drawn_border_is_still_read():
    """The same trap, reached a different way: a scanned drawing mounted on a
    sheet that also carries a ruled frame."""
    assert should_run_ocr(native_char_count=3, page_has_marks=True) is True


def test_a_sheet_that_carries_its_own_text_is_never_sent_to_recognition():
    """Reading a page that already has text costs minutes and returns what was
    already there — one plan set spent 85 minutes doing exactly that."""
    assert should_run_ocr(native_char_count=5000, page_has_marks=True) is False


def test_a_genuinely_empty_page_is_not_worth_recognising():
    assert should_run_ocr(native_char_count=0, page_has_marks=False) is False


# --- What the page classifier reports -------------------------------------


def _one_page(width=842.0, height=595.0):
    doc = fitz.open()
    return doc, doc.new_page(width=width, height=height)


def test_classification_reports_the_evidence_it_used():
    doc, page = _one_page()
    page.draw_line(fitz.Point(20, 20), fitz.Point(400, 20))
    classification, reasoning, evidence = classify_page(page)
    assert classification == "vector"
    assert evidence["text_chars"] == 0
    assert evidence["drawing_count"] >= 1
    assert "text_chars=0" in reasoning
    doc.close()


def test_a_blank_page_classifies_as_unknown_and_carries_no_marks():
    doc, page = _one_page()
    classification, _, evidence = classify_page(page)
    assert classification == "unknown"
    assert evidence == {"text_chars": 0, "image_ratio": 0.0, "drawing_count": 0}
    doc.close()


# --- Why a sheet came back with nothing on it -----------------------------


def test_a_sheet_that_read_normally_carries_no_note():
    assert (
        _why_nothing_was_read(
            has_any_text=True,
            ocr_status="skipped",
            ocr_error=None,
            page_has_marks=True,
            evidence={"drawing_count": 900},
        )
        is None
    )


def test_a_blank_page_says_it_is_blank():
    note = _why_nothing_was_read(
        has_any_text=False,
        ocr_status="skipped",
        ocr_error=None,
        page_has_marks=False,
        evidence={"drawing_count": 0},
    )
    assert note and "blank" in note.lower()


def test_a_sheet_recognition_could_not_run_on_passes_the_reason_through():
    note = _why_nothing_was_read(
        has_any_text=False,
        ocr_status="unavailable",
        ocr_error="…character recognition is not available on this server…",
        page_has_marks=True,
        evidence={"drawing_count": 900},
    )
    assert note and "not available on this server" in note


def test_a_drawn_sheet_that_recognition_returned_nothing_for_says_so():
    note = _why_nothing_was_read(
        has_any_text=False,
        ocr_status="ok",
        ocr_error=None,
        page_has_marks=True,
        evidence={"drawing_count": 16117},
    )
    assert note and "line work" in note


@pytest.mark.parametrize("status", ["unavailable", "failed", "timeout"])
def test_every_way_recognition_can_fail_produces_a_reason(status):
    note = _why_nothing_was_read(
        has_any_text=False,
        ocr_status=status,
        ocr_error="a reason a reader can act on",
        page_has_marks=True,
        evidence={"drawing_count": 5},
    )
    assert note == "a reason a reader can act on"


# --- A sheet larger than the machine reading it ---------------------------


def test_an_enormous_sheet_is_rendered_smaller_rather_than_failing():
    """A plotted sheet is not bounded in size. Rendering one at full
    resolution is what ends a run on a small server, and a run that ends part
    way through is the empty result this file is about."""
    a0_long = fitz.paper_size("A0")  # points
    doc = fitz.open()
    page = doc.new_page(width=a0_long[0] * 2, height=a0_long[1] * 2)
    pixmap = render_page(page, dpi=300)
    assert pixmap.width * pixmap.height <= 40_000_000
    # Still the whole sheet, at the sheet's own proportions — reduced, never cropped.
    assert pixmap.width / pixmap.height == pytest.approx(
        page.rect.width / page.rect.height, rel=0.02
    )
    doc.close()


# --- A text layer that is present and worthless ---------------------------


def test_a_drawings_own_wording_is_accepted():
    """Construction text is nearly all letters and digits. Measured across the
    drawings in use, the lowest share on any sheet is 0.90."""
    usable, why = text_layer_is_usable(
        "FLOOR PLAN  SCALE 1:100  BED 2  3,600 x 3,200  W12  Ø90 FALL 1:100"
    )
    assert usable is True
    assert why == ""


def test_text_that_maps_into_the_private_use_area_is_not_the_drawings_wording():
    """A PDF need not record what its glyphs mean, and a plot or an older CAD
    export often gets it wrong. The sheet then carries plenty of text that is
    not the words printed on it — and counting characters cannot tell."""
    usable, why = text_layer_is_usable("".join(chr(0xE000 + i) for i in range(40)))
    assert usable is False
    assert "cannot be displayed" in why


def test_text_with_no_letters_or_digits_is_not_the_drawings_wording():
    usable, why = text_layer_is_usable("!@#$%^&*()_+-={}[]|;:<>,./?~`" * 3)
    assert usable is False
    assert "letters and digits" in why


def test_an_unreadable_text_layer_sends_the_sheet_to_recognition():
    """Before this check, a sheet like this had thousands of characters, so it
    was never offered to recognition and every value read off it was nonsense
    or absent, with nothing on screen to say why."""
    stored = "".join(chr(0xE000 + (i % 26)) for i in range(400))
    assert should_run_ocr(len(stored), page_has_marks=True) is False  # counted
    usable, _ = text_layer_is_usable(stored)
    assert usable is False
    assert should_run_ocr(0 if not usable else len(stored), page_has_marks=True) is True


def test_an_empty_text_layer_is_left_for_the_character_count_to_judge():
    assert text_layer_is_usable("") == (True, "")
    assert text_layer_is_usable("   \n ") == (True, "")


def test_a_broken_text_layer_is_explained_on_the_sheet():
    note = _why_nothing_was_read(
        has_any_text=False,
        ocr_status="ok",
        ocr_error=None,
        page_has_marks=True,
        evidence={"drawing_count": 900},
        text_layer_problem="100% of this sheet's stored text is characters that cannot be displayed",
    )
    assert note and "exported without recording what its lettering says" in note
