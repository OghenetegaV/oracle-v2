# Phase 2: Legacy GA Adapter

Status: implemented and tested; **not connected to the wizard**. Revised after the core gained provenance, value
status and interpretations (schema 0.2.0, see [CORE_EVIDENCE_MODEL.md](CORE_EVIDENCE_MODEL.md)): the adapter now writes
them into the project, so they are saved with it.

```
structural GA DXF -> ga_dxf_parser.parse_multilevel_ga() -> oracle.adapters.legacy_ga.adapt_legacy_ga()
                  -> oracle.core.BuildingModel -> OracleProject -> JSON
```

Nothing in `oracle.core` imports the adapter, and the adapter imports no legacy script (it reads the
parser's result dict by duck typing). Both are checked by tests.

## 1. What the legacy parser does (audit findings)

Input: a structural GA DXF whose layers follow `COLUMN <a>-<b>` and `<tag> BEAMS` (e.g. `COLUMN G-1`,
`F.F BEAMS`), plus `storey_heights_m` and optional `per_level_loading` from the caller.

| Question | Finding |
|---|---|
| Extracts | Beam centrelines (LINE/LWPOLYLINE/POLYLINE segments), column outline **centroids**, VOID-layer **centroids**. |
| Levels | Level tags come from chaining `COLUMN a-b` layers (`['G','1','2','R']`); beam layers are matched to levels by ordinal name rank. `storey_heights_m` is misnamed: its values are **cumulative elevations in metres**, and they are not returned in the result. |
| Columns | One member per column per storey range: `(no, j_lo, j_hi, "column", (lo, hi), "column_mm")`. The drawn outline (e.g. 225x225) is used only for its centroid; the section is then a constant from `DEFAULT_SIZES`. |
| Beams | `(no, j1, j2, "beam", level_tag, size_key)`. One member per **polyline run between drawn vertices**, not per span: median 10 m, up to 24.3 m on the fixture. |
| Joints | `JointRegistry.coordinates()` gives `{no: (x, elevation, z)}` in metres, STAAD axes (`z` is the DXF Y). Joints are deduplicated at 1 mm; column joints snap to a beam joint within 75 mm. The registry also holds slab-panel corners (added later by the STAAD writer), 83 of 213 on the fixture. |
| Slabs | `slab_panels[level] = [{"vertices", "centroid"}]`, inferred from closed regions of beam centrelines (shapely). Panels have no IDs. A non-rectangular region is split into **triangles** (an FE meshing artefact, not a slab). |
| Openings | Only `void_centroids`, and the same list is applied to **every** floor. A panel containing a centroid is silently removed; the void outline is never kept. |
| Floor offsets | Floors drawn side by side are translated onto the first floor's bounding-box corner. The translation is reported only as message text ("offset by (-35.41 m, 0.00 m)"). |
| Layer names | Interpreted by regex and rank tables (`ORDINAL_PREFIX_RANK`); unrecognised schemes fall back to file order with a warning. |
| Issues | `ParseIssue(kind="warning"|"blocking", message)`: text only, no category, target or element reference. |
| Units | Assumes DXF units are millimetres. `$INSUNITS` is never read. |

### What the parser throws away (verified against the real fixture)

The GA drawing contains grid lines (`GRIDLINES`, 60) and labels (`GRID NAME`: 1-8, A-F and primes), level
titles in MTEXT ("FIRST FLOOR GENERAL ARRANGEMENT"...), 225x225 column outlines, and hatches. None reach the
result, and DXF entity handles are never retained. A 3-storey model is therefore built without grid,
level names, real column sizes or entity traceability.

### Defects found in the legacy output (not caused by the adapter)

1. **Beams are not split at the columns they cross.** On the fixture 16 of 77 columns have their end on the
   interior of a beam with no joint there; in the legacy STAAD model those column tops are not connected to the
   beam. The adapter raises one **blocking** issue per column/beam pair (`UNSUPPORTED_BEAM`, engineer action
   required, both objects' provenance as evidence) and does not split the beam.
2. **VOID handling** applies every marker to every floor and discards the outline. The drawing
   `... with voids.dxf` has no `VOID` layer, so it produces exactly the same 36 slab panels as the plain file: its
   voids are invisible to the pipeline.
3. **Slab triangulation** loses the slab outline (reported when it occurs; none on the current fixtures).

## 2. The adapter

`oracle/adapters/legacy_ga.py`: `adapt_legacy_ga(parse_result, elevations_m, *, project_name, engineer,
section_sizes_mm, ...) -> AdaptationResult` (types in `oracle/adapters/result.py`).

Translation only. No design, Claude, STAAD or drawing code. It never raises for bad source data: problems become
`EngineeringIssue`s on the returned project and `result.complete` is `False` if anything was dropped or could not be
built. **`complete` means "nothing dropped", not "confirmed"**: the real fixture is complete, yet
`result.readiness()` says it is NOT ready (29 blocking issues, 169 assumed values). A structural finding such as a
beam with no node under a column is blocking but is not an adapter failure, so it does not clear `complete`.

The caller supplies what the parser result does not carry: the elevations, the size table
(`ga_dxf_parser.DEFAULT_SIZES`, so no legacy constants are copied), and optionally the engineer's per-level loading
(only to tell an engineer-supplied slab thickness from the default), level names and a `DesignBasis`.

### Mapping

| Legacy | Canonical | Notes |
|---|---|---|
| level tag `'1'` | `Level(id='1', name='Level 1')` | Name is generated (flagged INFO). Elevation = `elevations_m` x 1000. `storey_height_mm` derived from the next level; top level `None`. Levels must be strictly increasing in legacy order or nothing is imported. |
| joint `(x, y, z)` m | `Node('N<joint>', level, Point2D(x*1000, z*1000))` | Only joints used by a member. Level found by exact elevation (0.5 mm). Joints within 1 mm on a level are merged and reported. |
| beam member `n` | `Beam('B<n>', level, start, end, Section)` | ID keeps the legacy member number. |
| column member `n` | `Column('C<n>', lower, upper, location, Section)` | Location = lower joint. Upper/lower plan drift over 1 mm is reported. |
| slab panel k of level L | `Slab('S<L>-<k>', L, Polygon2D, thickness, supported_by)` | Panels sorted by centroid before numbering, so IDs are stable. `supported_by` = beams collinear with an edge (25 mm tolerance); inferred. |
| `ParseIssue` | `EngineeringIssue` (source `ga_dxf_parser`) | Message kept verbatim; `warning`->WARNING, `blocking`->BLOCKING, anything else ERROR; category by a small regex table, default OTHER. |
| `void_centroids` | issue per level, no `Opening` | Outline unavailable. |
| `loading`, `MATERIAL` | not migrated | They belong to `DesignBasis`, which needs a design code and grades the adapter must not invent. |

Coordinates: core plan (x, y) = (legacy x, legacy z) x 1000 mm; the origin is the DXF sheet origin of the reference
floor (the parser has already translated the others onto it), **not** a structural grid origin.

### Fact, inference, assumption

| Item | Value status written |
|---|---|
| Beam centreline, node position | `source` |
| Column position (outline centroid, possibly snapped), slab boundary, `supported_by` | `inferred` |
| Level membership of beams/columns, level IDs | `inferred` (from layer names) |
| Level elevations | `engineer_defined`, backed by an accepted engineer decision the adapter records on the caller's declaration |
| Storey heights | `derived` |
| Member sections | **`assumed`** (legacy defaults), with field-level provenance naming the default table |
| Slab thickness | `assumed` (default), or `engineer_defined` if the engineer supplied it |
| Level names | `assumed` (generated), or `engineer_defined` if given |
| Materials, loading, cover, codes | nothing recorded; a WARNING says no design basis is attached |

Assumptions are values with status `assumed` and also open `EngineeringIssue`s. Confirming or changing one goes
through `project.set_value()`, which makes it `engineer_override`, keeps the old evidence and value in history, and
removes it from the readiness blockers.

### Validation and issue catalogue

The adapter checks: levels present and strictly increasing, finite elevations, ID validity and collisions,
joints with coordinates and a matching level, member endpoints at the elevation of the member's level(s), section
size available, slab thickness available, polygon validity, duplicate beams/slabs after merging, near-coincident
joints, columns whose ends lie on a beam interior without a node, suspicious longest beam (outside 0.5-100 m,
"parser assumes millimetres"), plus everything `project.validate()` enforces. Each failure is an
`EngineeringIssue` (`source` = `oracle.adapters.legacy_ga`) whose message states what was affected, the source,
what information is missing, and whether engineer action is required. Rejected elements are skipped, never
guessed.

## 3. Traceability

IDs embed the legacy number (`B12` = member 12, `N44` = joint 44). Every level, node, beam, column and slab has a
whole-object `ProvenanceRecord` in the project (file, layer, entity type, `member 12 (joints 19-20)`, source
coordinates with their frame, the level/floor context, method, producer, timestamp), and the values that are
placeholders (section, default slab thickness) have their own field-level record naming the default table (with no
file, because it is not in the drawing). Records are saved and loaded with the project. **The DXF entity handle is
still not available**: the parser never kept it.

`AdaptationResult.provenance_for()` / `provenance_records()` and the `SourceRef` / `Basis` types are now read-only
compatibility views over those records and the value statuses, kept so the Phase 2 API and tests still work.
Provenance previously lived only in memory on the result (the gap recorded at the end of Phase 2); it now lives in
`oracle.core`.

## 4. Information lost or not representable

| Lost / unrepresented | Where | Handling |
|---|---|---|
| DXF entity handles, original entity type | parser | provenance carries layer + member/joint number only |
| Column outline size, rotation | parser | sections assumed; issue |
| Grid lines and labels; level titles; annotations | parser | INFO issue; core `GridLine` unused |
| VOID outline; the floors a void really crosses; which slab it removed | parser | issue per floor, no `Opening` |
| Original slab outline where triangulated | parser | issue |
| Which members were dropped as duplicates | parser | parser's count kept as an issue |
| Sheet-space position of upper floors (before alignment) | parser | offsets stay in the parser's message |
| Original (pre-snap) column centroid | parser | drift reported |
| Loading, materials | not migrated | needs a `DesignBasis` |
| Beam continuity over supports | model | reported; not split |

## 5. Assumptions introduced by the adapter

Level names generated from tags; element IDs from legacy member numbers; nodes only for joints a member uses;
column location taken from the lower joint; slab numbering by centroid order; tolerances (elevation 0.5 mm, support
inference 25 mm, node-at-column 80 mm, over-column lateral 30 mm); legacy message categories by regex; level IDs
sanitised to `[A-Za-z0-9_.-]`; the parser's unit assumption (mm) is carried, only sanity-checked.

## 6. Known limitations

- Only `ga_dxf_parser` output is adapted. `ga_output.json` (Claude's single-floor layout) is **not**: it carries no
  column sections and beam depth only, and `Column`/`Beam` require a full `Section`, so it cannot be represented
  without inventing values.
- Column identity is per storey (`C53` G-1 and `C81` 1-2 are two elements); no column-stack or grid naming.
- Beams are the parser's polyline runs; supports along them are unknown.
- Results are stable per drawing and parameters, not across parser changes (IDs depend on member numbering).
- Not connected to the wizard; the wizard still works from its own dicts.

## 7. Boundary for a future architectural-DWG interpreter

The interpreter must be **adaptable, not layer-name-hardcoded** (varying layer conventions, several plans in one
DWG, sections/elevations, mezzanines, stepped levels, roofs, stairs/lifts, side-by-side plans, blocks/dimensions/
text). Keep three stages separate:

1. **Interpretation** (per project, heuristics and possibly an LLM): classifies entities and proposes floors,
   levels, grid, walls, openings, stair/lift cores and candidate structural positions, each with a confidence and the
   entity handles it used. Everything here is a proposal.
2. **Adapter** (deterministic, like this one): turns accepted proposals into `BuildingModel` and issues.
3. **Engineer**: confirms, edits or overrides through `EngineeringDecision`s. Nothing inferred becomes fact
   without that.

Contract an interpreter's adapter must satisfy:

- Return an `AdaptationResult` (`oracle/adapters/result.py`), never a bare model, and never raise on bad drawings.
- Give every object a `ProvenanceRecord` (with the DXF entity handle in `entity_handle`) and a `ValueStatusRecord`
  for each important field (`source` / `inferred` / `assumed` / `engineer_defined`), and put uncertain readings in
  `InterpretationSet`s with confidences instead of choosing one silently.
- Get elevations and storey heights from the drawing (sections/elevations/level tags) or ask the engineer; never
  default them silently. Report ambiguity (stepped levels, mezzanines) as issues.
- Use millimetres; state the source units and how they were established (`$INSUNITS`, a dimension, or asked).
- Record the coordinate frame: prefer a structural-grid origin, and say when it is only the sheet origin.
- Emit `Opening` (with outline), `Stair`, walls and `GridLine`s where found; report what it could not interpret
  instead of dropping it.
- Do not place structural members by AI here: proposed member positions are Oracle recommendations
  (`EngineeringDecision`, `source=ORACLE/AI_ASSISTANT`, `PROPOSED`) until the engineer accepts.

The core gaps named at the end of Phase 2 (provenance storage, an assumed/inferred flag, alternative interpretations)
are now closed in schema 0.2.0. What remains for that future work is listed in
[CORE_EVIDENCE_MODEL.md](CORE_EVIDENCE_MODEL.md) section 9: support/continuity/transfer records, policy for what blocks
readiness, and turning an accepted interpretation into elements.

## 8. Tests

`tests/test_adapter_legacy_ga.py` (43 tests): real GA fixture (multi-floor and a single-floor cut from it), a VOID case
added to that cut, the architectural `test_floor.dxf` as a failing input, hand-built parse results for failure paths,
the JSON round trip (in memory and via file), determinism, no mutation of the parser result, and module
independence. `tests/test_adapter_evidence.py` covers the provenance, value-status and blocking-issue behaviour
described above. Run everything with `python -m unittest discover -s tests -t .`.
