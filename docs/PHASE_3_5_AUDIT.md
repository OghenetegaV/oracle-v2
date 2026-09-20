# Phase 3.5 final architecture audit (before Phase 4)

Method: every claim below was checked by reading the code and by running a probe or a test against it (probe script results are quoted). Nothing was
changed in production code by this audit. Findings that need a decision or a fix are collected in section 8 as **blockers / recommended pre-Phase-4 fixes**.

## Summary

| | Area | Result |
|---|---|---|
| A | Domain boundary | **Pass** |
| B | Evidence and traceability | **Pass** (typed, validated, machine-readable; one documented caveat about aggregated observations) |
| C | Engineer authority | **Partial**: recommendations cannot become decisions; forged data is rejected on load; **four bypasses of the readiness gate exist in older APIs** |
| D | Multi-source | **Pass on identity and collisions; three hazards** (silent first-source defaults, single-source report, no revision relation) |
| E | Architectural to structural boundary | **Pass for what it contains; specific domain concepts are missing** (section 5) |
| F | Level model | **Pass**, except that the architectural-to-GA level mapping is an unvalidated free field |
| G | Phase 4 prerequisites | **Can begin with proposal-only records**, after the small fixes in section 8 |

## A. Domain boundary: pass

* `oracle/core` has no CAD-library dependency: an AST and token scan of every core module (`tests/test_boundary_and_schema.py`) finds no CAD library import, name, string or
  comment, with one pinned exception (the migration, which must name the keys of an old file). `import oracle.core` loads no ezdxf, shapely, matplotlib, anthropic or numpy (fresh-process test).
* The approved projection carries no CAD vocabulary: its serialised form contains none of `layer, handle, dxf, dwg, ezdxf, insunits, unit_code, layout, paper, a-wall, entity_ids, sha256, format_version, source_metadata`.
  It loads and runs in a fresh process that never imports ezdxf, `oracle.ingestion`, `oracle.interpretation` or `oracle.adapters`.
* Observations do not become elements: nothing under `oracle/interpretation`, `oracle/ingestion` or the new core modules calls `add_element` or constructs a `Column`, `Beam`, `Slab` or `Wall` (grep is empty). The only way an
  observation touches a structural object is an `EvidenceLink` made by an engineer.

## B. Evidence and traceability: pass

Demonstrated on the 3x3-column structural model with a synthetic drawing interpreted into it (`trace("C5")`, complete, no gaps):

```
element C5
  -> engineer decision (accepted, source engineer)            T-0003
  -> EvidenceLink EV-0001  derived_from OBS-00001              (typed: subject, evidence, relation, decision_id, note, created_at)
  -> ArchitecturalObservation OBS-00001 (accepted, and its view accepted)
  -> provenance PV-00025  (method layer_class, producer oracle.interpretation, confidence, recorded_at, file, entity handle, layer)
  -> source SRC-1: file, revision, sha256, interpretation instance AINT-1
  -> source entity id  '60'   (checked to be a real entity of the original drawing, on the layer the observation names)
```

The link is a validated dataclass (`oracle/core/evidence.py`), enforced by `project.link_evidence()` (engineer decision required; subject and evidence must exist; evidence must be an accepted view or observation, and an observation's
view must be accepted; duplicates refused) and re-checked on load. **No free-text convention is relied on**: `trace()` never reads `SourceReference.source_id` (the pipeline still fills that provenance field with the object id as the
producer's own identifier; it is descriptive, not a link). On the real drawing the trace ends at the exact sha256 of the original DWG.
Caveat: an aggregated observation (linework) lists at most 25 sample entity ids (`sampled: true` in the trace), so it traces to a sample of its entities, not all.

## C. Engineer authority

**Holds** (probe output):

* an ORACLE or AI_ASSISTANT decision cannot be added as accepted; a proposal cannot be set to accepted; neither can accept an interpretation, review a view, link evidence, set a value or establish levels (all refused);
* engineer overrides, resolutions, merges, alignments and links survive save and load byte for byte;
* forged data is rejected on load: a fabricated decision id on a value status, a forged link (unknown evidence, decision, subject, extra key, unknown relation), an edited resolution payload, a reopened issue behind a resolved set, an edited
  model value that contradicts its decision, an oracle decision passed off as an engineer's;
* a rejected alternative, a refused effect and a refused link change nothing (compared byte for byte).

**Does not hold: the readiness gate can be bypassed through older APIs** (all four reproduced):

| # | Bypass | Reproduced |
|---|---|---|
| C1 | `project.resolve_issue(id, "text")` closes an issue, **including a BLOCKING one, with no decision** (only `accept_issue` requires an engineer decision for ERROR/BLOCKING) | a blocking issue disappears from `readiness()` |
| C2 | `project.set_value_status(ValueStatusRecord(target, field, SOURCE))` accepts any non-engineer status with **no provenance and no decision**, so an ASSUMED value can be relabelled SOURCE | readiness blockers 3 -> 2 |
| C3 | Getters return mutable objects and `save()` does not call `validate()`: a decision or interpretation altered in memory reaches the file. `load()` then rejects it, so the file is safe to *read* but the forgery is not stopped at mutation or at save | forged state saved, rejected only on reload |
| C4 | `establish_levels(project, elevations)` **without a decision** creates BuildingModel levels from Oracle's own suggestion (their names remain ASSUMED, so readiness still blocks, but Oracle has acted, not proposed) | levels GF, L1, L2 created |

None of these is reachable from the new Phase 3.5 APIs; they are Phase 1/2 and convenience behaviour. Cosmetic: several error messages lowercase the ids they quote (`int-0001`) because they use `str.capitalize()`.

## D. Multi-source behaviour

Two sources (revision A, and revision B with a wider first floor and 3500 mm storeys) coexist in one project:

* **No collisions**: source ids `SRC-1/SRC-2`, interpretation instances `AINT-1/AINT-2`, different hashes and revisions; object ids do not overlap (source 2 starts at `VIEW-10001`, `OBS-10001`, `FRM-10000`); provenance, issue and interpretation-set ids are unique across the
  project; adding a second source changes nothing in the first (compared exactly) and carries no decision over; a duplicate source or colliding id is refused; establishing levels twice is refused as a duplicate level id (no silent overwrite).
* **One interpretation never silently replaces another**: correct, but three hazards remain:
  1. `project.architecture`, `suggest_elevations()` and `establish_levels()` default to the **first** source. With revision B present they silently return revision A's levels (`3300 / 6600`) unless `source_id="SRC-2"` is passed. (`approved_architecture()` correctly refuses to guess.)
  2. `render_report(project)` prints only the first source; the second is invisible in the report.
  3. **Nothing relates the two revisions or compares them**: A says 3300 mm and B says 3500 mm for the same storey and no issue is raised. That is the future *revision carry-forward / comparison* requirement (source-to-source relation, entity correspondence, staleness of links and decisions,
     see `PHASE_3_5_HARDENING.md` section 8). It is not implemented, deliberately. Hazard 1 is a correctness risk that should be closed first (section 8).

## E. Architectural to structural boundary

The approved projection gives a structural engine: views (kind, title, level id and key, variant, confidence, frame, bounding box in mm), observations (kind, label, count, closed, geometry in mm in the building or view frame, confidence, basis), hints with an `approved` flag, levels (identity, label,
source label, key, elevation and its type, storey height, structural elevation or None, datum text) and the unit with whether it is confirmed, plus `blockers`. It exposes none of DXF entities, layers, ezdxf objects, unit codes, sheet or layout names, or raw coordinates.

**Missing domain concepts** (named in domain terms, not CAD terms):

1. **Grid axes**: labels and positions. Grid linework is exposed only as aggregated `grid_lines`; grid *labels* are not in the projection although the interpreter reads them.
2. **Wall segments**: a centre line, a thickness and a wall type. Walls are aggregated linework per layer class (`count` > 1, no per-wall geometry).
3. **The level an observation belongs to** as a direct field (a consumer must join through the view).
4. **Orientation and scale** (north, drawing scale).
5. **Openings hosted by walls** (a door or window is a positioned symbol, not related to a wall).
6. Hint approval exists only per observation through `set_value` (no bulk approval).

## F. Level model

* Identity is separate from label: `id` (stable) / `name` (engineer label, settable) / `source_label` (verbatim) / `key` (interpretation identity); tested that renaming keeps ids and node references. **Pass.**
* Finished versus structural elevation: `elevation_type` (`unspecified` default) plus a separate `structural_elevation_mm`; `Level.structural_elevation` returns None unless the type is structural or a structural value was set; `establish_levels` raises a warning and never derives one from the other. **Pass.**
* A storey height with a recorded status is an actual value: tested as an invariant (no value status exists for a value that does not exist). **Pass.**
* Engineer-defined elevations, cascaded moves and their history survive save and load byte for byte. **Pass.**
* PODIUM (and any alias) keeps its own identity (`NAMED:PODIUM`, level id `PODIUM`, name "Podium", source label "PODIUM PLAN"), and is ordered by height evidence, not by a built-in rank. **Pass.**
* **Architectural versus legacy-GA levels**: they are not assumed identical (different id conventions, adapter levels have `key = None`, and nothing merges them). A mapping *is possible*: an engineer sets a GA level's `key` to an architectural key through `set_value`, and the projection then joins plans to that level. But it is only a free field:
  the key is **not validated** (`"not a key!"` is accepted), **not unique** (two levels mapped to `FLOOR:1` are accepted and the projection silently attaches plans to the last one), and there is no mapping proposal, issue or helper. **Partial; see section 8.**

## G. Can Phase 4 introduce these safely?

| Phase 4 concept | Existing support | Missing |
|---|---|---|
| structural candidates | `EvidenceLink`s from approved observations; `InterpretationSet` with structured effects for "what could this be" | a registry and a target scope for candidates; an effect kind to create/accept a candidate |
| structural intent | decisions, value statuses and provenance apply to any target | the intent records themselves and their scope |
| support relationships | `Slab.supported_by` only | a general support relation record |
| member continuity | none | a continuity record (column stacks, beam runs) |
| transfer conditions | none | a transfer record |
| retained / demolished / existing / new | hints `column_candidate`/`beam_candidate` (proposals) | a **condition attribute on elements** (`Column` has none) or an intent record carrying it |
| engineer approval / rejection | full machinery (decisions, `review_*`, `link_evidence`, resolution effects) | review of candidates/intents specifically |
| mapping observations to structural objects | `EvidenceLink` + `trace()` | nothing for one observation supporting several *alternative* structural readings beyond an `InterpretationSet` on the observation |

## 8. Blockers and recommended pre-Phase-4 fixes

Ordered. 1-4 are small and are recommended **before** Phase 4 writes structural records on top of this; 5-6 are Phase 4 scope itself.

1. **Close the authority bypasses (C1-C4)**: `resolve_issue` needs an engineer decision for ERROR/BLOCKING (as `accept_issue` does); `set_value_status` must refuse to change a value's status without provenance or a decision when it would improve trust (ASSUMED/INFERRED to SOURCE/DERIVED);
   `save()` should validate; `establish_levels` should require a decision (or mark its levels clearly as an Oracle proposal that readiness treats as unconfirmed).
2. **Make the multi-source defaults safe**: with more than one source, `project.architecture`, `suggest_elevations()`, `establish_levels()` and `render_report()` must require a source (or report all), never silently take the first.
3. **Validate and constrain the level mapping**: validate `Level.key` with the level-key grammar, enforce uniqueness (or model an explicit many-to-one relation), and give the mapping a proposal/issue and a decision path (an `EffectKind.SET_VALUE` on `key` already fits).
4. **Record the revision relation** (a typed `revision_of` between sources) before any structural decision may rest on evidence from two sources, and surface conflicting heights across revisions as an issue.
5. **Extend the approved projection** with the missing concepts in section E (grid axes, wall segments, per-observation level, orientation) *as domain concepts*; this needs interpretation work (grid labels are already read, wall centre lines are not).
6. **Phase 4 schema (0.5.0)**: a registry and target scope for structural candidates and intents, an element condition (existing/retained/demolished/new), general support/continuity/transfer records, and an effect kind for accepting a candidate.
