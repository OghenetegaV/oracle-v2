"""Oracle — Core Provenance

Purpose:
    ProvenanceRecord and SourceReference: a persistent, append-only record of where an object, or
    one field of it, came from. A SourceReference describes the evidence (file, entity handle,
    layer, entity type, the producer's own identifier such as a member number, coordinates, the
    floor/plan it sat on); a ProvenanceRecord ties it to a target, says how it was extracted and how
    confident the producer was, and when.

Role in Oracle:
    The SOURCE FACT layer of the chain source fact -> interpretation -> engineering decision ->
    design result. Records are project-level, keyed by (target, field), not fields on Column or
    Beam, so element classes stay free of source detail and any importer can supply evidence. A
    field of None means the object as a whole; the reserved field name 'geometry' means its position
    or shape as a whole (a beam's geometry is its two nodes, a slab's is its boundary).

    Records say where information came from. Whether the current value is trusted, assumed or
    confirmed is a different question, answered by oracle.core.value_status.

    The core stays CAD-neutral: 'layer', 'entity_handle' and 'entity_type' are optional descriptive
    strings, not DXF concepts, and nothing here imports a parser.

Dependencies:
    oracle.core.common (Target and the validators). Standard library only.

Consumers:
    oracle.core.project (stores, cross-checks and serialises records); oracle.core.value_status,
    oracle.core.interpretations and oracle.core.issues (refer to records by ID); adapters.

Status:
    Core (schema 0.2.0).

Migration/Notes:
    New in schema 0.2.0; projects migrated from 0.1.0 start with none, and none are invented for them.
    Records are never edited or deleted, so the evidence trail survives later engineer overrides.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from .common import (
    Target, TargetScope, ValidationError, check_confidence, check_field_path, check_id, check_keys, check_number,
    check_optional_text, check_text, check_timestamp, utc_now_iso,
)


@dataclass(frozen=True)
class SourceReference:
    """One piece of source evidence. At least one field must be given."""

    file: Optional[str] = None            # source file the evidence lives in
    entity_handle: Optional[str] = None   # the source system's own handle for the entity
    layer: Optional[str] = None
    entity_type: Optional[str] = None     # e.g. "LWPOLYLINE", "closed outline"
    source_id: Optional[str] = None       # the producer's own identifier, e.g. "member 12", "joint 44"
    coordinates: tuple = ()               # ((x, y) or (x, y, z), ...) in the source's own coordinate frame
    coordinate_frame: Optional[str] = None  # what those coordinates are relative to, and their unit
    context: Optional[str] = None         # floor / plan / view the evidence sat in

    def __post_init__(self):
        for name in ("file", "entity_handle", "layer", "entity_type", "source_id", "coordinate_frame", "context"):
            check_optional_text(getattr(self, name), f"source {name}")
        coords = []
        for point in self.coordinates:
            if not isinstance(point, (list, tuple)) or len(point) not in (2, 3):
                raise ValidationError(f"A source coordinate must be [x, y] or [x, y, z], got {point!r}.")
            coords.append(tuple(check_number(v, "source coordinate") for v in point))
        object.__setattr__(self, "coordinates", tuple(coords))
        if self.coordinates and not self.coordinate_frame:
            raise ValidationError("Source coordinates need a coordinate_frame saying what they are relative to.")
        if not any((self.file, self.entity_handle, self.layer, self.entity_type, self.source_id, self.coordinates,
                    self.context)):
            raise ValidationError("A source reference must identify its evidence: give at least one of file, "
                                  "entity_handle, layer, entity_type, source_id, coordinates or context.")

    def to_dict(self) -> dict:
        return {"file": self.file, "entity_handle": self.entity_handle, "layer": self.layer,
                "entity_type": self.entity_type, "source_id": self.source_id,
                "coordinates": [list(p) for p in self.coordinates], "coordinate_frame": self.coordinate_frame,
                "context": self.context}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SourceReference":
        check_keys(data, required=set(), optional={"file", "entity_handle", "layer", "entity_type", "source_id",
                                                   "coordinates", "coordinate_frame", "context"},
                   where="source reference")
        kwargs = dict(data)
        kwargs["coordinates"] = tuple(kwargs.get("coordinates") or ())
        return cls(**kwargs)


@dataclass
class ProvenanceRecord:
    id: str
    target: Target
    source: SourceReference
    method: str                            # how it was obtained, e.g. "polyline segment", "outline centroid"
    producer: str                          # which system produced it, e.g. "ga_dxf_parser"
    field: Optional[str] = None            # None = the object as a whole; else e.g. 'section', 'geometry'
    confidence: Optional[float] = None     # the producer's own confidence, if it has one
    recorded_at: str = ""
    note: Optional[str] = None

    def __post_init__(self):
        check_id(self.id, "provenance id")
        if not isinstance(self.target, Target):
            raise ValidationError("Provenance target must be a Target.")
        if self.target.scope == TargetScope.PROJECT:
            raise ValidationError("Provenance must be about a level, element, node or grid line, not the whole project.")
        if not isinstance(self.source, SourceReference):
            raise ValidationError("Provenance source must be a SourceReference.")
        check_text(self.method, "provenance method")
        check_text(self.producer, "provenance producer")
        if self.field is not None:
            check_field_path(self.field, "provenance field")
        if self.confidence is not None:
            check_confidence(self.confidence, "provenance confidence")
        self.recorded_at = check_timestamp(self.recorded_at or utc_now_iso(), "provenance recorded_at")
        check_optional_text(self.note, "provenance note")

    def to_dict(self) -> dict:
        return {"id": self.id, "target": self.target.to_dict(), "field": self.field, "source": self.source.to_dict(),
                "method": self.method, "producer": self.producer, "confidence": self.confidence,
                "recorded_at": self.recorded_at, "note": self.note}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ProvenanceRecord":
        check_keys(data, required={"id", "target", "source", "method", "producer", "recorded_at"},
                   optional={"field", "confidence", "note"}, where="provenance record")
        return cls(id=data["id"], target=Target.from_dict(data["target"]), source=SourceReference.from_dict(data["source"]),
                   method=data["method"], producer=data["producer"], field=data.get("field"),
                   confidence=data.get("confidence"), recorded_at=data["recorded_at"], note=data.get("note"))
