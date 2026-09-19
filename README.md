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
core (see [docs/PHASE_2_ADAPTER.md](docs/PHASE_2_ADAPTER.md)). **It is not connected to the wizard yet.** The core
(schema 0.2.0) also keeps provenance, the trust status of each value, alternative interpretations, decision history
and a readiness gate, so an assumed value or an open blocking issue can never pass as a confirmed fact (see
[docs/CORE_EVIDENCE_MODEL.md](docs/CORE_EVIDENCE_MODEL.md)). Old 0.1.0 projects still load.
Migration is planned in [docs/PHASE_1_ARCHITECTURE.md](docs/PHASE_1_ARCHITECTURE.md).

## Layout

```
oracle-v2/
  oracle/                  the Python package (NOT a copy of the repo)
    core/                  engineering domain model (V2), standard library only
    adapters/              import adapters into the core (Phase 2: legacy GA parser)
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
python -m unittest discover -s tests -t .
```

This runs the `oracle.core` unit tests, the adapter tests (which run the real legacy parser on the fixtures in
`input_dwgs/`) and a small regression test for `generate_test_dwg.py`. They are deterministic and need no Claude
API, STAAD.Pro, AutoCAD or network. There is **no automated test of the legacy application**: `test_conversion.py` is a
manual smoke script that prints a DXF's layers, and the legacy workflow has only been checked by hand.

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
