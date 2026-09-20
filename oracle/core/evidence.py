"""Oracle — Evidence Links (domain object -> source evidence)

Purpose:
    EvidenceLink: a typed, validated, serialisable statement that a domain object (a level, an element, a
    node, a grid line) or an engineering decision was DERIVED FROM, SUPPORTED BY or CONSTRAINED BY a piece of
    source evidence (an interpreted view, observation or height in the architectural interpretation), made
    under an accepted ENGINEER decision. It replaces the earlier convention of writing an observation's id into
    a provenance record's free-text source_id.

Role in Oracle:
    The join in the evidence chain: final object -> (this link, backed by an engineer decision) -> architectural
    evidence -> provenance -> source drawing and entity. The core knows only that evidence lives in the
    ARCHITECTURAL target scope; it does not know what an observation, a view, a DXF entity or a CAD layer is.
    The project enforces that a link is made only by an engineer, that the evidence exists and has been approved
    where approval applies, and that the subject exists.

Dependencies:
    oracle.core.common.

Consumers:
    oracle.core.project (registry and validation), oracle.core.trace (walks links backwards), the future
    structural reasoning layer (records why each object exists), tests.

Status:
    Core (added in schema 0.4.0).

Migration/Notes:
    New in schema 0.4.0 as the top-level `evidence_links` registry (empty in every migrated project).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional

from .common import (
    Target, TargetScope, ValidationError, check_id, check_keys, check_optional_text, check_timestamp, parse_enum,
)

_LINK_ID = re.compile(r"^EV-\d+$")


class EvidenceRelation(str, Enum):
    DERIVED_FROM = "derived_from"        # the subject exists because of this evidence
    SUPPORTED_BY = "supported_by"        # the evidence corroborates the subject
    CONSTRAINED_BY = "constrained_by"    # the evidence limits what the subject may be


@dataclass(frozen=True)
class EvidenceLink:
    id: str
    evidence: Target                      # what the subject rests on (ARCHITECTURAL scope)
    relation: EvidenceRelation
    decision_id: str                      # the accepted engineer decision that established the link
    subject: Optional[Target] = None      # a level, element, node or grid line ...
    subject_decision_id: Optional[str] = None  # ... or an engineering decision (exactly one of the two)
    note: Optional[str] = None
    created_at: str = ""

    def __post_init__(self):
        if not isinstance(self.id, str) or not _LINK_ID.match(self.id):
            raise ValidationError(f"Invalid evidence link id {self.id!r}: expected the form EV-<number>.")
        if not isinstance(self.evidence, Target) or self.evidence.scope != TargetScope.ARCHITECTURAL:
            raise ValidationError(f"Evidence link {self.id}: the evidence must be an architectural-scope target.")
        object.__setattr__(self, "relation", parse_enum(EvidenceRelation, self.relation, "evidence relation"))
        check_id(self.decision_id, "evidence link decision_id")
        if (self.subject is None) == (self.subject_decision_id is None):
            raise ValidationError(f"Evidence link {self.id}: name exactly one subject, an object or a decision.")
        if self.subject is not None:
            if not isinstance(self.subject, Target) or self.subject.scope in (TargetScope.PROJECT, TargetScope.ARCHITECTURAL):
                raise ValidationError(f"Evidence link {self.id}: the subject must be a level, element, node or grid line.")
        else:
            check_id(self.subject_decision_id, "evidence link subject decision id")
        check_optional_text(self.note, "evidence link note")
        if self.created_at:
            check_timestamp(self.created_at, "evidence link created_at")

    @property
    def subject_key(self) -> tuple:
        return ("decision", self.subject_decision_id) if self.subject is None else (self.subject.scope.value, self.subject.id)

    def to_dict(self) -> dict:
        return {"id": self.id, "subject": self.subject.to_dict() if self.subject else None,
                "subject_decision_id": self.subject_decision_id, "evidence": self.evidence.to_dict(),
                "relation": self.relation.value, "decision_id": self.decision_id, "note": self.note,
                "created_at": self.created_at}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EvidenceLink":
        check_keys(data, required={"id", "evidence", "relation", "decision_id"},
                   optional={"subject", "subject_decision_id", "note", "created_at"}, where="evidence link")
        return cls(data["id"], Target.from_dict(data["evidence"]), data["relation"], data["decision_id"],
                   Target.from_dict(data["subject"]) if data.get("subject") else None, data.get("subject_decision_id"),
                   data.get("note"), data.get("created_at") or "")
