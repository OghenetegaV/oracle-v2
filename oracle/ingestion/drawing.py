"""Oracle — Neutral Drawing Document

Purpose:
    DrawnEntity, ViewportInfo, LayerInfo and DrawingDocument: a plain, library-independent picture of
    what a CAD file contains. Each entity keeps its layer, its own handle-based ID, its points in the
    file's coordinates, and (where it has them) text, block name, radius, dimension value, rotation and
    text height, plus a bounding box. Nothing here imports ezdxf. This module is also where the FORMAT's own facts are
    translated into domain words before anyone else sees them: the raw unit code becomes `declared_unit` (mm, cm, m,
    inch, foot or None), a layer the source marks as not plotting becomes `LayerInfo.non_plotting`, and the remaining
    format-specific facts are offered as an opaque `source_metadata()` for the record.

Role in Oracle:
    The boundary between "reading a CAD file" (oracle.ingestion.dxf_reader, which needs a CAD library) and
    "interpreting a drawing" (oracle.interpretation, which must work on any source that can produce this
    document, including hand-built ones in tests). Coordinates stay in the file's own units and are never
    altered; unit meaning is a separate, uncertain question answered by the interpreter. The document can
    be saved and reloaded as JSON, so an expensive read (a 65 MB DXF takes about a minute) can be cached.

Dependencies:
    Standard library only.

Consumers:
    oracle.ingestion.dxf_reader (produces it); oracle.interpretation (consumes it); tests.

Status:
    Ingestion (Phase 3; boundary translation added in Phase 3.5).

Migration/Notes:
    Entity kinds are plain text: LINE, POLYLINE, CIRCLE, ARC, TEXT, DIMENSION, INSERT, HATCH, SPLINE. A
    reader for another format (IFC, PDF vector data) would fill the same structure.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

KINDS = ("LINE", "POLYLINE", "CIRCLE", "ARC", "TEXT", "DIMENSION", "INSERT", "HATCH", "SPLINE")


# DXF $INSUNITS codes that name a unit Oracle works with. Everything else (unitless, miles, ...) is "no usable unit".
_DECLARED_UNITS = {1: "inch", 2: "foot", 4: "mm", 5: "cm", 6: "m"}


@dataclass(frozen=True)
class DrawnEntity:
    id: str
    kind: str
    layer: str
    box: tuple                          # (min_x, min_y, max_x, max_y) in file units
    points: tuple = ()                  # vertices / end points / insertion point / defpoints
    closed: bool = False
    text: Optional[str] = None
    block: Optional[str] = None
    radius: Optional[float] = None
    value: Optional[float] = None       # a dimension's measured value, in file units
    rotation: float = 0.0               # degrees; a dimension's direction, a text's angle, an insert's rotation
    height: Optional[float] = None      # text height
    space: str = "model"

    @property
    def centre(self) -> tuple:
        return ((self.box[0] + self.box[2]) / 2.0, (self.box[1] + self.box[3]) / 2.0)

    @property
    def width(self) -> float:
        return self.box[2] - self.box[0]

    @property
    def depth(self) -> float:
        return self.box[3] - self.box[1]

    def to_dict(self) -> dict:
        row = {"id": self.id, "kind": self.kind, "layer": self.layer, "box": list(self.box)}
        if self.points:
            row["points"] = [list(p) for p in self.points]
        for name in ("closed", "text", "block", "radius", "value", "rotation", "height"):
            v = getattr(self, name)
            if v not in (None, False, 0.0):
                row[name] = v
        if self.space != "model":
            row["space"] = self.space
        return row

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "DrawnEntity":
        return cls(d["id"], d["kind"], d["layer"], tuple(d["box"]), tuple(tuple(p) for p in d.get("points", ())),
                   d.get("closed", False), d.get("text"), d.get("block"), d.get("radius"), d.get("value"),
                   d.get("rotation", 0.0), d.get("height"), d.get("space", "model"))


@dataclass(frozen=True)
class LayerInfo:
    name: str
    color: Optional[int] = None
    frozen: bool = False
    off: bool = False
    non_plotting: bool = False          # the source says this layer is not part of the printed drawing (a CAD convention, decided here)


@dataclass(frozen=True)
class ViewportInfo:
    layout: str
    box: tuple                          # the viewport's frame on the sheet
    model_centre: tuple                 # what part of model space it shows
    model_height: float


@dataclass
class DrawingDocument:
    source_file: str
    sha256: str
    format_version: Optional[str] = None
    unit_code: Optional[int] = None     # the file's own unit metadata, exactly as stored
    extents: Optional[tuple] = None     # from the file header, which may be missing or wrong
    layers: dict = field(default_factory=dict)      # name -> LayerInfo
    entities: list = field(default_factory=list)
    viewports: list = field(default_factory=list)
    layouts: tuple = ()
    block_count: int = 0
    warnings: list = field(default_factory=list)
    skipped: dict = field(default_factory=dict)     # entity type -> how many could not be read

    @property
    def declared_unit(self) -> Optional[str]:
        """The unit the file says it uses, in domain words (mm, cm, m, inch, foot), or None when it says nothing usable.
        The raw format code stays in `unit_code`; translating it is this layer's job, so nothing downstream sees it."""
        return _DECLARED_UNITS.get(self.unit_code) if self.unit_code else None

    def source_metadata(self) -> dict:
        """Format-specific facts kept verbatim for the record. The domain model stores them and never interprets them."""
        out = {}
        if self.format_version:
            out["format_version"] = self.format_version
        if self.unit_code is not None:
            out["unit_code"] = self.unit_code
        if self.layouts:
            out["layouts"] = list(self.layouts)
        return out

    def by_kind(self, kind: str) -> list:
        return [e for e in self.entities if e.kind == kind]

    def model_entities(self) -> list:
        return [e for e in self.entities if e.space == "model"]

    def geometry_box(self) -> Optional[tuple]:
        """The box around all drawn entities (not the header's claim)."""
        boxes = [e.box for e in self.entities if e.space == "model"]
        if not boxes:
            return None
        return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))

    def to_dict(self) -> dict:
        return {"source_file": self.source_file, "sha256": self.sha256, "format_version": self.format_version,
                "unit_code": self.unit_code, "extents": list(self.extents) if self.extents else None,
                "layers": [[l.name, l.color, l.frozen, l.off, l.non_plotting] for l in self.layers.values()],
                "entities": [e.to_dict() for e in self.entities],
                "viewports": [[v.layout, list(v.box), list(v.model_centre), v.model_height] for v in self.viewports],
                "layouts": list(self.layouts), "block_count": self.block_count, "warnings": list(self.warnings),
                "skipped": dict(self.skipped)}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "DrawingDocument":
        doc = cls(d["source_file"], d["sha256"], d.get("format_version"), d.get("unit_code"),
                  tuple(d["extents"]) if d.get("extents") else None)
        doc.layers = {row[0]: LayerInfo(*row) for row in d.get("layers", ())}       # older caches have four fields per layer
        doc.entities = [DrawnEntity.from_dict(e) for e in d.get("entities", ())]
        doc.viewports = [ViewportInfo(l, tuple(b), tuple(c), h) for l, b, c, h in d.get("viewports", ())]
        doc.layouts = tuple(d.get("layouts", ()))
        doc.block_count = d.get("block_count", 0)
        doc.warnings = list(d.get("warnings", ()))
        doc.skipped = dict(d.get("skipped", {}))
        return doc


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def box_of(points, radius: float = 0.0) -> tuple:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    if not xs or not all(math.isfinite(v) for v in xs + ys):
        raise ValueError("no finite points")
    return (min(xs) - radius, min(ys) - radius, max(xs) + radius, max(ys) + radius)
