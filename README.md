# Oracle V2

Oracle automates parts of a structural-engineering workflow for reinforced-concrete and steel
buildings, working to British Standards: read a drawing, produce a structural layout, run analysis, size
members, and produce reinforcement schedules and a detail drawing. It is being developed for
professional Nigerian structural-engineering practice. The engineer is the decision-maker; Oracle and
Claude propose.

This file is for developers. End users should read [HOW TO USE.md](HOW%20TO%20USE.md).

## Current status

Oracle V2 is a transition, and the repository holds two things side by side:

1. **The legacy application** (Python scripts at the repository root). It works today and is the
   only thing the launcher runs. It is a prototype: modules pass loosely structured dicts and JSON files
   to each other, and Claude produces the layout and the member design.
2. **The V2 engineering core** (`oracle/core/`), Phase 1. A validated, serialisable domain model of a
   project: building (levels, nodes, columns, beams, slabs, walls, stairs, foundations, openings), design
   basis, engineering decisions, engineering issues, saved as versioned JSON. **Nothing in the legacy
   application uses it yet.** No DXF adapter, analysis, design or drawing code has been connected to it.

Phase 2 adds `oracle/adapters/`: an adapter that translates the legacy multi-floor GA parser's output into the
core (see [docs/PHASE_2_ADAPTER.md](docs/PHASE_2_ADAPTER.md)). **It is not connected to the wizard yet.

Phase 3.5 hardened that boundary before any structural reasoning is built on it: an accepted interpretation now actually changes the model and resolves its
issues; a structural object can be linked, by a typed and validated evidence link, to approved architectural evidence; `project.trace(...)` walks that chain back to the
source drawing and its entity identifiers and reports exactly what is missing; `project.approved_architecture()` is a read-only, domain-level, millimetre projection that the
structural side can consume without knowing anything about CAD; the domain model no longer carries CAD vocabulary; observations describe what a drawing shows and never assert a
structural meaning; a level's identity is not its label and its elevation says what kind it is. See
[docs/PHASE_3_5_HARDENING.md](docs/PHASE_3_5_HARDENING.md).** The core
(schema 0.4.0) also keeps provenance, the trust status of each value, alternative interpretations, decision history
and a readiness gate, so an assumed value or an open blocking issue can never pass as a confirmed fact (see
[docs/CORE_EVIDENCE_MODEL.md](docs/CORE_EVIDENCE_MODEL.md)). Old 0.1.0 and 0.2.0 projects still load.
Migration is planned in [docs/PHASE_1_ARCHITECTURE.md](docs/PHASE_1_ARCHITECTURE.md).

Phase 3 adds the **architectural drawing intelligence layer** (`oracle/ingestion/`, `oracle/interpretation/`): it reads a
real architectural DWG/DXF and proposes what the drawing contains (views, levels, sections, elevations, units, layer meanings,
architectural observations) with evidence, confidence and alternatives, and turns every disagreement into an open question
and an issue instead of a silent choice. It designs nothing and creates no building until an engineer decides. See
[docs/PHASE_3_ARCHITECTURAL_INTERPRETATION.md](docs/PHASE_3_ARCHITECTURAL_INTERPRETATION.md). Try it (read only):

```
python -m oracle.interpretation "input_dwgs/some drawing.dwg" --engineer "Your Name" --project out.oracle.json --report out.txt
```

**It is a proposal engine, not an oracle of truth**: on real drawings it will leave views unknown and ask the engineer to confirm.

It is exposed in the wizard as the **Architectural Drawing** workflow (`oracle/application/`, `oracle/ui/`): press *Architectural Drawing...* on the
welcome screen (or run `python oracle_wizard.py --architectural`), choose a DWG/DXF, watch Oracle interpret it, review what it understood, make engineer
decisions, and save/reopen the project. It is separate from the eight structural steps, which are unchanged. See
[docs/ARCHITECTURAL_WORKFLOW_UI.md](docs/ARCHITECTURAL_WORKFLOW_UI.md).

## Layout

```
oracle-v2/
  oracle/                  the Python package (NOT a copy of the repo)
    core/                  engineering domain model (V2), standard library only
    adapters/              import adapters into the core (Phase 2: legacy GA parser)
    ingestion/             DWG/DXF -> neutral DrawingDocument (Phase 3; the only ezdxf reader)
    interpretation/        architectural drawing intelligence (Phase 3): views, levels, units, layers, reconciliation, CLI
    application/           service layer for the architectural workflow (session, read models, preview model); no toolkit, no CAD library
    ui/                    Tkinter architectural workspace (imports oracle.application only)
  tests/                   unit tests for oracle.core; fixtures/legacy_samples/ has reference JSON
  docs/                    architecture and repository inventory
  input_dwgs/              sample drawings used by the legacy app and as fixtures
  *.py (root)              the legacy application (see docs/REPOSITORY_INVENTORY.md for each file)
  company_standards.json   detailing standards read by the legacy code
  Launch Oracle.bat        starts the legacy wizard
```

Every Python file begins with a header stating its purpose, role, dependencies, consumers, status and
migration direction. New source files must have one.

The legacy flow, as it exists today:

```
oracle_wizard.py (Tkinter)
  drawing -> dxf_parser.py | ga_dxf_parser.py
          -> claude_ga_generator.py (single floor) -> analysis: staad_v8i_integration.py | staad_mock.py
          -> design_module.py (Claude) -> lisp_detail_generator.py -> dwg_detail_generator.py
```

The multi-floor path builds a model from an already-designed GA and uses default member sizes. The
single-floor path asks Claude to lay out the structure from an architectural drawing.

## Running the current application

Requires Python 3.11 or newer and `pip install -r requirements.txt`.

```
Launch Oracle.bat            # double-click; or:
python oracle_wizard.py      # same wizard, with a console
```

The first run asks for an Anthropic API key and stores it in a local `.env` file.

Optional external software:

- **STAAD.Pro V8i SS6** plus a 32-bit Python 3.11 at `C:\Python311-32\python.exe` with `pywin32`, for real
  analysis. Without it the wizard uses a tributary-area estimate.
- **ODA File Converter**, to read `.dwg` input and write `.dwg` output.
- **AutoCAD**, or any DWG/DXF viewer, to view results.

Setup details are in [HOW TO USE.md](HOW%20TO%20USE.md).

## Tests

```
python -m unittest discover -s tests -t .      # the everyday suite: unit + integration, no real drawing (about 20-45 s)
python -m tests unit                            # the fastest tier only (about 1 s)
python -m tests slow                            # the real architectural drawing and the interface workflow on it (about 80 s; needs the ODA converter)
python -m tests all                             # complete regression: every tier (about 1-2 min)
```

The tests are deterministic and need no Claude API, STAAD.Pro, AutoCAD or network (the slow and release tiers use the free ODA File Converter, and are
skipped with a message if it is missing). They are split into four tiers (unit, integration, slow, release) selected by one mechanism, `ORACLE_TESTS` /
`python -m tests`; nothing is removed or skipped to make the default fast. All commands, the tier of every test class and the timings are in
[docs/TESTING.md](docs/TESTING.md). There is **no automated test of the legacy application**: `test_conversion.py` is a manual smoke script that prints a
DXF's layers, and the legacy workflow has only been checked by hand.

## Security: `.env`

`.env` holds a live API credential. It is gitignored and must never be committed, shared, pasted into a
chat or copied into documentation. `.env.example` lists the variable name with no value. Delete `.env`
before giving anyone a copy of the folder.

## Current limitations

- The V2 core is not connected to the running application.
- Claude currently proposes the layout and produces the member design and reinforcement in the legacy
  code. Those results are not deterministic and are not checked by an independent design engine. Do not
  rely on them as final design.
- Multi-floor import uses standard default member sizes and default loading unless the engineer supplies
  them; slab panels are detected only for rectilinear bays.
- Reinforcement is carried as text strings; BS 8666 bar shapes are not modelled.
- No revision tracking, licensing, installer or multi-user support.
- Legacy generated folders (`output_json/`, `output_dwg/`, `output_lisp/`, `output_staad/`, `logs/`) are
  created at run time and are gitignored.
