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
    Core (schema 0.2.0; effects added in 0.4.0).

Migration/Notes:
    New in schema 0.2.0. Schema 0.4.0 adds structured `effects` (oracle.core.effects) and the `applied`
    record, so accepting an alternative changes the model through oracle.core.resolution instead of only marking a
    status. Creating a structural object (a slab, an opening) from an accepted reading is still the interpreter's or
    adapter's job, not this module's.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional

from .effects import Effect
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
    effects: tuple = ()                   # what accepting it MEANS, as validated data (oracle.core.effects); empty: nothing
    applied: tuple = ()                   # filled in on acceptance: what each effect actually did (ids, previous values)
    origin: str = "oracle"                # "oracle": proposed by an interpreter; "engineer": the engineer's own answer (oracle.core.clarifications)

    def __post_init__(self):
        check_id(self.id, "interpretation id")
        self.effects = tuple(e if isinstance(e, Effect) else Effect.from_dict(e) for e in self.effects)
        self.applied = tuple(dict(a) for a in self.applied)
        if self.applied and self.status != InterpretationStatus.ACCEPTED:
            raise ValidationError(f"Interpretation {self.id}: only an accepted interpretation has applied effects.")
        if self.applied and len(self.applied) != len(self.effects):
            raise ValidationError(f"Interpretation {self.id}: {len(self.applied)} applied record(s) for "
                                  f"{len(self.effects)} effect(s).")
        if self.origin not in ("oracle", "engineer"):
            raise ValidationError(f"Interpretation {self.id}: origin must be 'oracle' or 'engineer', got {self.origin!r}.")
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
        out = {"id": self.id, "meaning": self.meaning, "confidence": self.confidence, "rationale": self.rationale,
               "evidence": list(self.evidence), "status": self.status.value, "decision_id": self.decision_id}
        if self.effects:
            out["effects"] = [e.to_dict() for e in self.effects]
        if self.applied:
            out["applied"] = [dict(a) for a in self.applied]
        if self.origin != "oracle":
            out["origin"] = self.origin
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Interpretation":
        check_keys(data, required={"id", "meaning", "confidence", "status"},
                   optional={"rationale", "evidence", "decision_id", "effects", "applied", "origin"}, where="interpretation")
        return cls(id=data["id"], meaning=data["meaning"], confidence=data["confidence"],
                   rationale=data.get("rationale"), evidence=tuple(data.get("evidence") or ()),
                   status=data["status"], decision_id=data.get("decision_id"),
                   effects=tuple(Effect.from_dict(e) for e in data.get("effects") or ()),
                   applied=tuple(data.get("applied") or ()), origin=data.get("origin", "oracle"))


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

    def accept_engineer_answer(self, answer: Interpretation, decision_id: str) -> None:
        """The engineer answers in their own words: `answer` (origin "engineer") joins the set as the accepted reading and the
        still-proposed Oracle readings are rejected under the same decision. Nothing changes if the set is already settled."""
        if self.status != SetStatus.OPEN:
            raise ValidationError(f"Interpretation set {self.id} is already {self.status.value}.")
        if answer.origin != "engineer" or answer.decision_id != decision_id:
            raise ValidationError("Only an engineer's own answer, made under this decision, can be added this way.")
        if answer.meaning.strip().lower() in {a.meaning.strip().lower() for a in self.alternatives}:
            raise ValidationError(f"Interpretation set {self.id} already lists that meaning.")
        if any(a.id == answer.id for a in self.alternatives):
            raise ValidationError(f"Interpretation set {self.id} already has an alternative {answer.id!r}.")
        for a in self.alternatives:
            if a.status == InterpretationStatus.PROPOSED:
                a.status, a.decision_id = InterpretationStatus.REJECTED, decision_id
        self.alternatives.append(answer)

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
