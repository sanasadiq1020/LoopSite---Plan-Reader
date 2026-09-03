"""The progress bar is a measure of the work, taken from the run itself.

It used to give the sheets a flat 85% of the bar and the finishing work the
other 15%. Measured, the sheets are 96 to 98 per cent of the time on every plan
set tried — so three per cent of the work had fifteen per cent of the bar, and
the bar sat at 85 looking stalled while the run was very nearly done.
"""

import time

from app import progress


# A sheet of a real plan set takes about half a second and yields roughly a
# hundred records, so these keep that proportion — a hundred records per second
# of reading. The absolute times are shrunk to keep the tests quick; it is the
# ratio between them that the estimate is made of.
def _run(token="t", pages=20, per_sheet=0.02, records_per_sheet=2):
    progress.start(token, "plan.pdf")
    progress.set_page_count(token, pages)
    seen = []
    for n in range(1, pages + 1):
        time.sleep(per_sheet)
        progress.page_done(token, n, pages, f"A{n:02d}", records=records_per_sheet)
        seen.append(progress.read(token)["percent"])
    return seen


def test_the_bar_is_shared_out_by_the_number_of_sheets_in_this_document():
    """Nothing here is fixed to a document of a particular size: the step is
    the share one sheet is worth, and that comes from how many there are."""
    for pages in (3, 16, 17, 20, 47):
        seen = _run(token=f"t{pages}", pages=pages)
        assert len(seen) == pages
        assert seen == sorted(seen), "the bar went backwards"
        assert seen[-1] >= 90, "the sheets should own nearly all of the bar"
        assert seen[-1] < 100, "the sheets are not the whole job"


def test_the_sheets_own_nearly_all_of_it_because_that_is_where_the_time_goes():
    seen = _run(token="quick", pages=20)
    assert seen[-1] >= 95


def test_a_document_whose_finishing_work_is_heavy_gives_it_more_of_the_bar():
    """A plan set of schedules produces far more records than one of
    elevations, and writing them is what the finishing work does. The share is
    worked out from the records this document is actually producing."""
    light = _run(token="light", pages=8, per_sheet=0.02, records_per_sheet=2)
    heavy = _run(token="heavy", pages=8, per_sheet=0.02, records_per_sheet=400)
    assert heavy[-1] < light[-1], "more records to write should leave more bar for it"


def test_the_finishing_steps_are_shared_by_what_each_has_to_get_through():
    """A step with more to do gets more of the last stretch. The weights are
    real counts from the document, not four numbers chosen in advance."""
    progress.start("f", "plan.pdf")
    progress.set_page_count("f", 4)
    for n in range(1, 5):
        progress.page_done("f", n, 4, records=50)
    before = progress.read("f")["percent"]
    progress.begin_finishing("f", [("small", 1), ("large", 99)])
    progress.finishing_step("f", 0)
    after_small = progress.read("f")["percent"]
    progress.finishing_step("f", 1)
    after_large = progress.read("f")["percent"]

    assert after_small == before, "the first step starts where the sheets left off"
    assert after_large >= after_small
    assert progress.read("f")["stage"] == "large"


def test_the_bar_never_reaches_a_hundred_until_the_run_is_done():
    """A bar that sits at 100% while work continues is worse than no bar."""
    progress.start("h", "plan.pdf")
    progress.set_page_count("h", 3)
    for n in range(1, 4):
        progress.page_done("h", n, 3, records=10)
    progress.begin_finishing("h", [("a", 1), ("b", 1)])
    progress.finishing_step("h", 0)
    progress.finishing_step("h", 1)
    assert progress.read("h")["percent"] < 100
    progress.finish("h", "run-1")
    assert progress.read("h")["percent"] == 100
    assert progress.read("h")["finished"] is True


def test_a_step_reported_for_an_upload_nobody_is_watching_is_harmless():
    """The reading runs whether or not a browser is following it."""
    progress.page_done("no-such-token", 1, 5)
    progress.begin_finishing("no-such-token", [("a", 1)])
    progress.finishing_step("no-such-token", 0)
    assert progress.read("no-such-token") is None
