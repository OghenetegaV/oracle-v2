"""Oracle — Application Layer: Package

Purpose:
    The thin layer between an interface and the backend for the architectural-drawing workflow: file checking, the session that owns the
    project and runs the interpretation in honest stages, the read models an interface shows, the drawing-preview model, and each engineer
    action expressed as the ordinary domain decision.

Role in Oracle:
    UI -> oracle.application -> oracle.interpretation -> oracle.core. It imports no interface toolkit and no CAD library, so any front
    end (the wizard's architectural workspace today) can use it and tests can drive it without a window.

Dependencies:
    oracle.core, oracle.ingestion, oracle.interpretation.

Consumers:
    oracle.ui, oracle_wizard (through oracle.ui), tests.

Status:
    Application layer (interface phase).

Migration/Notes:
    Nothing here is persisted except through the OracleProject and the optional geometry cache beside a saved project.
"""

from .files import DrawingFileInfo, inspect_drawing_file
from .preview import DrawingPreview, Overlay
from .session import (
    ActionRefused, ArchitecturalSession, SourceChoiceRequired, STAGE_LABELS, StageEvent, WORKFLOW_STAGES, WorkflowError,
)

__all__ = ["ActionRefused", "ArchitecturalSession", "DrawingFileInfo", "DrawingPreview", "Overlay", "STAGE_LABELS", "SourceChoiceRequired",
           "StageEvent", "WORKFLOW_STAGES", "WorkflowError", "inspect_drawing_file"]
