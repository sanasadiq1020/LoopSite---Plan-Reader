# Known issues and open work

Honest record of what this build does not do well, and what is left. The
interface shows the reader-facing version of this under **About this tool**,
read from `config/version.json`.

---

## P0 — wall thickness is measured wrong, and reported as confirmed

**The single most serious open defect.** The computer-vision reader is now the
only wall detector. On a plan drawn to known dimensions for exactly this
purpose — a 12 m x 8 m building in 230 mm external wall with two 90 mm
partitions, published as a picture the way a real plan set does — it finds all
six walls and measures every one of them too thick:

| axis | wall | drawn | read | error |
|---|---|---|---|---|
| horizontal | external | 230 mm | 270.9 mm | +40.9 mm |
| horizontal | external | 230 mm | 270.9 mm | +40.9 mm |
| vertical | external | 230 mm | 270.9 mm | +40.9 mm |
| vertical | external | 230 mm | 270.9 mm | +40.9 mm |
| vertical | **internal** | **90 mm** | **143.9 mm** | **+53.9 mm** |
| horizontal | **internal** | **90 mm** | **135.5 mm** | **+45.5 mm** |

**Mean error 43.9 mm, worst 53.9 mm, against a 12 mm nominal tolerance.** The
external walls are 18% over; the 90 mm partitions are **60% over**. The reader
removed in the same change measured this drawing to **0.6 mm mean and 1.0 mm
worst** on the same file.

**Why this is P0 rather than a measurement quibble.** The error is not reported
as uncertainty — the wall is presented at a confidence that reads as settled,
and a 271 mm reading is then matched to the nearest thickness the office builds
and reported as *a 270 mm wall*, which is not what was drawn. Thickness is not
a display value: it feeds the 3D model's wall solids, the wall-length variance
metric, the depth of every opening cut into a wall, and every quantity in the
material take-off. Wrong geometry presented as confirmed is the P0 definition.
It blocks nothing **today** only because nothing thickness-critical has been
built on top of it yet.

**Pinned, not hidden.** `test_the_thickness_read_from_a_picture_is_pinned_to_what_it_measures`
asserts the error stays above the nominal tolerance *and* no worse than 55 mm,
so the defect cannot quietly grow and cannot quietly disappear unnoticed. Its
sibling `test_every_drawn_wall_is_found` stays strict: all six walls, right
places, measured from the page. When the thickness is fixed, the pinned test
becomes the strict one again.

**Not fixed in the removal phase by instruction** — that phase was removal and
rewiring only, and no detection logic was changed.

---

## Phase 2 findings — the thickness measurement is exact, and three faults destroy it

Investigation only; nothing below is fixed. The decisive result is that
**nothing needs to be invented, only preserved.** Measured on the synthetic plan
with an 870 mm opening cut into both faces of every wall so face pairs form, the
pairing reports the drawn thickness to within 0.05 mm:

| axis | faces (pt) | measured | drawn |
|---|---|---|---|
| h | 100.00, 106.52 | 230.0111 mm | 230.0 mm |
| h | 320.25, 326.77 | 230.0111 mm | 230.0 mm |
| v | 120.00, 126.52 | 230.0111 mm | 230.0 mm |
| v | 261.73, 264.28 | **89.9583 mm** | **90.0 mm** |
| v | 453.64, 460.16 | 230.0111 mm | 230.0 mm |

Yet **0 of 6 walls land within the 12 mm tolerance**, on a vector page and on a
rasterised copy alike (face path mean 38.4 mm, band path 46.7 mm). Three
separate faults stand between the measurement and the wall.

### P0 — the twin floor rejects an exact 90 mm pair by 42 micrometres

`wall.twin_min_mm` is `90.0` and the measured pair is `89.9583 mm`, so
`twin_min <= pair["thickness_mm"]` fails and **both 90 mm partitions fall back
to the band's 135.5 mm**. A floor written as the exact nominal cannot admit a
measurement *of* that nominal: a real measurement scatters either side of the
value, and half of that scatter is below it.

### P0 — a single-piece run carries no axis, so it can never consult the faces

`_merge_collinear_runs` returns `pieces[0]` unchanged where a run has one piece
(`wallgeometry.py:885`), and a gathered piece carries only `points`,
`fill_share` and `drawn_as` — no `axis`, no `position`, no `thickness_mm`.
`_thickness_from_the_faces` returns `None` on its first line for exactly that
(`if not face_pairs or "axis" not in run`). **Measured: 3 of 7 runs on the
synthetic plan reach the matcher with no axis** and silently take the band's
figure. The matcher fired 4 times, not 7, and matched 2.

### P0 — the correct measurement is averaged away after being made

`cvwalls.py:351` — `thickness_mm = sum(piece["thickness_mm"] for piece in run) / len(run)`
— takes an unweighted mean over the pieces rejoined into one wall.
`wallgeometry` emits the exact `230.011` alongside band readings of `270.933`
and `287.867` for the same wall, and the mean reports **250.5 and 258.9**. No
wall in the final reading carries 230.0 although two were measured at it. This
one destroys the fix for the other two: making the face path reach every wall
achieves nothing while this mean stands.

---

## P0 — one drawing region spans two separate drawings on a sheet

`drawing_region` (`walls.py:1004`) takes a single bounding box over **all** room
labels on a sheet. Nothing anywhere partitions a sheet into its separate
drawings — the multi-caption handling chooses a *title*, never geometry. Given
two plans side by side, measured:

| | region returned |
|---|---|
| one plan (rooms x=100..240) | `[75, 165, 270, 350]` |
| two plans (rooms x=100..240 and 560..740) | `[75, 165, 770, 350]` |

**195 pt wide becomes 695 pt — 23% of the sheet becomes 83%** — and the 320 pt
of empty paper between the drawings is now *inside* the region. That is the
paper the witness lines are printed on, which is the whole reason the region
exists: Section 4AM of `CLAUDE.md` records a framing sheet where **all 25
"walls" were witness lines printed between the two drawings**. Two further
consequences: the sheet's measured extent roughly doubles, so the envelope check
stops catching a wall running from one drawing into the other; and the scale is
pooled as **one value per sheet**, so two drawings at different scales give a
median blended from two populations and correct for neither.

---

## P1 — a watermark reaches the tracing through the picture fallback

`unseen_plan.pdf` carries a "DESIGNER PLANNING" watermark as a single image
XObject — **xref 881, 663x241 px, one placement, bbox [68, -210, 527, 1052]**,
identical on all 17 sheets and the only placement overhanging the page box. It
is cleanly separable from the 20 tiles of 992x876 px that carry the drawing.

It reaches the wall tracing only because that sheet's own vector geometry traces
2 walls — below the four a building takes — so the sheet is **re-read as a
picture, and that render bakes the watermark in**. Removing it (xref 881 only):

| | watermark in | watermark out |
|---|---|---|
| bands walked | 46 | 14 |
| centrelines | 30 | 20 |
| longest wall | 8.22 m | 5.95 m |
| median wall length | 1.16 m | **1.65 m** |

It **adds** roughly ten spurious runs including the false 8.22 m wall; it does
not sever real ones — removing it raises the median length and improves the
long-wall profile. It is not the cause of the under-tracing on that sheet: at 20
centrelines without it, the sheet still traces far less than the 48 of a
comparable vector floor plan.

**The generalisation is the P1, not the watermark.** A sheet whose vector
reading yields too few walls is re-read as a picture, and that render carries
every overlay, stamp and watermark the vector path would never have seen.

---

## P1/P2 — the dimension reader, traced (and a correction)

**Correction first.** An earlier report of this phase said `unseen_plan.pdf`
yields *zero dimension strings on every sheet*. That was a probe bug, not a
finding: the probe read a field named `dimension_strings` and the field is
`dimension_chains`. The real counts are P03 3 chains, P11 3, P12 1, P13 1, and
P01/P02 none. The conclusion drawn from it — that the drawing-region trim is
inert across the file — was wrong with it: on P03 the region trims to
`[478, 117, 842, 595]`. It is inert on P01 and P02 only.

**What the reader receives.** P02: 135 text lines, 102 reach the dimension
reader, 47 numeric (44 horizontal, 3 rotated). P03: 308 lines, 275 reach, 160
numeric (127 horizontal, 30 rotated, 3 at 45 degrees).

**Three suspected causes, all cleared by measurement:**

* *Text rotation is handled correctly.* P03's `3400`, `1690`, `1020` and `800`
  are rotated text (`dir=(-0.0, -1.0)`) and every one is correctly assigned
  `axis=y` and lands in a chain.
* *The table and multi-caption layout is innocent.* Table regions remove 33
  lines on P02 and 33 on P03, and **not one of them is a bare figure**.
* *The keyword list is innocent here.* The 41 and 141 `_classify` rejections are
  genuine notes text — `Stairs are to comply with BCA V2`,
  `T1 - TRUSSES AS PER MANUFACTURERS`, `BRACING : WIND SPEED: N2`.

**P03 is not broken.** Every figure named on it except `90` reaches the reader,
classifies as linear, takes the right axis and joins a chain; 3 chains are built
from groups of 6, 5 and 3 figures.

**P2 — a 90 mm figure is below the plausible floor.** `_classify` accepts no
bare figure under **100 mm**, so a printed `90` — the thickness of a stud
partition, and exactly the figure a wall reader would want — is not read as a
dimension. `365` and above are accepted.

**P1 — P02's setout strings are not in the text layer at all.** Its `820` and
`720` both reach the reader, classify as linear and take `axis=x`, then fail the
chain test because **each sits alone at its own coordinate**: all six of P02's
dimensions sit at six different heights (226, 265, 278, 290, 301, 346), and a
chain needs two or more sharing a perpendicular coordinate. Those six are the
scattered door-leaf widths; the sheet's setout strings are drawn inside its 21
tiled images as pixels. Six isolated figures cannot form a string, and that is
the correct answer to what the text layer holds — the defect is that the sheet
is a picture, which is the same root cause as its wall under-tracing.

**Only this file.** `new_sample_plan.pdf` reads **784 dimensions and 151 chains
across 23 sheets**; `sample_plan.pdf` reads 30 dimensions and 3 chains on each
of its floor plans. The failure is specific to the sheets stored as pictures.

**The consequence is still real on P01 and P02**: with no chain, scale
verification has nothing to check against, the envelope check has no measured
extent, and `drawing_region` falls back to the whole page — three defences
silently inert on the one sheet of the file that draws the building.

---

## P1 — the closing-kernel setting is dead, and the config and the code disagree about intent

`config/cv_detection.json` defines **`wall.closing_share_of_thinnest_wall`**.
The code reads **`wall.closing_share_of_thickest_wall`** (`wallgeometry.py:529`),
which exists only in the built-in defaults. The two names have never met:

* the config key is **never read** — it is the only orphaned key in that file
  (88 keys, 1 orphan), and an office editing it sees no effect whatsoever;
* the code always takes its own default of `1.0`;
* the config's own note describes the **opposite** behaviour — *"a kernel the
  width of the thinnest wall… 8 pixels on a 1:100 sheet at 300 DPI"* — while
  the kernel actually used is sized from `max_thickness_mm` and measures
  **35 px (296 mm)** on exactly that sheet.

**Not fixed, deliberately.** Renaming either side would silently adopt whichever
behaviour happens to be running, and the two describe genuinely different
designs: closing at the *thinnest* wall joins a wall's own two faces without
risking a bridge to its neighbour, while closing at the *thickest* reaches
across any wall the office builds but can weld a wall to something a room away.
Which is correct is a decision, not a typo.

Measured on the drawn-to-purpose plan, the kernel is **not** the cause of the
thickness P0 above — the band is 32 px before closing and 32 px after — so
nothing here is urgent, only wrong.

---

## Limits of what is read

**Walls are candidates, not confirmed walls.** They are pairs of parallel lines
measured from the drawing. A reviewer confirms them against the sheet. On plan
sets with clean line work most fall on a thickness the office actually builds;
on sheets whose drawing is stored as a picture, fewer do.

**A sheet drawn as a picture recovers less.** Where a plan set stores its
drawing as an embedded image rather than as line work, the wall lines are
traced from the page image. Less of the wall run is recovered than from a
vector sheet, and an opening can only be found in a wall that was traced.
Every measurement records which source it came from.

**Openings without printed marks are found from breaks in the walls.** Where a
plan set prints no door or window codes at all, openings are measured where a
wall stops and starts again. Only the width is claimed — not whether it is a
door or a window, and not its height, because a plan does not show one. Recall
on a picture-based sheet is well short of what the drawing contains.

**A title block is read wherever it is placed, except that a sideways strip
loses a field or two.** A block set out horizontally reads in full at any of
the eight positions an office uses - either end or the middle of the top or
bottom edge, and partway up the left or right edge. Where the strip runs up an
edge with the lettering turned 90 degrees, most fields read and one or two come
back as not found rather than wrong.

**A sheet with no title block is still read.** Its rooms, dimensions, walls and
openings all come from the drawing, and it is identified by its position in the
document. It says on its own view that no title block was found, and without a
printed scale no lengths are taken from it.

**A sheet not named as a plan needs its walls to look like a building.** Where
a drawing does not say what kind of sheet it is, walls are reported only when
enough of them are found at a thickness a wall is built to. A plan drawn at an
unusual scale, or one whose walls are drawn faintly, may fall short of that and
report none, saying so rather than showing an empty table.

**Room and title-block vocabulary is configurable, not universal.** Wording
outside `config/plan_reading.json` is reported as not found rather than
guessed. That is the intended behaviour, and it is also the signal that a
configuration entry is missing for that office.

**A sheet with nothing on it always says why.** A drawing can be unreadable
for reasons that have nothing to do with the drawing: its lettering was
converted to outlines on export, the file records no meaning for the glyphs it
draws, the page is a scan and this server cannot run character recognition, or
the upload's allowance for scanned sheets was spent. In every case the sheet
now carries a sentence saying which — on its row, on its own view, and once
above the results when the whole document is affected — and the reason is
written to the issues log. What it cannot do is recover the wording: those
sheets still read as empty, and a copy exported with its text kept as text
reads in full.

**Character recognition is slow and imperfect.** Sheets with no text layer are
read by OCR at roughly a few minutes per dense sheet on a CPU. Each upload has
a fixed budget; past it, remaining sheets are read from whatever text they have
and say so. Observed OCR errors are character-level — a lost apostrophe, a
lost space, a dropped digit — and they surface as ordinary records rather than
being silently corrected. Making large scanned sets practical needs a faster
recognition path and is separate work.

---

## Limits of the model

**A hole needs four things, and a plan can only give three.** Which wall, where
along it and how wide are all read off the drawing; how tall it is is the one
thing a horizontal cut can never show. Where a schedule gives a height, the
opening is cut as a real void in the wall and in the IFC. Where the drawing
names it a door or a window but gives no size, the office default from the
configuration is used and the opening carries that as a stated assumption.
Where nothing on the sheet says which it is, **no opening is cut** — it stays
on the model with its wall, and the model says in one sentence why.

**Where no break was traced, an opening's place comes from its mark.** A wall
drawn with its opening hatched leaves no break to measure, so the position is
taken from where the mark is printed beside it. That is approximate to within
the distance the mark sits from the opening, and every such opening says so on
screen, in `openings.csv` and on the model element.

**A mark between two equally likely walls is left unplaced.** Where nothing
separates two candidate walls — neither has a break where the mark points and
both are at a thickness the office builds — the opening is reported without a
wall and with the reason. A hole in the wrong wall is worse than one waiting to
be placed by hand.

**One storey, one sheet.** A model is built from a single sheet. Sheets are not
combined into a multi-storey building, and there is no site context.

**No materials.** A plan does not state them, so the model carries none.

**Height is only as good as the drawing.** Where a plan set states no storey
height anywhere — no section dimensioning one and no printed levels — the
office default from the configuration is used, and every height in that model
is an assumption. This is shown on screen with the model.

**A sheet carrying two drawings is modelled as one.** Some sheets print, for
example, a framing plan and a floor plan side by side. Both sets of walls end
up in the same model, so its extent is wider than the building. The walls
themselves are correct; they are simply not separated into two drawings.

---

## Not in this release

- Generated 2D elevations from the model
- Material take-off, cost estimate and crew work packages
- Multi-storey assembly
- A packaged single-command install or container image

---

## Memory

Reading a plan holds one page's pixels at a time, not one per sheet, so peak
memory depends on the size of a sheet rather than on how many there are.
Measured on a 23-sheet A3 set: **126 MB**, and 214 MB on a deliberately dense
one. That fits a small instance comfortably.

Two caches that made reading faster used to accumulate instead — every page's
render, and the text and line work cached on each page. Together they took the
same plan set to roughly 670 MB and the process was killed part way through on
a 512 MB server. Both are now released as soon as the page has been read.

If a future change caches anything per page, release it in the same place, or
the same failure returns on the same plan sets.

## Deployment

- The session cookie is set for local HTTP. Over HTTPS `secure=True` must be
  enabled; across different domains it also needs `samesite=none`, or session
  isolation silently stops working.
- The API currently allows one browser origin. That list needs the deployed
  origin added before it is put anywhere.
- The API's own advertised version is separate from
  `config/version.json` and is not currently kept in step with it.

---

## Measuring accuracy

Accuracy figures are only produced when a hand-answered checking sheet exists
for the plan being read, and only rows a person has confirmed are counted. A
run with no checking sheet reports no accuracy — which is correct, and is not
the same as reporting good accuracy.

The tests in `backend/tests/` cover the reading and modelling logic against
constructed cases, each one written from a real failure. They do not measure
accuracy against a real drawing; only a checking sheet can do that.
