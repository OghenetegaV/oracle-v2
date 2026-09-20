"""Oracle — Core Engineering Issues

Purpose:
    EngineeringIssue: a problem found in the model or design, with severity (info to blocking),
    category, target, source, status (open/resolved/accepted), resolution and the decision that
    settled it, plus the evidence (provenance record IDs) that shows it, related objects, and the
    interpretation it concerns.

Role in Oracle:
    Structured replacement for transient parser warnings (ParseIssue). Accepting an error or blocking
    issue requires a recorded engineer decision (the project also requires that decision to be the
    engineer's). An open BLOCKING issue keeps the project from being ready for final output.

Dependencies:
    oracle.core.common.

Consumers:
    oracle.core.project (stores, cross-checks, readiness); adapters and future checks create issues.

Status:
    Core (extended in schema 0.2.0).

Migration/Notes:
    0.2.0 added evidence, interpretation_id and related (optional, so 0.1.0 issues load unchanged).
    Parsers and design checks create issues through adapters.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional

from .common import (
    Target, ValidationError, check_id, check_keys, check_optional_text, check_text, parse_enum,
)


class IssueSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKING = "blocking"  # work must not proceed past this until it is resolved or formally accepted


class IssueStatus(str, Enum):
    OPEN = "open"
    RESOLVED = "resolved"  # the underlying problem was fixed
    ACCEPTED = "accepted"  # the problem stands and the engineer knowingly accepts it


class IssueCategory(str, Enum):
    AMBIGUOUS_GEOMETRY = "ambiguous_geometry"
    MISSING_SUPPORT = "missing_support"
    UNSUPPORTED_OPENING = "unsupported_opening"
    DISCONTINUOUS_COLUMN = "discontinuous_column"
    UNSUPPORTED_BEAM = "unsupported_beam"
    SUSPICIOUS_SPAN = "suspicious_span"
    EXCESSIVE_DEFLECTION = "excessive_deflection"
    REINFORCEMENT_CONFLICT = "reinforcement_conflict"
    INCOMPLETE_INFORMATION = "incomplete_information"
    DESIGN_CHECK_FAILURE = "design_check_failure"
    OTHER = "other"


_SEVERITIES_NEEDING_DECISION_TO_ACCEPT = (IssueSeverity.ERROR, IssueSeverity.BLOCKING)


@dataclass
class EngineeringIssue:
    id: str
    severity: IssueSeverity
    category: IssueCategory
    message: str
    target: Target
    source: str                              # which module/check raised it, e.g. "oracle.adapters.legacy_ga"
    status: IssueStatus = IssueStatus.OPEN
    resolution: Optional[str] = None
    decision_id: Optional[str] = None        # the EngineeringDecision that settled it, if any
    evidence: tuple = ()                     # provenance record IDs that show the problem
    interpretation_id: Optional[str] = None  # the interpretation this issue concerns, if any
    related: tuple = ()                      # other objects involved (Targets), e.g. the column and the beam
    interpretation_set_id: Optional[str] = None  # the open question (InterpretationSet) this issue is the visible face of

    def __post_init__(self):
        check_id(self.id, "issue id")
        self.severity = parse_enum(IssueSeverity, self.severity, "issue severity")
        self.category = parse_enum(IssueCategory, self.category, "issue category")
        self.status = parse_enum(IssueStatus, self.status, "issue status")
        check_text(self.message, "issue message")
        if not isinstance(self.target, Target):
            raise ValidationError("Issue target must be a Target.")
        check_text(self.source, "issue source")
        check_optional_text(self.resolution, "issue resolution")
        if self.decision_id is not None:
            check_id(self.decision_id, "issue decision_id")
        self.evidence = tuple(check_id(e, "issue evidence id") for e in self.evidence)
        if self.interpretation_id is not None:
            check_id(self.interpretation_id, "issue interpretation_id")
        if self.interpretation_set_id is not None:
            check_id(self.interpretation_set_id, "issue interpretation_set_id")
        self.related = tuple(self.related)
        if not all(isinstance(r, Target) for r in self.related):
            raise ValidationError(f"Issue {self.id}: related objects must be Targets.")
        if self.status != IssueStatus.OPEN and not self.resolution:
            raise ValidationError(f"Issue {self.id}: a {self.status.value} issue needs a resolution note.")
        if (self.status == IssueStatus.ACCEPTED and self.severity in _SEVERITIES_NEEDING_DECISION_TO_ACCEPT
                and self.decision_id is None):
            raise ValidationError(
                f"Issue {self.id}: accepting a {self.severity.value} issue requires the engineer decision that accepts it.")

    @property
    def is_open(self) -> bool:
        return self.status == IssueStatus.OPEN

    def resolve(self, resolution: str, decision_id: Optional[str] = None) -> None:
        self._close(IssueStatus.RESOLVED, resolution, decision_id)

    def accept(self, reason: str, decision_id: Optional[str] = None) -> None:
        self._close(IssueStatus.ACCEPTED, reason, decision_id)

    def _close(self, status: IssueStatus, note: str, decision_id: Optional[str]) -> None:
        previous = (self.status, self.resolution, self.decision_id)
        self.status, self.resolution, self.decision_id = status, note, decision_id
        try:
            self.__post_init__()
        except ValidationError:
            self.status, self.resolution, self.decision_id = previous
            raise

    def to_dict(self) -> dict:
        out = {"id": self.id, "severity": self.severity.value, "category": self.category.value,
                "message": self.message, "target": self.target.to_dict(), "source": self.source,
                "status": self.status.value, "resolution": self.resolution, "decision_id": self.decision_id,
                "evidence": list(self.evidence), "interpretation_id": self.interpretation_id,
                "related": [r.to_dict() for r in self.related]}
        if self.interpretation_set_id is not None:
            out["interpretation_set_id"] = self.interpretation_set_id
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EngineeringIssue":
        check_keys(data, required={"id", "severity", "category", "message", "target", "source", "status"},
                   optional={"resolution", "decision_id", "evidence", "interpretation_id", "related",
                             "interpretation_set_id"},
                   where="issue")
        return cls(data["id"], data["severity"], data["category"], data["message"],
                   Target.from_dict(data["target"]), data["source"], data["status"],
                   data.get("resolution"), data.get("decision_id"), tuple(data.get("evidence") or ()),
                   data.get("interpretation_id"), tuple(Target.from_dict(r) for r in data.get("related") or ()),
                   data.get("interpretation_set_id"))
