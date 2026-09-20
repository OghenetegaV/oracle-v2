"""Shared helpers for the Phase 3.5 hardening tests (tests/support35.py)

Purpose:
    Small builders the Phase 3.5 tests share: engineer decisions with fresh ids, a synthetic three-storey drawing
    interpreted into a project, a project whose views have been reviewed and whose levels have been established,
    and a project that also carries the 3x3-column structural model so a structural element can be linked to
    architectural evidence. Nothing here asserts anything.

Role in Oracle:
    Test support only.

Dependencies:
    oracle.core, oracle.interpretation, tests.drawing_factory, tests.fixtures.

Consumers:
    tests/test_levels_hardening.py, test_resolution_effects.py, test_evidence_trace_projection.py,
    test_boundary_and_schema.py.

Status:
    Test support (Phase 3.5).

Migration/Notes:
    None.
"""

from __future__ import annotations

from oracle.core import (
    DecisionCategory, DecisionSource, DecisionStatus, EngineeringDecision, OracleProject, Target, ViewType,
)
from oracle.interpretation import establish_levels, interpret_document, interpret_into, suggest_elevations
from tests.drawing_factory import Sheet, three_storey_sheet
from tests.fixtures import make_project

LEVELS3 = (("GROUND FLOOR", 0), ("FIRST FLOOR", 3300), ("SECOND FLOOR", 6600))
_n = [0]


def decision(target=None, instruction="engineer decision", *, source=DecisionSource.ENGINEER,
             status=DecisionStatus.ACCEPTED, field=None, value=None, responds_to=None, prefix="T"):
    _n[0] += 1
    return EngineeringDecision(f"{prefix}-{_n[0]:04d}", "Test Engineer", source, target or Target.project(),
                               DecisionCategory.OTHER, instruction, status=status, field=field, value=value,
                               responds_to=responds_to)


def drawing_sheet(**kw) -> Sheet:
    s = three_storey_sheet(**kw)
    s.section((0, -16000), "SECTION A-A", LEVELS3)
    return s


def plans(project, source_id=None):
    arch = project.architecture_of(source_id) if source_id else project.architecture
    return [v for v in arch.views_of(ViewType.FLOOR_PLAN) if v.variant is None]


def interpreted(**kw) -> OracleProject:
    return interpret_document(drawing_sheet().document(), project_name="P35", engineer="Test Engineer", **kw)


def reviewed(project: OracleProject, *, observations: bool = False) -> OracleProject:
    arch = project.architecture
    ids = [v.id for v in arch.views if v.view_type in (ViewType.FLOOR_PLAN, ViewType.SECTION, ViewType.ELEVATION)]
    project.review_views(ids, decision(Target.architectural(ids[0]), "views checked against the drawing"))
    if observations:
        obs = [o.id for o in arch.observations]
        project.review_observations(obs, decision(Target.architectural(obs[0]), "observations checked"))
    return project


def with_levels(elevation_type: str = "unspecified", **kw) -> OracleProject:
    p = reviewed(interpreted(**kw))
    establish_levels(p, suggest_elevations(p), decision(Target.project(), "take the levels from the drawing"),
                     elevation_type=elevation_type)
    return p


def structural_project() -> OracleProject:
    """The 3x3-column two-storey structural model, with the drawing interpreted into it as a source."""
    p = make_project()
    interpret_into(p, drawing_sheet().document())
    return p
