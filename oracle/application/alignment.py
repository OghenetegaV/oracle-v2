"""Oracle — Point-Pick Alignment Geometry (application layer)

Purpose:
    The arithmetic behind "Align Plan": the engineer clicks a point on the plan being aligned and the point that is the SAME PHYSICAL PLACE on the
    reference plan (a grid crossing, a building corner); with a second pair the plan is also turned. This module turns those correspondence pairs
    into the coordinate frame that lines the plan up, and previews where the plan's linework would land on the reference, WITHOUT touching the
    project. One pair fixes translation; two pairs fix translation and rotation. Scale is never changed (the domain's frames keep scale 1 here); a
    difference in the distance between the two point pairs is reported as a warning so the engineer can decide.

Role in Oracle:
    Pure computation for oracle.application.session (which commits the result only through an engineer decision). Source coordinates are never
    modified: points are read in the drawing's own coordinates, and the result is an interpretation relationship (a frame) between two views.

Dependencies:
    oracle.core (CoordinateFrame, the architecture's frame chain); standard library.

Consumers:
    oracle.application.session, oracle.ui.point_tools, tests.

Status:
    Application layer (interface refinement, quick build).

Migration/Notes:
    Convention: the alignment frame maps BUILDING coordinates to the plan's view-local coordinates: view_local = translation + R(rotation) * building.
    The reference plan's building coordinates are those of its own alignment frame (or its view frame when it has none: the base plan).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from oracle.core import CoordinateFrame

SCALE_WARNING = 0.02          # two-point distances differing by more than 2% are reported


@dataclass
class AlignmentSolution:
    rotation_deg: float
    frame_translation: tuple           # frame translation: view_local = frame_translation + R * building
    translation: tuple                 # the value the alignment decision records (building = view_local + translation when there is no rotation)
    shift_mm: tuple                    # how far the plan moves in the building frame, in millimetres (for the engineer's summary)
    scale_difference: Optional[float]  # (plan distance / reference distance) - 1 for a two-point alignment
    residual_mm: Optional[float]       # how far the second pair misses after the fit
    pairs: list
    warnings: list = field(default_factory=list)
    moved: list = field(default_factory=list)      # polylines of the plan's linework, placed on the reference plan (reference source coordinates)
    markers: list = field(default_factory=list)


def solve(arch, view, reference, pairs: list, paths: Optional[list] = None, max_paths: int = 4000) -> AlignmentSolution:
    """`pairs` = [((px, py), (qx, qy)), ...] with p on the plan being aligned and q on the reference, both in source drawing coordinates."""
    if not 1 <= len(pairs) <= 2:
        raise ValueError("Pick one pair of matching points (or two, to also turn the plan).")
    if view.frame_id is None:
        raise ValueError("This view has no coordinate frame to align.")
    reference_frame = reference.alignment_frame_id or reference.frame_id
    if reference_frame is None:
        raise ValueError("The reference plan has no coordinate frame.")
    p_local = [arch.from_source(view.frame_id, p) for p, _ in pairs]
    q_building = [arch.from_source(reference_frame, q) for _, q in pairs]
    rotation, scale_difference, warnings = 0.0, None, []
    if len(pairs) == 2:
        dp = (p_local[1][0] - p_local[0][0], p_local[1][1] - p_local[0][1])
        dq = (q_building[1][0] - q_building[0][0], q_building[1][1] - q_building[0][1])
        lp, lq = math.hypot(*dp), math.hypot(*dq)
        if lp < 1e-9 or lq < 1e-9:
            raise ValueError("The two points are the same point: pick two different points.")
        rotation = math.degrees(math.atan2(dp[1], dp[0]) - math.atan2(dq[1], dq[0]))
        rotation = (rotation + 180.0) % 360.0 - 180.0
        if abs(rotation) < 1e-6:
            rotation = 0.0
        scale_difference = lp / lq - 1.0
        if abs(scale_difference) > SCALE_WARNING:
            warnings.append(f"The two plans differ in scale by about {abs(scale_difference) * 100:.1f}% between your points. Oracle keeps the scale unchanged; "
                            "check that you picked the same two places.")
    c, s = math.cos(math.radians(rotation)), math.sin(math.radians(rotation))
    tx = p_local[0][0] - (c * q_building[0][0] - s * q_building[0][1])
    ty = p_local[0][1] - (s * q_building[0][0] + c * q_building[0][1])
    factor = arch.drawing.units.factor_to_mm
    residual = None
    if len(pairs) == 2:
        px = tx + c * q_building[1][0] - s * q_building[1][1]
        py = ty + s * q_building[1][0] + c * q_building[1][1]
        residual = math.hypot(px - p_local[1][0], py - p_local[1][1]) * factor
    frame = CoordinateFrame("FRM-99", "preview", view.frame_id, (tx, ty), rotation)
    moved = []
    for pts in (paths or [])[:max_paths]:
        line = []
        for point in pts:
            building = frame.from_parent(arch.from_source(view.frame_id, point))
            line.append(arch.to_source(reference_frame, building))
        moved.append(line)
    markers = [(q[0], q[1]) for _, q in pairs]
    return AlignmentSolution(rotation, (tx, ty), (-tx, -ty), (-tx * factor, -ty * factor), scale_difference, residual, list(pairs), warnings, moved, markers)


def describe(solution: AlignmentSolution) -> list:
    """The preview in a few plain sentences."""
    dx, dy = solution.shift_mm
    lines = [f"Moves the plan {abs(dx):,.0f} mm {'right' if dx >= 0 else 'left'} and {abs(dy):,.0f} mm {'up' if dy >= 0 else 'down'}."]
    lines.append(f"Turns it by {solution.rotation_deg:.2f}°." if solution.rotation_deg else "No rotation.")
    if solution.residual_mm is not None:
        lines.append(f"The second pair of points is matched to within {solution.residual_mm:,.0f} mm.")
    return lines + list(solution.warnings)
