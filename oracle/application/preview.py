"""Oracle — Drawing Preview Model (application layer)

Purpose:
    Prepares the geometry Oracle already holds for drawing on screen: the neutral DrawingDocument that the ingestion layer produced and
    the interpreter used. Each drawn entity becomes a small path (a line, a polyline, a circle or arc approximated by short segments,
    a block's footprint) in the drawing's OWN coordinates, with a bounding box and a spatial grid so that a view can ask for just the
    paths that fall inside the visible rectangle. It also resolves what a review selection means on the drawing: the rectangle of a
    view, the outline and source entities of an observation, or every object an unresolved question or an issue is about.

Role in Oracle:
    The data behind the drawing preview. It is the same normalised geometry the interpretation ran on: there is no second DXF parser,
    no CAD library is imported, and nothing here changes the drawing. Text and dimension entities are kept as labels, not drawn as
    linework; a block is drawn as its footprint (blocks are not expanded, exactly as in interpretation).

Dependencies:
    oracle.ingestion (the neutral DrawingDocument only); standard library.

Consumers:
    oracle.application.session, oracle.ui.preview_canvas, tests.

Status:
    Application layer (interface phase).

Migration/Notes:
    Level of detail is the canvas's business (it skips paths smaller than a pixel); this module is exact and toolkit-free. For a
    reopened project the document comes from the geometry cache saved beside it, or is re-read from the drawing file on request.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Optional

from oracle.ingestion import DrawingDocument


@dataclass(frozen=True)
class DrawnPath:
    entity_id: str
    layer: str
    points: tuple
    closed: bool
    box: tuple


@dataclass
class Overlay:
    """What a selection means on the drawing, in source coordinates. `style` names how the canvas emphasises it."""

    rects: list = field(default_factory=list)          # (box, style, label, ref): ref is the id of the object the rectangle stands for
    entity_ids: list = field(default_factory=list)     # source entities to emphasise
    polylines: list = field(default_factory=list)      # (points, style) from stored observation geometry
    focus: Optional[tuple] = None                      # the box to bring into view, if any

    def merge(self, other: "Overlay") -> "Overlay":
        self.rects += other.rects
        self.entity_ids += [e for e in other.entity_ids if e not in self.entity_ids]
        self.polylines += other.polylines
        self.focus = union_box([b for b in (self.focus, other.focus) if b])
        return self


def union_box(boxes: Iterable) -> Optional[tuple]:
    boxes = [b for b in boxes if b]
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def _arc_points(centre, radius, start_deg, end_deg, steps_per_circle=48):
    sweep = (end_deg - start_deg) % 360.0 or 360.0
    n = max(3, int(steps_per_circle * sweep / 360.0))
    return tuple((centre[0] + radius * math.cos(math.radians(start_deg + sweep * i / n)),
                  centre[1] + radius * math.sin(math.radians(start_deg + sweep * i / n))) for i in range(n + 1))


def _box(points) -> tuple:
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


class DrawingPreview:
    """Paths of one drawing in its own coordinates, with a spatial grid for fast 'what is in this rectangle'."""

    def __init__(self, document: DrawingDocument):
        self.source_file = document.source_file
        self.sha256 = document.sha256
        self.paths: list = []
        self.texts: list = []                      # (x, y, text, height) for labels at high zoom
        self._index: dict = {}
        self._by_entity: dict = {}
        self.entity_boxes: dict = {e.id: e.box for e in document.entities if e.space == "model"}
        for e in document.entities:
            if e.space != "model":
                continue
            pts = self._points_of(e)
            if e.kind == "TEXT" and e.text and e.points:
                self.texts.append((e.points[0][0], e.points[0][1], e.text, e.height or 0.0))
            if pts is None or len(pts) < 2:
                continue
            path = DrawnPath(e.id, e.layer, tuple(pts), bool(e.closed or e.kind in ("CIRCLE", "INSERT")), _box(pts))
            self._by_entity.setdefault(e.id, []).append(len(self.paths))
            self.paths.append(path)
        boxes = [p.box for p in self.paths]
        self.bounds = union_box(boxes)
        self._build_grid()

    @staticmethod
    def _points_of(e):
        if e.kind in ("LINE", "POLYLINE", "SPLINE"):
            return list(e.points)
        if e.kind == "CIRCLE" and e.points and e.radius:
            return list(_arc_points(e.points[0], e.radius, 0.0, 360.0))
        if e.kind == "ARC" and e.points and e.radius:
            return list(_arc_points(e.points[0], e.radius, e.rotation or 0.0, e.value if e.value is not None else 360.0))
        if e.kind == "INSERT" and e.box[2] > e.box[0] and e.box[3] > e.box[1]:
            b = e.box
            return [(b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3]), (b[0], b[1])]
        return None

    def _build_grid(self) -> None:
        if not self.paths:
            self._cell = 1.0
            return
        x0, y0, x1, y1 = self.bounds
        n = max(4, min(96, int(math.sqrt(len(self.paths)) / 2) + 1))
        self._cell = max((x1 - x0) / n, (y1 - y0) / n, 1e-9)
        self._origin = (x0, y0)
        for i, p in enumerate(self.paths):
            for cx in range(int((p.box[0] - x0) / self._cell), int((p.box[2] - x0) / self._cell) + 1):
                for cy in range(int((p.box[1] - y0) / self._cell), int((p.box[3] - y0) / self._cell) + 1):
                    self._index.setdefault((cx, cy), []).append(i)

    def visible(self, box: tuple) -> list:
        """Indices of paths whose box touches `box`."""
        if not self.paths:
            return []
        x0, y0 = self._origin
        seen, out = set(), []
        for cx in range(max(0, int((box[0] - x0) / self._cell)), int((box[2] - x0) / self._cell) + 1):
            for cy in range(max(0, int((box[1] - y0) / self._cell)), int((box[3] - y0) / self._cell) + 1):
                for i in self._index.get((cx, cy), ()):
                    if i not in seen:
                        seen.add(i)
                        b = self.paths[i].box
                        if b[0] <= box[2] and b[2] >= box[0] and b[1] <= box[3] and b[3] >= box[1]:
                            out.append(i)
        return out

    def paths_for(self, entity_ids: Iterable) -> list:
        return [i for eid in entity_ids for i in self._by_entity.get(eid, ())]

    def box_of_entities(self, entity_ids: Iterable) -> Optional[tuple]:
        return union_box(self.entity_boxes[e] for e in entity_ids if e in self.entity_boxes)

    def __len__(self) -> int:
        return len(self.paths)
