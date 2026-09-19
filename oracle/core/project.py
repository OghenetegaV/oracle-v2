"""Oracle — Core Project Aggregate

Purpose:
    OracleProject: the top-level aggregate for one engineering project. It holds identity and
    metadata, the design basis, the building model, decisions and issues, enforces the
    cross-references between them, and reads and writes deterministic, versioned JSON.

Role in Oracle:
    The canonical project object that future adapters, engines and the GUI will read and write.
    Phase 1 provides it alongside the legacy workflow; the legacy code does not use it yet.

Dependencies:
    oracle (version), and every other oracle.core module.

Consumers:
    tests; future adapters.

Status:
    Core.

Migration:
    Remains. Analysis, design, reinforcement, drawing and revision records will be added as
    schema versions, each with a migration.
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
    SCHEMA_VERSION, SchemaVersionError, Target, TargetScope, ValidationError, check_id, check_keys,
    check_optional_text, check_text, check_timestamp, check_unique_ids, parse_enum, utc_now_iso,
)
from .decisions import DecisionStatus, EngineeringDecision
from .design_basis import DesignBasis
from .issues import EngineeringIssue, IssueSeverity


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
        if self._decisions or self._issues:
            self._check_targets(building, self._decisions.values(), self._issues.values())
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
        if decision.id in self._decisions:
            raise ValidationError(f"Duplicate decision id {decision.id!r}.")
        self._check_target(self._building, decision.target, f"decision {decision.id}")
        self._decisions[decision.id] = decision
        self.touch()
        return decision

    def decisions_for(self, target: Target) -> list:
        return [d for d in self._decisions.values() if d.target == target]

    def set_decision_status(self, decision_id: str, status: DecisionStatus) -> None:
        status = parse_enum(DecisionStatus, status, "decision status")
        if status == DecisionStatus.SUPERSEDED:
            raise ValidationError("Use supersede_decision() to mark a decision as superseded.")
        decision = self.get_decision(decision_id)
        decision.status = status
        decision.superseded_by = None
        decision.__post_init__()
        self.touch()

    def supersede_decision(self, old_id: str, new_id: str) -> None:
        old, _ = self.get_decision(old_id), self.get_decision(new_id)
        old.supersede(new_id)
        self.touch()

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
        self._check_target(self._building, issue.target, f"issue {issue.id}")
        if issue.decision_id is not None:
            self.get_decision(issue.decision_id)
        self._issues[issue.id] = issue
        self.touch()
        return issue

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
            self.get_decision(decision_id)
        self.get_issue(issue_id).accept(reason, decision_id)
        self.touch()

    # ---- validation ----

    @staticmethod
    def _check_target(building: Optional[BuildingModel], target: Target, what: str) -> None:
        if target.scope == TargetScope.PROJECT:
            return
        if building is None:
            raise ValidationError(f"{what.capitalize()} targets {target.scope.value} {target.id!r} but the "
                                  "project has no building model yet.")
        try:
            if target.scope == TargetScope.LEVEL:
                building.get_level(target.id)
            else:
                building.get_element(target.id)
        except ValidationError:
            raise ValidationError(f"{what.capitalize()} targets unknown {target.scope.value} {target.id!r}.") from None

    @classmethod
    def _check_targets(cls, building, decisions, issues) -> None:
        for d in decisions:
            cls._check_target(building, d.target, f"decision {d.id}")
        for i in issues:
            cls._check_target(building, i.target, f"issue {i.id}")

    def validate(self) -> None:
        """Full consistency check: building, design basis and every cross-reference."""
        if self.design_basis is not None:
            self.design_basis.validate()
        if self._building is not None:
            self._building.validate()
        self._check_targets(self._building, self._decisions.values(), self._issues.values())
        for d in self._decisions.values():
            if d.superseded_by is not None and d.superseded_by not in self._decisions:
                raise ValidationError(f"Decision {d.id} is superseded by unknown decision {d.superseded_by!r}.")
        for i in self._issues.values():
            if i.decision_id is not None and i.decision_id not in self._decisions:
                raise ValidationError(f"Issue {i.id} references unknown decision {i.decision_id!r}.")

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
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OracleProject":
        if not isinstance(data, Mapping):
            raise ValidationError("A project file must contain a JSON object.")
        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            raise SchemaVersionError(
                f"Project schema_version {version!r} is not supported by this Oracle (expects {SCHEMA_VERSION!r}).")
        check_keys(data, required={"schema_version", "project_id", "name", "engineer", "created_at", "modified_at",
                                   "decisions", "issues"},
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
        check_unique_ids((d.id for d in decisions), "decision id")
        check_unique_ids((i.id for i in issues), "issue id")
        project._decisions = {d.id: d for d in decisions}
        project._issues = {i.id: i for i in issues}
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
