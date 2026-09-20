"""Oracle — DXF/DWG Reader

Purpose:
    Reads a DXF (or a DWG, through the ODA File Converter) into a neutral DrawingDocument: model-space
    entities with their layers, handles, points, text, block names and dimension values; layer table;
    the file's unit metadata and header extents; layouts and viewports. Block references are recorded as
    INSERT entities carrying their block name and the footprint of the block definition; they are NOT
    expanded into their contents by default (a real architectural sheet has thousands).

Role in Oracle:
    The only place that touches a CAD library (ezdxf) in the architectural pipeline; everything downstream
    works on the neutral document. It reads faithfully and interprets nothing: it does not decide what a
    layer means, what the units are, or what a text says. Unreadable entities are counted and reported in
    the document's warnings, never silently dropped; a file that cannot be opened at all raises
    IngestionError so the caller can turn it into an engineering issue.

Dependencies:
    ezdxf (imported here only). The ODA File Converter executable, only for DWG input.

Consumers:
    oracle.interpretation.pipeline (interpret_file); tests; the CLI.

Status:
    Ingestion (Phase 3).

Migration/Notes:
    Paper-space layouts are listed and their viewports recorded, but their entities are not read into the
    document yet (the real sample has empty layouts). Original coordinates are kept untouched.
"""

from __future__ import annotations

import glob
import math
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from .drawing import DrawingDocument, DrawnEntity, LayerInfo, ViewportInfo, box_of, file_sha256


class IngestionError(Exception):
    """The file could not be read as a drawing at all."""


def find_oda_converter() -> Optional[str]:
    """The newest ODA File Converter in the usual Windows install folders, or None."""
    hits = sorted(glob.glob(r"C:\Program Files\ODA\*\ODAFileConverter.exe")
                  + glob.glob(r"C:\Program Files (x86)\ODA\*\ODAFileConverter.exe"))
    return hits[-1] if hits else None


def convert_dwg_to_dxf(dwg_path: Path, out_dir: Path, converter: Optional[str] = None, timeout: int = 600) -> Path:
    converter = converter or find_oda_converter()
    if not converter or not Path(converter).exists():
        raise IngestionError("Reading a DWG needs the ODA File Converter (free); none was found. Convert the file "
                             "to DXF, or pass the converter's path.")
    src_dir = Path(tempfile.mkdtemp(prefix="oracle_dwg_"))
    try:
        shutil.copy2(dwg_path, src_dir / dwg_path.name)
        result = subprocess.run([converter, str(src_dir), str(out_dir), "ACAD2018", "DXF", "0", "1", dwg_path.name],
                                capture_output=True, text=True, timeout=timeout)
        produced = out_dir / (dwg_path.stem + ".dxf")
        if not produced.exists():
            raise IngestionError(f"The ODA File Converter produced no DXF for {dwg_path.name}. {result.stderr[:200]}")
        return produced
    finally:
        shutil.rmtree(src_dir, ignore_errors=True)


def read_drawing(path, *, oda_converter: Optional[str] = None) -> DrawingDocument:
    """Read a .dxf or .dwg. The hash and file name in the document are those of the file given, even for a DWG."""
    path = Path(path)
    if not path.exists():
        raise IngestionError(f"No such file: {path}")
    digest = file_sha256(path)
    if path.suffix.lower() == ".dwg":
        with tempfile.TemporaryDirectory(prefix="oracle_dxf_") as tmp:
            dxf = convert_dwg_to_dxf(path, Path(tmp), oda_converter)
            doc = _read_dxf(dxf, path.name, digest)
        doc.warnings.insert(0, "Read from a DWG through the ODA File Converter (converted to DXF first).")
        return doc
    if path.suffix.lower() != ".dxf":
        raise IngestionError(f"Unsupported file type {path.suffix!r}; expected .dxf or .dwg.")
    return _read_dxf(path, path.name, digest)


def _read_dxf(dxf_path: Path, display_name: str, digest: str) -> DrawingDocument:
    import ezdxf
    try:
        doc = ezdxf.readfile(str(dxf_path))
    except Exception as exc:
        try:
            from ezdxf import recover
            doc, auditor = recover.readfile(str(dxf_path))
            result = _from_ezdxf(doc, display_name, digest)
            result.warnings.insert(0, f"The file needed recovery to open ({exc}); {len(auditor.errors)} problem(s) were repaired.")
            return result
        except Exception as exc2:
            raise IngestionError(f"Could not read {display_name} as a drawing: {exc2}") from exc2
    return _from_ezdxf(doc, display_name, digest)


def read_ezdxf_document(doc, display_name: str = "in-memory", digest: Optional[str] = None) -> DrawingDocument:
    """For callers that already hold an ezdxf document (tests build them in memory)."""
    return _from_ezdxf(doc, display_name, digest or ("0" * 64))


def _from_ezdxf(doc, display_name: str, digest: str) -> DrawingDocument:
    out = DrawingDocument(display_name, digest, getattr(doc, "dxfversion", None))
    header = doc.header
    code = header.get("$INSUNITS", None)
    out.unit_code = int(code) if code is not None else None
    ext_min, ext_max = header.get("$EXTMIN", None), header.get("$EXTMAX", None)
    try:
        if ext_min is not None and ext_max is not None and ext_max[0] > ext_min[0] and ext_max[1] > ext_min[1] \
                and abs(ext_max[0]) < 1e15:
            out.extents = (float(ext_min[0]), float(ext_min[1]), float(ext_max[0]), float(ext_max[1]))
    except (TypeError, IndexError):
        pass
    out.layouts = tuple(l.name for l in doc.layouts)
    out.block_count = len(doc.blocks)
    for layer in doc.layers:
        name = layer.dxf.name
        non_plotting = name.lower() == "defpoints" or layer.dxf.get("plot", 1) == 0     # AutoCAD's own conventions, kept here
        out.layers[name] = LayerInfo(name, layer.dxf.get("color", None), layer.is_frozen(), layer.is_off(), non_plotting)

    block_boxes: dict = {}

    def block_box(name: str):
        if name not in block_boxes:
            block_boxes[name] = _block_box(doc, name)
        return block_boxes[name]

    for e in doc.modelspace():
        try:
            entity = _convert(e, block_box)
        except Exception:
            out.skipped[e.dxftype()] = out.skipped.get(e.dxftype(), 0) + 1
            continue
        if entity is not None:
            out.entities.append(entity)
        else:
            out.skipped[e.dxftype()] = out.skipped.get(e.dxftype(), 0) + 1
    for layout in doc.layouts:
        if layout.name == "Model":
            continue
        for e in layout:
            if e.dxftype() == "VIEWPORT":
                try:
                    c, s = e.dxf.center, e.dxf.size if hasattr(e.dxf, "size") else (e.dxf.width, e.dxf.height)
                    w, h = e.dxf.width, e.dxf.height
                    vc = e.dxf.view_center_point
                    out.viewports.append(ViewportInfo(layout.name, (c[0] - w / 2, c[1] - h / 2, c[0] + w / 2, c[1] + h / 2),
                                                      (vc[0], vc[1]), float(e.dxf.view_height)))
                except Exception:
                    out.skipped["VIEWPORT"] = out.skipped.get("VIEWPORT", 0) + 1
    if out.skipped:
        out.warnings.append("Entities that could not be read (type: count): "
                            + ", ".join(f"{k}: {v}" for k, v in sorted(out.skipped.items())))
    return out


def _block_box(doc, name: str):
    """Footprint (min_x, min_y, max_x, max_y) of a block definition in its own coordinates, or None."""
    try:
        from ezdxf import bbox
        block = doc.blocks.get(name)
        if block is None:
            return None
        extents = bbox.extents(block, fast=True)
        if not extents.has_data:
            return None
        return (extents.extmin.x, extents.extmin.y, extents.extmax.x, extents.extmax.y)
    except Exception:
        return None


def _arc_box(centre, radius: float, start_deg: float, end_deg: float) -> tuple:
    """Box around the swept part of an arc (not the whole circle): its end points plus any axis extremes it passes."""
    a0, a1 = start_deg % 360.0, end_deg % 360.0
    if a1 <= a0:
        a1 += 360.0
    angles = [a0, a1] + [q + k for k in (0.0, 360.0) for q in (0.0, 90.0, 180.0, 270.0) if a0 < q + k < a1]
    pts = [(centre[0] + radius * math.cos(math.radians(a)), centre[1] + radius * math.sin(math.radians(a)))
           for a in angles]
    return box_of(pts)


def _entity_id(e) -> str:
    return str(e.dxf.handle) if e.dxf.hasattr("handle") else f"H{id(e):x}"


def _convert(e, block_box) -> Optional[DrawnEntity]:
    t = e.dxftype()
    layer = e.dxf.layer if e.dxf.hasattr("layer") else "0"
    eid = _entity_id(e)
    if t == "LINE":
        pts = ((e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y))
        return DrawnEntity(eid, "LINE", layer, box_of(pts), pts)
    if t == "LWPOLYLINE":
        pts = tuple((p[0], p[1]) for p in e.get_points("xy"))
        return DrawnEntity(eid, "POLYLINE", layer, box_of(pts), pts, bool(e.closed)) if pts else None
    if t == "POLYLINE":
        pts = tuple((v.dxf.location.x, v.dxf.location.y) for v in e.vertices)
        return DrawnEntity(eid, "POLYLINE", layer, box_of(pts), pts, bool(e.is_closed)) if pts else None
    if t == "CIRCLE":
        c, r = (e.dxf.center.x, e.dxf.center.y), float(e.dxf.radius)
        return DrawnEntity(eid, "CIRCLE", layer, box_of([c], r), (c,), True, radius=r)
    if t == "ARC":
        c, r = (e.dxf.center.x, e.dxf.center.y), float(e.dxf.radius)
        a0, a1 = float(e.dxf.start_angle), float(e.dxf.end_angle)
        return DrawnEntity(eid, "ARC", layer, _arc_box(c, r, a0, a1), (c,), radius=r, rotation=a0, value=a1)
    if t in ("TEXT", "MTEXT"):
        text = e.dxf.text if t == "TEXT" else e.plain_text()
        p = e.dxf.insert
        height = float(e.dxf.height if t == "TEXT" else e.dxf.char_height)
        rot = float(e.dxf.rotation) if e.dxf.hasattr("rotation") else 0.0
        return DrawnEntity(eid, "TEXT", layer, box_of([(p.x, p.y)]), ((p.x, p.y),), text=(text or "").strip(),
                           rotation=rot, height=height)
    if t == "DIMENSION":
        pts = []
        for name in ("defpoint", "defpoint2", "defpoint3", "text_midpoint"):
            if e.dxf.hasattr(name):
                v = e.dxf.get(name)
                pts.append((v[0], v[1]))
        try:
            value = float(e.get_measurement())
            if hasattr(value, "magnitude"):
                value = float(value.magnitude)
        except Exception:
            value = None
        angle = float(e.dxf.angle) if e.dxf.hasattr("angle") else 0.0
        text = e.dxf.text if e.dxf.hasattr("text") and e.dxf.text not in ("", "<>") else None
        return DrawnEntity(eid, "DIMENSION", layer, box_of(pts), tuple(pts), value=value, rotation=angle, text=text) if pts else None
    if t == "INSERT":
        p = e.dxf.insert
        name = e.dxf.name
        bb = block_box(name)
        sx = float(e.dxf.xscale) if e.dxf.hasattr("xscale") else 1.0
        sy = float(e.dxf.yscale) if e.dxf.hasattr("yscale") else 1.0
        rot = float(e.dxf.rotation) if e.dxf.hasattr("rotation") else 0.0
        if bb is None:
            box = box_of([(p.x, p.y)])
        else:
            c, s = math.cos(math.radians(rot)), math.sin(math.radians(rot))
            corners = [(p.x + c * x * sx - s * y * sy, p.y + s * x * sx + c * y * sy)
                       for x in (bb[0], bb[2]) for y in (bb[1], bb[3])]
            box = box_of(corners)
        return DrawnEntity(eid, "INSERT", layer, box, ((p.x, p.y),), block=name, rotation=rot)
    if t == "HATCH":
        from ezdxf import bbox
        ext = bbox.extents([e], fast=True)
        if not ext.has_data:
            return None
        return DrawnEntity(eid, "HATCH", layer, (ext.extmin.x, ext.extmin.y, ext.extmax.x, ext.extmax.y),
                           text=e.dxf.pattern_name if e.dxf.hasattr("pattern_name") else None)
    if t == "SPLINE":
        pts = tuple((p[0], p[1]) for p in e.control_points)
        return DrawnEntity(eid, "SPLINE", layer, box_of(pts), pts) if pts else None
    return None  # other entity types are counted as skipped by the caller
