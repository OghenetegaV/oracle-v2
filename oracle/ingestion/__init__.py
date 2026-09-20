"""Oracle — Drawing Ingestion: Package

Purpose:
    Reads CAD files into the neutral DrawingDocument that the interpreter works on. CAD-library specific
    code lives here and nowhere else in the architectural pipeline.

Role in Oracle:
    Stage 1 of Architectural DWG/DXF -> CAD extraction -> drawing entities -> interpretation -> engineer
    review. It reads and reports; it does not interpret.

Dependencies:
    ezdxf (in dxf_reader only).

Consumers:
    oracle.interpretation; tests; the CLI.

Status:
    Ingestion (Phase 3).

Migration/Notes:
    Other formats would add a reader beside dxf_reader that fills the same DrawingDocument.
"""

from .drawing import DrawingDocument, DrawnEntity, LayerInfo, ViewportInfo
from .dxf_reader import IngestionError, find_oda_converter, read_drawing, read_ezdxf_document

__all__ = ["DrawingDocument", "DrawnEntity", "LayerInfo", "ViewportInfo", "IngestionError", "find_oda_converter",
           "read_drawing", "read_ezdxf_document"]
