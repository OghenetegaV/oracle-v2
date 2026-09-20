"""Oracle — Evidence Trace

Purpose:
    trace(project, target): walks the evidence chain of a domain object BACKWARDS and returns it as data:

        final object (level, element, node, grid, decision, or an interpretation object)
          -> its engineer decisions and value statuses
          -> its evidence links (each backed by an engineer decision)
          -> the architectural evidence they name (view, observation, height) and its approval state
          -> the provenance records of that evidence (method, producer, confidence, recording time, source file, entity handle)
          -> the source drawing (file, revision, hash, interpretation instance)
          -> the source entity identifiers

    Whatever is missing is reported as a GAP in words; nothing is inferred to fill it. A trace with no gaps is
    `complete`. Engineer-defined heights are followed through the resolution that created them to the evidence of the
    question that was answered.

Role in Oracle:
    Makes the guarantee "any final value can be explained back to the drawing" checkable instead of assumed, and the
    exact place where it fails visible. It reads the project and changes nothing. Entity identifiers are opaque
    strings supplied by the ingestion layer; the trace neither knows nor cares what produced them.

Dependencies:
    oracle.core.common, oracle.core.architecture, oracle.core.decisions.

Consumers:
    OracleProject.trace(), tests, and later the structural reasoning and reporting layers.

Status:
    Core (added in schema 0.4.0).

Migration/Notes:
    Objects created before 0.4.0 have no evidence links, so their traces report that link as missing; they are not
    guessed at.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .architecture import ArchitecturalObservation, HeightEvidence, ReviewStatus
from .common import Target, TargetScope, ValidationError
from .decisions import DecisionSource, DecisionStatus


@dataclass(frozen=True)
class TraceStep:
    kind: str        # target | value | decision | evidence_link | evidence | provenance | resolution | source
    ref: str
    summary: str
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "ref": self.ref, "summary": self.summary, "data": self.data}


@dataclass(frozen=True)
class Trace:
    subject: dict
    steps: tuple
    sources: tuple
    entities: tuple            # {"source_id", "entity_id", "via", "sampled"}
    gaps: tuple
    confidence: Optional[float]

    @property
    def complete(self) -> bool:
        return not self.gaps

    def steps_of(self, kind: str) -> list:
        return [s for s in self.steps if s.kind == kind]

    def to_dict(self) -> dict:
        return {"subject": self.subject, "complete": self.complete, "confidence": self.confidence,
                "steps": [s.to_dict() for s in self.steps], "sources": list(self.sources),
                "entities": list(self.entities), "gaps": list(self.gaps)}


def _resolve(project, target):
    if isinstance(target, Target):
        return target
    if not isinstance(target, str):
        raise ValidationError("trace() takes a Target or an object id.")
    found = []
    b = project.building
    if b is not None:
        for scope, getter in ((TargetScope.ELEMENT, b.get_element), (TargetScope.LEVEL, b.get_level),
                              (TargetScope.NODE, b.get_node), (TargetScope.GRID, b.get_grid)):
            try:
                getter(target)
                found.append(Target(scope, target))
            except ValidationError:
                pass
    if project._arch_holding(target) is not None:
        found.append(Target.architectural(target))
    if target in project._decisions:
        found.append(("decision", target))
    if len(found) > 1:
        raise ValidationError(f"{target!r} names more than one kind of object; pass a Target.")
    if not found:
        raise ValidationError(f"Nothing in this project is called {target!r}.")
    return found[0]


class _Walk:
    def __init__(self, project):
        self.p = project
        self.steps: list = []
        self.gaps: list = []
        self.entities: list = []
        self.sources: dict = {}
        self.weights: list = []
        self._seen: set = set()

    def add(self, kind, ref, summary, **data):
        self.steps.append(TraceStep(kind, ref, summary, data))

    def gap(self, text):
        if text not in self.gaps:
            self.gaps.append(text)

    def source(self, arch):
        d = arch.drawing
        if d.id not in self.sources:
            self.sources[d.id] = {"id": d.id, "file": d.file, "revision": d.revision, "sha256": d.sha256,
                                  "interpretation_id": d.interpretation_id, "declared_unit": d.declared_unit}
            self.add("source", d.id, f"source {d.file}" + (f" revision {d.revision}" if d.revision else ""),
                     **self.sources[d.id])

    def entity(self, arch, entity_id, via, sampled=False):
        row = {"source_id": arch.drawing.id, "entity_id": entity_id, "via": via, "sampled": sampled}
        if row not in self.entities:
            self.entities.append(row)

    # ---- a decision and the field/value statuses of a target
    def decision(self, d, why):
        self.add("decision", d.id, f"{d.source.value} decision {d.id}: {d.instruction}", status=d.status.value,
                 author=d.author, source=d.source.value, target=d.target.to_dict(), field=d.field, value=d.value,
                 previous_value=d.previous_value, created_at=d.created_at, why=why)

    def values_of(self, target):
        for r in self.p.value_statuses:
            if r.target == target:
                self.add("value", f"{target.id}.{r.field}", f"{r.field} is {r.status.value}", status=r.status.value,
                         decision_id=r.decision_id, provenance_ids=list(r.provenance_ids), note=r.note)

    # ---- the architectural evidence and everything behind it
    def evidence(self, target: Target):
        if target.id in self._seen:
            return
        self._seen.add(target.id)
        arch = self.p._arch_holding(target.id)
        if arch is None:
            self.gap(f"The evidence {target.id} is named but does not exist in this project.")
            return
        obj = arch.get(target.id)
        review = getattr(obj, "review", None)
        self.add("evidence", target.id, f"{type(obj).__name__} {target.id}", type=type(obj).__name__,
                 review=review.value if review else None, confidence=getattr(obj, "confidence", None),
                 object_kind=getattr(obj, "kind", None) or getattr(getattr(obj, "view_type", None), "value", None),
                 source_id=arch.drawing.id)
        if review is not None and review != ReviewStatus.ACCEPTED:
            self.gap(f"{target.id} is {review.value}, not accepted by an engineer.")
        if getattr(obj, "confidence", None) is not None:
            self.weights.append(obj.confidence)
        records = self.p.provenance_for(target)
        if not records:
            self.gap(f"{target.id} has no provenance record: where it came from is not recorded.")
        for r in records:
            s = r.source
            self.add("provenance", r.id, f"{r.method} by {r.producer}", method=r.method, producer=r.producer,
                     confidence=r.confidence, recorded_at=r.recorded_at, file=s.file, entity_handle=s.entity_handle, layer=s.layer,
                     entity_type=s.entity_type, context=s.context, field=r.field, note=r.note)
            if r.confidence is not None:
                self.weights.append(r.confidence)
            if s.entity_handle:
                self.entity(arch, s.entity_handle, f"provenance {r.id}")
        ids = list(getattr(obj, "entity_ids", ()) or ())
        sampled = isinstance(obj, ArchitecturalObservation) and obj.count > len(ids)
        for eid in ids:
            self.entity(arch, eid, f"{type(obj).__name__} {obj.id}", sampled)
        if isinstance(obj, HeightEvidence) and obj.source == "engineer_resolution":
            self.resolution(arch, obj)
        elif not ids and not any(r.source.entity_handle for r in records):
            self.gap(f"No source entity identifier is recorded for {target.id}.")
        if isinstance(obj, HeightEvidence) and obj.view_id and obj.source != "engineer_resolution":
            self.add("evidence", obj.view_id, f"height {obj.id} was read from view {obj.view_id}", relation="read_from")
        self.source(arch)

    def resolution(self, arch, height):
        for s in self.p.interpretation_sets:
            alt = s.accepted
            if alt is None:
                continue
            if any(height.id in (rec.get("created") or []) for rec in alt.applied):
                self.add("resolution", s.id, f"{height.id} was created when the engineer accepted {alt.id} ({alt.meaning})",
                         question=s.question, alternative=alt.id, decision_id=alt.decision_id)
                d = self.p._decisions.get(alt.decision_id)
                if d is not None:
                    self.decision(d, "accepted the interpretation")
                for pid in s.evidence:
                    r = self.p._provenance.get(pid)
                    if r is not None:
                        self.add("provenance", r.id, f"{r.method} by {r.producer} (evidence of the question)",
                                 method=r.method, producer=r.producer, confidence=r.confidence, recorded_at=r.recorded_at,
                                 file=r.source.file, entity_handle=r.source.entity_handle, layer=r.source.layer,
                                 entity_type=r.source.entity_type, context=r.source.context, field=r.field, note=r.note)
                        if r.source.entity_handle:
                            self.entity(arch, r.source.entity_handle, f"question evidence {r.id}")
                if not s.evidence:
                    self.gap(f"The question {s.id} that produced {height.id} cites no evidence.")
                return
        self.gap(f"{height.id} is engineer-defined but no accepted interpretation records how it came about.")


def trace(project, target) -> Trace:
    subject = _resolve(project, target)
    w = _Walk(project)
    if isinstance(subject, tuple):                     # a decision
        d = project.get_decision(subject[1])
        w.decision(d, "the decision traced")
        links = project.links_for(subject[1])
        if d.target.scope != TargetScope.PROJECT:
            w.add("target", d.target.id, f"the decision is about {d.target.scope.value} {d.target.id}", target=d.target.to_dict())
            links = links + project.links_for(d.target)
            if d.target.scope == TargetScope.ARCHITECTURAL:
                w.evidence(d.target)
        _links(w, project, links)
        subject_dict = {"decision": subject[1]}
    elif subject.scope == TargetScope.ARCHITECTURAL:
        w.add("target", subject.id, f"architectural object {subject.id}", target=subject.to_dict())
        w.evidence(subject)
        for d in project.decisions_for(subject):
            w.decision(d, "an engineer decision about it")
        subject_dict = subject.to_dict()
    else:
        w.add("target", subject.id, f"{subject.scope.value} {subject.id}", target=subject.to_dict())
        w.values_of(subject)
        for d in project.decisions_for(subject):
            w.decision(d, "an engineer decision about it")
        links = project.links_for(subject)
        if not links:
            w.gap(f"{subject.scope.value} {subject.id} has no evidence link: nothing records which drawing evidence it "
                  "was derived from.")
        _links(w, project, links)
        subject_dict = subject.to_dict()
    confidence = round(min(w.weights), 3) if w.weights else None
    return Trace(subject_dict, tuple(w.steps), tuple(w.sources.values()), tuple(w.entities), tuple(w.gaps), confidence)


def _links(w: _Walk, project, links) -> None:
    for link in links:
        w.add("evidence_link", link.id, f"{link.relation.value} {link.evidence.id}", relation=link.relation.value,
              evidence=link.evidence.to_dict(), decision_id=link.decision_id, created_at=link.created_at, note=link.note)
        d = project._decisions.get(link.decision_id)
        if d is None:
            w.gap(f"Evidence link {link.id} cites decision {link.decision_id}, which does not exist.")
        else:
            w.decision(d, f"established evidence link {link.id}")
            if d.source != DecisionSource.ENGINEER or d.status not in (DecisionStatus.ACCEPTED, DecisionStatus.SUPERSEDED):
                w.gap(f"Evidence link {link.id} is not backed by an accepted engineer decision.")
        w.evidence(link.evidence)
