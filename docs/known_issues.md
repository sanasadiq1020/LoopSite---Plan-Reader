# Known issues and open work

Honest record of what this build does not do well, and what is left. The
interface shows the reader-facing version of this under **About this tool**,
read from `config/version.json`.

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

**Room and title-block vocabulary is configurable, not universal.** Wording
outside `config/plan_reading.json` is reported as not found rather than
guessed. That is the intended behaviour, and it is also the signal that a
configuration entry is missing for that office.

**Character recognition is slow and imperfect.** Sheets with no text layer are
read by OCR at roughly a few minutes per dense sheet on a CPU. Each upload has
a fixed budget; past it, remaining sheets are read from whatever text they have
and say so. Observed OCR errors are character-level — a lost apostrophe, a
lost space, a dropped digit — and they surface as ordinary records rather than
being silently corrected. Making large scanned sets practical needs a faster
recognition path and is separate work.

---

## Limits of the model

**Walls only.** Doors and windows are carried in the canonical model with the
wall they belong to, but are not yet cut as openings in the 3D geometry.

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

- Cutting door and window openings into the 3D walls
- Generated 2D elevations from the model
- Material take-off, cost estimate and crew work packages
- Multi-storey assembly
- A packaged single-command install or container image

---

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
