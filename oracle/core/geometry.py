"""Oracle — Core Plan Geometry

Purpose:
    Point2D and Polygon2D, in millimetres. Polygon2D rejects too few vertices, coincident
    consecutive vertices, zero area and self-intersection.

Role in Oracle:
    Structured geometry for slabs, openings, stairs, foundations, grid lines and column
    locations, so geometry is never stored as arbitrary strings.

Dependencies:
    oracle.core.common.

Consumers:
    oracle.core.elements, oracle.core.building.

Status:
    Core.

Migration:
    Remains. 3D and curved geometry are not modelled yet.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

from .common import POSITION_TOL_MM, ValidationError, check_number


@dataclass(frozen=True)
class Point2D:
    x_mm: float
    y_mm: float

    def __post_init__(self):
        check_number(self.x_mm, "x_mm")
        check_number(self.y_mm, "y_mm")

    def distance_to(self, other: "Point2D") -> float:
        return math.hypot(self.x_mm - other.x_mm, self.y_mm - other.y_mm)

    def to_list(self) -> list:
        return [self.x_mm, self.y_mm]

    @classmethod
    def from_list(cls, data: Any) -> "Point2D":
        if not isinstance(data, (list, tuple)) or len(data) != 2:
            raise ValidationError(f"A point must be [x_mm, y_mm], got {data!r}.")
        return cls(data[0], data[1])


def _orient(a: Point2D, b: Point2D, c: Point2D) -> float:
    return (b.x_mm - a.x_mm) * (c.y_mm - a.y_mm) - (b.y_mm - a.y_mm) * (c.x_mm - a.x_mm)


def _segments_intersect(p1: Point2D, p2: Point2D, p3: Point2D, p4: Point2D) -> bool:
    """True if the closed segments p1-p2 and p3-p4 share any point."""
    d1, d2 = _orient(p3, p4, p1), _orient(p3, p4, p2)
    d3, d4 = _orient(p1, p2, p3), _orient(p1, p2, p4)
    if ((d1 > 0 > d2) or (d1 < 0 < d2)) and ((d3 > 0 > d4) or (d3 < 0 < d4)):
        return True

    def on_segment(a, b, c):  # c collinear with a-b: is it within the box?
        return (min(a.x_mm, b.x_mm) <= c.x_mm <= max(a.x_mm, b.x_mm)
                and min(a.y_mm, b.y_mm) <= c.y_mm <= max(a.y_mm, b.y_mm))

    return ((d1 == 0 and on_segment(p3, p4, p1)) or (d2 == 0 and on_segment(p3, p4, p2))
            or (d3 == 0 and on_segment(p1, p2, p3)) or (d4 == 0 and on_segment(p1, p2, p4)))


@dataclass(frozen=True)
class Polygon2D:
    """A simple (non-self-intersecting) closed plan region. The closing edge back to the
    first vertex is implied -- do not repeat the first vertex at the end."""

    vertices: tuple

    def __post_init__(self):
        object.__setattr__(self, "vertices", tuple(self.vertices))
        verts = self.vertices
        if len(verts) < 3:
            raise ValidationError(f"A polygon needs at least 3 vertices, got {len(verts)}.")
        if not all(isinstance(v, Point2D) for v in verts):
            raise ValidationError("Polygon vertices must be Point2D values.")
        n = len(verts)
        for i in range(n):
            if verts[i].distance_to(verts[(i + 1) % n]) <= POSITION_TOL_MM:
                raise ValidationError(
                    f"Polygon has coincident consecutive vertices at index {i} "
                    "(a repeated closing vertex is not allowed)."
                )
        if abs(self.signed_area_mm2()) <= POSITION_TOL_MM ** 2:
            raise ValidationError("Polygon has zero area.")
        for i in range(n):
            for j in range(i + 1, n):
                if j == i + 1 or (i == 0 and j == n - 1):
                    continue  # adjacent edges legitimately share a vertex
                if _segments_intersect(verts[i], verts[(i + 1) % n], verts[j], verts[(j + 1) % n]):
                    raise ValidationError(f"Polygon edges {i} and {j} cross: the boundary is self-intersecting.")

    def signed_area_mm2(self) -> float:
        v = self.vertices
        return 0.5 * sum(v[i].x_mm * v[(i + 1) % len(v)].y_mm - v[(i + 1) % len(v)].x_mm * v[i].y_mm
                         for i in range(len(v)))

    @property
    def area_mm2(self) -> float:
        return abs(self.signed_area_mm2())

    def to_list(self) -> list:
        return [v.to_list() for v in self.vertices]

    @classmethod
    def from_list(cls, data: Any) -> "Polygon2D":
        if not isinstance(data, Sequence) or isinstance(data, (str, bytes)):
            raise ValidationError(f"A polygon must be a list of [x_mm, y_mm] points, got {data!r}.")
        return cls(tuple(Point2D.from_list(p) for p in data))

    @classmethod
    def rectangle(cls, x0_mm: float, y0_mm: float, x1_mm: float, y1_mm: float) -> "Polygon2D":
        return cls((Point2D(x0_mm, y0_mm), Point2D(x1_mm, y0_mm), Point2D(x1_mm, y1_mm), Point2D(x0_mm, y1_mm)))
