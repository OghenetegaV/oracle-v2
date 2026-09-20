# Oracle v2 — Phase 3.5: Hardening the Architectural Boundary

Status: implemented and tested (schema **0.4.0**). This is NOT Phase 4: nothing here designs, sizes, generates or analyses
anything structural, nothing connects to STAAD, and the legacy workflow is untouched. It makes the boundary between drawing
interpretation and the future structural reasoning engine **clean, persistent and traceable**. The architectural model is
still a proposal and evidence model: Oracle interprets and proposes; only an engineer decision approves, rejects, overrides,
merges, splits, aligns or renames.

Read [PHASE_3_ARCHITECTURAL_INTERPRETATION.md](PHASE_3_ARCHITECTURAL_INTERPRETATION.md) and
[CORE_EVIDENCE_MODEL.md](CORE_EVIDENCE_MODEL.md) first. This document records what a review of the 415-test Phase 3 code found,
what was changed, and what was deliberately left alone.

## 1. What was wrong

| # | Finding | Consequence |
|---|---|---|
| 1 | `establish_levels()` recorded `storey_height_mm` as DERIVED without ever storing a height | a value status for a value that did not exist |
| 2 | `set_value()` could not change a level | an engineer could not confirm a generated level name; readiness could only be cleared through a workaround |
| 3 | Resolving an interpretation set only flipped a status | the model, the linked issue and the level suggestion all stayed as they were; the alternatives were free text (the alignment translation lived inside a rationale string) |
| 4 | A structural object had no typed link to the observation it rested on | traceability depended on a free-text `source_id` convention; nothing validated or walked it |
| 5 | No trace API | "which drawing entity is this from?" was unanswerable in code |
| 6 | No approved model for the structural side to consume | a consumer would have to read the interpretation, filter by review status, convert units and follow frame chains itself, and would meet layer names, entity handles and format codes |
| 7 | CAD vocabulary in the domain model (`unit_code`, `format_version`, `layouts`, the `DWG-` id prefix) and vendor logic in the interpreter (`Defpoints`, the `$INSUNITS` table) | the core "knew" what a DXF was |
| 8 | Observation kinds `existing_column` / `beam_shown` and hints `existing_column` / `existing_beam` | a symbol on a column layer was recorded as an existing column: a structural claim made by a drawing reader |
| 9 | A closed English-flavoured level vocabulary; one `elevation_mm` with no statement of what it was | "PODIUM" collapsed onto a floor number; a finished-floor elevation could pass for a structural one |
| 10 | One `architecture` slot per project | a second drawing, a structural GA or a revision could not be represented |

## 2. What was changed

### 2.1 The level value-status bug (Part 1.1)

`establish_levels()` now stores each level's real storey height (the gap to the next level) and gives it a DERIVED status; the
top level has none and therefore no status. The general rule, tested as an invariant, is **no value status exists for a
value that does not exist**. Everything survives save and load, and readiness counts only real assumptions (a level's
generated name is still ASSUMED and blocks until an engineer confirms it).

### 2.2 Engineer changes to levels (Part 1.2)

`set_value()` now applies to levels through the same checks and the same history as elements: the decision must name exactly
the target, field and value; the object is rebuilt and fully re-validated; the previous value goes into
`decision.previous_value`; earlier accepted decisions on the field are superseded; the field becomes ENGINEER_DEFINED or
ENGINEER_OVERRIDE; nothing changes if any step is refused. There is **no bypass for levels**. `_commit_engineer_value()` is the
single place this history is written, for every kind of object.

What is specific to levels is only that levels depend on each other (`oracle/core/level_changes.py`, read-only planning):

* changing a **storey height** moves every level above it (the height *is* the gap; the model refuses a height that disagrees
  with the gap), and each moved level is recorded as **its own engineer decision** ("Moved with D-x: ...") with its previous
  value, so the history of every elevation stays complete;
* changing an **elevation** recomputes the neighbours' *derived* storey heights (status DERIVED, note "was ...");
* an **engineer-set storey height is never recomputed silently**: if a change would make it disagree with the gap the change is
  refused and says which value to change first.

`BuildingModel.replace_levels()` swaps changed levels in as one validated step, so a change that moves several levels is atomic.

### 2.3 Structured resolution (Part 2)

`oracle/core/effects.py`: an alternative carries `effects`, each a strictly validated `Effect` (unknown kinds, missing and extra
parameters are rejected at construction and on load):

| Effect | Meaning of accepting it |
|---|---|
| `set_value {target, field, value}` | change a field of a level, element or architectural object (units, a view's level or type, a hint, ...) |
| `accept_height {from_level, to_level, height_mm}` | the storey height between two levels: engineer-defined height evidence that `suggest_elevations()` then uses |
| `align_view {view_id, translation}` | create the alignment frame and set the view's `alignment_frame_id` |
| `merge_views` / `split_view` | carried out by the ordinary review methods |
| `acknowledge {note?}` | the answer is recorded and the linked issue is answered; no model state changes |

`OracleProject.accept_interpretation()` (implemented in `oracle/core/resolution.py`) then: records the choice, rejects the
siblings under the same engineer decision, applies each effect **through the existing machinery** (a field change becomes a derived
engineer decision `<decision>.E<n>` applied with `set_value()`, so it has previous value, supersession and value status), records
what each effect did in `Interpretation.applied`, and resolves the issues linked to the set (`EngineeringIssue.interpretation_set_id`)
under the same decision, but only when the alternative actually carries an effect. It is **all or nothing**: it is first rehearsed on a
copy of the project, so a refusal anywhere means nothing at all changed. Rejecting an alternative applies no effect and touches no
issue. `apply_effects=False` records the choice only (used by `align_view()` when the engineer supplies their own translation).
On load, `check_resolutions()` re-verifies every resolved set against its payload, its derived decisions, the model, and its issues, so a payload,
decision or value edited by hand is rejected.

The pipeline now attaches effects to every set it proposes (units, view type, which level a plan shows, alignment, storey
heights) and links each issue that is the face of a set to that set. Sets written before 0.4.0 have no effects and behave as before.

### 2.4 Typed evidence links (Part 3)

`oracle/core/evidence.py`: `EvidenceLink(id, subject, evidence, relation, decision_id, note, created_at)`. The **subject** is a level,
element, node or grid line (or an engineering decision); the **evidence** is an architectural-scope target; the relation is
`derived_from`, `supported_by` or `constrained_by`; the **decision** is the accepted engineer decision that made the link.
`project.link_evidence()` enforces: only an engineer; the subject and evidence exist; evidence that has a review status (a view, an
observation) must be **accepted**, and so must the view an observation belongs to; the evidence must be a view, an observation or a
height. A refused link leaves nothing behind (not even its decision). The core knows only "architectural-scope target"; it does
not know what an observation is, and `ArchitecturalObservation` does not live in the link. `establish_levels()` creates links from each
level to its height evidence and accepted plans under the engineer's decision.

### 2.5 `trace()` (Part 4)

`project.trace(target_or_id)` returns a `Trace` (plain data, `to_dict()`): the target and its value statuses, the engineer decisions about
it, its evidence links and their decisions, the evidence and its approval state, the provenance records (method, producer, confidence,
recording time, file, entity handle, layer), the source (id, file, revision, sha256, interpretation instance), the entity identifiers
(flagged `sampled` when an aggregated observation lists only some of its entities), and a list of **gaps**. A gap is any missing
link, in words: no evidence link; evidence not approved or since rejected; no provenance; no source entity identifier; a link not backed
by an engineer decision; an engineer-defined height with no recorded resolution. Nothing is inferred to fill a gap. Engineer-defined
heights are followed through the resolution that created them to the question's own evidence.

### 2.6 The approved architecture projection (Part 5)

`project.approved_architecture(source_id=None)` returns a frozen, read-only `ApprovedArchitecture` in domain terms and **millimetres**:
accepted views (with the building level they realise), accepted observations (whose view is also accepted) with geometry in the
building frame (or, for a plan not yet aligned, in its own view frame, and it says which), hints with an `approved` flag (True only
once an engineer decision has set the hint: approving a symbol does not approve a meaning), levels (identity, label, source label, key,
elevation, elevation type, storey height, and the structural elevation, None when not established), the unit and whether it is confirmed,
and `blockers` (unconfirmed unit, open questions, unreviewed views, unaligned plans, no levels). **No layer names, entity handles, format
codes, layouts or raw drawing coordinates.** Its link back to the drawing is `trace()`, the audit path, not the working model. It lives in
`oracle.core` and is tested to load and run in a fresh process without importing ezdxf, `oracle.ingestion`, `oracle.interpretation` or
`oracle.adapters`.

### 2.7 CAD vocabulary out of the domain model (Part 6)

| Was | Now |
|---|---|
| `DrawingSource.unit_code`, `format_version`, `layouts` | `declared_unit` (mm/cm/m/inch/foot or None) and an opaque `source_metadata` (scalars and lists, stored, never interpreted) |
| `DWG-n` ids | `SRC-n` |
| the `$INSUNITS` table in `interpretation/units.py` | in the reader: `DrawingDocument.declared_unit` |
| the `Defpoints` layer name in `interpretation/layers.py` | the reader sets `LayerInfo.non_plotting` (also from the file's plot flag); the classifier uses the flag |
| the report printing a named format key | prints the opaque metadata as a whole |

Architecture tests (`tests/test_boundary_and_schema.py`) scan every `oracle/core` module by AST and tokens for CAD vocabulary in imports,
names, strings and comments and for CAD/AI/geometry libraries, and fail on a hit. **The one deliberate exception** is
`oracle/core/migrations.py`, which must name the keys of an old file to carry them across; the test pins it to that function. Only
`oracle/ingestion/dxf_reader.py` imports ezdxf (also tested). Layer *classification* by NCS-style names stays in
`oracle/interpretation/layers.py`: it is interpretation of conventions, not domain.

### 2.8 Observations describe; they do not assert (Part 7)

`existing_column` → **`column_symbol`**, `beam_shown` → **`beam_symbol`**; hints `existing_column` → **`column_candidate`**,
`existing_beam` → **`beam_candidate`**. `oracle/interpretation/observations.py` now holds `OBSERVATION_VOCABULARY`: every kind the
pipeline can produce with a stated meaning ("a closed symbol or block on a layer named for columns"). Three tiers are kept apart:

| Tier | Example | Who |
|---|---|---|
| OBSERVATION | "a rectangular symbol is drawn here" (`column_symbol`) | Oracle, INFERRED/SOURCE, review PROPOSED |
| INTERPRETATION | "this symbol may be a column" (`column_candidate` hint) | Oracle proposal; `approved` False |
| ENGINEER DECISION | "this is accepted as a retained column" | an engineer: `set_value(observation, 'hint', ...)`, then an `EvidenceLink` from the structural object |

The tests check produced kinds against the vocabulary and reject structural words (retained, existing, load, bearing, demolished, ...) token by
token (so `stair_linework` is not mistaken for containing "new"), which is stricter than the earlier word search that would not have caught
`existing_column`.

### 2.9 The level model (Part 8)

`Level` gains, all optional and omitted from the file when default: `source_label` (verbatim from the drawing), `elevation_type`
(`unspecified` by default; `finished_floor`, `structural`, `datum`), `structural_elevation_mm` (None = **not established**), `key`
(the interpretation identity it realises) and `datum` (what the elevation is measured from, in words). **Identity is not a label**:
`id` is stable, `name` is the engineer-facing label and may change, `source_label` is what the drawing said. Interpretation keys
gain `NAMED:<LABEL>`, so an alias may map any label (PODIUM, LOWER TERRACE) to itself instead of onto a floor number; ordering of
such levels comes from height evidence (or the elevations), never from a built-in rank. Finished-floor, structural and datum elevations
are never converted silently: `Level.structural_elevation` returns None unless the level's type is structural or a structural value has been
set; `establish_levels()` takes an `elevation_type` (default `unspecified`, which claims nothing; claiming a type without a decision leaves
that claim ASSUMED) and raises a warning that the structural elevation has not been established. `suggest_elevations()` reports elevations relative to
the lowest plan level and says nothing about what kind of elevation they are.

### 2.10 Multiple drawing sources (Part 10)

The single `architecture` slot became `architectures`, one `ArchitecturalInterpretation` per **source**. `DrawingSource` now separates
`id` (SRC-n, within the project), `revision` (as declared or supplied), `sha256` (the exact bytes), `interpretation_id` (AINT-n: one run of the
interpreter) and `interpreter`. `interpret_into(project, document, revision=...)` adds a source: object numbers start in their own block (source 2
numbers from 10 001), so ids never collide, issue/set/decision numbers continue the project's, nothing existing is changed, and **no decision is carried
over**. Readiness counts unreviewed views of every source; `trace()` and `approved_architecture(source_id)` work per source (the projection refuses to
guess when there are several). See section 8 for what revision carry-forward will need.

## 3. Schema 0.4.0 and migration

`SCHEMA_VERSION` 0.3.0 → **0.4.0**. A schema bump was necessary: the strict-keys policy rejects unknown fields, and the changes are persisted.
Added or changed:

* project: `architecture` → `architectures` (list); new `evidence_links` (list);
* `DrawingSource`: `declared_unit`, `revision`, `interpretation_id`, `interpreter`, `source_metadata` added; `unit_code`, `format_version`, `layouts` removed
  (moved into `source_metadata`); id prefix `SRC-`;
* `Interpretation`: optional `effects`, `applied`; `EngineeringIssue`: optional `interpretation_set_id`;
* `Level`: optional `source_label`, `elevation_type`, `structural_elevation_mm`, `key`, `datum`;
* level keys: `NAMED:<LABEL>`; observation kinds and hint values renamed (above).

**Migration 0.3.0 → 0.4.0** (`oracle/core/migrations.py`, tested against a trimmed but genuine 0.3.0 file written by the Phase 3 code,
`tests/fixtures/schema_0_3_0_project.json`): wraps the architecture in `architectures`; moves `format_version`, `unit_code` and `layouts` verbatim into
`source_metadata`; adds `revision` null and `interpretation_id` `AINT-1`; renames `DWG-n` to `SRC-n` wherever it is a whole-string id (free text is never
rewritten); renames observation kinds and hints; adds an empty `evidence_links`. **It invents nothing**: `declared_unit` stays absent because turning a
format's unit code into a unit is the reader's job (the raw code is kept), and no evidence links, effects or issue links are fabricated. It refuses a 0.3.0
file that already has the new keys, lacks `architecture`, is malformed, or has unknown fields; unknown or future versions raise `SchemaVersionError`; 0.1.0 and
0.2.0 files migrate through the chain. Old code cannot read 0.4.0 files.

Existing-test edits made **because of this deliberate schema correction** (none weakened, none deleted): the literal version strings and the registered-migration
list in `tests/test_migration_readiness.py`; `DWG-1` → `SRC-1`, `unit_code`/`format_version` → `declared_unit`/`source_metadata` and the renamed observation
kinds in the Phase 3 tests; one assertion in `tests/test_decision_authority.py` that used a *level* as its example of an unsupported `set_value` target (levels are now
supported; it uses a node instead); and the Defpoints classification example, which moved from a name-only test to an ingestion test.

## 4. The evidence chain, end to end

```
source drawing bytes (sha256, revision)             DrawingSource
  -> source entity (opaque identifier)              entity_ids / provenance.entity_handle
    -> provenance record (method, producer, confidence, time)
      -> architectural evidence (view / observation / height; review status)
        -> [engineer decision]  approves the evidence           review_views / review_observations
          -> EvidenceLink (derived_from / supported_by / constrained_by), backed by an engineer decision
            -> domain object (level, element, node, grid) or engineering decision
              -> its own value statuses and decision history (previous values, supersession)
```

`project.trace(x)` walks this from the bottom up. Tested on synthetic drawings (the traced entity id is checked to be a real entity of the original
drawing on the layer the observation names) and on the real squash-court drawing (the trace ends at the exact hash of the original DWG).

## 5. What remains intentionally deferred

* **Structural anything**: member generation, sizing, load, supports, STAAD, reinforcement, structural intent, structural GA generation.
* **Automatic revision carry-forward** of engineer decisions (section 8).
* **Observation → structural object promotion**: an engineer links an object to evidence; nothing creates the object.
* **Discovering unfamiliar level labels automatically**: they are recognised through configured aliases or set by the engineer; an unknown label on a plan is reported
  ("does not name its level").
* **Non-2D views** (3D views, isometrics), **view scale/north**, **linking a view to its sheet**.
* **A structural elevation convention** (top of slab, underside, datum offsets): only representable (`structural_elevation_mm`, None until established), not derived.
* **Compaction of the project file** (section 7), and a database.
* **Approving a hint in bulk** and a UI for any of this.
* **`acknowledge` effects change no state**: the answer to "how many levels does the building have?" is recorded and the issue answered, but the levels are still created by an
  explicit `establish_levels()` decision.
* Legacy-adapter level ids (from GA tags) and architectural level ids (`GF`, `L1`, `NAMED` slugs) are **not unified**; merging a structural GA and an architectural interpretation
  in one project needs an explicit mapping decision (`Level.key` is the hook).

## 6. Approved projection versus interpretation: what a consumer may rely on

| Question | Answer |
|---|---|
| May it import `oracle.core` only? | Yes (tested in a fresh process) |
| Are coordinates trustworthy? | Only where `frame == "building"` and `unit_confirmed`; otherwise the projection says so in `blockers` |
| Are hints decisions? | No; `approved` is False until an engineer decision sets them |
| Is a structural elevation given? | No: None until established |
| Can it be modified? | No (frozen dataclasses, tuples) |
| Where is the audit trail? | `project.trace()` |

## 7. Performance and size (Part 11)

Profiled on the real squash-court drawing (24,159 entities; `interpret_document` about 1.2 s once read; reading the DWG about 40 s; project load 0.33 s):

| | bytes | share |
|---|---|---|
| project JSON, `indent=2` | 7,249,901 | 100 % |
| the same, compact | 4,047,294 | 56 % (**indentation is 44 % of the file**) |
| gzip of the indented file | 328,293 | 4.5 % (about 22x) |
| `provenance` (3,228 records) | 2,999,629 | 41.4 % |
| `architectures` (1) | 2,849,215 | 39.3 % (observations 1,938,077 = 26.7 %; views 5.1 %) |
| `value_status` (3,146 records) | 774,300 | 10.7 % |
| everything else | | < 1 % |

Findings:

* **Observations dominate, and windows dominate observations**: 2,510 of 2,956 observations (85 %) are single window blocks (one record each, each with its own provenance and value status).
* **Provenance is one record per observation** (1.0 per observation) and is the largest single cost. Its `source.file` string (73 bytes) is repeated in all 3,228 records (about 3 %), and the identical
  `producer` and timestamp text is another 3.6 %. 1,483 distinct notes across 3,228 records: real content, not duplication.
* **`value_status` duplicates `observation.basis`**: 2,956 of its 3,146 records are the `kind` status of an observation (10.7 % of the file). They exist so an engineer's override of `kind` has something to replace; they could be
  implicit until an override happens.
* **Geometry**: observation geometry is 8.7 % and entity ids 1.0 %; 428 observations (14 %) have geometry identical to another (stacked curtain-wall windows). Not worth deduplicating.
* **Extrapolation**: a project 10x this size would be about 72 MB of indented text and load in a few seconds. Fine for now; a large multi-source project could become uncomfortable to diff, not to load.

Options, in order of value, none implemented (the brief says not to sacrifice traceability for size): (1) write compact JSON or gzip on save (about 44 % / 95 % smaller, no model change);
(2) aggregate runs of identical individual observations (windows) into one observation with a count and all entity ids (the aggregation already exists for linework); (3) reference the source file
in a provenance record by source id instead of repeating the file name; (4) make the default `kind` value status implicit. **Provenance can be deduplicated in the file, but not merged in the model**:
each record is one fact about one object, and that is what `trace()` walks. No database is warranted by anything measured.

## 8. Multi-drawing and revision readiness: what carry-forward will need

Built now: source identity (id, revision, hash, interpretation instance), per-source architectures, collision-free ids, per-source readiness/trace/projection, and "no decision crosses sources". **Needed later**, in order:

1. **A source-to-source relation**: "SRC-2 is a revision of SRC-1" (a typed field, not free text), so carry-forward knows what to compare.
2. **Entity correspondence between revisions**: matching observations/views across two interpretations (by geometry, layer, label and proximity), with a confidence, kept as data.
3. **A carry-forward proposal, not an action**: for each engineer decision on the old source, a *proposed* re-application on the new one (same effect, matched target), which an engineer accepts or rejects through the ordinary machinery (the
   `effects` payloads make the decisions reapplicable as data).
4. **Staleness**: evidence links and level decisions that rest on old-revision evidence must be flagged when the evidence changed or vanished (`trace()` already reports rejected/missing evidence; it needs a `superseded_by` link between revisions).
5. **A policy for what carries** (a rename yes; an alignment only if the footprint is unchanged; a height only if the section is unchanged).

## 9. Tests run

* `python -m unittest discover -s tests -t .` runs the everyday tiers; `python -m tests all` runs every test including the real drawing (about 30-60 s). See [TESTING.md](TESTING.md) for the tiers and commands.
* New files: `tests/test_levels_hardening.py`, `test_resolution_effects.py`, `test_evidence_trace_projection.py`, `test_boundary_and_schema.py`, the helper `tests/support35.py`, the genuine 0.3.0 fixture
  `tests/fixtures/schema_0_3_0_project.json`; and additions to `tests/test_real_architectural_drawing.py` (trace on the real drawing, resolving its unit conflict, the projection of a reviewed real plan, size shape).
* A regression test exists for every bug fixed: no value status for a missing value; levels settable with history; resolution changes state and issues; forged payloads rejected.
* Mutation check: breaking `plan_level_change`'s refusal rule, `check_resolutions`, and the evidence-approval rule each made tests fail (see the final report).

## 10. Known limitations

* A resolved `acknowledge`-type set (level count, duplicate plans, repeated level tags) records the answer but changes no state: the engineer still acts on it explicitly.
* The rehearsal in `accept_interpretation()` copies the whole project (about 0.35 s for the real one); acceptable for an interactive decision, not for a loop of thousands.
* Cascaded level moves are recorded as engineer decisions authored by the same engineer and marked "Moved with D-x"; they are consequences of one decision, not five separate ones. A future report should present them as one.
* Evidence approval is checked when a link is made and reported by `trace()` afterwards; a link is not deleted when its evidence is later rejected (it is history, and the trace shows the gap).
* `set_value()` supports top-level fields of elements, levels and architectural objects; nodes, grids and the design basis are still not settable.
* Multi-source support has no relation between sources and no cross-source level or alignment reasoning.
* The 0.3.0 → 0.4.0 migration leaves `declared_unit` absent (it will not translate a format code) and does not link old issues to old sets, so an old project's questions still resolve by the old, status-only route.
* Hints can only be approved one observation at a time (`set_value`).
* `interpret_into()` numbers a source's objects in blocks of 10,000. A source with more than 10,000 observations (or views, frames...) would run into the next source's block; adding that next source is then refused loudly by the id-collision check, never silently. A proper namespace for object ids is the cleaner fix and would be a schema change.
