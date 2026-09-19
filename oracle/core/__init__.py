"""Oracle — Engineering Core: Public API

Purpose:
    Re-exports the core's public classes so callers import from oracle.core: the building model,
    project, design basis, decisions, issues, and (schema 0.2.0) provenance, value status,
    alternative interpretations and readiness.

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

Migration/Notes:
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
from .interpretations import Interpretation, InterpretationSet, InterpretationStatus, SetStatus
from .issues import EngineeringIssue, IssueCategory, IssueSeverity, IssueStatus
from .project import OracleProject
from .provenance import ProvenanceRecord, SourceReference
from .readiness import Blocker, BlockerKind, ProjectReadiness
from .value_status import ENGINEER_STATUSES, ValueStatus, ValueStatusRecord

__all__ = [
    "BuildingModel", "GridLine", "Level", "Node", "SCHEMA_VERSION", "SchemaVersionError", "Target", "TargetScope",
    "ValidationError", "DecisionCategory", "DecisionSource", "DecisionStatus", "EngineeringDecision",
    "DesignBasis", "LevelLoading", "SeismicBasis", "WindBasis", "Beam", "Column", "ElementKind", "Foundation",
    "FoundationType", "Opening", "Section", "SectionShape", "Slab", "Stair", "StructuralElement", "Wall",
    "Point2D", "Polygon2D", "EngineeringIssue", "IssueCategory", "IssueSeverity", "IssueStatus", "OracleProject",
    "Interpretation", "InterpretationSet", "InterpretationStatus", "SetStatus", "ProvenanceRecord", "SourceReference",
    "Blocker", "BlockerKind", "ProjectReadiness", "ENGINEER_STATUSES", "ValueStatus", "ValueStatusRecord",
]
