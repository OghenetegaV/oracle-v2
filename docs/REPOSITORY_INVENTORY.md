# Repository Inventory

Produced by a repository audit before any cleanup. "Used By" is taken from an AST import search of
every root module plus reads of the code and docs, not from filenames. Two kinds of removal were
made: Python caches and generated runtime files. No source file was deleted.

Git state when audited: every file below except those marked *untracked* is tracked. Everything under
`output_*/` and `logs/` was already gitignored and untracked, so it was not recoverable from Git; it was
backed up before removal (see section 4).

Actions: RETAIN, MOVE, REMOVE, GITIGNORE, REVIEW (kept, needs a human decision).

## 1. Source and configuration files

### V2 core (new, Phase 1)

| File | Category | Purpose | Used By | Status | Action |
|---|---|---|---|---|---|
| `oracle/__init__.py` | Core | Package root; `__version__` | `oracle.core.project`, tests | Active | RETAIN |
| `oracle/core/__init__.py` | Core | Public API re-exports | tests | Active | RETAIN |
| `oracle/core/common.py` | Core | Errors, IDs, `Target`, validators, `SCHEMA_VERSION` | all other core modules | Active | RETAIN |
| `oracle/core/geometry.py` | Core | `Point2D`, `Polygon2D` | `elements`, `building` | Active | RETAIN |
| `oracle/core/elements.py` | Core | `Section` and the 7 element kinds | `building`, `design_basis` | Active | RETAIN |
| `oracle/core/building.py` | Core | `Level`, `GridLine`, `Node`, `BuildingModel` | `project` | Active | RETAIN |
| `oracle/core/design_basis.py` | Core | `DesignBasis` and loading/wind/seismic | `project` | Active | RETAIN |
| `oracle/core/decisions.py` | Core | `EngineeringDecision` | `project` | Active | RETAIN |
| `oracle/core/issues.py` | Core | `EngineeringIssue` | `project` | Active | RETAIN |
| `oracle/core/project.py` | Core | `OracleProject` aggregate, JSON load/save | tests | Active | RETAIN |

The inner `oracle/` directory is the Python package, not a copy of the repository.

### Tests and docs (new)

| File | Category | Purpose | Used By | Status | Action |
|---|---|---|---|---|---|
| `tests/__init__.py` | Test | Package marker for `unittest discover -t .` | test runner | Active | RETAIN |
| `tests/fixtures.py` | Test | Shared model builders (3x3 columns, 2 storeys) | all core tests | Active | RETAIN |
| `tests/test_project.py`, `test_building.py`, `test_elements.py`, `test_decisions_issues.py`, `test_design_basis.py` | Test | 67 unit tests of the core | test runner | Active | RETAIN |
| `oracle/core/provenance.py`, `value_status.py`, `interpretations.py`, `readiness.py`, `migrations.py` | Core | Schema 0.2.0: provenance, value status, alternative interpretations, readiness gate, project-file migration | `project`, adapters, tests | Active | RETAIN |
| `tests/fixtures/schema_0_1_0_project.json` | Sample Input | A genuine schema-0.1.0 project written by the Phase 1 code (tag `v2.0.0-phase1`), for the migration tests | `tests/test_migration_readiness.py` | Reference | RETAIN |
| `tests/test_provenance_status.py`, `test_decision_authority.py`, `test_interpretations.py`, `test_migration_readiness.py`, `test_adapter_evidence.py` | Test | Core evidence model and adapter provenance tests | test runner | Active | RETAIN |
| `docs/CORE_EVIDENCE_MODEL.md` | Documentation | Provenance, value status, interpretations, decision authority, readiness, migration | readers | New | RETAIN |
| `oracle/adapters/__init__.py`, `result.py`, `legacy_ga.py` | Adapter | Phase 2: translate `ga_dxf_parser` output into `oracle.core` (result/provenance types; the adapter) | `tests/test_adapter_legacy_ga.py`; not yet the wizard | Active, not connected to the app | RETAIN |
| `tests/test_adapter_legacy_ga.py` | Test | 43 integration/regression tests on the real GA fixture (see `docs/PHASE_2_ADAPTER.md`) | test runner | Active | RETAIN |
| `docs/PHASE_2_ADAPTER.md` | Documentation | Legacy parser audit, mappings, gaps, future architectural-DWG boundary | readers | New | RETAIN |
| `tests/test_generate_test_dwg.py` | Test | 3 regression tests: importing `generate_test_dwg` must not write a DXF; `main()` still builds the same drawing (uses a temp dir; the real fixture is snapshotted and restored) | test runner | Active | RETAIN |
| `tests/fixtures/legacy_samples/*.json` (4) + `README.md` | Sample Input | Snapshots of legacy `ga_output`, `staad_results`, `design_output`, `bbs_output` (see its README) | nothing yet; reference for a future adapter | Reference | RETAIN (copied from `output_json/`) |
| `docs/PHASE_1_ARCHITECTURE.md` | Documentation | Phase 1 architecture | readers | Active | RETAIN |
| `docs/REPOSITORY_INVENTORY.md` | Documentation | This file | readers | Active | RETAIN |
| `README.md` | Documentation | Developer overview and how to run/test | readers | New | RETAIN |
| `.env.example` | Configuration | Placeholder variable name only, no value | humans | New | RETAIN |

### Phase 3 and 3.5: architectural drawing intelligence and its hardened boundary (new; schema 0.4.0)

| File | Category | Purpose | Used By | Status | Action |
|---|---|---|---|---|---|
| `oracle/core/architecture.py` | Core | Interpretation model: `DrawingSource`, `UnitEstimate`, `CoordinateFrame`, `LayerClassification`, `DrawingView`, `ArchitecturalObservation`, `HeightEvidence`, `CrossViewFinding`, container | `oracle.core.project`, `oracle.interpretation`, tests | Active | RETAIN |
| `oracle/core/common.py`, `migrations.py`, `readiness.py`, `project.py`, `__init__.py` | Core | Modified for schemas 0.3.0 and 0.4.0: `SCHEMA_VERSION`, `ARCHITECTURAL` target scope, migrations `0.2.0 -> 0.3.0 -> 0.4.0`, unreviewed-view blocker, review/merge/split, `set_value` for architectural objects and levels, `architectures` and `evidence_links` registries, `link_evidence`, `trace`, `approved_architecture`, resolution on `accept_interpretation`, exports | everything | Active | RETAIN |
| `oracle/ingestion/__init__.py`, `drawing.py`, `dxf_reader.py` | Ingestion | DWG (via ODA) / DXF -> neutral `DrawingDocument` (the only ezdxf reader in Phase 3) | `oracle.interpretation`, tests | Active, not connected to the app | RETAIN |
| `oracle/interpretation/naming.py`, `layers.py`, `units.py` | Interpretation | Level-name normalisation and title classification (configurable), layer semantic classification, unit detection | `pipeline`, tests | Active | RETAIN |
| `oracle/interpretation/segmentation.py`, `vertical.py`, `observations.py`, `alignment.py`, `reconcile.py` | Interpretation | View segmentation and classification, section/elevation level evidence, architectural observations, plan alignment, cross-view reconciliation | `pipeline`, tests | Active | RETAIN |
| `oracle/interpretation/pipeline.py`, `report.py`, `advisor.py`, `__init__.py`, `__main__.py` | Interpretation | Orchestration into an `OracleProject` (also `align_view`, `establish_levels`, `suggest_elevations`), text report, bounded AI-advice hook, CLI | tests, engineers | Active, not connected to the app | RETAIN |
| `tests/drawing_factory.py` | Test support | Builds synthetic architectural DXFs with known content | Phase 3 tests | Active | RETAIN |
| `tests/test_interpretation_scenarios.py`, `test_engineer_review.py`, `test_architecture_core.py`, `test_naming_and_layers.py`, `test_ingestion_and_units.py`, `test_view_analysis.py` | Test | Phase 3 tests (the 15 scenarios, engineer review, core model, naming/layers, ingestion/units, view analysis) | test runner | Active | RETAIN |
| `tests/test_real_architectural_drawing.py` | Test | Slow integration test on the real squash-court drawing (tier `slow`: not in the default run; `python -m tests slow`; skipped without the file or ODA) | test runner | Active | RETAIN |
| `tests/test_migration_readiness.py` | Test | Modified: literal versions updated (0.3.0, then 0.4.0); added the 0.2.0 migration tests | test runner | Active | RETAIN |
| `tests/fixtures/schema_0_2_0_project.json` | Sample Input | A genuine schema-0.2.0 project written by the pre-Phase-3 code, for the migration test | `tests/test_migration_readiness.py` | Reference | RETAIN |
| `docs/PHASE_3_ARCHITECTURAL_INTERPRETATION.md` | Documentation | Phase 3 architecture, method, limitations, real-drawing results (superseded in part by Phase 3.5) | readers | New | RETAIN |
| `oracle/core/effects.py`, `resolution.py` | Core | Structured, validated interpretation effects and their application under an engineer decision (schema 0.4.0) | `interpretations`, `project`, `oracle.interpretation`, tests | Active | RETAIN |
| `oracle/core/evidence.py` | Core | `EvidenceLink`: typed link from a domain object to approved architectural evidence (schema 0.4.0) | `project`, `trace`, tests | Active | RETAIN |
| `oracle/core/trace.py` | Core | `trace()`: the evidence chain backwards, with explicit gaps | `project`, tests | Active | RETAIN |
| `oracle/core/approved.py` | Core | The read-only approved architecture projection (domain terms, millimetres, no CAD vocabulary) | `project`, the future structural layer, tests | Active | RETAIN |
| `oracle/core/level_changes.py` | Core | Read-only planning of what changing one level means for the others | `project`, tests | Active | RETAIN |
| `oracle/core/building.py`, `interpretations.py`, `issues.py`, `architecture.py` | Core | Modified for 0.4.0: level identity/label/elevation-type model and `replace_levels`; effects on alternatives; issue -> set link; format-neutral `DrawingSource` with revision and interpretation identity, `NAMED:` level keys, renamed hints | `project`, tests | Active | RETAIN |
| `oracle/ingestion/drawing.py`, `dxf_reader.py` | Ingestion | Modified: the format's unit code and non-plotting layers are translated here (`declared_unit`, `LayerInfo.non_plotting`, `source_metadata()`) | `oracle.interpretation` | Active | RETAIN |
| `oracle/interpretation/*.py` | Interpretation | Modified: `interpret_into` (further sources), effects and linked issues on every set, `establish_levels` (level model, links), `suggest_elevations` (accepted heights, named levels), `OBSERVATION_VOCABULARY`, `column_symbol`/`beam_symbol`, format codes removed from `units`/`layers`/`report` | tests, engineers | Active | RETAIN |
| `tests/tiers.py`, `tests/__main__.py`, `tests/test_tiers.py`, `docs/TESTING.md` | Test infrastructure | The test-tier mechanism (unit / integration / slow / release; `ORACLE_TESTS` or `python -m tests`), its runner and its own tests, and the documentation of the commands and classification. `tests/__init__.py` carries the load hook | test runner, developers | Active | RETAIN |
| `oracle/application/__init__.py`, `files.py`, `session.py`, `review_models.py`, `preview.py` | Application | Interface phase: the thin service between an interface and the backend: file checking, `ArchitecturalSession` (interpretation in honest stages, open/save with a geometry sidecar, every engineer action as a domain decision), read models per tab, drawing-preview and overlay models. No toolkit, no CAD library | `oracle.ui`, tests | Active | RETAIN |
| `oracle/ui/__init__.py`, `architectural_workspace.py`, `review_panels.py`, `preview_canvas.py`, `dialogs.py`, `theme.py` | UI | Interface phase: the Architectural Drawing workspace (choose file, processing, review, decisions, save/open), embedded in the wizard window; imports `oracle.application` only | `oracle_wizard.py` (lazy), tests | Active | RETAIN |
| `oracle_wizard.py` | UI (legacy) | Modified minimally: a second entry button on the welcome step, `open_architectural_workflow` / `close_architectural_workflow`, `--architectural`, and `content_area` / `footer` kept as attributes. The eight structural steps are unchanged | you | Active | RETAIN |
| `oracle/interpretation/pipeline.py`, `__init__.py` | Interpretation | Modified, backward compatible: optional `progress(stage, "start"/"done")` callback and `STAGES` | `oracle.application` | Active | RETAIN |
| `tests/test_application_unit.py`, `test_application_workflow.py`, `test_ui_workspace.py`, `test_real_drawing_workflow.py` | Test | Interface-phase tests: file checks and small models (unit); service, decisions, save/reopen, multi-source, layer boundaries (integration); Tk workspace and wizard (integration); the real drawing through the service and canvas (slow) | test runner | Active | RETAIN |
| `docs/ARCHITECTURAL_WORKFLOW_UI.md` | Documentation | How to open the workflow, import another drawing, what each tab shows, limitations | readers | New | RETAIN |
| `tests/support35.py`, `test_levels_hardening.py`, `test_resolution_effects.py`, `test_evidence_trace_projection.py`, `test_boundary_and_schema.py` | Test | Phase 3.5 tests and their shared builders | test runner | Active | RETAIN |
| `tests/fixtures/schema_0_3_0_project.json` | Sample Input | A trimmed subset of a genuine schema-0.3.0 project written by the Phase 3 code, for the 0.3.0 -> 0.4.0 migration tests | `tests/test_boundary_and_schema.py` | Reference | RETAIN |
| `docs/PHASE_3_5_AUDIT.md` | Documentation | The final Phase 3.5 architecture audit before Phase 4: what holds, what is bypassable, what is missing, and the ordered blockers | readers | New | RETAIN |
| `docs/PHASE_3_5_HARDENING.md` | Documentation | What the Phase 3 review found, what changed, what is deferred, schema/migration, size profile | readers | New | RETAIN |

### Legacy application (root level, working)

| File | Category | Purpose | Used By | Status | Action |
|---|---|---|---|---|---|
| `oracle_wizard.py` | Legacy | Tkinter 8-step workflow, the working application | `Launch Oracle.bat` | Primary app. Uncommitted local edits present | RETAIN |
| `oracle_pipeline.py` | Legacy | Older CLI driver of the same phases | nothing imports it | Legacy, candidate for deprecation: superseded by the wizard; hard-coded `test_floor.dxf`, fixed report/timestamp. Owner decision: keep, do not modify | RETAIN |
| `config.py` | Legacy / Configuration | Paths, ODA discovery, API-key handling; creates dirs on import | 12 modules | Active | RETAIN |
| `oracle_log.py` | Legacy | Shared event log | `claude_ga_generator`, `design_module`, `oracle_wizard` | Active | RETAIN |
| `dxf_parser.py` | Legacy | Single-floor architectural DXF parser | `oracle_wizard`, `oracle_pipeline` | Active | RETAIN |
| `ga_dxf_parser.py` | Legacy | Multi-floor structural GA parser + STAAD file | `oracle_wizard` | Active. Uncommitted local edits present | RETAIN |
| `claude_ga_generator.py` | Legacy | Claude proposes the single-floor layout | `oracle_wizard`, `oracle_pipeline` | Active | RETAIN |
| `design_module.py` | Legacy | Claude sizes members/reinforcement | `oracle_wizard`, `oracle_pipeline` | Active | RETAIN |
| `staad_v8i_integration.py` | Integration | Real STAAD.Pro V8i SS6 analysis via COM (32-bit subprocess steps) | `oracle_wizard`, `oracle_pipeline` | Active | RETAIN |
| `staad_mock.py` | Legacy | Tributary-area analysis fallback | `oracle_wizard`, `oracle_pipeline` | Active | RETAIN |
| `staad_integration.py` | Legacy | Older openstaadpy route | nothing | Legacy, candidate for deprecation: unreferenced; `openstaadpy` not in requirements. Owner decision: keep, do not modify | RETAIN |
| `lisp_detail_generator.py` | Legacy | Schedules, BBS, LISP output | `dwg_detail_generator`, `oracle_wizard` | Active | RETAIN |
| `dwg_detail_generator.py` | Legacy | ezdxf detail drawing + ODA to DWG | `oracle_wizard` | Active | RETAIN |
| `ga_sketch.py` | Legacy | PNG sketch, AI single-floor layout | `oracle_wizard` | Active | RETAIN |
| `ml_sketch.py` | Legacy | PNG sketch, multi-floor model | `oracle_wizard` | Active; part of the application. Still **untracked** in Git, so it must be `git add`ed with the next commit. Not ignored. Unchanged | RETAIN |
| `generate_test_dwg.py` | Dev utility | Regenerates `input_dwgs/test_floor.dxf` when run as a script. Importing it now has no side effects (body moved into `main()` behind a `__main__` guard; behaviour when run is unchanged) | nothing; `tests/test_generate_test_dwg.py` | Dev utility | RETAIN (could move to `tools/` later) |
| `test_conversion.py` | Test (manual smoke script) | Opens `test_floor.dxf`, prints layers; no assertions | nothing | Legacy smoke script | RETAIN |
| `company_standards.json` | Configuration | Detailing standards, loads, cover, bar weights | `lisp_detail_generator`, `staad_mock`, `staad_v8i_integration`, `oracle_wizard` | Active | RETAIN |
| `Launch Oracle.bat` | Legacy | `pythonw oracle_wizard.py` | user | Active | RETAIN |
| `HOW TO USE.md` | Documentation | End-user guide | users | Active | RETAIN |
| `requirements.txt` | Configuration | Runtime dependencies | pip | Active | RETAIN |
| `.gitignore` | Configuration | Ignore rules | Git | Updated (see 5) | RETAIN |
| `.env` | Local-only | Holds the local Anthropic credential | `config.py` at run time | Gitignored, never committed, never in history | RETAIN (untouched, contents not read into any document) |

All legacy Python files now carry a module header (purpose, role, dependencies, consumers, status,
migration). No code was changed; this was checked by comparing the syntax tree (excluding docstrings)
of all 34 Python files before and after.

## 2. Sample inputs (`input_dwgs/`, all tracked)

| File | Size | Purpose | Used By | Action |
|---|---|---|---|---|
| `1st Flr, 2nd Flr and Roof GAs.dxf` | 1.1 MB | Real multi-floor structural GA; the fixture for `ga_dxf_parser` (4 levels, 213 joints, 129 members verified) | wizard multi-floor path | RETAIN |
| `test_floor.dxf` | 78 KB | Synthetic single-floor architectural drawing | `dxf_parser`, `oracle_pipeline`, `test_conversion` | RETAIN. Working copy differs from Git only in `$TDUPDATE` timestamps |
| `test_floor.dwg` | 21 KB | DWG version of the same drawing (presumably to test the ODA DWG-input path; not confirmed) | nothing in code | RETAIN |
| `test_multiple_floors.dwg` | 25 KB | DWG multi-floor test drawing (presumably an ODA-input test; not confirmed) | nothing in code | RETAIN (owner decision; do not modify) |
| `Truss.dxf` | 304 KB | A truss drawing; its parse produced 81 bytes of output, i.e. it does not fit either parser | nothing | RETAIN (owner decision; do not modify) |
| `Sample Architectural Drawings - Proposed Squash Court Extension.dwg` | ~9 MB | Real Revit-exported architectural sheet set (20 sheets, ~24,000 entities): the Phase 3 real-world fixture. *Untracked* until the owner commits it. Never modified | `tests/test_real_architectural_drawing.py`, `python -m oracle.interpretation` | RETAIN (do not modify) |

Nothing here was removed.

## 3. Runtime and generated directories (all gitignored, untracked)

Every one of these folders is created on demand by the code (`config.py` for `input_dwgs`, `output_dxf`,
`output_json`, `logs`; `dwg_detail_generator`, `lisp_detail_generator` and `staad_v8i_integration` for
`output_dwg`, `output_lisp`, `output_staad`), so a clean checkout works with them absent or empty.
Their contents were cleared; the empty folders remain. Nothing is deleted from Git, because none of it was tracked.

| Directory | Contents found | Verdict | Action |
|---|---|---|---|
| `output_json/` | 11 generated files: `ga_output`, `design_output`, `staad_results`, `bbs_output`, `staad_v8i_results`, `test_floor_parsed`, two near-empty `*_parsed.json`, `cortex_report.json` (pre-rename), two preview PNGs | Runtime output. The four contract files were kept as fixtures (see section 1); the rest are regenerable or obsolete | Contents removed; 4 files copied to `tests/fixtures/legacy_samples/` |
| `output_dwg/` | 5 files: `cortex_detail_drawing` (.dwg/.dxf, pre-rename), `oracle_detail_drawing` (.dwg/.dxf), `preview.png` | Generated | Contents removed |
| `output_lisp/` | `cortex_details.lsp` (pre-rename), `oracle_details.lsp` | Generated | Contents removed |
| `output_staad/` | 72 files, see below | Generated STAAD run files plus one-off probe scripts | Contents removed |
| `output_dxf/` | empty | Created by `config.py`; `OUTPUT_DXF_DIR` is imported only by `test_conversion.py` and no code writes there | Left in place, empty, by owner decision. Already gitignored |
| `logs/` | `oracle.log` (16 KB) | Runtime log, recreated on first event | Contents removed |
| `__pycache__/` (root, `oracle/`, `oracle/core/`, `tests/`) | `.pyc` caches | Never source | Removed (regenerate on run) |

### `output_staad/` in detail (72 files, all removed)

| Group | Files | Finding |
|---|---|---|
| Probe scripts `_probe*.py` (10) | COM API experiments on a 2-node model | One-off debugging. Hard-coded to the pre-rename `...\cortex\` folder. Their findings (which COM calls are unreliable) are recorded in the `staad_v8i_integration.py` docstring |
| Test scripts `_test_*.py` (8) | Trial runs of the build/patch/subprocess steps | One-off debugging; same hard-coded paths |
| `build_model.py`, `patch_model.py`, `parse_anl_results.py`, `check_results.py`, `get_reactions*.py` (9) | Earlier standalone versions of the geometry build, `.STD` patching, `.ANL` parsing and result reading | Superseded: the same functions exist in `staad_v8i_integration.py` (`build_geometry_com`, `patch_std_text`, `parse_reactions`, `parse_member_forces`); hard-coded to `...\cortex\` |
| `_test2/3/4.std`, `cortex_model.*` (18), `oracle_model.*` (22), `ora9AF9.sbk`, `_member_map.json` | STAAD model, analysis and backup files | Generated by STAAD.Pro runs; `oracle_model.*` and `_member_map.json` are rewritten by every run |

No file in the repository references any of these scripts.

## 4. Backup of removed files

Because the removed files were untracked, they cannot be restored from Git. All 91 were saved first to
`../oracle-v2-removed-runtime-files-backup.zip`, next to the repository folder and outside it. Delete the
zip once you are satisfied.

## 5. `.gitignore` changes

Added: `.env.*` with `!.env.example` (so variants of the secret file are ignored but the placeholder
is tracked), `*.tmp` (temporary files from the atomic project save), `.pytest_cache/`. Already present and
verified: `.env`, `__pycache__/`, `*.pyc`, `output_json/`, `output_dxf/`, `output_dwg/`, `output_staad/`,
`output_lisp/`, `logs/`, `input_dwgs/*.dwl`, `*.dwl2`.

`.env` was checked: it is ignored, has never been in Git history, and no `sk-ant-` style string appears in any
other file. Its value was not printed or copied.

## 6. Duplicate or overlapping functionality

Nothing was consolidated or removed on the strength of overlap alone.

| Area | Implementation A | Implementation B | Currently used | Assessment |
|---|---|---|---|---|
| DXF parsing | `dxf_parser.py`: architectural drawing, fixed layers, circle columns | `ga_dxf_parser.py`: structural multi-floor GA, layer-name pattern, joints/members/slabs | Both, by different wizard paths | Different jobs, not true duplicates. Both become adapters into `oracle.core.BuildingModel` |
| STAAD | `staad_v8i_integration.py`: real COM analysis | `staad_integration.py`: openstaadpy attempt; `staad_mock.py`: estimate | v8i (real) and mock (fallback). `staad_integration.py` unused | v8i should be canonical; mock stays as the fallback; `staad_integration.py` is REVIEW |
| Sketches | `ga_sketch.py`: AI single-floor layout | `ml_sketch.py`: parsed multi-floor model | Both | Same idea, different inputs; a candidate to merge after the core exists |
| Drawing / BBS | `lisp_detail_generator.py`: schedules, BBS, LISP | `dwg_detail_generator.py`: draws, imports the builders from the LISP module | Both | Layered rather than duplicated; not two copies of the same logic |
| Orchestration | `oracle_wizard.py` (GUI) | `oracle_pipeline.py` (CLI) | Wizard | Pipeline is stale; REVIEW |
| Logging | `oracle_log.py` | none | Yes | Single system |
| Configuration | `config.py` (but `staad_v8i_integration.py` defines its own `PROJECT_ROOT` and output paths, and `STANDARDS_PATH` is defined separately in `staad_mock.py`, `lisp_detail_generator.py` and `oracle_wizard.py`) | none | Yes | One config module, with some duplicated path constants. Not touched |
| Claude calls | `claude_ga_generator.py`, `design_module.py`, wizard chat | each makes its own client, retry and JSON-fence stripping (`_strip_json_fences` exists in both generators) | Yes | Real duplication of a small helper; left alone |
| Design/analysis in core | `oracle.core` (new) | legacy dict/JSON models | Legacy only, core unused by the app | Intended: legacy is wrapped by adapters over time |

## 7. Items needing human review

Decisions recorded by the owner after the first audit are marked (decided).

1. `ml_sketch.py` (decided: part of the application, keep unchanged). Still untracked in Git; it is not
   ignored, so `git add ml_sketch.py` with the next commit is all that is needed. Until then a fresh clone
   would silently lose the multi-floor preview.
2. Uncommitted local changes to `ga_dxf_parser.py`, `oracle_wizard.py` and `input_dwgs/test_floor.dxf`
   predate this cleanup and are left as they are. The `.dxf` diff is timestamps only. The two `.py` diffs are
   functional edits plus the header added by the cleanup.
3. `staad_integration.py` (decided: keep, do not modify). Legacy, candidate for deprecation: unreferenced.
4. `oracle_pipeline.py` (decided: keep, do not modify). Legacy, candidate for deprecation: stale and unlaunched.
5. `generate_test_dwg.py` (fixed): importing it no longer generates or overwrites a DXF. Moving it to a
   `tools/` folder is optional and later.
6. `input_dwgs/Truss.dxf` and `test_multiple_floors.dwg` (decided: keep, do not modify). Purpose still
   unconfirmed; no code uses them.
7. `output_dxf/` (decided: leave in place). `OUTPUT_DXF_DIR` still appears unused apart from
   `test_conversion.py`.
8. `staad_v8i_integration.py`'s docstring says `oracle_pipeline.py` shells out to the 32-bit interpreter, but
   the subprocess call is in `staad_v8i_integration.py` itself. Left as written (documentation only).
9. `HOW TO USE.md` still describes the old workflow only; that is correct for the current app.
10. Phase 3 engineering decisions (see the Phase 3 report and `docs/PHASE_3_ARCHITECTURAL_INTERPRETATION.md`): the unit of the squash-court
    drawing (file says inches, drawing is millimetres), which of its two elevations each of FIRST FLOOR and ROOF means, and whether it has 3 or 5 levels.
11. Legacy files use mixed line endings (some CRLF, some LF). Headers preserved each file's style. A
    `.gitattributes` would settle it; not added.
