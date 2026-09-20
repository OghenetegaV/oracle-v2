"""Oracle — Vertical Evidence from Sections and Elevations

Purpose:
    Extracts what a section or elevation says about the building's levels: LEVEL TAGS (a level name text
    paired with the elevation number written beside it, e.g. "01- GROUND FLOOR" with "300"), horizontal
    level LINES with their coverage of the view width (a full-width line is a floor; a partial one hints at
    a mezzanine, a void or a double-height space), vertical DIMENSIONS, and, for elevations, the horizontal
    BANDS visible. From these it derives HEIGHT EVIDENCE between levels: a height between two written
    elevations is SOURCE (the text says so), a height taken from drawn line spacing is INFERRED, and a
    vertical dimension that matches a level pair corroborates it.

Role in Oracle:
    Stage 5. It never invents a storey height: with no numbers and no labelled lines it returns nothing,
    and the pipeline raises an issue. It also refuses to trust drawn spacing where the tags say otherwise:
    on the real sample the level-tag stack is a cosmetic column (drawn 1957/1493/1598 apart for elevations
    300/3150/900 apart), so the written numbers are used and the disagreement is reported, not "resolved".
    An elevation is another evidence source, never treated as a floor plan.

Dependencies:
    oracle.interpretation.naming; oracle.interpretation.segmentation (Region); standard library.

Consumers:
    oracle.interpretation.pipeline, .reconcile; tests.

Status:
    Interpretation (Phase 3).

Migration/Notes:
    Level-tag pairing assumes the number sits within a few text heights of the name. Elevation numbers are
    read as millimetres unless written with a decimal point below 100 (metres) or with an m/mm unit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from .naming import LevelNamer, level_sort_key, parse_elevation_mm

_FLOOR_COVERAGE = 0.7          # a level line covering at least this share of the view width is a full floor
_PARTIAL_COVERAGE = 0.15       # ... and one covering less than this is not counted as a level line at all


@dataclass
class LevelTag:
    key: str
    name_text: object                    # DrawnEntity
    number_text: Optional[object]        # DrawnEntity or None
    elevation_mm: Optional[float]
    confidence: float
    label: str


@dataclass
class LevelLine:
    y: float
    coverage: float
    layer: str
    entity_ids: list
    label_key: Optional[str] = None
    label_text: Optional[str] = None


@dataclass
class VerticalFindings:
    tags: list = field(default_factory=list)
    lines: list = field(default_factory=list)          # LevelLine, bottom to top
    heights: list = field(default_factory=list)        # (from_key, to_key, height_mm, source, basis, confidence)
    vertical_dims_mm: list = field(default_factory=list)   # (value_mm, y_low, y_high)
    partial_lines: list = field(default_factory=list)
    tag_geometry_consistent: Optional[bool] = None      # do drawn spacings follow the written elevations?
    notes: list = field(default_factory=list)


def extract_level_tags(texts: list, namer: LevelNamer) -> list:
    """Pair each level-name text with the elevation-number text on the same row (one to one). A number sits at
    about the name's height, to its right or just below; vertical alignment matters more than distance."""
    names = []
    for t in texts:
        if len(t.text) > 40:
            continue
        level = namer.parse(t.text)
        if level is not None:
            names.append((t, level))
    numbers = [(t, parse_elevation_mm(t.text)) for t in texts if parse_elevation_mm(t.text) is not None and len(t.text) <= 10]
    candidates = []
    for i, (nt, _level) in enumerate(names):
        h = nt.height or 0.0
        for j, (tt, _value) in enumerate(numbers):
            vx, vy = tt.centre[0] - nt.centre[0], tt.centre[1] - nt.centre[1]
            if h > 0 and abs(vy) <= 3.0 * h and abs(vx) <= 25.0 * h:
                candidates.append((i, j, vx, vy, h, tt.layer == nt.layer))
    # Tags of one kind are drawn with the same name-to-number offset. Find the commonest offset in this view and
    # prefer pairs that match it: on a tightly packed tag stack the nearest number is often the neighbour's.
    votes: dict = {}
    for i, j, vx, vy, h, same in candidates:
        if same:
            key = (round(vx / (0.5 * h)), round(vy / (0.5 * h)))
            votes.setdefault(key, []).append((vx, vy))
    dominant = None
    if votes:
        best = max(votes.values(), key=len)
        dominant = (sum(v[0] for v in best) / len(best), sum(v[1] for v in best) / len(best))
    pairs = []
    for i, j, vx, vy, h, same in candidates:
        miss = math.hypot(vx - dominant[0], vy - dominant[1]) if dominant else abs(vy) + 0.1 * abs(vx)
        pairs.append((miss + (0.0 if same else 10.0 * h), i, j))
    used_n, used_t, matched = set(), set(), {}
    for d, i, j in sorted(pairs):
        if i in used_n or j in used_t:
            continue
        used_n.add(i)
        used_t.add(j)
        matched[i] = j
    tags = []
    for i, (nt, level) in enumerate(names):
        num = numbers[matched[i]] if i in matched else None
        tags.append(LevelTag(level.key, nt, num[0] if num else None, num[1] if num else None,
                             round(level.confidence * (1.0 if num else 0.6), 2), nt.text))
    return sorted(tags, key=lambda t: (t.elevation_mm if t.elevation_mm is not None else math.inf, level_sort_key(t.key)))


def horizontal_lines(region, min_coverage: float = _PARTIAL_COVERAGE) -> list:
    """Long horizontal lines of a region grouped by height, each with the share of the view width it covers."""
    width = max(region.width, 1e-9)
    depth = max(region.depth, 1e-9)
    tol = 0.004 * depth
    rows: list = []
    for e in region.geometry:
        if e.kind != "LINE" or len(e.points) != 2:
            continue
        (x1, y1), (x2, y2) = e.points
        length = abs(x2 - x1)
        if abs(y2 - y1) > 0.002 * depth + 1e-9 or length < 0.1 * width:
            continue
        y = (y1 + y2) / 2
        for row in rows:
            if abs(row["y"] - y) <= tol:
                row["spans"].append((min(x1, x2), max(x1, x2)))
                row["ids"].append(e.id)
                row["layer"] = row["layer"] or e.layer
                break
        else:
            rows.append({"y": y, "spans": [(min(x1, x2), max(x1, x2))], "ids": [e.id], "layer": e.layer})
    lines = []
    for row in rows:
        spans = sorted(row["spans"])
        covered, cur_lo, cur_hi = 0.0, spans[0][0], spans[0][1]
        for lo, hi in spans[1:]:
            if lo <= cur_hi:
                cur_hi = max(cur_hi, hi)
            else:
                covered += cur_hi - cur_lo
                cur_lo, cur_hi = lo, hi
        covered += cur_hi - cur_lo
        coverage = min(1.0, covered / width)
        if coverage >= min_coverage:
            lines.append(LevelLine(row["y"], round(coverage, 3), row["layer"], row["ids"]))
    return sorted(lines, key=lambda l: l.y)


def analyse_section(region, namer: LevelNamer, unit_factor: float) -> VerticalFindings:
    """Level evidence from a section (also usable on an elevation: see analyse_elevation)."""
    out = VerticalFindings()
    out.tags = extract_level_tags(region.texts, namer)
    out.lines = horizontal_lines(region)
    full = [l for l in out.lines if l.coverage >= _FLOOR_COVERAGE]
    out.partial_lines = [l for l in out.lines if l.coverage < _FLOOR_COVERAGE]
    _label_lines(out, region, namer)

    tagged = _heights_from_tags(out, 0.85)
    labelled = [l for l in full if l.label_key]
    if not tagged and len(labelled) >= 2:
        for a, b in zip(labelled, labelled[1:]):
            if a.label_key != b.label_key:
                out.heights.append((a.label_key, b.label_key, round((b.y - a.y) * unit_factor, 1), "section_lines", "inferred", 0.7))
    elif tagged and len(labelled) >= 2:
        _check_tag_geometry(out, labelled, tagged, unit_factor)

    for d in region.dims:
        if d.value and abs(math.sin(math.radians(d.rotation))) > 0.9 and len(d.points) >= 3:
            ys = sorted(p[1] for p in d.points[1:3])
            out.vertical_dims_mm.append((round(d.value * unit_factor, 1), ys[0], ys[1]))
    # a vertical dimension that spans two consecutive labelled level lines corroborates their height
    for a, b in zip(labelled, labelled[1:]):
        if a.label_key == b.label_key:
            continue
        for value, lo, hi in out.vertical_dims_mm:
            if abs(lo - a.y) <= 0.02 * max(region.depth, 1e-9) and abs(hi - b.y) <= 0.02 * max(region.depth, 1e-9):
                out.heights.append((a.label_key, b.label_key, value, "section_dimension", "source", 0.9))
    return out


def _heights_from_tags(out: VerticalFindings, confidence: float) -> list:
    """Heights between consecutive tagged levels: both elevations are written text, so the difference is SOURCE."""
    tagged = [t for t in out.tags if t.elevation_mm is not None]
    for a, b in zip(tagged, tagged[1:]):
        if a.key != b.key and b.elevation_mm > a.elevation_mm:
            out.heights.append((a.key, b.key, round(b.elevation_mm - a.elevation_mm, 1), "level_tags", "source", confidence))
    return tagged


def _label_lines(out: VerticalFindings, region, namer: LevelNamer) -> None:
    """Attach to each level line the level-name text whose height matches it (within a couple of text heights)."""
    named = [(t, namer.parse(t.text)) for t in region.texts if len(t.text) <= 40]
    named = [(t, l) for t, l in named if l is not None]
    for line in out.lines:
        best = None
        for t, level in named:
            gap = abs(t.centre[1] - line.y)
            if gap <= max(2.5 * (t.height or 0.0), 0.02 * region.depth) and (best is None or gap < best[0]):
                best = (gap, t, level)
        if best is not None:
            line.label_key, line.label_text = best[2].key, best[1].text


def _check_tag_geometry(out: VerticalFindings, labelled: list, tagged: list, unit_factor: float) -> None:
    by_key = {t.key: t.elevation_mm for t in tagged}
    ratios = []
    for a, b in zip(labelled, labelled[1:]):
        if a.label_key in by_key and b.label_key in by_key and by_key[b.label_key] != by_key[a.label_key]:
            ratios.append(((b.y - a.y) * unit_factor) / (by_key[b.label_key] - by_key[a.label_key]))
    if ratios:
        out.tag_geometry_consistent = all(abs(r - 1.0) <= 0.1 for r in ratios)
        if not out.tag_geometry_consistent:
            out.notes.append("The drawn spacing of the level lines does not follow the written elevations; the written "
                             "numbers are used and the drawn spacing is not.")


def analyse_elevation(region, namer: LevelNamer, unit_factor: float) -> VerticalFindings:
    """Level tags and the horizontal bands visible on an elevation (evidence about the number of floors)."""
    out = VerticalFindings()
    out.tags = extract_level_tags(region.texts, namer)
    out.lines = [l for l in horizontal_lines(region, 0.4)]
    _heights_from_tags(out, 0.75)          # a little less than a section: elevations are more often schematic
    return out


def band_heights_mm(lines: list, unit_factor: float) -> list:
    ys = [l.y for l in lines]
    return [round((b - a) * unit_factor, 1) for a, b in zip(ys, ys[1:])]
