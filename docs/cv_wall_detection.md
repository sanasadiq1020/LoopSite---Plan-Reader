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
| 2 | what one point of this sheet measures, taken off its own dimension figures, falling back to what its title block states | `calibration.py`, `titleblockscale.py` |
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

### When the drawing cannot measure itself, the title block is asked

A great many sheets carry too few dimension figures to pool — and they are not
unusual sheets. A plan set published as pictures has no vector dimension lines
to measure against at all; a cover sheet, a notes page and a details sheet have
none either. So where the measurement cannot be made, the sheet's **own
statement about itself** is read (`titleblockscale.py`), used, and marked
plainly as unverified.

**Where it looks matters more than what it matches.** The four edge strips are
searched first, because a title block sits on an edge — AS 1100 puts it
bottom-right, and the bottom, right, left and top have all been seen on real
sets. That ordering is what separates *this sheet's* scale from the scales
printed beside its details. On one real detail sheet the title block says
`1:50 @ A3` while `1:50` is also set in large type under one drawing, and a
drawing index elsewhere in the set lists 1:200, 1:100 and 1:50.

Each statement is ranked by how firmly it is tied to this sheet:

| rank | where it was printed |
|---|---|
| 4 | labelled `SCALE`, in the bottom or right strip — AS 1100's own position |
| 3 | labelled, in the left or top strip — the uncommon variants |
| 2 | labelled, anywhere else on the sheet |
| 0 | **unlabelled — not usable, wherever it is printed** |

**Zero matters as much as four.** A title block *labels* its scale cell; a
ratio with no label beside it is a caption under a drawing. Measured across all
three plan sets, every scale actually recovered scores 4 — so refusing the
unlabelled ones costs nothing and removes the whole class of error.

Five traps, every one taken from a real sheet rather than imagined:

* `1:100MM FALL ON SPANDECK` — **not a scale.** It is printed on a real floor
  plan, inside the strip where a title block sits. A ratio followed straight by
  a letter is a fall, a grade or a product code.
* `DO NOT SCALE DRAWING` and `DO NOT SCALE FROM DRAWINGS` — **not a scale
  label.** Both are printed across the bottom strip of real sheets, exactly
  where a scale would be.
* A **drawing-index column**: a cover sheet's index has `SCALE` as a column
  *header* with a row per sheet — 22 ratios stacked beneath it on one real
  cover. Read as a cell label, the first became "this sheet's scale" with the
  highest confidence available, on a sheet that draws nothing at all. A title
  block's scale cell holds exactly one value.
* `Scale:` on one line with `1:100 @ A3` on the next, and `SCALE:` with
  `1 : 200` beside it — a label above or beside its value is how a ruled cell is
  set out, and two different offices do it the two different ways.
* `NTS` / `NOT TO SCALE` in the title block — a positive statement that nothing
  on the drawing may be measured, which is different from finding nothing.

**The sheet size is part of the claim.** `1:50 @ A3` says the ratio holds when
the drawing is printed at A3. If the page really is A3 it stands; if the page is
A4, the drawing was reduced and every length taken from the printed ratio is out
by the ratio of the two long edges — **1.414 between one A size and the next,
which is a 3 m wall reported as 2.1 m**. ISO sizes are a standard and the page
states its own size, so that correction can be made exactly. This is the whole
reason offices print the sheet size next to the scale.

**A printed claim never outranks a measurement**, except where it is the sheet's
own title block (rank 4) and the measurement is thin. A ratio picked up from a
caption or an index is not allowed to set aside a real measurement — that would
be preferring the weaker evidence.

*Measured across the corpus: the sheets that can establish a scale are 6 of 6,
21 of 23 and 8 of 17. Every sheet that cannot is a cover, a notes page, an
engineering report page, or a details sheet whose drawings are marked NTS —
there is genuinely no scale on them to find, and each says so rather than
guessing.*

### A site plan and a roof plan have no walls on them

Adding the fallback above immediately exposed a defect it had been hiding. A
site-plan-and-roof-plan sheet had previously produced nothing only because its
scale could not be read; once it could, the sheet reported **124 walls and
400 metres** of them — every one a boundary, a setback, a driveway or a roof
batten.

A site plan draws the block, not the building; a roof plan draws what is over
it. And the thickness test cannot separate their lines from walls: **at 1:200
the band that means "a 70–320 mm wall" is 1 to 4.5 points of paper**, and on a
site plan almost every pair of lines is that far apart.

So walls are not traced on a sheet whose own title names a drawing of that
kind. The wording is configuration, not a list in code — what one office calls
a stormwater plan another calls a drainage plan. A sheet naming *both* a kind
with walls and a kind without (a floor plan with a small roof plan inset) is
traced: the safe direction is to read it and let the geometry decide.

**Every "traces walls" entry names a drawing, and that is not a style choice.**
A bare `GROUND FLOOR` matched a site-coverage *table* on the real site plan —
`BUILDING SITE COVERAGE (AREA M2) | GROUND FLOOR | HOUSE: 182.66 M2` — and
rescued the very sheet the rule exists to stop. `FLOOR PLAN` already covers
`GROUND FLOOR PLAN` as a substring, so requiring the word `PLAN` loses nothing.

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

| plan set | sheets | scale established | walls | openings | placed on a wall |
|---|---|---|---|---|---|
| published as pictures | 6 | **6 of 6** | 431 | 8 | 6 |
| vector, 23 sheets | 23 | **21 of 23** | 549 | 222 | 117 |
| unseen office, 17 sheets | 17 | 8 of 17 | 125 | 39 | 19 |

Every sheet without a scale is a cover, a notes page, an engineering report
page bound into the set, or a details sheet whose drawings are marked NTS.
**They report no lengths and say so** rather than measuring from a ratio that
may belong to a detail printed beside the drawing. Two further sheets — a site
plan and a roof plan — do establish a scale and are deliberately not traced for
walls (above).

A wall here is a stretch between two junctions, so the counts are not
comparable with a reader that reports one wall per pair of drawn faces — this
one reports each wall **once**, and the metres are centreline rather than drawn
line.

### The breaks have to be read before the closing

Closing joins a wall's two faces into one band with a kernel the width of the
thickest wall. A door is 820 mm, so in principle closing cannot bridge it — in
practice it does, because **a doorway is not empty on a drawing**: the office
draws the jambs across the wall, the leaf, the swing arc and often a threshold.
Closing joins the wall to that ink and the ink to the far side, and the band
comes out continuous. The break is gone before anything can look for it.

`breaks.py` reads them off the **faces** instead, before any rasterising: a
door goes through the wall, so both faces stop at the same place and start
again together. `walls.py`'s own face merging, best-first pairing and shared-gap
test are imported and reused — there must not be two answers in one codebase to
"what is a break".

**Best-first pairing is the whole difference between a break and an artefact.**
Pairing every plausible pair lets one face pair with a dozen others, and every
fragmentation gap in any of them becomes a "shared gap": measured on one sheet
read as a picture, 69 of them, which punched so much out of the mask that the
sheet fell from 37 walls to 16.

**Cutting the gaps out of the mask is off by default, and that is measured.**
Cutting is the obvious way to stop the morphology welding a doorway shut, and
it does raise the breaks reaching a wall — 18 to 44 on a 23-sheet set. But it
raised the **openings** on no set and cost two on another, because cutting also
removes the wall either side of the gap, and *an opening has wall on both sides
of it* is exactly what `openingevidence` tests before it will call a gap a
door. The same applies to the clearance: swept on two sets, a clearance share of
0.0 gives 4 openings and 20 breaks, 0.25 gives 3 and 19, 0.5 gives 1 and 19. So
the gaps are recorded on the walls and the cutting is left off, with both
capabilities configurable and the measurements beside them.

## Performance

Thinning is the expensive step and it is done per connected component:

| | one A3 floor plan at 300 DPI |
|---|---|
| thinning the whole sheet | **52 s** |
| thinning each component in its own box | **2.9 s** |
| a sprawling component, at full resolution | **14.1 s of a 15 s stage** |
| the same, reduced to a pixel budget | **~1 s** |

Components are 8-disconnected by definition, so thinning one can never depend
on another, and each one's box is a few per cent of the sheet — 93% of an A3
plan at 300 DPI is blank paper. The skeleton is unchanged; the saving is
eighteen-fold.

**But a component's bounding box is not its size.** The walls of a building are
one connected network, so its component sprawls: measured on one real sheet, two
components had boxes of 7.9 and 8.7 megapixels on an 8.7 megapixel image, and
all its components' boxes added to **18.7 megapixels on an 8.7 megapixel
sheet**. So a box larger than `thinning_pixel_budget` is thinned at a reduced
resolution — and **the thickness does not go with it**, because that is measured
from the full-resolution distance transform at the centreline's own pixels. Only
the path down the middle is traced coarsely, and it is simplified to
`simplify_mm` afterwards anyway: at 1:100 a divisor of two moves a centreline by
at most 17 mm against a 30 mm tolerance. The guard is a pixel floor — the
thinnest wall must still be several pixels across, because thinning a line
rather than a band gives a skeleton that wanders.

*Measured: the worst sheet's wall stage went from **26.6 s to 8.6 s**, and a
17-sheet plan set from **55.5 s to 33.5 s** end to end.*

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

`backend/tests/test_cvdetect.py` — 77 tests, each naming the mistake it
prevents rather than restating what the code does. They include an end-to-end
run against a **building drawn for the purpose**: a 12 m × 8 m rectangle in
230 mm wall at 1:100, read back and compared with what was drawn. Scoring a
reader against a plan set it was built on is how a threshold gets tuned to that
plan set.

```bash
cd backend && python -m pytest tests/test_cvdetect.py -q
```
