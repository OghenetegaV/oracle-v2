# Oracle v2 — Phase 3: Architectural Drawing Intelligence Layer

Status: implemented and tested. Written for schema 0.3.0; **[PHASE_3_5_HARDENING.md](PHASE_3_5_HARDENING.md) supersedes it where they differ** (schema 0.4.0:
multiple sources, structured resolution, typed evidence links, `trace()`, the approved projection, renamed observation kinds `column_symbol`/`beam_symbol` and hints
`column_candidate`/`beam_candidate`, `SRC-n` ids, a generic level model). Not connected to the legacy wizard. Read
[CORE_EVIDENCE_MODEL.md](CORE_EVIDENCE_MODEL.md) first: this phase is built on its provenance, value-status,
interpretation-set, decision and readiness machinery and adds nothing that bypasses it.

**What this layer answers:** *what does this drawing appear to contain, and what does Oracle know versus infer versus
not know?* It does **not** design anything. It never creates structural elements. It does not even create the
building's levels: an engineer does that, through a decision, after reading the interpretation.

```
DWG / DXF
  -> CAD extraction            oracle.ingestion         (ezdxf, ODA File Converter)   -> DrawingDocument
  -> drawing entities          neutral, JSON-cacheable, source coordinates untouched
  -> spatial / grouping        oracle.interpretation.segmentation
  -> architectural reading     views, levels, sections, elevations, units, layers, observations
  -> alternatives              every real ambiguity becomes an InterpretationSet + an EngineeringIssue
  -> engineer review           accept / reject / modify / merge / split / rename / supply / override (decisions)
  -> approved BuildingModel    (established by an engineer decision; NOT done automatically)
  -> structural reasoning      (later phase)
```

## 1. Architecture

| Package | Role |
|---|---|
| `oracle/core/architecture.py` (new) | The data model: `DrawingSource`, `UnitEstimate`, `CoordinateFrame`, `LayerClassification`, `DrawingView`, `ArchitecturalObservation`, `HeightEvidence`, `CrossViewFinding`, and the container `ArchitecturalInterpretation`. Standard library only: **the core imports no CAD library, AI client or plotting library** (tested in a subprocess). |
| `oracle/ingestion/` (new) | Reads a DWG/DXF into a neutral `DrawingDocument`. The only place ezdxf is used. |
| `oracle/interpretation/` (new) | The interpretation itself: `naming`, `layers`, `units`, `segmentation`, `vertical`, `observations`, `alignment`, `reconcile`, the orchestrating `pipeline`, `advisor` (bounded AI hook), `report`, and the `__main__` CLI. |
| `oracle/core/project.py` (extended) | Holds one `ArchitecturalInterpretation`; `review_views`, `merge_views`, `split_view`; `set_value` now also changes architectural objects; readiness gains the *unreviewed view* blocker. |

Design rules that hold throughout:

1. **Evidence first.** Every derived object gets a `ProvenanceRecord` (method, producer, weight, the source entity handle,
   layer, coordinates) and a `ValueStatusRecord` (`source` / `inferred` / `derived` / `assumed`). Tested: every view,
   observation and layer classification has provenance.
2. **Never silently choose.** Where the evidence is close or the sources disagree the result is an `InterpretationSet`
   (open, with ranked alternatives) **and** an `EngineeringIssue`, and it blocks readiness until an engineer decides.
3. **Never invent.** No storey height without a written number or a labelled line; no units without corroboration; no
   level for a plan whose title does not say; no alignment when it is not clear; no `BuildingModel` at all.
4. **Original geometry is never modified.** Source coordinates are kept as drawn; the pipeline works on a copy and
   the tests compare the document before and after.
5. **Observations, not structure.** Kinds are architectural (`wall_external`, `door`, `stair`, ...). Anything a structural
   engineer might care about is a `HintKind` (`column_candidate`, `stair_opening`, `double_height`, ...) *with evidence*, and
   explicitly "not a decision".

## 2. Data flow

`interpret_file(path)` / `interpret_document(document)` in `pipeline.py` run these stages in order; each stage's
output is recorded, not just passed on:

1. read the drawing (hash the original, convert if DWG) → `DrawingDocument`
2. detect units → `UnitEstimate` + basis + conflicts
3. classify layers → `LayerClassification` per layer
4. find sheet frames and segment the remaining geometry into regions; attach titles; score and classify each region → views
5. per view: evidence, level (plans), rotation (view frame), observations, level tags and heights (sections/elevations),
   floor-level text (plans)
6. align plans that carry levels to each other (grid labels first, then footprints)
7. reconcile across views → findings, interpretation sets, issues
8. write everything into an `OracleProject` in a fixed order (provenance → value status → sets → findings → issues)

The result is an ordinary `OracleProject` (schema 0.3.0) that saves and loads like any other, plus a plain-text report
(`render_report`). The stages are separate modules that can be tested and replaced on their own.

## 3. Ingestion (`oracle/ingestion/`)

* `read_drawing(path)` accepts `.dxf` and `.dwg`. A DWG is converted by the ODA File Converter into a **temporary**
  directory (never beside the original); the SHA-256 recorded is that of the original DWG, so a project can prove which
  file it came from. A damaged DXF is retried with ezdxf's recovery, and the repair is a warning on the document.
* Entities become `DrawnEntity` (id, kind, layer, bounding box, points, closed, text, block, radius, value, rotation,
  height, space). Kinds: LINE, POLYLINE (LWPOLYLINE/POLYLINE), CIRCLE, ARC, TEXT (TEXT/MTEXT), DIMENSION, INSERT, HATCH,
  SPLINE. **Blocks are not expanded**: an INSERT is kept as a placed footprint with its block name (doors, windows and
  columns are usually blocks). Anything that cannot be read is counted in `skipped` and reported in `warnings`, never dropped
  quietly. Arcs are boxed by the part actually drawn (a full-circle box once bridged every view of the real drawing).
* `DrawingDocument` round-trips through JSON, so a 24,000-entity drawing (~40 s to read) can be cached and interpreted again in
  about a second.
* Nothing here interprets. Failures raise `IngestionError` with a message an engineer can act on (missing file, wrong
  extension, not a drawing, no converter).

## 4. View detection (`segmentation.py`)

Views are found **spatially, not by layer** (a test builds the same drawing with unrecognisable layer names and gets the
same regions).

1. **Sheet frames.** Large closed axis-aligned rectangles that contain many entities and are at least 15× the median line
   length are frames (title-block borders). They become `title_block` views and are excluded from clustering so a border does
   not weld every view on the sheet together.
2. **Clustering.** Geometry (not text or dimensions) is grouped by bounding-box overlap at a tolerance of 0.05 % of the drawing's
   diagonal (a sweep line with union-find). A cluster is significant with at least `max(10, 0.2 %)` of the geometry entities.
   Small clusters within 4 % of a region's size join it; regions overlapping by ≥ 30 % of the smaller are merged (nested or
   interleaved parts). All thresholds are relative to the drawing, none to a unit.
3. **Titles.** Text inside a region belongs to it. A title is a short text (≤ 8 words, ≤ 60 characters) that names a view type;
   it is assigned by proximity, preferring text **below** a view; text *inside* a view counts only if much larger than the ordinary
   text (so a note sentence containing "detail" is not a title). A second title-like text becomes evidence, not a second view.
4. **Classification** scores each region for floor plan / section / elevation / detail / schedule / notes / legend from three independent
   kinds of evidence: the title (0.65), the semantic classes of the layers its geometry sits on (walls, doors and windows → plan), and
   geometry (level-tag texts, mostly vertical dimensions → section; text share → schedule). `confidence = top − 0.5·second + 0.03`.
   When the top two are close the region gets **alternatives** and an interpretation set. Variants of a plan (blow-up, furniture,
   dimension, site) are recorded as `variant` and are *not* rival plans.
5. Every score comes with its evidence list; each view records `entity_ids`, `bbox` (source units), confidence, title, level,
   review status and frame.

Multi-plan sheets work (scenarios 2 and 5). On the real 20-sheet drawing the segmentation recovered 29 view regions and 6 frames.

## 5. Floor (level) detection

`naming.LevelNamer` normalises level names and is configurable; nothing is hard-coded to one country or one office:

* Recognised without configuration: `GF`, `G/F PLAN`, `GROUND FLR`, `GROUND FLOOR`, `LEVEL 00`, `L00`, `L-00`, `FF`, `FIRST FLOOR`,
  `LEVEL 01`, `1ST FLOOR`, `2/F`, `R/F`, `FLOOR 3`, `ROOF`, `MEZZANINE`, `BASEMENT`, `LEVEL -1` (basement), `NGL` (datum).
  Room names and ordinary words are not levels (`CHANGING ROOM`, `STORE`, `AVOID`, `ROOM`).
* **Aliases** (`InterpretationConfig.level_aliases`) map any phrase to a level key ("PODIUM" → first floor; "MAIN HALL" → ground) and
  win over the defaults. **`numeric_offset`** handles conventions where level 1 is the ground floor.
* A typo is tolerated only for words of five or more letters (one edit; four-letter words only transposed letters), always with lower
  confidence and marked `fuzzy` ("GRUOND"). "ROOM" is never "ROOF".
* A title naming **two** levels ("FIRST & SECOND FLOOR PLAN") gets no level and an interpretation set with both readings.
* Only a floor plan may carry a `level_key` (validated by the core). The vocabulary is `DATUM`, `GROUND`, `FLOOR:n`, `BASEMENT:n`,
  `MEZZANINE`, `ROOF`, `PENTHOUSE`; display names and BuildingModel ids (`GF`, `L1`, `B1`) are derived from it.
* A plan whose title names no level is reported ("does not name its level") and left unassigned. Plans are not merged by level:
  two primary plans claiming one level, or a missing intermediate floor, are reported.

## 6. Section detection and vertical evidence (`vertical.py`)

A section is a section because its title says so *and* its content looks like one (level tags, vertical dimensions), not because of a layer.

* **Level tags.** A level-name text is paired with the elevation number beside it. On a tightly packed tag stack the nearest number
  is often the neighbour's, so Oracle finds the **dominant name→number offset** in the view (the commonest displacement, preferring the
  same layer) and pairs against that. Numbers are read as millimetres unless written in metres (`3.3`, `3.3m`).
* **Heights.** The difference between two written elevations is a **SOURCE** height (`level_tags`, both ends are text). A vertical
  dimension that spans two labelled level lines corroborates it (`section_dimension`, SOURCE). A height taken only from drawn line
  spacing is **INFERRED** (`section_lines`) and is used only when there are no written numbers.
* **Written numbers beat drawn spacing.** On the real drawing the tag stack is a cosmetic column (drawn 1957/1493/1598 apart for
  elevations 300/3150/900 apart): Oracle uses the numbers and records "the drawn spacing does not follow the written elevations".
* **Full lines vs partial lines.** Long horizontal lines are grouped by height; coverage of the view's width ≥ 70 % is a level line, less
  is a `partial_floor_line` observation carrying the hint `double_height` (a mezzanine, a void or a gallery).
* **No height is ever invented.** With no numbers and no labelled lines the section yields no height and a warning; for plan levels with
  no height path between them the issue reads *"Storey height for Ground Floor → First Floor could not be reliably established from the
  available architectural drawing. It is not assumed; the engineer must supply it."*

## 7. Elevation detection

Elevations are titled ("FRONT ELEVATION", "LEFT SIDE ELEVATION", ...; orientation recorded) and are **another evidence source, never plans**:
they carry no level, contribute level tags and written heights (a little less weight than a section, `0.75` vs `0.85`, because they are more often
schematic), and their level count is compared with the plans' and sections' in reconciliation. Plan-only observations (doors, columns) are not
produced from an elevation.

## 8. Layer semantic classification (`layers.py`)

Method, in order: **configured alias** (0.99) → **tokens** of the name (any separator, camelCase, e.g. `A-WALL-EXT`, `Walls-Ext`, `external walls`)
→ **discipline prefix** (A architecture, S structure, I interiors, G general, Q equipment, ...) → **qualifiers** (ext/int/finish/pattern/label/hidden)
→ **geometry** (a layer of only text is an annotation layer) → **block names** on the layer (weak, capped below 0.8).

* `A-WALL-EXT` → `wall_external` (0.95); `A-WALL-INT` → `wall_internal`; `A-FLOR-LEVL` → `level_marker` (a refinement, not a conflict).
* `WALL-FINISH-02` → **`unknown_wall_related`, 0.52**: a wall word with a finish qualifier is not evidence of a structural wall.
* A discipline that disagrees with the word (`S-STRS`: a structural-discipline layer named for stairs) lowers confidence and says so in its note.
* An unrecognised name is `unknown` with confidence 0.0, is reported as an issue, and **its content is not used** (no observations are
  invented from it). On the real drawing all 54 layers were classified except one (`Q-SPCQ`, no recognisable word).
* This list is a starting vocabulary, not a standard. Offices extend it with `LayerConfig(aliases=...)`; the semantic classes are validated
  words, not an enum, so new conventions need no schema change.

## 9. Unit detection (`units.py`)

Canonical unit: **millimetres**; `factor_to_mm` is stored with every estimate and every source-unit value keeps its source unit.

Evidence combined: the file's `$INSUNITS`, stated-versus-drawn dimension agreement, median stated dimension, median drawn line, and overall
extent, each scored for plausibility against each candidate unit (mm, cm, m, inch, foot) with a prior.

* Metadata **corroborated** by the drawing → `SOURCE`, confidence ≥ 0.9.
* Metadata the drawing **contradicts** → an `ERROR` issue and an interpretation set ("What unit is the drawing in?") with the alternatives; the
  provisional unit is `ASSUMED`, confidence ≤ 0.6. **This happens on the real drawing** (file says inches; it is millimetres).
* **No** metadata → `ASSUMED`, confidence ≤ 0.75, same issue and set. Oracle never uses a guessed unit silently.
* An assumed value blocks readiness (existing rule).

## 10. Coordinate transformations

Three frames, each mapping its own coordinates to its parent's (translation, rotation, scale, mirror):

```
source drawing (FRM-0)  <-  view-local frame (FRM-nn)  <-  building alignment frame (FRM-mm)
```

* **Source coordinates are never altered.** A view's bbox and every entity/observation geometry stay in source coordinates.
* The **view frame** rotates the view so its dominant wall direction is axis-aligned (estimated from wall-class line directions, else all lines) and puts
  the footprint's minimum corner at the origin. A rotated plan reports its angle (an info issue). Far-from-origin plans (e.g. 1,250,000 / 4,800,000)
  are carried in the frame translation and round-trip to 1e-6 (tested).
* The **alignment frame** is the proposed translation of that view into the building. Plans are aligned to the lowest full plan by **grid-label voting**
  (≥ 3 shared labels; the translation most labels agree on) first, then by footprint (equal footprint, or min-corner / centre / max-corner). Grid labels and
  equal footprints that say the same thing corroborate each other (0.98); footprints of *different* sizes give one 0.5 candidate and two weaker ones.
* **Weak alignment is not merged.** Unless the best candidate is ≥ 0.75 and every other < 0.3, no alignment frame is set; the alternatives become an
  interpretation set and a warning. A **mirrored** plan is not silently merged either (its grid labels disagree), which is the tested behaviour.
* `ArchitecturalInterpretation.to_source(frame, point)` / `from_source(frame, point)` convert through the chain. The engineer supplies or confirms an
  alignment with `align_view(project, view_id, translation, engineer=..., reason=...)`, which records one accepted engineer decision, creates the frame,
  sets the view's field through `set_value`, and resolves the interpretation set.

## 11. Cross-view reconciliation (`reconcile.py`)

| Check | Result on disagreement |
|---|---|
| Number of levels: plans vs sections vs elevations | set "How many structural levels exist, and which are they?" (one alternative per source with its confidence) + ERROR issue + finding |
| One level tagged with several elevations (e.g. FIRST FLOOR 3450 and 4350) | set "one level or several?" (0.7 / 0.3) + WARNING |
| Storey heights from different sources (plan text 3300 vs section 3600) | set with **both** heights (each alternative names its sources) + ERROR + finding; tolerance `max(50 mm, 2 %)` |
| Two primary plans for one level; a missing intermediate floor | set / WARNING |
| Duplicate section or elevation titles | WARNING |
| Drawing's own sheet list vs the views found | finding (agree / disagree) + info issue |

Agreements are recorded as `agree` findings without issues. Nothing is resolved by Oracle: `suggest_elevations(project)` returns level elevations only when
**no** interpretation set is open and every consecutive pair has consistent height evidence, and otherwise `None`. Turning them into levels is
`establish_levels(project, elevations, decision)`, which needs an accepted engineer decision.

## 12. Provenance

Each object has one or more `ProvenanceRecord`s (`producer = "oracle.interpretation"`, a method such as `spatial_cluster`, `title_text`, `layer_class`,
`level_tags`, `sheet_frame`, `unit_detection`), with the file, the entity handle where there is one, layer, entity type, coordinates and a weight. Value statuses
carry the provenance ids. Interpretation sets cite the provenance of their subject views. Issues cite evidence. `OracleProject.validate()` checks every reference.
The evidence chain is preserved end to end: **source fact → interpretation → engineer decision → (later) design result**.

Schema `0.3.0` adds the `ARCHITECTURAL` target scope so provenance, statuses, decisions and issues can point at views, layers, observations, heights and the drawing itself.

## 13. AI involvement

Deliberately minimal and bounded (`advisor.py`). Oracle's own reading is deterministic and uses no AI service. An optional `LayerAdvisor`
(any object with `advise(layer_name, sample_entities)`) is consulted only for layers the rules left below 0.6 confidence. Its answer is recorded **only** as a
`PROPOSED` `EngineeringDecision` with source `AI_ASSISTANT` naming the layer, the field `semantic_class`, the proposed value and its reasoning. The classification
does not change; observations are not created from it. The engineer accepts or overrides it through `set_value(..., responds_to=<advice id>)`, after which the value is
`ENGINEER_OVERRIDE`/`ENGINEER_DEFINED` with the AI's recommendation in the decision history. The module makes no network call and imports no AI client;
a Claude-backed advisor is a separate component to be written. The core cannot be modified by the advisor (a decision from `AI_ASSISTANT` can only be `PROPOSED`, enforced in the core and tested).
Not yet implemented: AI advice about titles, level names or alternative interpretations (same pattern).

## 14. Engineer intervention

Every action is an accepted `ENGINEER` decision recorded in the existing decision system; refused actions change nothing (tested).

| Engineer wants to | How |
|---|---|
| accept / reject views | `project.review_views(ids, decision, accept=True/False)` (one decision covers all; unreviewed plans, sections and elevations block readiness) |
| modify / rename / supply missing info / override | `project.set_value(Target.architectural(id), field, value, decision)` (e.g. a plan's `level_key`, `title`, a layer's `semantic_class`); becomes `ENGINEER_DEFINED`, or `ENGINEER_OVERRIDE` with `replaces` if Oracle had a value; previous value and superseded decisions kept |
| merge views | `project.merge_views(ids, new_id, decision)` (same type, no conflicting levels; originals kept, superseded; observations move) |
| split a view | `project.split_view(id, parts, decision)` (parts must cover the original's entities exactly once) |
| choose between alternatives | `project.accept_interpretation(set_id, interpretation_id, decision_id)` (siblings are rejected under the same decision) |
| align plans | `align_view(...)` |
| create the levels | `establish_levels(project, elevations, decision)` (elevations `ENGINEER_DEFINED`; generated display names stay `ASSUMED`) |

A recommendation from Oracle or the AI can never make any of these changes (tested).

## 15. Known limitations

* **Not 100 % accurate, and not claimed to be.** It is a proposal engine. On the real sample two views are untitled plans, three regions remain `unknown`, two
  blow-ups are ambiguous, and several regions have confidence below 0.7.
* (Phase 3 limitation; since Phase 3.5 a project holds several sources.) **Paper-space layouts and viewports are read but not segmented** (model space only); a drawing that is only a layout is not interpreted yet.
* Blocks are not expanded; an INSERT is its footprint. Block *contents* (a door's swing, a stair's treads) are not analysed. Xrefs are not followed.
* Mirroring is *not detected* (a mirrored plan is left unaligned instead). Non-90° building geometry (a fan-shaped or curved plan) gives a low-support rotation.
* Title recognition is English keywords (plus fuzzy and configured aliases); non-English titles need aliases. Elevation orientation words are a fixed list.
* Levels within a plan sheet are assumed to be one plan per view region; a plan cut into several disconnected pieces is split (merge them with `merge_views`).
* Dimensions are read as measured values; unusual dimension styles or overridden text may mislead the units and section-height checks.
* Tag pairing assumes the elevation number sits within a few text heights of the name. Exotic level markers (symbols with the value inside a block attribute) are not read.
* Wall thickness, room areas, openings' sizes and real stair geometry are not derived. Observations are aggregated linework plus individually placed doors, windows and columns.
* Performance: reading is pure Python (about 40 s for 24,000 entities); the interpretation is about a second. Region clustering is O(n log n) but the frame search is O(frames × entities).
* Level keys cover typical buildings; split levels, multiple basements above `BASEMENT:9`, and named levels without an alias are not represented.
* `test_generate_test_dwg` (existing) imports `config.py`, which prints a check mark; on a Windows console that is not UTF-8 it fails at import. Run the tests with `PYTHONIOENCODING=utf-8` if that occurs. (Pre-existing; not changed.)

## 16. Interface for future structural reasoning

Structural reasoning should consume, not re-derive:

* `project.architecture.views` (accepted ones), their `level_key`, `alignment_frame_id` and the frame chain to convert any observation to building coordinates;
* `observations` with `hint`s (`column_candidate`, `beam_candidate`, `stair_opening`, `lift_shaft`, `large_opening`, `double_height`), each with provenance;
* `heights` (with `basis` and sources) and, once an engineer has decided, the `BuildingModel` levels created by `establish_levels`;
* `project.readiness()` as the gate: nothing structural should run while any view is unreviewed, any interpretation set is open, or any value is assumed.

What it must **not** do: treat an observation as a structural member, or a hint as a decision. The natural next step (see the final report) is a *structural
intent* layer that lets an engineer accept a hint ("this column is existing and retained") as a recorded decision, and then a support/continuity/transfer model.

## Schema change and migration

`SCHEMA_VERSION` 0.2.0 → **0.3.0**. Added: the optional top-level `architecture` section (null when a project has no drawing interpretation), the `architectural`
target scope, and the `unreviewed_view` readiness blocker. Why: Phase 3 needs to store a drawing interpretation inside the project so it can carry provenance,
decisions and readiness like everything else, rather than in a side file the decision system cannot see.

`0.2.0` files load: the migration `0.2.0 → 0.3.0` adds `architecture: null` and nothing else (it refuses a `0.2.0` file that already has the key; unknown fields are still
rejected). `0.1.0` files migrate through `0.2.0` first. Verified against a genuine `0.2.0` file written by the pre-Phase-3 code
(`tests/fixtures/schema_0_2_0_project.json`) and the genuine `0.1.0` file. The existing migration assertions that literally named the current version (`0.2.0`, in six places
in `tests/test_migration_readiness.py`) now say `0.3.0`, the list of registered migrations gained `0.2.0`, and the example "future" version `0.3.0` became `0.4.0`;
no test was weakened or removed. Old code cannot read `0.3.0` files.

## Tests

| File | Covers |
|---|---|
| `tests/test_interpretation_scenarios.py` | the 15 scenarios of the brief, plus determinism, JSON round-trip and shuffled-entity-order checks (62 tests) |
| `tests/test_engineer_review.py` | accept/reject/rename/modify/override/merge/split/align/establish levels, AI-bounded advice, persistence |
| `tests/test_architecture_core.py` | validation, frames, strict round-trip, project registry, readiness blocker, core independence |
| `tests/test_naming_and_layers.py` | level naming conventions, aliases, titles, layer classification |
| `tests/test_ingestion_and_units.py` | DXF/DWG reading, arcs, refusals, unit detection |
| `tests/test_view_analysis.py` | segmentation, level tags, level lines, alignment, reconciliation |
| `tests/test_real_architectural_drawing.py` | the real squash-court drawing (tier `slow`: run with `python -m tests slow`; skipped without the file/ODA) |
| `tests/drawing_factory.py` | builds the synthetic DXFs with known content |
| `tests/test_migration_readiness.py` | extended with the 0.2.0 → 0.3.0 migration |

Run: `python -m unittest discover -s tests -t .` (everyday tiers) or `python -m tests all` (with the real drawing); see [TESTING.md](TESTING.md).

## The real drawing

`python -m oracle.interpretation "input_dwgs/Sample Architectural Drawings - Proposed Squash Court Extension.dwg" --engineer "Name" --project out.oracle.json --report out.txt`
(reads the file only; never writes beside it). It is a Revit sheet set of 20 sheets, 24,159 model entities, 54 layers. What Oracle reported:

* **Views:** 35 (ground / first / roof plans with blow-ups and furniture plans, sections A-A, B-B, C-C, six elevations, a sheet list, a symbols legend, six title-block frames; 3 unknown).
* **Levels/heights:** ground → first 3150 mm (SOURCE, nine pieces of evidence), first → roof 2100 mm, natural ground → ground 300 mm; and it **reported** that first floor is tagged 3450 *and* 4350,
  roof 6450 *and* 7950, that a section dimension gives natural ground → first as 3150 and 3750, and that three plans show three levels while sections/elevations show five level tags.
* **Units:** file says inches, drawing is millimetres → ERROR + open question, mm provisional.
* **Not established** (correctly): plan alignment for the first and roof plans (only footprint evidence), the levels themselves, anything structural.
* 20 issues (3 errors, 10 warnings, 7 info), 11 open interpretation sets, 112 structural hints (47 existing columns, 56 double-height indications, 7 stair openings, 2 beams). The project is **not ready**.

These need an engineer, not code.
