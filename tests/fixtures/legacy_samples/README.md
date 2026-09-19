# Legacy output samples

Real outputs of the legacy (pre-`oracle.core`) workflow for the single-floor 10 m x 10 m
test project (`input_dwgs/test_floor.dxf`, 3x3 columns on a 5 m grid, storey height 3 m):

| File | Produced by | Contents |
|---|---|---|
| `ga_output.json` | `claude_ga_generator.py` | Proposed layout: columns, beams, slabs, dimensions (metres) |
| `staad_results.json` | `staad_mock.py` / `staad_v8i_integration.py` | Node and member forces |
| `design_output.json` | `design_module.py` | Member sizes and reinforcement strings |
| `bbs_output.json` | `lisp_detail_generator.py` | Bar bending schedule |

They are kept as reference for the legacy data contract that a future adapter into `oracle.core`
must read. They are static snapshots (the layout and design were produced by Claude and will
differ on a re-run), so they are fixtures, not expected outputs. No test uses them yet.
