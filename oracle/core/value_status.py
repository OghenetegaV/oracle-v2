"""Oracle — Core Value Status

Purpose:
    ValueStatus and ValueStatusRecord: the current epistemic state of one value, so an assumed or
    inferred value can never be mistaken for a confirmed engineering fact. There is one record per
    (target, field). The states are SOURCE (read from the source), INFERRED (deduced from source by
    an interpreter), ASSUMED (a placeholder for missing information), ENGINEER_DEFINED (the engineer
    stated it), ENGINEER_OVERRIDE (the engineer replaced a value Oracle or the source had given),
    CALCULATED (a result of analysis or design calculation) and DERIVED (a deterministic consequence
    of other values, e.g. a storey height from two elevations).

Role in Oracle:
    Answers "how far can this value be trusted?", while oracle.core.provenance answers "where did it
    come from?". Example: a column outline is SOURCE; its structural centre is INFERRED; a 300x300
    section Oracle had to guess is ASSUMED; the engineer's 350x350 is ENGINEER_OVERRIDE, pointing at the
    decision that made it; reinforcement demand from STAAD is CALCULATED; the final arrangement built
    from it is DERIVED. Records are current state only: the history of changes is the chain of
    EngineeringDecisions, and evidence is in the provenance registry.

    ENGINEER_DEFINED and ENGINEER_OVERRIDE need a decision_id: an engineer's word is always backed by
    a recorded engineer decision (project-level validation checks that it exists, is the engineer's
    and is accepted). Assumed values are unconfirmed: they keep a project from being ready for final
    engineering output (see oracle.core.readiness).

Dependencies:
    oracle.core.common (Target, validators).

Consumers:
    oracle.core.project (stores, cross-checks, serialises and updates records on engineer overrides);
    oracle.core.readiness; adapters.

Status:
    Core (schema 0.2.0).

Migration/Notes:
    New in schema 0.2.0; migrated projects start with none, and unrecorded values are therefore of
    unknown status, not silently 'source'.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional

from .common import (
    Target, TargetScope, ValidationError, check_field_path, check_id, check_keys, check_optional_text, parse_enum,
)


class ValueStatus(str, Enum):
    SOURCE = "source"
    INFERRED = "inferred"
    ASSUMED = "assumed"
    ENGINEER_DEFINED = "engineer_defined"
    ENGINEER_OVERRIDE = "engineer_override"
    CALCULATED = "calculated"
    DERIVED = "derived"


ENGINEER_STATUSES = frozenset({ValueStatus.ENGINEER_DEFINED, ValueStatus.ENGINEER_OVERRIDE})


@dataclass
class ValueStatusRecord:
    target: Target
    field: str
    status: ValueStatus
    decision_id: Optional[str] = None        # required for the ENGINEER_* statuses
    replaces: Optional[ValueStatus] = None   # for ENGINEER_OVERRIDE: the status of the value that was replaced
    provenance_ids: tuple = ()               # evidence for this value's current state
    note: Optional[str] = None

    def __post_init__(self):
        if not isinstance(self.target, Target):
            raise ValidationError("Value-status target must be a Target.")
        if self.target.scope == TargetScope.PROJECT:
            raise ValidationError("A value status must be about a level, element, node or grid line, not the whole project.")
        check_field_path(self.field, "value-status field")
        self.status = parse_enum(ValueStatus, self.status, "value status")
        if self.replaces is not None:
            self.replaces = parse_enum(ValueStatus, self.replaces, "replaced value status")
            if self.status != ValueStatus.ENGINEER_OVERRIDE:
                raise ValidationError("'replaces' only applies to an ENGINEER_OVERRIDE.")
        if self.status in ENGINEER_STATUSES:
            if self.decision_id is None:
                raise ValidationError(f"{self.target.id}.{self.field}: a {self.status.value} value needs the "
                                      "engineer decision that made it (decision_id).")
            check_id(self.decision_id, "value-status decision_id")
        elif self.decision_id is not None:
            raise ValidationError(f"{self.target.id}.{self.field}: only engineer-defined or overridden values "
                                  "carry a decision_id.")
        self.provenance_ids = tuple(check_id(i, "value-status provenance id") for i in self.provenance_ids)
        check_optional_text(self.note, "value-status note")

    @property
    def key(self) -> tuple:
        return (self.target.scope.value, self.target.id, self.field)

    def to_dict(self) -> dict:
        return {"target": self.target.to_dict(), "field": self.field, "status": self.status.value,
                "decision_id": self.decision_id, "replaces": self.replaces.value if self.replaces else None,
                "provenance_ids": list(self.provenance_ids), "note": self.note}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ValueStatusRecord":
        check_keys(data, required={"target", "field", "status"},
                   optional={"decision_id", "replaces", "provenance_ids", "note"}, where="value status")
        return cls(target=Target.from_dict(data["target"]), field=data["field"], status=data["status"],
                   decision_id=data.get("decision_id"), replaces=data.get("replaces"),
                   provenance_ids=tuple(data.get("provenance_ids") or ()), note=data.get("note"))
