"""Oracle — Consequences of Changing a Level

Purpose:
    Works out, without changing anything, what an engineer's change to one level field means for the other levels:
    changing a level's elevation may make a neighbour's derived storey height stale, and changing a level's storey
    height moves every level above it (the height IS the gap to the next level, and the model refuses a height that
    disagrees with the gap). The result is a plan: the complete replacement list for the building's levels, the
    engineer-level consequences (other levels that move), and the derived storey heights that must be recomputed.
    A height the ENGINEER set is never recomputed silently: if a change would make it disagree with the gap the plan
    refuses, and says which value to change first.

Role in Oracle:
    Keeps OracleProject.set_value() generic. Levels are settable through the same evidence/decision path as elements;
    only this small, separate module knows that levels depend on each other. It reads the project (statuses) and
    changes nothing, so a refused plan leaves the model exactly as it was.

Dependencies:
    oracle.core.building, oracle.core.common, oracle.core.value_status.

Consumers:
    oracle.core.project (set_value on a level), tests.

Status:
    Core (added in schema 0.4.0).

Migration/Notes:
    No persisted data of its own. Levels moved as a consequence are recorded by the project as their own engineer
    decisions ("Moved with D-x ...") so the history of every elevation stays complete.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from .building import STOREY_HEIGHT_TOL_MM, Level
from .common import Target, ValidationError
from .value_status import ENGINEER_STATUSES


@dataclass
class LevelPlan:
    levels: list = field(default_factory=list)            # every changed Level, ready for BuildingModel.replace_levels
    cascaded: list = field(default_factory=list)          # (level_id, field, new_value, old_value): other levels that move
    derived_heights: list = field(default_factory=list)   # (level_id, new_height, old_height): recomputed derived heights


def plan_level_change(project, old: Level, new: Level, changed_field: str) -> LevelPlan:
    building = project.building
    ordered = building.levels
    work = {lv.id: lv for lv in ordered}
    work[old.id] = new
    plan = LevelPlan(levels=[new])
    tol = STOREY_HEIGHT_TOL_MM

    if changed_field == "storey_height_mm" and new.storey_height_mm is not None:
        i = next(k for k, lv in enumerate(ordered) if lv.id == old.id)
        if i + 1 < len(ordered):
            delta = (new.elevation_mm + new.storey_height_mm) - ordered[i + 1].elevation_mm
            if abs(delta) > tol:
                for upper in ordered[i + 1:]:
                    moved = replace(upper, elevation_mm=round(upper.elevation_mm + delta, 6))
                    work[upper.id] = moved
                    plan.levels.append(moved)
                    plan.cascaded.append((upper.id, "elevation_mm", moved.elevation_mm, upper.elevation_mm))

    after = sorted(work.values(), key=lambda lv: lv.elevation_mm)
    for lo, hi in zip(after, after[1:]):
        step = round(hi.elevation_mm - lo.elevation_mm, 6)
        if lo.storey_height_mm is None or abs(lo.storey_height_mm - step) <= tol:
            continue
        if lo.id == old.id and changed_field == "storey_height_mm":
            continue                                       # the engineer's own value; the levels above were moved to fit it
        status = project.value_status_of(Target.level(lo.id), "storey_height_mm")
        if status is not None and status.status in ENGINEER_STATUSES:
            raise ValidationError(
                f"Level {lo.id!r} has an engineer-set storey height of {lo.storey_height_mm} mm, which would no longer "
                f"match the {step} mm to the next level. Change that storey height (or the elevation) explicitly first.")
        healed = replace(lo, storey_height_mm=step)
        work[lo.id] = healed
        if lo.id == old.id:
            plan.levels[0] = healed                        # the changed level itself carries its recomputed height
        else:
            plan.levels = [healed if lv.id == lo.id else lv for lv in plan.levels]
            if all(lv.id != lo.id for lv in plan.levels):
                plan.levels.append(healed)
        plan.derived_heights.append((lo.id, step, lo.storey_height_mm))
    return plan
