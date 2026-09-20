"""Oracle — Architectural Observations

Purpose:
    Turns the contents of a view into ARCHITECTURAL OBSERVATIONS: what the drawing appears to show, as
    neutral records. Linework on wall, window, stair, grid, roof, floor and furniture layers becomes one
    aggregated observation per layer (with a count and a sample of source entities); doors, windows and
    existing columns drawn as blocks or closed shapes become individual observations; labels ("STAIR",
    "LIFT", "VOID OVER...", "DOUBLE HEIGHT", "BALCONY", "TOILET", "SHAFT") become observations at the
    label, matched with one typo allowed and enclosed by the smallest closed shape around the label when
    there is one. Vertical evidence adds level lines and partial floor lines from sections.

Role in Oracle:
    Stage 6. It states architecture, not structure: a wall observation is not a structural wall, a closed
    rectangle on a column layer is an "existing column" the architect drew (a hint to look at), and a big
    stair or lift opening is a STRUCTURAL_HINT with the evidence that produced it. Basis is SOURCE when the
    thing is drawn as that kind of object, INFERRED when it is deduced from a label or shape, and
    confidence reflects both the layer's confidence and the kind of match.

Dependencies:
    oracle.core (HintKind, ValueStatus); oracle.interpretation.naming (fuzzy word match); standard library.

Consumers:
    oracle.interpretation.pipeline; tests.

Status:
    Interpretation (Phase 3).

Migration/Notes:
    Not implemented yet: stairs recognised from tread geometry alone, room polygons (so no long-span hint),
    cantilevers and transfer-like discontinuities. Those need structural reasoning over several views.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from oracle.core import HintKind, ValueStatus

from .naming import has_word

_SAMPLE = 25

# What each observation kind MEANS: what the drawing shows at that place, and nothing about what it does structurally.
# A kind is an OBSERVATION ("a rectangular symbol is drawn here"); a hint is a proposed INTERPRETATION ("it may be a column");
# only an engineer's decision and an evidence link make it a structural fact. Adding a kind means adding its meaning here.
OBSERVATION_VOCABULARY = {
    "wall": "linework on a layer named for walls", "wall_external": "linework on a layer named for external walls",
    "wall_internal": "linework on a layer named for internal walls or partitions",
    "door": "a door symbol (a placed block on a door layer)", "window": "a window symbol (a placed block on a window layer)",
    "glazing": "glazing linework", "column_symbol": "a closed symbol or block on a layer named for columns",
    "beam_symbol": "linework on a layer named for beams", "stair_linework": "linework on a layer named for stairs",
    "lift_linework": "linework on a layer named for lifts", "handrail": "handrail or balustrade linework",
    "grid_lines": "grid linework", "roof_linework": "linework on a layer named for roofs",
    "floor_linework": "linework on a layer named for floors", "furniture": "furniture linework",
    "casework": "casework linework", "sanitary_fixtures": "sanitary fixture linework", "ramp": "ramp linework",
    "stair": "a text label naming a stair, with the closed shape around it if there is one",
    "lift": "a text label naming a lift, with the closed shape around it if there is one",
    "void": "a text label naming a void, with the closed shape around it if there is one",
    "shaft": "a text label naming a shaft, with the closed shape around it if there is one",
    "balcony": "a text label naming a balcony", "toilet": "a text label naming a toilet",
    "double_height_space": "a text label naming a double-height space",
    "level_line": "a horizontal line spanning most of a section or elevation",
    "partial_floor_line": "a horizontal line spanning only part of a section or elevation",
}
_LINEWORK = {"wall": "wall", "wall_external": "wall_external", "wall_internal": "wall_internal", "window": "glazing",
             "stair": "stair_linework", "grid": "grid_lines", "roof": "roof_linework", "floor": "floor_linework",
             "beam": "beam_symbol", "handrail": "handrail", "furniture": "furniture", "casework": "casework",
             "sanitary": "sanitary_fixtures", "ramp": "ramp", "lift": "lift_linework"}
_LABELS = (("STAIR", "stair", HintKind.STAIR_OPENING), ("STAIRS", "stair", HintKind.STAIR_OPENING),
           ("STAIRCASE", "stair", HintKind.STAIR_OPENING), ("LIFT", "lift", HintKind.LIFT_SHAFT),
           ("ELEVATOR", "lift", HintKind.LIFT_SHAFT), ("VOID", "void", HintKind.DOUBLE_HEIGHT),
           ("BALCONY", "balcony", None), ("TOILET", "toilet", None), ("WC", "toilet", None),
           ("SHAFT", "shaft", None), ("DUCT", "shaft", None), ("RISER", "shaft", None))
_LARGE_OPENING_MM2 = 4.0e6      # 4 square metres


@dataclass
class ObservationSpec:
    kind: str
    basis: ValueStatus
    confidence: float
    layer: Optional[str]
    entity_ids: list
    geometry: list = field(default_factory=list)
    closed: bool = False
    label: Optional[str] = None
    count: int = 1
    hint: Optional[HintKind] = None
    evidence: list = field(default_factory=list)        # (method, description, weight)


def _rect(box) -> list:
    return [(box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3])]


def _box(entities) -> tuple:
    return (min(e.box[0] for e in entities), min(e.box[1] for e in entities),
            max(e.box[2] for e in entities), max(e.box[3] for e in entities))


def _enclosure(region, point) -> Optional[object]:
    """The smallest closed polyline of the region that surrounds the point."""
    best = None
    for e in region.geometry:
        if e.kind == "POLYLINE" and e.closed and len(e.points) >= 4 and e.box[0] <= point[0] <= e.box[2] \
                and e.box[1] <= point[1] <= e.box[3]:
            if best is None or e.width * e.depth < best.width * best.depth:
                best = e
    return best


def plan_observations(region, layer_class: dict, layer_conf: dict, unit_factor: float) -> list:
    out = []
    by_layer: dict = {}
    for e in region.geometry:
        by_layer.setdefault(e.layer, []).append(e)
    for layer, entities in sorted(by_layer.items()):
        cls, conf = layer_class.get(layer, "unknown"), layer_conf.get(layer, 0.0)
        if cls in ("door", "window"):
            inserts = [e for e in entities if e.kind == "INSERT"]
            for e in inserts:
                out.append(ObservationSpec(cls, ValueStatus.SOURCE, round(0.9 * conf, 2), layer, [e.id], _rect(e.box), True,
                                           e.block, 1, None, [("layer_class", f"a block ({e.block}) on a {cls} layer", conf)]))
            rest = [e for e in entities if e.kind != "INSERT"]
            if cls == "window" and rest:
                out.append(_aggregate("glazing", layer, rest, conf))
            continue
        if cls == "column":
            for e in entities:
                if e.kind in ("INSERT", "POLYLINE", "CIRCLE", "HATCH"):
                    out.append(ObservationSpec("column_symbol", ValueStatus.INFERRED, round(0.85 * conf, 2), layer, [e.id],
                                               _rect(e.box), True, e.block, 1, HintKind.COLUMN_CANDIDATE,
                                               [("layer_class", f"a {e.kind.lower()} on a column-class layer", conf)]))
            continue
        if cls in _LINEWORK:
            kind = _LINEWORK[cls]
            spec = _aggregate(kind, layer, entities, conf)
            if cls == "beam":
                spec.hint = HintKind.BEAM_CANDIDATE
            out.append(spec)
    labelled = set()
    for t in region.texts:
        if len(t.text) > 60:
            continue
        for word, kind, hint in _LABELS:
            match = has_word(t.text, word)
            if match is None:
                continue
            conf = 0.65 if match else 0.45
            geometry, closed, evidence = [t.centre], False, [("label", f"the text {t.text!r} names a {kind}"
                                                              + ("" if match else " (one typo)"), conf)]
            box = _enclosure(region, t.centre)
            evidence_hint = hint
            if box is not None and kind in ("stair", "lift", "void", "shaft", "balcony", "toilet"):
                geometry, closed, conf = _rect(box.box), True, min(0.85, conf + 0.2)
                area_mm2 = box.width * box.depth * unit_factor * unit_factor
                evidence.append(("enclosure", f"enclosed by a closed shape of {area_mm2 / 1e6:.1f} m2", 0.2))
                if kind in ("stair", "lift", "void") and area_mm2 >= _LARGE_OPENING_MM2 and hint is None:
                    evidence_hint = HintKind.LARGE_OPENING
                elif kind in ("stair", "lift", "void") and area_mm2 >= _LARGE_OPENING_MM2:
                    evidence.append(("size", "the enclosure is at least 4 m2, a large opening", 0.1))
            out.append(ObservationSpec(kind, ValueStatus.INFERRED, round(conf, 2), t.layer, [t.id] + ([box.id] if box else []),
                                       geometry, closed, t.text, 1, evidence_hint, evidence))
            labelled.add(t.id)
            break
        if "DOUBLE" in t.text.upper().split() and has_word(t.text, "HEIGHT") is not None and t.id not in labelled:
            out.append(ObservationSpec("double_height_space", ValueStatus.INFERRED, 0.6, t.layer, [t.id], [t.centre], False,
                                       t.text, 1, HintKind.DOUBLE_HEIGHT, [("label", f"the text {t.text!r} names a double-height space", 0.6)]))
    return out


def _aggregate(kind: str, layer: str, entities: list, conf: float) -> ObservationSpec:
    box = _box(entities)
    return ObservationSpec(kind, ValueStatus.SOURCE, round(0.9 * conf, 2), layer, [e.id for e in entities[:_SAMPLE]],
                           _rect(box), True, None, len(entities), None,
                           [("layer_class", f"{len(entities)} entities on layer {layer}", conf)])


def vertical_observations(findings) -> list:
    """Level lines (full width) and partial floor lines from a section, as observations."""
    out = []
    for line in findings.lines:
        full = line.coverage >= 0.7
        label = line.label_text
        if full:
            out.append(ObservationSpec("level_line", ValueStatus.SOURCE, 0.8, line.layer, line.entity_ids[:_SAMPLE],
                                       [(0.0, line.y)], False, label, len(line.entity_ids), None,
                                       [("geometry", f"a horizontal line spanning {line.coverage:.0%} of the view", 0.8)]))
        else:
            out.append(ObservationSpec("partial_floor_line", ValueStatus.INFERRED, 0.5, line.layer, line.entity_ids[:_SAMPLE],
                                       [(0.0, line.y)], False, label, len(line.entity_ids), HintKind.DOUBLE_HEIGHT,
                                       [("geometry", f"a floor line covering only {line.coverage:.0%} of the view: a mezzanine, a void "
                                                     "or a double-height space", 0.5)]))
    return out
