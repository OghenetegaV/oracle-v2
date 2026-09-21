"""Oracle — Engineer Clarifications (free-form engineer input)

Purpose:
    EngineerClarification: what an engineer says in their own words about something Oracle read from a drawing ("Treat this as an
    existing reinforced concrete balcony", "These columns are existing and retained"). It is the safe home for input that does not
    fit any reading Oracle proposed: the statement is kept VERBATIM, tied to the thing it is about (a target), to the engineer
    decision that recorded it (identity, time, status) and, where the target is an architectural object, to a provenance record so
    it shows in that object's evidence. It is never turned into structural geometry or a model change by this module.

Role in Oracle:
    Makes "Ask Engineer" possible without weakening any rule. Two forms:
      - record(): guidance about a target (no question involved); it is preserved and flagged for the next stage.
      - answer_question(): the engineer answers an OPEN interpretation set in their own words instead of choosing one of Oracle's
        alternatives. The answer becomes a further alternative with origin "engineer", accepted under the engineer decision (the
        Oracle readings are rejected under the same decision, exactly as when one of them is chosen), so the question is settled by
        the engineer's authority and the choice and what was passed over both stay on record. Issues attached to the question are
        ACCEPTED (not fixed), quoting the statement. No effect is applied: whatever the words might mean for the model must still be
        done explicitly, by a structured decision.
    Disposition "guidance" means: preserved as intent, not yet applied to the model. When the engineer supplies a VALUE that maps to a
    structured effect (a storey height, say), answer_question(..., effects=...) applies it through the ordinary resolution machinery and
    the disposition is "applied"; free prose is never converted into effects here.

Dependencies:
    oracle.core.common, decisions, interpretations, provenance. It works on an OracleProject instance and does not import it.

Consumers:
    oracle.core.project (record_clarification, answer_with_engineer_input, validate, serialisation), oracle.core.approved
    (engineer guidance for the next stage), oracle.application.

Status:
    Core (added in schema 0.5.0).

Migration/Notes:
    New in schema 0.5.0 as the top-level `clarifications` registry (empty in every migrated project). Ids are CLR-001, CLR-002...
    Disposition "applied" is used only when the engineer's answer carried validated structured effects.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from .common import (
    Target, TargetScope, ValidationError, check_id, check_keys, check_optional_text, check_text, check_timestamp, utc_now_iso,
)
from .decisions import DecisionSource, DecisionStatus, EngineeringDecision
from . import resolution as _resolution
from .interpretations import Interpretation, InterpretationStatus, SetStatus
from .provenance import ProvenanceRecord, SourceReference

DISPOSITIONS = ("guidance", "applied")
_BACKING = (DecisionStatus.ACCEPTED, DecisionStatus.SUPERSEDED)
MAX_MEANING = 160


@dataclass
class EngineerClarification:
    id: str
    decision_id: str
    target: Target
    statement: str
    author: str
    created_at: str = ""
    notes: Optional[str] = None
    interpretation_set_id: Optional[str] = None
    interpretation_id: Optional[str] = None
    provenance_id: Optional[str] = None
    disposition: str = "guidance"

    def __post_init__(self):
        check_id(self.id, "clarification id")
        check_id(self.decision_id, "clarification decision_id")
        if not isinstance(self.target, Target):
            raise ValidationError("A clarification target must be a Target.")
        check_text(self.statement, "clarification statement")
        check_text(self.author, "clarification author")
        check_optional_text(self.notes, "clarification notes")
        self.created_at = check_timestamp(self.created_at or utc_now_iso(), "clarification created_at")
        if (self.interpretation_set_id is None) != (self.interpretation_id is None):
            raise ValidationError(f"Clarification {self.id}: a question and the alternative that holds the answer go together.")
        for value, what in ((self.interpretation_set_id, "interpretation_set_id"), (self.interpretation_id, "interpretation_id"),
                            (self.provenance_id, "provenance_id")):
            if value is not None:
                check_id(value, what)
        if self.disposition not in DISPOSITIONS:
            raise ValidationError(f"Clarification {self.id}: disposition must be one of {DISPOSITIONS}, got {self.disposition!r}.")

    def to_dict(self) -> dict:
        return {"id": self.id, "decision_id": self.decision_id, "target": self.target.to_dict(), "statement": self.statement,
                "notes": self.notes, "author": self.author, "created_at": self.created_at,
                "interpretation_set_id": self.interpretation_set_id, "interpretation_id": self.interpretation_id,
                "provenance_id": self.provenance_id, "disposition": self.disposition}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EngineerClarification":
        check_keys(data, required={"id", "decision_id", "target", "statement", "author", "created_at"},
                   optional={"notes", "interpretation_set_id", "interpretation_id", "provenance_id", "disposition"},
                   where="clarification")
        return cls(id=data["id"], decision_id=data["decision_id"], target=Target.from_dict(data["target"]),
                   statement=data["statement"], author=data["author"], created_at=data["created_at"], notes=data.get("notes"),
                   interpretation_set_id=data.get("interpretation_set_id"), interpretation_id=data.get("interpretation_id"),
                   provenance_id=data.get("provenance_id"), disposition=data.get("disposition", "guidance"))


def _next_id(project) -> str:
    highest = max((int(c.id[4:]) for c in project.clarifications if c.id.startswith("CLR-") and c.id[4:].isdigit()), default=0)
    return f"CLR-{highest + 1:03d}"


def _engineer_decision(project, decision: EngineeringDecision) -> None:
    project._engineer_review_decision(decision)          # accepted, from the engineer, not a field change, not a duplicate id


def _meaning_of(statement: str, taken: set) -> str:
    first = statement.strip().splitlines()[0].strip()
    meaning = first if len(first) <= MAX_MEANING else first[:MAX_MEANING - 1].rstrip() + "…"
    if meaning.lower() in taken:
        meaning += " (engineer's wording)"
    return meaning


def record(project, decision: EngineeringDecision, target: Target, statement: str, notes: Optional[str] = None, *,
           interpretation_set_id: Optional[str] = None, interpretation_id: Optional[str] = None) -> EngineerClarification:
    """Keep the engineer's words about `target`, verbatim, under `decision`. Nothing in the model changes."""
    check_text(statement, "the engineer's statement")
    _engineer_decision(project, decision)
    project._check_target(project.building, target, f"clarification for decision {decision.id}")
    project._store_decision(decision)
    provenance_id = None
    if target.scope != TargetScope.PROJECT:
        rec = project.add_provenance(ProvenanceRecord(
            project.next_provenance_id(), target, SourceReference(context=f"engineer clarification under decision {decision.id}"),
            "engineer_clarification", "engineer", None, None, utc_now_iso(), statement))
        provenance_id = rec.id
    clarification = EngineerClarification(_next_id(project), decision.id, target, statement.strip(), decision.author, decision.created_at,
                                          notes.strip() if notes and notes.strip() else None, interpretation_set_id, interpretation_id,
                                          provenance_id)
    project._clarifications[clarification.id] = clarification
    project.touch()
    return clarification


def answer_question(project, set_id: str, decision: EngineeringDecision, statement: str, notes: Optional[str] = None, *,
                    target: Optional[Target] = None, effects: tuple = ()) -> EngineerClarification:
    """The engineer answers an open question in their own words, optionally with structured effects for a VALUE they supply. All or
    nothing: with effects, the whole answer is first rehearsed on a copy, so a refusal anywhere changes nothing."""
    if effects:
        rehearsal = type(project).from_dict(project.to_dict())
        try:
            _answer_now(rehearsal, set_id, decision, statement, notes, target, effects)
        except ValidationError as exc:
            raise ValidationError(f"The answer to {set_id} was refused and nothing was changed: {exc}") from None
    return _answer_now(project, set_id, decision, statement, notes, target, effects)


def _answer_now(project, set_id, decision, statement, notes, target, effects) -> EngineerClarification:
    s = project.get_interpretation_set(set_id)
    if s.status != SetStatus.OPEN:
        raise ValidationError(f"Interpretation set {set_id} is already {s.status.value}; it cannot be answered again.")
    check_text(statement, "the engineer's statement")
    _engineer_decision(project, decision)
    target = target or s.subject or Target.project()
    project._check_target(project.building, target, f"clarification answering {set_id}")
    alt_id = f"{set_id}-ENG"
    if project._find_interpretation(alt_id) is not None:
        raise ValidationError(f"{set_id} already holds an engineer answer.")
    taken = {a.meaning.strip().lower() for a in s.alternatives}
    clarification_id = _next_id(project)
    rationale = f"The engineer's own answer, kept in full as {clarification_id}."
    if effects:
        alternative = Interpretation(alt_id, _meaning_of(statement, taken), 1.0, rationale, effects=tuple(effects), origin="engineer")
    else:
        alternative = Interpretation(alt_id, _meaning_of(statement, taken), 1.0, rationale, status=InterpretationStatus.ACCEPTED,
                                     decision_id=decision.id, origin="engineer")
    clarification = record(project, decision, target, statement, notes, interpretation_set_id=set_id, interpretation_id=alt_id)
    assert clarification.id == clarification_id
    if effects:
        clarification.disposition = "applied"
        s.alternatives.append(alternative)                    # proposed for an instant, then accepted through the resolution machinery
        _resolution.check_effect_targets(project, s, alternative)
        _resolution._accept_now(project, set_id, alt_id, decision.id, True)       # the whole answer was rehearsed on a copy already
    else:
        s.accept_engineer_answer(alternative, decision.id)
    for issue in list(project.issues):
        if issue.interpretation_set_id == set_id and issue.is_open:        # (an applied answer has already resolved them)
            project.accept_issue(issue.id, f"Engineer clarification {clarification.id}: {clarification.statement}", decision.id)
    project.touch()
    return clarification


def check_clarifications(project) -> None:
    """Every clarification must rest on a recorded engineer decision and on things that exist; an engineer-authored alternative
    must be backed by exactly one clarification."""
    seen_alternatives = set()
    for c in project.clarifications:
        what = f"Clarification {c.id}"
        d = project._decisions.get(c.decision_id)
        if d is None or d.source != DecisionSource.ENGINEER or d.status not in _BACKING:
            raise ValidationError(f"{what}: {c.decision_id!r} is not a recorded engineer decision.")
        project._check_target(project.building, c.target, what)
        if c.provenance_id is not None:
            record_ = project._provenance.get(c.provenance_id)
            if record_ is None or record_.target != c.target:
                raise ValidationError(f"{what}: provenance {c.provenance_id!r} does not describe {c.target.id}.")
        if c.interpretation_set_id is not None:
            s = project.get_interpretation_set(c.interpretation_set_id)
            alt = s.get(c.interpretation_id)
            if alt.origin != "engineer" or alt.decision_id != c.decision_id or alt.status != InterpretationStatus.ACCEPTED:
                raise ValidationError(f"{what}: {c.interpretation_id} is not the engineer's answer recorded by this clarification.")
            seen_alternatives.add(alt.id)
    for s in project.interpretation_sets:
        for a in s.alternatives:
            if a.origin == "engineer" and a.id not in seen_alternatives:
                raise ValidationError(f"Interpretation {a.id} claims to be the engineer's own answer but no clarification records it.")
