# How it is put together

For whoever maintains or extends this. For using it, see the README at the root
of the project.

---

## The shape of it

```
PDF  →  read each sheet  →  canonical building model  →  3D model
                                                     →  (elevations)
                                                     →  (take-off)
                                                     →  (crew packages)
```

The **canonical model** is the contract. Everything built from the plan reads
from it, and nothing downstream reads the PDF again or redraws what the model
already holds. That is what stops a wall length in a 3D model disagreeing with
the same wall in a schedule.

The reading stage and the model stage are deliberately separate. Reading knows
about PDFs, text, line work and drawing conventions. The model knows about
millimetres, storeys and buildings. Neither knows anything about the other's
problems.

---

## What happens when

**An upload does only what the first screen needs.** For each sheet: classify
the page, read its text (from the page image where it has none of its own),
build one text model, then read the sheet - title block, rooms, dimensions,
schedules, legends, opening marks, page type, scale, walls, openings. Once
every sheet is read, the drawing index is cross-checked against each of them,
opening marks are reconciled against the whole document's schedules, the
tables are written and the metrics computed.

**What is not done during an upload:** no picture of a page and no marked-up
sheet is drawn. Nothing on the first screen shows either, and a reader opens
two or three sheets out of twenty. Both are drawn the first time one is asked
for, from the saved source PDF and the reading already stored for that page,
and kept once made. Making all of them up front was more than half of an
upload's time.

**The 3D model** is built when a reader chooses a sheet, never during an
upload.

## The reading pipeline

Each module does one thing and hands on a plain data structure.

| Module | What it does |
|---|---|
| `intake` | Opens the PDF, classifies each page, orchestrates the rest, and makes a sheet's images when one is asked for |
| `textmodel` | One text model per page: writing direction, font size, de-duplication, overprints — and the page's own rotation applied |
| `layout` | Ruling lines, table cells, and pairing a label with its value by geometry |
| `validators` | Per-field format checks; a value that fails is reported as not found, never stored raw |
| `titleblock` | Sheet number, title, scale, revision, project and issue details |
| `sheetindex` | The drawing index printed on the cover, cross-checked against each sheet |
| `pagetype` | What each sheet draws — and separately, whether it draws a plan |
| `rooms` | Room and area labels, instances and floor areas |
| `dimensions` | Figures, the axis they measure, strings, and arithmetic self-checks |
| `schedules` | Schedule tables (both layouts) and legends |
| `scale` | The printed scale verified against the sheet's own dimension strings |
| `walls` | Pairs of parallel faces merged and matched into candidate walls |
| `rasterlines` | Line work recovered from the page image, for sheets drawn as pictures |
| `openings` | Marks reconciled against the whole document's schedules, and placed on walls |
| `overlay` | Everything found, drawn over the original sheet |
| `accuracy` | Comparison against a hand-answered checking sheet, when one exists |
| `reading` | Orchestration, metrics and the spreadsheet outputs |

## The model pipeline

| Module | What it does |
|---|---|
| `model/height` | The storey height, from the drawing where it states one |
| `model/canonical` | The building model in millimetres, with stable element identifiers |
| `model/exporters` | IFC, GLB and OBJ, each written from the model and never from each other. A wall is built as the solid pieces left once its openings are taken out; IFC carries them as real `IfcOpeningElement` voids |
| `model/build` | Builds and writes one sheet's model on request |

---

## How an opening reaches the model

An opening exists in two places on a plan set — a mark on the drawing and a row
in a schedule — and the schedules are printed on their own sheets. So it is
assembled in four steps, each doing one thing:

1. `openings.place_openings_on_walls` — per sheet, gathers every wall the mark
   could be labelling, with the breaks in each.
2. `openings.reconcile_openings_with_schedules` — once the whole document has
   been read, joins each mark to its schedule row.
3. `openings.settle_opening_placement` — chooses the wall and the place along
   it, now that the schedule's width is known. A break beside the mark that
   measures what the schedule says is the opening itself; where there is no
   such break the mark's own position is used, and the record says which.
   A break holds one opening, so the breaks on a sheet are handed out
   best-first rather than each mark taking its favourite.
4. `model/canonical` — turns that into a hole: which wall, where along it, how
   wide, how tall. Anything the drawings do not establish is carried on the
   model uncut, with one sentence saying which of the four is missing.

---

## Rules the code holds to

**Nothing extracted is hardcoded.** No room name, coordinate, count or answer
from any particular plan appears in the code. What varies between offices —
vocabulary, labels, thicknesses, tolerances — lives in
`config/plan_reading.json`. What does not vary — that a PDF point is 1/72 inch,
that a rotated figure dimensions the vertical axis, that a wall is two parallel
faces — lives in the code.

**One canonical model.** Every derived view reads from it. Nothing is
recalculated from a screenshot or redrawn independently.

**Every output is traceable.** Every record carries the sheet it came from, the
position on that sheet, how it was read, and a confidence.

**Nothing is guessed silently.** An unknown becomes a visible flag with a
reason. A value that fails its format check is reported as not found rather
than stored as read.

**Nothing crashes.** Every file and processing operation is guarded; a failure
degrades to a logged, visible error state and the rest of the run continues.

**Every element in the model carries** an identifier, a type, its storey,
geometry, dimensions, source sheet, source position, extraction method,
confidence, review status and linked issues.

---

## Some decisions worth knowing about

**Text is read with the page's own rotation applied.** A PDF page carries a
rotation, and drawing exports very often use one — a sheet drafted portrait and
printed landscape is stored upright with a 90° rotation. The text and line work
come back in the *unrotated* space while the page renders rotated, so the
coordinates are turned once, where they are read. Left alone, every mark lands
somewhere else on the sheet and horizontal text is measured as vertical.

**A row of a table is text printed in one direction.** A figure printed
sideways is as tall on the page as it is long, so it overlaps everything beside
it. Rows are grouped by direction as well as position.

**Label and value are paired by geometry, not by distance.** Ruling lines give
the enclosing cell, and the labels printed on one row define the column
boundaries between them. Nearest-neighbour pairing puts the next column's value
in a blank cell.

**A title block may be printed sideways.** The geometry is turned 90° and the
same pairing rules are applied, rather than writing a second set of rules.

**Wall faces are merged across openings and paired by shared run length.**
Joining only touching pieces finds nothing longer than a room, and pairing by
nearest neighbour finds joinery instead of walls.

**A sheet's own geometry is measured first.** Only when it yields no walls at
all is the page rendered and its lines recovered from the image — deciding on
the outcome rather than on a line count.

**A wall cannot be longer than the sheet measures.** A candidate longer than
any distance the sheet dimensions is flagged rather than deleted: a sheet may
also dimension only part of what it draws.

**The building has its own coordinate system.** X east, Y north, Z up, origin
at the building's south-west corner. A PDF's Y grows downward; left unturned,
the model is a mirror image of the plan and looks entirely convincing.

---

## Access and storage

Every browser gets an anonymous server-issued session (an HttpOnly cookie, no
sign-in), enforced on every route that returns data. One session can never read
another's plan, even knowing its identifier. There is deliberately no static
file mount; every output file is served through a checked route.

This is per-browser isolation, not user accounts. Clearing cookies or switching
browsers starts a new session. It is enough for sharing one deployed link; real
multi-user accounts would be a separate piece of work.

**Deployment note.** The session cookie is currently `secure=False,
samesite=lax` for local HTTP. Over HTTPS, `secure=True` must be enabled. If the
interface and the API end up on different domains, the cookie needs
`samesite=none` together with `secure=True`, or session isolation silently
stops working across domains.

**One run at a time on disk.** A new upload clears what came before it, and a
run the reader leaves discards its own folder. What is on disk matches what is
on screen.

---

## Adding a stage

The canonical model is the place to start. Read `project_model.json`, never the
plan. Anything a new stage needs that the model does not carry should be added
to the model rather than re-derived — otherwise two stages end up with two
different answers to the same question, which is precisely what the single
model exists to prevent.
