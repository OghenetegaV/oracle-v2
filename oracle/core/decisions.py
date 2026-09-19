"""Oracle — Core Engineering Decisions

Purpose:
    EngineeringDecision: a recommendation, an engineer's ruling, or an engineer's override, as one
    structured record (author, source, target, category, reason, status, supersession), optionally
    naming the recommendation it answers (`responds_to`) and a structured value change (`field`,
    `value`, `previous_value`).

Role in Oracle:
    The ENGINEERING DECISION layer of source fact -> interpretation -> decision -> design result. It
    keeps three things apart: an Oracle/AI recommendation (source ORACLE or AI_ASSISTANT, which can only
    be PROPOSED), an engineer decision, and an engineer override (an engineer decision that departs from
    the recommendation it responds to). Only an engineer decision can make a recommendation accepted,
    overridden or rejected, and the project enforces that. A chain of decisions superseding one another on
    the same (target, field) is that value's history, with `previous_value` recording each step.

Dependencies:
    oracle.core.common.

Consumers:
    oracle.core.project (stores, cross-checks and applies decisions; set_value); value_status,
    interpretations and issues refer to decisions by ID.

Status:
    Core (extended in schema 0.2.0).

Migration/Notes:
    0.2.0 added responds_to, field, value and previous_value (all optional, so 0.1.0 decisions load
    unchanged). `oracle_recommendation` remains as free text for older projects. The wizard's engineer
    notes, element notes and chat instructions become decisions in a later phase.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional

from .common import (
    Target, TargetScope, ValidationError, check_field_path, check_id, check_json_value, check_keys,
    check_optional_text, check_text, check_timestamp, parse_enum, utc_now_iso,
)


class DecisionStatus(str, Enum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    OVERRIDDEN = "overridden"
    SUPERSEDED = "superseded"


class DecisionSource(str, Enum):
    ENGINEER = "engineer"
    ORACLE = "oracle"          # a deterministic Oracle module
    AI_ASSISTANT = "ai_assistant"  # e.g. Claude -- can only ever propose
    IMPORTED = "imported"      # from a legacy project / external file


class DecisionCategory(str, Enum):
    LAYOUT = "layout"
    SECTION_SIZING = "section_sizing"
    LOADING = "loading"
    MATERIALS = "materials"
    DETAILING = "detailing"
    ANALYSIS = "analysis"
    ARCHITECTURAL_COORDINATION = "architectural_coordination"
    CODE_COMPLIANCE = "code_compliance"
    OTHER = "other"


@dataclass
class EngineeringDecision:
    """e.g. Target.element("C12"), LAYOUT, "Keep column C12 aligned with the architectural wall.".

    Three different things are kept apart:
      - an Oracle/AI RECOMMENDATION: source ORACLE or AI_ASSISTANT, which can only propose;
      - an ENGINEER DECISION: source ENGINEER, which may respond to a recommendation (`responds_to`);
      - an ENGINEER OVERRIDE: an engineer decision with `overrides_recommendation`, which departs from
        the recommendation it responds to.
    A value change is a decision that names `field` and the new `value`; `previous_value` is what it
    replaced, so a chain of superseded decisions on one (target, field) is that value's history.

    `oracle_recommendation` is free text kept for projects that pre-date `responds_to`."""

    id: str
    author: str
    source: DecisionSource
    target: Target
    category: DecisionCategory
    instruction: str
    reason: Optional[str] = None
    status: DecisionStatus = DecisionStatus.PROPOSED
    overrides_recommendation: bool = False
    oracle_recommendation: Optional[str] = None  # what Oracle had recommended, when overriding it
    superseded_by: Optional[str] = None
    created_at: str = ""
    responds_to: Optional[str] = None      # the recommendation (another decision) this engineer decision answers
    field: Optional[str] = None            # a structured value change: which field of the target...
    value: Any = None                      # ...to what (JSON form, e.g. {"shape": "rectangular", ...})
    previous_value: Any = None             # what the value was, filled in when the change is applied

    def __post_init__(self):
        check_id(self.id, "decision id")
        check_text(self.author, "decision author")
        self.source = parse_enum(DecisionSource, self.source, "decision source")
        self.category = parse_enum(DecisionCategory, self.category, "decision category")
        self.status = parse_enum(DecisionStatus, self.status, "decision status")
        if not isinstance(self.target, Target):
            raise ValidationError("Decision target must be a Target.")
        check_text(self.instruction, "decision instruction")
        check_optional_text(self.reason, "decision reason")
        check_optional_text(self.oracle_recommendation, "oracle_recommendation")
        if not isinstance(self.overrides_recommendation, bool):
            raise ValidationError("overrides_recommendation must be true or false.")
        if self.overrides_recommendation and self.source != DecisionSource.ENGINEER:
            raise ValidationError(
                f"Decision {self.id}: only an engineer decision can override an Oracle recommendation.")
        if self.oracle_recommendation and not self.overrides_recommendation:
            raise ValidationError(
                f"Decision {self.id}: oracle_recommendation is only meaningful when overrides_recommendation is true.")
        if (self.status == DecisionStatus.SUPERSEDED) != (self.superseded_by is not None):
            raise ValidationError(
                f"Decision {self.id}: superseded_by must be set exactly when the status is 'superseded'.")
        if self.superseded_by is not None:
            check_id(self.superseded_by, "superseded_by")
            if self.superseded_by == self.id:
                raise ValidationError(f"Decision {self.id} cannot supersede itself.")
        self.created_at = check_timestamp(self.created_at or utc_now_iso(), "decision created_at")
        if self.responds_to is not None:
            check_id(self.responds_to, "responds_to")
            if self.source != DecisionSource.ENGINEER:
                raise ValidationError(f"Decision {self.id}: only an engineer decision can respond to a recommendation.")
            if self.responds_to == self.id:
                raise ValidationError(f"Decision {self.id} cannot respond to itself.")
        if self.field is not None:
            check_field_path(self.field, "decision field")
            if self.target.scope == TargetScope.PROJECT:
                raise ValidationError(f"Decision {self.id}: a field change needs a level, element, node or grid target.")
            if self.value is None:
                raise ValidationError(f"Decision {self.id}: a field change needs the new value.")
        elif self.value is not None or self.previous_value is not None:
            raise ValidationError(f"Decision {self.id}: value and previous_value only make sense with a field.")
        check_json_value(self.value, "decision value")
        check_json_value(self.previous_value, "decision previous_value")

    def supersede(self, by_decision_id: str) -> None:
        check_id(by_decision_id, "superseded_by")
        if by_decision_id == self.id:
            raise ValidationError(f"Decision {self.id} cannot supersede itself.")
        self.superseded_by = by_decision_id
        self.status = DecisionStatus.SUPERSEDED

    def to_dict(self) -> dict:
        return {
            "id": self.id, "created_at": self.created_at, "author": self.author, "source": self.source.value,
            "target": self.target.to_dict(), "category": self.category.value, "instruction": self.instruction,
            "reason": self.reason, "status": self.status.value,
            "overrides_recommendation": self.overrides_recommendation,
            "oracle_recommendation": self.oracle_recommendation, "superseded_by": self.superseded_by,
            "responds_to": self.responds_to, "field": self.field, "value": self.value,
            "previous_value": self.previous_value,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EngineeringDecision":
        check_keys(data, required={"id", "created_at", "author", "source", "target", "category", "instruction",
                                   "status"},
                   optional={"reason", "overrides_recommendation", "oracle_recommendation", "superseded_by",
                             "responds_to", "field", "value", "previous_value"},
                   where="decision")
        return cls(
            id=data["id"], author=data["author"], source=data["source"], target=Target.from_dict(data["target"]),
            category=data["category"], instruction=data["instruction"], reason=data.get("reason"),
            status=data["status"], overrides_recommendation=data.get("overrides_recommendation", False),
            oracle_recommendation=data.get("oracle_recommendation"), superseded_by=data.get("superseded_by"),
            created_at=data["created_at"], responds_to=data.get("responds_to"), field=data.get("field"),
            value=data.get("value"), previous_value=data.get("previous_value"),
        )
