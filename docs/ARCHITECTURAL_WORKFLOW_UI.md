# Architectural Drawing workflow: the interface

Interface phase (after Phase 3.5, before Phase 4). It exposes what Oracle can already do with an architectural DWG/DXF
through the existing Tkinter wizard. It adds **no new interpretation** and **no structural intent**.

## How to open it

- Double-click **Launch Oracle.bat** (or `pythonw oracle_wizard.py`). On the welcome screen press
  **"Architectural Drawing: read and review a DWG/DXF..."** (below the list of the eight structural steps). The structural
  workflow (**Get started**) is unchanged.
- To open straight into the workspace: `python oracle_wizard.py --architectural` (or `pythonw oracle_wizard.py --architectural`).

## Importing another drawing

1. Open Oracle and press **Architectural Drawing...** on the welcome screen.
2. Press **Browse for a drawing...** and choose any `.dwg` or `.dxf`. The name, type, size and full path are shown, with
   "Oracle can read this file" or the reason it cannot (missing, empty, not a DWG/DXF, DWG without the ODA File Converter, a DXF
   saved under a `.dwg` name...).
3. Check the engineer name (recorded on every decision) and press **Interpret drawing**. Each stage is listed and ticked only when
   it has really finished. The window stays responsive.
4. Review the result. To interpret a different drawing later press **Drawings...**; to bring in a further drawing (another revision or
   discipline) into the same project press **Add drawing...**. A project with several sources makes you choose which one to review
   (**Source** box); Oracle never picks one for you.
5. **Save project** writes `<name>.oracle.json` (and a `<name>.oracle.geometry.json.gz` beside it holding the drawing linework so the
   preview works after reopening). **Open project...** reloads it without interpreting anything again.

Your drawing file is only ever read.

## Layers

```
oracle_wizard.py            the wizard: one button + open/close of the workspace (lazy import)
oracle/ui/                  Tkinter only: workspace, review tabs, canvas, dialogs, theme
oracle/application/         no toolkit, no CAD library: session, file checks, read models, preview model
oracle/interpretation/      Phase 3/3.5 pipeline (unchanged; gained only an optional progress callback)
oracle/core/                the domain model
```

- The UI holds **no model of its own**: `ArchitecturalSession.project` is the only state. A screen is a function of it and is
  redrawn from it after every action.
- `oracle.ui` imports `oracle.application` only (no `oracle.core`, `oracle.interpretation`, `oracle.ingestion`, no CAD library);
  nothing below imports `oracle.ui`. Both are enforced by tests.
- The drawing preview draws the neutral `DrawingDocument` that ingestion already produced; there is no second DXF parser.
- Interpretation runs on a worker thread; results reach the interface through a queue polled by `after`.

## What the review screen shows

The banner is derived from the project's readiness (`project.readiness()`): *Engineer review required*, *Blocked* or *Ready for the
next phase*. Tabs: **Summary**, **Views**, **Levels** (detected by Oracle vs established by the engineer), **Observations** (by
category, including column and beam candidates), **Questions** (unresolved interpretations with alternatives, confidence, evidence,
consequences of each answer), **Issues**, **Approved** (`project.approved_architecture()`: engineer-approved only, separate from
what is proposed or unresolved), **Decisions** (the recorded engineer decisions). The drawing preview supports fit, zoom, pan and
highlights the selected view, observation, question or issue on the linework.

Wording: Oracle **detected / proposes**; *Engineer review required*; *Engineer approved*; *Unresolved*.

## Engineer actions (all recorded as engineer decisions)

Approve / reject views, observations and proposed readings; accept or reject a reading of an unresolved question (the structured
effect of the reading is applied and the linked issue resolved by the domain); rename a view or set its level; establish levels
from elevations you confirm (Oracle may suggest, and says so; a finished-floor level is never labelled structural); rename a level
or set its elevation / structural elevation (levels above may move; the domain records each consequence); align a plan; merge
views; split a view; accept an issue with a reason (accepted, not fixed). A refused action is shown in words and changes nothing.

## Errors

A bad file is refused on the first screen with the reason. A failure during interpretation shows a plain message, the file name
and **Try again** / **Choose another drawing**; the technical trace goes to `logs/oracle.log` through `oracle_log.log_event`
(`architectural.*` events). No traceback is shown to the engineer.

## Limitations

- The preview is a viewer of the drawing's linework: no layers panel, no measurement, no printing. Very dense drawings are drawn at
  reduced detail while panning (the status line says how many entities are drawn).
- Text is drawn only when zoomed in.
- Alignments and splits are entered as numbers (drawing units); there is no on-canvas picking tool yet.
- Undo is not offered: a decision is corrected by a later decision (the history is kept).
- The workspace uses the wizard window (resized while open). It is not yet a separate top-level application.
- Nothing structural is inferred: no columns, beams or loads are created from observations (Phase 4).
