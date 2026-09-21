"""Oracle — Approved Architecture Projection

Purpose:
    A read-only view of what an engineer has APPROVED about one architectural source, expressed only in domain terms
    and millimetres: the accepted views, the accepted observations with their geometry in the building frame (or, for
    a plan that has not been aligned, in its own view frame, and said so), the hints on those observations with whether
    an engineer has approved them, the building's levels (identity, label, source label, elevation, elevation type,
    storey height and the structural elevation, None when not established), the units, and everything that still
    stands between this model and being trustworthy (unconfirmed units, open questions, unreviewed or unaligned views).
    Everything is a frozen dataclass holding tuples; nothing in it can change the project.

Role in Oracle:
    The interface between drawing interpretation and structural reasoning: "Drawing interpretation -> engineer-approved
    architectural model -> structural engine". It deliberately carries NO CAD vocabulary: no layer names, no entity
    handles, no format codes, no paper-space layouts, no raw drawing coordinates. An observation's link back to the
    drawing is not lost, it is reached through project.trace(), which is the audit path, not the working model.

Dependencies:
    oracle.core.architecture, oracle.core.common, oracle.core.value_status. Nothing outside oracle.core.

Consumers:
    OracleProject.approved_architecture(), the future structural reasoning layer, tests.

Status:
    Core (added in schema 0.4.0).

Migration/Notes:
    A projection, not stored data: it adds no persisted field. Approving an observation approves that it is there and
    what it appears to be, never a structural meaning; a hint is reported with `approved` False until an engineer's
    decision has set it (project.set_value on the observation's 'hint').
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .architecture import ReviewStatus
from .common import Target, TargetScope, ValidationError
from .interpretations import SetStatus
from .value_status import ENGINEER_STATUSES, ValueStatus


@dataclass(frozen=True)
class ApprovedLevel:
    id: str
    label: str
    source_label: Optional[str]
    key: Optional[str]
    elevation_mm: float
    elevation_type: str
    structural_elevation_mm: Optional[float]       # None: NOT established (never derived from a finished level)
    storey_height_mm: Optional[float]
    index: int
    datum: Optional[str]


@dataclass(frozen=True)
class ApprovedView:
    id: str
    kind: str                                      # floor_plan, section, elevation, ...
    title: Optional[str]
    level_id: Optional[str]                        # the building level this plan realises, when there is one
    level_key: Optional[str]
    variant: Optional[str]
    confidence: float
    frame: str                                     # "building", "view" (not aligned yet) or "unavailable"
    bbox_mm: Optional[tuple]


@dataclass(frozen=True)
class ApprovedObservation:
    id: str
    view_id: str
    kind: str                                      # what the drawing shows: wall_external, door, column_symbol, ...
    label: Optional[str]
    count: int
    closed: bool
    geometry_mm: Optional[tuple]                   # ((x, y), ...) in the frame of its view; None if that is unavailable
    frame: str
    confidence: float
    basis: str                                     # source or inferred


@dataclass(frozen=True)
class ApprovedHint:
    observation_id: str
    view_id: str
    hint: str                                      # a proposed interpretation, e.g. column_candidate
    confidence: float
    approved: bool                                 # True only once an engineer decision has set it


@dataclass(frozen=True)
class EngineerGuidance:
    """The engineer's own words about something in this drawing, kept verbatim and NOT applied to the model: the next stage must
    read it and act on it through a structured decision (or leave it)."""
    clarification_id: str
    target_scope: str
    target_id: str
    statement: str
    notes: Optional[str]
    author: str
    recorded_at: str
    decision_id: str
    disposition: str                               # "guidance": preserved intent, not applied


@dataclass(frozen=True)
class ApprovedArchitecture:
    source_id: str
    revision: Optional[str]
    unit: str
    unit_confirmed: bool
    views: tuple
    observations: tuple
    hints: tuple
    levels: tuple
    blockers: tuple                                # reasons this model must not yet be relied on
    field_notes: dict = field(default_factory=dict)
    engineer_guidance: tuple = ()                  # the engineer's free-form input about this drawing (verbatim, not applied)

    @property
    def ready(self) -> bool:
        return not self.blockers

    def to_dict(self) -> dict:
        def row(o):
            return {k: (list(v) if isinstance(v, tuple) else v) for k, v in o.__dict__.items()}
        return {"source_id": self.source_id, "revision": self.revision, "unit": self.unit,
                "unit_confirmed": self.unit_confirmed, "ready": self.ready, "blockers": list(self.blockers),
                "views": [row(v) for v in self.views], "observations": [row(o) for o in self.observations],
                "hints": [row(h) for h in self.hints], "levels": [row(l) for l in self.levels],
                "engineer_guidance": [row(g) for g in self.engineer_guidance]}


def _pick_source(project, source_id):
    if source_id is not None:
        return project.architecture_of(source_id)
    archs = project.architectures
    if not archs:
        raise ValidationError("This project has no architectural interpretation to approve.")
    if len(archs) > 1:
        raise ValidationError("This project has several sources; name one: "
                              f"{[a.drawing.id for a in archs]}.")
    return archs[0]


def approved_architecture(project, source_id: Optional[str] = None) -> ApprovedArchitecture:
    arch = _pick_source(project, source_id)
    source = arch.drawing
    factor = source.units.factor_to_mm
    units_status = project.value_status_of(Target.architectural(source.id), "units")
    unit_confirmed = units_status is not None and units_status.status in (ValueStatus.SOURCE, *ENGINEER_STATUSES)

    levels = tuple(ApprovedLevel(lv.id, lv.name, lv.source_label, lv.key, lv.elevation_mm, lv.elevation_type,
                                 lv.structural_elevation, lv.storey_height_mm, lv.index, lv.datum)
                   for lv in (project.building.levels if project.building else []))
    by_key = {l.key: l.id for l in levels if l.key}

    def transform(view, points):
        frame_id = view.alignment_frame_id or view.frame_id
        if frame_id is None:
            return None, "unavailable"
        out = tuple((round(x * factor, 3), round(y * factor, 3)) for x, y in (arch.from_source(frame_id, p) for p in points))
        return out, "building" if view.alignment_frame_id else "view"

    views, view_frames = [], {}
    for v in arch.views:
        if v.review != ReviewStatus.ACCEPTED:
            continue
        x0, y0, x1, y1 = v.bbox
        pts, frame = transform(v, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
        bbox = None if pts is None else (min(p[0] for p in pts), min(p[1] for p in pts),
                                          max(p[0] for p in pts), max(p[1] for p in pts))
        view_frames[v.id] = frame
        views.append(ApprovedView(v.id, v.view_type.value, v.title, by_key.get(v.level_key), v.level_key, v.variant,
                                  v.confidence, frame, bbox))
    observations, hints = [], []
    for o in arch.observations:
        if o.review != ReviewStatus.ACCEPTED or o.view_id not in view_frames:
            continue
        view = arch.get(o.view_id)
        pts, frame = transform(view, list(o.geometry)) if o.geometry else (tuple(), view_frames[o.view_id])
        observations.append(ApprovedObservation(o.id, o.view_id, o.kind, o.label, o.count, o.closed, pts, frame,
                                                o.confidence, o.basis.value))
        if o.hint is not None:
            status = project.value_status_of(Target.architectural(o.id), "hint")
            hints.append(ApprovedHint(o.id, o.view_id, o.hint.value, o.confidence,
                                      status is not None and status.status in ENGINEER_STATUSES))

    blockers = []
    if not unit_confirmed:
        blockers.append(f"The unit of {source.id} is not confirmed ({source.units.unit}, confidence "
                        f"{source.units.confidence:.2f}); coordinates in millimetres rest on that assumption.")
    open_sets = [s for s in project.interpretation_sets if s.status == SetStatus.OPEN]
    if open_sets:
        blockers.append(f"{len(open_sets)} interpretation question(s) are open: {[s.id for s in open_sets][:5]}.")
    unreviewed = [v.id for v in arch.views if v.review == ReviewStatus.PROPOSED
                  and v.view_type.value in ("floor_plan", "section", "elevation")]
    if unreviewed:
        blockers.append(f"{len(unreviewed)} plan/section/elevation view(s) have not been reviewed: {unreviewed[:5]}.")
    unaligned = [v.id for v in views if v.kind == "floor_plan" and v.frame != "building" and v.variant is None]
    if unaligned:
        blockers.append(f"Approved plan(s) not aligned to the building frame: {unaligned[:5]}.")
    if not levels:
        blockers.append("No building levels have been established by an engineer decision.")
    guidance = tuple(
        EngineerGuidance(c.id, c.target.scope.value, c.target.id, c.statement, c.notes, c.author, c.created_at, c.decision_id, c.disposition)
        for c in project.clarifications if c.target.scope == TargetScope.PROJECT or arch.has(c.target.id))
    return ApprovedArchitecture(source.id, source.revision, "mm", unit_confirmed, tuple(views), tuple(observations),
                                tuple(hints), levels, tuple(blockers), engineer_guidance=guidance)
