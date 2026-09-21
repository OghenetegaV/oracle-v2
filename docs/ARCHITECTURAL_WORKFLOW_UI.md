# Architectural Drawing Review: the interface

Interface phase, refined twice (a calmer review, then a structural-engineering view with guided drawing tools). It exposes what Oracle can
already do with an architectural DWG/DXF through the existing Tkinter wizard. It adds **no structural intent**: no beams, columns, slabs,
loads or supports are created from a drawing.

## How to open it

- Double-click **Launch Oracle.bat** (or `pythonw oracle_wizard.py`). On the welcome step press **"Architectural Drawing Review..."**.
  The structural workflow (**Get started**) is unchanged.
- To open straight into the workspace: `python oracle_wizard.py --architectural`.

## Importing another drawing

1. Press **Open Architectural Drawing**, then **Browse for drawing...** and choose any `.dwg` or `.dxf`. Name, type and "Ready to interpret" are
   shown; path, size and the engineer name (recorded on every decision) are under **Details**.
2. Press **Interpret Drawing**. Each stage is ticked only when it has really finished.
3. Review. **Project ▾** offers Save, Open, *Add another drawing* (a revision, say) and *Choose a different drawing*. With several drawings in
   a project Oracle asks which to review; it never picks one.
4. **Save project** writes `<name>.oracle.json` (plus a `.geometry.json.gz` so the preview works after reopening). **Open project** reloads it
   without interpreting again. The drawing file is only ever read.

## The review screen

The drawing is the centre. Beside it: three status lines, a **review queue** (what needs you; completed items below), a **Views** list
(Needs review / Accepted / Rejected) and ONE action card with a single primary action. Ids, hashes, confidence numbers and provenance are not on
it; **Evidence & Details** opens the technical tabs (and the numeric Align/Split forms) for anyone who wants them.

Three voices are kept apart: **Oracle suggests** (amber; never worded as fact), **Engineer input** (blue) and **Engineer decision** (green).

### Structural Review and Original Drawing

The canvas has two read-only renderings:

- **Structural Review** (default) hides what only helps the architect present the building: furniture, casework, sanitary fixtures, equipment,
  services, ceilings, area/room fills, finishes and patterns, title blocks, non-plotting layers, generic symbols, and furniture *blocks* even on
  ordinary layers, plus general text (room names, notes). It keeps walls, doors and windows, stairs, grid lines with their bubbles and names, level
  names and elevations, dimensions, columns/beams/slab linework and other structural symbols.
- **Original Drawing** shows every entity.

This is a rendering choice driven by Oracle's own layer/block classification. Nothing is deleted from the drawing, the project or the provenance,
and neither view can change the source file. The canvas keeps a reserved, empty **structural overlay** layer (above the linework, below the
selection) so slab panels, slab openings, supports and span directions can be shown cleanly by a later phase; nothing generates them now.

### View Actions (per view)

The view card shows **Type** and **Status**, one primary action (Accept) and **View Actions ▾**:

- **Change View Type...** Plan / Section / Elevation / Detail / Other (the domain's own types; *Other* is `unknown`). The view is **kept**: same
  evidence, same review state. Its type changes by an engineer decision that records Oracle's original type (`previous_value`), so the history
  shows "Oracle read Plan; the engineer corrected it to Section". A section, elevation or detail has no floor level, so leaving *Plan* drops
  the level Oracle had read (and the decision says so).
- **Align Plan...** (plans only), **Split View...**, **Reject View...**, **Ask Engineer...**, **Review Evidence**.

**Change type is not Reject.** Reject says "this region should not take part"; the view stays on record, marked rejected with a reason category and
explanation, and questions that only concern it are closed. Change type says "this region is useful, Oracle classified it wrongly".

### Align Plan (point-picking)

1. *Step 1 of 2*: click a point on the plan being aligned (a grid crossing, column, building corner); the pointer is a crosshair that snaps to drawn
   corners and crossings, the plan is highlighted and a banner says what to click.
2. *Step 2 of 2*: click the same physical place on the reference plan (choose the reference in the panel).
3. **Preview alignment**: the plan's linework is drawn where the alignment would place it on the reference, with numbered markers. Nothing is
   recorded yet. Buttons: **Accept Alignment**, **Add second point pair (also turns the plan)**, **Pick Again**, **Cancel**.
4. A second pair fixes rotation as well as translation. **Scale is never changed**: if the distance between the two picked points differs
   by more than 2% between the plans, the preview says so.
5. **Accept Alignment** records one engineer decision (the alignment frame, with its rotation) through the ordinary mechanism; the picked points are
   kept in the decision's reason. Source coordinates are never modified; the alignment is a relationship between two views and survives save/load.

### Split View

Used when Oracle grouped one region as a single view but it really holds several (a sheet with a ground floor plan and a first floor plan). Choose a
vertical or horizontal dividing line, click where the view divides, see the two resulting parts with counts, then **Accept Split**. The original view
stays on record as split; the drawing is unchanged.

### Ask Engineer

Available next to Oracle's suggestions and in the View Actions menu. Free text, not limited to Oracle's readings. The words are kept verbatim as an
engineer clarification (author, time, target, decision, provenance) and flagged as guidance for the next stage; they never create model geometry.

## Layers

```
oracle_wizard.py            one button + open/close of the workspace (lazy import)
oracle/ui/                  Tkinter only: workspace, review screen, tools, canvas, dialogs, Evidence & Details
oracle/application/         no toolkit, no CAD library: session, guide, alignment geometry, preview and filters
oracle/interpretation/      Phase 3/3.5 pipeline (gained a progress callback and a rotation on align_view)
oracle/core/                the domain model (schema 0.5.0)
```

`oracle.ui` imports `oracle.application` only; nothing below imports `oracle.ui` (both enforced by tests).

## Limitations

- The structural filter follows Oracle's layer/block classification: on a drawing with unclassifiable layer names, some clutter may remain (use the
  Original view to compare) and a furnishing on an unrecognised layer is not hidden.
- Alignment picks are on the visible drawing; the reference plan's own alignment frame is used as the building frame.
- Split is by a straight vertical/horizontal line, not a drawn region.
- No undo: a decision is corrected by a later decision (the history is kept).
- Slab panels, supports and span directions are not created; only their overlay layer is reserved.
