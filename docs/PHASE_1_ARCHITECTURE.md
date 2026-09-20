# Oracle v2 — Phase 1: Engineering Core Foundation

Status: Phase 1 complete. The Phase 2 legacy GA adapter is described in [PHASE_2_ADAPTER.md](PHASE_2_ADAPTER.md), and the
schema-0.2.0 additions to the core (provenance, value status, interpretations, decision authority, readiness) in
[CORE_EVIDENCE_MODEL.md](CORE_EVIDENCE_MODEL.md). This document describes the original 0.1.0 design; where it says
"schema 0.1.0" the current version is 0.4.0 (0.2.0 added the evidence model; 0.3.0 added the architectural interpretation, see
[PHASE_3_ARCHITECTURAL_INTERPRETATION.md](PHASE_3_ARCHITECTURAL_INTERPRETATION.md); 0.4.0 hardened its boundary, see
[PHASE_3_5_HARDENING.md](PHASE_3_5_HARDENING.md)).

## 1. Current architecture (audit findings)

Oracle today is a flat folder of Python scripts (about 5,300 lines) with no package structure, no
domain model and no automated test suite. Modules share data by reading and writing JSON files in
`output_json/` and by passing loosely structured dictionaries. The Tkinter wizard holds the live
project state in one `self.data` dict.

| Concern | Module(s) | What it does / how it holds data |
|---|---|---|
| Configuration | `config.py`, `company_standards.json`, `.env` | Paths, ODA converter path, API-key handling; detailing standards (BS 8110 / BS 8666 / BS 4449 cover, bar weights, mark format). Creates output folders and prints on import. |
| Simple DXF parsing | `dxf_parser.py` | Walls/columns/gridlines from fixed layer names into a dict (`*_parsed.json`). Circle columns only. |
| Multi-floor GA parsing | `ga_dxf_parser.py` (888 lines) | Detects levels from layer names (`COLUMN a-b`, `<tag> BEAMS`), aligns floors, builds joints/members via `JointRegistry`, detects slab panels, excludes VOID regions, writes a STAAD `.std` text. Reports problems as `ParseIssue(kind="warning"\|"blocking", message)`. Sections/loads are constants (`DEFAULT_SIZES`, `SLAB_THICKNESS_MM`, `MATERIAL`). Units: mm in, metres out. |
| AI layout generation | `claude_ga_generator.py` | Prompts Claude for a GA (columns/beams/slabs) as JSON → `ga_output.json`. Engineer notes are appended to the prompt as text. |
| Analysis | `staad_v8i_integration.py` (real, 32-bit COM + `.std` patching), `staad_integration.py` (older OpenSTAAD path), `staad_mock.py` (tributary-area estimate) | Produce `staad_results.json` (node/member forces). |
| Design | `design_module.py` | Sends GA + forces to Claude to size members and produce reinforcement → `design_output.json`. Section/bar data is strings such as `"225x450"`, `"4Y16"`. |
| Reinforcement / BBS | `lisp_detail_generator.py` | Parses those strings, builds column/beam/steel schedules, `MarkRegistry`, bar-mark labels, `bbs_output.json`, AutoLISP output. |
| Drawing | `dwg_detail_generator.py`, `ga_sketch.py`, `ml_sketch.py` | `ezdxf` detail drawing (+ ODA → DWG); matplotlib previews. |
| Orchestration / GUI | `oracle_wizard.py` (1,572 lines), `oracle_pipeline.py` | 8-step Tkinter wizard (the real workflow; runs work in threads, holds everything in `self.data`, including free-text `engineer_notes`, `element_notes`, `chat_instructions`); `oracle_pipeline.py` is an older hard-coded CLI pipeline. |
| Logging | `oracle_log.py` | Append-only text log, fed back into the chat context. |

Dependencies: `ezdxf`, `anthropic`, `matplotlib`, `shapely`, `pywin32` (STAAD only). Launch: `Launch Oracle.bat`
→ `pythonw oracle_wizard.py`.

Observations that motivated the core:

- The same building is represented four incompatible ways: parsed DXF dict, `ga_output.json`
  (`{columns: [{name,x,y}], beams: [{start_col,end_col,depth_mm}], slabs: [...]}`, single storey, metres),
  the multi-floor `model` dict (joint numbers and tuples, `levels` as tag strings), and STAAD text.
- Levels are strings (`"G"`, `"1"`, `"R"`) plus a separate `{tag: elevation_m}` dict; a level is not an object.
- The design basis is scattered across `company_standards.json`, wizard fields (`concrete_grade`,
  `exposure_class`, `occupancy`), and constants in `ga_dxf_parser.py`.
- Engineer notes are strings concatenated into Claude prompts; nothing records who decided what, why,
  or whether the engineer overrode something Oracle proposed.
- Problems are `ParseIssue` objects that exist only for the duration of one screen.
- Units mix mm (DXF parser) and metres (GA JSON, STAAD).
- The only existing test artefact is `test_conversion.py`, a script that opens a DXF and prints layers.

## 2. New core architecture

A new package, `oracle/core/`, sits **alongside** the legacy scripts. It imports nothing from them and
nothing from Tkinter, `anthropic`, STAAD/COM, `ezdxf` or matplotlib (standard library only). Phase 1 itself
modified no existing file. (The later repository cleanup, see `REPOSITORY_INVENTORY.md`, added documentation
headers to the legacy modules; it changed no code.)

```
 ┌────────────────────────────── LEGACY (unchanged, still the running app) ─────────────────────────────┐
 │  oracle_wizard.py (Tkinter) ── oracle_pipeline.py                                                     │
 │      │                                                                                                │
 │      ├─ dxf_parser.py / ga_dxf_parser.py ──► JSON files & dicts ──► staad_*.py ──► design_module.py   │
 │      ├─ claude_ga_generator.py (Claude)                                    (Claude)                   │
 │      └─ lisp_detail_generator.py ─► dwg_detail_generator.py ─► DXF/DWG                                │
 └───────────────────────────────────────────────────────────────────────────────────────────────────────┘
                     │  (Phase 2+: adapters read/write the core; nothing does so yet)
                     ▼
 ┌──────────────────────────────────────── oracle/core  (NEW) ───────────────────────────────────────────┐
 │                                                                                                       │
 │   OracleProject ──┬─ metadata, schema_version, created/modified                                        │
 │   (project.py)    ├─ DesignBasis        (design_basis.py)   codes, materials, cover, loading, wind…    │
 │                   ├─ BuildingModel      (building.py)       Level · GridLine · Node                    │
 │                   │     └─ elements     (elements.py)       Column Beam Slab Wall Stair Foundation     │
 │                   │                                          Opening   (+ Section)                     │
 │                   ├─ EngineeringDecision[] (decisions.py)   who decided what, why, status              │
 │                   └─ EngineeringIssue[]    (issues.py)      severity, status, resolution, decision     │
 │                                                                                                       │
 │   geometry.py: Point2D, Polygon2D        common.py: ValidationError, IDs, Target, checks               │
 │                                                                                                       │
 │   JSON  ◄── to_dict()/from_dict() ──►  Python objects   (schema_version "0.1.0")                      │
 └───────────────────────────────────────────────────────────────────────────────────────────────────────┘
        ▲ future adapters (each depends on the core, never the reverse)
        │  DXF importer · Claude proposals · STAAD writer/reader · design engine · BBS · drawing generator · GUI
```

### Module responsibilities

| Module | Responsibility |
|---|---|
| `common.py` | `ValidationError`, `SchemaVersionError`, `SCHEMA_VERSION`, ID rules, `Target` (project / level / element), numeric/text/enum/timestamp validators, strict key checking for deserialisation. |
| `geometry.py` | `Point2D`, `Polygon2D` (rejects <3 vertices, repeated/coincident vertices, zero area, self-intersection). Millimetres. |
| `elements.py` | `Section` (rectangular / circular / standard-designation) and the seven element kinds. Each element validates itself; cross-references are checked by `BuildingModel`. |
| `building.py` | `Level` (ordered by elevation, index assigned), `GridLine`, `Node`, and `BuildingModel`, which owns them, enforces uniqueness and reference integrity, and provides geometric queries (`elements_on_level`, member length/orientation). |
| `design_basis.py` | `DesignBasis` plus `LevelLoading`, `WindBasis`, `SeismicBasis`. Required: design code, concrete grade, reinforcement grade. Everything else is `None` until the engineer defines it; `missing_items()` lists what is open. No code equations. |
| `decisions.py` | `EngineeringDecision` with source, target, category, status, override flag, supersession. |
| `issues.py` | `EngineeringIssue` with severity, category, status, resolution, linked decision. |
| `project.py` | `OracleProject`: identity, metadata, owns the above, enforces project-level integrity (decision/issue targets exist, referenced decisions exist), JSON load/save. |

### Key modelling choices

- **Units are explicit.** Lengths are millimetres (field names end `_mm`), elevations are mm relative to the
  project datum, loads kN/m², strengths N/mm². The legacy GA JSON is in metres; adapters convert.
- **Levels are objects.** A level has an ID, name, elevation, optional storey height and an `index` derived
  from elevation order (so basements at negative elevation work). If a level declares a storey height it
  must agree with the elevation step to the next level.
- **Nodes.** Beams and walls run between `Node`s on their level (matching how the STAAD path already thinks
  in joints). Columns are located by point and span `lower_level_id` → `upper_level_id`, so a column can
  legitimately pass several storeys.
- **One element ID namespace.** "C12" identifies exactly one element whatever its kind, so decisions and
  issues can target it unambiguously. Levels, nodes and grids have their own namespaces; `Target` carries a scope.
- **Decisions are records, not prompts.** Only an `ENGINEER` source may set `overrides_recommendation`;
  `AI_ASSISTANT` and `ORACLE` sources can only propose. Statuses: proposed, accepted, rejected, overridden,
  superseded (supersession must name an existing decision).
- **Issues close with a paper trail.** A resolved/accepted issue needs a note; accepting an `error` or
  `blocking` issue requires the engineer decision that accepts it.
- **Nothing is defaulted for the engineer.** The design basis does not fill in "typical" values.
  Optional fields stay `None` and show up in `missing_items()`.
- **Strict, versioned JSON.** `schema_version` (`"0.1.0"`, independent of the Oracle app version, which is
  recorded separately as `oracle_version`) must match on load. Unknown fields are rejected rather than
  silently dropped. Output is deterministic (fixed field order; elements grouped by kind), and
  `to_json(from_json(x)) == x`. Saves are atomic (temp file + replace). Loading does not change `modified_at`.
- **Validate on add, and again on demand.** `add_*` methods reject invalid data before mutating, so a failed
  add leaves the model unchanged; `validate()` re-checks everything (used after load and after in-place edits).

## 3. What remains in the legacy workflow (unchanged)

Everything. The wizard, both DXF parsers, Claude GA/design generation, STAAD integration and mock,
BBS/LISP generation, drawing generation, `config.py`, `company_standards.json`, logging and the launch
script are functionally unchanged and remain the only path the running application uses. Phase 1 adds no import from
the app into the core or vice versa.

## 4. How existing modules will eventually connect to the core

| Legacy artefact | Core equivalent | Notes for the adapter |
|---|---|---|
| `ga_dxf_parser` levels (`["G","1","2","R"]`) + `storey_heights_m` | `Level` list (ids from tags, elevation ×1000) | Keep DXF layer tag as the level ID. |
| `JointRegistry` joints, members `(no, j1, j2, "beam", level, size_key)` | `Node` (per level) + `Beam` / `Column` | Joint number → node ID; snapping stays in the parser. |
| `DEFAULT_SIZES`, `SLAB_THICKNESS_MM`, `MATERIAL` | `Section`, `Slab.thickness_mm`, `DesignBasis` | Today's constants become explicit basis entries the engineer can see and change. |
| Slab panels + VOID centroids | `Slab.boundary`, `Opening`, `Slab.opening_ids` | |
| `ParseIssue("warning"\|"blocking", msg)` | `EngineeringIssue` (`WARNING` / `BLOCKING`) | Add a category and target when known. |
| `ga_output.json` (`columns/beams/slabs`, metres) | `Column` / `Beam` / `Slab` | Beam `start_col`/`end_col` → nodes at the column positions. |
| Wizard `occupancy`, `concrete_grade`, `exposure_class`, `storey_height_m` | `DesignBasis` (`LevelLoading`, grades, `exposure_class`, `default_storey_height_mm`) | |
| `engineer_notes`, `element_notes`, `chat_instructions` | `EngineeringDecision` (engineer source; element notes get `Target.element`) | Claude prompts are then *rendered from* accepted decisions instead of being the store. |
| `company_standards.json` | `DesignBasis` (cover, codes) now; a detailing-standards object in a later phase | |
| STAAD results / `design_output.json` / `bbs_output.json` | Not modelled yet | Phase 2+ (analysis, design, reinforcement results). |

## 5. Explicit architectural decisions

1. Separate package, no edits to existing files: zero regression risk to the running app.
2. Standard library only; the core can be tested without Claude, STAAD, AutoCAD, ezdxf or a display.
3. Hand-written `to_dict`/`from_dict` instead of reflection or a schema library: explicit, deterministic,
   no new dependency, and every field is visible when the schema changes.
4. Plain dataclasses and enums; a small number of classes (no factories, registries, event bus or DI).
5. Free-text engineering identifiers (design code, grades, exposure class, materials) rather than fixed
   enums, because the engineer chooses these (BS or Eurocode, project-specific grades).
6. Claude is never the authority: the core has no AI dependency, and `AI_ASSISTANT`-sourced decisions cannot
   override a recommendation.
7. Tests use `unittest` (no pytest dependency added to `requirements.txt`).

## 6. Known limitations

- No analysis, design, reinforcement, detailing, drawing, revision or architectural-source records yet.
  Their top-level keys do not exist in schema 0.1.0.
- Plan geometry only (2D polygons and points per level); no sloping or curved members, no cross-level
  geometry beyond column/stair spans.
- Validation is structural and referential, not engineering: it does not check that slab boundaries close on
  their supports, that openings lie inside slabs, that columns coincide with nodes, or that spans are plausible.
  Those are `EngineeringIssue` producers for a later phase.
- Slab support and foundation-to-column links are plain ID lists with no load-path semantics.
- Elements and design basis are mutable dataclasses; in-place edits are only caught by `validate()`.
- `modified_at` is updated by project and building `add_*` methods, not by direct field assignment.
- Schema loading accepts exactly `0.1.0`; there is no migration machinery yet.
- IDs: 1–64 characters, letters/digits/`_ - .`, starting alphanumeric. Legacy layer tags with spaces need mapping.
- No concurrency or multi-user handling; one project per file.

## 7. Future migration path

1. **Phase 2 – importers.** Build `ga_dxf_parser` results and `ga_output.json` into an `OracleProject`
   (adapter module outside `oracle/core`), converting units, and turn `ParseIssue`s into `EngineeringIssue`s.
   Run the core and legacy paths side by side and compare.
2. **Wizard writes the core.** Have the wizard populate an `OracleProject` in parallel with `self.data`,
   save it next to the outputs, and store notes as `EngineeringDecision`s; Claude prompts read from decisions.
3. **Results in the core.** Add analysis, design and reinforcement result models (schema 0.2.0, with the first
   real migration), driven by deterministic, testable engineering modules.
4. **Adapters become thin.** STAAD writer/reader, BBS and drawing generators take core objects as input.
5. **Retire dict-passing.** Once every stage reads the core, remove the duplicated legacy representations.

## 8. Tests

`python -m unittest discover -s tests -t .` runs the Phase 1 suite (project, building/levels, elements
and geometry, decisions/issues, design basis, JSON round-trip). Tests are deterministic and need no
network, API key, STAAD.Pro or AutoCAD.
