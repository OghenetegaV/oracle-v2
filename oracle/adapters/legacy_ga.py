"""Oracle — Legacy GA Parser Adapter

Purpose:
    Translates the result of the legacy multi-floor GA parser (ga_dxf_parser.parse_multilevel_ga)
    into an oracle.core BuildingModel inside an OracleProject: levels, nodes, columns, beams and
    slab panels, with a ProvenanceRecord for every object (file, layer, entity type, member/joint
    number, coordinates), a ValueStatus for its important fields, and EngineeringIssues for everything
    that was assumed, inferred, lost, could not be represented, or looks structurally wrong.

    Value statuses written: beam and node geometry SOURCE; column and slab geometry, level
    membership, slab supports and level IDs INFERRED; member sections and default slab thickness
    ASSUMED (placeholders from the parser's default tables, with their own field-level provenance);
    level elevations, engineer-given names and thicknesses ENGINEER_DEFINED, backed by an ACCEPTED
    engineer decision the adapter records on the caller's declaration; storey heights DERIVED. Nothing
    is invented for material or design basis. A column ending on a beam interior with no node is
    reported as a BLOCKING structural issue and the beam is not split.

Role in Oracle:
    First import adapter (Phase 2). It is translation only. It contains no structural design,
    no Claude calls, no STAAD and no drawing generation, and it does not bend oracle.core to fit
    parser quirks: what the core cannot hold is reported as an issue, never silently dropped.
    It reads the parser's result dict by duck typing and never imports the legacy scripts, so
    oracle stays independent of them.

    Units and axes: the parser works in metres with STAAD axes, i.e. a joint is
    (x, elevation, z) where z is the DXF Y coordinate. The core uses millimetres and a plan
    (x, y) per level, so core x_mm = x*1000, core y_mm = z*1000, level elevation_mm =
    elevation*1000. The plan origin is the DXF sheet origin of the reference floor (the parser
    translates other floors onto it), not a structural grid origin.

Dependencies:
    oracle.core; oracle.adapters.result. The caller supplies the parser result, the elevations
    it was run with, and the parser's section size table (ga_dxf_parser.DEFAULT_SIZES), so no
    legacy constants are duplicated here.

Consumers:
    tests; the wizard and a future importer once connected (not connected yet).

Status:
    Adapter (Phase 2).

Migration/Notes:
    Retained while ga_dxf_parser is in use. If the parser is later made to keep DXF entity
    handles, VOID outlines, grid lines and column outline sizes, this adapter should read them
    and the corresponding issues should disappear. See docs/PHASE_2_ADAPTER.md.
"""

from __future__ import annotations

import math
import re
from typing import Any, Mapping, Optional

from oracle.core import (
    Beam, BuildingModel, Column, DecisionCategory, DecisionSource, DecisionStatus, DesignBasis, ElementKind,
    EngineeringDecision, EngineeringIssue, IssueCategory, IssueSeverity, Level, Node, OracleProject, Point2D, Polygon2D,
    ProvenanceRecord, Section, Slab, SourceReference, Target, ValidationError, ValueStatus, ValueStatusRecord,
)
from oracle.core.common import POSITION_TOL_MM, utc_now_iso

from .result import AdaptationResult

FRAME_JOINT = "ga_dxf_parser joint frame: metres, (x, elevation, DXF y)"
FRAME_PLAN = "ga_dxf_parser plan frame: metres, (x, DXF y)"

SYSTEM = "ga_dxf_parser"
ADAPTER = "oracle.adapters.legacy_ga"
M_TO_MM = 1000.0
ELEVATION_MATCH_TOL_MM = 0.5
SUPPORT_TOL_MM = 25.0  # parser snaps panel corners to 5 mm and tolerates 20 mm drafting error
END_NODE_TOL_MM = 80.0   # parser snaps column joints onto beam joints within 75 mm
INTERIOR_TOL_MM = 30.0   # lateral tolerance for "the column sits on this beam's centreline"
SUSPECT_SPAN_MM = (500.0, 100_000.0)  # longest beam outside this suggests the unit assumption is wrong
_PREVIEW = 6

# Maps known legacy message wording to a category. Text matching is brittle by nature, so the
# fallback is OTHER and the legacy message is always preserved verbatim.
_LEGACY_CATEGORY_RULES = (
    (re.compile(r"drawn offset|duplicate member|snapped onto|neither purely horizontal", re.I),
     IssueCategory.AMBIGUOUS_GEOMETRY),
    (re.compile(r"No ground-level", re.I), IssueCategory.MISSING_SUPPORT),
    (re.compile(r"VOID markers|No 'COLUMN|don't chain|beam layer|storey height|Column layer tag", re.I),
     IssueCategory.INCOMPLETE_INFORMATION),
)
_LEGACY_SEVERITY = {"warning": IssueSeverity.WARNING, "blocking": IssueSeverity.BLOCKING}


class _Run:
    """State of one adaptation: the project being built and the helpers that write issues, provenance,
    value statuses and engineer-input decisions into it."""

    def __init__(self, project: OracleProject, source_file: Optional[str]):
        self.project = project
        self.source_file = source_file
        self.now = utc_now_iso()  # one timestamp for the whole import
        self.level_id: dict = {}  # legacy level tag -> core level id
        self.failures = 0         # dropped or failed items; structural observations do not count
        self._issue_no = 0
        self._decision_no = 0
        self._prov_of: dict = {}

    def issue(self, severity, category, message, target: Optional[Target] = None, source: str = ADAPTER, *,
              evidence=(), related=(), structural: bool = False):
        """`structural` marks an observation about the source model (e.g. a beam with no node under a column):
        blocking for final output, but not a sign that the adapter dropped or failed to build something."""
        self._issue_no += 1
        target = target or Target.project()
        if severity in (IssueSeverity.ERROR, IssueSeverity.BLOCKING) and not structural:
            self.failures += 1
        issue_id = f"IMP-{self._issue_no:04d}"
        try:
            self.project.add_issue(EngineeringIssue(issue_id, severity, category, message, target, source,
                                                    evidence=tuple(evidence), related=tuple(related)))
        except ValidationError:  # target missing from the model: keep the issue, widen its scope
            self.project.add_issue(EngineeringIssue(issue_id, severity, category,
                                                    f"{message} (originally about {target.scope.value} {target.id})",
                                                    Target.project(), source))

    def prov(self, target: Target, *, method: str, source_id: str, field: Optional[str] = None,
             layer: Optional[str] = None, entity_type: Optional[str] = None, context: Optional[str] = None,
             coordinates=(), frame: Optional[str] = None, note: Optional[str] = None,
             from_file: bool = True) -> str:
        """`from_file` is False for evidence that is not in the source drawing (the parser's default tables)."""
        record = ProvenanceRecord(
            self.project.next_provenance_id(), target,
            SourceReference(file=self.source_file if from_file else None, layer=layer, entity_type=entity_type, source_id=source_id,
                            coordinates=tuple(coordinates), coordinate_frame=frame if coordinates else None,
                            context=context),
            method=method, producer=SYSTEM, field=field, recorded_at=self.now, note=note)
        self.project.add_provenance(record)
        if field is None:
            self._prov_of[(target.scope.value, target.id)] = record.id
        return record.id

    def prov_id(self, target: Target) -> Optional[str]:
        return self._prov_of.get((target.scope.value, target.id))

    def status(self, target: Target, field: str, status: ValueStatus, *, provenance_ids=(), decision_id=None,
               note: Optional[str] = None):
        self.project.set_value_status(ValueStatusRecord(target, field, status, decision_id=decision_id,
                                                        provenance_ids=tuple(provenance_ids), note=note))

    def engineer_input(self, target: Target, category: DecisionCategory, instruction: str) -> str:
        """Record something the caller declared to be the engineer's input as an ACCEPTED engineer decision,
        so the values it defines can be marked ENGINEER_DEFINED. The adapter only relays the caller's claim."""
        self._decision_no += 1
        decision = EngineeringDecision(f"IMP-D-{self._decision_no:04d}", self.project.engineer,
                                       DecisionSource.ENGINEER, target, category, instruction,
                                       status=DecisionStatus.ACCEPTED, reason="Supplied by the engineer at import.",
                                       created_at=self.now)
        self.project.add_decision(decision)
        return decision.id


def _msg(what: str, *, source: str, missing: str, action_required: bool) -> str:
    return (f"{what} Source: {source}. Missing information: {missing}. "
            f"Engineer action: {'required' if action_required else 'not required'}.")


def _preview(ids) -> str:
    ids = list(ids)
    shown = ", ".join(ids[:_PREVIEW])
    return shown + (f" and {len(ids) - _PREVIEW} more" if len(ids) > _PREVIEW else "")


def _safe_id(raw: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.\-]", "_", str(raw))[:64]
    return cleaned if re.match(r"[A-Za-z0-9]", cleaned or "_") else f"L{cleaned}"[:64]


def _num_key(object_id: str):
    digits = re.findall(r"\d+", object_id)
    return (int(digits[0]) if digits else 0, object_id)


def adapt_legacy_ga(parse_result: Mapping[str, Any], elevations_m: Mapping[str, float], *,
                    project_name: str, engineer: str, section_sizes_mm: Mapping[str, Any],
                    project_id: Optional[str] = None, source_file: Optional[str] = None,
                    building_name: Optional[str] = None, level_names: Optional[Mapping[str, str]] = None,
                    engineer_loading: Optional[Mapping[str, Any]] = None,
                    design_basis: Optional[DesignBasis] = None) -> AdaptationResult:
    """Adapt one parse_multilevel_ga() result.

    parse_result       the dict returned by ga_dxf_parser.parse_multilevel_ga(..., storey_heights_m=...)
    elevations_m       {level tag: elevation in metres above ground}; the SAME dict passed to the parser
                       as storey_heights_m (that name is misleading: the values are cumulative elevations)
    section_sizes_mm   the parser's size table, {"column_mm": (w, d), "beam_mm": (w, d), ...}
    engineer_loading   the per_level_loading dict given to the parser, if any; used only to tell an
                       engineer-supplied slab thickness from the parser's default
    design_basis       attached as given; never invented here

    Never raises for bad source data: problems become EngineeringIssues on the returned project, and
    result.complete is False if anything was dropped or could not be built."""
    if project_id:
        project = OracleProject(project_id, project_name, engineer,
                                description=f"Imported from legacy GA: {source_file}" if source_file else None)
    else:
        project = OracleProject.create(project_name, engineer,
                                       description=f"Imported from legacy GA: {source_file}" if source_file else None)
    run = _Run(project, source_file)

    for legacy in parse_result.get("issues") or ():
        kind = getattr(legacy, "kind", None)
        message = str(getattr(legacy, "message", legacy))
        severity = _LEGACY_SEVERITY.get(kind, IssueSeverity.ERROR)
        category = next((c for rx, c in _LEGACY_CATEGORY_RULES if rx.search(message)), IssueCategory.OTHER)
        if kind not in _LEGACY_SEVERITY:
            message = f"[unrecognised legacy issue kind {kind!r}] {message}"
        run.issue(severity, category, message, source=SYSTEM)

    building = _build_levels(run, parse_result, elevations_m, level_names, building_name or project_name)
    if building is None:
        return AdaptationResult(project, run.failures)

    _build_structure(run, building, parse_result, section_sizes_mm, engineer_loading)
    _attach_design_basis(run, design_basis)
    _general_notes(run, level_names)

    try:
        project.validate()
    except ValidationError as exc:
        run.issue(IssueSeverity.BLOCKING, IssueCategory.OTHER,
                  _msg(f"The adapted project failed core validation: {exc}", source="oracle.core validate()",
                       missing="a consistent model", action_required=True))
    return AdaptationResult(project, run.failures)


# ------------------------------------------------------------------ levels

def _build_levels(run: _Run, parse_result, elevations_m, level_names, building_name) -> Optional[BuildingModel]:
    def fail(text, missing):
        run.issue(IssueSeverity.BLOCKING, IssueCategory.INCOMPLETE_INFORMATION,
                  _msg(text, source=SYSTEM, missing=missing, action_required=True))
        return None

    tags = list(parse_result.get("levels") or [])
    if parse_result.get("model") is None or not tags:
        return fail("The legacy parse result contains no structural model, so nothing was imported.",
                    "levels and members (the parser stopped before building a model; see the parser issues)")
    missing = [t for t in tags if t not in elevations_m]
    if missing:
        return fail(f"No elevation was given for level(s) {missing}.", "level elevations")
    values = []
    for t in tags:
        v = elevations_m[t]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            return fail(f"Elevation for level {t!r} is not a finite number ({v!r}).", "a valid elevation")
        values.append(float(v))
    if any(b <= a for a, b in zip(values, values[1:])):
        return fail(f"Level elevations {dict(zip(tags, values))} are not strictly increasing in the legacy level "
                    "order (ground first), so the storey sequence is contradictory.", "consistent elevations")

    ids = [_safe_id(t) for t in tags]
    if len(set(ids)) != len(ids):
        return fail(f"Level tags {tags} collide once made into valid IDs.", "unique level IDs")

    building = BuildingModel("BLD-1", building_name)
    run.level_id = dict(zip(tags, ids))
    layers = parse_result.get("beam_layer_by_level") or {}
    try:
        for i, (tag, lid, elev) in enumerate(zip(tags, ids, values)):
            name = (level_names or {}).get(tag) or f"Level {tag}"
            height = round((values[i + 1] - elev) * M_TO_MM, 3) if i + 1 < len(values) else None
            building.add_level(Level(lid, name, round(elev * M_TO_MM, 3), height))
    except ValidationError as exc:
        return fail(f"Levels could not be created: {exc}", "valid levels")
    run.project.set_building(building)

    elevation_decision = run.engineer_input(
        Target.project(), DecisionCategory.LAYOUT,
        "Level elevations supplied by the engineer at import (metres above ground): "
        + ", ".join(f"{t}={v:g}" for t, v in zip(tags, values)) + ".")
    given_names = {t: n for t, n in (level_names or {}).items() if t in tags and n}
    names_decision = None
    if given_names:
        names_decision = run.engineer_input(
            Target.project(), DecisionCategory.OTHER,
            "Level names supplied by the engineer at import: "
            + ", ".join(f"{t}={n}" for t, n in given_names.items()) + ".")
    for i, (tag, lid) in enumerate(zip(tags, ids)):
        target = Target.level(lid)
        pv = run.prov(target, method="level tag taken from the COLUMN/beam layer-name pattern", source_id=f"tag {tag}",
                      layer=layers.get(tag), context=f"legacy level sequence {tags}",
                      note="ID from the layer-name tag; elevation supplied by the caller (engineer input), not read "
                           "from the drawing" + ("" if tag in given_names else "; name is generated"))
        run.status(target, "id", ValueStatus.INFERRED, provenance_ids=[pv])
        if tag in given_names:
            run.status(target, "name", ValueStatus.ENGINEER_DEFINED, decision_id=names_decision)
        else:
            run.status(target, "name", ValueStatus.ASSUMED, provenance_ids=[pv], note="generated from the tag")
        run.status(target, "elevation_mm", ValueStatus.ENGINEER_DEFINED, decision_id=elevation_decision)
        if i + 1 < len(tags):
            run.status(target, "storey_height_mm", ValueStatus.DERIVED, note="difference of consecutive elevations")
    return building


# ------------------------------------------------------------------ nodes, members, slabs

def _build_structure(run: _Run, building: BuildingModel, parse_result, section_sizes_mm, engineer_loading):
    model = parse_result["model"]
    coords = {int(k): v for k, v in model["joints"].coordinates().items()}
    members = list(model.get("members") or [])
    tags = list(parse_result["levels"])
    level_id = run.level_id
    beam_layers = parse_result.get("beam_layer_by_level") or {}
    column_layers = parse_result.get("column_layer_by_boundary") or {}
    elevation_mm = {level_id[t]: building.get_level(level_id[t]).elevation_mm for t in tags}

    def level_at(elev_m):
        e = elev_m * M_TO_MM
        return next((lid for lid, v in elevation_mm.items() if abs(v - e) <= ELEVATION_MATCH_TOL_MM), None)

    def plan(joint):
        x, _, z = coords[joint]
        return Point2D(round(float(x) * M_TO_MM, 3), round(float(z) * M_TO_MM, 3))

    # ---- nodes: only joints that a member uses (the parser also registers slab-panel corners)
    node_of, merged = {}, []
    for j in sorted({j for m in members for j in (m[1], m[2])}):
        if j not in coords:
            run.issue(IssueSeverity.ERROR, IssueCategory.OTHER,
                      _msg(f"Joint {j} is used by a member but has no coordinates.", source=f"{SYSTEM} joint {j}",
                           missing="joint coordinates", action_required=True))
            continue
        lid = level_at(float(coords[j][1]))
        if lid is None:
            run.issue(IssueSeverity.ERROR, IssueCategory.INCOMPLETE_INFORMATION,
                      _msg(f"Joint {j} is at elevation {coords[j][1]} m, which matches no level.",
                           source=f"{SYSTEM} joint {j}", missing="the level this joint belongs to",
                           action_required=True))
            continue
        loc = plan(j)
        twin = next((n for n in building.nodes if n.level_id == lid
                     and n.location.distance_to(loc) <= POSITION_TOL_MM), None)
        if twin is not None:
            node_of[j] = twin.id
            merged.append((j, twin.id, twin.location.distance_to(loc)))
            continue
        building.add_node(Node(f"N{j}", lid, loc))
        node_of[j] = f"N{j}"
        node_target = Target.node(f"N{j}")
        pv = run.prov(node_target, method="joint registered by the parser", source_id=f"joint {j}",
                      entity_type="joint", context=f"level {lid}",
                      coordinates=[tuple(float(v) for v in coords[j])], frame=FRAME_JOINT,
                      note="joint coordinates as built by the parser (floor-aligned, columns snapped to beam joints)")
        run.status(node_target, "geometry", ValueStatus.SOURCE, provenance_ids=[pv])
    if merged:
        run.issue(IssueSeverity.WARNING, IssueCategory.AMBIGUOUS_GEOMETRY,
                  _msg(f"{len(merged)} legacy joint(s) lay within {POSITION_TOL_MM:g} mm of another joint on the same "
                       f"level and were merged into it (e.g. joint {merged[0][0]} into {merged[0][1]}).",
                       source=f"{SYSTEM} joints", missing="which of the two positions is correct",
                       action_required=False))

    # ---- sections come from the caller's size table; they are assumptions, never drawing facts
    def section(size_key):
        w, d = section_sizes_mm[size_key]
        return Section.rectangular(w, d), (w, d)

    beams_by_group, columns_by_group, beam_ends = {}, {}, {}
    seen_beam_pairs = set()
    beam_lengths, drift = [], {}
    for no, j1, j2, kind, where, size_key in members:
        if kind not in ("beam", "column"):
            run.issue(IssueSeverity.ERROR, IssueCategory.OTHER,
                      _msg(f"Member {no} has kind {kind!r}, which is neither beam nor column, and was not imported.",
                           source=f"{SYSTEM} member {no}", missing="a supported member type", action_required=True))
            continue
        try:
            sec, wd = section(size_key)
        except (KeyError, TypeError, ValueError, ValidationError):
            run.issue(IssueSeverity.ERROR, IssueCategory.INCOMPLETE_INFORMATION,
                      _msg(f"Member {no} needs section size key {size_key!r} which is absent or invalid in the "
                           "supplied size table, so it was not imported.", source=f"{SYSTEM} member {no}",
                           missing="a section size", action_required=True))
            continue
        try:
            if kind == "beam":
                _add_beam(run, building, no, j1, j2, where, size_key, sec, wd, node_of, coords, level_at, level_id,
                          beam_layers, seen_beam_pairs, beams_by_group, beam_ends, beam_lengths)
            else:
                _add_column(run, building, no, j1, j2, where, size_key, sec, wd, coords, plan, level_at, level_id,
                            column_layers, columns_by_group, drift)
        except ValidationError as exc:
            run.issue(IssueSeverity.ERROR, IssueCategory.OTHER,
                      _msg(f"{kind.capitalize()} from member {no} was rejected by the core model: {exc}",
                           source=f"{SYSTEM} member {no}", missing="a valid element", action_required=True))

    _report_groups(run, building, beams_by_group, columns_by_group, drift)
    _report_unconnected_columns(run, building)
    if beam_lengths and not (SUSPECT_SPAN_MM[0] <= max(beam_lengths) <= SUSPECT_SPAN_MM[1]):
        run.issue(IssueSeverity.WARNING, IssueCategory.SUSPICIOUS_SPAN,
                  _msg(f"The longest beam is {max(beam_lengths):.1f} mm, outside {SUSPECT_SPAN_MM[0]:g}-"
                       f"{SUSPECT_SPAN_MM[1]:g} mm. The parser assumes DXF units are millimetres; a drawing in other "
                       "units would give this result.", source=f"{SYSTEM} geometry", missing="confirmed drawing units",
                       action_required=True))

    _build_slabs(run, building, parse_result, tags, level_id, beam_layers, beam_ends, engineer_loading)
    _report_voids(run, parse_result, tags, level_id)


def _add_beam(run, building, no, j1, j2, tag, size_key, sec, wd, node_of, coords, level_at, level_id, beam_layers,
              seen_pairs, groups, beam_ends, lengths):
    if tag not in level_id:
        raise ValidationError(f"beam level tag {tag!r} is not one of the legacy levels")
    lid = level_id[tag]
    n1, n2 = node_of.get(j1), node_of.get(j2)
    if n1 is None or n2 is None:
        raise ValidationError(f"joint {j1 if n1 is None else j2} could not be turned into a node")
    for j in (j1, j2):
        if level_at(float(coords[j][1])) != lid:
            raise ValidationError(f"joint {j} is not at the elevation of level {tag!r}")
    key = (lid, frozenset((n1, n2)))
    if n1 == n2 or key in seen_pairs:
        run.issue(IssueSeverity.WARNING, IssueCategory.AMBIGUOUS_GEOMETRY,
                  _msg(f"Beam from member {no} duplicates or collapses onto another beam (nodes {n1}, {n2}) after "
                       "joints were merged, and was not imported again.", source=f"{SYSTEM} member {no}",
                       missing="none (redundant geometry)", action_required=False))
        return
    seen_pairs.add(key)
    beam_id = f"B{no}"
    building.add_element(Beam(beam_id, lid, n1, n2, sec))
    lengths.append(building.member_length_mm(beam_id))
    beam_ends[beam_id] = (building.get_node(n1).location, building.get_node(n2).location)
    groups.setdefault((lid, size_key, wd), []).append(beam_id)
    target = Target.element(beam_id)
    pv = run.prov(target, method="beam centreline segment extracted by the parser",
                  source_id=f"member {no} (joints {j1}-{j2})", layer=beam_layers.get(tag),
                  entity_type="polyline segment", context=f"level {tag}",
                  coordinates=[tuple(float(v) for v in coords[j1]), tuple(float(v) for v in coords[j2])],
                  frame=FRAME_JOINT, note="centreline geometry extracted; section is ASSUMED (legacy default size)")
    run.status(target, "geometry", ValueStatus.SOURCE, provenance_ids=[pv])
    run.status(target, "level_id", ValueStatus.INFERRED, provenance_ids=[pv], note="from the beam layer name")
    _assume_section(run, target, size_key, wd)


def _add_column(run, building, no, j1, j2, rng, size_key, sec, wd, coords, plan, level_at, level_id, column_layers,
                groups, drift):
    lo_tag, hi_tag = rng
    if lo_tag not in level_id or hi_tag not in level_id:
        raise ValidationError(f"column level range {rng!r} is not made of legacy levels")
    lo, hi = level_id[lo_tag], level_id[hi_tag]
    for j in (j1, j2):
        if j not in coords:
            raise ValidationError(f"joint {j} has no coordinates")
    if level_at(float(coords[j1][1])) != lo or level_at(float(coords[j2][1])) != hi:
        raise ValidationError(f"joints {j1}/{j2} are not at the elevations of levels {lo_tag!r}/{hi_tag!r}")
    location = plan(j1)  # lower joint = the drawn column centroid for the lowest range, else its snapped position
    offset = location.distance_to(plan(j2))
    col_id = f"C{no}"
    building.add_element(Column(col_id, lo, hi, location, sec))
    groups.setdefault((lo, hi, size_key, wd), []).append(col_id)
    if offset > POSITION_TOL_MM:
        drift.setdefault((lo, hi), []).append((col_id, offset))
    target = Target.element(col_id)
    pv = run.prov(target, method="centroid of the column outline, possibly snapped to a beam joint",
                  source_id=f"member {no} (joints {j1}-{j2})", layer=column_layers.get((lo_tag, hi_tag)),
                  entity_type="column outline", context=f"levels {lo_tag} to {hi_tag}",
                  coordinates=[tuple(float(v) for v in coords[j1]), tuple(float(v) for v in coords[j2])],
                  frame=FRAME_JOINT,
                  note="position is the outline centroid, possibly snapped to a beam joint; "
                       "section is ASSUMED (legacy default), the drawn outline size was not kept")
    run.status(target, "geometry", ValueStatus.INFERRED, provenance_ids=[pv], note="outline centroid, not a drawn point")
    for level_field in ("lower_level_id", "upper_level_id"):
        run.status(target, level_field, ValueStatus.INFERRED, provenance_ids=[pv], note="from the column layer name")
    _assume_section(run, target, size_key, wd)


def _assume_section(run, target, size_key, wd):
    """The parser never reads member sizes from the drawing: they come from its default table."""
    pv = run.prov(target, field="section", method="legacy default size table", source_id=f"DEFAULT_SIZES[{size_key}]",
                  context="ga_dxf_parser default member sizes", from_file=False,
                  note=f"{wd[0]}x{wd[1]} mm is a placeholder, not read from the drawing")
    run.status(target, "section", ValueStatus.ASSUMED, provenance_ids=[pv], note="legacy default")


def _report_groups(run, building, beams_by_group, columns_by_group, drift):
    for (lid, size_key, (w, d)), ids in sorted(beams_by_group.items(), key=lambda kv: (building.get_level(kv[0][0]).index, kv[0][1])):
        run.issue(IssueSeverity.WARNING, IssueCategory.INCOMPLETE_INFORMATION,
                  _msg(f"{len(ids)} beam(s) on level {building.get_level(lid).name} ({_preview(ids)}) carry the "
                       f"section {w}x{d} mm from the legacy default size table ({size_key}); it was not read from the "
                       "drawing.", source=f"{SYSTEM} DEFAULT_SIZES", missing="the real beam sections",
                       action_required=True), Target.level(lid))
    for (lo, hi, size_key, (w, d)), ids in sorted(columns_by_group.items(),
                                                          key=lambda kv: (building.get_level(kv[0][0]).index, building.get_level(kv[0][1]).index, kv[0][2])):
        run.issue(IssueSeverity.WARNING, IssueCategory.INCOMPLETE_INFORMATION,
                  _msg(f"{len(ids)} column(s) from {lo} to {hi} ({_preview(ids)}) carry the section {w}x{d} mm from "
                       f"the legacy default size table ({size_key}); the drawn outline size was discarded by the "
                       "parser.", source=f"{SYSTEM} DEFAULT_SIZES", missing="the real column sections",
                       action_required=True), Target.level(lo))
    for (lo, hi), items in sorted(drift.items(), key=lambda kv: (building.get_level(kv[0][0]).index, building.get_level(kv[0][1]).index)):
        worst = max(o for _, o in items)
        run.issue(IssueSeverity.WARNING, IssueCategory.AMBIGUOUS_GEOMETRY,
                  _msg(f"{len(items)} column(s) between {lo} and {hi} ({_preview(c for c, _ in items)}) have upper and "
                       f"lower joints up to {worst:.1f} mm apart in plan (snapping); they are modelled vertical at the "
                       "lower position.", source=f"{SYSTEM} column joints", missing="the exact column position",
                       action_required=False), Target.level(lo))


def _report_unconnected_columns(run, building):
    """The parser makes one beam member per polyline run, splitting only at drawn vertices. A run can
    therefore pass over columns without a joint there, and those columns end up not connected to it.
    Reported, not repaired: splitting beams is a modelling decision for the engineer or a later phase."""
    beams = building.elements_of(ElementKind.BEAM)
    ends = {}
    for bm in beams:
        ends.setdefault(bm.level_id, []).extend(
            building.get_node(n).location for n in (bm.start_node_id, bm.end_node_id))
    found = []
    for col in building.elements_of(ElementKind.COLUMN):
        for lid in dict.fromkeys(col.level_ids):
            if lid not in ends or any(col.location.distance_to(p) <= END_NODE_TOL_MM for p in ends[lid]):
                continue
            for bm in beams:
                if bm.level_id == lid and _on_interior(col.location, building.get_node(bm.start_node_id).location,
                                                       building.get_node(bm.end_node_id).location):
                    found.append((col, bm))
    for col, bm in sorted(found, key=lambda cb: (_num_key(cb[1].id), _num_key(cb[0].id))):
        col_target, beam_target = Target.element(col.id), Target.element(bm.id)
        run.issue(IssueSeverity.BLOCKING, IssueCategory.UNSUPPORTED_BEAM,
                  _msg(f"Column {col.id} terminates on the interior of Beam {bm.id} "
                       f"({building.member_length_mm(bm.id) / 1000:.1f} m), but the source beam geometry contains no "
                       "corresponding structural node: the beam runs over the column with no node at that point, so "
                       "the imported connectivity does not connect them. The legacy parser splits beams only at drawn "
                       "polyline vertices and its STAAD model has the same gap. The beam was not split.",
                       source=f"{SYSTEM} member/joint connectivity",
                       missing="a node where the beam crosses the column, or the engineer's ruling that they are "
                               "not connected", action_required=True),
                  beam_target, related=(col_target,),
                  evidence=[e for e in (run.prov_id(beam_target), run.prov_id(col_target)) if e], structural=True)


def _on_interior(pt, a, b) -> bool:
    dx, dy = b.x_mm - a.x_mm, b.y_mm - a.y_mm
    length = math.hypot(dx, dy)
    if length == 0:
        return False
    along = ((pt.x_mm - a.x_mm) * dx + (pt.y_mm - a.y_mm) * dy) / length
    across = abs(dx * (pt.y_mm - a.y_mm) - dy * (pt.x_mm - a.x_mm)) / length
    return across <= INTERIOR_TOL_MM and END_NODE_TOL_MM < along < length - END_NODE_TOL_MM


def _edge_supports(p, q, beams) -> set:
    """Beams collinear with edge p-q (within SUPPORT_TOL_MM) that overlap it along its length."""
    dx, dy = q.x_mm - p.x_mm, q.y_mm - p.y_mm
    length = math.hypot(dx, dy)
    if length == 0:
        return set()
    found = set()
    for beam_id, (a, b) in beams.items():
        def off(pt):
            return abs(dx * (pt.y_mm - p.y_mm) - dy * (pt.x_mm - p.x_mm)) / length
        if off(a) > SUPPORT_TOL_MM or off(b) > SUPPORT_TOL_MM:
            continue
        ta = ((a.x_mm - p.x_mm) * dx + (a.y_mm - p.y_mm) * dy) / length
        tb = ((b.x_mm - p.x_mm) * dx + (b.y_mm - p.y_mm) * dy) / length
        if min(max(ta, tb), length) - max(min(ta, tb), 0.0) > SUPPORT_TOL_MM:
            found.add(beam_id)
    return found


def _build_slabs(run, building, parse_result, tags, level_id, beam_layers, beam_ends, engineer_loading):
    model = parse_result["model"]
    loading = parse_result.get("loading") or {}
    panels_by_level = model.get("slab_panels") or {}
    for tag in tags[1:]:
        lid = level_id[tag]
        level_beams = {b: ends for b, ends in beam_ends.items() if building.get_element(b).level_id == lid}
        panels = sorted(panels_by_level.get(tag, ()),
                        key=lambda p: (round(float(p["centroid"][0]), 3), round(float(p["centroid"][1]), 3)))
        thickness = (loading.get(tag) or {}).get("slab_thickness_mm")
        given = bool(engineer_loading and "slab_thickness_mm" in (engineer_loading.get(tag) or {}))
        thickness_decision = None
        if panels and not thickness:
            run.issue(IssueSeverity.ERROR, IssueCategory.INCOMPLETE_INFORMATION,
                      _msg(f"{len(panels)} slab panel(s) on level {tag} were not imported: no slab thickness.",
                           source=f"{SYSTEM} loading", missing="slab thickness", action_required=True),
                      Target.level(lid))
            continue
        made, triangles, seen = [], 0, set()
        for k, panel in enumerate(panels, start=1):
            verts = [Point2D(round(float(x) * M_TO_MM, 3), round(float(y) * M_TO_MM, 3)) for x, y in panel["vertices"]]
            key = frozenset((v.x_mm, v.y_mm) for v in verts)
            if key in seen:
                run.issue(IssueSeverity.WARNING, IssueCategory.AMBIGUOUS_GEOMETRY,
                          _msg(f"Slab panel {k} on level {tag} repeats another panel and was not imported again.",
                               source=f"{SYSTEM} slab panel", missing="none (redundant geometry)",
                               action_required=False), Target.level(lid))
                continue
            seen.add(key)
            slab_id = _safe_id(f"S{tag}-{k}")
            try:
                edges = [(verts[i], verts[(i + 1) % len(verts)]) for i in range(len(verts))]
                supports = set().union(*(_edge_supports(p, q, level_beams) for p, q in edges))
                building.add_element(Slab(slab_id, lid, Polygon2D(tuple(verts)), thickness,
                                          supported_by=sorted(supports, key=_num_key)))
            except ValidationError as exc:
                run.issue(IssueSeverity.ERROR, IssueCategory.AMBIGUOUS_GEOMETRY,
                          _msg(f"Slab panel {k} on level {tag} was rejected by the core model: {exc}",
                               source=f"{SYSTEM} slab panel", missing="a valid slab boundary", action_required=True),
                          Target.level(lid))
                continue
            made.append(slab_id)
            triangles += len(verts) == 3
            target = Target.element(slab_id)
            pv = run.prov(target, method="region enclosed by beam centrelines (shapely polygonize)",
                          source_id=f"level {tag} panel {k}", layer=beam_layers.get(tag),
                          entity_type="slab panel", context=f"level {tag}",
                          coordinates=[(float(x), float(y)) for x, y in panel["vertices"]], frame=FRAME_PLAN,
                          note="boundary and supported_by are inferred from beam geometry; thickness is "
                               + ("ENGINEER_INPUT" if given else "ASSUMED (legacy default)"))
            run.status(target, "geometry", ValueStatus.INFERRED, provenance_ids=[pv])
            run.status(target, "supported_by", ValueStatus.INFERRED, provenance_ids=[pv],
                       note="beams collinear with the panel edges")
            if given:
                thickness_decision = thickness_decision or run.engineer_input(
                    Target.level(lid), DecisionCategory.SECTION_SIZING,
                    f"Slab thickness for level {tag} supplied by the engineer at import: {thickness:g} mm.")
                run.status(target, "thickness_mm", ValueStatus.ENGINEER_DEFINED, decision_id=thickness_decision)
            else:
                tpv = run.prov(target, field="thickness_mm", method="legacy default slab thickness",
                               source_id="SLAB_THICKNESS_MM", context="ga_dxf_parser default loading", from_file=False,
                               note=f"{thickness:g} mm is a placeholder; the engineer did not supply one")
                run.status(target, "thickness_mm", ValueStatus.ASSUMED, provenance_ids=[tpv], note="legacy default")
        if made and not given:
            run.issue(IssueSeverity.WARNING, IssueCategory.INCOMPLETE_INFORMATION,
                      _msg(f"{len(made)} slab(s) on level {tag} ({_preview(made)}) use thickness {thickness} mm, the "
                           "legacy default; the engineer did not supply one.", source=f"{SYSTEM} SLAB_THICKNESS_MM",
                           missing="the real slab thickness", action_required=True), Target.level(lid))
        if triangles:
            run.issue(IssueSeverity.WARNING, IssueCategory.AMBIGUOUS_GEOMETRY,
                      _msg(f"{triangles} slab panel(s) on level {tag} are triangles. The parser splits non-rectangular "
                           "slabs into triangles for STAAD, so these may be pieces of one slab; the original outline "
                           "cannot be recovered from the parser output.", source=f"{SYSTEM} slab panels",
                           missing="the original slab outline", action_required=True), Target.level(lid))


def _report_voids(run, parse_result, tags, level_id):
    for tag in tags[1:]:
        for x, z in (parse_result.get("void_centroids") or {}).get(tag, ()):
            run.issue(IssueSeverity.WARNING, IssueCategory.INCOMPLETE_INFORMATION,
                      _msg(f"A VOID marker at plan ({float(x) * M_TO_MM:.0f}, {float(z) * M_TO_MM:.0f}) mm was applied "
                           f"to level {tag} by the parser (it applies every VOID marker to every floor) and excluded "
                           "any slab panel containing it. No Opening was created.", source=f"{SYSTEM} VOID layer",
                           missing="the void outline and the floors it really passes through",
                           action_required=True), Target.level(level_id[tag]))


# ------------------------------------------------------------------ design basis and notes

def _attach_design_basis(run: _Run, design_basis: Optional[DesignBasis]):
    if design_basis is None:
        run.issue(IssueSeverity.WARNING, IssueCategory.INCOMPLETE_INFORMATION,
                  _msg("No design basis is attached. Elements carry no material, and design code, material grades, "
                       "cover, loading, exposure, fire, wind and seismic assumptions are undefined.",
                       source="caller (none supplied)", missing="the project design basis",
                       action_required=True))
        return
    run.project.set_design_basis(design_basis)
    open_items = design_basis.missing_items()
    if open_items:
        run.issue(IssueSeverity.INFO, IssueCategory.INCOMPLETE_INFORMATION,
                  _msg(f"The supplied design basis is incomplete: {', '.join(open_items)}.", source="caller",
                       missing=", ".join(open_items), action_required=True))


def _general_notes(run: _Run, level_names):
    run.issue(IssueSeverity.INFO, IssueCategory.INCOMPLETE_INFORMATION,
              _msg("The legacy GA parser does not extract grid lines or grid labels, level titles, column outline "
                   "dimensions or rotation, text annotations, or DXF entity identity, so none of these are in the "
                   "model, even if the drawing contains them.", source=f"{SYSTEM} scope",
                   missing="grid, level titles, column sizes, annotations, entity handles", action_required=False))
    run.issue(IssueSeverity.INFO, IssueCategory.AMBIGUOUS_GEOMETRY,
              _msg("Plan coordinates are the DXF sheet coordinates of the reference floor (millimetres), with other "
                   "floors translated onto it by the parser. They are not measured from a structural grid origin.",
                   source=f"{SYSTEM} floor alignment", missing="a grid-based origin", action_required=False))
    if not level_names:
        run.issue(IssueSeverity.INFO, IssueCategory.INCOMPLETE_INFORMATION,
                  _msg("Level IDs are the legacy layer tags and level names are generated from them.",
                       source=f"{SYSTEM} layer names", missing="real level names", action_required=False))
