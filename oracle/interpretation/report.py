"""Oracle — Interpretation Report

Purpose:
    Renders an OracleProject's architectural interpretation as plain text for the engineer: the drawing and
    its units, detected views (by type, with level, confidence and review status), levels and height
    evidence, layer classifications, architectural observations and structural hints, cross-view findings,
    open interpretations (ambiguities) with their alternatives, issues by severity, and what stands between
    the project and readiness. It reads the project and never changes it.

Role in Oracle:
    The minimum review surface for Phase 3 (a CLI/report instead of a UI): it shows what Oracle SOURCE-read,
    INFERRED, ASSUMED, and does not know, so the engineer can approve, correct or override. The polished
    interface comes later.

Dependencies:
    oracle.core.

Consumers:
    oracle.interpretation.__main__; tests; the real-drawing report.

Status:
    Interpretation (Phase 3).

Migration/Notes:
    Output is deterministic text; the authoritative record is the project JSON.
"""

from __future__ import annotations

from collections import Counter

from oracle.core import IssueSeverity, OracleProject, ViewType


def render_report(project: OracleProject, *, max_lines_per_section: int = 60) -> str:
    arch = project.architecture
    if arch is None:
        return "No architectural interpretation in this project."
    out = []
    add = out.append
    d = arch.drawing
    add(f"ARCHITECTURAL INTERPRETATION: {d.file}")
    meta = d.source_metadata
    add(f"  source {d.id} revision {d.revision or '(not stated)'}  sha256 {d.sha256[:16]}...  interpretation {d.interpretation_id}  "
        f"entities {d.entity_count}  ")
    u = d.units
    status = project.value_status_of(project_target(d.id), "units")
    add(f"  UNITS: {u.unit} (factor {u.factor_to_mm:g} to mm, confidence {u.confidence:.2f}, {u.method}); "
        f"status {status.status.value if status else 'unrecorded'}; the source declares {d.declared_unit or 'no unit'}; source metadata {dict(meta)}")
    if u.note:
        add(f"    note: {u.note}")
    add("")
    counts = Counter(v.view_type.value for v in arch.views if v.review.value != "superseded")
    add(f"VIEWS: {sum(counts.values())}  " + ", ".join(f"{k} {n}" for k, n in sorted(counts.items())))
    for vt in (ViewType.FLOOR_PLAN, ViewType.SECTION, ViewType.ELEVATION, ViewType.SCHEDULE, ViewType.LEGEND, ViewType.DETAIL,
               ViewType.NOTES, ViewType.UNKNOWN):
        rows = [v for v in arch.views_of(vt)]
        if not rows:
            continue
        add(f"  {vt.value.upper()} ({len(rows)})")
        for v in rows[:max_lines_per_section]:
            extra = " ".join(x for x in (f"level={v.level_key}" if v.level_key else "", f"variant={v.variant}" if v.variant else "",
                                         f"label={v.section_label}" if v.section_label else "",
                                         f"orientation={v.orientation}" if v.orientation else "") if x)
            add(f"    {v.id} conf={v.confidence:.2f} {v.review.value:8} {v.title or '(untitled)'} {extra}")
    add("")
    tags = {}
    for h in arch.heights:
        tags.setdefault((h.from_level, h.to_level), []).append((h.height_mm, h.source))
    add(f"HEIGHT EVIDENCE: {len(arch.heights)} records across {len(tags)} level pairs")
    for (a, c), rows in sorted(tags.items()):
        vals = Counter(f"{h:g} mm ({s})" for h, s in rows)
        add(f"    {a} -> {c}: " + "; ".join(f"{k} x{n}" for k, n in vals.most_common(4)))
    add(f"BUILDING LEVELS ESTABLISHED: {[lv.id + '=' + format(lv.elevation_mm, 'g') for lv in project.building.levels] if project.building else 'none yet (engineer must establish)'}")
    add("")
    add(f"LAYER CLASSIFICATIONS: {len(arch.layers)}")
    for l in sorted(arch.layers, key=lambda l: (-l.entity_count))[:max_lines_per_section]:
        add(f"    {l.name:18} {l.entity_count:6} -> {l.semantic_class:22} {l.confidence:.2f} ({l.method})")
    add("")
    kinds = Counter()
    for o in arch.observations:
        kinds[o.kind] += o.count
    add(f"OBSERVATIONS: {len(arch.observations)} records covering {sum(kinds.values())} items")
    add("    " + ", ".join(f"{k} {n}" for k, n in kinds.most_common(24)))
    hints = [o for o in arch.observations if o.hint]
    add(f"STRUCTURAL HINTS ({len(hints)}; hints are not decisions)")
    hint_counts = Counter((o.hint.value, o.kind) for o in hints)
    for (h, k), n in hint_counts.most_common(15):
        add(f"    {h}: {n} x {k}")
    add("")
    add(f"CROSS-VIEW FINDINGS: {len(arch.findings)}")
    for f in arch.findings[:max_lines_per_section]:
        add(f"    [{f.agreement.upper()}] {f.question} - {f.summary[:200]}")
    add("")
    sets = project.interpretation_sets
    add(f"AMBIGUITIES (interpretation sets): {len(sets)}; open {len(project.open_interpretation_sets())}")
    for s in sets[:max_lines_per_section]:
        add(f"    {s.id} [{s.status.value}] {s.question[:160]}")
        for a in s.ranked():
            add(f"        {a.confidence:.2f} {a.meaning} - {(a.rationale or '')[:120]}")
    add("")
    by = Counter(i.severity.value for i in project.issues)
    add(f"ISSUES: {len(project.issues)}  " + ", ".join(f"{k} {by[k]}" for k in ("blocking", "error", "warning", "info") if by[k]))
    for sev in (IssueSeverity.BLOCKING, IssueSeverity.ERROR, IssueSeverity.WARNING, IssueSeverity.INFO):
        for i in [i for i in project.issues if i.severity == sev][:max_lines_per_section]:
            add(f"    {i.id} {sev.value.upper():8} {i.message[:230]}")
    add("")
    readiness = project.readiness()
    add("READINESS: " + readiness.summary())
    return "\n".join(out)


def project_target(object_id: str):
    from oracle.core import Target
    return Target.architectural(object_id)
