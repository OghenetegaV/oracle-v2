"""Oracle — Core Project Aggregate

Purpose:
    OracleProject: the top-level aggregate for one engineering project. It holds identity and
    metadata, the design basis, the building model, engineering decisions and issues, and the three
    evidence registries added in schema 0.2.0: provenance (where things came from), value statuses
    (how far each value can be trusted) and interpretation sets (competing readings of the source).
    It enforces the cross-references between all of them, applies engineer value changes with their
    history, reports readiness for final output, and reads and writes deterministic, versioned JSON
    (older schemas are migrated on load).

Role in Oracle:
    The canonical project object that adapters, engines and the GUI read and write. It is where the
    chain SOURCE FACT (provenance) -> INTERPRETATION (interpretation sets, value statuses) ->
    ENGINEERING DECISION (decisions) -> DESIGN RESULT (later phases) is kept intact, never collapsed:

      - Decision authority: an Oracle or AI recommendation can only be PROPOSED. It becomes ACCEPTED,
        OVERRIDDEN or REJECTED only through an ENGINEER decision that responds to it, so Oracle's own
        inference is never recorded as engineer-approved.
      - Value changes: set_value() applies an engineer decision to an element field, marks the field
        ENGINEER_DEFINED or ENGINEER_OVERRIDE, supersedes the previous decision on it and keeps the old
        value in the decision chain instead of overwriting history.
      - Readiness: readiness() says whether the project may be presented as ready for final output.

Dependencies:
    oracle (version), and every other oracle.core module.

Consumers:
    tests; oracle.adapters (which build projects); future engines and the GUI.

Status:
    Core (schema 0.2.0).

Migration/Notes:
    Schema 0.2.0 added the provenance, value_status and interpretations registries, and the decision
    and issue fields listed in their modules. 0.1.0 files load through oracle.core.migrations and are
    saved as 0.2.0. Analysis, design, reinforcement and drawing records will arrive as later schema
    versions, each with a migration.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Mapping, Optional, Union

from .. import __version__ as ORACLE_VERSION
from .building import BuildingModel
from .common import (
    SCHEMA_VERSION, Target, TargetScope, ValidationError, check_field_path, check_id, check_keys,
    check_optional_text, check_text, check_timestamp, check_unique_ids, parse_enum, utc_now_iso,
)
from .decisions import DecisionSource, DecisionStatus, EngineeringDecision
from .design_basis import DesignBasis
from .interpretations import InterpretationSet, SetStatus
from .issues import EngineeringIssue, IssueSeverity
from .migrations import migrate
from .provenance import ProvenanceRecord
from .readiness import Blocker, BlockerKind, ProjectReadiness
from .value_status import ENGINEER_STATUSES, ValueStatus, ValueStatusRecord

# A backing engineer decision may have been superseded since; that is history, not invalidity.
_BACKING_STATUSES = (DecisionStatus.ACCEPTED, DecisionStatus.SUPERSEDED)
_RECOMMENDER_SOURCES = (DecisionSource.ORACLE, DecisionSource.AI_ASSISTANT)


class OracleProject:
    def __init__(self, project_id: str, name: str, engineer: str, *, description: Optional[str] = None,
                 client: Optional[str] = None, location: Optional[str] = None,
                 created_at: Optional[str] = None, modified_at: Optional[str] = None):
        self.project_id = check_id(project_id, "project id")
        self.name = check_text(name, "project name")
        self.engineer = check_text(engineer, "engineer")
        self.description = check_optional_text(description, "project description")
        self.client = check_optional_text(client, "client")
        self.location = check_optional_text(location, "location")
        self.schema_version = SCHEMA_VERSION
        self.created_at = check_timestamp(created_at or utc_now_iso(), "created_at")
        self.modified_at = check_timestamp(modified_at or self.created_at, "modified_at")
        self.design_basis: Optional[DesignBasis] = None
        self._building: Optional[BuildingModel] = None
        self._decisions: dict = {}
        self._issues: dict = {}
        self._provenance: dict = {}
        self._value_status: dict = {}
        self._interpretations: dict = {}
        self._provenance_seq = 0

    @classmethod
    def create(cls, name: str, engineer: str, **metadata) -> "OracleProject":
        return cls(uuid.uuid4().hex, name, engineer, **metadata)

    def touch(self) -> None:
        self.modified_at = utc_now_iso()

    # ---- design basis / building ----

    def set_design_basis(self, basis: DesignBasis) -> None:
        if not isinstance(basis, DesignBasis):
            raise ValidationError("Expected a DesignBasis.")
        basis.validate()
        self.design_basis = basis
        self.touch()

    @property
    def building(self) -> Optional[BuildingModel]:
        return self._building

    def set_building(self, building: BuildingModel) -> None:
        if any((self._decisions, self._issues, self._provenance, self._value_status, self._interpretations)):
            self._check_all_targets(building)
        self._building = building
        building.on_change = self.touch
        self.touch()

    # ---- decisions ----

    @property
    def decisions(self) -> list:
        return list(self._decisions.values())

    def get_decision(self, decision_id: str) -> EngineeringDecision:
        try:
            return self._decisions[decision_id]
        except KeyError:
            raise ValidationError(f"Unknown decision {decision_id!r}.") from None

    def add_decision(self, decision: EngineeringDecision) -> EngineeringDecision:
        self._check_new_decision(decision)
        if self._is_accepted_field_change(decision):
            raise ValidationError(f"Decision {decision.id} is an accepted change to {decision.target.id}."
                                  f"{decision.field}: apply it with set_value(), which changes the model and its history.")
        return self._store_decision(decision)

    @staticmethod
    def _is_accepted_field_change(decision: EngineeringDecision) -> bool:
        return (decision.field is not None and decision.source == DecisionSource.ENGINEER
                and decision.status == DecisionStatus.ACCEPTED)

    def _check_new_decision(self, decision: EngineeringDecision) -> None:
        if decision.id in self._decisions:
            raise ValidationError(f"Duplicate decision id {decision.id!r}.")
        self._check_target(self._building, decision.target, f"decision {decision.id}")
        if decision.source in _RECOMMENDER_SOURCES and decision.status != DecisionStatus.PROPOSED:
            raise ValidationError(
                f"Decision {decision.id}: a recommendation from {decision.source.value} can only be added as "
                "proposed. It becomes accepted, overridden or rejected only when an engineer decision responds to it.")
        if decision.responds_to is not None:
            recommendation = self.get_decision(decision.responds_to)
            if recommendation.source == DecisionSource.ENGINEER:
                raise ValidationError(f"Decision {decision.id} responds to {recommendation.id}, which is itself an "
                                      "engineer decision, not a recommendation.")
            if recommendation.status == DecisionStatus.SUPERSEDED:
                raise ValidationError(f"Decision {decision.id} responds to {recommendation.id}, which is superseded.")

    def _store_decision(self, decision: EngineeringDecision) -> EngineeringDecision:
        self._decisions[decision.id] = decision
        self._record_response(decision)
        self.touch()
        return decision

    def _record_response(self, response: EngineeringDecision) -> None:
        """Move the recommendation an engineer decision answers to the matching status."""
        if response.responds_to is None:
            return
        recommendation = self._decisions[response.responds_to]
        if response.status == DecisionStatus.ACCEPTED:
            recommendation.status = (DecisionStatus.OVERRIDDEN if response.overrides_recommendation
                                     else DecisionStatus.ACCEPTED)
        elif response.status == DecisionStatus.REJECTED:
            recommendation.status = DecisionStatus.REJECTED
        elif response.status == DecisionStatus.PROPOSED:
            recommendation.status = DecisionStatus.PROPOSED

    def decisions_for(self, target: Target) -> list:
        return [d for d in self._decisions.values() if d.target == target]

    def decision_history(self, target: Target, field: Optional[str] = None) -> list:
        """Every decision about a target (and field, if given) in the order recorded, superseded ones
        included: the audit trail of how a value or ruling changed."""
        return [d for d in self._decisions.values()
                if d.target == target and (field is None or d.field == field)]

    def set_decision_status(self, decision_id: str, status: DecisionStatus) -> None:
        status = parse_enum(DecisionStatus, status, "decision status")
        if status == DecisionStatus.SUPERSEDED:
            raise ValidationError("Use supersede_decision() to mark a decision as superseded.")
        decision = self.get_decision(decision_id)
        if (decision.field is not None and decision.source == DecisionSource.ENGINEER
                and status == DecisionStatus.ACCEPTED and decision.status != DecisionStatus.ACCEPTED):
            raise ValidationError(f"Decision {decision_id} changes {decision.target.id}.{decision.field}: it can only "
                                  "be accepted by applying it with set_value().")
        if decision.source in _RECOMMENDER_SOURCES and status != DecisionStatus.PROPOSED:
            raise ValidationError(
                f"{decision.source.value} recommendation {decision.id} cannot be set to {status.value} directly: "
                "record an engineer decision that responds to it.")
        decision.status = status
        decision.superseded_by = None
        decision.__post_init__()
        self._record_response(decision)
        self.touch()

    def supersede_decision(self, old_id: str, new_id: str) -> None:
        old, new = self.get_decision(old_id), self.get_decision(new_id)
        if old.target != new.target or old.field != new.field:
            raise ValidationError(f"Decision {new_id} cannot supersede {old_id}: they are not about the same "
                                  "target and field.")
        before = (old.status, old.superseded_by)
        old.supersede(new_id)  # rejects a decision superseding itself
        try:
            self._check_supersession_chains()
        except ValidationError:
            old.status, old.superseded_by = before
            raise
        self.touch()

    def _check_supersession_chains(self) -> None:
        for start in self._decisions.values():
            seen, current = {start.id}, start
            while current.superseded_by is not None and current.superseded_by in self._decisions:
                current = self._decisions[current.superseded_by]
                if current.id in seen:
                    raise ValidationError(f"Decision {start.id} is part of a supersession loop.")
                seen.add(current.id)

    # ---- issues ----

    @property
    def issues(self) -> list:
        return list(self._issues.values())

    def get_issue(self, issue_id: str) -> EngineeringIssue:
        try:
            return self._issues[issue_id]
        except KeyError:
            raise ValidationError(f"Unknown issue {issue_id!r}.") from None

    def add_issue(self, issue: EngineeringIssue) -> EngineeringIssue:
        if issue.id in self._issues:
            raise ValidationError(f"Duplicate issue id {issue.id!r}.")
        self._check_issue_refs(issue)
        self._issues[issue.id] = issue
        self.touch()
        return issue

    def _check_issue_refs(self, issue: EngineeringIssue) -> None:
        what = f"issue {issue.id}"
        self._check_target(self._building, issue.target, what)
        for related in issue.related:
            self._check_target(self._building, related, f"{what} (related object)")
        if issue.decision_id is not None:
            self.get_decision(issue.decision_id)
        for evidence_id in issue.evidence:
            if evidence_id not in self._provenance:
                raise ValidationError(f"{what.capitalize()} cites unknown provenance record {evidence_id!r}.")
        if issue.interpretation_id is not None and self._find_interpretation(issue.interpretation_id) is None:
            raise ValidationError(f"{what.capitalize()} refers to unknown interpretation {issue.interpretation_id!r}.")

    def issues_for(self, target: Target) -> list:
        return [i for i in self._issues.values() if i.target == target]

    def open_issues(self, severity: Optional[IssueSeverity] = None) -> list:
        return [i for i in self._issues.values() if i.is_open and (severity is None or i.severity == severity)]

    def has_blocking_issues(self) -> bool:
        return bool(self.open_issues(IssueSeverity.BLOCKING))

    def resolve_issue(self, issue_id: str, resolution: str, decision_id: Optional[str] = None) -> None:
        if decision_id is not None:
            self.get_decision(decision_id)
        self.get_issue(issue_id).resolve(resolution, decision_id)
        self.touch()

    def accept_issue(self, issue_id: str, reason: str, decision_id: Optional[str] = None) -> None:
        if decision_id is not None:
            decision = self.get_decision(decision_id)
            if decision.source != DecisionSource.ENGINEER:
                raise ValidationError(f"An issue can only be accepted on the strength of an engineer decision; "
                                      f"{decision_id} is from {decision.source.value}.")
        self.get_issue(issue_id).accept(reason, decision_id)
        self.touch()

    # ---- provenance ----

    @property
    def provenance(self) -> list:
        return list(self._provenance.values())

    def get_provenance(self, provenance_id: str) -> ProvenanceRecord:
        try:
            return self._provenance[provenance_id]
        except KeyError:
            raise ValidationError(f"Unknown provenance record {provenance_id!r}.") from None

    def next_provenance_id(self) -> str:
        self._provenance_seq += 1
        return f"PV-{self._provenance_seq:05d}"

    def add_provenance(self, record: ProvenanceRecord) -> ProvenanceRecord:
        if record.id in self._provenance:
            raise ValidationError(f"Duplicate provenance id {record.id!r}.")
        self._check_target(self._building, record.target, f"provenance {record.id}")
        self._check_field(record.target, record.field, f"provenance {record.id}")
        self._provenance[record.id] = record
        self.touch()
        return record

    def provenance_for(self, target: Target, field: Optional[str] = None) -> list:
        """Records about a target: all of them, or only those about one field."""
        return [r for r in self._provenance.values() if r.target == target and (field is None or r.field == field)]

    # ---- value status ----

    @property
    def value_statuses(self) -> list:
        return list(self._value_status.values())

    def set_value_status(self, record: ValueStatusRecord) -> ValueStatusRecord:
        """Record (or replace) the current status of one value."""
        self._check_value_status(record)
        self._value_status[record.key] = record
        self.touch()
        return record

    def value_status_of(self, target: Target, field: str) -> Optional[ValueStatusRecord]:
        return self._value_status.get((target.scope.value, target.id, field))

    def values_with_status(self, *statuses: ValueStatus) -> list:
        wanted = {parse_enum(ValueStatus, s, "value status") for s in statuses}
        return [r for r in self._value_status.values() if r.status in wanted]

    def _check_value_status(self, record: ValueStatusRecord) -> None:
        what = f"value status {record.target.id}.{record.field}"
        self._check_target(self._building, record.target, what)
        self._check_field(record.target, record.field, what)
        for pid in record.provenance_ids:
            if pid not in self._provenance:
                raise ValidationError(f"{what.capitalize()} cites unknown provenance record {pid!r}.")
        if record.status in ENGINEER_STATUSES:
            decision = self._require_engineer_decision(record.decision_id, what)
            if decision.field is not None and (decision.target != record.target or decision.field != record.field):
                raise ValidationError(f"{what.capitalize()} is backed by decision {decision.id}, which is about "
                                      f"{decision.target.id}.{decision.field}.")

    def _require_engineer_decision(self, decision_id: str, what: str) -> EngineeringDecision:
        decision = self._decisions.get(decision_id)
        if decision is None:
            raise ValidationError(f"{what.capitalize()} references unknown decision {decision_id!r}.")
        if decision.source != DecisionSource.ENGINEER or decision.status not in _BACKING_STATUSES:
            raise ValidationError(f"{what.capitalize()} needs an accepted engineer decision; {decision_id} is "
                                  f"{decision.status.value} and from {decision.source.value}.")
        return decision

    # ---- applying an engineer's value ----

    def set_value(self, target: Target, field: str, value: Any, decision: EngineeringDecision) -> ValueStatusRecord:
        """Apply an engineer's decision to one field of an element.

        The decision (source ENGINEER, ACCEPTED, naming this target, field and value) is recorded, the
        element is rebuilt and fully re-validated with the new value, the field becomes ENGINEER_OVERRIDE
        if it already had a status (something the source, Oracle or an earlier engineer set) or
        ENGINEER_DEFINED if it had none, and every earlier accepted decision on the same field is marked
        superseded by this one. The old value stays in decision.previous_value. Nothing changes if any step
        is rejected. Only top-level element fields are supported (e.g. 'section', 'thickness_mm')."""
        if self._building is None or target.scope != TargetScope.ELEMENT:
            raise ValidationError("set_value applies to structural elements of a project that has a building.")
        check_field_path(field, "field")
        if "." in field:
            raise ValidationError("set_value takes a top-level field; replace the whole value (e.g. the section).")
        element = self._building.get_element(target.id)
        old = element.to_dict()
        if field == "id" or field not in old:
            raise ValidationError(f"Element {target.id} has no changeable field {field!r}.")
        if decision.source != DecisionSource.ENGINEER or decision.status != DecisionStatus.ACCEPTED:
            raise ValidationError("A value is only changed by an accepted decision whose source is the engineer.")
        if (decision.target, decision.field, decision.value) != (target, field, value):
            raise ValidationError("The decision must name exactly the target, field and value being applied.")
        changed = dict(old)
        changed[field] = value
        replacement = type(element).from_dict(changed)  # raises if the new value is not valid for the element
        self._check_new_decision(decision)
        self._building.replace_element(replacement)  # last step that can fail; nothing else has changed yet
        decision.previous_value = old[field]
        self._store_decision(decision)
        for earlier in self.decision_history(target, field):
            if earlier.id != decision.id and earlier.status == DecisionStatus.ACCEPTED:
                earlier.supersede(decision.id)
        existing = self.value_status_of(target, field)
        record = ValueStatusRecord(
            target, field, ValueStatus.ENGINEER_OVERRIDE if existing else ValueStatus.ENGINEER_DEFINED,
            decision_id=decision.id, replaces=existing.status if existing else None)
        self._value_status[record.key] = record
        self.touch()
        return record

    # ---- alternative interpretations ----

    @property
    def interpretation_sets(self) -> list:
        return list(self._interpretations.values())

    def get_interpretation_set(self, set_id: str) -> InterpretationSet:
        try:
            return self._interpretations[set_id]
        except KeyError:
            raise ValidationError(f"Unknown interpretation set {set_id!r}.") from None

    def _find_interpretation(self, interpretation_id: str):
        for s in self._interpretations.values():
            for a in s.alternatives:
                if a.id == interpretation_id:
                    return s, a
        return None

    def add_interpretation_set(self, interpretation_set: InterpretationSet) -> InterpretationSet:
        if interpretation_set.id in self._interpretations:
            raise ValidationError(f"Duplicate interpretation set id {interpretation_set.id!r}.")
        for a in interpretation_set.alternatives:
            if self._find_interpretation(a.id) is not None:
                raise ValidationError(f"Duplicate interpretation id {a.id!r}.")
        self._check_interpretation_set(interpretation_set)
        self._interpretations[interpretation_set.id] = interpretation_set
        self.touch()
        return interpretation_set

    def _check_interpretation_set(self, s: InterpretationSet) -> None:
        what = f"interpretation set {s.id}"
        if s.subject is not None:
            self._check_target(self._building, s.subject, what)
        for evidence_id in s.evidence:
            if evidence_id not in self._provenance:
                raise ValidationError(f"{what.capitalize()} cites unknown provenance record {evidence_id!r}.")
        for a in s.alternatives:
            for evidence_id in a.evidence:
                if evidence_id not in self._provenance:
                    raise ValidationError(f"Interpretation {a.id} cites unknown provenance record {evidence_id!r}.")
            if a.decision_id is not None:
                self._require_engineer_decision(a.decision_id, f"interpretation {a.id}")

    def open_interpretation_sets(self) -> list:
        return [s for s in self._interpretations.values() if s.status == SetStatus.OPEN]

    def accept_interpretation(self, set_id: str, interpretation_id: str, decision_id: str) -> None:
        """The engineer chooses one reading; the others are rejected under the same decision."""
        s = self.get_interpretation_set(set_id)
        self._require_engineer_decision(decision_id, f"accepting interpretation {interpretation_id}")
        s.accept(interpretation_id, decision_id)
        self.touch()

    def reject_interpretation(self, set_id: str, interpretation_id: str, decision_id: str) -> None:
        s = self.get_interpretation_set(set_id)
        self._require_engineer_decision(decision_id, f"rejecting interpretation {interpretation_id}")
        s.reject(interpretation_id, decision_id)
        self.touch()

    # ---- readiness ----

    def readiness(self) -> ProjectReadiness:
        """May this project be presented as ready for final engineering output? Not while any blocking
        issue is open, any interpretation set is unresolved, any value is merely ASSUMED, or there is no
        building. INFERRED values do not block; they are listed as unconfirmed."""
        result = ProjectReadiness()
        if self._building is None:
            result.blockers.append(Blocker(BlockerKind.NO_BUILDING, "project", "The project has no building model."))
        for issue in self.open_issues(IssueSeverity.BLOCKING):
            result.blockers.append(Blocker(BlockerKind.BLOCKING_ISSUE, issue.id, issue.message))
        for s in self.open_interpretation_sets():
            result.blockers.append(Blocker(BlockerKind.OPEN_INTERPRETATION, s.id, s.question))
        for r in self.values_with_status(ValueStatus.ASSUMED):
            ref = f"{r.target.scope.value} {r.target.id}.{r.field}"
            result.blockers.append(Blocker(BlockerKind.ASSUMED_VALUE, ref, f"{ref} is an unconfirmed assumption."))
        for r in self.values_with_status(ValueStatus.INFERRED):
            result.unconfirmed.append(f"{r.target.scope.value} {r.target.id}.{r.field}")
        return result

    # ---- validation ----

    def _object_dict(self, building: Optional[BuildingModel], target: Target) -> dict:
        getter = {TargetScope.LEVEL: building.get_level, TargetScope.NODE: building.get_node,
                  TargetScope.GRID: building.get_grid, TargetScope.ELEMENT: building.get_element}[target.scope]
        return getter(target.id).to_dict()

    def _check_target(self, building: Optional[BuildingModel], target: Target, what: str) -> None:
        if target.scope == TargetScope.PROJECT:
            return
        if building is None:
            raise ValidationError(f"{what.capitalize()} targets {target.scope.value} {target.id!r} but the "
                                  "project has no building model yet.")
        try:
            self._object_dict(building, target)
        except ValidationError:
            raise ValidationError(f"{what.capitalize()} targets unknown {target.scope.value} {target.id!r}.") from None

    def _check_field(self, target: Target, field: Optional[str], what: str) -> None:
        """A field must be a real property of the target object, or the reserved 'geometry' (its position
        or shape as a whole). Only the first segment of a dotted path is checked."""
        if field is None:
            return
        first = field.split(".")[0]
        if first != "geometry" and first not in self._object_dict(self._building, target):
            raise ValidationError(f"{what.capitalize()} names field {field!r}, which {target.scope.value} "
                                  f"{target.id!r} does not have.")

    def _check_all_targets(self, building: Optional[BuildingModel]) -> None:
        for d in self._decisions.values():
            self._check_target(building, d.target, f"decision {d.id}")
        for i in self._issues.values():
            self._check_target(building, i.target, f"issue {i.id}")
            for related in i.related:
                self._check_target(building, related, f"issue {i.id} (related object)")
        for r in self._provenance.values():
            self._check_target(building, r.target, f"provenance {r.id}")
        for r in self._value_status.values():
            self._check_target(building, r.target, f"value status {r.target.id}.{r.field}")
        for s in self._interpretations.values():
            if s.subject is not None:
                self._check_target(building, s.subject, f"interpretation set {s.id}")

    def validate(self) -> None:
        """Full consistency check: building, design basis and every cross-reference."""
        if self.design_basis is not None:
            self.design_basis.validate()
        if self._building is not None:
            self._building.validate()
        self._check_all_targets(self._building)
        for d in self._decisions.values():
            self._check_decision(d)
        self._check_supersession_chains()
        self._check_recommendation_outcomes()
        self._check_value_decisions_are_applied()
        for i in self._issues.values():
            self._check_issue_refs(i)
        for r in self._provenance.values():
            self._check_field(r.target, r.field, f"provenance {r.id}")
        for r in self._value_status.values():
            self._check_value_status(r)
        seen_alternatives = set()
        for s in self._interpretations.values():
            self._check_interpretation_set(s)
            for a in s.alternatives:
                if a.id in seen_alternatives:
                    raise ValidationError(f"Duplicate interpretation id {a.id!r}.")
                seen_alternatives.add(a.id)

    def _check_decision(self, d: EngineeringDecision) -> None:
        if d.superseded_by is not None and d.superseded_by not in self._decisions:
            raise ValidationError(f"Decision {d.id} is superseded by unknown decision {d.superseded_by!r}.")
        if d.responds_to is not None:
            recommendation = self._decisions.get(d.responds_to)
            if recommendation is None:
                raise ValidationError(f"Decision {d.id} responds to unknown decision {d.responds_to!r}.")
            if recommendation.source == DecisionSource.ENGINEER:
                raise ValidationError(f"Decision {d.id} responds to {recommendation.id}, which is an engineer decision.")
        if d.field is not None and d.target.scope != TargetScope.PROJECT:
            self._check_field(d.target, d.field, f"decision {d.id}")

    def _check_recommendation_outcomes(self) -> None:
        """An Oracle/AI recommendation may only be accepted, overridden or rejected because an engineer
        decision says so."""
        responses = {}
        for d in self._decisions.values():
            if d.responds_to is not None:
                responses.setdefault(d.responds_to, []).append(d)
        for rec in self._decisions.values():
            if rec.source not in _RECOMMENDER_SOURCES or rec.status in (DecisionStatus.PROPOSED, DecisionStatus.SUPERSEDED):
                continue
            answers = responses.get(rec.id, [])
            supported = {
                DecisionStatus.ACCEPTED: any(a.status == DecisionStatus.ACCEPTED and not a.overrides_recommendation
                                             for a in answers),
                DecisionStatus.OVERRIDDEN: any(a.status == DecisionStatus.ACCEPTED and a.overrides_recommendation
                                               for a in answers),
                DecisionStatus.REJECTED: any(a.status == DecisionStatus.REJECTED for a in answers),
            }.get(rec.status, False)
            if not supported:
                raise ValidationError(
                    f"{rec.source.value} recommendation {rec.id} is {rec.status.value}, but no engineer decision "
                    "responds to it that way. Oracle's own inference cannot be recorded as engineer-approved.")

    def _check_value_decisions_are_applied(self) -> None:
        """The model must reflect the latest accepted engineer decision on each element field."""
        latest = {}
        for d in self._decisions.values():
            if (d.field is not None and d.target.scope == TargetScope.ELEMENT and d.source == DecisionSource.ENGINEER
                    and d.status == DecisionStatus.ACCEPTED):
                if (d.target.id, d.field) in latest:
                    raise ValidationError(f"Decisions {latest[(d.target.id, d.field)].id} and {d.id} both stand on "
                                          f"{d.target.id}.{d.field}; the earlier one should be superseded.")
                latest[(d.target.id, d.field)] = d
        for (element_id, field), d in latest.items():
            current = self._building.get_element(element_id).to_dict().get(field)
            if current != d.value:
                raise ValidationError(f"Decision {d.id} says {element_id}.{field} = {d.value!r} but the model has "
                                      f"{current!r}.")

    # ---- serialisation ----

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "oracle_version": ORACLE_VERSION,
            "project_id": self.project_id,
            "name": self.name,
            "description": self.description,
            "client": self.client,
            "location": self.location,
            "engineer": self.engineer,
            "created_at": self.created_at,
            "modified_at": self.modified_at,
            "design_basis": self.design_basis.to_dict() if self.design_basis else None,
            "building": self._building.to_dict() if self._building else None,
            "decisions": [d.to_dict() for d in self._decisions.values()],
            "issues": [i.to_dict() for i in self._issues.values()],
            "provenance": [r.to_dict() for r in self._provenance.values()],
            "value_status": [r.to_dict() for r in self._value_status.values()],
            "interpretations": [s.to_dict() for s in self._interpretations.values()],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OracleProject":
        data = migrate(data)  # refuses unknown versions; upgrades older ones without inventing content
        check_keys(data, required={"schema_version", "project_id", "name", "engineer", "created_at", "modified_at",
                                   "decisions", "issues", "provenance", "value_status", "interpretations"},
                   optional={"oracle_version", "description", "client", "location", "design_basis", "building"},
                   where="project")
        project = cls(data["project_id"], data["name"], data["engineer"], description=data.get("description"),
                      client=data.get("client"), location=data.get("location"),
                      created_at=data["created_at"], modified_at=data["modified_at"])
        if data.get("design_basis") is not None:
            project.design_basis = DesignBasis.from_dict(data["design_basis"])
        if data.get("building") is not None:
            project._building = BuildingModel.from_dict(data["building"])
        decisions = [EngineeringDecision.from_dict(d) for d in data["decisions"]]
        issues = [EngineeringIssue.from_dict(i) for i in data["issues"]]
        provenance = [ProvenanceRecord.from_dict(r) for r in data["provenance"]]
        statuses = [ValueStatusRecord.from_dict(r) for r in data["value_status"]]
        interpretations = [InterpretationSet.from_dict(s) for s in data["interpretations"]]
        check_unique_ids((d.id for d in decisions), "decision id")
        check_unique_ids((i.id for i in issues), "issue id")
        check_unique_ids((r.id for r in provenance), "provenance id")
        check_unique_ids((s.id for s in interpretations), "interpretation set id")
        check_unique_ids((f"{r.target.scope.value}:{r.target.id}.{r.field}" for r in statuses), "value status for")
        project._decisions = {d.id: d for d in decisions}
        project._issues = {i.id: i for i in issues}
        project._provenance = {r.id: r for r in provenance}
        project._value_status = {r.key: r for r in statuses}
        project._interpretations = {s.id: s for s in interpretations}
        project._provenance_seq = max(
            (int(r.id[3:]) for r in provenance if r.id.startswith("PV-") and r.id[3:].isdigit()), default=0)
        project.validate()
        if project._building is not None:
            project._building.on_change = project.touch  # attached last: loading must not bump modified_at
        return project

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n"

    @classmethod
    def from_json(cls, text: str) -> "OracleProject":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"Project file is not valid JSON: {exc}") from None
        return cls.from_dict(data)

    def save(self, path: Union[str, Path]) -> None:
        """Write atomically (temp file then replace) so a crash never leaves a half-written project."""
        path = Path(path)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(self.to_json(), encoding="utf-8", newline="\n")
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: Union[str, Path]) -> "OracleProject":
        return cls.from_json(Path(path).read_text(encoding="utf-8"))
