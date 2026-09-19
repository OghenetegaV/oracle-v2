# Core evidence model (schema 0.2.0)

Status: implemented and tested in `oracle.core`. The architectural-DWG interpreter is **not** built; this is the
data foundation it will depend on. Everything here is CAD-neutral: `oracle.core` imports no parser, DXF/DWG
library, STAAD or Claude (checked by tests).

## 1. The chain that must never collapse

```
SOURCE FACT  ->  INTERPRETATION  ->  ENGINEERING DECISION  ->  DESIGN RESULT
provenance       value status,        decisions                 (later phases:
                 interpretation sets                            CALCULATED / DERIVED values)
```

Each stage is a different kind of statement with a different owner, so each has its own record type. A value in
the model is never just "the value": the project can say where it came from, how far it can be trusted, which
alternatives were considered, and which engineer decision (if any) stands behind it.

| Question | Answered by | Stored in |
|---|---|---|
| Where did this come from? | `ProvenanceRecord` | `OracleProject.provenance` (append-only) |
| How far can this value be trusted? | `ValueStatusRecord` | `OracleProject.value_status` (one per target + field) |
| What else could it have been? | `InterpretationSet` | `OracleProject.interpretations` |
| Who decided, and what changed? | `EngineeringDecision` | `OracleProject.decisions` |
| What is wrong or unresolved? | `EngineeringIssue` | `OracleProject.issues` |
| May it be presented as final? | `ProjectReadiness` | computed, not stored |

All three new registries are **project-level tables keyed by object ID and field**, not fields on `Column` or `Beam`,
so element classes stay clean and any importer or interpreter can supply evidence. A field of `None` means the
whole object; the reserved name `geometry` means position/shape as a whole (a beam's geometry is its two nodes, a
slab's is its boundary). Only the first segment of a dotted field path is checked against the real object.

## 2. Provenance (`oracle/core/provenance.py`)

`ProvenanceRecord`: `id` (`PV-00001`), `target` (level, element, node or grid line), optional `field`, `method`,
`producer`, optional `confidence` (0-1), `recorded_at`, `note`, and a `SourceReference`: `file`, `entity_handle`,
`layer`, `entity_type`, `source_id` (the producer's own identifier, e.g. "member 12"), `coordinates` (+ required
`coordinate_frame`) and `context` (the floor/plan). At least one identifying field is required. Records are never
edited or deleted, so the evidence survives later overrides. Several records may describe the same target and
field.

## 3. Value status (`oracle/core/value_status.py`)

| Status | Meaning | Example |
|---|---|---|
| `source` | Read from the source | a beam centreline |
| `inferred` | Deduced by an interpreter | a column's structural centre from its outline |
| `assumed` | Placeholder for missing information | a 300x300 section Oracle had to guess |
| `engineer_defined` | The engineer stated it | a level elevation the engineer entered |
| `engineer_override` | The engineer replaced a value the source/Oracle/an earlier ruling gave | 350x350 replacing the guess |
| `calculated` | Result of analysis/design calculation | reinforcement demand from STAAD (later phase) |
| `derived` | Deterministic consequence of other values | a storey height from two elevations; the final bar arrangement (later phase) |

Rules: the two engineer statuses need a `decision_id`, and the project checks it is an **accepted engineer
decision** (a superseded one is allowed as history) about the same target and field. An override records the
status it `replaces`. Other statuses must not carry a decision. Records are current state; history is the decision
chain.

## 4. Alternative interpretations (`oracle/core/interpretations.py`)

An `InterpretationSet` poses a question about shared evidence (`evidence` = provenance IDs, optional `subject`) and
holds `Interpretation`s: `id`, `meaning` (a free-form code; the core hard-codes no architectural classes),
`confidence`, `rationale`, optional own `evidence`, `status` (proposed / accepted / rejected) and the `decision_id`
that settled it. The set's status is **derived**: open while anything is proposed, resolved once one is accepted,
none-apply when all are rejected. Accepting one rejects its still-proposed siblings under the same decision; the
engineer may choose a low-confidence reading. Accepting or rejecting needs an accepted engineer decision, so Oracle
cannot choose for the engineer. Example: "slab panel" 0.82, "void" 0.14, "balcony" 0.04. Carrying out the accepted
reading (creating the slab or opening) is the interpreter's job.

## 5. Decisions and authority (`oracle/core/decisions.py`, `project.py`)

| Kind | Source | Allowed status | How it changes |
|---|---|---|---|
| Recommendation | `oracle`, `ai_assistant` | **proposed only** | never by itself |
| Engineer decision | `engineer` | any | by the engineer |
| Engineer override | `engineer`, `overrides_recommendation`, `responds_to` a recommendation | any | departs from the recommendation |

- Oracle/AI decisions cannot be added or set as accepted/overridden/rejected. An engineer decision that
  `responds_to` the recommendation moves it to accepted, overridden or rejected. A file that records an Oracle
  inference as approved without such a response is **rejected on load**.
- **Changing a value** is `project.set_value(target, field, value, decision)`: the decision must be an accepted
  engineer decision naming exactly that target, field and value. The element is rebuilt and fully re-validated;
  the field becomes `engineer_override` (or `engineer_defined` if it had no status); earlier accepted decisions on
  it are superseded; the old value is kept in `decision.previous_value`. If anything fails, nothing changes.
  Example: C12 300x300 (D1) then 350x350 (D2) leaves D1 `superseded`, `D1.value` = 300x300, `D2.previous_value` =
  300x300, and `decision_history(C12, "section")` = [D1, D2]. An accepted field-change decision cannot be added any
  other way, and a file whose model disagrees with its latest accepted decision is rejected.
- Supersession needs the same target and field and may not form a loop. Only an engineer decision can accept an
  issue.
- `IMPORTED` decisions are exempt from the recommendation rule (they carry accepted state from a legacy file).

## 6. Issues and readiness

`EngineeringIssue` now also has `evidence` (provenance IDs), `interpretation_id` and `related` (other targets, e.g.
the column and the beam). Severity is unchanged (info, warning, error, blocking).

`project.readiness()` returns a `ProjectReadiness`. The project is **not ready for final engineering output** while
any of these stand: an open **blocking** issue, an open interpretation set, an **assumed** value, or no building.
`inferred` values do not block but are listed as `unconfirmed`. Readiness is computed, not stored. It is strict on
purpose: relaxing it takes an engineer's decision (accept the issue, confirm the value).

## 7. Schema 0.2.0 and migration (`oracle/core/migrations.py`)

Changed: new required top-level registries `provenance`, `value_status`, `interpretations`; new optional decision
fields `responds_to`, `field`, `value`, `previous_value`; new optional issue fields `evidence`,
`interpretation_id`, `related`; `NODE` and `GRID` target scopes.

`0.1.0` files load: the migration adds the empty registries and nothing else (no provenance is invented for old
projects), and the project is saved back as `0.2.0`. It refuses a `0.1.0` file that already contains the new
registries. Unknown versions raise `SchemaVersionError`; unknown fields are still rejected after migration. The
migration is tested against a genuine `0.1.0` file written by the Phase 1 code (`tests/fixtures/schema_0_1_0_project.json`,
from tag `v2.0.0-phase1`). Old code cannot read `0.2.0` files.

## 8. Drawing geometry is not analytical geometry

Two lines crossing is not a structural connection. What the core keeps for the distinction: beams and walls connect
only through explicit `Node`s, so connectivity is a stated fact, never inferred from a crossing; provenance keeps the
raw source geometry separately from the modelled one; levels and elevations are explicit; openings are elements.
The legacy adapter therefore does not split a beam that runs over a column: it raises a **blocking** issue ("Column
C16 terminates on the interior of Beam B23, but the source beam geometry contains no corresponding structural
node") with both objects' provenance as evidence. **Not yet represented:** support relationships beyond a slab's
`supported_by`, member continuity across supports, and transfer conditions. Those need their own records before
design (see the open questions).

## 9. Judgement calls and open architectural questions

1. **Adapter-recorded engineer input.** To mark caller-supplied elevations, names and thicknesses
   `engineer_defined`, the adapter records an accepted `ENGINEER` decision on the caller's declaration. That is the
   adapter relaying a claim, not authenticating it. Alternative: a distinct `caller_input` status.
2. **Should `assumed` always block?** Currently yes for the whole project. A per-output rule (only values that reach
   the drawing) may be better.
3. **`inferred` never blocks.** Arguably a low-confidence inference should. Needs a policy.
4. **Field-level engineer changes** are limited to top-level element fields; levels, nodes and the design basis
   cannot yet be overridden through `set_value`.
5. **Project-level provenance for the design basis** (grades, cover) is not supported (targets must be an object).
6. **Interpretation to model.** Nothing yet turns an accepted interpretation into elements; an interpreter must.
7. **Status per field is coarse** for composite values (a section is one field).
8. **File size.** The real 3-storey GA now saves at about 0.7 MB (464 provenance records, 717 value statuses). Fine now;
   a compact form may matter for large buildings.
9. **Confidence is unitless** (0-1) and each producer defines what it means; there is no calibration.
10. **Support/continuity/transfer model** (section 8) is the next core gap for structural reasoning.

## 10. Tests

`test_provenance_status.py`, `test_decision_authority.py`, `test_interpretations.py`, `test_migration_readiness.py`
(core only) and `test_adapter_evidence.py` (adapter on the real fixture). They include malformed-data rejection on
load, save/load equality of every registry, and were mutation-checked (breaking six key rules made tests fail).
`python -m unittest discover -s tests -t .`
