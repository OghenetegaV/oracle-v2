"""Oracle — Core Architectural Interpretation Model

Purpose:
    The CAD-neutral data model of what an interpreter concluded about a source drawing: the
    DrawingSource (file, hash, unit estimate), CoordinateFrames (source -> view -> building),
    LayerClassifications, DrawingViews (floor plans, sections, elevations...), ArchitecturalObservations
    (walls, doors, stairs, voids, structural hints...), HeightEvidence (floor-to-floor heights and where
    each came from) and CrossViewFindings (do the drawings agree?), all held by one
    ArchitecturalInterpretation. It stores conclusions and their geometry in the drawing's own
    coordinates; it contains no drawing-reading code and no interpretation algorithms.

Role in Oracle:
    The persistent home for "what does this architectural drawing appear to contain?". It exists in the
    core, not in the interpreter, because engineer decisions, provenance records, value statuses and
    issues must be able to TARGET a view, a layer classification or an observation (Target.architectural),
    and the project must be able to check that those targets exist. Nothing here is a building fact: a
    view or observation is an interpretation with a confidence, a review status and provenance, and only
    becomes part of the BuildingModel through an engineer-approved step. The chain is preserved:
    drawing entity (SOURCE) -> observation/view (INFERRED, with provenance) -> engineer decision ->
    Level/Element in the BuildingModel.

    Original coordinates are never destroyed: observation geometry is in source-drawing units, and a
    chain of CoordinateFrames (each mapping its own coordinates to its parent's) carries source ->
    view-local -> building. Millimetres are the canonical unit; the drawing's UnitEstimate says how to
    get there and how sure we are.

Dependencies:
    oracle.core.common only. No CAD library, no geometry library, no AI client.

Consumers:
    oracle.core.project (holds one ArchitecturalInterpretation, validates targets, applies engineer
    changes and review actions); oracle.interpretation (builds it); tests; future structural reasoning.

Status:
    Core (added in schema 0.3.0).

Migration/Notes:
    Schema 0.3.0 added the optional project-level `architecture` section and the ARCHITECTURAL target
    scope (0.2.0 projects migrate by gaining `architecture: null`). Schema 0.4.0 made the drawing source
    format-neutral (declared_unit and an opaque source_metadata replace the DXF-specific unit_code,
    format_version and layouts; the id prefix is SRC-), added revision and interpretation identity, and allows
    unfamiliar level labels (NAMED:<LABEL> keys). Vocabulary that is meant to be
    extended (semantic classes, observation kinds) is validated text, not an enum, so new conventions
    need no schema change. Only one drawing per project is supported today.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional

from .common import (
    ValidationError, check_confidence, check_id, check_keys, check_number, check_optional_text, check_positive,
    check_text, check_unique_ids, parse_enum,
)
from .value_status import ValueStatus

_ID_PATTERNS = {
    "drawing": re.compile(r"^SRC-\d+$"), "frame": re.compile(r"^FRM-\d+$"), "layer": re.compile(r"^LAY-\d+$"),
    "view": re.compile(r"^VIEW-\d+$"), "observation": re.compile(r"^OBS-\d+$"), "height": re.compile(r"^HGT-\d+$"),
    "finding": re.compile(r"^FND-\d+$"),
}
# A level key is an interpretation-layer IDENTITY, not a display label. The standard forms cover the common cases;
# NAMED:<SLUG> keeps any unfamiliar label (PODIUM, LOWER_TERRACE, ...) as itself instead of forcing it onto a number.
_LEVEL_KEY_RE = re.compile(r"^(DATUM|GROUND|ROOF|PENTHOUSE|MEZZANINE(:\d+)?|FLOOR:\d+|BASEMENT:\d+|NAMED:[A-Z0-9][A-Z0-9_]{0,39})$")
_VOCAB_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_UNITS = {"mm", "cm", "m", "inch", "foot"}


def check_kind_id(kind: str, value: Any) -> str:
    if not isinstance(value, str) or not _ID_PATTERNS[kind].match(value):
        raise ValidationError(f"Invalid {kind} id {value!r}: expected the form {_ID_PATTERNS[kind].pattern}.")
    return value


def check_level_key(value: Any, what: str = "level key") -> str:
    if not isinstance(value, str) or not _LEVEL_KEY_RE.match(value):
        raise ValidationError(f"Invalid {what} {value!r}: expected DATUM, GROUND, ROOF, PENTHOUSE, MEZZANINE, FLOOR:n, "
                              "BASEMENT:n or NAMED:<LABEL>.")
    return value


def check_vocab(value: Any, what: str) -> str:
    if not isinstance(value, str) or not _VOCAB_RE.match(value):
        raise ValidationError(f"Invalid {what} {value!r}: use lower-case words joined by underscores.")
    return value


def check_bbox(value: Any, what: str = "bbox") -> tuple:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValidationError(f"{what} must be (min_x, min_y, max_x, max_y), got {value!r}.")
    box = tuple(float(check_number(v, what)) for v in value)
    if box[0] > box[2] or box[1] > box[3]:
        raise ValidationError(f"{what} has a minimum above its maximum: {box}.")
    return box


def _points(value: Any, what: str) -> tuple:
    out = []
    for p in value:
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            raise ValidationError(f"{what} must be a list of [x, y] points, got {p!r}.")
        out.append((float(check_number(p[0], what)), float(check_number(p[1], what))))
    return tuple(out)


class ViewType(str, Enum):
    FLOOR_PLAN = "floor_plan"
    SECTION = "section"
    ELEVATION = "elevation"
    DETAIL = "detail"
    SCHEDULE = "schedule"
    NOTES = "notes"
    TITLE_BLOCK = "title_block"
    LEGEND = "legend"
    UNKNOWN = "unknown"


class ReviewStatus(str, Enum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"   # merged into or split into other views; kept for history


class HintKind(str, Enum):
    """A proposed INTERPRETATION of an observation: a clue a structural engineer would want to look at. It is
    neither what the drawing shows (that is the observation) nor an approved fact (that is an engineer's
    decision and an EvidenceLink)."""
    COLUMN_CANDIDATE = "column_candidate"   # a column-like symbol: it MAY represent a column; that is not established
    BEAM_CANDIDATE = "beam_candidate"       # a beam-like symbol: it MAY represent a beam; that is not established
    STAIR_OPENING = "stair_opening"
    LIFT_SHAFT = "lift_shaft"
    LARGE_OPENING = "large_opening"
    DOUBLE_HEIGHT = "double_height"
    LONG_SPAN = "long_span"


@dataclass(frozen=True)
class UnitEstimate:
    unit: str
    factor_to_mm: float
    confidence: float
    method: str                         # e.g. "header_metadata", "extent_plausibility", "engineer"
    note: Optional[str] = None

    def __post_init__(self):
        if self.unit not in _UNITS:
            raise ValidationError(f"Unknown unit {self.unit!r}; expected one of {sorted(_UNITS)}.")
        check_positive(self.factor_to_mm, "factor_to_mm")
        check_confidence(self.confidence, "unit confidence")
        check_text(self.method, "unit method")
        check_optional_text(self.note, "unit note")

    def to_dict(self) -> dict:
        return {"unit": self.unit, "factor_to_mm": self.factor_to_mm, "confidence": self.confidence,
                "method": self.method, "note": self.note}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "UnitEstimate":
        check_keys(data, required={"unit", "factor_to_mm", "confidence", "method"}, optional={"note"}, where="units")
        return cls(data["unit"], data["factor_to_mm"], data["confidence"], data["method"], data.get("note"))


@dataclass(frozen=True)
class CoordinateFrame:
    """Maps this frame's coordinates to its parent's: parent = translation + R(rotation) * (scale * local),
    with local x flipped first if `mirrored`. The root frame (parent None) is the drawing's own coordinates."""

    id: str
    name: str
    parent: Optional[str] = None
    translation: tuple = (0.0, 0.0)
    rotation_deg: float = 0.0
    scale: float = 1.0
    mirrored: bool = False

    def __post_init__(self):
        check_kind_id("frame", self.id)
        check_text(self.name, "frame name")
        if self.parent is not None:
            check_kind_id("frame", self.parent)
        object.__setattr__(self, "translation", _points([self.translation], "frame translation")[0])
        check_number(self.rotation_deg, "frame rotation_deg")
        check_positive(self.scale, "frame scale")
        if not isinstance(self.mirrored, bool):
            raise ValidationError("frame mirrored must be true or false.")

    def to_parent(self, p: tuple) -> tuple:
        x, y = (-p[0] if self.mirrored else p[0]) * self.scale, p[1] * self.scale
        c, s = math.cos(math.radians(self.rotation_deg)), math.sin(math.radians(self.rotation_deg))
        return (self.translation[0] + c * x - s * y, self.translation[1] + s * x + c * y)

    def from_parent(self, p: tuple) -> tuple:
        x, y = p[0] - self.translation[0], p[1] - self.translation[1]
        c, s = math.cos(math.radians(self.rotation_deg)), math.sin(math.radians(self.rotation_deg))
        lx, ly = (c * x + s * y) / self.scale, (-s * x + c * y) / self.scale
        return (-lx if self.mirrored else lx, ly)

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "parent": self.parent, "translation": list(self.translation),
                "rotation_deg": self.rotation_deg, "scale": self.scale, "mirrored": self.mirrored}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CoordinateFrame":
        check_keys(data, required={"id", "name"}, optional={"parent", "translation", "rotation_deg", "scale", "mirrored"},
                   where="coordinate frame")
        return cls(data["id"], data["name"], data.get("parent"), tuple(data.get("translation", (0.0, 0.0))),
                   data.get("rotation_deg", 0.0), data.get("scale", 1.0), data.get("mirrored", False))


def _check_metadata(value: Any) -> dict:
    """Opaque source-format facts (a format version, the raw code a unit was declared with, the names of the
    layouts): JSON scalars or lists of scalars under text keys. The core stores them and never interprets them."""
    if not isinstance(value, Mapping):
        raise ValidationError("source_metadata must be an object.")
    out = {}
    for k, v in value.items():
        check_text(k, "source metadata key")
        items = v if isinstance(v, (list, tuple)) else [v]
        if not all(x is None or isinstance(x, (str, int, float, bool)) for x in items):
            raise ValidationError(f"source_metadata[{k!r}] must be a text, number, true/false or a list of them.")
        out[k] = list(v) if isinstance(v, (list, tuple)) else v
    return out


@dataclass(frozen=True)
class DrawingSource:
    """Which source, which revision, which bytes, and which interpretation of them. The identity is four separate
    things so several sources (an architectural drawing, a structural GA, a revised issue of either) and several
    interpretations of one source can live in one project: `id` names the source within the project,
    `revision` is the issue/revision the source declares, `sha256` pins the exact bytes, and `interpretation_id`
    names this run of the interpreter over them."""

    id: str
    file: str
    sha256: str
    units: UnitEstimate
    declared_unit: Optional[str] = None        # the unit the source says it uses (mm, cm, m, inch, foot), if it says
    extents: Optional[tuple] = None            # source units
    entity_count: int = 0
    warnings: tuple = ()
    revision: Optional[str] = None
    interpretation_id: str = "AINT-1"
    interpreter: Optional[str] = None          # what produced the interpretation, e.g. "oracle.interpretation 0.4"
    source_metadata: Any = None                # format-specific facts kept verbatim and never interpreted here

    def __post_init__(self):
        check_kind_id("drawing", self.id)
        check_text(self.file, "drawing file")
        if not re.fullmatch(r"[0-9a-f]{64}", self.sha256 or ""):
            raise ValidationError("sha256 must be 64 hex digits.")
        if not isinstance(self.units, UnitEstimate):
            raise ValidationError("units must be a UnitEstimate.")
        if self.declared_unit is not None and self.declared_unit not in _UNITS:
            raise ValidationError(f"declared_unit must be one of {sorted(_UNITS)} or absent, got {self.declared_unit!r}.")
        if self.extents is not None:
            object.__setattr__(self, "extents", check_bbox(self.extents, "extents"))
        if isinstance(self.entity_count, bool) or not isinstance(self.entity_count, int) or self.entity_count < 0:
            raise ValidationError("entity_count must be a non-negative whole number.")
        object.__setattr__(self, "warnings", tuple(check_text(x, "drawing warning") for x in self.warnings))
        check_optional_text(self.revision, "source revision")
        if not isinstance(self.interpretation_id, str) or not re.fullmatch(r"AINT-\d+", self.interpretation_id):
            raise ValidationError(f"Invalid interpretation id {self.interpretation_id!r}: expected AINT-<number>.")
        check_optional_text(self.interpreter, "interpreter")
        object.__setattr__(self, "source_metadata", _check_metadata(self.source_metadata or {}))

    def to_dict(self) -> dict:
        return {"id": self.id, "file": self.file, "sha256": self.sha256, "units": self.units.to_dict(),
                "declared_unit": self.declared_unit, "extents": list(self.extents) if self.extents else None,
                "entity_count": self.entity_count, "warnings": list(self.warnings), "revision": self.revision,
                "interpretation_id": self.interpretation_id, "interpreter": self.interpreter,
                "source_metadata": dict(self.source_metadata)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DrawingSource":
        check_keys(data, required={"id", "file", "sha256", "units"},
                   optional={"declared_unit", "extents", "entity_count", "warnings", "revision", "interpretation_id",
                             "interpreter", "source_metadata"}, where="drawing source")
        return cls(data["id"], data["file"], data["sha256"], UnitEstimate.from_dict(data["units"]),
                   data.get("declared_unit"), tuple(data["extents"]) if data.get("extents") else None,
                   data.get("entity_count", 0), tuple(data.get("warnings") or ()), data.get("revision"),
                   data.get("interpretation_id", "AINT-1"), data.get("interpreter"), data.get("source_metadata") or {})


@dataclass(frozen=True)
class LayerClassification:
    id: str
    name: str                       # the layer's name in the drawing (any text)
    semantic_class: str             # extensible vocabulary, e.g. "wall_external", "unknown_wall_related"
    confidence: float
    method: str                     # "configured_alias", "token_match", "geometry", "block_name", "ai_advice", "none"
    note: Optional[str] = None
    entity_count: int = 0

    def __post_init__(self):
        check_kind_id("layer", self.id)
        check_text(self.name, "layer name")
        check_vocab(self.semantic_class, "semantic class")
        check_confidence(self.confidence, "layer classification confidence")
        check_text(self.method, "layer classification method")
        check_optional_text(self.note, "layer classification note")
        if isinstance(self.entity_count, bool) or not isinstance(self.entity_count, int) or self.entity_count < 0:
            raise ValidationError("entity_count must be a non-negative whole number.")

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "semantic_class": self.semantic_class, "confidence": self.confidence,
                "method": self.method, "note": self.note, "entity_count": self.entity_count}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "LayerClassification":
        check_keys(data, required={"id", "name", "semantic_class", "confidence", "method"},
                   optional={"note", "entity_count"}, where="layer classification")
        return cls(data["id"], data["name"], data["semantic_class"], data["confidence"], data["method"],
                   data.get("note"), data.get("entity_count", 0))


@dataclass(frozen=True)
class DrawingView:
    id: str
    view_type: ViewType
    bbox: tuple                              # source units
    confidence: float
    title: Optional[str] = None              # the title text as read, never normalised away
    level_key: Optional[str] = None
    section_label: Optional[str] = None      # e.g. "A-A"
    orientation: Optional[str] = None        # for elevations, e.g. "north"
    frame_id: Optional[str] = None           # source -> view-local
    alignment_frame_id: Optional[str] = None  # view-local -> building (proposed)
    entity_ids: tuple = ()
    review: ReviewStatus = ReviewStatus.PROPOSED
    superseded_by: tuple = ()
    variant: Optional[str] = None            # e.g. "blowup", "furniture": another view of the same subject, not a rival

    def __post_init__(self):
        check_kind_id("view", self.id)
        object.__setattr__(self, "view_type", parse_enum(ViewType, self.view_type, "view type"))
        if self.variant is not None:
            check_vocab(self.variant, "view variant")
        object.__setattr__(self, "bbox", check_bbox(self.bbox))
        check_confidence(self.confidence, "view confidence")
        check_optional_text(self.title, "view title")
        if self.level_key is not None:
            check_level_key(self.level_key)
        check_optional_text(self.section_label, "section label")
        check_optional_text(self.orientation, "orientation")
        for f in (self.frame_id, self.alignment_frame_id):
            if f is not None:
                check_kind_id("frame", f)
        object.__setattr__(self, "entity_ids", tuple(check_text(e, "entity id") for e in self.entity_ids))
        object.__setattr__(self, "review", parse_enum(ReviewStatus, self.review, "review status"))
        object.__setattr__(self, "superseded_by", tuple(check_kind_id("view", v) for v in self.superseded_by))
        if (self.review == ReviewStatus.SUPERSEDED) != bool(self.superseded_by):
            raise ValidationError(f"View {self.id}: superseded_by must be set exactly when the review status is 'superseded'.")
        if self.level_key is not None and self.view_type != ViewType.FLOOR_PLAN:
            raise ValidationError(f"View {self.id}: only a floor plan carries a level key.")

    def to_dict(self) -> dict:
        return {"id": self.id, "view_type": self.view_type.value, "title": self.title, "level_key": self.level_key,
                "section_label": self.section_label, "orientation": self.orientation, "bbox": list(self.bbox),
                "confidence": self.confidence, "frame_id": self.frame_id, "alignment_frame_id": self.alignment_frame_id,
                "entity_ids": list(self.entity_ids), "review": self.review.value,
                "superseded_by": list(self.superseded_by), "variant": self.variant}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DrawingView":
        check_keys(data, required={"id", "view_type", "bbox", "confidence"},
                   optional={"title", "level_key", "section_label", "orientation", "frame_id", "alignment_frame_id",
                             "entity_ids", "review", "superseded_by", "variant"}, where="view")
        return cls(data["id"], data["view_type"], tuple(data["bbox"]), data["confidence"], data.get("title"),
                   data.get("level_key"), data.get("section_label"), data.get("orientation"), data.get("frame_id"),
                   data.get("alignment_frame_id"), tuple(data.get("entity_ids") or ()), data.get("review", "proposed"),
                   tuple(data.get("superseded_by") or ()), data.get("variant"))


@dataclass(frozen=True)
class ArchitecturalObservation:
    """Something the drawing appears to show (a wall, a door, a stair enclosure...). Not a structural fact."""

    id: str
    kind: str                                # extensible vocabulary, e.g. "wall_external", "door", "stair"
    view_id: str
    basis: ValueStatus                       # SOURCE (drawn as such) or INFERRED
    confidence: float
    layer: Optional[str] = None
    entity_ids: tuple = ()
    geometry: tuple = ()                     # points in source units
    closed: bool = False
    label: Optional[str] = None
    count: int = 1                           # >1 for aggregated linework (one record for many entities)
    hint: Optional[HintKind] = None
    review: ReviewStatus = ReviewStatus.PROPOSED

    def __post_init__(self):
        check_kind_id("observation", self.id)
        check_vocab(self.kind, "observation kind")
        check_kind_id("view", self.view_id)
        object.__setattr__(self, "basis", parse_enum(ValueStatus, self.basis, "observation basis"))
        if self.basis not in (ValueStatus.SOURCE, ValueStatus.INFERRED, ValueStatus.ASSUMED):
            raise ValidationError("An observation's basis must be source, inferred or assumed.")
        check_confidence(self.confidence, "observation confidence")
        check_optional_text(self.layer, "observation layer")
        object.__setattr__(self, "entity_ids", tuple(check_text(e, "entity id") for e in self.entity_ids))
        object.__setattr__(self, "geometry", _points(self.geometry, "observation geometry"))
        if not isinstance(self.closed, bool):
            raise ValidationError("closed must be true or false.")
        check_optional_text(self.label, "observation label")
        if isinstance(self.count, bool) or not isinstance(self.count, int) or self.count < 1:
            raise ValidationError("observation count must be a positive whole number.")
        if self.hint is not None:
            object.__setattr__(self, "hint", parse_enum(HintKind, self.hint, "structural hint"))
        object.__setattr__(self, "review", parse_enum(ReviewStatus, self.review, "review status"))

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "view_id": self.view_id, "basis": self.basis.value,
                "confidence": self.confidence, "layer": self.layer, "entity_ids": list(self.entity_ids),
                "geometry": [list(p) for p in self.geometry], "closed": self.closed, "label": self.label,
                "count": self.count, "hint": self.hint.value if self.hint else None, "review": self.review.value}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ArchitecturalObservation":
        check_keys(data, required={"id", "kind", "view_id", "basis", "confidence"},
                   optional={"layer", "entity_ids", "geometry", "closed", "label", "count", "hint", "review"},
                   where="observation")
        return cls(data["id"], data["kind"], data["view_id"], data["basis"], data["confidence"], data.get("layer"),
                   tuple(data.get("entity_ids") or ()), tuple(tuple(p) for p in data.get("geometry") or ()),
                   data.get("closed", False), data.get("label"), data.get("count", 1), data.get("hint"),
                   data.get("review", "proposed"))


@dataclass(frozen=True)
class HeightEvidence:
    """One piece of evidence for the height between two levels, kept with its source, never merged silently."""

    id: str
    from_level: str
    to_level: str
    height_mm: float
    source: str                              # "section_dimension", "section_lines", "plan_text", "elevation_bands"
    basis: ValueStatus
    confidence: float
    view_id: Optional[str] = None

    def __post_init__(self):
        check_kind_id("height", self.id)
        check_level_key(self.from_level, "from_level")
        check_level_key(self.to_level, "to_level")
        if self.from_level == self.to_level:
            raise ValidationError(f"Height evidence {self.id}: from_level and to_level are the same.")
        check_positive(self.height_mm, "height_mm")
        check_vocab(self.source, "height source")
        object.__setattr__(self, "basis", parse_enum(ValueStatus, self.basis, "height basis"))
        check_confidence(self.confidence, "height confidence")
        if self.view_id is not None:
            check_kind_id("view", self.view_id)

    def to_dict(self) -> dict:
        return {"id": self.id, "from_level": self.from_level, "to_level": self.to_level, "height_mm": self.height_mm,
                "source": self.source, "basis": self.basis.value, "confidence": self.confidence,
                "view_id": self.view_id}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "HeightEvidence":
        check_keys(data, required={"id", "from_level", "to_level", "height_mm", "source", "basis", "confidence"},
                   optional={"view_id"}, where="height evidence")
        return cls(data["id"], data["from_level"], data["to_level"], data["height_mm"], data["source"], data["basis"],
                   data["confidence"], data.get("view_id"))


@dataclass(frozen=True)
class CrossViewFinding:
    id: str
    question: str
    agreement: str                           # "agree", "disagree" or "insufficient"
    summary: str
    views: tuple = ()
    interpretation_set_id: Optional[str] = None

    def __post_init__(self):
        check_kind_id("finding", self.id)
        check_text(self.question, "finding question")
        if self.agreement not in ("agree", "disagree", "insufficient"):
            raise ValidationError(f"Finding agreement must be agree, disagree or insufficient, got {self.agreement!r}.")
        check_text(self.summary, "finding summary")
        object.__setattr__(self, "views", tuple(check_kind_id("view", v) for v in self.views))
        if self.interpretation_set_id is not None:
            check_id(self.interpretation_set_id, "interpretation_set_id")

    def to_dict(self) -> dict:
        return {"id": self.id, "question": self.question, "agreement": self.agreement, "summary": self.summary,
                "views": list(self.views), "interpretation_set_id": self.interpretation_set_id}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CrossViewFinding":
        check_keys(data, required={"id", "question", "agreement", "summary"},
                   optional={"views", "interpretation_set_id"}, where="cross-view finding")
        return cls(data["id"], data["question"], data["agreement"], data["summary"], tuple(data.get("views") or ()),
                   data.get("interpretation_set_id"))


_COLLECTIONS = {"frames": CoordinateFrame, "layers": LayerClassification, "views": DrawingView,
                "observations": ArchitecturalObservation, "heights": HeightEvidence, "findings": CrossViewFinding}
_KIND_OF = {CoordinateFrame: "frame", LayerClassification: "layer", DrawingView: "view",
            ArchitecturalObservation: "observation", HeightEvidence: "height", CrossViewFinding: "finding"}


class ArchitecturalInterpretation:
    """One drawing's interpretation. Objects are immutable; a change replaces the object (replace())."""

    def __init__(self, drawing: DrawingSource):
        if not isinstance(drawing, DrawingSource):
            raise ValidationError("An interpretation needs a DrawingSource.")
        self.drawing = drawing
        self._items = {name: {} for name in _COLLECTIONS}
        self._all: dict = {drawing.id: drawing}

    # ---- lookup
    def get(self, object_id: str):
        try:
            return self._all[object_id]
        except KeyError:
            raise ValidationError(f"Unknown architectural object {object_id!r}.") from None

    def has(self, object_id: str) -> bool:
        return object_id in self._all

    @property
    def frames(self) -> list: return list(self._items["frames"].values())
    @property
    def layers(self) -> list: return list(self._items["layers"].values())
    @property
    def views(self) -> list: return list(self._items["views"].values())
    @property
    def observations(self) -> list: return list(self._items["observations"].values())
    @property
    def heights(self) -> list: return list(self._items["heights"].values())
    @property
    def findings(self) -> list: return list(self._items["findings"].values())

    def views_of(self, view_type: ViewType, *, include_superseded: bool = False) -> list:
        return [v for v in self.views if v.view_type == view_type
                and (include_superseded or v.review != ReviewStatus.SUPERSEDED)]

    def observations_in(self, view_id: str) -> list:
        return [o for o in self.observations if o.view_id == view_id]

    # ---- change
    def add(self, obj):
        name = next((n for n, c in _COLLECTIONS.items() if type(obj) is c), None)
        if name is None:
            raise ValidationError(f"Cannot add {type(obj).__name__} to an interpretation.")
        if obj.id in self._all:
            raise ValidationError(f"Duplicate architectural id {obj.id!r}.")
        self._check_refs(obj)
        if name == "layers" and any(l.name == obj.name for l in self.layers):
            raise ValidationError(f"Duplicate layer classification for {obj.name!r}.")
        self._items[name][obj.id] = obj
        self._all[obj.id] = obj
        if name == "frames":
            self._check_frame_tree()
        return obj

    def replace(self, obj):
        """Swap in a changed version of an existing object (same id, same type)."""
        current = self.get(obj.id)
        if type(current) is not type(obj):
            raise ValidationError(f"{obj.id} is a {type(current).__name__}, not a {type(obj).__name__}.")
        if isinstance(obj, DrawingSource):
            self.drawing = obj
            self._all[obj.id] = obj
            return obj
        self._check_refs(obj)
        name = next(n for n, c in _COLLECTIONS.items() if type(obj) is c)
        self._items[name][obj.id] = obj
        self._all[obj.id] = obj
        if name == "frames":
            self._check_frame_tree()
        return obj

    def discard_frame(self, frame_id: str) -> None:
        """Remove a coordinate frame that nothing refers to yet (used to roll back a refused alignment)."""
        used = [o.id for o in list(self.views) + list(self.frames) if getattr(o, "frame_id", None) == frame_id
                or getattr(o, "alignment_frame_id", None) == frame_id or getattr(o, "parent", None) == frame_id]
        if used:
            raise ValidationError(f"Frame {frame_id} is still used by {used[:3]}.")
        self._items["frames"].pop(frame_id, None)
        self._all.pop(frame_id, None)

    def _check_refs(self, obj) -> None:
        def need(object_id, kind):
            if object_id is not None and not (self.has(object_id) and _KIND_OF.get(type(self._all[object_id])) == kind):
                raise ValidationError(f"{obj.id} refers to unknown {kind} {object_id!r}.")
        if isinstance(obj, CoordinateFrame):
            need(obj.parent, "frame")
        elif isinstance(obj, DrawingView):
            need(obj.frame_id, "frame")
            need(obj.alignment_frame_id, "frame")
        elif isinstance(obj, ArchitecturalObservation):
            need(obj.view_id, "view")
        elif isinstance(obj, HeightEvidence):
            need(obj.view_id, "view")
        elif isinstance(obj, CrossViewFinding):
            for v in obj.views:
                need(v, "view")

    def _check_frame_tree(self) -> None:
        for f in self.frames:
            seen, cur = set(), f
            while cur.parent is not None:
                if cur.id in seen:
                    raise ValidationError(f"Coordinate frames form a loop at {cur.id}.")
                seen.add(cur.id)
                cur = self._items["frames"].get(cur.parent)
                if cur is None:
                    raise ValidationError(f"Frame {f.id} has a broken parent chain.")

    def validate(self) -> None:
        for name in _COLLECTIONS:
            for obj in self._items[name].values():
                self._check_refs(obj)
        self._check_frame_tree()
        for v in self.views:
            for sup in v.superseded_by:
                if not self.has(sup):
                    raise ValidationError(f"View {v.id} is superseded by unknown view {sup!r}.")

    # ---- coordinates
    def _chain(self, frame_id: str) -> list:
        chain, cur = [], self._items["frames"].get(frame_id)
        if cur is None:
            raise ValidationError(f"Unknown frame {frame_id!r}.")
        while cur is not None:
            chain.append(cur)
            cur = self._items["frames"].get(cur.parent) if cur.parent else None
        return chain

    def to_source(self, frame_id: str, point: tuple) -> tuple:
        """A point in `frame_id` coordinates, expressed in the drawing's own (source) coordinates."""
        for frame in self._chain(frame_id):
            point = frame.to_parent(point)
        return point

    def from_source(self, frame_id: str, point: tuple) -> tuple:
        """A point in the drawing's own coordinates, expressed in `frame_id` coordinates."""
        for frame in reversed(self._chain(frame_id)):
            point = frame.from_parent(point)
        return point

    def source_to_millimetres(self, value: float) -> float:
        return value * self.drawing.units.factor_to_mm

    # ---- serialisation
    def to_dict(self) -> dict:
        out = {"drawing": self.drawing.to_dict()}
        for name in _COLLECTIONS:
            out[name] = [o.to_dict() for o in self._items[name].values()]
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ArchitecturalInterpretation":
        check_keys(data, required={"drawing", *_COLLECTIONS}, where="architecture")
        arch = cls(DrawingSource.from_dict(data["drawing"]))
        for name, klass in _COLLECTIONS.items():
            rows = data[name]
            if not isinstance(rows, list):
                raise ValidationError(f"architecture {name} must be a list.")
            objs = [klass.from_dict(r) for r in rows]
            check_unique_ids((o.id for o in objs), f"{name} id")
            arch._items[name] = {o.id: o for o in objs}
        arch._all = {arch.drawing.id: arch.drawing}
        for name in _COLLECTIONS:
            for obj in arch._items[name].values():
                if obj.id in arch._all:
                    raise ValidationError(f"Duplicate architectural id {obj.id!r}.")
                arch._all[obj.id] = obj
        if len({l.name for l in arch.layers}) != len(arch.layers):
            raise ValidationError("Duplicate layer classification names.")
        arch.validate()
        return arch
