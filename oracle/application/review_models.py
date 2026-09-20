"""Oracle — Review Models (application layer)

Purpose:
    Turns an OracleProject that holds an architectural interpretation into the plain rows a review screen shows: a drawing summary, the
    detected views, the detected and the engineer-established levels, the observations grouped by category, the open questions
    (interpretation sets with their alternatives, evidence and linked issues), the issues, the readiness banner and the approved
    architecture. Every number and label is READ from the project; nothing here is a default, a placeholder or a guess. Where the
    backend does not know something (an elevation in dispute, a concept the approved projection does not yet contain), the row says so.

Role in Oracle:
    The read side of the interface. It contains no interface toolkit code and changes nothing, so it can be tested without a window and
    reused by any front end. Readiness is never decided here: the banner is a description of oracle.core's own ProjectReadiness.

Dependencies:
    oracle.core; oracle.interpretation.observations (the observation vocabulary); standard library.

Consumers:
    oracle.application.session, oracle.ui.architectural_workspace, tests.

Status:
    Application layer (interface phase).

Migration/Notes:
    Observation categories group the interpreter's own kinds for browsing; a kind the table does not know is shown under "Other
    architectural observations", never dropped and never renamed.
"""

from __future__ import annotations

from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from typing import Optional

from oracle.core import (
    BlockerKind, EffectKind, IssueSeverity, OracleProject, ReviewStatus, SetStatus, Target, TargetScope, ValueStatus, ViewType,
)
from oracle.core.value_status import ENGINEER_STATUSES
from oracle.interpretation.naming import level_display_name, level_sort_key

# ---------------------------------------------------------------- vocabulary of the display

OBSERVATION_CATEGORIES = OrderedDict([
    ("walls", ("Walls", ("wall", "wall_external", "wall_internal"))),
    ("openings", ("Openings: doors and windows", ("door", "window", "glazing"))),
    ("column_candidates", ("Column candidates (symbols)", ("column_symbol",))),
    ("beam_candidates", ("Beam candidates (symbols)", ("beam_symbol",))),
    ("circulation", ("Stairs, lifts and circulation", ("stair", "stair_linework", "lift", "lift_linework", "ramp", "handrail"))),
    ("voids", ("Voids, shafts and double-height spaces", ("void", "shaft", "double_height_space"))),
    ("levels", ("Level and floor lines", ("level_line", "partial_floor_line", "floor_linework", "roof_linework"))),
    ("grid", ("Grid", ("grid_lines",))),
    ("rooms", ("Rooms and fixtures", ("toilet", "balcony", "furniture", "casework", "sanitary_fixtures"))),
])
OTHER_CATEGORY = ("other", "Other architectural observations")
_CATEGORY_OF = {kind: key for key, (_title, kinds) in OBSERVATION_CATEGORIES.items() for kind in kinds}


def category_of(kind: str) -> str:
    return _CATEGORY_OF.get(kind, OTHER_CATEGORY[0])


def category_title(key: str) -> str:
    return OBSERVATION_CATEGORIES[key][0] if key in OBSERVATION_CATEGORIES else OTHER_CATEGORY[1]


REVIEW_WORDS = {"proposed": "Proposed (not reviewed)", "accepted": "Engineer approved", "rejected": "Rejected by engineer",
                "superseded": "Superseded (merged or split)"}


def review_words(status) -> str:
    return REVIEW_WORDS.get(getattr(status, "value", status), str(status))


# ---------------------------------------------------------------- sources and summary

@dataclass(frozen=True)
class SourceInfo:
    id: str
    file: str
    revision: Optional[str]
    sha256: str
    interpretation_id: str
    declared_unit: Optional[str]
    entity_count: int

    @property
    def label(self) -> str:
        return f"{self.id}: {self.file}" + (f" (revision {self.revision})" if self.revision else "")


def source_infos(project: OracleProject) -> list:
    return [SourceInfo(a.drawing.id, a.drawing.file, a.drawing.revision, a.drawing.sha256, a.drawing.interpretation_id,
                       a.drawing.declared_unit, a.drawing.entity_count) for a in project.architectures]


@dataclass
class Summary:
    source: SourceInfo
    unit: str
    unit_factor_to_mm: float
    unit_confidence: float
    unit_method: str
    unit_status: str                      # the value status in words
    unit_confirmed: bool
    unit_note: Optional[str]
    frame_lines: list
    views_total: int
    views_by_type: dict
    views_accepted: int
    views_unreviewed: int
    plan_levels_detected: int
    levels_established: int
    observations: int
    observation_kinds: int
    open_interpretations: int
    resolved_interpretations: int
    issues_open: int
    issues_blocking: int
    issues_error: int
    issues_warning: int
    warnings: list


def _value_status_words(project: OracleProject, target: Target, field_name: str) -> tuple:
    record = project.value_status_of(target, field_name)
    if record is None:
        return "not recorded", False
    words = {ValueStatus.SOURCE: "read from the file", ValueStatus.INFERRED: "inferred by Oracle", ValueStatus.ASSUMED: "assumed (unconfirmed)",
             ValueStatus.DERIVED: "derived", ValueStatus.ENGINEER_DEFINED: "engineer established",
             ValueStatus.ENGINEER_OVERRIDE: "engineer override", ValueStatus.CALCULATED: "calculated"}
    return words.get(record.status, record.status.value), record.status in ENGINEER_STATUSES or record.status == ValueStatus.SOURCE


def build_summary(project: OracleProject, source_id: str) -> Summary:
    arch = project.architecture_of(source_id)
    d = arch.drawing
    status_words, confirmed = _value_status_words(project, Target.architectural(d.id), "units")
    views = [v for v in arch.views if v.review != ReviewStatus.SUPERSEDED]
    plans = [v for v in views if v.view_type == ViewType.FLOOR_PLAN and v.variant is None]
    rotated = []
    for v in views:
        frame = next((f for f in arch.frames if f.id == v.frame_id), None)
        if frame is not None and abs(frame.rotation_deg) >= 0.5:
            rotated.append(f"{v.id} ({frame.rotation_deg:.1f}°)")
    aligned = [v for v in plans if v.alignment_frame_id]
    lines = [f"Source drawing coordinates are kept exactly as drawn ({d.entity_count} entities read).",
             f"{len(views)} view(s) have their own view frame; {len(rotated)} drawn rotated" + (f": {', '.join(rotated)}." if rotated else ".")]
    if plans:
        lines.append(f"{len(aligned)} of {len(plans)} plan(s) are aligned to the building frame"
                     + ("" if len(aligned) == len(plans) else "; the rest are not merged until an alignment is settled."))
    issues = [i for i in project.issues if i.is_open and _issue_in_source(project, i, arch)]
    sets = [s for s in project.interpretation_sets if _set_in_source(project, s, arch)]
    kinds = {o.kind for o in arch.observations}
    return Summary(
        source=SourceInfo(d.id, d.file, d.revision, d.sha256, d.interpretation_id, d.declared_unit, d.entity_count),
        unit=d.units.unit, unit_factor_to_mm=d.units.factor_to_mm, unit_confidence=d.units.confidence, unit_method=d.units.method,
        unit_status=status_words, unit_confirmed=confirmed, unit_note=d.units.note, frame_lines=lines,
        views_total=len(views), views_by_type=dict(Counter(v.view_type.value for v in views)),
        views_accepted=sum(v.review == ReviewStatus.ACCEPTED for v in views),
        views_unreviewed=sum(v.review == ReviewStatus.PROPOSED for v in views),
        plan_levels_detected=len({v.level_key for v in plans if v.level_key}),
        levels_established=len(project.building.levels) if project.building else 0,
        observations=len(arch.observations), observation_kinds=len(kinds),
        open_interpretations=sum(s.status == SetStatus.OPEN for s in sets),
        resolved_interpretations=sum(s.status == SetStatus.RESOLVED for s in sets),
        issues_open=len(issues), issues_blocking=sum(i.severity == IssueSeverity.BLOCKING for i in issues),
        issues_error=sum(i.severity == IssueSeverity.ERROR for i in issues),
        issues_warning=sum(i.severity == IssueSeverity.WARNING for i in issues), warnings=list(d.warnings))


def _owner(project: OracleProject, object_id: Optional[str]):
    """The architectural interpretation (source) that holds this object id, or None."""
    return next((a for a in project.architectures if a.has(object_id)), None) if object_id else None


def _provenance(project: OracleProject, provenance_id: str):
    try:
        return project.get_provenance(provenance_id)
    except Exception:
        return None


def _issue_in_source(project: OracleProject, issue, arch) -> bool:
    ids = [issue.target.id] + [t.id for t in issue.related]
    if issue.target.scope != TargetScope.ARCHITECTURAL and not any(t.scope == TargetScope.ARCHITECTURAL for t in issue.related):
        return True                       # a project-wide issue belongs to every source
    return any(_owner(project, i) is arch for i in ids if i)


def _set_in_source(project: OracleProject, s, arch) -> bool:
    ids = affected_ids(project, s)
    return not ids or any(_owner(project, i) is arch for i in ids)


# ---------------------------------------------------------------- what a question is about

def affected_ids(project: OracleProject, s) -> list:
    """Architectural objects an interpretation set is about: its subject, the objects its evidence records describe, the objects its
    alternatives would change, and the views named by the cross-view finding that raised it."""
    found: list = []

    def add(object_id):
        if object_id and object_id not in found and _owner(project, object_id) is not None:
            found.append(object_id)

    if s.subject is not None and s.subject.scope == TargetScope.ARCHITECTURAL:
        add(s.subject.id)
    for pid in s.evidence:
        record = _provenance(project, pid)
        if record is not None and record.target.scope == TargetScope.ARCHITECTURAL:
            add(record.target.id)
    for alt in s.alternatives:
        for e in alt.effects:
            if e.kind == EffectKind.SET_VALUE and e.target.scope == TargetScope.ARCHITECTURAL:
                add(e.target.id)
            elif e.kind == EffectKind.ALIGN_VIEW:
                add(e.params["view_id"])
            elif e.kind == EffectKind.MERGE_VIEWS:
                for v in e.params["view_ids"]:
                    add(v)
            elif e.kind == EffectKind.SPLIT_VIEW:
                add(e.params["view_id"])
    for a in project.architectures:
        for f in a.findings:
            if f.interpretation_set_id == s.id:
                for v in f.views:
                    add(v)
    return found


def describe_effect(effect) -> str:
    """One plain sentence for what accepting an alternative WOULD do (its machine-readable effect, in words)."""
    p = effect.params
    k = effect.kind
    if k == EffectKind.SET_VALUE:
        value = p["value"]
        if isinstance(value, dict) and "unit" in value:
            value = f"{value['unit']} (factor {value['factor_to_mm']:g} to mm)"
        t = effect.target
        return f"Set {p['field'].replace('_', ' ')} of {t.scope.value} {t.id} to {value}"
    if k == EffectKind.ACCEPT_HEIGHT:
        return f"Accept {p['height_mm']:g} mm as the storey height from {p['from_level']} to {p['to_level']}"
    if k == EffectKind.ALIGN_VIEW:
        return f"Align {p['view_id']} to the building frame by translation ({p['translation'][0]:.0f}, {p['translation'][1]:.0f})"
    if k == EffectKind.MERGE_VIEWS:
        return f"Merge {', '.join(p['view_ids'])} into one view ({p['new_id']})"
    if k == EffectKind.SPLIT_VIEW:
        return f"Split {p['view_id']} into {', '.join(p['parts'])}"
    return p.get("note") or "Record this answer (no model value changes)"


# ---------------------------------------------------------------- views

@dataclass
class ViewRow:
    id: str
    source_id: str
    title: str
    type: str
    level_key: Optional[str]
    level_label: str
    variant: Optional[str]
    confidence: float
    review: str
    review_words: str
    aligned: Optional[bool]               # None for a view that is not a plan
    flags: list
    open_sets: list
    entity_count: int
    orientation: Optional[str]
    section_label: Optional[str]


def build_view_rows(project: OracleProject, source_id: str) -> list:
    arch = project.architecture_of(source_id)
    open_by_view: dict = {}
    for s in project.interpretation_sets:
        if s.status == SetStatus.OPEN:
            for oid in affected_ids(project, s):
                open_by_view.setdefault(oid, []).append(s.id)
    rows = []
    for v in arch.views:
        flags = []
        sets = open_by_view.get(v.id, [])
        if sets:
            flags.append(f"{len(sets)} open question(s)")
        if v.view_type == ViewType.FLOOR_PLAN and v.variant is None and v.level_key is None and v.review != ReviewStatus.SUPERSEDED:
            flags.append("level not named")
        if v.view_type == ViewType.UNKNOWN:
            flags.append("type not established")
        if v.confidence < 0.5:
            flags.append("low confidence")
        level_label = ""
        if v.level_key:
            level_label = level_display_name(v.level_key)
        rows.append(ViewRow(
            v.id, source_id, v.title or "(untitled)", v.view_type.value.replace("_", " "), v.level_key, level_label, v.variant,
            v.confidence, v.review.value, review_words(v.review),
            (v.alignment_frame_id is not None) if v.view_type == ViewType.FLOOR_PLAN and v.variant is None else None,
            flags, sets, len(v.entity_ids), v.orientation, v.section_label))
    order = {t: i for i, t in enumerate(("floor plan", "section", "elevation", "detail", "schedule", "legend", "notes", "title block", "unknown"))}
    rows.sort(key=lambda r: (order.get(r.type, 99), r.level_key is None, level_sort_key(r.level_key) if r.level_key else (9, 0), r.id))
    return rows


# ---------------------------------------------------------------- levels

@dataclass
class DetectedLevel:
    key: str
    label: str
    plan_titles: list
    plan_view_ids: list
    height_above_previous: Optional[float]
    height_evidence: list                 # (height_mm, source, basis)
    elevation_from_lowest_mm: Optional[float]
    conflict: bool
    conflict_note: Optional[str]
    open_sets: list
    status: str                           # words: what is known about this level
    established_id: Optional[str]


@dataclass
class EstablishedLevel:
    id: str
    name: str
    source_label: Optional[str]
    key: Optional[str]
    elevation_mm: float
    elevation_type: str
    elevation_type_words: str
    storey_height_mm: Optional[float]
    structural_elevation_mm: Optional[float]
    elevation_status: str
    name_status: str
    engineer_established: bool
    evidence_links: int
    datum: Optional[str]


_TYPE_WORDS = {"unspecified": "type not stated", "finished_floor": "finished floor level", "structural": "structural level", "datum": "datum / reference line"}


def build_levels(project: OracleProject, source_id: str) -> tuple:
    """(detected levels, established levels). Detected levels are what the drawing shows (Oracle's reading); established levels
    are BuildingModel levels an engineer created."""
    arch = project.architecture_of(source_id)
    plans = [v for v in arch.views_of(ViewType.FLOOR_PLAN) if v.variant is None and v.level_key]
    keys = {v.level_key for v in plans}
    pairs: dict = {}
    for h in arch.heights:
        pairs.setdefault((h.from_level, h.to_level), []).append(h)
        keys.update((h.from_level, h.to_level))
    ordered = sorted(keys, key=level_sort_key)
    plan_ordered = [k for k in ordered if k in {v.level_key for v in plans}]
    established = {lv.key: lv for lv in (project.building.levels if project.building else []) if lv.key}
    open_sets = [s for s in project.interpretation_sets if s.status == SetStatus.OPEN]

    def sets_for(key):
        found = []
        for s in open_sets:
            for alt in s.alternatives:
                if any(e.kind == EffectKind.ACCEPT_HEIGHT and key in (e.params["from_level"], e.params["to_level"]) for e in alt.effects):
                    found.append(s.id)
                    break
            else:
                if f"level name {key} " in s.question or f"the {key} level" in s.question:
                    found.append(s.id)
        return found

    detected = []
    cumulative: Optional[float] = 0.0
    previous = None
    for key in plan_ordered:
        titles = [v.title for v in plans if v.level_key == key and v.title]
        evidence, conflict, note, above = [], False, None, None
        if previous is not None:
            rows = pairs.get((previous, key), [])
            evidence = [(h.height_mm, h.source, h.basis.value) for h in rows]
            accepted = [h for h in rows if h.basis == ValueStatus.ENGINEER_DEFINED]
            values = sorted({round(h.height_mm) for h in rows})
            if accepted:
                above = accepted[-1].height_mm
            elif len(values) == 1:
                above = float(values[0])
            elif len(values) > 1 and max(values) - min(values) > max(50.0, 0.02 * max(values)):
                conflict, note = True, "conflicting evidence: " + " / ".join(f"{v} mm" for v in values)
            elif values:
                above = sum(h.height_mm for h in rows) / len(rows)
            else:
                note = "no written height between these levels"
        cumulative = None if (cumulative is None or (previous is not None and above is None)) else cumulative + (above or 0.0)
        est = established.get(key)
        level_sets = sets_for(key)
        if conflict or level_sets:
            conflict = True
            note = note or "an open question concerns this level"
        status = "Engineer established" if est is not None else "Detected / inferred (not confirmed)"
        detected.append(DetectedLevel(key, level_display_name(key), titles, [v.id for v in plans if v.level_key == key], above, evidence,
                                      cumulative, conflict, note, level_sets, status, est.id if est else None))
        previous = key

    built = []
    for lv in (project.building.levels if project.building else []):
        el_status, _ = _value_status_words(project, Target.level(lv.id), "elevation_mm")
        name_status, _ = _value_status_words(project, Target.level(lv.id), "name")
        record = project.value_status_of(Target.level(lv.id), "elevation_mm")
        built.append(EstablishedLevel(
            lv.id, lv.name, lv.source_label, lv.key, lv.elevation_mm, lv.elevation_type, _TYPE_WORDS.get(lv.elevation_type, lv.elevation_type),
            lv.storey_height_mm, lv.structural_elevation_mm, el_status, name_status,
            record is not None and record.status in ENGINEER_STATUSES, len(project.links_for(Target.level(lv.id))), lv.datum))
    return detected, built


# ---------------------------------------------------------------- observations

@dataclass
class ObservationRow:
    id: str
    source_id: str
    view_id: str
    view_title: str
    kind: str
    category: str
    label: Optional[str]
    count: int
    confidence: float
    basis: str
    hint: Optional[str]
    hint_approved: bool
    review: str
    review_words: str
    closed: bool
    layer_note: Optional[str]


def build_observation_groups(project: OracleProject, source_id: str) -> "OrderedDict":
    arch = project.architecture_of(source_id)
    titles = {v.id: (v.title or v.id) for v in arch.views}
    groups: "OrderedDict[str, list]" = OrderedDict((key, []) for key in [*OBSERVATION_CATEGORIES, OTHER_CATEGORY[0]])
    for o in arch.observations:
        hint_status = project.value_status_of(Target.architectural(o.id), "hint")
        groups[category_of(o.kind)].append(ObservationRow(
            o.id, source_id, o.view_id, titles.get(o.view_id, o.view_id), o.kind, category_of(o.kind), o.label, o.count, o.confidence,
            o.basis.value, o.hint.value if o.hint else None, bool(hint_status and hint_status.status in ENGINEER_STATUSES),
            o.review.value, review_words(o.review), o.closed, o.layer))
    return OrderedDict((k, v) for k, v in groups.items() if v)


# ---------------------------------------------------------------- questions and issues

@dataclass
class AlternativeRow:
    id: str
    meaning: str
    confidence: float
    rationale: Optional[str]
    consequences: list                    # what accepting it would do, in words
    status: str
    is_leading: bool                      # Oracle's highest-confidence reading (a proposal, never pre-selected)


@dataclass
class IssueRow:
    id: str
    severity: str
    category: str
    message: str
    status: str
    target: str
    related: list
    set_id: Optional[str]
    decision_id: Optional[str]
    resolution: Optional[str]
    affected: list


@dataclass
class QuestionCard:
    id: str
    question: str
    status: str
    observed: list                        # what Oracle observed: findings and issue messages
    alternatives: list
    evidence: list                        # provenance summaries
    affected: list                        # architectural object ids (views / observations / sources)
    issues: list                          # IssueRow linked to this question
    accepted_id: Optional[str]
    decision_id: Optional[str]
    kind: str


def _issue_row(project: OracleProject, i) -> IssueRow:
    affected = [t.id for t in [i.target, *i.related] if t.scope == TargetScope.ARCHITECTURAL]
    return IssueRow(i.id, i.severity.value, i.category.value.replace("_", " "), i.message, i.status.value,
                    f"{i.target.scope.value} {i.target.id}" if i.target.id else "project", [f"{t.scope.value} {t.id}" for t in i.related],
                    i.interpretation_set_id, i.decision_id, i.resolution, affected)


def build_issue_rows(project: OracleProject, source_id: Optional[str] = None, *, include_closed: bool = True) -> list:
    arch = project.architecture_of(source_id) if source_id else None
    order = {"blocking": 0, "error": 1, "warning": 2, "info": 3}
    rows = [_issue_row(project, i) for i in project.issues
            if (include_closed or i.is_open) and (arch is None or _issue_in_source(project, i, arch))]
    rows.sort(key=lambda r: (r.status != "open", order.get(r.severity, 9), r.id))
    return rows


def _question_kind(s) -> str:
    q = s.question.lower()
    if "unit" in q:
        return "units"
    if "height" in q:
        return "storey height"
    if "line up" in q:
        return "alignment"
    if "kind of view" in q:
        return "view type"
    if "which level" in q:
        return "level of a plan"
    if "levels exist" in q or "level name" in q:
        return "levels"
    return "interpretation"


def build_questions(project: OracleProject, source_id: Optional[str] = None, *, open_only: bool = False) -> list:
    arch = project.architecture_of(source_id) if source_id else None
    cards = []
    for s in project.interpretation_sets:
        if arch is not None and not _set_in_source(project, s, arch):
            continue
        if open_only and s.status != SetStatus.OPEN:
            continue
        issues = [_issue_row(project, i) for i in project.issues if i.interpretation_set_id == s.id]
        observed = []
        for a in project.architectures:
            observed += [f"{f.summary}" for f in a.findings if f.interpretation_set_id == s.id]
        observed += [i.message for i in issues]
        evidence = []
        for pid in s.evidence:
            r = _provenance(project, pid)
            if r is not None:
                evidence.append(f"{r.target.id}: {r.method} by {r.producer}" + (f" – {r.note}" if r.note else ""))
        leading = max(s.alternatives, key=lambda a: a.confidence).id if s.alternatives else None
        alts = [AlternativeRow(a.id, a.meaning.replace("_", " "), a.confidence, a.rationale, [describe_effect(e) for e in a.effects],
                               a.status.value, a.id == leading) for a in sorted(s.alternatives, key=lambda a: -a.confidence)]
        cards.append(QuestionCard(s.id, s.question, s.status.value, observed, alts, evidence, affected_ids(project, s), issues,
                                  s.accepted.id if s.accepted else None, s.accepted.decision_id if s.accepted else None, _question_kind(s)))
    cards.sort(key=lambda c: (c.status != "open", c.id))
    return cards


# ---------------------------------------------------------------- readiness

@dataclass
class ReadinessBanner:
    level: str                            # "ready", "review" or "blocked": a description of ProjectReadiness, not a second rule
    headline: str
    summary: str
    blockers: list                        # (kind label, reference, message)
    unconfirmed: int


_KIND_LABEL = {BlockerKind.BLOCKING_ISSUE: "Blocking issue", BlockerKind.OPEN_INTERPRETATION: "Unresolved interpretation",
               BlockerKind.ASSUMED_VALUE: "Assumed value", BlockerKind.NO_BUILDING: "No building levels established",
               BlockerKind.UNREVIEWED_VIEW: "View awaiting review"}


def build_readiness(project: OracleProject) -> ReadinessBanner:
    r = project.readiness()
    blockers = [(_KIND_LABEL.get(b.kind, b.kind.value), b.reference, b.message) for b in r.blockers]
    if r.ready:
        level, head = "ready", "✓ Ready: the project has no readiness blockers"
    elif r.by_kind(BlockerKind.BLOCKING_ISSUE):
        level, head = "blocked", f"✕ Blocked: {len(r.blockers)} item(s) stand in the way, including blocking issues"
    else:
        level, head = "review", f"⚠ Engineer review required: {len(r.blockers)} item(s)"
    return ReadinessBanner(level, head, r.summary(), blockers, len(r.unconfirmed))


# ---------------------------------------------------------------- approved architecture

@dataclass
class ApprovedPanel:
    source: SourceInfo
    unit: str
    unit_confirmed: bool
    ready: bool
    blockers: list
    views: list
    observations: list
    hints: list
    levels: list
    proposed_views: list
    proposed_observation_count: int
    open_questions: int
    relationships: list
    not_yet_available: list


def build_approved(project: OracleProject, source_id: str) -> ApprovedPanel:
    approved = project.approved_architecture(source_id)
    arch = project.architecture_of(source_id)
    d = arch.drawing
    proposed_views = [(v.id, v.title or "(untitled)", v.view_type.value.replace("_", " "), review_words(v.review))
                      for v in arch.views if v.review in (ReviewStatus.PROPOSED, ReviewStatus.REJECTED)]
    accepted_views = {v.id for v in arch.views if v.review == ReviewStatus.ACCEPTED}
    proposed_obs = sum(1 for o in arch.observations if o.review != ReviewStatus.ACCEPTED or o.view_id not in accepted_views)
    relationships = []
    for link in project.evidence_links:
        if _owner(project, link.evidence.id) is arch:
            subject = (f"{link.subject.scope.value} {link.subject.id}" if link.subject is not None else f"decision {link.subject_decision_id}")
            relationships.append(f"{subject} {link.relation.value.replace('_', ' ')} {link.evidence.id} (decision {link.decision_id})")
    return ApprovedPanel(
        SourceInfo(d.id, d.file, d.revision, d.sha256, d.interpretation_id, d.declared_unit, d.entity_count), approved.unit,
        approved.unit_confirmed, approved.ready, list(approved.blockers),
        [(v.id, v.title or "(untitled)", v.kind.replace("_", " "), v.level_id or v.level_key or "", v.frame, v.confidence) for v in approved.views],
        [(o.id, o.view_id, o.kind, o.label or "", o.count, o.frame, o.confidence) for o in approved.observations],
        [(h.observation_id, h.hint, h.approved) for h in approved.hints],
        [(l.id, l.label, l.source_label or "", l.key or "", l.elevation_mm, l.elevation_type, l.storey_height_mm, l.structural_elevation_mm)
         for l in approved.levels],
        proposed_views, proposed_obs, sum(s.status == SetStatus.OPEN for s in project.interpretation_sets), relationships,
        ["Grid axes (labels and positions)", "Wall segments (centre line and thickness)", "Orientation (north) and drawing scale",
         "Openings related to the wall that hosts them"])
