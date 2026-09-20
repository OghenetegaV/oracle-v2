"""Oracle — Drawing File Check (application layer)

Purpose:
    Looks at a file the engineer has chosen BEFORE any interpretation starts and says, in plain language, what it is (name, type,
    size, full path) and whether Oracle can read it: it exists, is a file, is a .dwg or .dxf, is not empty, can be opened, looks like
    the format its extension claims (DWG magic bytes; a DXF text header or the binary DXF sentinel), and, for a DWG, whether the ODA File
    Converter that Oracle needs to read it is installed. It reads at most a few hundred bytes and never writes.

Role in Oracle:
    The "Oracle validates the file" step of the architectural workflow. It is deliberately shallow: it does not parse the drawing (that
    is the ingestion layer's job, and can take tens of seconds) and it never modifies the original.

Dependencies:
    oracle.ingestion (only find_oda_converter); standard library.

Consumers:
    oracle.application.session, oracle.ui.architectural_workspace, tests.

Status:
    Application layer (interface phase).

Migration/Notes:
    A file that passes this check can still fail to interpret (a corrupt entity table, for instance); that failure is reported by the
    session as a WorkflowError with the file named, not swallowed here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from oracle.ingestion import find_oda_converter

SUPPORTED_SUFFIXES = (".dwg", ".dxf")
_BINARY_DXF = b"AutoCAD Binary DXF"


@dataclass(frozen=True)
class DrawingFileInfo:
    path: Path
    name: str
    kind: str                       # "DWG", "DXF" or the extension of an unsupported file
    size_bytes: int
    size_text: str
    ok: bool
    problem: Optional[str] = None   # why Oracle cannot use the file, in words an engineer can act on
    note: Optional[str] = None      # something worth knowing about a usable file (e.g. a DWG will be converted)


def format_size(size: int) -> str:
    if size < 1024:
        return f"{size} bytes"
    for unit, factor in (("KB", 1024), ("MB", 1024 ** 2), ("GB", 1024 ** 3)):
        if size < factor * 1024 or unit == "GB":
            return f"{size / factor:.1f} {unit}"
    return f"{size} bytes"


def inspect_drawing_file(path, *, converter_finder: Callable[[], Optional[str]] = find_oda_converter) -> DrawingFileInfo:
    """What the file is and whether Oracle can read it. Never raises for a bad file: the problem is in the result."""
    p = Path(str(path)).expanduser()
    suffix = p.suffix.lower()
    kind = suffix.lstrip(".").upper() if suffix else "unknown"

    def bad(problem: str, size: int = 0) -> DrawingFileInfo:
        return DrawingFileInfo(p, p.name or str(p), kind, size, format_size(size), False, problem)

    try:
        if not p.exists():
            return bad(f"The file {p.name or str(p)!r} was not found at {p}.")
        if not p.is_file():
            return bad(f"{p} is a folder, not a drawing file.")
        size = p.stat().st_size
    except OSError as exc:
        return bad(f"The file cannot be examined: {exc.strerror or exc}.")
    if suffix not in SUPPORTED_SUFFIXES:
        return bad(f"Oracle reads .dwg and .dxf drawings; {p.name!r} is a {kind} file.", size)
    if size == 0:
        return bad("The file is empty.", size)
    try:
        with open(p, "rb") as handle:
            head = handle.read(2048)
    except OSError as exc:
        return bad(f"The file cannot be opened for reading: {exc.strerror or exc}. Close it in other programs and try again.", size)
    if suffix == ".dwg":
        if head.startswith(_BINARY_DXF) or b"SECTION" in head[:64]:
            return bad("This file is named .dwg but its content is a DXF drawing. Rename a copy to .dxf and choose that; Oracle does not guess "
                       "the format from the content.", size)
        if not head.startswith(b"AC10"):
            return bad("The file does not look like a DWG (it lacks the DWG version header). It may be damaged or misnamed.", size)
        if not converter_finder():
            return bad("This is a DWG file, and Oracle reads DWG through the free ODA File Converter, which is not installed. "
                       "Install it, or save the drawing as DXF and choose that instead.", size)
        return DrawingFileInfo(p, p.name, "DWG", size, format_size(size), True, None,
                               "A DWG is converted to a temporary DXF for reading; the original is never changed.")
    if not (head.startswith(_BINARY_DXF) or b"SECTION" in head):
        return bad("The file does not look like a DXF (no drawing header was found). It may be damaged or misnamed.", size)
    return DrawingFileInfo(p, p.name, "DXF", size, format_size(size), True, None,
                           "Large drawings can take a minute to read." if size > 20 * 1024 * 1024 else None)
