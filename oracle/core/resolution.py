"""Oracle — Resolving an Interpretation Set

Purpose:
    Makes accepting one alternative of an InterpretationSet MEAN something. accept() records the choice, rejects the
    siblings, then carries out the chosen alternative's structured effects (oracle.core.effects) through the same
    machinery an engineer would use by hand: a field change becomes an engineer decision applied with set_value(), a
    height becomes engineer-defined height evidence, an alignment creates the frame and sets the view's field, a merge
    or split calls the review methods. Issues linked to the set are resolved under the same decision when the
    resolution addresses them. It first rehearses everything on a copy of the project, so a refusal anywhere means
    nothing at all changed. check_resolutions() re-verifies every resolved set against the model on load, so a payload,
    a decision or a value that was edited by hand is caught.

Role in Oracle:
    Closes the gap between "an interpretation was resolved" and "the model and the issue list reflect it". It adds no
    new authority: effects are applied only under an accepted ENGINEER decision, every field change keeps its history
    (previous value, supersession, value status), and nothing outside the listed effects is touched.

Dependencies:
    oracle.core.effects, oracle.core.architecture, oracle.core.decisions, oracle.core.provenance,
    oracle.core.value_status. It works on an OracleProject instance and does not import it.

Consumers:
    oracle.core.project (accept_interpretation, validate), tests.

Status:
    Core (added in schema 0.4.0).

Migration/Notes:
    Sets written before 0.4.0 have no effects: accepting them records the choice and changes nothing else, exactly as
    before, and they never resolve linked issues (they have none).
"""

from __future__ import annotations

from .architecture import DrawingView, HeightEvidence
from .common import Target, TargetScope, ValidationError, utc_now_iso
from .decisions import DecisionSource, DecisionStatus, EngineeringDecision
from .effects import Effect, EffectKind
from .interpretations import SetStatus
from .provenance import ProvenanceRecord, SourceReference
from .value_status import ValueStatus, ValueStatusRecord

_BACKING = (DecisionStatus.ACCEPTED, DecisionStatus.SUPERSEDED)


def check_effect_targets(project, interpretation_set, alternative) -> None:
    """A proposed alternative may only promise to change things that exist."""
    what = f"interpretation {alternative.id}"
    for eff in alternative.effects:
        if eff.kind == EffectKind.SET_VALUE:
            project._check_target(project.building, eff.target, what)
        elif eff.kind == EffectKind.ALIGN_VIEW:
            project._arch_object(eff.params["view_id"])
        elif eff.kind == EffectKind.MERGE_VIEWS:
            for v in eff.params["view_ids"]:
                project._arch_object(v)
        elif eff.kind == EffectKind.SPLIT_VIEW:
            project._arch_object(eff.params["view_id"])


def accept(project, set_id: str, interpretation_id: str, decision_id: str, *, apply_effects: bool = True) -> None:
    s = project.get_interpretation_set(set_id)
    project._require_engineer_decision(decision_id, f"accepting interpretation {interpretation_id}")
    alt = s.get(interpretation_id)
    if alt.effects and apply_effects:
        rehearsal = type(project).from_dict(project.to_dict())      # nothing is real until this succeeds
        try:
            _accept_now(rehearsal, set_id, interpretation_id, decision_id, apply_effects)
        except ValidationError as exc:
            raise ValidationError(f"Accepting {interpretation_id} of {set_id} was refused and nothing was changed: {exc}") from None
    _accept_now(project, set_id, interpretation_id, decision_id, apply_effects)


def _accept_now(project, set_id: str, interpretation_id: str, decision_id: str, apply_effects: bool) -> None:
    s = project.get_interpretation_set(set_id)
    decision = project.get_decision(decision_id)
    alt = s.get(interpretation_id)
    s.accept(interpretation_id, decision_id)
    applied = []
    for n, effect in enumerate(alt.effects, 1):
        if apply_effects:
            applied.append(_apply(project, s, alt, decision, effect, n))
        else:
            applied.append({"kind": "skipped", "reason": "the engineer supplied the values directly"})
    alt.applied = tuple(applied)
    if alt.effects:
        for issue in list(project.issues):
            if issue.interpretation_set_id == s.id and issue.is_open:
                project.resolve_issue(
                    issue.id, f"Resolved by the engineer's choice of {alt.meaning!r} ({alt.id}) for {s.id}.", decision_id)
    project.touch()


def _derived_decision(decision, s, alt, n: int, target: Target, field: str, value) -> EngineeringDecision:
    return EngineeringDecision(
        f"{decision.id}.E{n}", decision.author, DecisionSource.ENGINEER, target, decision.category,
        f"Applied by accepting {alt.id} ({alt.meaning}) of {s.id}: {field} = {value!r}.", reason=decision.reason,
        status=DecisionStatus.ACCEPTED, field=field, value=value)


def _arch_for_set(project, s):
    if s.subject is not None and s.subject.scope == TargetScope.ARCHITECTURAL:
        found = project._arch_holding(s.subject.id)
        if found is not None:
            return found
    for pid in s.evidence:
        record = project._provenance.get(pid)
        if record is not None and record.target.scope == TargetScope.ARCHITECTURAL:
            found = project._arch_holding(record.target.id)
            if found is not None:
                return found
    return project.architecture


def _apply(project, s, alt, decision, effect: Effect, n: int) -> dict:
    kind, p = effect.kind, effect.params
    if kind == EffectKind.SET_VALUE:
        target, field, value = effect.target, p["field"], p["value"]
        derived = _derived_decision(decision, s, alt, n, target, field, value)
        project.set_value(target, field, value, derived)
        return {"kind": kind.value, "decisions": [derived.id], "previous": derived.previous_value}
    if kind == EffectKind.ALIGN_VIEW:
        frame_id = project.next_frame_id()
        derived = _derived_decision(decision, s, alt, n, Target.architectural(p["view_id"]), "alignment_frame_id", frame_id)
        frame = project.align_view(p["view_id"], p["translation"], derived)
        return {"kind": kind.value, "decisions": [derived.id], "created": [frame.id]}
    if kind == EffectKind.ACCEPT_HEIGHT:
        arch = _arch_for_set(project, s)
        if arch is None:
            raise ValidationError("There is no architectural interpretation to hold an accepted height.")
        highest = max((int(h.id[4:]) for a in project.architectures for h in a.heights), default=0)
        hid = f"HGT-{highest + 1:03d}"
        arch.add(HeightEvidence(hid, p["from_level"], p["to_level"], float(p["height_mm"]), "engineer_resolution",
                                ValueStatus.ENGINEER_DEFINED, 1.0, None))
        record = project.add_provenance(ProvenanceRecord(
            project.next_provenance_id(), Target.architectural(hid),
            SourceReference(context=f"accepted interpretation {alt.id} of {s.id} under decision {decision.id}"),
            "engineer_resolution", "engineer", "height_mm", 1.0, utc_now_iso(),
            f"The engineer accepted {p['height_mm']:g} mm from {p['from_level']} to {p['to_level']}."))
        project.set_value_status(ValueStatusRecord(Target.architectural(hid), "height_mm", ValueStatus.ENGINEER_DEFINED,
                                                   provenance_ids=(record.id,), decision_id=decision.id))
        return {"kind": kind.value, "created": [hid]}
    if kind == EffectKind.MERGE_VIEWS:
        merged = project._merge_views(p["view_ids"], p["new_id"], decision, store=False)
        return {"kind": kind.value, "created": [merged.id]}
    if kind == EffectKind.SPLIT_VIEW:
        made = project._split_view(p["view_id"], p["parts"], decision, store=False)
        return {"kind": kind.value, "created": [v.id for v in made]}
    return {"kind": EffectKind.ACKNOWLEDGE.value}


# ---------------------------------------------------------------- load-time verification

def check_resolutions(project) -> None:
    """Every resolved set must agree with its own payload and with the model, and must have addressed its issues."""
    for s in project.interpretation_sets:
        if s.status != SetStatus.RESOLVED:
            continue
        alt = s.accepted
        if not alt.effects:
            continue
        what = f"resolved interpretation set {s.id}"
        if len(alt.applied) != len(alt.effects):
            raise ValidationError(f"{what.capitalize()}: {alt.id} has {len(alt.effects)} effect(s) but "
                                  f"{len(alt.applied)} applied record(s).")
        for effect, record in zip(alt.effects, alt.applied):
            if record.get("kind") == "skipped":
                continue
            if record.get("kind") != effect.kind.value:
                raise ValidationError(f"{what.capitalize()}: the applied record {record.get('kind')!r} does not match "
                                      f"the effect {effect.kind.value!r}.")
            _check_applied(project, what, effect, record, alt)
        for issue in project.issues:
            if issue.interpretation_set_id == s.id and issue.is_open:
                raise ValidationError(f"Issue {issue.id} is still open although {s.id} was resolved with an alternative "
                                      "that addresses it.")


def _decision_of(project, what: str, decision_id: str) -> EngineeringDecision:
    d = project._decisions.get(decision_id)
    if d is None or d.source != DecisionSource.ENGINEER or d.status not in _BACKING:
        raise ValidationError(f"{what.capitalize()}: {decision_id!r} is not a recorded engineer decision.")
    return d


def _check_applied(project, what: str, effect: Effect, record: dict, alt) -> None:
    kind, p = effect.kind, effect.params
    if kind == EffectKind.SET_VALUE:
        d = _decision_of(project, what, (record.get("decisions") or [None])[0])
        if (d.target, d.field, d.value) != (effect.target, p["field"], p["value"]):
            raise ValidationError(f"{what.capitalize()}: the payload says {effect.target.id}.{p['field']} = {p['value']!r} "
                                  f"but the decision it was applied by says {d.target.id}.{d.field} = {d.value!r}.")
    elif kind == EffectKind.ALIGN_VIEW:
        d = _decision_of(project, what, (record.get("decisions") or [None])[0])
        frame_id = (record.get("created") or [None])[0]
        arch, _view = project._arch_object(p["view_id"])
        frame = next((f for f in arch.frames if f.id == frame_id), None)
        if (frame is None or d.field != "alignment_frame_id" or d.value != frame_id
                or tuple(frame.translation) != (-float(p["translation"][0]), -float(p["translation"][1]))):
            raise ValidationError(f"{what.capitalize()}: the alignment of {p['view_id']} does not match its payload.")
    elif kind == EffectKind.ACCEPT_HEIGHT:
        hid = (record.get("created") or [None])[0]
        found = next((h for a in project.architectures for h in a.heights if h.id == hid), None)
        if (found is None or (found.from_level, found.to_level) != (p["from_level"], p["to_level"])
                or abs(found.height_mm - float(p["height_mm"])) > 1e-6 or found.basis != ValueStatus.ENGINEER_DEFINED):
            raise ValidationError(f"{what.capitalize()}: the accepted height {p['from_level']} -> {p['to_level']} "
                                  "does not match its payload.")
    elif kind == EffectKind.MERGE_VIEWS:
        arch, merged = project._arch_object(p["new_id"])
        if not isinstance(merged, DrawingView) or any(arch.get(v).superseded_by != (p["new_id"],) for v in p["view_ids"]):
            raise ValidationError(f"{what.capitalize()}: the merge into {p['new_id']} is not reflected in the views.")
    elif kind == EffectKind.SPLIT_VIEW:
        for new_id in p["parts"]:
            project._arch_object(new_id)
