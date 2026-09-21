"""Oracle — Core Project Aggregate

Purpose:
    OracleProject: the top-level aggregate for one engineering project. It holds identity and
    metadata, the design basis, the building model, engineering decisions and issues, the three
    evidence registries added in schema 0.2.0: provenance (where things came from), value statuses
    (how far each value can be trusted) and interpretation sets (competing readings of the source), and
    (schema 0.3.0) the architectural interpretation of the source drawing, with the engineer's review,
    merge and split actions on its views.
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
    Core (schema 0.4.0).

Migration/Notes:
    Schema 0.2.0 added the provenance, value_status and interpretations registries, and the decision
    and issue fields listed in their modules; 0.3.0 added `architecture`; 0.4.0 turned it into `architectures` (one
    interpretation per drawing source), added `evidence_links` (typed links from domain objects to approved
    architectural evidence), structured interpretation effects that make resolving a set change the model, level
    values under set_value(), trace() and the read-only approved_architecture() projection. Older files load through
    oracle.core.migrations and are saved as the current schema. Analysis, design, reinforcement and drawing records will arrive as later schema
    versions, each with a migration.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Mapping, Optional, Union

from .. import __version__ as ORACLE_VERSION
from dataclasses import replace as dataclass_replace

from . import approved as _approved
from . import clarifications as _clarifications
from . import resolution as _resolution
from . import trace as _trace
from .architecture import (
    ArchitecturalInterpretation, ArchitecturalObservation, CoordinateFrame, DrawingView, HeightEvidence, ReviewStatus,
    ViewType,
)
from .building import BuildingModel, Level
from .clarifications import EngineerClarification
from .evidence import EvidenceLink, EvidenceRelation
from .level_changes import plan_level_change
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
        self._architectures: dict = {}          # source id -> ArchitecturalInterpretation
        self._evidence_links: dict = {}
        self._clarifications: dict = {}
        self._provenance_seq = 0

    @classmethod
    def create(cls, name: str, engineer: str, **metadata) -> "OracleProject":
        return cls(uuid.uuid4().hex, name, engineer, **metadata)

    def touch(self) -> None:
        self.modified_at = utc_now_iso()

    # ---- architectural interpretation (one or more sources) ----

    @property
    def architecture(self) -> Optional[ArchitecturalInterpretation]:
        """The first (usually the only) architectural interpretation, or None. With several sources use
        `architectures` or `architecture_of(source_id)`."""
        return next(iter(self._architectures.values()), None)

    @property
    def architectures(self) -> list:
        return list(self._architectures.values())

    def architecture_of(self, source_id: str) -> ArchitecturalInterpretation:
        try:
            return self._architectures[source_id]
        except KeyError:
            raise ValidationError(f"Unknown drawing source {source_id!r}.") from None

    def add_architecture(self, architecture: ArchitecturalInterpretation) -> None:
        """Attach the interpretation of one source. Sources are told apart by id, revision, hash and interpretation
        instance; every architectural object id must be unique across the whole project so a Target names exactly one."""
        architecture.validate()
        source = architecture.drawing
        if source.id in self._architectures:
            raise ValidationError(f"This project already has an interpretation of source {source.id}.")
        for other in self._architectures.values():
            if other.drawing.interpretation_id == source.interpretation_id:
                raise ValidationError(f"Interpretation instance {source.interpretation_id} is already used by {other.drawing.id}.")
            clash = sorted(set(architecture._all) & set(other._all))
            if clash:
                raise ValidationError(f"Architectural id(s) {clash[:3]} are already used by source {other.drawing.id}; "
                                      "every object id must be unique across the project.")
        self._architectures[source.id] = architecture
        self.touch()

    set_architecture = add_architecture      # the original name, from when a project held one drawing

    def _arch_holding(self, object_id: str) -> Optional[ArchitecturalInterpretation]:
        return next((a for a in self._architectures.values() if a.has(object_id)), None)

    def _arch_object(self, object_id: str):
        arch = self._arch_holding(object_id)
        if arch is None:
            raise ValidationError(f"Unknown architectural object {object_id!r}.")
        return arch, arch.get(object_id)

    def _engineer_review_decision(self, decision: EngineeringDecision) -> None:
        if decision.source != DecisionSource.ENGINEER or decision.status != DecisionStatus.ACCEPTED:
            raise ValidationError("A review action needs an accepted decision whose source is the engineer.")
        if decision.field is not None:
            raise ValidationError("A review decision is not a field change; use set_value() for that.")
        self._check_new_decision(decision)

    def _review_status(self, target: Target, decision_id: str, note: str) -> None:
        self._value_status[(target.scope.value, target.id, "review")] = ValueStatusRecord(
            target, "review", ValueStatus.ENGINEER_DEFINED, decision_id=decision_id, note=note)

    def review_views(self, view_ids, decision: EngineeringDecision, *, accept: bool = True) -> None:
        """The engineer accepts (or rejects) views. One engineer decision covers them all; nothing changes
        if any view is unknown or already superseded."""
        self._engineer_review_decision(decision)
        found = []
        for vid in view_ids:
            arch, v = self._arch_object(vid)
            if not isinstance(v, DrawingView) or v.review == ReviewStatus.SUPERSEDED:
                raise ValidationError(f"{vid} is not a reviewable view.")
            found.append((arch, v))
        status = ReviewStatus.ACCEPTED if accept else ReviewStatus.REJECTED
        self._store_decision(decision)
        for arch, v in found:
            arch.replace(dataclass_replace(v, review=status))
            self._review_status(Target.architectural(v.id), decision.id, status.value)
        self.touch()

    def review_observations(self, observation_ids, decision: EngineeringDecision, *, accept: bool = True) -> None:
        """The engineer accepts (or rejects) what the drawing appears to show at these places. Approving an observation
        approves ONLY that it is there and what it appears to be; it makes no structural statement."""
        self._engineer_review_decision(decision)
        found = []
        for oid in observation_ids:
            arch, o = self._arch_object(oid)
            if not isinstance(o, ArchitecturalObservation) or o.review == ReviewStatus.SUPERSEDED:
                raise ValidationError(f"{oid} is not a reviewable observation.")
            found.append((arch, o))
        status = ReviewStatus.ACCEPTED if accept else ReviewStatus.REJECTED
        self._store_decision(decision)
        for arch, o in found:
            arch.replace(dataclass_replace(o, review=status))
            self._review_status(Target.architectural(o.id), decision.id, status.value)
        self.touch()

    def reconsider_views(self, view_ids, decision: EngineeringDecision) -> None:
        """The engineer takes an earlier decision about views back to "needs review" (an accepted or a rejected view). It is a new
        engineer decision: the earlier one stays on record, and nothing is deleted or resurrected without a decision."""
        self._engineer_review_decision(decision)
        found = []
        for vid in view_ids:
            arch, v = self._arch_object(vid)
            if not isinstance(v, DrawingView) or v.review not in (ReviewStatus.ACCEPTED, ReviewStatus.REJECTED):
                raise ValidationError(f"{vid} is not an accepted or rejected view; there is nothing to reconsider.")
            found.append((arch, v))
        self._store_decision(decision)
        for arch, v in found:
            arch.replace(dataclass_replace(v, review=ReviewStatus.PROPOSED))
            self._review_status(Target.architectural(v.id), decision.id, "reconsidered")
        self.touch()

    def change_view_type(self, view_id: str, new_type, decision: EngineeringDecision) -> ValueStatusRecord:
        """The engineer corrects what kind of view this is (plan, section, elevation, detail ...) WITHOUT rejecting it: the view stays, keeps its
        evidence, and its type changes through the ordinary value mechanism, so Oracle's original type stays in `decision.previous_value` and in the
        history. Only a floor plan carries a level key, so leaving the plan type drops the level Oracle had read for it (the caller says so in the
        decision's instruction); nothing is changed if the correction is refused."""
        arch, view = self._arch_object(view_id)
        if not isinstance(view, DrawingView) or view.review == ReviewStatus.SUPERSEDED:
            raise ValidationError(f"{view_id} is not a view whose type can be corrected.")
        new = parse_enum(ViewType, new_type, "view type")
        if new == view.view_type:
            raise ValidationError(f"{view_id} is already a {new.value}.")
        if new != ViewType.FLOOR_PLAN and view.level_key is not None:
            arch.replace(dataclass_replace(view, level_key=None))
        try:
            record = self.set_value(Target.architectural(view_id), "view_type", new.value, decision)
        except ValidationError:
            arch.replace(view)
            raise
        if view.level_key is not None and new != ViewType.FLOOR_PLAN:      # an engineer's earlier ruling on the dropped level is history now
            for earlier in self.decision_history(Target.architectural(view_id), "level_key"):
                if earlier.status == DecisionStatus.ACCEPTED:
                    earlier.supersede(decision.id)
            self._value_status.pop(("architectural", view_id, "level_key"), None)
        return record

    def merge_views(self, view_ids, new_id: str, decision: EngineeringDecision) -> DrawingView:
        """The engineer says several views are one. The originals are kept, marked superseded; the new view
        takes over their observations. Its frames are cleared: the interpreter must derive them again."""
        self._engineer_review_decision(decision)
        return self._merge_views(view_ids, new_id, decision, store=True)

    def _merge_views(self, view_ids, new_id: str, decision: EngineeringDecision, *, store: bool) -> DrawingView:
        holders = [self._arch_object(v) for v in view_ids]
        views = [v for _a, v in holders]
        if len(views) < 2 or not all(isinstance(v, DrawingView) and v.review != ReviewStatus.SUPERSEDED for v in views):
            raise ValidationError("Merging needs at least two current views.")
        arch = holders[0][0]
        if any(a is not arch for a, _v in holders):
            raise ValidationError("Only views of one source can be merged.")
        if len({v.view_type for v in views}) != 1:
            raise ValidationError("Only views of the same type can be merged.")
        keys = {v.level_key for v in views if v.level_key is not None}
        if len(keys) > 1:
            raise ValidationError(f"The views name different levels {sorted(keys)}; decide the level first.")
        boxes = [v.bbox for v in views]
        merged = DrawingView(
            new_id, views[0].view_type, (min(b[0] for b in boxes), min(b[1] for b in boxes),
                                          max(b[2] for b in boxes), max(b[3] for b in boxes)),
            min(v.confidence for v in views), views[0].title, next(iter(keys), None), views[0].section_label,
            views[0].orientation, None, None, tuple(dict.fromkeys(e for v in views for e in v.entity_ids)),
            ReviewStatus.ACCEPTED, variant=views[0].variant)
        if self._arch_holding(new_id) is not None:
            raise ValidationError(f"Duplicate architectural id {new_id!r}.")
        if store:
            self._store_decision(decision)
        arch.add(merged)
        for v in views:
            arch.replace(dataclass_replace(v, review=ReviewStatus.SUPERSEDED, superseded_by=(new_id,)))
            for o in arch.observations_in(v.id):
                arch.replace(dataclass_replace(o, view_id=new_id))
        self._review_status(Target.architectural(new_id), decision.id, "accepted")
        self.touch()
        return merged

    def split_view(self, view_id: str, parts: Mapping[str, Mapping[str, Any]], decision: EngineeringDecision) -> list:
        """The engineer says one view is several. `parts` maps each new view id to {"bbox": ..., "entity_ids": [...]},
        and together the parts must contain exactly the original's entities. The original is kept, superseded."""
        self._engineer_review_decision(decision)
        return self._split_view(view_id, parts, decision, store=True)

    def _split_view(self, view_id: str, parts: Mapping[str, Mapping[str, Any]], decision: EngineeringDecision, *,
                    store: bool) -> list:
        arch, original = self._arch_object(view_id)
        if not isinstance(original, DrawingView) or original.review == ReviewStatus.SUPERSEDED:
            raise ValidationError(f"{view_id} is not a current view.")
        if len(parts) < 2:
            raise ValidationError("Splitting needs at least two parts.")
        assigned = [e for part in parts.values() for e in part["entity_ids"]]
        if sorted(assigned) != sorted(original.entity_ids) or len(set(assigned)) != len(assigned):
            raise ValidationError("The parts must contain each of the original view's entities exactly once.")
        for new_id in parts:
            if self._arch_holding(new_id) is not None:
                raise ValidationError(f"Duplicate architectural id {new_id!r}.")
        new_views = [DrawingView(new_id, original.view_type, tuple(part["bbox"]), original.confidence, original.title,
                                 original.level_key, original.section_label, original.orientation, original.frame_id,
                                 None, tuple(part["entity_ids"]), ReviewStatus.ACCEPTED, variant=original.variant)
                     for new_id, part in parts.items()]
        if store:
            self._store_decision(decision)
        for v in new_views:
            arch.add(v)
            self._review_status(Target.architectural(v.id), decision.id, "accepted")
        owner = {e: v.id for v in new_views for e in v.entity_ids}
        for o in arch.observations_in(view_id):
            arch.replace(dataclass_replace(o, view_id=next((owner[e] for e in o.entity_ids if e in owner),
                                                            new_views[0].id)))
        arch.replace(dataclass_replace(original, review=ReviewStatus.SUPERSEDED,
                                       superseded_by=tuple(v.id for v in new_views)))
        self.touch()
        return new_views

    def next_frame_id(self) -> str:
        """The next unused coordinate-frame id, unique across every source in the project."""
        highest = max((int(f.id[4:]) for a in self._architectures.values() for f in a.frames), default=0)
        return f"FRM-{highest + 1:02d}"

    def align_view(self, view_id: str, translation, decision: EngineeringDecision, *, rotation_deg: float = 0.0) -> CoordinateFrame:
        """The engineer says how a plan lines up with the building: building = view-local + translation (in the source
        drawing's units). With a `rotation_deg` (a two-point alignment) the plan is also turned: view-local = R(rotation) * building -
        translation; scale is never changed (the frame keeps scale 1). The alignment frame is created and the view's alignment_frame_id is set THROUGH set_value,
        so the change has the ordinary history. `decision` must be an accepted ENGINEER decision on the view's
        alignment_frame_id whose value is the new frame's id (see next_frame_id())."""
        arch, view = self._arch_object(view_id)
        if not isinstance(view, DrawingView) or view.view_type != ViewType.FLOOR_PLAN or view.frame_id is None:
            raise ValidationError(f"{view_id} is not a floor plan with a coordinate frame.")
        frame = CoordinateFrame(decision.value, f"{view_id} to building (engineer)", view.frame_id,
                                (-float(translation[0]), -float(translation[1])), float(rotation_deg))
        if decision.field != "alignment_frame_id" or decision.target != Target.architectural(view_id):
            raise ValidationError("An alignment decision must be about the view's alignment_frame_id.")
        arch.add(frame)
        try:
            self.set_value(decision.target, "alignment_frame_id", frame.id, decision)
        except ValidationError:
            arch.discard_frame(frame.id)                   # nothing half-applied
            raise
        return frame

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
        if any((self._decisions, self._issues, self._provenance, self._value_status, self._interpretations,
                self._evidence_links)):
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

    def _check_engineer_change(self, target: Target, field: str, value: Any, decision: EngineeringDecision,
                               current: dict) -> None:
        """The checks every engineer value change must pass, whatever kind of object it is about."""
        check_field_path(field, "field")
        if "." in field:
            raise ValidationError("set_value takes a top-level field; replace the whole value (e.g. the section).")
        if field in ("id", "index") or field not in current:
            raise ValidationError(f"{target.id} has no changeable field {field!r}.")
        if decision.source != DecisionSource.ENGINEER or decision.status != DecisionStatus.ACCEPTED:
            raise ValidationError("A value is only changed by an accepted decision whose source is the engineer.")
        if (decision.target, decision.field, decision.value) != (target, field, value):
            raise ValidationError("The decision must name exactly the target, field and value being applied.")

    def _commit_engineer_value(self, target: Target, field: str, value: Any, decision: EngineeringDecision,
                               previous: Any) -> ValueStatusRecord:
        """Record an engineer's value: the decision (with the value it replaced), the supersession of earlier accepted
        decisions on the same field, and the value's status. The single place this history is written."""
        decision.previous_value = previous
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

    def set_value(self, target: Target, field: str, value: Any, decision: EngineeringDecision) -> ValueStatusRecord:
        """Apply an engineer's decision to one field of a level, an element or an architectural object.

        The decision (source ENGINEER, ACCEPTED, naming this target, field and value) is recorded, the object is
        rebuilt and fully re-validated with the new value, the field becomes ENGINEER_OVERRIDE if it already had a
        status (something the source, Oracle or an earlier engineer set) or ENGINEER_DEFINED if it had none, and every
        earlier accepted decision on the same field is marked superseded by this one. The old value stays in
        decision.previous_value. Nothing changes if any step is rejected. Only top-level fields are supported (e.g.
        'section', 'thickness_mm', a level's 'elevation_mm').

        Levels are not special-cased: a level change goes through the same checks and the same history. What is
        specific to levels is only that levels depend on each other (see oracle.core.level_changes): moving one level,
        or changing a storey height, may move the levels above it, and each such consequence is recorded as its own
        engineer decision, authored by the same engineer, that says what caused it."""
        if target.scope == TargetScope.LEVEL and self._building is not None:
            return self._set_level_value(target, field, value, decision)
        if target.scope == TargetScope.ELEMENT and self._building is not None:
            element, replace_object = self._building.get_element(target.id), self._building.replace_element
        elif target.scope == TargetScope.ARCHITECTURAL and self._arch_holding(target.id) is not None:
            arch = self._arch_holding(target.id)
            element, replace_object = arch.get(target.id), arch.replace
        else:
            raise ValidationError("set_value applies to levels, structural elements and architectural interpretation "
                                  "objects of a project that has them.")
        old = element.to_dict()
        self._check_engineer_change(target, field, value, decision, old)
        changed = dict(old)
        changed[field] = value
        replacement = type(element).from_dict(changed)  # raises if the new value is not valid for the element
        self._check_new_decision(decision)
        replace_object(replacement)  # last step that can fail; nothing else has changed yet
        return self._commit_engineer_value(target, field, value, decision, old[field])

    def _set_level_value(self, target: Target, field: str, value: Any, decision: EngineeringDecision) -> ValueStatusRecord:
        building = self._building
        level = building.get_level(target.id)
        current = level.to_full_dict()
        self._check_engineer_change(target, field, value, decision, current)
        changed = dict(current)
        changed[field] = value
        replacement = Level.from_dict(changed)
        plan = plan_level_change(self, level, replacement, field)
        self._check_new_decision(decision)
        cascade = []
        for n, (level_id, cfield, new, old) in enumerate(plan.cascaded, 1):
            cid = f"{decision.id}.S{n}"
            if cid in self._decisions:
                raise ValidationError(f"Duplicate decision id {cid!r}.")
            cascade.append((Target.level(level_id), cfield, new, old, EngineeringDecision(
                cid, decision.author, DecisionSource.ENGINEER, Target.level(level_id), decision.category,
                f"Moved with {decision.id}: changing {target.id}.{field} to {value!r} shifts {level_id}.{cfield} "
                f"from {old!r} to {new!r}.", reason=decision.reason, status=DecisionStatus.ACCEPTED, field=cfield, value=new)))
        building.replace_levels(plan.levels)                  # atomic and last to fail: nothing else has changed yet
        record = self._commit_engineer_value(target, field, value, decision, current[field])
        for ctarget, cfield, new, old, cdecision in cascade:
            self._commit_engineer_value(ctarget, cfield, new, cdecision, old)
        for level_id, step, old in plan.derived_heights:
            self.set_value_status(ValueStatusRecord(
                Target.level(level_id), "storey_height_mm", ValueStatus.DERIVED,
                note=f"recomputed from the level elevations after {decision.id} (was {old!r})"))
        return record

    # ---- evidence links ----

    @property
    def evidence_links(self) -> list:
        return list(self._evidence_links.values())

    def get_evidence_link(self, link_id: str) -> EvidenceLink:
        try:
            return self._evidence_links[link_id]
        except KeyError:
            raise ValidationError(f"Unknown evidence link {link_id!r}.") from None

    def links_for(self, subject) -> list:
        """The evidence links whose subject is a Target (or a decision id)."""
        key = ("decision", subject) if isinstance(subject, str) else (subject.scope.value, subject.id)
        return [l for l in self._evidence_links.values() if l.subject_key == key]

    def links_from(self, evidence: Target) -> list:
        """Everything that rests on this piece of evidence."""
        return [l for l in self._evidence_links.values() if l.evidence == evidence]

    def link_evidence(self, subject: Optional[Target], evidence: Target, relation, decision, *,
                      subject_decision_id: Optional[str] = None, note: Optional[str] = None) -> EvidenceLink:
        """The engineer says a domain object (or a decision) was derived from, is supported by, or is constrained by a
        piece of architectural evidence. `decision` is a new accepted ENGINEER decision, or the id of one already
        recorded (so one decision can link several objects). Evidence that has a review status must have been ACCEPTED,
        and so must the view an observation belongs to: an unapproved reading cannot support anything."""
        fresh = isinstance(decision, EngineeringDecision)
        if fresh:
            self._engineer_review_decision(decision)
            decision_id = decision.id
        else:
            decision_id = decision
            self._require_engineer_decision(decision_id, "an evidence link")
        relation = parse_enum(EvidenceRelation, relation, "evidence relation")
        link_id = f"EV-{max((int(l.id[3:]) for l in self._evidence_links.values()), default=0) + 1:04d}"
        link = EvidenceLink(link_id, evidence, relation, decision_id, subject, subject_decision_id, note, utc_now_iso())
        self._check_evidence_link(link, approval=True, pending=decision if fresh else None)
        if any(l.subject_key == link.subject_key and l.evidence == evidence and l.relation == relation
               for l in self._evidence_links.values()):
            raise ValidationError(f"{link.subject_key[1]} is already linked to {evidence.id} as {relation.value}.")
        if fresh:
            self._store_decision(decision)
        self._evidence_links[link_id] = link
        self.touch()
        return link

    def _check_evidence_link(self, link: EvidenceLink, *, approval: bool, pending: Optional[EngineeringDecision] = None,
                             building: Optional[BuildingModel] = None) -> None:
        what = f"evidence link {link.id}"
        building = building or self._building
        if pending is None:
            self._require_engineer_decision(link.decision_id, what)
        if link.subject is not None:
            self._check_target(building, link.subject, what)
        elif link.subject_decision_id not in self._decisions and not (
                pending is not None and link.subject_decision_id == pending.id):
            raise ValidationError(f"{what.capitalize()} names unknown decision {link.subject_decision_id!r}.")
        self._check_target(building, link.evidence, what)
        arch, obj = self._arch_object(link.evidence.id)
        if not isinstance(obj, (DrawingView, ArchitecturalObservation, HeightEvidence)):
            raise ValidationError(f"{what.capitalize()}: {link.evidence.id} is not something a decision can rest on "
                                  "(a view, an observation or a height).")
        if approval:
            if isinstance(obj, (DrawingView, ArchitecturalObservation)) and obj.review != ReviewStatus.ACCEPTED:
                raise ValidationError(f"{link.evidence.id} has not been accepted by an engineer "
                                      f"(it is {obj.review.value}); unapproved evidence cannot support a decision.")
            if isinstance(obj, ArchitecturalObservation):
                view = arch.get(obj.view_id)
                if view.review != ReviewStatus.ACCEPTED:
                    raise ValidationError(f"{link.evidence.id} belongs to {view.id}, which has not been accepted.")

    def trace(self, target):
        """Walk the evidence chain of a target backwards: final object -> decisions -> evidence links -> architectural
        evidence -> provenance -> source drawing and entity identifiers. `target` is a Target or an object id. The
        result says exactly which links are missing; nothing is filled in."""
        return _trace.trace(self, target)

    def approved_architecture(self, source_id: Optional[str] = None):
        """The read-only projection of what an engineer has approved, in domain terms and millimetres, for the
        structural side to consume. See oracle.core.approved."""
        return _approved.approved_architecture(self, source_id)

    # ---- engineer clarifications (free-form engineer input) ----

    @property
    def clarifications(self) -> list:
        return list(self._clarifications.values())

    def get_clarification(self, clarification_id: str) -> EngineerClarification:
        try:
            return self._clarifications[clarification_id]
        except KeyError:
            raise ValidationError(f"Unknown clarification {clarification_id!r}.") from None

    def clarifications_for(self, target: Target) -> list:
        return [c for c in self._clarifications.values() if c.target == target]

    def record_clarification(self, decision: EngineeringDecision, target: Target, statement: str,
                             notes: Optional[str] = None) -> EngineerClarification:
        """Keep the engineer's own words about a target, verbatim, under an accepted engineer decision. Nothing in the model
        changes: the statement is guidance, flagged for the stage that can act on it. See oracle.core.clarifications."""
        return _clarifications.record(self, decision, target, statement, notes)

    def answer_with_engineer_input(self, set_id: str, decision: EngineeringDecision, statement: str, notes: Optional[str] = None,
                                   *, target: Optional[Target] = None, effects: tuple = ()) -> EngineerClarification:
        """The engineer answers an open interpretation set in their own words instead of choosing one of Oracle's readings. `effects`
        (oracle.core.effects) are only for a value the engineer supplied; prose alone never changes the model."""
        return _clarifications.answer_question(self, set_id, decision, statement, notes, target=target, effects=effects)

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
            if a.status.value == "proposed":                  # a resolved set's targets may legitimately have moved on
                _resolution.check_effect_targets(self, s, a)
            for evidence_id in a.evidence:
                if evidence_id not in self._provenance:
                    raise ValidationError(f"Interpretation {a.id} cites unknown provenance record {evidence_id!r}.")
            if a.decision_id is not None:
                self._require_engineer_decision(a.decision_id, f"interpretation {a.id}")

    def open_interpretation_sets(self) -> list:
        return [s for s in self._interpretations.values() if s.status == SetStatus.OPEN]

    def accept_interpretation(self, set_id: str, interpretation_id: str, decision_id: str, *,
                              apply_effects: bool = True) -> None:
        """The engineer chooses one reading; the others are rejected under the same decision, and what the chosen
        reading MEANS (its effects: a value, a height, an alignment, a merge...) is carried out through the ordinary
        engineer-decision machinery and recorded on the alternative. Issues linked to the set that the resolution
        addresses are resolved under the same decision. All or nothing: it is first rehearsed on a copy, and if any
        effect would be refused nothing at all changes. `apply_effects=False` records the choice only, for an
        engineer who supplies the values directly."""
        _resolution.accept(self, set_id, interpretation_id, decision_id, apply_effects=apply_effects)

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
        for arch in self._architectures.values():
            for v in arch.views:
                if (v.view_type in (ViewType.FLOOR_PLAN, ViewType.SECTION, ViewType.ELEVATION)
                        and v.review == ReviewStatus.PROPOSED):
                    result.blockers.append(Blocker(BlockerKind.UNREVIEWED_VIEW, v.id, f"{v.id} ({v.view_type.value}"
                                                   f"{': ' + v.title if v.title else ''}) has not been reviewed."))
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
        if target.scope == TargetScope.ARCHITECTURAL:
            return self._arch_object(target.id)[1].to_dict()
        if target.scope == TargetScope.LEVEL:
            return building.get_level(target.id).to_full_dict()
        getter = {TargetScope.NODE: building.get_node, TargetScope.GRID: building.get_grid,
                  TargetScope.ELEMENT: building.get_element}[target.scope]
        return getter(target.id).to_dict()

    def _check_target(self, building: Optional[BuildingModel], target: Target, what: str) -> None:
        if target.scope == TargetScope.PROJECT:
            return
        if target.scope == TargetScope.ARCHITECTURAL:
            if self._arch_holding(target.id) is None:
                raise ValidationError(f"{what.capitalize()} targets unknown architectural object {target.id!r}.")
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
        for link in self._evidence_links.values():
            self._check_evidence_link(link, approval=False, building=building)

    def validate(self) -> None:
        """Full consistency check: building, design basis and every cross-reference."""
        if self.design_basis is not None:
            self.design_basis.validate()
        if self._building is not None:
            self._building.validate()
        for arch in self._architectures.values():
            arch.validate()
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
        for i in self._issues.values():
            if i.interpretation_set_id is not None and i.interpretation_set_id not in self._interpretations:
                raise ValidationError(f"Issue {i.id} refers to unknown interpretation set {i.interpretation_set_id!r}.")
        seen_alternatives = set()
        for s in self._interpretations.values():
            self._check_interpretation_set(s)
            for a in s.alternatives:
                if a.id in seen_alternatives:
                    raise ValidationError(f"Duplicate interpretation id {a.id!r}.")
                seen_alternatives.add(a.id)
        _resolution.check_resolutions(self)
        _clarifications.check_clarifications(self)

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
            if (d.field is not None and d.target.scope in (TargetScope.ELEMENT, TargetScope.ARCHITECTURAL, TargetScope.LEVEL)
                    and d.source == DecisionSource.ENGINEER and d.status == DecisionStatus.ACCEPTED):
                key = (d.target.scope.value, d.target.id, d.field)
                if key in latest:
                    raise ValidationError(f"Decisions {latest[key].id} and {d.id} both stand on "
                                          f"{d.target.id}.{d.field}; the earlier one should be superseded.")
                latest[key] = d
        for (_scope_name, element_id, field), d in latest.items():
            scope = d.target.scope
            if scope == TargetScope.ARCHITECTURAL:
                current = self._arch_object(element_id)[1].to_dict().get(field)
            elif scope == TargetScope.LEVEL:
                current = self._building.get_level(element_id).to_full_dict().get(field)
            else:
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
            "architectures": [a.to_dict() for a in self._architectures.values()],
            "evidence_links": [l.to_dict() for l in self._evidence_links.values()],
            "clarifications": [c.to_dict() for c in self._clarifications.values()],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OracleProject":
        data = migrate(data)  # refuses unknown versions; upgrades older ones without inventing content
        check_keys(data, required={"schema_version", "project_id", "name", "engineer", "created_at", "modified_at",
                                   "decisions", "issues", "provenance", "value_status", "interpretations",
                                   "architectures", "evidence_links", "clarifications"},
                   optional={"oracle_version", "description", "client", "location", "design_basis", "building"},
                   where="project")
        project = cls(data["project_id"], data["name"], data["engineer"], description=data.get("description"),
                      client=data.get("client"), location=data.get("location"),
                      created_at=data["created_at"], modified_at=data["modified_at"])
        if data.get("design_basis") is not None:
            project.design_basis = DesignBasis.from_dict(data["design_basis"])
        if data.get("building") is not None:
            project._building = BuildingModel.from_dict(data["building"])
        rows = data["architectures"]
        if not isinstance(rows, list):
            raise ValidationError("architectures must be a list.")
        for row in rows:
            arch = ArchitecturalInterpretation.from_dict(row)
            if arch.drawing.id in project._architectures:
                raise ValidationError(f"Duplicate drawing source {arch.drawing.id!r}.")
            project._architectures[arch.drawing.id] = arch
        clarifications = [EngineerClarification.from_dict(c) for c in data["clarifications"]]
        check_unique_ids((c.id for c in clarifications), "clarification id")
        project._clarifications = {c.id: c for c in clarifications}
        links = [EvidenceLink.from_dict(l) for l in data["evidence_links"]]
        check_unique_ids((l.id for l in links), "evidence link id")
        project._evidence_links = {l.id: l for l in links}
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
