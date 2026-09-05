# Reading walls, doors, windows and scale from a plan, with computer vision

`backend/pipeline/plan/cvdetect/` is a self-contained reader for Australian
residential construction drawings (AS 1100.301). It takes a PDF and returns
wall centrelines with measured thicknesses, doors and windows, and the link
between them — with every value traceable to the sheet and position it came
from.

It uses **PyMuPDF, OpenCV, NumPy and Shapely and nothing else**. No external
service, no paid API, no trained model, no GPU.

## Running it

```bash
cd backend
python -m pipeline.plan.cvdetect.cli ../input/sample_plan.pdf
python -m pipeline.plan.cvdetect.cli ../input/plan.pdf --pages 4 5 --json reading.json
```

From Python:

```python
from pipeline.plan.cvdetect import read_document, read_sheet

result = read_document("input/plan.pdf")
for sheet in result["sheets"]:
    print(sheet["sheet_name"], sheet["summary"])
```

`read_sheet(page, printed_scale="1:100")` reads one page. Both always return a
result: a sheet that cannot be read comes back with the reason attached rather
than raising (Critical Rule 6).

## The five steps, and why they are in this order

The order is the design, not an implementation detail.

| | step | module |
|---|---|---|
| 1 | the drawing's own paths, with dashed and lightly plotted line work set aside **before** any image is made | `vectorpaths.py` |
| 2 | what one point of this sheet measures, taken off its own dimension figures | `calibration.py` |
| 3 | doors and windows, found and painted white **before** any gap is closed | `openings.py` |
| 4 | wall bands, outlines by contour, centrelines by skeletonisation | `wallgeometry.py` |
| 5 | which wall each opening is in, or why that could not be said | `crosslink.py` |

**Calibration comes before every threshold**, because a threshold in
millimetres is meaningless until a millimetre has a size on this sheet.

**Openings come before walls**, because Step 4 closes gaps to join a wall's two
faces, and a door gap is exactly the size of gap that closing bridges. Bridge
it and the wall reads as continuous, the door is gone, and nothing downstream
can know it was ever there.

## No distance is written into the code

Every threshold is stated once, in `config/cv_detection.json`, in one of two
units — and the difference matters:

* **Millimetres of building** — how thick a wall is, how wide a door is. These
  pass through the scale measured off each individual sheet, so the same reader
  works on a 1:50 detail and a 1:200 site plan without being retuned.
* **Points of paper** — how long a dash is, how heavily a line is plotted.
  AS 1100.301 states these on the paper, so they are never scaled.

A pixel figure in the code would be a promise about one drawing at one scale,
and the first sheet drawn differently would break it silently.

## What was measured, and what it cost

Each of these was found by running the reader on real drawings, not by
reasoning about it. Every one produced output that looked plausible and was
wrong.

### The scale is measured, not read off the title block

A dimension figure is printed *against the line that measures it*, so its
printed value divided by that line's drawn length is one reading of the sheet's
scale. Pooled across a sheet and taken as a median, one figure paired with the
wrong line is outvoted rather than believed.

*Measured on a real 1:100 floor plan: 61 of its 61 horizontal figures found
their dimension line; the median came to **35.294 mm per point against a true
35.278** — 0.05% out — with 53 of the 61 samples agreeing to within 5%.*

Across a 23-sheet set it reads **1:100 on the floor plans and exactly 1:50 on
the detail sheets**, each sheet on its own evidence.

**Overturning a printed scale needs more evidence than confirming one.** An
elevation sheet prints a handful of height figures — "2100 TO HEAD HT", "2600
TO CEILING HT" — and four of them agreed with each other on a ratio 32% away
from the sheet's perfectly correct 1:100 title block. Four figures on an
elevation are not evidence that a title block is wrong. A sheet genuinely
re-plotted from A3 to A4 is real and common, so the capability stays, but it
takes a proper number of figures in near-unanimous agreement.

### Stroke weight does not separate structure from annotation

AS 1100.301 has offices plot a structural outline heavier than an annotation
line, so the idea is sound. The drawings say otherwise:

| sheet | weights present, by drawn length |
|---|---|
| one office's floor plan | 0.28 pt 48%, 0.37 pt 24%, 0.51 pt 18%, 0.71 pt 10% |
| another office's floor plan | **0.17 pt 66%**, 0.42 pt 2%, **1.36 pt 29%** |

A "structural is ≥ 0.35 pt" rule throws away nearly half of the first sheet.
Otsu's method over the same histogram is worse on the second: it puts the cut
at 1.36 pt — the **drawing frame and title block** — and deletes the whole
building, which is drawn at 0.17 pt, while looking entirely reasonable.

So the rule is turned around: the weight class carrying the most drawn length
*is* the drawing, and only what is plotted lighter than the drawing is an
annotation line. It cannot delete the drawing, because the drawing sets the
threshold. **Measured, it removes nothing on any of the three plan sets** — the
honest finding. What actually separates a wall from a dimension line is the
thickness of its two paired faces, which is Step 4's job. Otsu is kept as a
configurable option with the measurement recorded beside it.

### A dashed line is dashed whatever the file says

All **5,595 paths** on one floor plan report a solid dash pattern (`[] 0`),
including the roof extent that is plainly dashed on the page: exporters
routinely emit dashes as separate short segments. So the shape of the run is
read as well — many short collinear pieces separated by **regular** gaps.

Regularity is the whole test. A wall face broken by a doorway is also a line in
pieces, but it is two long pieces with one big gap, not eight short pieces with
seven equal ones. Without that, a wall with two doors in it would be discarded
as a dashed line.

### A wall's own two faces are not a window

"Two parallel lines closer together than a wall is thick" is the definition of
a wall. Used as a glazing test, it reported **112 windows on a floor plan that
has about thirty**. A window is glazing drawn *inside* a wall, so what is
looked for is an outermost pair a wall thickness apart with one or more further
lines strictly between them. A wall with nothing drawn inside it is a wall.

### The closing kernel has to reach across the wall

A wall drawn as an outline is two faces its own thickness apart, so the kernel
must be the width of the **thickest** wall the office builds. Sized at the
thinnest instead, a 230 mm wall's faces are never joined at all — measured on a
building drawn to known dimensions, **it reported no walls whatever**. Two
different walls are a room apart, so a kernel this size joins a wall to itself
and never to its neighbour, and anything closed into a wider blob is caught by
the thickness test.

### Thinning must not touch the edge of its own crop

Each component is thinned inside its own bounding box, which is an 18-fold
saving (below). But cropped tightly, a shape touches all four borders of the
crop, thinning treats out-of-bounds as background, and the border pixels are
kept as though they were the edge of the shape.

*Measured on the building drawn to known dimensions: every centreline came out
on the **outer face** of its wall and every thickness read **81 mm against a
drawn 230 mm**.* Two pixels of blank margin fixes it. Nothing about the output
looked broken — the walls were all there, in about the right places, with a
confidently wrong thickness.

### A right-angled corner sweeps a quarter turn too

`cv2.HoughCircles` proposes thousands of circles on a floor plan — every basin,
every pan, every cooktop burner, every letter O. It is used here only as a
*proposer*, and only on a sheet whose own geometry holds no arcs; each proposal
is then put back to the image as a question with a checkable answer: is there
ink along this circle, and through how many degrees?

Counting the **total** ink anywhere round the circle read the **four corners of
a plain rectangular building** as door swings. Each was painted out as an
opening and the building reported no walls at all. A swing is a curve: its ink
is *one continuous run*. Scattered crossings that happen to add up to the same
are two straight lines. Measuring the longest unbroken run instead separates
them.

### The reader has to allow for the gap it made itself

Step 3 paints every opening white before the walls are closed, so by the time a
wall band exists there is a hole in it exactly where the opening is. An opening
therefore never overlaps its own wall, and measuring without allowing for that
reported real doors carrying their own `D`-marks as "no wall within 200 mm".

Two further corrections, both drafting facts rather than tuning:

* **Distance is measured to the wall band, not its centreline.** A centreline
  sits half a thickness inside the wall, so a door drawn hard against its own
  jamb is already 45–115 mm away before it has moved.
* **A mark is printed *beside* its opening; a symbol is drawn *on* it.** A
  `D12` commonly sits inside the room on a leader. Measuring both to the same
  allowance reported real, marked doors as having no wall near them.

*Placement on the marked floor plan went 19% → 32% → 44% → **76%** across these
three corrections.*

### Skeleton spurs and walls cut in half

Closing rounds a wall's corners and thinning grows a whisker off each one.
Left in, every whisker is counted as a wall and breaks the wall it hangs off.
A run is a spur when it is short **and** has an end no other run reaches — a
short run joined at both ends is a nib, a pier or the stub between two
doorways, and those are real.

A skeleton is also cut at every junction, because running through a T would
join two different walls into one line. But a wall does not stop at a T — the
partition stops, the wall carries on — so pieces leaving a meeting in nearly
opposite directions are put back together. At a corner they leave at right
angles and stay two walls, which is right.

*Measured on the marked floor plan: 48 walls / 65 m → **66 walls / 96 m**.*

## Measured on the whole corpus

Every sheet of all three plan sets, end to end through the CLI:

| plan set | sheets | scale established | walls | centreline | openings | placed on a wall |
|---|---|---|---|---|---|---|
| published as pictures | 6 | **6 of 6** | 431 | 691 m | 8 | 6 |
| vector, 23 sheets | 23 | **21 of 23** | 610 | 1,093 m | 214 | 117 |
| unseen office, 17 sheets | 17 | 9 of 17 | 131 | 408 m | 41 | 21 |

The two sheets of the 23-sheet set without a scale are a cover and a notes
sheet, which print no dimension figures and more than one ratio. On the unseen
set, 8 of its 17 sheets are in the same position. **They report no lengths and
say so** rather than measuring from a ratio that may belong to a detail printed
beside the drawing.

A wall here is a stretch between two junctions, so the counts are not
comparable with a reader that reports one wall per pair of drawn faces — this
one reports each wall **once**, and the metres are centreline rather than drawn
line.

## Performance

Thinning is the expensive step and it is done per connected component:

| | one A3 floor plan at 300 DPI |
|---|---|
| thinning the whole sheet | **52 s** |
| thinning each component in its own box | **2.9 s** |

Components are 8-disconnected by definition, so thinning one can never depend
on another, and each one's box is a few per cent of the sheet — 93% of an A3
plan at 300 DPI is blank paper. The skeleton is unchanged; the saving is
eighteen-fold.

Rendering is bounded rather than assumed. Drawing sizes are not: an A0 sheet at
300 DPI is 140 megapixels, and on a small server that render is what ends the
run. Past `max_megapixels` the resolution is **reduced and logged** — the sheet
is never cropped, because cropping loses part of the drawing silently.

## What it does not do

* **It does not certify anything.** Everything it produces is a draft for
  review by a competent Australian construction professional. It makes no claim
  of engineering approval, code compliance or safety certification.
* **A sheet whose scale could not be established reports no lengths**, and says
  so. A wrong scale makes every length wrong by the same factor with nothing
  looking odd, which is the one failure this must never produce silently.
* **An opening between two equally close walls is reported unplaced**, with the
  reason. A wrong link is worse than none: every later stage trusts it and cuts
  the void into the wrong wall.
* **A plan set published as pictures reads less well than one published as
  geometry**, and every record says which it came from. Pixels cannot resolve
  the glazing lines drawn inside a wall.
* **Vocabulary is configuration.** A plan set using door and window prefixes
  not in `config/cv_detection.json` will report those marks as not found rather
  than guessing at them. That is the intended behaviour and it is also the
  signal that a config entry is missing.

## Tests

`backend/tests/test_cvdetect.py` — 43 tests, each naming the mistake it
prevents rather than restating what the code does. They include an end-to-end
run against a **building drawn for the purpose**: a 12 m × 8 m rectangle in
230 mm wall at 1:100, read back and compared with what was drawn. Scoring a
reader against a plan set it was built on is how a threshold gets tuned to that
plan set.

```bash
cd backend && python -m pytest tests/test_cvdetect.py -q
```
