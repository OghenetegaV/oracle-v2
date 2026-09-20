"""Oracle — Architectural Interpretation Pipeline

Purpose:
    Runs the whole architectural interpretation of one drawing (interpret_document, or interpret_into for a further
    source of an existing project) and returns an OracleProject holding it:
    units -> layer classification -> spatial view segmentation and classification -> coordinate frames and
    multi-floor alignment -> architectural observations -> vertical (section/elevation) evidence and height
    evidence -> cross-view reconciliation. Every conclusion gets a ProvenanceRecord (with the source
    entity handles, layer, coordinates and the confidence of the evidence) and a ValueStatus; every
    disagreement becomes an InterpretationSet and an EngineeringIssue; nothing is decided for the engineer.
    Every alternative it proposes carries its meaning as validated effects (a value, a height, an alignment ...), so
    an engineer's choice changes the model, and each issue that is the visible face of a question is linked to it.
    establish_levels() is the engineer-approved step that turns confirmed elevations into BuildingModel
    levels (identity, source label, stored storey heights, an elevation type, and evidence links); it never derives
    a structural elevation.

Role in Oracle:
    The architectural drawing intelligence layer, between CAD ingestion and structural reasoning:
    DrawingDocument -> [this] -> OracleProject with an ArchitecturalInterpretation -> engineer review ->
    approved BuildingModel. It creates NO structural elements (no columns, beams or walls in the
    BuildingModel) and no levels until the engineer establishes them. An optional advisor (Claude or any
    other) may propose layer meanings; its output enters only as PROPOSED AI recommendations that the
    engineer accepts or overrides through the normal decision system, never as a direct model change.

Dependencies:
    oracle.core; oracle.ingestion (read_drawing, DrawingDocument); the sibling interpretation modules.

Consumers:
    oracle.interpretation.report and __main__; tests; the future structural reasoning engine and wizard.

Status:
    Interpretation (Phase 3; multi-source and resolution effects in Phase 3.5).

Migration/Notes:
    Deterministic: the same document gives the same IDs and results (timestamps aside). A drawing with no
    readable entities yields a project with a blocking issue, not an exception.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from dataclasses import replace as dataclass_replace
from typing import Mapping, Optional

from oracle import __version__ as ORACLE_VERSION
from oracle.core import (
    ArchitecturalInterpretation, ArchitecturalObservation, BuildingModel, CoordinateFrame, CrossViewFinding,
    DecisionCategory, DecisionSource, DecisionStatus, DrawingSource, DrawingView, Effect, EngineeringDecision,
    EngineeringIssue, EvidenceRelation, HeightEvidence, Interpretation, InterpretationSet, IssueCategory, IssueSeverity,
    LayerClassification, Level, OracleProject, ProvenanceRecord, SourceReference, Target, ValidationError, ValueStatus,
    ValueStatusRecord, ViewType,
)
from oracle.core.common import utc_now_iso
from oracle.ingestion import DrawingDocument, read_drawing

from .alignment import alignment_candidates, grid_label_positions, view_geometry
from .layers import LayerConfig, classify_layers
from .naming import LevelNamer, classify_title, level_display_name, level_id_for, level_sort_key
from .observations import plan_observations, vertical_observations
from .reconcile import reconcile
from .segmentation import dominant_rotation, segment
from .units import detect_units
from .vertical import analyse_elevation, analyse_section

PRODUCER = "oracle.interpretation"
_FFL_RE = re.compile(r"^(?:FFL|F\.?F\.?L\.?|FLOOR LEVEL|LEVEL)\s*[:=]?\s*([+-]?\d+(?:\.\d+)?)\s*(M|MM)?$", re.I)
_SKIP_OBSERVATIONS = (ViewType.SCHEDULE, ViewType.LEGEND, ViewType.NOTES, ViewType.TITLE_BLOCK)


@dataclass
class InterpretationConfig:
    layers: LayerConfig = field(default_factory=LayerConfig)
    level_aliases: Mapping = field(default_factory=dict)
    numeric_offset: int = 0
    advisor: Optional[object] = None          # see oracle.interpretation.advisor; never required


class _Builder:
    """Collects the objects and the evidence for them, then writes both into a project in a fixed order."""

    def __init__(self, project: OracleProject, arch: ArchitecturalInterpretation, document: DrawingDocument, base: int = 0):
        self.project, self.arch, self.doc = project, arch, document
        self.base = base                              # object numbers of a further source start above this block
        self.source_id = arch.drawing.id
        self.root_frame = f"FRM-{base}"
        self.evidence: list = []
        self.statuses: list = []
        self.issue_no = _highest(project.issues, "ARC-")
        self.set_no = _highest(project.interpretation_sets, "IS-")
        self.alt_no = max((int(a.id[4:]) for st in project.interpretation_sets for a in st.alternatives
                           if a.id.startswith("INT-") and a.id[4:].isdigit()), default=0)
        self.pending_issues: list = []
        self.pending_sets: list = []

    def ev(self, target_id: str, method: str, weight: Optional[float], note: str, field: Optional[str] = None,
           handles=(), layer: Optional[str] = None, entity_type: Optional[str] = None, context: Optional[str] = None,
           coordinates=()):
        self.evidence.append((target_id, field, method, weight, note, tuple(handles)[:1], layer, entity_type, context,
                              tuple(coordinates)))

    def status(self, target_id: str, field: str, status: ValueStatus, note: Optional[str] = None):
        self.statuses.append((target_id, field, status, note))

    def issue(self, severity, category, message, target_id: Optional[str] = None, related=(), set_key: Optional[str] = None):
        self.pending_issues.append((severity, category, message, target_id, tuple(related), set_key))


ID_BLOCK = 10000          # each further source numbers its views, observations, frames... from its own block


def _highest(objects, prefix: str) -> int:
    return max((int(o.id[len(prefix):]) for o in objects if o.id.startswith(prefix) and o.id[len(prefix):].isdigit()), default=0)


def interpret_file(path, *, project_name: str, engineer: str, config: Optional[InterpretationConfig] = None,
                   oda_converter: Optional[str] = None) -> OracleProject:
    return interpret_document(read_drawing(path, oda_converter=oda_converter), project_name=project_name,
                              engineer=engineer, config=config)


def interpret_document(document: DrawingDocument, *, project_name: str, engineer: str,
                       config: Optional[InterpretationConfig] = None, project_id: Optional[str] = None,
                       revision: Optional[str] = None, progress=None) -> OracleProject:
    """Interpret one drawing into a NEW project. To add a further drawing (another sheet set, a revised issue) to an
    existing project use interpret_into()."""
    project = (OracleProject(project_id, project_name, engineer, description=f"Architectural interpretation of {document.source_file}")
               if project_id else OracleProject.create(project_name, engineer,
                                                       description=f"Architectural interpretation of {document.source_file}"))
    return interpret_into(project, document, config=config, revision=revision, progress=progress)


# The real stages of interpret_into, in order. A caller may pass `progress(stage, "start" | "done")` to be told when each begins
# and when it has actually finished; nothing is reported "done" that has not completed, and a stage that raises is never "done".
STAGES = ("units", "layers", "regions", "views", "analysis", "alignment", "reconcile", "project")


def _emit(progress, stage: str, event: str) -> None:
    if progress is not None:
        progress(stage, event)


def interpret_into(project: OracleProject, document: DrawingDocument, *, config: Optional[InterpretationConfig] = None,
                   revision: Optional[str] = None, progress=None) -> OracleProject:
    """Interpret a drawing as a further source of `project`. Sources are told apart by id (SRC-n), the revision the caller
    states, the file hash and the interpretation instance (AINT-n); their object numbers never collide. Nothing that
    is already in the project is changed, and no decision is carried over from an earlier source or revision.
    `progress` (optional) is called as progress(stage, "start") and progress(stage, "done") for each of STAGES."""
    config = config or InterpretationConfig()
    namer = LevelNamer(config.level_aliases, config.numeric_offset)
    number = len(project.architectures) + 1
    base = (number - 1) * ID_BLOCK
    now = utc_now_iso()
    _emit(progress, "units", "start")
    verdict = detect_units(document)
    _emit(progress, "units", "done")
    arch = ArchitecturalInterpretation(DrawingSource(
        f"SRC-{number}", document.source_file, document.sha256, verdict.estimate, document.declared_unit, document.extents,
        len(document.entities), tuple(document.warnings), revision, f"AINT-{number}", f"oracle.interpretation {ORACLE_VERSION}",
        document.source_metadata()))
    arch.add(CoordinateFrame(f"FRM-{base}", "Drawing coordinates (source)"))
    b = _Builder(project, arch, document, base)
    source_id = b.source_id
    factor = verdict.estimate.factor_to_mm

    b.ev(source_id, "file_read", None, f"Read {len(document.entities)} entities from {document.source_file}.", handles=(), entity_type="file")
    b.ev(source_id, "unit_detection", verdict.estimate.confidence, "; ".join(d for d, _w in verdict.evidence), field="units",
         entity_type="file")
    b.status(source_id, "units", verdict.basis, verdict.estimate.note)
    if verdict.conflicts:
        b.issue(IssueSeverity.ERROR, IssueCategory.AMBIGUOUS_GEOMETRY, " ".join(verdict.conflicts) + " The engineer must confirm the "
                f"unit; Oracle is provisionally using {verdict.estimate.unit} (factor {factor:g} to mm).", source_id, set_key="units")
    elif verdict.needs_engineer:
        b.issue(IssueSeverity.WARNING, IssueCategory.INCOMPLETE_INFORMATION,
                f"The drawing's units are not established with confidence: provisionally {verdict.estimate.unit} (confidence "
                f"{verdict.estimate.confidence:.2f}). The engineer must confirm.", source_id, set_key="units")
    unit_alternatives = [(f"{a.unit}", a.confidence, f"factor {a.factor_to_mm:g} to mm") for a in verdict.alternatives]

    if not document.entities:
        b.issue(IssueSeverity.BLOCKING, IssueCategory.INCOMPLETE_INFORMATION,
                "The drawing contains no readable entities, so nothing could be interpreted.", source_id)
        return _finish_reporting(progress, b, [], [], unit_alternatives, verdict, now)

    _emit(progress, "layers", "start")
    verdicts = classify_layers(document, config.layers)
    layer_class = {v.name: v.semantic_class for v in verdicts}
    layer_conf = {v.name: v.confidence for v in verdicts}
    layer_ids = {}
    for i, v in enumerate(sorted(verdicts, key=lambda v: v.name), start=base + 1):
        lid = f"LAY-{i:03d}"
        layer_ids[v.name] = lid
        arch.add(LayerClassification(lid, v.name, v.semantic_class, v.confidence, v.method, v.note, v.entity_count))
        b.ev(lid, v.method, v.confidence, v.note or f"Layer {v.name!r} classified as {v.semantic_class} by {v.method}.",
             field="semantic_class", layer=v.name, entity_type="layer")
        b.status(lid, "semantic_class", ValueStatus.INFERRED, v.method)
    unknown = [v for v in verdicts if v.semantic_class == "unknown" and v.entity_count and v.name not in ("0",)]
    if unknown:
        share = sum(v.entity_count for v in unknown) / max(1, len(document.entities))
        b.issue(IssueSeverity.WARNING if share > 0.2 else IssueSeverity.INFO, IssueCategory.INCOMPLETE_INFORMATION,
                f"{len(unknown)} layer(s) could not be classified ({share:.0%} of the entities): "
                f"{', '.join(v.name for v in unknown[:12])}{' ...' if len(unknown) > 12 else ''}. Their content is not used "
                "until the engineer says what they are.", None)
    _emit(progress, "layers", "done")

    _emit(progress, "regions", "start")
    regions, frames, stray = segment(document.entities, layer_class, namer)
    if not regions:
        b.issue(IssueSeverity.BLOCKING, IssueCategory.INCOMPLETE_INFORMATION,
                "No drawn geometry was found (only text or dimensions), so no view could be identified.", source_id)
        _emit(progress, "regions", "done")
        return _finish_reporting(progress, b, [], [], unit_alternatives, verdict, now)
    if stray:
        b.issue(IssueSeverity.INFO, IssueCategory.INCOMPLETE_INFORMATION,
                f"{len(stray)} entities did not belong to any view (isolated marks or text far from every view).", None)

    _emit(progress, "regions", "done")

    _emit(progress, "views", "start")
    views = _build_views(b, regions, frames, layer_class, layer_conf, namer, factor)
    _emit(progress, "views", "done")
    _emit(progress, "analysis", "start")
    plan_rows, verticals, height_rows, obs_count = _analyse_views(b, views, layer_class, layer_conf, namer, factor, unit_alternatives)
    _emit(progress, "analysis", "done")
    _emit(progress, "alignment", "start")
    _align(b, views, layer_class, obs_count)
    _emit(progress, "alignment", "done")
    _emit(progress, "reconcile", "start")
    all_views = [(v["id"], v["type"], v["title"], v["level"], v["variant"], v["label"], v["orientation"]) for v in views]
    sheet_titles = _sheet_titles(views)
    rec = reconcile(plan_rows, verticals, height_rows, sheet_titles, all_views, namer)
    _height_gaps(b, plan_rows, height_rows)
    _emit(progress, "reconcile", "done")
    return _finish_reporting(progress, b, views, rec, unit_alternatives, verdict, now, config, layer_ids, verdicts)


def _finish_reporting(progress, *args):
    _emit(progress, "project", "start")
    project = _finish(*args)
    _emit(progress, "project", "done")
    return project


def _build_views(b, regions, frames, layer_class, layer_conf, namer, factor) -> list:
    arch = b.arch
    views = []
    n = b.base
    for region in regions:
        n += 1
        vid = f"VIEW-{n:02d}"
        info = region.title_info
        levels = list(info.levels) if info else []
        level_key = levels[0].key if (region.view_type == ViewType.FLOOR_PLAN and len(levels) == 1) else None
        rotation, support = dominant_rotation(region.geometry, layer_class)
        rotation = rotation if support >= 0.3 else 0.0
        geo = view_geometry(region, rotation)
        fid = f"FRM-{n:02d}"
        arch.add(CoordinateFrame(fid, f"{vid} local frame", b.root_frame, geo.origin_source, rotation))
        views.append({"id": vid, "region": region, "type": region.view_type, "title": region.title.text if region.title else None,
                      "level": level_key, "levels": levels, "variant": info.variant if info else None,
                      "label": info.section_label if info else None, "orientation": info.orientation if info else None,
                      "frame": fid, "geo": geo, "rotation": rotation, "support": support, "align": None})
    for f in frames:
        n += 1
        vid = f"VIEW-{n:02d}"
        views.append({"id": vid, "frame_entity": f, "type": ViewType.TITLE_BLOCK, "title": None, "level": None, "levels": [],
                      "variant": None, "label": None, "orientation": None, "frame": None, "region": None})
    return views


def _analyse_views(b, views, layer_class, layer_conf, namer, factor, unit_alternatives):
    plan_rows, verticals, height_rows = [], [], []
    obs_no = b.base
    hgt_no = b.base
    for v in views:
        region = v["region"]
        if region is None:                                       # a sheet frame
            f = v["frame_entity"]
            b.arch.add(DrawingView(v["id"], ViewType.TITLE_BLOCK, f.box, 0.7, None, entity_ids=(f.id,)))
            b.ev(v["id"], "sheet_frame", 0.7, "A large closed rectangle enclosing many entities, taken as a sheet frame.",
                 field="view_type", handles=(f.id,), layer=f.layer, entity_type="rectangle",
                 coordinates=[(f.box[0], f.box[1]), (f.box[2], f.box[3])])
            b.status(v["id"], "view_type", ValueStatus.INFERRED, "sheet frame")
            continue
        info = region.title_info
        multi = len(v["levels"]) > 1
        b.arch.add(DrawingView(v["id"], v["type"], region.box, region.confidence, v["title"], v["level"], v["label"], v["orientation"],
                               v["frame"], None, tuple(region.entity_ids()), variant=v["variant"]))
        for kind, description, weight in region.evidence:
            b.ev(v["id"], kind, weight, description, field="view_type",
                 handles=[region.title.id] if region.title and kind == "title_text" else [], layer=None,
                 entity_type="TEXT" if kind == "title_text" else "region", context=f"region of {len(region.geometry)} entities")
        b.ev(v["id"], "spatial_cluster", None, f"{len(region.geometry)} geometry entities connected within a small tolerance; "
             f"{len(region.texts)} texts and {len(region.dims)} dimensions inside.", field="bbox", entity_type="region",
             coordinates=[(region.box[0], region.box[1]), (region.box[2], region.box[3])])
        b.status(v["id"], "view_type", ValueStatus.INFERRED, region.view_type.value)
        b.status(v["id"], "bbox", ValueStatus.DERIVED, "box around the region's entities")
        if v["level"]:
            lv = v["levels"][0]
            b.ev(v["id"], "level_name", lv.confidence, f"The title {v['title']!r} names level {lv.key} ({lv.method}).", field="level_key",
                 handles=[region.title.id], entity_type="TEXT")
            b.status(v["id"], "level_key", ValueStatus.INFERRED, lv.method)
        b.ev(v["id"], "dominant_direction", round(v["support"], 2), f"Dominant line direction {v['rotation']:.1f} degrees "
             f"({v['support']:.0%} of line length).", field="frame_id", entity_type="region")
        b.status(v["id"], "frame_id", ValueStatus.INFERRED, "dominant wall direction")
        if abs(v["rotation"]) >= 0.5:
            b.issue(IssueSeverity.INFO, IssueCategory.AMBIGUOUS_GEOMETRY,
                    f"View {v['id']} is drawn rotated by {v['rotation']:.1f} degrees; its frame rotates it back so its walls run along the axes.", v["id"])
        if region.other_titles and v["type"] not in (ViewType.SCHEDULE,):
            names = [t.text for t, _i in region.other_titles][:4]
            b.ev(v["id"], "other_titles", 0.2, f"Other title-like text near this view: {names}.", field="view_type", entity_type="TEXT")
        if region.alternatives:
            vtarget = Target.architectural(v["id"])
            alts = []
            for t, c in region.alternatives:
                effects = ([Effect.set_value(vtarget, "level_key", None)] if v["level"] and t != ViewType.FLOOR_PLAN else [])
                effects.append(Effect.set_value(vtarget, "view_type", t.value))
                alts.append((t.value, c, "evidence for this reading is close to the leading one", effects))
            _add_set(b, f"What kind of view is {v['id']} ({v['title'] or 'untitled'})?", alts, [v["id"]], f"type_{v['id']}")
            b.issue(IssueSeverity.WARNING, IssueCategory.AMBIGUOUS_GEOMETRY,
                    f"View {v['id']} could be {', '.join(t.value for t, _c in region.alternatives)}; the evidence is close. Engineer review is required.",
                    v["id"], set_key=f"type_{v['id']}")
        if v["type"] == ViewType.FLOOR_PLAN and multi:
            alts = [(lv.key, lv.confidence, f"the title mentions {lv.label!r}",
                     [Effect.set_value(Target.architectural(v["id"]), "level_key", lv.key)]) for lv in v["levels"]]
            _add_set(b, f"Which level does {v['id']} ({v['title']}) show?", alts, [v["id"]], f"level_{v['id']}")
            b.issue(IssueSeverity.WARNING, IssueCategory.AMBIGUOUS_GEOMETRY,
                    f"The title of {v['id']} names several levels ({', '.join(lv.key for lv in v['levels'])}); which one it shows is undecided.",
                    v["id"], set_key=f"level_{v['id']}")
        if v["type"] == ViewType.FLOOR_PLAN:
            plan_rows.append((v["id"], v["level"], v["variant"], v["title"], region.confidence))
            for t in region.texts:
                m = _FFL_RE.match(t.text.strip())
                if m and v["level"] and v["variant"] is None:
                    value = float(m.group(1)) * (1000.0 if (m.group(2) or "").upper() == "M" or ("." in m.group(1) and abs(float(m.group(1))) < 100 and not m.group(2)) else 1.0)
                    v.setdefault("ffl", []).append((v["level"], value, t))
        if v["type"] in (ViewType.SECTION, ViewType.ELEVATION):
            findings = (analyse_section if v["type"] == ViewType.SECTION else analyse_elevation)(region, namer, factor)
            v["findings"] = findings
            verticals.append((v["id"], v["type"], [(t.key, t.elevation_mm) for t in findings.tags],
                              sum(1 for l in findings.lines if l.coverage >= 0.7)))
            for a, c, h, source, basis, conf in findings.heights:
                if a == c:
                    continue
                key = (v["id"], a, c, h, source)
                if key in v.setdefault("_hseen", set()):
                    continue
                v["_hseen"].add(key)
                hgt_no += 1
                hid = f"HGT-{hgt_no:03d}"
                b.arch.add(HeightEvidence(hid, a, c, h, source, ValueStatus(basis), conf, v["id"]))
                handles = [t.name_text.id for t in findings.tags if t.key in (a, c)][:2] if source == "level_tags" else []
                b.ev(hid, source, conf, f"{h:g} mm from {a} to {c} ({source.replace('_', ' ')}).", field="height_mm", handles=handles,
                     entity_type="TEXT" if source == "level_tags" else "LINE", context=f"view {v['id']}")
                b.status(hid, "height_mm", ValueStatus(basis))
                height_rows.append((a, c, h, source, v["id"]))
            for note in findings.notes:
                b.ev(v["id"], "tag_geometry", 0.3, note, field="view_type", entity_type="LINE")
            if findings.tag_geometry_consistent is False:
                b.issue(IssueSeverity.INFO, IssueCategory.INCOMPLETE_INFORMATION,
                        f"In {v['id']} ({v['title']}) the drawn spacing of the level lines does not follow the written elevations; "
                        "the written numbers were used.", v["id"])
            if v["type"] == ViewType.SECTION and not findings.tags and not findings.heights:
                b.issue(IssueSeverity.WARNING, IssueCategory.INCOMPLETE_INFORMATION,
                        f"Section {v['id']} ({v['title']}) carries no level tags and no labelled level lines, so no storey height "
                        "could be established from it.", v["id"])
        for level, value, t in v.get("ffl", []):
            v.setdefault("ffl_rows", []).append((level, value))
        if v["type"] not in _SKIP_OBSERVATIONS:
            specs = plan_observations(region, layer_class, layer_conf, factor)
            if v["type"] in (ViewType.SECTION, ViewType.ELEVATION):
                specs += vertical_observations(v["findings"])
            for s in specs:
                obs_no += 1
                oid = f"OBS-{obs_no:05d}"
                b.arch.add(ArchitecturalObservation(oid, s.kind, v["id"], s.basis, s.confidence, s.layer, tuple(s.entity_ids),
                                                    tuple(s.geometry), s.closed, s.label, s.count, s.hint))
                first = s.evidence[0] if s.evidence else ("observation", "detected", s.confidence)
                b.ev(oid, first[0], first[2], "; ".join(d for _m, d, _w in s.evidence) or f"{s.kind} detected", field="kind",
                     handles=s.entity_ids[:1], layer=s.layer, entity_type=s.kind, context=f"view {v['id']}",
                     coordinates=[(round(x, 3), round(y, 3)) for x, y in s.geometry[:4]])
                b.status(oid, "kind", s.basis)
    # plan-level elevations written on plans (FFL +3300) give heights derived from two known elevations
    ffl = {}
    for v in views:
        for level, value in v.get("ffl_rows", []):
            ffl.setdefault(level, []).append((value, v["id"]))
    ordered = sorted(ffl, key=level_sort_key)
    for a in ordered:
        if len({round(x[0], 1) for x in ffl[a]}) > 1:
            b.issue(IssueSeverity.WARNING, IssueCategory.AMBIGUOUS_GEOMETRY,
                    f"Plans of {level_display_name(a)} state different floor levels ({sorted({x[0] for x in ffl[a]})} mm); none is used.", None,
                    [x[1] for x in ffl[a]])
    ordered = [a for a in ordered if len({round(x[0], 1) for x in ffl[a]}) == 1]
    for a, c in zip(ordered, ordered[1:]):
        (va, ida), (vc, idc) = ffl[a][0], ffl[c][0]
        if vc > va:
            hgt_no += 1
            hid = f"HGT-{hgt_no:03d}"
            b.arch.add(HeightEvidence(hid, a, c, round(vc - va, 1), "plan_text", ValueStatus.DERIVED, 0.7, idc))
            b.ev(hid, "plan_text", 0.7, f"The {a} plan states a floor level of {va:g} mm and the {c} plan {vc:g} mm; the "
                 f"difference is {vc - va:g} mm.", field="height_mm", entity_type="TEXT", context=f"views {ida}, {idc}")
            b.status(hid, "height_mm", ValueStatus.DERIVED, "difference of two written floor levels")
            height_rows.append((a, c, round(vc - va, 1), "plan_text", idc))
    return plan_rows, verticals, height_rows, obs_no


def _sheet_titles(views) -> list:
    titles = []
    for v in views:
        r = v.get("region")
        if r is not None and v["type"] == ViewType.SCHEDULE:
            texts = [t.text for t in r.texts] + [t.text for t, _i in r.other_titles]
            if any("SHEET" in x.upper() for x in texts):
                titles.extend(x for x in texts if classify_title(x).view_type is not None)
    return list(dict.fromkeys(titles))


def _add_set(b, question, alternatives, view_ids, key):
    b.pending_sets.append((question, alternatives, view_ids, key))


def _align(b, views, layer_class, obs_count):
    plans = [v for v in views if v["type"] == ViewType.FLOOR_PLAN and v["level"] and v["variant"] is None and v.get("region")]
    if not plans:
        return
    order = sorted(plans, key=lambda v: (level_sort_key(v["level"]), -len(v["region"].geometry)))
    ref = order[0]
    frame_no = max((int(f.id[4:]) for f in b.arch.frames), default=b.base)
    ref_grid = grid_label_positions(ref["region"], layer_class, ref["geo"])
    for v in order:
        frame_no += 1
        fid = f"FRM-{frame_no:02d}"
        if v is ref:
            b.arch.add(CoordinateFrame(fid, f"{v['id']} to building (reference)", v["frame"], (0.0, 0.0)))
            v["align"] = fid
            b.ev(v["id"], "alignment_reference", 1.0, "The lowest full plan is the reference for the building frame.", field="alignment_frame_id", entity_type="region")
            b.status(v["id"], "alignment_frame_id", ValueStatus.INFERRED, "reference view")
            continue
        cands = alignment_candidates(ref["geo"], ref_grid, v["geo"], grid_label_positions(v["region"], layer_class, v["geo"]))
        best = cands[0]
        strong = best[2] >= 0.75 and all(c[2] < 0.3 for c in cands[1:])
        if strong:
            b.arch.add(CoordinateFrame(fid, f"{v['id']} to building", v["frame"], (-best[0][0], -best[0][1])))
            v["align"] = fid
            b.ev(v["id"], "alignment", best[2], f"Aligned to {ref['id']} by {best[1]}: translation ({best[0][0]:.0f}, {best[0][1]:.0f}).",
                 field="alignment_frame_id", entity_type="region")
            b.status(v["id"], "alignment_frame_id", ValueStatus.INFERRED, best[1])
        else:
            alts = [(f"{m.split(' ')[0]}", c, f"translation ({t[0]:.0f}, {t[1]:.0f}) by {m}",
                     [Effect.align_view(v["id"], (float(t[0]), float(t[1])))]) for t, m, c in cands]
            _add_set(b, f"How does the {v['level']} plan ({v['id']}) line up with the {ref['level']} plan ({ref['id']})?", alts,
                     [v["id"], ref["id"]], f"alignment_{v['id']}")
            b.issue(IssueSeverity.WARNING, IssueCategory.AMBIGUOUS_GEOMETRY,
                    f"The {v['level']} plan {v['id']} cannot be lined up with {ref['id']} with confidence (best: {best[1]}, "
                    f"{best[2]:.2f}); the plans are not merged until the engineer chooses.", v["id"], [ref["id"]],
                    set_key=f"alignment_{v['id']}")
    # rebuild views with alignment frames
    for v in order:
        if v["align"] and b.arch.has(v["id"]):
            b.arch.replace(dataclass_replace(b.arch.get(v["id"]), alignment_frame_id=v["align"]))


def _height_gaps(b, plan_rows, height_rows):
    """A storey height between two consecutive plan levels that no view establishes is reported, never assumed."""
    levels = sorted({p[1] for p in plan_rows if p[1] and p[2] is None}, key=level_sort_key)
    edges: dict = {}
    for a, c, _h, _s, _v in height_rows:
        edges.setdefault(a, set()).add(c)
        edges.setdefault(c, set()).add(a)

    def connected(start, goal) -> bool:
        seen, todo = {start}, [start]
        while todo:
            for nxt in edges.get(todo.pop(), ()):
                if nxt == goal:
                    return True
                if nxt not in seen:
                    seen.add(nxt)
                    todo.append(nxt)
        return False

    for a, c in zip(levels, levels[1:]):
        if not connected(a, c):
            b.issue(IssueSeverity.WARNING, IssueCategory.INCOMPLETE_INFORMATION,
                    f"Storey height for {level_display_name(a)} → {level_display_name(c)} could not be reliably established "
                    "from the available architectural drawing. It is not assumed; the engineer must supply it.", None)


def _finish(b, views, rec, unit_alternatives, verdict, now, config=None, layer_ids=None, verdicts=None):
    project, arch = b.project, b.arch
    project.add_architecture(arch)
    ids: dict = {}
    for (target_id, field, method, weight, note, handles, layer, entity_type, context, coordinates) in b.evidence:
        src = SourceReference(file=b.doc.source_file, entity_handle=handles[0] if handles else None, layer=layer,
                              entity_type=entity_type, source_id=target_id, coordinates=tuple(coordinates),
                              coordinate_frame="drawing coordinates" if coordinates else None, context=context)
        rec_ = ProvenanceRecord(project.next_provenance_id(), Target.architectural(target_id), src, method, PRODUCER,
                                field, None if weight is None else round(min(1.0, max(0.0, weight)), 2), now, note)
        project.add_provenance(rec_)
        ids.setdefault((target_id, field), rec_.id)
        ids.setdefault((target_id, None), rec_.id)
    for target_id, field, status, note in b.statuses:
        pid = ids.get((target_id, field))
        project.set_value_status(ValueStatusRecord(Target.architectural(target_id), field, status,
                                                   provenance_ids=(pid,) if pid else (), note=note))
    set_ids: dict = {}
    pending = list(b.pending_sets)
    if verdict_needs_units(verdict, unit_alternatives):
        unit_rows = [(verdict.estimate.unit, verdict.estimate.confidence, "the leading reading")] + list(unit_alternatives)
        pending.append(("What unit is the drawing in?",
                        [(u, c, r, [_unit_effect(arch.drawing.id, u)]) for u, c, r in unit_rows], [], "units"))
    for r in (rec.sets if rec else []):
        pending.append((r.question, r.alternatives, r.subject_views, r.key))
    for question, alternatives, view_ids, key in pending:
        b.set_no += 1
        sid = f"IS-{b.set_no:03d}"
        alts = []
        for row in alternatives:
            meaning, conf, rationale = row[:3]
            effects = tuple(row[3]) if len(row) > 3 and row[3] else ()
            b.alt_no += 1
            alts.append(Interpretation(f"INT-{b.alt_no:04d}", re.sub(r"[^a-z0-9]+", "_", str(meaning).lower()).strip("_") or "unspecified",
                                       round(min(1.0, max(0.0, conf)), 2), rationale, effects=effects))
        seen, uniq = set(), []
        for a in alts:
            if a.meaning not in seen:
                seen.add(a.meaning)
                uniq.append(a)
        evidence = tuple(dict.fromkeys(ids[(v, None)] for v in view_ids if (v, None) in ids))
        subject = Target.architectural(view_ids[0]) if len(view_ids) == 1 else (Target.architectural(arch.drawing.id) if key == "units" else None)
        project.add_interpretation_set(InterpretationSet(sid, question, uniq, evidence, subject))
        set_ids[key] = sid
    findings = b.base
    for f in (rec.findings if rec else []):
        findings += 1
        arch.add(CrossViewFinding(f"FND-{findings:03d}", f.question, f.agreement, f.summary, tuple(f.view_ids), set_ids.get(f.set_key)))
    for issue in (rec.issues if rec else []):
        b.pending_issues.append((issue.severity, issue.category, issue.message, issue.view_ids[0] if issue.view_ids else None,
                                 tuple(issue.view_ids[1:]), issue.set_key))
    for severity, category, message, target_id, related, set_key in b.pending_issues:
        b.issue_no += 1
        target = Target.architectural(target_id) if target_id else Target.project()
        evidence = tuple(ids[(t, None)] for t in ([target_id] if target_id else []) + list(related) if (t, None) in ids)[:6]
        project.add_issue(EngineeringIssue(f"ARC-{b.issue_no:04d}", severity, category, message, target, PRODUCER,
                                           evidence=evidence, related=tuple(Target.architectural(r) for r in related),
                                           interpretation_set_id=set_ids.get(set_key) if set_key else None))
    if config is not None and config.advisor is not None and verdicts:
        from .advisor import record_layer_advice
        for v in verdicts:
            if v.entity_count and v.confidence < 0.6 and v.name in (layer_ids or {}):
                record_layer_advice(project, layer_ids[v.name], v.name, config.advisor, [e for e in b.doc.entities if e.layer == v.name][:20])
    return project


def _unit_effect(source_id: str, unit: str) -> Effect:
    """Accepting a unit reading sets the drawing's effective unit, as the engineer's own statement (confidence 1)."""
    factors = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "inch": 25.4, "foot": 304.8}
    return Effect.set_value(Target.architectural(source_id), "units", {
        "unit": unit, "factor_to_mm": factors[unit], "confidence": 1.0, "method": "engineer_decision", "note": None})


def verdict_needs_units(verdict, alternatives) -> bool:
    return bool(verdict.conflicts) or (verdict.needs_engineer and bool(alternatives))


def suggest_elevations(project: OracleProject, source_id: Optional[str] = None) -> Optional[dict]:
    """Cumulative level elevations (mm, relative to the lowest plan level) the drawing supports without doubt, or None.
    Refuses while any interpretation set is open or any storey height is missing. Where the engineer has accepted a height
    for a pair of levels that accepted height is used; otherwise the written evidence must agree with itself. It never
    resolves a disagreement, and the numbers it returns say nothing about WHAT elevation they are (finished floor or
    structural): that is stated when the levels are established."""
    arch = project.architecture_of(source_id) if source_id else project.architecture
    if arch is None or project.open_interpretation_sets():
        return None
    levels = sorted({v.level_key for v in arch.views_of(ViewType.FLOOR_PLAN) if v.level_key and v.variant is None},
                    key=level_sort_key)
    if len(levels) < 2:
        return None
    heights: dict = {}
    accepted: dict = {}
    for h in arch.heights:
        heights.setdefault((h.from_level, h.to_level), []).append(h.height_mm)
        if h.basis == ValueStatus.ENGINEER_DEFINED:
            accepted[(h.from_level, h.to_level)] = h.height_mm          # the latest accepted one wins
    if any(k.startswith("NAMED:") for k in levels):                     # an unfamiliar label has no built-in place in the order
        levels = _order_by_heights(levels, heights)
        if levels is None:
            return None
    elevations = {levels[0]: 0.0}
    for a, c in zip(levels, levels[1:]):
        if (a, c) in accepted:
            elevations[c] = elevations[a] + accepted[(a, c)]
            continue
        values = heights.get((a, c))
        if not values or max(values) - min(values) > max(50.0, 0.02 * max(values)):
            return None
        elevations[c] = elevations[a] + sum(values) / len(values)
    return elevations


def _order_by_heights(levels: list, heights: dict) -> Optional[list]:
    """Bottom-to-top order from the height evidence alone (a pair (a, c) says a is below c); None if it is not a single chain."""
    remaining, ordered = list(levels), []
    while remaining:
        bottom = [k for k in remaining if not any((o, k) in heights for o in remaining if o != k)]
        if len(bottom) != 1:
            return None
        ordered.append(bottom[0])
        remaining.remove(bottom[0])
    return ordered


def align_view(project: OracleProject, view_id: str, translation: tuple, *, engineer: str, reason: str,
               set_id: Optional[str] = None, interpretation_id: Optional[str] = None) -> EngineeringDecision:
    """The engineer says how a plan lines up with the building: building = view-local + translation (source units of the
    view's own frame), which may be a value of their own rather than one of Oracle's candidates. One accepted ENGINEER
    decision is recorded; the alignment frame is created and the view's alignment_frame_id is set through set_value (so it
    is an ENGINEER value with history). When the alignment was an open interpretation set, the chosen alternative is
    accepted and its siblings rejected WITHOUT applying that alternative's own translation (the engineer's stands), and
    the issue linked to the set is resolved."""
    if not project.architectures:
        raise ValidationError("This project has no architectural interpretation.")
    project._arch_object(view_id)
    if set_id is not None:                                        # check everything that could fail before changing anything
        chosen_set = project.get_interpretation_set(set_id)
        if chosen_set.status.value != "open" or interpretation_id not in {a.id for a in chosen_set.alternatives}:
            raise ValidationError(f"{interpretation_id!r} cannot be accepted in interpretation set {set_id}.")
    frame_id = project.next_frame_id()
    n = len(project.decisions) + 1
    while any(d.id == f"ENG-{n:04d}" for d in project.decisions):
        n += 1
    decision = EngineeringDecision(
        f"ENG-{n:04d}", engineer, DecisionSource.ENGINEER, Target.architectural(view_id),
        DecisionCategory.OTHER, f"{view_id} lines up with the building by translation {tuple(translation)}.", reason=reason,
        status=DecisionStatus.ACCEPTED, field="alignment_frame_id", value=frame_id)
    project.align_view(view_id, translation, decision)
    if set_id is not None:
        project.accept_interpretation(set_id, interpretation_id, decision.id, apply_effects=False)
    return decision


def establish_levels(project: OracleProject, elevations_mm: Mapping[str, float],
                     decision: Optional[EngineeringDecision] = None, *, elevation_type: str = "unspecified",
                     source_id: Optional[str] = None) -> list:
    """Create BuildingModel levels from elevations (level key -> mm). With an accepted engineer decision the elevations are
    ENGINEER_DEFINED and each level is linked, under that decision, to the height evidence it rests on; without one they are
    only DERIVED.

    Each level keeps its identity (`key`), its label as the drawing wrote it (`source_label`, from the plan's own title) and
    its own engineer-facing name (a generated one is ASSUMED, so it blocks readiness until confirmed). Storey heights are the
    real gaps between the elevations, stored and DERIVED; the top level has none. `elevation_type` says what the elevations
    are (`finished_floor`, `structural`, `datum`; the default `unspecified` claims nothing); claiming one without a decision
    leaves that claim ASSUMED. The STRUCTURAL elevation is never derived from a finished one: unless the type is structural
    it stays unestablished and a warning says so."""
    if decision is not None:
        if decision.source != DecisionSource.ENGINEER or decision.status != DecisionStatus.ACCEPTED or decision.field is not None:
            raise ValidationError("Levels are established by an accepted engineer decision that is not a field change.")
        project._check_new_decision(decision)
    arch = project.architecture_of(source_id) if source_id else project.architecture
    ordered = sorted(elevations_mm.items(), key=lambda kv: kv[1])
    titles: dict = {}
    if arch is not None:
        for v in arch.views_of(ViewType.FLOOR_PLAN):
            if v.level_key and v.variant is None and v.title:
                titles.setdefault(v.level_key, v.title)
    lowest = level_display_name(ordered[0][0]) if ordered else None
    new_levels = []
    for i, (key, elevation) in enumerate(ordered):
        height = round(ordered[i + 1][1] - elevation, 6) if i + 1 < len(ordered) else None
        new_levels.append(Level(level_id_for(key), level_display_name(key), float(elevation), height,
                                source_label=titles.get(key), elevation_type=elevation_type, key=key,
                                datum=f"relative to the lowest level established here ({lowest}); no site datum applied"))
    building = project.building
    created = building is None
    if created:
        building = BuildingModel("BLD-1", project.name)
    for level in new_levels:
        building.add_level(level)
    if created:
        project.set_building(building)
    if decision is not None:
        project._store_decision(decision)
    for level in new_levels:
        target = Target.level(level.id)
        project.set_value_status(ValueStatusRecord(target, "id", ValueStatus.INFERRED, note="from the drawing's level name"))
        project.set_value_status(ValueStatusRecord(target, "name", ValueStatus.ASSUMED, note="generated display name"))
        if level.source_label:
            project.set_value_status(ValueStatusRecord(target, "source_label", ValueStatus.INFERRED,
                                                       note="the title of this level's plan in the drawing"))
        if decision is not None:
            project.set_value_status(ValueStatusRecord(target, "elevation_mm", ValueStatus.ENGINEER_DEFINED, decision_id=decision.id))
        else:
            project.set_value_status(ValueStatusRecord(target, "elevation_mm", ValueStatus.DERIVED, note="from height evidence in the drawing"))
        if level.storey_height_mm is not None:
            project.set_value_status(ValueStatusRecord(target, "storey_height_mm", ValueStatus.DERIVED,
                                                       note="the difference between this level's elevation and the next"))
        if elevation_type != "unspecified":
            project.set_value_status(ValueStatusRecord(
                target, "elevation_type", ValueStatus.ENGINEER_DEFINED if decision is not None else ValueStatus.ASSUMED,
                decision_id=decision.id if decision is not None else None,
                note=None if decision is not None else f"the elevations are assumed to be {elevation_type} levels"))
    if elevation_type != "structural":
        n = _highest(project.issues, "ARC-") + 1
        stated = "stated as " + elevation_type.replace("_", " ") if elevation_type != "unspecified" else "of unstated type"
        project.add_issue(EngineeringIssue(
            f"ARC-{n:04d}", IssueSeverity.WARNING, IssueCategory.INCOMPLETE_INFORMATION,
            f"The structural elevation of the {len(new_levels)} level(s) {[lv.id for lv in new_levels]} has not been established: their "
            f"elevations are {stated} and nothing derives a structural level (such as top of slab) from them. It must come from "
            "evidence or an engineer's decision.", Target.project(), PRODUCER))
    if decision is not None and arch is not None:
        by_key = {lv.key: lv for lv in new_levels}
        for (a, c), rows in _pairs(arch, by_key):
            for h in rows:
                project.link_evidence(Target.level(by_key[c].id), Target.architectural(h.id), EvidenceRelation.DERIVED_FROM,
                                      decision.id, note=f"{h.height_mm:g} mm from {a} to {c}")
        for v in arch.views_of(ViewType.FLOOR_PLAN):
            if v.level_key in by_key and v.variant is None and v.review.value == "accepted":
                project.link_evidence(Target.level(by_key[v.level_key].id), Target.architectural(v.id),
                                      EvidenceRelation.SUPPORTED_BY, decision.id, note="the plan this level is named on")
    return building.levels


def _pairs(arch, by_key: dict) -> list:
    """(from, to) -> the height evidence between two of the levels being established, in level order."""
    out: dict = {}
    for h in arch.heights:
        if h.from_level in by_key and h.to_level in by_key:
            out.setdefault((h.from_level, h.to_level), []).append(h)
    return sorted(out.items(), key=lambda kv: by_key[kv[0][1]].elevation_mm)
