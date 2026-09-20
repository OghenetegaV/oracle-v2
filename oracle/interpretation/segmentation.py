"""Oracle — Spatial View Segmentation and Classification

Purpose:
    Splits a drawing's entities into candidate VIEWS and says what each appears to be. It finds sheet
    frames (large closed rectangles enclosing many entities), clusters the remaining geometry into regions
    by spatial connectivity at a small tolerance, attaches small satellites to the region they belong to,
    associates title text to regions by proximity (preferring text below a view), and scores each region as
    floor plan / section / elevation / detail / schedule / notes / legend from THREE independent kinds of
    evidence: the title text, the semantic classes of the layers its geometry sits on, and its geometry
    (walls, doors, level tags, dimension orientation, text density). It also estimates a region's rotation
    from its dominant line direction.

Role in Oracle:
    Stage 4 of the architectural pipeline: CAD entity -> drawing region -> view -> view type. It does not
    group by layer and does not assume one floor per file: on the real sample it recovers about 30 views
    from 24,000 entities spread over 20 sheets. Every score is kept with its evidence so the result can be
    reviewed; a close call becomes alternatives, not a silent choice. Classification is a proposal.

Dependencies:
    oracle.core (ViewType); oracle.ingestion; oracle.interpretation.naming, .layers; standard library.

Consumers:
    oracle.interpretation.pipeline; tests.

Status:
    Interpretation (Phase 3).

Migration/Notes:
    Thresholds are relative to the drawing's own size, not to any unit. Paper-space layouts are not
    segmented yet. A view drawn in disconnected fragments farther apart than the tolerance is split; the
    engineer merges such views (project.merge_views).
"""

from __future__ import annotations

import math
import statistics
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from oracle.core import ViewType

from .naming import LevelNamer, TitleInfo, classify_title

_TEXT_KINDS = ("TEXT",)


@dataclass
class Region:
    box: tuple
    geometry: list                                  # DrawnEntity (not text or dimension)
    texts: list = field(default_factory=list)       # text inside the region
    dims: list = field(default_factory=list)
    title: Optional[object] = None                  # the DrawnEntity chosen as its title
    title_info: Optional[TitleInfo] = None
    other_titles: list = field(default_factory=list)
    frame: Optional[object] = None                  # the sheet frame entity it sits inside, if any
    scores: dict = field(default_factory=dict)      # ViewType -> score
    evidence: list = field(default_factory=list)    # (kind, description, weight)
    view_type: ViewType = ViewType.UNKNOWN
    confidence: float = 0.0
    alternatives: list = field(default_factory=list)    # [(ViewType, confidence)] when the call is close
    rotation_deg: float = 0.0

    @property
    def width(self) -> float:
        return self.box[2] - self.box[0]

    @property
    def depth(self) -> float:
        return self.box[3] - self.box[1]

    def entity_ids(self) -> list:
        return [e.id for e in self.geometry + self.texts + self.dims]


def _union_box(boxes):
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def _gap(a, b) -> float:
    dx = max(a[0] - b[2], b[0] - a[2], 0.0)
    dy = max(a[1] - b[3], b[1] - a[3], 0.0)
    return math.hypot(dx, dy)


def _inflate(b, m):
    return (b[0] - m, b[1] - m, b[2] + m, b[3] + m)


def _point_box_gap(p, b) -> float:
    return _gap((p[0], p[1], p[0], p[1]), b)


def _contains(box, p, margin=0.0) -> bool:
    return box[0] - margin <= p[0] <= box[2] + margin and box[1] - margin <= p[1] <= box[3] + margin


def _components(boxes: list, eps: float) -> list:
    """Groups of box indices that touch or overlap once each box is grown by eps (sweep line + union-find)."""
    n = len(boxes)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    active: list = []
    for i in sorted(range(n), key=lambda k: boxes[k][0]):
        b = boxes[i]
        active = [j for j in active if boxes[j][2] + eps >= b[0]]
        for j in active:
            c = boxes[j]
            if b[1] - eps <= c[3] and c[1] - eps <= b[3]:
                a, d = find(i), find(j)
                if a != d:
                    parent[a] = d
        active.append(i)
    groups: dict = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def find_frames(entities: list) -> list:
    """Large axis-aligned closed rectangles that enclose many entities and lie inside no other such rectangle."""
    lengths = [math.hypot(e.points[1][0] - e.points[0][0], e.points[1][1] - e.points[0][1])
               for e in entities if e.kind == "LINE" and len(e.points) == 2]
    median_line = statistics.median(lengths) if lengths else 0.0
    candidates = []
    for e in entities:
        if e.kind != "POLYLINE" or not e.closed or len(e.points) not in (4, 5):
            continue
        xs = {round(p[0], 3) for p in e.points}
        ys = {round(p[1], 3) for p in e.points}
        if len(xs) > 2 or len(ys) > 2 or e.width < 15 * median_line or e.depth < 15 * median_line:
            continue
        inside = sum(1 for o in entities if o is not e and _contains(e.box, o.centre))
        if inside >= 30:
            candidates.append((e, inside))
    frames = []
    for e, _n in sorted(candidates, key=lambda c: -(c[0].width * c[0].depth)):
        if not any(_contains(f.box, e.centre) for f in frames):
            frames.append(e)
    return frames


def segment(entities: list, layer_class: dict, namer: Optional[LevelNamer] = None) -> tuple:
    """(regions, frames, stray_entity_ids). `layer_class` maps a layer name to its semantic class."""
    namer = namer or LevelNamer()
    frames = find_frames(entities)
    frame_ids = {f.id for f in frames}
    geometry = [e for e in entities if e.kind not in ("TEXT", "DIMENSION") and e.id not in frame_ids
                and layer_class.get(e.layer) not in ("non_plotting",)]
    texts = [e for e in entities if e.kind == "TEXT" and e.text]
    dims = [e for e in entities if e.kind == "DIMENSION"]
    if not geometry:
        return [], frames, [e.id for e in texts + dims]
    box_all = _union_box([e.box for e in geometry])
    diag = math.hypot(box_all[2] - box_all[0], box_all[3] - box_all[1])
    eps = max(5e-4 * diag, 1e-9)
    groups = _components([e.box for e in geometry], eps)
    total = len(geometry)
    minimum = max(10, int(0.002 * total))
    significant = [g for g in groups if len(g) >= minimum]
    if not significant:
        significant = [max(groups, key=len)]
    regions = [Region(_union_box([geometry[i].box for i in g]), [geometry[i] for i in g]) for g in significant]
    sig_set = {i for g in significant for i in g}
    stray = []
    for g in groups:
        if g and g[0] in sig_set:
            continue
        members = [geometry[i] for i in g]
        box = _union_box([m.box for m in members])
        near = min(regions, key=lambda r: _gap(box, r.box))
        if _gap(box, near.box) <= 0.04 * max(near.width, near.depth):
            near.geometry.extend(members)
            near.box = _union_box([near.box, box])
        else:
            stray.extend(m.id for m in members)
    # regions whose boxes substantially overlap belong to one view (nested or interleaved parts)
    merged = True
    while merged:
        merged = False
        for i in range(len(regions)):
            for j in range(i + 1, len(regions)):
                a, b = regions[i], regions[j]
                ox = max(0.0, min(a.box[2], b.box[2]) - max(a.box[0], b.box[0]))
                oy = max(0.0, min(a.box[3], b.box[3]) - max(a.box[1], b.box[1]))
                smaller = min(a.width * a.depth, b.width * b.depth)
                if smaller > 0 and ox * oy >= 0.3 * smaller:
                    a.geometry.extend(b.geometry)
                    a.box = _union_box([a.box, b.box])
                    del regions[j]
                    merged = True
                    break
            if merged:
                break
    for r in regions:
        r.frame = next((f for f in frames if _contains(f.box, ((r.box[0] + r.box[2]) / 2, (r.box[1] + r.box[3]) / 2))), None)
    regions.sort(key=lambda r: (-round(r.box[3] / max(diag * 0.02, 1e-9)), r.box[0]))
    unassigned_text = _assign_content(regions, texts, dims, namer)
    for r in regions:
        _score(r, layer_class, namer)
    return regions, frames, stray + [t.id for t in unassigned_text]


def _assign_content(regions: list, texts: list, dims: list, namer: LevelNamer) -> list:
    """Texts and dimensions inside a region belong to it; texts outside are title candidates for nearby regions."""
    heights = [t.height for t in texts if t.height]
    median_h = statistics.median(heights) if heights else 0.0
    for d in dims:
        home = next((r for r in regions if _contains(r.box, d.centre, 0.02 * max(r.width, r.depth))), None)
        if home is None and regions:
            near = min(regions, key=lambda r: _point_box_gap(d.centre, r.box))
            home = near if _point_box_gap(d.centre, near.box) <= 0.1 * max(near.width, near.depth) else None
        if home is not None:
            home.dims.append(d)
    outside, inside_texts = [], []
    for t in texts:
        home = next((r for r in regions if _contains(r.box, t.centre)), None)
        if home is not None:
            home.texts.append(t)
            inside_texts.append((t, home))
        else:
            outside.append(t)
    # A title is a short text. It normally sits outside its view; text inside one only counts when it is much
    # larger than the drawing's ordinary text (a sentence of notes that happens to say "detail" is not a title).
    candidates = [(t, None) for t in outside] + [(t, home) for t, home in inside_texts
                                                 if (t.height or 0) >= 2.0 * median_h and median_h > 0]
    pairs = []
    for t, home in candidates:
        if len(t.text.split()) > 8 or len(t.text) > 60:
            continue
        info = classify_title(t.text, namer)
        if info.view_type is None:
            continue
        strength = 1.0 if (t.height or 0) >= 1.4 * median_h else 0.7
        for r in ([home] if home is not None else regions):
            d = _point_box_gap(t.centre, r.box)
            reach = max(0.3 * max(r.width, r.depth), 6 * (t.height or 0))
            if home is not None:
                d = 0.1 * max(r.width, r.depth)
            elif d > reach:
                continue
            below = t.centre[1] < r.box[1]
            above = t.centre[1] > r.box[3]
            factor = 1.0 if below else 1.3 if above else 1.6 if home is not None else 1.2
            pairs.append((d * factor / strength, id(r), r, t, info))
    used_texts, titled = set(), set()
    for _d, _rid, r, t, info in sorted(pairs, key=lambda p: (p[0], p[3].id)):
        if t.id in used_texts:
            continue
        if id(r) in titled:
            r.other_titles.append((t, info))
            used_texts.add(t.id)
            continue
        r.title, r.title_info = t, info
        titled.add(id(r))
        used_texts.add(t.id)
    return [t for t in outside if t.id not in used_texts]


def _score(r: Region, layer_class: dict, namer: LevelNamer) -> None:
    classes = Counter(layer_class.get(e.layer, "unknown") for e in r.geometry)
    n = len(r.geometry)
    walls = sum(v for k, v in classes.items() if k.startswith("wall") and not k.endswith(("_pattern", "_label")))
    doors_windows = sum(v for k, v in classes.items() if k in ("door", "window"))
    stairs = classes.get("stair", 0)
    level_texts = sum(1 for t in r.texts if layer_class.get(t.layer) == "level_marker"
                      or (namer.parse(t.text) and len(t.text) < 30))
    vertical = sum(1 for d in r.dims if abs(math.sin(math.radians(d.rotation))) > 0.9)
    horizontal = sum(1 for d in r.dims if abs(math.cos(math.radians(d.rotation))) > 0.9)
    text_share = len(r.texts) / max(1, len(r.texts) + n)
    s = {t: 0.0 for t in ViewType}
    ev = r.evidence
    if r.title_info and r.title_info.view_type:
        s[r.title_info.view_type] += 0.65 * (r.title_info.confidence / 0.9)
        ev.append(("title_text", f"Text {r.title.text!r} announces a {r.title_info.view_type.value}"
                                 + (f" ({r.title_info.method} match)" if r.title_info.method == "fuzzy" else ""),
                   round(0.65 * r.title_info.confidence / 0.9, 2)))
    if n and (walls >= 20 or walls / n >= 0.2):
        s[ViewType.FLOOR_PLAN] += 0.25
        ev.append(("layer_class", f"{walls} entities lie on wall-class layers", 0.25))
    if doors_windows >= 3:
        s[ViewType.FLOOR_PLAN] += 0.05
        ev.append(("layer_class", f"{doors_windows} door/window entities", 0.05))
    if vertical and horizontal and min(vertical, horizontal) / (vertical + horizontal) >= 0.2:
        s[ViewType.FLOOR_PLAN] += 0.05
        ev.append(("dimensions", f"{horizontal} horizontal and {vertical} vertical dimensions, as in a plan", 0.05))
    if level_texts >= 4:
        s[ViewType.SECTION] += 0.2
        s[ViewType.ELEVATION] += 0.15
        ev.append(("level_tags", f"{level_texts} level-tag texts, as in a section or elevation", 0.2))
    if r.dims and vertical / len(r.dims) >= 0.6 and level_texts >= 4:
        s[ViewType.SECTION] += 0.15
        ev.append(("dimensions", "dimensions are mostly vertical, as in a section", 0.15))
    if stairs and r.title_info is None:
        s[ViewType.DETAIL] += 0.1
    if text_share >= 0.6 and len(r.texts) >= 8:
        s[ViewType.SCHEDULE] += 0.35
        s[ViewType.NOTES] += 0.2
        ev.append(("geometry", f"{text_share:.0%} of the region is text, as in a schedule or notes", 0.35))
    ranked = sorted(((v, t) for t, v in s.items() if v > 0), key=lambda x: -x[0])
    r.scores = {t: round(v, 3) for v, t in ranked}
    if not ranked or ranked[0][0] < 0.3:
        r.view_type, r.confidence = ViewType.UNKNOWN, round(min(0.3, ranked[0][0] if ranked else 0.0), 2)
        if ranked:
            r.alternatives = [(t, round(min(0.95, v), 2)) for v, t in ranked[:3]]
        return
    top, second = ranked[0], (ranked[1] if len(ranked) > 1 else (0.0, None))
    r.view_type = top[1]
    r.confidence = round(max(0.05, min(0.98, top[0] - 0.5 * second[0] + 0.03)), 2)
    if second[1] is not None and second[0] >= 0.25 and top[0] - second[0] < 0.2:
        r.alternatives = [(t, round(min(0.95, v), 2)) for v, t in ranked[:3]]


def dominant_rotation(entities: list, layer_class: dict) -> tuple:
    """(degrees in (-45, 45], share of line length that supports it). 0 for an axis-aligned drawing."""
    lines = [e for e in entities if e.kind == "LINE" and len(e.points) == 2]
    walls = [e for e in lines if str(layer_class.get(e.layer, "")).startswith("wall")]
    lines = walls if len(walls) >= 8 else lines
    bins: Counter = Counter()
    total = 0.0
    for e in lines:
        dx, dy = e.points[1][0] - e.points[0][0], e.points[1][1] - e.points[0][1]
        length = math.hypot(dx, dy)
        if length <= 0:
            continue
        angle = math.degrees(math.atan2(dy, dx)) % 90.0
        bins[round(angle * 2) % 180] += length
        total += length
    if not total:
        return 0.0, 0.0
    peak = max(bins, key=bins.get)
    support = sum(v for k, v in bins.items() if min(abs(k - peak), 180 - abs(k - peak)) <= 2) / total
    angle = peak / 2.0
    return (angle - 90.0 if angle > 45.0 else angle), support
