"""Oracle — Core Alternative Interpretations

Purpose:
    InterpretationSet and Interpretation: a way to hold several competing readings of the same source
    evidence without choosing one silently. A set poses a question ("what is this rectangular region?"),
    cites the shared evidence (provenance record IDs), optionally names the existing object it concerns,
    and lists alternatives, each with a proposed meaning, a confidence and a rationale. The engineer
    accepts one alternative or rejects them; either needs a recorded engineer decision.

Role in Oracle:
    The INTERPRETATION layer of source fact -> interpretation -> engineering decision -> design
    result. An interpreter (Oracle or Claude) may propose, with confidence, that a region is "slab
    panel" 0.82, "void" 0.14 or "balcony" 0.04; nothing is built from it until the engineer decides.
    Accepting one alternative rejects its siblings under the same decision, so the choice and what was
    passed over both stay on record. 'meaning' is a free-form code chosen by the interpreter: the core
    does not hard-code architectural classes.

    A set's status is derived from its alternatives, never stored: OPEN while any alternative is still
    PROPOSED, RESOLVED once one is ACCEPTED, NONE_APPLY when every alternative was rejected. An OPEN
    set means ambiguity is unresolved and keeps the project from being ready for final output.

Dependencies:
    oracle.core.common (Target, validators).

Consumers:
    oracle.core.project (stores, cross-checks, serialises, and applies accept/reject);
    oracle.core.readiness; oracle.core.issues (an issue may point at an interpretation); interpreters.

Status:
    Core (schema 0.2.0).

Migration/Notes:
    New in schema 0.2.0. Carrying out an accepted interpretation (creating the slab, the opening) is the
    interpreter/adapter's job, not this module's.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional

from .common import (
    Target, ValidationError, check_confidence, check_id, check_keys, check_optional_text, check_text,
    check_unique_ids, parse_enum,
)


class InterpretationStatus(str, Enum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class SetStatus(str, Enum):
    OPEN = "open"
    RESOLVED = "resolved"
    NONE_APPLY = "none_apply"


@dataclass
class Interpretation:
    id: str
    meaning: str                          # the proposed reading, e.g. "slab_panel"; free-form, interpreter's choice
    confidence: float
    rationale: Optional[str] = None       # why the interpreter proposes it
    evidence: tuple = ()                  # provenance record IDs specific to this reading (shared ones are on the set)
    status: InterpretationStatus = InterpretationStatus.PROPOSED
    decision_id: Optional[str] = None     # the engineer decision that accepted or rejected it

    def __post_init__(self):
        check_id(self.id, "interpretation id")
        check_text(self.meaning, "interpretation meaning")
        check_confidence(self.confidence, "interpretation confidence")
        check_optional_text(self.rationale, "interpretation rationale")
        self.evidence = tuple(check_id(e, "interpretation evidence id") for e in self.evidence)
        self.status = parse_enum(InterpretationStatus, self.status, "interpretation status")
        if self.status == InterpretationStatus.PROPOSED:
            if self.decision_id is not None:
                raise ValidationError(f"Interpretation {self.id}: a proposed interpretation has no decision yet.")
        elif self.decision_id is None:
            raise ValidationError(f"Interpretation {self.id}: an {self.status.value} interpretation needs the "
                                  "engineer decision that made it (decision_id).")
        else:
            check_id(self.decision_id, "interpretation decision_id")

    def to_dict(self) -> dict:
        return {"id": self.id, "meaning": self.meaning, "confidence": self.confidence, "rationale": self.rationale,
                "evidence": list(self.evidence), "status": self.status.value, "decision_id": self.decision_id}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Interpretation":
        check_keys(data, required={"id", "meaning", "confidence", "status"},
                   optional={"rationale", "evidence", "decision_id"}, where="interpretation")
        return cls(id=data["id"], meaning=data["meaning"], confidence=data["confidence"],
                   rationale=data.get("rationale"), evidence=tuple(data.get("evidence") or ()),
                   status=data["status"], decision_id=data.get("decision_id"))


@dataclass
class InterpretationSet:
    id: str
    question: str
    alternatives: list
    evidence: tuple = ()                  # provenance record IDs all alternatives are readings of
    subject: Optional[Target] = None      # an existing object this is about; None if nothing exists yet

    def __post_init__(self):
        check_id(self.id, "interpretation set id")
        check_text(self.question, "interpretation question")
        self.alternatives = list(self.alternatives)
        if not self.alternatives:
            raise ValidationError(f"Interpretation set {self.id} needs at least one alternative.")
        if not all(isinstance(a, Interpretation) for a in self.alternatives):
            raise ValidationError(f"Interpretation set {self.id}: alternatives must be Interpretation objects.")
        check_unique_ids((a.id for a in self.alternatives), f"interpretation id in set {self.id}:")
        meanings = [a.meaning.strip().lower() for a in self.alternatives]
        if len(meanings) != len(set(meanings)):
            raise ValidationError(f"Interpretation set {self.id} lists the same meaning twice.")
        self.evidence = tuple(check_id(e, "interpretation set evidence id") for e in self.evidence)
        if self.subject is not None and not isinstance(self.subject, Target):
            raise ValidationError("Interpretation set subject must be a Target.")
        if sum(a.status == InterpretationStatus.ACCEPTED for a in self.alternatives) > 1:
            raise ValidationError(f"Interpretation set {self.id} has more than one accepted alternative.")

    @property
    def accepted(self) -> Optional[Interpretation]:
        return next((a for a in self.alternatives if a.status == InterpretationStatus.ACCEPTED), None)

    @property
    def status(self) -> SetStatus:
        if self.accepted is not None:
            return SetStatus.RESOLVED
        if all(a.status == InterpretationStatus.REJECTED for a in self.alternatives):
            return SetStatus.NONE_APPLY
        return SetStatus.OPEN

    def get(self, interpretation_id: str) -> Interpretation:
        for a in self.alternatives:
            if a.id == interpretation_id:
                return a
        raise ValidationError(f"Interpretation set {self.id} has no alternative {interpretation_id!r}.")

    def ranked(self) -> list:
        """Alternatives, most confident first (ties keep their listed order)."""
        return sorted(self.alternatives, key=lambda a: -a.confidence)

    def accept(self, interpretation_id: str, decision_id: str) -> None:
        """Accept one alternative and reject the still-proposed rest, all under the same decision.
        Nothing changes if the set is already settled or the ID is unknown."""
        chosen = self.get(interpretation_id)
        if self.status != SetStatus.OPEN:
            raise ValidationError(f"Interpretation set {self.id} is already {self.status.value}.")
        if chosen.status != InterpretationStatus.PROPOSED:
            raise ValidationError(f"Interpretation {chosen.id} was already {chosen.status.value}.")
        check_id(decision_id, "decision id")
        for a in self.alternatives:
            if a.status == InterpretationStatus.PROPOSED:
                a.status = InterpretationStatus.ACCEPTED if a is chosen else InterpretationStatus.REJECTED
                a.decision_id = decision_id

    def reject(self, interpretation_id: str, decision_id: str) -> None:
        a = self.get(interpretation_id)
        if a.status != InterpretationStatus.PROPOSED:
            raise ValidationError(f"Interpretation {a.id} was already {a.status.value}.")
        check_id(decision_id, "decision id")
        a.status, a.decision_id = InterpretationStatus.REJECTED, decision_id

    def to_dict(self) -> dict:
        return {"id": self.id, "question": self.question, "subject": self.subject.to_dict() if self.subject else None,
                "evidence": list(self.evidence), "alternatives": [a.to_dict() for a in self.alternatives]}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "InterpretationSet":
        check_keys(data, required={"id", "question", "alternatives"}, optional={"subject", "evidence"},
                   where="interpretation set")
        alternatives = data["alternatives"]
        if not isinstance(alternatives, list):
            raise ValidationError("Interpretation set alternatives must be a list.")
        return cls(id=data["id"], question=data["question"],
                   alternatives=[Interpretation.from_dict(a) for a in alternatives],
                   evidence=tuple(data.get("evidence") or ()),
                   subject=Target.from_dict(data["subject"]) if data.get("subject") else None)
