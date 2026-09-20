"""Oracle — Cross-View Reconciliation

Purpose:
    Compares what the different views of one drawing say, and records agreement or disagreement instead of
    choosing. From the floor plans (which levels have a plan), the sections and elevations (which levels are
    tagged, at what elevations, how many horizontal bands), and the height evidence, it answers: how many
    levels are there and are they the same ones; are any level names repeated with different elevations; do
    the storey heights agree between sources; do two views claim the same level; does the drawing's own
    sheet list match the views found. A disagreement becomes an INTERPRETATION SET with confidence-ranked
    alternatives and an engineering issue asking for review; agreement is recorded as a finding.

Role in Oracle:
    Stage 8, the piece that stops Oracle silently picking one drawing over another. On the real sample it
    finds that the plans show three levels (ground, first, roof) while every section and elevation carries
    five tagged levels (a second "FIRST FLOOR" at 4350 and a second "ROOF" at 7950): the count question and
    the "one level or two?" questions are raised for the engineer. It reads no geometry; it works on the
    results of earlier stages and returns plain specifications the pipeline turns into core objects.

Dependencies:
    oracle.core (ViewType, IssueSeverity, IssueCategory); oracle.interpretation.naming; standard library.

Consumers:
    oracle.interpretation.pipeline; tests.

Status:
    Interpretation (Phase 3).

Migration/Notes:
    Height tolerance is max(50 mm, 2%). Confidences of alternatives are the mean confidence of the evidence
    behind each; they are not calibrated probabilities.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Optional

from oracle.core import Effect, IssueCategory, IssueSeverity, ViewType

from .naming import LevelNamer, classify_title, level_sort_key


@dataclass
class PendingSet:
    question: str
    alternatives: list                    # (meaning, confidence, rationale)
    subject_views: list = field(default_factory=list)
    key: str = ""


@dataclass
class PendingIssue:
    severity: IssueSeverity
    category: IssueCategory
    message: str
    view_ids: list = field(default_factory=list)
    set_key: Optional[str] = None            # the PendingSet this issue is the visible face of


@dataclass
class PendingFinding:
    question: str
    agreement: str
    summary: str
    view_ids: list = field(default_factory=list)
    set_key: Optional[str] = None


@dataclass
class Reconciliation:
    findings: list = field(default_factory=list)
    sets: list = field(default_factory=list)
    issues: list = field(default_factory=list)


def _tol(value: float) -> float:
    return max(50.0, 0.02 * value)


def _cluster(values: list) -> list:
    groups: list = []
    for v in sorted(values):
        if groups and abs(v - statistics.mean(groups[-1])) <= _tol(v):
            groups[-1].append(v)
        else:
            groups.append([v])
    return groups


def reconcile(plans: list, verticals: list, height_rows: list, sheet_titles: list, all_views: list,
              namer: LevelNamer) -> Reconciliation:
    """plans: [(view_id, level_key|None, variant, title, confidence)] for floor-plan views;
    verticals: [(view_id, view_type, tags, band_count)] for sections and elevations, where tags is
    [(level_key, elevation_mm|None)]; height_rows: [(from, to, height_mm, source, view_id)];
    sheet_titles: titles from the drawing's own sheet list; all_views: [(view_id, view_type, title, level_key,
    variant, label, orientation)]."""
    out = Reconciliation()
    _plan_levels(out, plans)
    _level_counts(out, plans, verticals)
    _repeated_tags(out, verticals)
    _heights(out, height_rows)
    _duplicate_titles(out, all_views)
    _sheet_list(out, sheet_titles, all_views, namer)
    return out


def _primary(plans):
    return [p for p in plans if p[2] is None]


def _plan_levels(out: Reconciliation, plans: list) -> None:
    primary = _primary(plans)
    unnamed = [p for p in primary if p[1] is None]
    for p in unnamed:
        out.issues.append(PendingIssue(IssueSeverity.WARNING, IssueCategory.INCOMPLETE_INFORMATION,
                                       f"Floor plan view {p[0]} ({p[3] or 'untitled'}) does not name its level, so it cannot "
                                       "be placed in the building until the engineer says which level it is.", [p[0]]))
    by_level: dict = {}
    for p in primary:
        if p[1] is not None:
            by_level.setdefault(p[1], []).append(p)
    for key, rows in sorted(by_level.items(), key=lambda kv: level_sort_key(kv[0])):
        if len(rows) > 1:
            meaning = lambda r: f"{r[0]} is the {key} plan"
            out.sets.append(PendingSet(
                f"{len(rows)} plan views ({', '.join(r[0] for r in rows)}) each claim to be the {key} plan. Are they "
                "duplicates, revisions, or is one mislabelled?",
                [(f"all_are_{key.lower().replace(':', '_')}_plan", 0.6, "the titles all name the same level; treated as copies "
                                                                       "or revisions of one plan",
                  [Effect.acknowledge("The plans are copies or revisions of one another.")]),
                 ("one_is_mislabelled", 0.4, "two primary plans of one level would normally not both be drawn",
                  [Effect.acknowledge("One of the plans is mislabelled; the engineer corrects its level separately.")])],
                [r[0] for r in rows], key=f"duplicate_plan_{key}"))
            out.issues.append(PendingIssue(IssueSeverity.WARNING, IssueCategory.AMBIGUOUS_GEOMETRY,
                                           f"{len(rows)} plan views claim the {key} level: {', '.join(r[0] for r in rows)}.",
                                           [r[0] for r in rows], f"duplicate_plan_{key}"))
    ordinals = sorted(int(k.split(":")[1]) for k in by_level if k.startswith("FLOOR:"))
    missing = [n for n in range(1, (ordinals[-1] if ordinals else 0)) if n not in ordinals]
    if missing and ordinals:
        out.issues.append(PendingIssue(IssueSeverity.WARNING, IssueCategory.INCOMPLETE_INFORMATION,
                                       f"There are plans for floors {ordinals} but none for floor(s) {missing}: a plan may "
                                       "be missing from the drawing, or those floors do not exist.", []))


def _level_counts(out: Reconciliation, plans: list, verticals: list) -> None:
    primary_levels = sorted({p[1] for p in _primary(plans) if p[1]}, key=level_sort_key)
    if not primary_levels and not verticals:
        return
    hypotheses = []
    if primary_levels:
        conf = statistics.mean(p[4] for p in _primary(plans) if p[1]) if any(p[1] for p in _primary(plans)) else 0.5
        hypotheses.append(("floor_plans", primary_levels, round(conf, 2),
                           f"{len(primary_levels)} levels have a plan", [p[0] for p in _primary(plans)]))
    section_tags = [v for v in verticals if v[1] == ViewType.SECTION and v[2]]
    elev_tags = [v for v in verticals if v[1] == ViewType.ELEVATION and v[2]]
    for name, group, base in (("sections", section_tags, 0.8), ("elevations", elev_tags, 0.6)):
        if not group:
            continue
        counts: dict = {}
        for view_id, _t, tags, _b in group:
            key = tuple(sorted(k for k, _e in tags if k != "DATUM"))
            counts.setdefault(key, []).append(view_id)
        key, view_ids = max(counts.items(), key=lambda kv: len(kv[1]))
        tag_entries = sum(1 for k, _e in group[0][2] if k != "DATUM") if group else 0
        hypotheses.append((name, list(key), round(base * len(view_ids) / len(group), 2),
                           f"{len(view_ids)} of {len(group)} {name} carry {len(key)} distinct level names "
                           f"({tag_entries} level tags)", view_ids, tag_entries))
    if len(hypotheses) < 2:
        out.findings.append(PendingFinding("How many levels does the building have?", "insufficient",
                                           "Only one kind of view gives level evidence; nothing to compare it with.",
                                           hypotheses[0][4] if hypotheses else []))
        return
    plan_h = hypotheses[0]
    disagreements = []
    for h in hypotheses[1:]:
        entries = h[5] if len(h) > 5 else len(h[1])
        if set(h[1]) != set(plan_h[1]) or entries != len(plan_h[1]):
            disagreements.append(h)
    views = sorted({v for h in hypotheses for v in h[4]})
    if not disagreements:
        out.findings.append(PendingFinding("How many levels does the building have?", "agree",
                                           f"Plans, sections and elevations agree on {len(plan_h[1])} levels: {plan_h[1]}.", views))
        return
    alts = [(h[0], h[2], f"{h[3]}: {h[1]}", [Effect.acknowledge(f"The levels are those shown by the {h[0]}: {h[1]}.")])
            for h in hypotheses]
    out.sets.append(PendingSet("How many structural levels exist, and which are they?", alts, views, key="level_count"))
    out.findings.append(PendingFinding(
        "How many levels does the building have?", "disagree",
        "; ".join(f"{h[0]}: {h[3]}" for h in hypotheses) + ". These disagree.", views, "level_count"))
    out.issues.append(PendingIssue(IssueSeverity.ERROR, IssueCategory.AMBIGUOUS_GEOMETRY,
                                   "The floor plans and the sections/elevations disagree about the levels: "
                                   + "; ".join(f"{h[0]} show {h[3]}" for h in hypotheses) + ". Engineer review is required "
                                   "before any level is created.", views, "level_count"))


def _repeated_tags(out: Reconciliation, verticals: list) -> None:
    seen: dict = {}
    for view_id, _t, tags, _b in verticals:
        by_key: dict = {}
        for key, elevation in tags:
            if elevation is not None:
                by_key.setdefault(key, set()).add(elevation)
        for key, elevations in by_key.items():
            if len(elevations) > 1:
                seen.setdefault((key, tuple(sorted(elevations))), []).append(view_id)
    for (key, elevations), views in sorted(seen.items()):
        share = len(views) / max(1, len([v for v in verticals if v[2]]))
        two = round(0.4 + 0.3 * share, 2)
        out.sets.append(PendingSet(
            f"The level name {key} appears with {len(elevations)} different elevations {list(elevations)} in "
            f"{len(views)} view(s). Is that one level or {len(elevations)} levels?",
            [(f"{len(elevations)}_distinct_levels", two, "the same pair of tags repeats consistently across views, which suggests "
                                                       "deliberate distinct levels (a mezzanine, gallery or raised part)",
              [Effect.acknowledge(f"{key} is {len(elevations)} distinct levels at {list(elevations)} mm.")]),
             ("one_level_with_a_conflicting_elevation", round(1 - two, 2), "one of the tags may be a drafting error or an "
                                                                          "alternative datum",
              [Effect.acknowledge(f"{key} is one level; the tags {list(elevations)} mm disagree and one is an error.")])],
            views, key=f"repeated_{key}_{'_'.join(str(int(e)) for e in elevations)}"))
        out.issues.append(PendingIssue(IssueSeverity.WARNING, IssueCategory.AMBIGUOUS_GEOMETRY,
                                       f"Level {key} is tagged at {list(elevations)} mm in {len(views)} view(s); the number of "
                                       "levels and the elevation of each need the engineer's decision.", views,
                                       f"repeated_{key}_{'_'.join(str(int(e)) for e in elevations)}"))


def _heights(out: Reconciliation, rows: list) -> None:
    by_pair: dict = {}
    for a, b, h, source, view_id in rows:
        by_pair.setdefault((a, b), []).append((h, source, view_id))
    for (a, b), items in sorted(by_pair.items(), key=lambda kv: level_sort_key(kv[0][0])):
        groups = _cluster([h for h, _s, _v in items])
        views = sorted({v for _h, _s, v in items if v})
        if len(groups) == 1:
            out.findings.append(PendingFinding(f"Height from {a} to {b}", "agree",
                                               f"{len(items)} piece(s) of evidence agree on {statistics.mean(groups[0]):.0f} mm.", views))
            continue
        alts = []
        for g in groups:
            srcs = sorted({s for h, s, _v in items if h in g})
            alts.append((f"{statistics.mean(g):.0f}_mm", round(len(g) / len(items), 2),
                         f"{len(g)} of {len(items)} pieces of evidence, from {', '.join(srcs)}",
                         [Effect.accept_height(a, b, round(statistics.mean(g), 1))]))
        key = f"height_{a}_{b}".replace(":", "_")
        out.sets.append(PendingSet(f"What is the height from {a} to {b}?", alts, views, key=key))
        out.findings.append(PendingFinding(f"Height from {a} to {b}", "disagree",
                                           "The sources give different heights: " + "; ".join(f"{a_[0]}" for a_ in alts) + ".",
                                           views, key))
        out.issues.append(PendingIssue(IssueSeverity.ERROR, IssueCategory.AMBIGUOUS_GEOMETRY,
                                       f"The height from {a} to {b} is given as " + " and ".join(a_[0].replace("_mm", " mm") for a_ in alts)
                                       + " by different sources. Both are kept as evidence; the engineer must decide.", views, key))


def _duplicate_titles(out: Reconciliation, all_views: list) -> None:
    seen: dict = {}
    for view_id, vtype, title, level, variant, label, orientation in all_views:
        if vtype in (ViewType.SECTION, ViewType.ELEVATION, ViewType.DETAIL) and title:
            key = (vtype, " ".join(title.upper().split()))
            seen.setdefault(key, []).append(view_id)
    for (vtype, title), ids in sorted(seen.items(), key=lambda kv: kv[1]):
        if len(ids) > 1 and vtype in (ViewType.SECTION, ViewType.ELEVATION):
            out.issues.append(PendingIssue(IssueSeverity.WARNING, IssueCategory.AMBIGUOUS_GEOMETRY,
                                           f"{len(ids)} {vtype.value} views carry the same title {title!r} ({', '.join(ids)}). One may be "
                                           "a mislabelled neighbour; the engineer should check which is which.", ids))


def _sheet_list(out: Reconciliation, sheet_titles: list, all_views: list, namer: LevelNamer) -> None:
    if not sheet_titles:
        return
    expected = []
    for title in sheet_titles:
        info = classify_title(title, namer)
        if info.view_type in (ViewType.FLOOR_PLAN, ViewType.SECTION, ViewType.ELEVATION) and info.variant != "furniture":
            expected.append((title, info))
    found_titles = {" ".join(v[2].upper().split()) for v in all_views if v[2]}
    missing = [t for t, _i in expected if " ".join(t.upper().split()) not in found_titles
               and not any(t.upper() in f or f in t.upper() for f in found_titles)]
    if missing:
        out.findings.append(PendingFinding("Does every sheet in the sheet list have a matching view?", "disagree",
                                           f"{len(missing)} of {len(expected)} listed plan/section/elevation sheets were not matched "
                                           f"to a detected view: {missing[:8]}.", []))
        out.issues.append(PendingIssue(IssueSeverity.INFO, IssueCategory.INCOMPLETE_INFORMATION,
                                       f"The drawing's sheet list names {len(missing)} plan/section/elevation view(s) Oracle did "
                                       f"not find as separate views: {missing[:8]}. They may be on sheets that were not included, "
                                       "or titled differently.", []))
    else:
        out.findings.append(PendingFinding("Does every sheet in the sheet list have a matching view?", "agree",
                                           f"All {len(expected)} listed plan/section/elevation sheets match a detected view.", []))
