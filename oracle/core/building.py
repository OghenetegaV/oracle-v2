"""Oracle — Core Building Model

Purpose:
    Level, GridLine, Node and BuildingModel: the canonical, CAD-independent description of the
    building. It owns levels (ordered by elevation), nodes and elements, and enforces unique IDs
    and reference integrity when anything is added.

Role in Oracle:
    The single representation of the building that the drawing importers, analysis writers, design
    engine and drawing generators will share once adapters exist.

Dependencies:
    oracle.core.common, oracle.core.elements, oracle.core.geometry.

Consumers:
    oracle.core.project.

Status:
    Core.

Migration/Notes:
    Remains. The legacy parser's joints/members map onto Node/Beam/Column through
    oracle.adapters.legacy_ga. No structural calculation belongs here. replace_element() (schema 0.2.0
    work) lets an engineer's value change swap in a re-validated element; get_grid() completes lookup
    by ID for every kind of target. Schema 0.4.0 (Phase 3.5): a Level separates IDENTITY (id), engineer-facing
    LABEL (name), the label the source drawing used (source_label) and the interpretation key it realises (key),
    and says what its elevation IS (elevation_type: finished_floor / structural / datum / unspecified) with the
    structural elevation kept separately and None until established; replace_levels() swaps changed levels in as
    one validated step so that a change that moves several levels is atomic.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

from .common import (
    POSITION_TOL_MM, ValidationError, check_id, check_keys, check_number, check_optional_positive,
    check_text,
)
from .elements import (
    ELEMENT_CLASSES, Beam, Column, ElementKind, Foundation, Opening, Slab, Stair, StructuralElement, Wall,
)
from .geometry import Point2D

# Elevation difference to the next level must match a declared storey height to within this.
STOREY_HEIGHT_TOL_MM = 1.0

# Fixed order elements are serialised and reloaded in, so a file always loads dependency-first
# (openings before the slabs that reference them, columns before their foundations) and
# to_dict(from_dict(x)) reproduces x exactly.
_LOAD_ORDER = (ElementKind.COLUMN, ElementKind.BEAM, ElementKind.WALL, ElementKind.OPENING,
               ElementKind.SLAB, ElementKind.STAIR, ElementKind.FOUNDATION)
_KIND_KEYS = {k: k.value + "s" for k in ElementKind}


ELEVATION_TYPES = ("unspecified", "finished_floor", "structural", "datum")


@dataclass
class Level:
    """A horizontal datum. `index` is assigned by the building from elevation order (0 = lowest).

    IDENTITY IS NOT A LABEL. `id` is the stable identity that everything else refers to; `name` is the
    engineer-facing label and may change; `source_label` is what the source drawing called it, kept verbatim
    (an unfamiliar name such as "PODIUM" is preserved, never mapped onto a generic floor number); `key` is the
    interpretation-layer identity this level realises, if it came from one.

    ELEVATIONS ARE NOT INTERCHANGEABLE. `elevation_mm` is the elevation the level is currently placed at and
    `elevation_type` says what it is: 'finished_floor' (a drawing's floor level), 'structural' (a structural
    datum such as top of slab), 'datum' (a reference line such as natural ground), or 'unspecified' (the
    historical default; nothing is claimed). `structural_elevation_mm` is the structural level when it is
    established separately; None means NOT ESTABLISHED, and nothing derives it from a finished level."""

    id: str
    name: str
    elevation_mm: float
    storey_height_mm: Optional[float] = None  # height from this level up to the next; None if unknown/top
    index: int = -1
    source_label: Optional[str] = None
    elevation_type: str = "unspecified"
    structural_elevation_mm: Optional[float] = None
    key: Optional[str] = None
    datum: Optional[str] = None               # what elevation_mm is measured from, in words, when known

    def __post_init__(self):
        check_id(self.id, "level id")
        check_text(self.name, "level name")
        check_number(self.elevation_mm, "level elevation_mm")
        check_optional_positive(self.storey_height_mm, "level storey_height_mm")
        for name in ("source_label", "key", "datum"):
            v = getattr(self, name)
            if v is not None:
                check_text(v, f"level {name}")
        if self.elevation_type not in ELEVATION_TYPES:
            raise ValidationError(f"Level {self.id}: elevation_type must be one of {ELEVATION_TYPES}, got {self.elevation_type!r}.")
        if self.structural_elevation_mm is not None:
            check_number(self.structural_elevation_mm, "level structural_elevation_mm")
            if self.elevation_type == "structural" and abs(self.structural_elevation_mm - self.elevation_mm) > STOREY_HEIGHT_TOL_MM:
                raise ValidationError(f"Level {self.id}: elevation_mm is already the structural elevation; "
                                      "structural_elevation_mm disagrees with it.")

    @property
    def structural_elevation(self) -> Optional[float]:
        """The structural elevation if one is established, else None. A finished-floor elevation is never returned
        here: converting one into the other needs evidence or an engineer's decision."""
        if self.elevation_type == "structural":
            return self.elevation_mm
        return self.structural_elevation_mm

    def to_dict(self) -> dict:
        out = {"id": self.id, "name": self.name, "index": self.index, "elevation_mm": self.elevation_mm,
               "storey_height_mm": self.storey_height_mm}
        for name, default in (("source_label", None), ("elevation_type", "unspecified"),
                              ("structural_elevation_mm", None), ("key", None), ("datum", None)):
            value = getattr(self, name)
            if value != default:          # older files and older code see exactly the fields they always saw
                out[name] = value
        return out

    def to_full_dict(self) -> dict:
        """Every field, defaults included: what a field-level check or an engineer change works on."""
        return {"id": self.id, "name": self.name, "index": self.index, "elevation_mm": self.elevation_mm,
                "storey_height_mm": self.storey_height_mm, "source_label": self.source_label,
                "elevation_type": self.elevation_type, "structural_elevation_mm": self.structural_elevation_mm,
                "key": self.key, "datum": self.datum}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Level":
        check_keys(data, required={"id", "name", "elevation_mm"},
                   optional={"index", "storey_height_mm", "source_label", "elevation_type", "structural_elevation_mm",
                             "key", "datum"}, where="level")
        return cls(data["id"], data["name"], data["elevation_mm"], data.get("storey_height_mm"),
                   data.get("index", -1), data.get("source_label"), data.get("elevation_type", "unspecified"),
                   data.get("structural_elevation_mm"), data.get("key"), data.get("datum"))


@dataclass
class GridLine:
    label: str
    start: Point2D
    end: Point2D

    def __post_init__(self):
        check_id(self.label, "grid label")
        if self.start.distance_to(self.end) <= POSITION_TOL_MM:
            raise ValidationError(f"Grid line {self.label} has zero length.")

    def to_dict(self) -> dict:
        return {"label": self.label, "start": self.start.to_list(), "end": self.end.to_list()}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "GridLine":
        check_keys(data, required={"label", "start", "end"}, where="grid line")
        return cls(data["label"], Point2D.from_list(data["start"]), Point2D.from_list(data["end"]))


@dataclass
class Node:
    """A framing point on a level; beams and walls connect between nodes."""

    id: str
    level_id: str
    location: Point2D

    def __post_init__(self):
        check_id(self.id, "node id")
        check_id(self.level_id, "node level_id")

    def to_dict(self) -> dict:
        return {"id": self.id, "level_id": self.level_id, "location": self.location.to_list()}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Node":
        check_keys(data, required={"id", "level_id", "location"}, where="node")
        return cls(data["id"], data["level_id"], Point2D.from_list(data["location"]))


def _check_levels(levels: Iterable[Level]) -> None:
    """Whole-list level checks: unique ids/names/elevations, and declared storey heights that
    agree with the elevation step to the next level."""
    ordered = sorted(levels, key=lambda lv: lv.elevation_mm)
    seen_ids, seen_names = set(), set()
    for lv in ordered:
        if lv.id in seen_ids:
            raise ValidationError(f"Duplicate level id {lv.id!r}.")
        if lv.name.strip().lower() in seen_names:
            raise ValidationError(f"Duplicate level name {lv.name!r}.")
        seen_ids.add(lv.id)
        seen_names.add(lv.name.strip().lower())
    for lo, hi in zip(ordered, ordered[1:]):
        step = hi.elevation_mm - lo.elevation_mm
        if step <= STOREY_HEIGHT_TOL_MM:
            raise ValidationError(
                f"Levels {lo.id!r} and {hi.id!r} are at the same elevation ({lo.elevation_mm} mm).")
        if lo.storey_height_mm is not None and abs(lo.storey_height_mm - step) > STOREY_HEIGHT_TOL_MM:
            raise ValidationError(
                f"Level {lo.id!r} declares storey height {lo.storey_height_mm} mm but the next level "
                f"{hi.id!r} is {step} mm above it.")


class BuildingModel:
    """Levels, grid lines, nodes and structural elements of one building.

    `add_*` methods validate before mutating, so a failed add leaves the model unchanged.
    Element IDs share one namespace across all element kinds (so "C12" is unambiguous as a
    decision/issue target); level, node and grid IDs each have their own."""

    def __init__(self, building_id: str, name: str):
        self.id = check_id(building_id, "building id")
        self.name = check_text(name, "building name")
        self._levels: dict = {}
        self._grids: dict = {}
        self._nodes: dict = {}
        self._elements: dict = {}
        self.on_change = None  # optional callable, set by OracleProject to track modification time

    def _changed(self):
        if self.on_change:
            self.on_change()

    # ---- levels ----

    @property
    def levels(self) -> list:
        """Levels ordered from lowest to highest elevation."""
        return sorted(self._levels.values(), key=lambda lv: lv.elevation_mm)

    def get_level(self, level_id: str) -> Level:
        try:
            return self._levels[level_id]
        except KeyError:
            raise ValidationError(f"Unknown level {level_id!r}.") from None

    def add_level(self, level: Level) -> Level:
        if level.id in self._levels:
            raise ValidationError(f"Duplicate level id {level.id!r}.")
        _check_levels([*self._levels.values(), level])
        self._levels[level.id] = level
        for i, lv in enumerate(self.levels):
            lv.index = i
        self._changed()
        return level

    def replace_levels(self, replacements: Iterable) -> None:
        """Swap in changed versions of existing levels (same ids) as ONE step: the whole level list is
        re-validated with them, and nothing changes if it would be invalid. Moving one level can move others
        (a storey-height change shifts the levels above), so a level is never replaced on its own."""
        replacements = list(replacements)
        for lv in replacements:
            if lv.id not in self._levels:
                raise ValidationError(f"Unknown level {lv.id!r}.")
        merged = {**self._levels, **{lv.id: lv for lv in replacements}}
        _check_levels(list(merged.values()))
        self._levels = merged
        for i, lv in enumerate(self.levels):
            lv.index = i
        self._changed()

    # ---- grids ----

    @property
    def grids(self) -> list:
        return list(self._grids.values())

    def get_grid(self, label: str) -> GridLine:
        try:
            return self._grids[label]
        except KeyError:
            raise ValidationError(f"Unknown grid line {label!r}.") from None

    def add_grid(self, grid: GridLine) -> GridLine:
        if grid.label in self._grids:
            raise ValidationError(f"Duplicate grid label {grid.label!r}.")
        self._grids[grid.label] = grid
        self._changed()
        return grid

    # ---- nodes ----

    @property
    def nodes(self) -> list:
        return list(self._nodes.values())

    def get_node(self, node_id: str) -> Node:
        try:
            return self._nodes[node_id]
        except KeyError:
            raise ValidationError(f"Unknown node {node_id!r}.") from None

    def add_node(self, node: Node) -> Node:
        if node.id in self._nodes:
            raise ValidationError(f"Duplicate node id {node.id!r}.")
        self.get_level(node.level_id)
        for other in self._nodes.values():
            if other.level_id == node.level_id and other.location.distance_to(node.location) <= POSITION_TOL_MM:
                raise ValidationError(
                    f"Node {node.id!r} coincides with node {other.id!r} on level {node.level_id!r}.")
        self._nodes[node.id] = node
        self._changed()
        return node

    # ---- elements ----

    @property
    def elements(self) -> list:
        return list(self._elements.values())

    def get_element(self, element_id: str) -> StructuralElement:
        try:
            return self._elements[element_id]
        except KeyError:
            raise ValidationError(f"Unknown element {element_id!r}.") from None

    def has_element(self, element_id: str) -> bool:
        return element_id in self._elements

    def elements_of(self, kind: ElementKind) -> list:
        return [e for e in self._elements.values() if e.kind == kind]

    def elements_on_level(self, level_id: str) -> list:
        """Elements that touch the level (a column touches both its lower and upper level)."""
        self.get_level(level_id)
        return [e for e in self._elements.values() if level_id in e.level_ids]

    def add_element(self, element: StructuralElement) -> StructuralElement:
        if not isinstance(element, StructuralElement):
            raise ValidationError(f"Not a structural element: {element!r}.")
        if element.id in self._elements:
            raise ValidationError(f"Duplicate element id {element.id!r}.")
        self._check_element(element)
        self._elements[element.id] = element
        self._changed()
        return element

    def replace_element(self, element: StructuralElement) -> StructuralElement:
        """Swap in a changed version of an existing element (same ID and kind), keeping its place in the
        order. It is fully re-checked first, so a rejected change leaves the model unchanged."""
        if not isinstance(element, StructuralElement):
            raise ValidationError(f"Not a structural element: {element!r}.")
        current = self.get_element(element.id)
        if current.kind != element.kind:
            raise ValidationError(f"Element {element.id} is a {current.kind.value}; it cannot be replaced by a "
                                  f"{element.kind.value}.")
        self._check_element(element)
        self._elements[element.id] = element
        self._changed()
        return element

    def _check_element(self, e: StructuralElement) -> None:
        for lid in e.level_ids:
            self.get_level(lid)
        if isinstance(e, (Column, Stair)):
            lower, upper = self.get_level(e.lower_level_id), self.get_level(e.upper_level_id)
            if upper.elevation_mm <= lower.elevation_mm:
                raise ValidationError(
                    f"{e.kind.value.capitalize()} {e.id}: upper level {upper.id!r} must be above "
                    f"lower level {lower.id!r}.")
        elif isinstance(e, (Beam, Wall)):
            start, end = self.get_node(e.start_node_id), self.get_node(e.end_node_id)
            for n in (start, end):
                if n.level_id != e.level_id:
                    raise ValidationError(
                        f"{e.kind.value.capitalize()} {e.id} is on level {e.level_id!r} but node "
                        f"{n.id!r} is on level {n.level_id!r}.")
            if start.location.distance_to(end.location) <= POSITION_TOL_MM:
                raise ValidationError(f"{e.kind.value.capitalize()} {e.id} has zero length.")
        elif isinstance(e, Slab):
            for sid in e.supported_by:
                support = self.get_element(sid)
                if support.kind not in (ElementKind.BEAM, ElementKind.WALL, ElementKind.COLUMN):
                    raise ValidationError(
                        f"Slab {e.id} cannot be supported by {support.kind.value} {sid!r} "
                        "(expected a beam, wall or column).")
            for oid in e.opening_ids:
                opening = self.get_element(oid)
                if opening.kind != ElementKind.OPENING or opening.level_id != e.level_id:
                    raise ValidationError(f"Slab {e.id}: {oid!r} is not an opening on level {e.level_id!r}.")
        elif isinstance(e, Foundation):
            for cid in e.supported_column_ids:
                if self.get_element(cid).kind != ElementKind.COLUMN:
                    raise ValidationError(f"Foundation {e.id}: {cid!r} is not a column.")

    # ---- derived geometry (no structural calculation) ----

    def member_length_mm(self, element_id: str) -> float:
        e = self.get_element(element_id)
        if not isinstance(e, (Beam, Wall)):
            raise ValidationError(f"{element_id!r} is a {e.kind.value}; only beams and walls have a plan length.")
        return self.get_node(e.start_node_id).location.distance_to(self.get_node(e.end_node_id).location)

    def member_orientation_deg(self, element_id: str) -> float:
        """Direction of a beam/wall from start to end, anticlockwise from plan +x, in [0, 180)
        (a member has no intrinsic sense, so 10 deg and 190 deg are the same orientation)."""
        e = self.get_element(element_id)
        if not isinstance(e, (Beam, Wall)):
            raise ValidationError(f"{element_id!r} is a {e.kind.value}; only beams and walls have an orientation.")
        s, t = self.get_node(e.start_node_id).location, self.get_node(e.end_node_id).location
        return math.degrees(math.atan2(t.y_mm - s.y_mm, t.x_mm - s.x_mm)) % 180.0

    def storey_height_mm(self, level_id: str) -> Optional[float]:
        """Height from this level to the next one up, or None for the highest level."""
        levels = self.levels
        i = next(i for i, lv in enumerate(levels) if lv.id == level_id)
        return levels[i + 1].elevation_mm - levels[i].elevation_mm if i + 1 < len(levels) else None

    # ---- whole-model validation ----

    def validate(self) -> None:
        """Re-check everything. add_* already validates, so this matters after loading a file
        or after someone mutated an element in place."""
        _check_levels(self._levels.values())
        for i, lv in enumerate(self.levels):
            if lv.index != i:
                raise ValidationError(f"Level {lv.id!r} has index {lv.index} but is number {i} by elevation.")
        for n in self._nodes.values():
            self.get_level(n.level_id)
        for e in self._elements.values():
            self._check_element(e)

    # ---- serialisation ----

    def to_dict(self) -> dict:
        elements = {
            _KIND_KEYS[k]: [e.to_dict() for e in self._elements.values() if e.kind == k] for k in _LOAD_ORDER
        }
        return {
            "id": self.id,
            "name": self.name,
            "levels": [lv.to_dict() for lv in self.levels],
            "grids": [g.to_dict() for g in self._grids.values()],
            "nodes": [n.to_dict() for n in self._nodes.values()],
            "elements": elements,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "BuildingModel":
        check_keys(data, required={"id", "name", "levels", "grids", "nodes", "elements"}, where="building")
        model = cls(data["id"], data["name"])
        declared = [(d.get("id"), d.get("index")) for d in data["levels"] if isinstance(d, Mapping)]
        for d in data["levels"]:
            model.add_level(Level.from_dict(d))
        for lid, idx in declared:
            if idx is not None and model.get_level(lid).index != idx:
                raise ValidationError(
                    f"Level {lid!r} is stored with index {idx} but its elevation places it at "
                    f"index {model.get_level(lid).index}.")
        for d in data["grids"]:
            model.add_grid(GridLine.from_dict(d))
        for d in data["nodes"]:
            model.add_node(Node.from_dict(d))
        check_keys(data["elements"], required=set(), optional=set(_KIND_KEYS.values()), where="building elements")
        for kind in _LOAD_ORDER:
            for d in data["elements"].get(_KIND_KEYS[kind], []):
                model.add_element(ELEMENT_CLASSES[kind].from_dict(d))
        model.validate()
        return model
