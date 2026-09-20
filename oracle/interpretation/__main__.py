"""Oracle — Architectural Interpretation: Command Line

Purpose:
    `python -m oracle.interpretation DRAWING [--project OUT.oracle.json] [--report OUT.txt] [--engineer NAME]
    [--name NAME] [--oda PATH]` reads a DWG or DXF, interprets it, prints the plain-text report and, when
    asked, saves the OracleProject as JSON (the authoritative record) and the report as text. It never writes
    next to the drawing and never modifies it.

Role in Oracle:
    The Phase 3 review surface (a CLI and structured JSON instead of a UI). The engineer reads the report,
    then corrects or approves through the decision system (oracle.core project methods); this command only
    produces the proposal.

Dependencies:
    oracle.interpretation, oracle.ingestion (and the ODA File Converter for a .dwg).

Consumers:
    Engineers, scripts and the real-drawing test.

Status:
    Interpretation (Phase 3).

Migration/Notes:
    Exit status is 0 when a project was produced (even if it is not ready), 2 when the drawing could not be read.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from oracle.ingestion import IngestionError

from .pipeline import interpret_file
from .report import render_report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m oracle.interpretation",
                                     description="Interpret an architectural DWG/DXF into an evidence-backed project.")
    parser.add_argument("drawing", help="path to a .dwg or .dxf file (read only)")
    parser.add_argument("--project", help="write the OracleProject JSON here")
    parser.add_argument("--report", help="write the text report here")
    parser.add_argument("--engineer", default="Unassigned", help="engineer named on the project")
    parser.add_argument("--name", help="project name (default: the drawing's file name)")
    parser.add_argument("--oda", help="path to ODAFileConverter.exe (for .dwg files)")
    parser.add_argument("--quiet", action="store_true", help="do not print the report")
    args = parser.parse_args(argv)

    path = Path(args.drawing)
    try:
        project = interpret_file(path, project_name=args.name or path.stem, engineer=args.engineer, oda_converter=args.oda)
    except IngestionError as exc:
        print(f"Could not read the drawing: {exc}", file=sys.stderr)
        return 2
    report = render_report(project)
    if args.project:
        project.save(args.project)
    if args.report:
        Path(args.report).write_text(report, encoding="utf-8")
    if not args.quiet:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
