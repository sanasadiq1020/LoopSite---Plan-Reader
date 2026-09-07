# Known issues and open work

Honest record of what this build does not do well, and what is left. The
interface shows the reader-facing version of this under **About this tool**,
read from `config/version.json`.

---

## P0 (FIXED) — the building envelope was detected at full length and then deleted

The reader was tracing every external wall and two downstream rules were
setting them aside. Both rested on a premise that is **structurally false for
the outside of a building**, so neither could be repaired by widening a
threshold.

**The structure-word rule** (`cvwalls._the_rest_of_that_structure`) set a
candidate aside when a roof word was printed near it and it stood clear of the
box the room labels occupy. That guard is false by construction: room labels sit
inside rooms, so their box always falls inside the outermost walls, and **every**
external wall stands clear of it. On one floor plan it missed by **0.9 pt of
paper — 32 mm** and deleted the building's whole **13.2 m south external wall, a
wall carrying nine junctions to interior partitions**, because `Extent of roof`
was printed 564 mm below it. Cost: 22% of that sheet's traced wall.

*Fixed* by requiring the geometry to show a roof member before a printed word is
believed: parallel companions, one repeated spacing, and nothing landing on the
candidate between its two ends. The word may now only confirm what the drawing
has already shown.

**The building outline** (`walls.building_outline`) was the bounding box of the
largest connected group of walls. An external wall is the one a house has fewest
junctions on, so where the tracing is fragmented the envelope is exactly what
that group leaves out; the outline shrank to the interior and the envelope then
tested "outside the building". Measured on one sheet the box was **4.4 m
narrower than the building on the west and 4.8 m on the east**, and it set aside
ten external walls carrying 35 m — 44% of that sheet's traced wall.

*Fixed* by checking the group against the sheet's own printed overall
dimensions, which come from the drafter rather than from any wall. An axis whose
group falls materially short of what the drawing says the building measures, or
which prints no overall at all, no longer judges anything.

*Measured, end to end on all three plan sets:* P01 63.3 → 76.5 m and closure
44.0% → 57.7%; P04 62.6 → 90.9 m with the envelope check going **fail → pass**
(x −25.0% → −5.0%). `new_sample_plan.pdf` is byte-identical on all four of its
drawn sheets. Ten candidates changed verdict across the corpus and every one of
them is now kept; nothing was lost anywhere.

---

## P1 — a carport drawn attached to the house is reported as walls of the house

`sample_plan.pdf` P04 draws its carport joined to the building, so the
detached-structure rule cannot see it (the same limitation recorded in
CLAUDE.md 4AP for a pergola). Four of that sheet's candidates — 17.6 m — are the
carport's own outline and are reported as walls.

This is pre-existing in kind, and the envelope repair above made four more of
them visible rather than creating them. It is arguably not even wrong on this
sheet: the sheet's own `23,530 OVERALL` dimension string spans the house *and*
the carport, which is why the envelope check passes with them included. It will
be wrong for the take-off, where a carport post is not a wall.

Telling an attached structure from the building it is attached to needs
something the wall graph does not carry — the roof line, or the absence of
enclosure — and is a separate piece of work.

---

## P1 — walls are traced on a sheet whose scale is contradicted

`unseen_plan.pdf` P03 is classified `notes`, reports its scale as
**contradicted**, and still produces fourteen wall candidates with lengths in
millimetres. Four are kept and **not one of them is a wall**: they are the
sheet's own drawing border and the ruled lines of its notes tables. The envelope
check on it reads +39% and +131%, which is the reading correctly saying the
result is meaningless.

Two separate faults sit behind it. The sheet plainly carries a complete
`PROPOSED FLOOR PLAN` and a `PROPOSED SUB-FLOOR FRAMING` plan but is classified
from a title that names neither (CLAUDE.md 4Q). And a sheet that could not
establish a scale should report no lengths at all, which is what
`wall.min_walls_for_vector` and the scale gate are supposed to guarantee.

The envelope repair above added one more such line to that sheet — a 21.66 m
run which is the sheet's right-hand border. It is a symptom of this defect
rather than a new one, but it is named here rather than buried.

---

## P1 (PARTLY FIXED) — external walls formed too few junctions

**A correction to what this file previously said.** It recorded that traced
centrelines stop "0.2 m to 4.4 m short of the wall they should meet" and that a
wall's two ends sat 356 mm and 364 mm from their neighbours — "two junctions
missed by 3 mm and 11 mm", implying a junction-test bug. **That measurement was
wrong.** It took the distance from a wall end to the nearest *bounding box* on
the sheet, which counts two parallel walls a room apart as a missed junction. It
was also read from `plan_reading.json`, which deliberately does not carry
per-wall junction points — they are in `wall_graph.json` — so every end looked
free.

Measured properly, against pairs that could legitimately meet:

* **75% of wall ends were already joined** (265 of 354).
* **Not one** free end was inside tolerance on both axes, so there was no
  junction-test bug to find.
* Of the 89 free ends, only 8 were inside the ray's reach, and only 5 open
  corners existed across all three plan sets.

**What was actually wrong** was that nothing carried an end more than a short
way. 35 of 69 free ends pointing along their own line at a wall had that wall
*drawn in the gap*, 14 with unbroken ink over 1.8 to 14.8 m. Fixed by
`junctions.extend_along_the_ink`: an end is carried on for as long as the sheet
draws both of the wall's faces the whole way to a wall on its own line. Walls on
a closed circuit went 15/26 → 17/28, 12/32 → 24/35, 38/48 → 41/55, 0/8 → 7/9,
25/26 → 26/26 and 17/29 → 26/40.

**What remains**, and it is the honest half: 20 free ends have nothing on their
own line to meet, and 32 stop more than 3 m short with the drawing showing
nothing in between. Those are wall the tracing never found, and no rule that
respects the drawing can close them.

---

## P2 — closure is a ratio, and keeping more walls can lower it

On one sheet the ink-carried extension took walls on a closed circuit from
**38 to 41** while the closure figure fell from **79.2% to 74.5%**, because
seven more candidates were kept at the same time. The reading got better and the
headline number got worse.

The metric is not wrong — the share of kept walls that enclose something is
worth knowing — but it cannot be read on its own, and this project has already
been misled twice by a single number moving the wrong way (wall length rising
while the reading got worse; a sheet scoring best on wall length having found no
wall at all). `self_check.json` carries `on_a_closed_loop` and `walls` beside the
percentage; anything quoting closure should quote the raw pair with it.

---

## P1 — one sheet's traced building is 13% wider than the figure it prints

`new_sample_plan.pdf` A06, an electrical plan: x envelope +10.5% before the
ink-carried extension and **+13.2%** after, against a 10% tolerance. Its longest
wall is traced at 24.14 m where the sheet's largest dimension-string total is
21,495 mm.

Not established either way, and stated as such. The printed figure is a
`dimension_string_total` rather than an overall the drafter marked as one, so it
may not span the whole building; and the traced run may genuinely be carrying on
past the building along a setout line. Settling it needs the sheet read against
a figure that certainly measures the whole building, which that sheet may not
print. The y envelope on the same sheet is −5.4% and passes.

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

## P0 (FIXED) — a scale status the response schema had never heard of emptied the whole plan

**The worst class of failure this product can produce, and it shipped.** The
interface showed "The results for this plan could not be loaded"; every tab read
zero — rooms, dimensions, walls, doors and windows, schedules, legends — and
opening a sheet said "Nothing was read from this sheet". The sheet register
rendered perfectly, which is what made it look like a reading problem rather
than a transport one.

Nothing was wrong with the reading. `GET /{run_id}/reading` returned **HTTP
500**: `pages.0.scale_calibration.result — Input should be 'confirmed',
'contradicted', 'inconclusive' or 'not_checked' [input_value='printed_only']`,
repeated for five of the six sheets.

**The cause.** Phase 4A added the `printed_only` scale status to the reader and
to the browser's TypeScript, and not to `app/schemas.py`, which declares that
field as a closed set. Pydantic does not degrade a field it cannot validate — it
**rejects the entire response**. One unlisted string emptied a whole plan.

Each side was individually correct, which is why nothing caught it: the
TypeScript compiled, the tests passed, and the pipeline wrote the right answer
to disk. The commit that introduced it verified the frontend build and never
exercised the endpoint — the same mistake Section 4AK records as *a check that
passes because the machine lacks something proves nothing*.

**A second, quieter half.** The models declare no `extra` policy, so Pydantic's
default silently **drops** any field the schema does not name. Every field added
in Phases 4A to 7 was being stripped from the response and never reached the
interface: eight on the scale record and ten on the wall record, including all
of the thickness provenance and uncertainty. The app looked fine; the
information simply was not there.

**Fixed**: the `result` literal now lists every value the reader can produce,
and the new fields are declared on both models and rendered in the walls table.

**Guarded**, in `backend/tests/test_response_contract.py`, and the guard is
proved to fail — removing `printed_only` from the schema again fails it with
"the reader can report ['printed_only'] and the response schema does not list
it, so any plan with such a sheet would fail validation and reach the browser as
an empty reading":

* `scale.RESULTS` is now the reader's own list of what it can emit, and a test
  asserts the schema lists every one of them, so the two cannot drift.
* Two tests assert that the scale record and the wall record carry no field the
  response would silently drop, with the deliberately internal wall fields named
  in `WALL_FIELDS_KEPT_OUT_OF_THE_RESPONSE` — so withholding a field is a
  decision rather than an accident.

**P2 left open**: eleven pre-existing wall fields are still not in the response
(`gaps_pt`, `junction_count`, `merged_from`, `source_sheet`, `stroke_pt` and
others). They predate this work and are named in that list rather than added,
because whether the interface wants them is a separate question.

---

## Phase 7 — the raster path measures between face centres, sub-pixel

The vector reader measures a wall between the centres of its two drawn face
lines. On a rendered sheet those two lines are still two runs of ink separated
by paper; what is lost is only the exactness of a coordinate, and that is
recoverable. A plotted stroke is symmetric about the line it draws and rendering
spreads it symmetrically through anti-aliasing, so the **intensity-weighted
centroid of a run is that line's own position**, to a fraction of a pixel. That
is the standard sub-pixel edge localisation result, and it is why weighing the
grey beats anything measured off a binarised mask: thresholding discards exactly
the grey that says where inside a pixel the line fell.

| the plan drawn to purpose | outer-to-outer | band less stroke | **face centres** |
|---|---|---|---|
| rasterised, mean error | 42.4 mm | 2.0 mm | **1.7 mm** |
| rasterised, worst error | 45.5 mm | 3.1 mm | **2.1 mm** |
| rasterised, within 12 mm | 0 of 6 | 6 of 6 | **6 of 6** |
| vector, mean error | — | 0.5 mm | **0.5 mm** |

### The two corrections are exclusive, never layered

A centroid is a **position**, not an edge, so measuring between two centroids
gives centre-to-centre directly and there is no stroke in it to remove.
Subtracting one as well would take off a width that was never included. So
`_band_thickness_mm` picks exactly one of three, in order, and the choice
travels with the wall as its provenance:

1. `page_faces` — the two ink runs' centroids on the greyscale render;
2. `band_less_stroke` — where the profile cannot be read, the binarised band
   with the plotted stroke measured off it and subtracted;
3. `band` — where neither is possible, the raw band, which reads wide.

Stroke subtraction is therefore **retained as the stated fallback, not
superseded outright**: it fired 16 times on one plan set, 10 on another and 5 on
the third, on runs where the profile gave fewer than two ink runs.

**What the centroid relies on**: a greyscale render being available; a stroke
being symmetric about its centre; a wall's two faces being separable by paper.
**What breaks it**, in each case falling back rather than misreporting: a
bi-level scan with no grey to weigh; a wall drawn solid or with its faces welded
together, giving one run instead of two; a face lost to thresholding upstream.
Heavy JPEG ringing makes a run asymmetric and biases its centroid, which widens
the disagreement between cuts along the wall and is reported as uncertainty.

Ink is counted where it is darker than a share of each cut's **own peak**, so
the floor scales with the sheet's contrast rather than assuming white paper. The
centroid of a symmetric profile is unchanged by symmetric truncation, so that
share only has to exclude a neighbouring feature; it is not a tuned number.

### A measurement may not be held to a precision the instrument lacks

The reportable buildable range is a nominal bound on a measurement, and Phase 5
gave it an allowance drawn from the PDF's **coordinate** precision — right for a
thickness measured between two vector faces, wrong for one measured off an
image, which is quantised at the pixel however carefully the profile is weighed.
A real 90 mm wall measured at 87.0 was dropped entirely rather than reported
three millimetres out. The allowance is now the measurement's own precision: a
pixel where the value came from pixels, which is 8.5 mm at 300 DPI and 1:100 and
16.9 mm at 1:200 — the honest statement that at that resolution the two cannot
be told apart.

### P1 — right on ground truth, and mixed across the plan sets

**Derbyshire A05 recovered**, which is what this change was taken up to test:
closure back from 72.0% to **96.2%** with its envelope unchanged, undoing the
Phase 6 regression exactly. A06 improved too, 42.4% to **58.6%**.

But two plan sets lost walls:

| | walls | closure |
|---|---|---|
| sample P01 | 34 → 25 | 61.8% → 44.0% |
| sample P04 | 36 → 24 | 47.2% → 37.5% |
| Derbyshire A02 | 48 → 48 | 81.2% → 79.2% |
| **Derbyshire A05** | 25 → 26 | **72.0% → 96.2%** |
| Derbyshire A06 | 33 → 29 | 42.4% → **58.6%** |
| unseen P02 | 13 → 4 | 0% → 0% |

**Where the walls went, measured.** On one plan set the buildable-range gate
rejected 77 candidates, **56 of them as too thin, measuring 39 to 59 mm**. Those
are pairs of lines genuinely closer together than any wall an office builds -
linings, hatch boundaries, joinery - which the inflated band reading used to
push up into the 90-300 mm range and report as walls. Measuring them correctly
is why they are gone.

**That does not settle it, and it is not claimed to.** Closure fell on both
sheets of that set, and a spurious candidate can be part of a real circuit, so a
correct removal can still break one. Whether the count fell because false walls
went or because real ones did is not established here, and the overlays are the
place to settle it. Stopped and reported rather than proceeding.

---

## Phase 6 — the band's stroke is subtracted; the raster path measures for the first time

Measured on the plan drawn to purpose, published as a picture — the only ground
truth in this project:

| | before | after |
|---|---|---|
| **within the 12 mm tolerance, rasterised** | **0 of 6** | **6 of 6** |
| mean absolute error, rasterised | 42.4 mm | **2.0 mm** |
| worst error, rasterised | 45.5 mm | **3.1 mm** |
| within tolerance, vector | 5 of 6 | **6 of 6** |
| mean absolute error, vector | 7.6 mm | **0.5 mm** |

`test_the_thickness_read_from_a_picture_is_pinned_to_what_it_measures` has
become `..._is_within_the_nominal_tolerance` — the strict test again, exactly as
the pinned version said it would when the defect was fixed.

**The rule, and what it rests on.** A band is measured outer edge to outer edge,
so twice the distance transform spans from the far side of one drawn face to the
far side of the other — the wall's thickness plus one whole stroke, because each
face contributes half a stroke at each end. A stroke is symmetric about the line
it draws, so subtracting one puts the measurement back between the lines. That
is geometry: it holds at any stroke width, resolution or scale.

**The stroke is measured off the image, never assumed.** What reaches the pixels
is not the width the PDF states: rendering resolves the line onto a grid,
anti-aliases its edges, and the binariser keeps whatever passes its threshold.
Measured here, one 0.7 pt line came back **7 px wide on one face of a wall and 5
px on the other**.

**The asymmetry is resolved by taking the narrower run, not the mean.**
Anti-aliasing widens both runs alike, but anything else that touches a run only
ever *adds* ink — a fitting drawn against the wall, a hatch meeting it, a
neighbour the closing welded on — and none of it can make a run narrower than
the stroke that drew it. So the wider run is contaminated and the narrower one
is the stroke. Using the mean instead was measured and rejected: one wall's runs
came back 5 px and 11 px, the mean of 8 subtracted a stroke that was never
there, and the wall fell below the thinnest thickness an office builds and was
dropped altogether. The difference between the two runs is carried as the
measurement's uncertainty rather than averaged away.

**What would break it.** A drafter plotting the two faces of one wall with
different pens, which is not how a wall is drawn; and a face so faint that
binarisation loses part of it, which makes the narrower run too narrow and
under-subtracts — leaving the wall reading wide rather than vanishing.

### P1 — the correction is right on ground truth and mixed on the plan sets

Stopped here rather than continuing, because the effect across the three plan
sets is genuinely mixed and the method for this phase says to stop and say so.

| | walls | closure | envelope |
|---|---|---|---|
| sample P04 | 37 → 36 | 43.2% → **47.2%** | x −26% → **−9%**, y +18% → **−2%** |
| sample P01 | 40 → 34 | 65.0% → 61.8% | x +15% → **−4%**, y +12% → +24% |
| Derbyshire A02 | 53 → 48 | 83.0% → 81.2% | y −13% → −17% |
| **Derbyshire A05** | 26 → 25 | **96.2% → 72.0%** | unchanged (19721 → 19692 traced) |
| unseen P02 | 7 → **13** | 0% → 0% | see below |

**A05 is a real regression** and is not explained away: one wall fewer, and
seven walls fell off the closed circuit while the envelope barely moved. A
thinner corrected thickness changes which candidates pass the buildable range,
and on that sheet it cost circuit membership.

**unseen P02's envelope is not measurable in either direction.** The only figure
that sheet prints is a **single 820 mm door leaf width**, so the check is
comparing a whole traced building against a door. Before: 4.98 m × 5.57 m.
After: 28.57 m × 14.86 m. The second is a plausible house and the first is not,
but the check cannot say so — this is the Phase 4 finding about that sheet,
unchanged.

**Run time**: 24.9 → 28.0 s, 53.2 → 58.6 s, 35.2 → 36.9 s. The cost is the
profile cut taken across each wall at up to nine points along it.

### Not attempted, and why

Adaptive render resolution and a sub-pixel face-equivalent on raster were the
next two changes of this phase and were **not made**, because the method says to
stop when one plan set improves and another worsens. Both were measured first
and the numbers are worth keeping:

* **A sub-pixel face-equivalent works, and works well.** Taking the
  intensity-weighted centroid of each of a wall's two ink runs from the
  *greyscale* render, rather than the binarised mask, recovers the face
  separation on the drawn-to-purpose plan to a **worst error of 0.84 mm across
  all six walls** — against 3.13 mm for the binarised outer-minus-stroke used
  above, and 40.9 mm for the uncorrected band. It is the raster equivalent of
  the vector path's face pairing and would supersede the stroke subtraction
  where the profile can be read.
* **Resolution is quantised before any error occurs.** At 300 DPI and 1:100 a
  pixel is 8.47 mm, so a 12 mm tolerance is 1.4 px and a 90 mm wall is 10.6 px;
  at 1:200 a pixel is 16.9 mm. Rendering the same sheet at 600 DPI did **not**
  improve the centroid measurement here (0.50 mm against 0.30 mm at 300),
  because the fixture's drawing is itself an embedded image with a resolution of
  its own — enlarging it adds no information, which is the same finding Section
  4D records for the render resolution.

---

## Phase 5 — the three thickness P0s are fixed; what remains

Measured on the plan drawn to purpose — a 12 m x 8 m building in 230 mm
external wall with two 90 mm partitions, read back and compared with what was
drawn. It is the only ground truth in this project.

| | before | after |
|---|---|---|
| walls within the 12 mm tolerance, vector | **0 of 6** | **5 of 6** |
| mean absolute error, vector | 38.4 mm | **7.6 mm** |
| worst error, vector | 49.4 mm | 45.5 mm |
| walls within tolerance, raster | 0 of 6 | 0 of 6 |

The four external walls now read **230.0 mm (+0.0)** and the vertical partition
**90.0 mm (-0.0)**. `averaged_mixed` no longer occurs on any of the three plan
sets.

**What was fixed, and where each fault actually was.**

*The boundary rejection was in two places, not one.* `wall.twin_min_mm` rejected
a real 90 mm pair measuring 89.9583 by 42 micrometres, and so did
`wall.min_thickness_mm` on the reportable range - fixing only the first made the
partition disappear entirely rather than measure correctly, because the true
89.96 was then refused by the second. Both bounds now carry a tolerance derived
from the drawing's coordinate precision through the sheet's own scale
(`wall.coordinate_precision_pt`, 0.71 mm at 1:100).

*The axis-less runs were bent runs, not single-piece runs.* The Phase 2 note
attributed them to `_merge_collinear_runs` returning `pieces[0]` unchanged. That
is not the cause: a single-piece run keeps the axis it was given at the
straight/bent split. The runs with no axis are the ones that **turn a corner** -
a skeleton traced round an external corner spreads in both x and y, fails the
axis-aligned test and is returned as "bent". Measured, that was 25%, 28% and 33%
of all runs on the three plan sets. They are now asked about the faces one
straight stretch at a time.

*The mean is gone.* Face-derived pieces decide a wall's thickness where there
are any, weighted by length; band pieces decide it only where there are none,
and are recorded as set aside rather than averaged in. A wall whose pieces
differ by more than the nominal tolerance carries reduced confidence and
`thickness_pieces_disagree`.

### P1 (FIXED in Phase 6) — the band's stroke inflation is real, correctable in principle, and left

The band is measured outer edge to outer edge and never subtracts the plotted
stroke. Measured on the drawn-to-purpose plan at 300 DPI, where the two faces of
a 230 mm wall are drawn 6.520 pt apart with a 0.7 pt stroke:

| | |
|---|---|
| outer edge to outer edge | 32 px = **270.9 mm** (+40.9) |
| the two rasterised strokes | **7 px** and **5 px** — for the same 0.7 pt stroke |
| subtracting one mean stroke | 220.1 mm (**-9.9 mm**, inside tolerance) |

So the correction would work. It is left for the raster work for three reasons,
each of which is a fact rather than a preference:

* it needs the stroke width **measured off the rendered image** for each wall,
  which is raster measurement rather than face-pair measurement;
* the rasterised stroke is **asymmetric** - 7 px against 5 px for one stroke -
  so the correction carries about a pixel of noise, which is 8.5 mm of building
  at 300 DPI, and landing inside a 12 mm tolerance on that basis is not robust;
* it is only ever needed where the faces are unavailable, and the face path now
  reaches most walls on a vector sheet, so what remains is concentrated on
  sheets stored as pictures - which is where the raster work lives.

Until then a band reading is labelled: every such wall carries
`thickness_is_a_stand_in` and a sentence saying the band is measured outer edge
to outer edge and reads wider than the wall is, and the per-sheet provenance
check repeats it.

### P2 (ADDRESSED in Phase 7) — the raster path has no face measurement at all

On the drawn-to-purpose plan published as a picture, all six walls still read
from the band and none lands within tolerance. That is not a regression and not
a fault in this phase's work: a sheet stored as a picture carries no vector
faces to pair, so there is nothing for the face path to consult. It is the same
finding as the stroke inflation above seen from the other side, and the same
raster work would address both.

---

## Phase 4 — scale calibration: what was found outside its own scope

Recorded here rather than fixed, because the scale phase was scoped to
`calibration.py`, `titleblockscale.py`, `dimensions.py` and their config.

### P1 — the ranked title-block scale reader is not in the pipeline that runs

`cvdetect/titleblockscale.py` implements the ranked reading recorded in Section
4AZ of `CLAUDE.md` — the four edge strips, the confidence ranks, the five traps
(`1:100MM FALL ON SPANDECK`, `DO NOT SCALE DRAWING`, the drawing-index column,
label-above-value, `NTS`). It is imported by `cvdetect/calibration.py`, and
**`cvdetect/calibration.py` is imported only by `cvdetect/detector.py`**, which
is the standalone command-line reader. The web pipeline calibrates through
`pipeline/plan/scale.py` and hands that record to `cvwalls.detect_walls`, so
none of that ranking or trap-avoidance is applied to an upload. The ISO
paper-size correction was reimplemented in `scale.py` during this phase for
that reason; the rest of the ranked reading was left alone as out of scope.

### P1 — the printed-clear-of-the-plan rule refuses real internal setout strings

The rule that stops a run of door-leaf widths being read as a dimension string
(Phase 4, Step 1) works on the convention that a setout string is laid clear of
the drawing it measures. Some offices dimension internally, and those strings
are refused as scale evidence even though they are real. Measured across the
three plan sets, 14 chains were refused, and the following are genuine setout
strings rather than annotations:

| sheet | chain | figures |
|---|---|---|
| A06 | CH05 | `90, 620, 90, 2910, 90, 610, 1190, 90, …` |
| A18 | CH08, CH10 | `500, 1000, 300, 900` and `100, 200, 100, 1700` |
| A19 | CH07, CH08 | `300, 900, 600, 900` |

**No sheet lost its confirmation as a result** — each still had ten or more
strings printed clear of the plan. The cost is real but currently free, and it
would stop being free on a sheet that dimensions internally and carries few
strings. The rule degrades gracefully by design: a sheet that lays *no* string
clear of the plan falls back to its internal strings and says so in
`strings_from`.

### P2 — two calibration constants are in code, not config

`scale.py` holds `_MIN_MEMBERS = 3` and `_MIN_SPAN_MM = 1000.0` as module
constants. Both decide whether a printed string counts as evidence, which is
the kind of threshold Critical Rule 1 puts in `/config`. `_MIN_MEMBERS = 3` is
the reason `P01-CH03` (a real two-figure string) is not counted, which is part
of why that sheet has only one usable string.

### P1 — a picture-drawn floor plan reports an envelope 508% over

`unseen_plan` P02 traces an envelope 507.9% wider than the largest figure the
sheet prints. It predates this phase and is unchanged by it. The sheet is
traced from pixels and its setout strings are not in the text layer at all, so
the envelope check is comparing a badly traced building against a door width —
both halves are weak. It is recorded because it is the largest single
disagreement the self-checks report anywhere.

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

### P0 (FIXED in Phase 5) — the twin floor rejects an exact 90 mm pair by 42 micrometres

`wall.twin_min_mm` is `90.0` and the measured pair is `89.9583 mm`, so
`twin_min <= pair["thickness_mm"]` fails and **both 90 mm partitions fall back
to the band's 135.5 mm**. A floor written as the exact nominal cannot admit a
measurement *of* that nominal: a real measurement scatters either side of the
value, and half of that scatter is below it.

### P0 (FIXED in Phase 5, and the cause was different) — a run with no axis can never consult the faces

`_merge_collinear_runs` returns `pieces[0]` unchanged where a run has one piece
(`wallgeometry.py:885`), and a gathered piece carries only `points`,
`fill_share` and `drawn_as` — no `axis`, no `position`, no `thickness_mm`.
`_thickness_from_the_faces` returns `None` on its first line for exactly that
(`if not face_pairs or "axis" not in run`). **Measured: 3 of 7 runs on the
synthetic plan reach the matcher with no axis** and silently take the band's
figure. The matcher fired 4 times, not 7, and matched 2.

### P0 (FIXED in Phase 5) — the correct measurement is averaged away after being made

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
