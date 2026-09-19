"""Oracle — Core Structural Elements

Purpose:
    Domain objects for Section and the seven element kinds (Column, Beam, Slab, Wall, Stair,
    Foundation, Opening). Each validates its own fields and (de)serialises itself.

Role in Oracle:
    Defines what a structural element is. Cross-references (levels, nodes, supports) are only
    IDs here; BuildingModel checks that they resolve.

Dependencies:
    oracle.core.common, oracle.core.geometry.

Consumers:
    oracle.core.building (owner), oracle.core.design_basis (ElementKind for cover keys).

Status:
    Core.

Migration:
    Remains. Analysis, design and reinforcement results will be separate records that
    reference these elements by ID.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar, Mapping, Optional

from .common import (
    ValidationError, check_id, check_keys, check_number, check_optional_non_negative,
    check_optional_text, check_positive, check_text, check_unique_ids, parse_enum,
)
from .geometry import Point2D, Polygon2D


class ElementKind(str, Enum):
    COLUMN = "column"
    BEAM = "beam"
    SLAB = "slab"
    WALL = "wall"
    STAIR = "stair"
    FOUNDATION = "foundation"
    OPENING = "opening"


class SectionShape(str, Enum):
    RECTANGULAR = "rectangular"
    CIRCULAR = "circular"
    STANDARD = "standard"  # rolled/proprietary section named by designation, e.g. "UB 305x165x40"


@dataclass(frozen=True)
class Section:
    shape: SectionShape
    width_mm: Optional[float] = None
    depth_mm: Optional[float] = None
    diameter_mm: Optional[float] = None
    designation: Optional[str] = None

    def __post_init__(self):
        shape = parse_enum(SectionShape, self.shape, "section shape")
        object.__setattr__(self, "shape", shape)
        used = {
            SectionShape.RECTANGULAR: {"width_mm", "depth_mm"},
            SectionShape.CIRCULAR: {"diameter_mm"},
            SectionShape.STANDARD: {"designation"},
        }[shape]
        for name in ("width_mm", "depth_mm", "diameter_mm", "designation"):
            value = getattr(self, name)
            if name in used:
                if name == "designation":
                    check_text(value, "section designation")
                else:
                    check_positive(value, f"section {name}")
            elif value is not None:
                raise ValidationError(f"A {shape.value} section must not define {name}.")

    @classmethod
    def rectangular(cls, width_mm: float, depth_mm: float) -> "Section":
        return cls(SectionShape.RECTANGULAR, width_mm=width_mm, depth_mm=depth_mm)

    @classmethod
    def circular(cls, diameter_mm: float) -> "Section":
        return cls(SectionShape.CIRCULAR, diameter_mm=diameter_mm)

    @classmethod
    def standard(cls, designation: str) -> "Section":
        return cls(SectionShape.STANDARD, designation=designation)

    def to_dict(self) -> dict:
        out: dict = {"shape": self.shape.value}
        for name in ("width_mm", "depth_mm", "diameter_mm", "designation"):
            if getattr(self, name) is not None:
                out[name] = getattr(self, name)
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Section":
        check_keys(data, required={"shape"}, optional={"width_mm", "depth_mm", "diameter_mm", "designation"},
                   where="section")
        return cls(data["shape"], data.get("width_mm"), data.get("depth_mm"),
                   data.get("diameter_mm"), data.get("designation"))


class StructuralElement:
    """Base for all structural elements. Subclasses are dataclasses that define `id`."""

    kind: ClassVar[ElementKind]
    id: str

    @property
    def level_ids(self) -> tuple:
        raise NotImplementedError

    def to_dict(self) -> dict:
        raise NotImplementedError


@dataclass
class Column(StructuralElement):
    """A vertical member running from lower_level_id up to upper_level_id."""

    kind: ClassVar[ElementKind] = ElementKind.COLUMN
    id: str
    lower_level_id: str
    upper_level_id: str
    location: Point2D
    section: Section
    material: Optional[str] = None
    orientation_deg: float = 0.0  # rotation of the section about the vertical axis, anticlockwise from plan +x

    def __post_init__(self):
        check_id(self.id, "column id")
        check_id(self.lower_level_id, "column lower_level_id")
        check_id(self.upper_level_id, "column upper_level_id")
        if self.lower_level_id == self.upper_level_id:
            raise ValidationError(f"Column {self.id}: lower and upper level must differ.")
        check_number(self.orientation_deg, "column orientation_deg")
        check_optional_text(self.material, "column material")

    @property
    def level_ids(self) -> tuple:
        return (self.lower_level_id, self.upper_level_id)

    def to_dict(self) -> dict:
        return {"id": self.id, "lower_level_id": self.lower_level_id, "upper_level_id": self.upper_level_id,
                "location": self.location.to_list(), "section": self.section.to_dict(),
                "material": self.material, "orientation_deg": self.orientation_deg}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Column":
        check_keys(data, required={"id", "lower_level_id", "upper_level_id", "location", "section"},
                   optional={"material", "orientation_deg"}, where="column")
        return cls(data["id"], data["lower_level_id"], data["upper_level_id"],
                   Point2D.from_list(data["location"]), Section.from_dict(data["section"]),
                   data.get("material"), data.get("orientation_deg", 0.0))


@dataclass
class Beam(StructuralElement):
    """A horizontal member framing at level_id, running between two nodes on that level."""

    kind: ClassVar[ElementKind] = ElementKind.BEAM
    id: str
    level_id: str
    start_node_id: str
    end_node_id: str
    section: Section
    material: Optional[str] = None

    def __post_init__(self):
        check_id(self.id, "beam id")
        check_id(self.level_id, "beam level_id")
        check_id(self.start_node_id, "beam start_node_id")
        check_id(self.end_node_id, "beam end_node_id")
        if self.start_node_id == self.end_node_id:
            raise ValidationError(f"Beam {self.id}: start and end node must differ.")
        check_optional_text(self.material, "beam material")

    @property
    def level_ids(self) -> tuple:
        return (self.level_id,)

    def to_dict(self) -> dict:
        return {"id": self.id, "level_id": self.level_id, "start_node_id": self.start_node_id,
                "end_node_id": self.end_node_id, "section": self.section.to_dict(), "material": self.material}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Beam":
        check_keys(data, required={"id", "level_id", "start_node_id", "end_node_id", "section"},
                   optional={"material"}, where="beam")
        return cls(data["id"], data["level_id"], data["start_node_id"], data["end_node_id"],
                   Section.from_dict(data["section"]), data.get("material"))


@dataclass
class Slab(StructuralElement):
    """A floor/roof panel at level_id. `supported_by` lists the beams/walls/columns carrying it;
    `opening_ids` lists Opening elements on the same level that cut through it."""

    kind: ClassVar[ElementKind] = ElementKind.SLAB
    id: str
    level_id: str
    boundary: Polygon2D
    thickness_mm: float
    material: Optional[str] = None
    supported_by: list = field(default_factory=list)
    opening_ids: list = field(default_factory=list)

    def __post_init__(self):
        check_id(self.id, "slab id")
        check_id(self.level_id, "slab level_id")
        check_positive(self.thickness_mm, "slab thickness_mm")
        check_optional_text(self.material, "slab material")
        self.supported_by = list(self.supported_by)
        self.opening_ids = list(self.opening_ids)
        for i in self.supported_by:
            check_id(i, "slab supported_by id")
        for i in self.opening_ids:
            check_id(i, "slab opening id")
        check_unique_ids(self.supported_by, f"support reference on slab {self.id}:")
        check_unique_ids(self.opening_ids, f"opening reference on slab {self.id}:")

    @property
    def level_ids(self) -> tuple:
        return (self.level_id,)

    def to_dict(self) -> dict:
        return {"id": self.id, "level_id": self.level_id, "boundary": self.boundary.to_list(),
                "thickness_mm": self.thickness_mm, "material": self.material,
                "supported_by": list(self.supported_by), "opening_ids": list(self.opening_ids)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Slab":
        check_keys(data, required={"id", "level_id", "boundary", "thickness_mm"},
                   optional={"material", "supported_by", "opening_ids"}, where="slab")
        return cls(data["id"], data["level_id"], Polygon2D.from_list(data["boundary"]), data["thickness_mm"],
                   data.get("material"), data.get("supported_by", []), data.get("opening_ids", []))


@dataclass
class Wall(StructuralElement):
    kind: ClassVar[ElementKind] = ElementKind.WALL
    id: str
    level_id: str
    start_node_id: str
    end_node_id: str
    thickness_mm: float
    material: Optional[str] = None
    load_bearing: bool = False

    def __post_init__(self):
        check_id(self.id, "wall id")
        check_id(self.level_id, "wall level_id")
        check_id(self.start_node_id, "wall start_node_id")
        check_id(self.end_node_id, "wall end_node_id")
        if self.start_node_id == self.end_node_id:
            raise ValidationError(f"Wall {self.id}: start and end node must differ.")
        check_positive(self.thickness_mm, "wall thickness_mm")
        check_optional_text(self.material, "wall material")
        if not isinstance(self.load_bearing, bool):
            raise ValidationError(f"Wall {self.id}: load_bearing must be true or false.")

    @property
    def level_ids(self) -> tuple:
        return (self.level_id,)

    def to_dict(self) -> dict:
        return {"id": self.id, "level_id": self.level_id, "start_node_id": self.start_node_id,
                "end_node_id": self.end_node_id, "thickness_mm": self.thickness_mm,
                "material": self.material, "load_bearing": self.load_bearing}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Wall":
        check_keys(data, required={"id", "level_id", "start_node_id", "end_node_id", "thickness_mm"},
                   optional={"material", "load_bearing"}, where="wall")
        return cls(data["id"], data["level_id"], data["start_node_id"], data["end_node_id"],
                   data["thickness_mm"], data.get("material"), data.get("load_bearing", False))


@dataclass
class Stair(StructuralElement):
    """A stair flight/landing footprint connecting two levels."""

    kind: ClassVar[ElementKind] = ElementKind.STAIR
    id: str
    lower_level_id: str
    upper_level_id: str
    boundary: Polygon2D
    waist_thickness_mm: Optional[float] = None
    material: Optional[str] = None

    def __post_init__(self):
        check_id(self.id, "stair id")
        check_id(self.lower_level_id, "stair lower_level_id")
        check_id(self.upper_level_id, "stair upper_level_id")
        if self.lower_level_id == self.upper_level_id:
            raise ValidationError(f"Stair {self.id}: lower and upper level must differ.")
        if self.waist_thickness_mm is not None:
            check_positive(self.waist_thickness_mm, "stair waist_thickness_mm")
        check_optional_text(self.material, "stair material")

    @property
    def level_ids(self) -> tuple:
        return (self.lower_level_id, self.upper_level_id)

    def to_dict(self) -> dict:
        return {"id": self.id, "lower_level_id": self.lower_level_id, "upper_level_id": self.upper_level_id,
                "boundary": self.boundary.to_list(), "waist_thickness_mm": self.waist_thickness_mm,
                "material": self.material}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Stair":
        check_keys(data, required={"id", "lower_level_id", "upper_level_id", "boundary"},
                   optional={"waist_thickness_mm", "material"}, where="stair")
        return cls(data["id"], data["lower_level_id"], data["upper_level_id"],
                   Polygon2D.from_list(data["boundary"]), data.get("waist_thickness_mm"), data.get("material"))


class FoundationType(str, Enum):
    PAD = "pad"
    STRIP = "strip"
    RAFT = "raft"
    PILE_CAP = "pile_cap"


@dataclass
class Foundation(StructuralElement):
    """A foundation founded at level_id (the foundation/ground level), supporting the listed columns."""

    kind: ClassVar[ElementKind] = ElementKind.FOUNDATION
    id: str
    level_id: str
    foundation_type: FoundationType
    boundary: Polygon2D
    depth_mm: Optional[float] = None
    material: Optional[str] = None
    supported_column_ids: list = field(default_factory=list)

    def __post_init__(self):
        check_id(self.id, "foundation id")
        check_id(self.level_id, "foundation level_id")
        self.foundation_type = parse_enum(FoundationType, self.foundation_type, "foundation type")
        if self.depth_mm is not None:
            check_positive(self.depth_mm, "foundation depth_mm")
        check_optional_text(self.material, "foundation material")
        self.supported_column_ids = list(self.supported_column_ids)
        for i in self.supported_column_ids:
            check_id(i, "foundation supported column id")
        check_unique_ids(self.supported_column_ids, f"column reference on foundation {self.id}:")

    @property
    def level_ids(self) -> tuple:
        return (self.level_id,)

    def to_dict(self) -> dict:
        return {"id": self.id, "level_id": self.level_id, "foundation_type": self.foundation_type.value,
                "boundary": self.boundary.to_list(), "depth_mm": self.depth_mm, "material": self.material,
                "supported_column_ids": list(self.supported_column_ids)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Foundation":
        check_keys(data, required={"id", "level_id", "foundation_type", "boundary"},
                   optional={"depth_mm", "material", "supported_column_ids"}, where="foundation")
        return cls(data["id"], data["level_id"], data["foundation_type"], Polygon2D.from_list(data["boundary"]),
                   data.get("depth_mm"), data.get("material"), data.get("supported_column_ids", []))


@dataclass
class Opening(StructuralElement):
    """A void/opening in the floor plate at level_id (stair well, lift shaft, service riser...)."""

    kind: ClassVar[ElementKind] = ElementKind.OPENING
    id: str
    level_id: str
    boundary: Polygon2D
    purpose: Optional[str] = None

    def __post_init__(self):
        check_id(self.id, "opening id")
        check_id(self.level_id, "opening level_id")
        check_optional_text(self.purpose, "opening purpose")

    @property
    def level_ids(self) -> tuple:
        return (self.level_id,)

    def to_dict(self) -> dict:
        return {"id": self.id, "level_id": self.level_id, "boundary": self.boundary.to_list(),
                "purpose": self.purpose}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Opening":
        check_keys(data, required={"id", "level_id", "boundary"}, optional={"purpose"}, where="opening")
        return cls(data["id"], data["level_id"], Polygon2D.from_list(data["boundary"]), data.get("purpose"))


ELEMENT_CLASSES = {c.kind: c for c in (Column, Beam, Slab, Wall, Stair, Foundation, Opening)}
