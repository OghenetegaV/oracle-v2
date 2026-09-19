"""Oracle — Core Engineering Decisions

Purpose:
    EngineeringDecision: an engineer's instruction or ruling as a structured record (author,
    source, target, category, reason, status, override flag, supersession).

Role in Oracle:
    Replaces free-text notes passed to Claude. Only an engineer-sourced decision may override an
    Oracle recommendation, so Claude and Oracle can propose but the engineer decides.

Dependencies:
    oracle.core.common.

Consumers:
    oracle.core.project (stores and cross-checks decisions).

Status:
    Core.

Migration:
    Remains. The wizard's engineer notes, element notes and chat instructions become decisions in a later phase.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional

from .common import (
    Target, ValidationError, check_id, check_keys, check_optional_text, check_text, check_timestamp,
    parse_enum, utc_now_iso,
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

    `overrides_recommendation` records that this decision deliberately departs from something
    Oracle proposed; only the engineer can do that (ORACLE / AI_ASSISTANT sources only propose)."""

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

    def supersede(self, by_decision_id: str) -> None:
        self.superseded_by = check_id(by_decision_id, "superseded_by")
        if self.superseded_by == self.id:
            raise ValidationError(f"Decision {self.id} cannot supersede itself.")
        self.status = DecisionStatus.SUPERSEDED

    def to_dict(self) -> dict:
        return {
            "id": self.id, "created_at": self.created_at, "author": self.author, "source": self.source.value,
            "target": self.target.to_dict(), "category": self.category.value, "instruction": self.instruction,
            "reason": self.reason, "status": self.status.value,
            "overrides_recommendation": self.overrides_recommendation,
            "oracle_recommendation": self.oracle_recommendation, "superseded_by": self.superseded_by,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EngineeringDecision":
        check_keys(data, required={"id", "created_at", "author", "source", "target", "category", "instruction",
                                   "status"},
                   optional={"reason", "overrides_recommendation", "oracle_recommendation", "superseded_by"},
                   where="decision")
        return cls(
            id=data["id"], author=data["author"], source=data["source"], target=Target.from_dict(data["target"]),
            category=data["category"], instruction=data["instruction"], reason=data.get("reason"),
            status=data["status"], overrides_recommendation=data.get("overrides_recommendation", False),
            oracle_recommendation=data.get("oracle_recommendation"), superseded_by=data.get("superseded_by"),
            created_at=data["created_at"],
        )
