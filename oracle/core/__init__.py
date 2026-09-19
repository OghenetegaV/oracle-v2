"""Oracle — Engineering Core: Public API

Purpose:
    Re-exports the core's public classes so callers import from oracle.core.

Role in Oracle:
    The import surface of the engineering core. The core is independent of the GUI, Claude,
    STAAD.Pro, AutoCAD and CAD parsing; those become adapters that depend on it, never the
    reverse.

Dependencies:
    The sibling modules in this package (standard library only).

Consumers:
    tests; future adapters. The legacy scripts do not import it yet.

Status:
    Core.

Migration:
    Remains. Add exports here as new core modules appear.
"""

from .building import BuildingModel, GridLine, Level, Node
from .common import SCHEMA_VERSION, SchemaVersionError, Target, TargetScope, ValidationError
from .decisions import DecisionCategory, DecisionSource, DecisionStatus, EngineeringDecision
from .design_basis import DesignBasis, LevelLoading, SeismicBasis, WindBasis
from .elements import (
    Beam, Column, ElementKind, Foundation, FoundationType, Opening, Section, SectionShape, Slab, Stair,
    StructuralElement, Wall,
)
from .geometry import Point2D, Polygon2D
from .issues import EngineeringIssue, IssueCategory, IssueSeverity, IssueStatus
from .project import OracleProject

__all__ = [
    "BuildingModel", "GridLine", "Level", "Node", "SCHEMA_VERSION", "SchemaVersionError", "Target", "TargetScope",
    "ValidationError", "DecisionCategory", "DecisionSource", "DecisionStatus", "EngineeringDecision",
    "DesignBasis", "LevelLoading", "SeismicBasis", "WindBasis", "Beam", "Column", "ElementKind", "Foundation",
    "FoundationType", "Opening", "Section", "SectionShape", "Slab", "Stair", "StructuralElement", "Wall",
    "Point2D", "Polygon2D", "EngineeringIssue", "IssueCategory", "IssueSeverity", "IssueStatus", "OracleProject",
]
