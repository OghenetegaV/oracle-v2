"""Oracle — Architectural Interpretation: Package

Purpose:
    Interprets an architectural drawing (a neutral DrawingDocument from oracle.ingestion) into an
    evidence-backed ArchitecturalInterpretation inside an OracleProject: units, layer meanings, views
    (floor plans, sections, elevations...), levels, coordinate frames, observations, height evidence and
    cross-view reconciliation, with every uncertainty kept as issues and alternative interpretations.

Role in Oracle:
    The architectural drawing intelligence layer. It sits between CAD reading (oracle.ingestion) and the
    engineer's review, and produces what a later structural reasoning phase will consume. It makes no
    structural decisions and calls no AI service by itself.

Dependencies:
    oracle.core; oracle.ingestion.

Consumers:
    The CLI (python -m oracle.interpretation), tests, and future structural reasoning and the wizard.

Status:
    Interpretation (Phase 3).

Migration/Notes:
    Entry points: interpret_file, interpret_document, align_view, establish_levels, suggest_elevations, render_report.
"""

from .pipeline import (
    InterpretationConfig, STAGES, align_view, establish_levels, interpret_document, interpret_file, interpret_into,
    suggest_elevations,
)
from .report import render_report

__all__ = ["InterpretationConfig", "STAGES", "interpret_document", "interpret_file", "interpret_into", "establish_levels", "suggest_elevations", "align_view",
           "render_report"]
