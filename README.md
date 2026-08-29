# LoopSite Plan Reader

Upload one approved Australian residential construction PDF. Get back every
sheet read, every room, dimension, wall and opening found, marked over the
original drawing — and a 3D building model built from the same measurements.

**Every value shows where it came from**: which sheet, where on that sheet, how
it was read, and how certain it is. Anything the reader could not establish is
reported as *not found*. Nothing is guessed.

---

## What it produces

| | |
|---|---|
| **Sheet list** | Every page with its drawing number, title, scale, revision and what it draws |
| **Rooms and areas** | Named spaces with their printed sizes — rooms, and areas such as a garage, carport, verandah or deck |
| **Dimensions** | Every figure printed on the drawings, with which axis it measures and what it measures to |
| **Walls** | Wall lines with length and thickness, measured through each sheet's checked scale |
| **Doors and windows** | Every opening, joined to its schedule row and to the wall it sits in |
| **Schedules and legends** | Every row of every schedule table, and every legend entry as printed |
| **Drawing index** | The index printed on the cover, cross-checked against each sheet's own title block |
| **Marked-up sheets** | Everything found, drawn over the original drawing, so it can be checked by eye |
| **3D model** | A building model in millimetres, exported as IFC, GLB and OBJ |
| **Items to check** | Everything worth a second look, with the sheet and position it came from |

Every table can be searched, sorted, and downloaded as a spreadsheet. The
marked-up sheets download one at a time or all together.

---

## Requirements

- **Python 3.10 or later**
- **Node.js 18 or later**
- About 2 GB of free disk space (mostly the Python packages)

Windows, macOS and Linux are all supported. The commands below use Windows
paths; on macOS and Linux use `venv/bin/python` instead of
`venv\Scripts\python.exe`.

---

## Installation

**1. The backend**

```
cd backend
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt
```

**2. The frontend**

```
cd frontend
npm install
```

---

## Running it

Two processes; both need to be running.

**Backend** — from `backend/`:

```
venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

**Frontend** — from `frontend/`:

```
npm run dev
```

Then open **http://localhost:3000** and drop a plan PDF onto the page.

Nothing else is needed. There is no database, no sign-in and no configuration
to fill in before the first run.

---

## Using it

1. **Upload** — drop a PDF on the first screen, or click to choose one. A
   progress bar counts the sheets as they are read and estimates the time left.

2. **Look at the results** — the tabs across the top hold everything found. Any
   sheet in the sheet list opens to show that sheet marked up, with its own
   rooms, dimensions, walls and openings beside it.

3. **Check what needs checking** — anything uncertain is shown as such, with a
   confidence and the reason. The **Items to check** download lists every one
   of them with the sheet and the position on it.

4. **Build the 3D model** — the **3D model** tab lets you choose which sheet to
   build from, then turn the model, zoom, and click any wall to see its length,
   thickness, height and which sheet it was measured from.

5. **Download what you need** — the **Download** button holds every table as a
   spreadsheet, the marked-up sheets, and the model as IFC, GLB, OBJ or the
   underlying data.

---

## How it decides things

The reader is deliberately cautious. A few of the rules worth knowing:

**A printed scale is a claim, not a measurement.** Before any length is taken
from a sheet, the printed scale is checked against that sheet's own dimension
strings. If they disagree but the sheet agrees with itself — which happens when
a drawing has been printed at a reduced size — lengths are measured from the
sheet's own figures and the screen says so. If the strings do not agree with
each other, no lengths are produced from that sheet at all.

**A wall is two parallel lines a wall thickness apart.** Broken pieces are
joined across the openings that interrupt them, and faces are paired by how far
they run together. The result is called a *candidate* on purpose: a reviewer
confirms them against the drawing.

**A floor plan cannot show height.** It is a horizontal cut. So the storey
height is taken from the drawing set two independent ways — the figure the
section sheets dimension, and the distance between the printed floor and
ceiling levels — and the two are compared. Where a plan set states no height
anywhere, the office default from the configuration is used and every height in
that model is marked as an assumption.

**Doors and windows exist in two places.** A mark on a drawing and a row in a
schedule. Both are kept: a mark with no schedule row and a scheduled item drawn
on no sheet are each reported, neither dropped and neither invented. Where a
plan prints no marks at all, openings are measured from the breaks in the walls
instead, and each says which way it was found.

**A sheet drawn as a picture is still read.** Some plan sets store the drawing
as an embedded image rather than as line work. Those sheets have their wall
lines traced from the page image, and every measurement records whether it came
from the drawing's own lines or from the page as a picture.

**A scanned sheet is read by character recognition.** Only pages with no text
of their own — a page that already has text is never re-read, whatever is drawn
behind it. Each upload has a fixed budget for recognition; past it, remaining
sheets are read from whatever text they have and say so.

---

## Configuring it for a different office

Everything an office might legitimately differ on lives in
`config/plan_reading.json` — room names, title-block labels, drawing-type
words, wall thicknesses, opening mark prefixes, every tolerance, the resolution
sheets are rendered at, and the colours used on the marked-up sheets.

Editing an entry there retunes the reader without touching any code. If a plan
uses wording that is not in the configuration, those values are reported as
*not found* rather than guessed — which is both the intended behaviour and the
signal that an entry is missing.

`config/version.json` holds what the release claims about itself and its known
limitations. The interface reads both from these files; nothing about them is
written into a screen.

---

## Where things are kept

```
input/          plan PDFs, untouched
config/         settings and the release description
output/plan/    the current run: source copy, page images, marked-up sheets,
                every table, the issues log and the 3D model
logs/           what happened, per run
docs/           this file and the supporting documents
tests/          automated tests and the optional checking sheet
backend/        the reader and the API
frontend/       the browser interface
```

**One plan at a time.** The interface shows the plan just uploaded and nothing
else — no upload history and no run browser. Uploading another plan, refreshing
the page or closing it discards the previous run's folder, so what is on disk
matches what is on screen.

**Nobody else can see your plan.** Every browser gets its own anonymous
session, and every file is served through a route that checks it. One session
can never read another's plan, even knowing its identifier.

---

## Measuring how accurate it is

The screen can only ever show what *was* found — it can never show what was
missed. To turn "that looks right" into a measured number, a sheet has to be
written out by hand and compared.

Every run writes a **checking sheet** into its own folder, pre-filled with what
the reader read and where on the sheet to look for it. Answering it is one
question per row — *is this right, yes or no?* — and where the answer is no,
what the drawing actually says. Save the answered file as
`tests/ground_truth.csv` and the next run reports recall, precision and wall
variance against it.

**This is entirely optional.** The tool needs nothing from it. A row nobody has
confirmed counts for nothing, because comparing a run against numbers taken
from that same run would always score perfectly and prove nothing. One checking
sheet describes one plan, and a checking sheet written for a different plan is
refused rather than scored.

---

## Running the tests

From `backend/`:

```
venv\Scripts\python.exe -m pytest tests/ -q
```

Every test names the mistake it prevents. Most were written from a real failure
measured against a real plan set, so a change that reintroduces one fails here
rather than in a run nobody checks.

---

## Deploying it

The interface and the API deploy to two different places: the interface to
Vercel, and the API to a container host. The API cannot run on Vercel — it is
over a gigabyte installed, writes each run's files to disk, and takes twenty to
forty seconds on a large plan set, none of which a serverless function does.

`docs/deployment.md` has the full walk-through, including what to do when the
uploads hang (almost always one of two settings).

---

## Important — read before relying on anything here

**This tool produces drafts for review, not construction information.**

- Everything it produces must be checked by a **qualified Australian
  construction professional** before it is used for any purpose.
- **No certification, code compliance, engineering approval or structural
  adequacy** is claimed or implied by anything the tool outputs.
- Wall lines are **candidates** for review. They have not been confirmed
  against the drawing by anyone.
- Where the tool could not establish a value it says so. A value it reports as
  *not found* has not been quietly filled in — but a value it reports with low
  confidence still needs checking against the sheet.
- Read the **known limitations** in the interface, under *About this tool*,
  before quoting any figure from it.

Open **About this tool** in the interface at any time for the current version
and the full list of limitations, read from the product's own configuration.
