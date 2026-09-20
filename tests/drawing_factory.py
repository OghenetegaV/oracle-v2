"""Synthetic architectural drawings for the Phase 3 tests (tests/drawing_factory.py)

Purpose:
    Builds small, fully known DXF drawings in memory with ezdxf so the interpretation layer can be tested
    against ground truth: floor plans (walls, doors, windows, columns, grid, room labels, optional stair,
    lift and void enclosures, rotation, mirroring and any origin), sections and elevations (level lines with
    level-name and elevation-number tags, partial floor lines, vertical dimensions), and the layer naming
    schemes and unit settings a real office might use. Nothing here reads or writes a real drawing.

Role in Oracle:
    Test support only. Every drawing records what it contains (`Sheet.truth`) so a test can compare Oracle's
    reading with what was actually drawn, instead of trusting Oracle's own output.

Dependencies:
    ezdxf (test time only); oracle.ingestion.read_ezdxf_document.

Consumers:
    tests/test_interpretation_scenarios.py, tests/test_ingestion_and_units.py and the other Phase 3 tests.

Status:
    Test support (Phase 3).

Migration/Notes:
    Coordinates are millimetres unless a Sheet is built with another unit; INSUNITS is set from `units`
    (mm=4, m=6, inch=1, unitless=0) unless `insunits` overrides it.
"""

from __future__ import annotations

import math
from typing import Optional

import ezdxf

from oracle.ingestion import read_ezdxf_document

INSUNITS = {"mm": 4, "m": 6, "inch": 1, "foot": 2, "unitless": 0}

NCS = dict(wall_ext="A-WALL-EXT", wall_int="A-WALL-INT", door="A-DOOR", window="A-GLAZ", column="A-COLS", grid="A-GRID",
           grid_label="A-GRID-IDEN", text="A-ANNO-TEXT", dims="A-ANNO-DIMS", opening="A-FLOR-OVHD", level="A-FLOR-LEVL",
           furniture="A-FURN", outline="A-ELEV-OTLN", section_cut="A-SECT-CUT")
PLAIN = dict(wall_ext="EXTERNAL WALLS", wall_int="INTERNAL WALLS", door="DOORS", window="WINDOWS", column="COLUMNS", grid="GRID",
             grid_label="GRID TEXT", text="TEXT", dims="DIMENSIONS", opening="FLOOR", level="LEVELS", furniture="FURNITURE",
             outline="OUTLINE", section_cut="CUT")
UNKNOWN = dict(wall_ext="XZ_101", wall_int="XZ_102", door="XZ_103", window="XZ_104", column="XZ_105", grid="XZ_106",
               grid_label="XZ_107", text="XZ_108", dims="XZ_109", opening="XZ_110", level="XZ_111", furniture="XZ_112",
               outline="XZ_113", section_cut="XZ_114")


class Sheet:
    """One DXF drawing under construction. Add views with plan / section / elevation, then take document()."""

    def __init__(self, units: str = "mm", layers: Optional[dict] = None, insunits: Optional[int] = None,
                 unit_scale: Optional[float] = None):
        self.doc = ezdxf.new("R2018")
        self.doc.header["$INSUNITS"] = INSUNITS[units] if insunits is None else insunits
        self.layers = dict(layers or NCS)
        # drawing units per millimetre: 1 for mm, 0.001 for m, 1/25.4 for inches
        self.k = unit_scale if unit_scale is not None else {"mm": 1.0, "m": 0.001, "inch": 1 / 25.4, "foot": 1 / 304.8,
                                                             "unitless": 1.0}[units]
        for name in self.layers.values():
            if name not in self.doc.layers:
                self.doc.layers.add(name)
        self.msp = self.doc.modelspace()
        self._blocks: set = set()
        self.truth: dict = {"plans": [], "sections": [], "elevations": []}
        self.title_height = 350.0

    # ---- helpers -------------------------------------------------------------------------------------------------
    def _block(self, name: str, w: float, d: float) -> str:
        if name not in self._blocks:
            b = self.doc.blocks.new(name)
            k = self.k
            b.add_lwpolyline([(0, 0), (w * k, 0), (w * k, d * k), (0, d * k)], close=True)
            b.add_line((0, 0), (w * k, d * k))
            self._blocks.add(name)
        return name

    def _xf(self, origin, rotation_deg=0.0, mirror=False):
        c, s = math.cos(math.radians(rotation_deg)), math.sin(math.radians(rotation_deg))

        def apply(p):
            x, y = p[0] * self.k, p[1] * self.k
            if mirror:
                x = -x
            return (origin[0] + c * x - s * y, origin[1] + s * x + c * y)
        return apply

    def _line(self, layer_key, a, b, xf):
        self.msp.add_line(xf(a), xf(b), dxfattribs={"layer": self.layers[layer_key]})

    def _rect(self, layer_key, x0, y0, x1, y1, xf):
        self.msp.add_lwpolyline([xf((x0, y0)), xf((x1, y0)), xf((x1, y1)), xf((x0, y1))], close=True,
                                dxfattribs={"layer": self.layers[layer_key]})

    def _text(self, layer_key, s, p, height, xf=None, rotation=0.0):
        q = xf(p) if xf else p
        e = self.msp.add_text(s, height=height * self.k, dxfattribs={"layer": self.layers[layer_key], "rotation": rotation})
        e.set_placement(q, align=ezdxf.enums.TextEntityAlignment.MIDDLE_CENTER)

    def _insert(self, layer_key, block, p, xf, rotation=0.0):
        self.msp.add_blockref(block, xf(p), dxfattribs={"layer": self.layers[layer_key], "rotation": rotation})

    # ---- plan ----------------------------------------------------------------------------------------------------
    def plan(self, origin=(0.0, 0.0), title: Optional[str] = "GROUND FLOOR PLAN", width: float = 12000.0, depth: float = 8000.0,
             rotation_deg: float = 0.0, mirror: bool = False, grid: bool = True, columns: bool = True,
             labels: tuple = (), ffl: Optional[str] = None, extra_text: tuple = (), wall: float = 200.0,
             title_gap: float = 1500.0):
        """A rectangular building plan with two partitions, doors, windows, an optional grid A-C / 1-2, columns, and
        `labels` (each 'STAIR', 'LIFT', 'VOID', 'DOUBLE HEIGHT SPACE', ...) placed in closed enclosures."""
        xf = self._xf(origin, rotation_deg, mirror)
        W, D, t = width, depth, wall
        self._rect("wall_ext", 0, 0, W, D, xf)
        self._rect("wall_ext", t, t, W - t, D - t, xf)
        for x in (W / 2 - t / 2, W / 2 + t / 2):
            self._line("wall_int", (x, t), (x, D - t), xf)
        for y in (D / 2 - t / 2, D / 2 + t / 2):
            self._line("wall_int", (W / 2 + t / 2, y), (W - t, y), xf)
        door = self._block("DOOR-900", 900, 900)
        win = self._block("WINDOW-1500", 1500, 200)
        for p in ((W / 4, 0), (W / 2 - 450, D / 2), (3 * W / 4, D / 2)):
            self._insert("door", door, p, xf)
        for p in ((W / 6, D - t), (W / 2, D - t), (5 * W / 6, D - t), (W - t, D / 2)):
            self._insert("window", win, p, xf)
        col = self._block("COLUMN-300", 300, 300)
        gx, gy = [0.0, W / 2, W], [0.0, D]
        if columns:
            for x in gx:
                for y in gy:
                    self._insert("column", col, (x - 150, y - 150), xf)
        if grid:
            for x, name in zip(gx, "ABC"):
                self._line("grid", (x, -600), (x, D + 1200), xf)
                self._text("grid_label", name, (x, D + 900), 250, xf)
            for y, name in zip(gy, "12"):
                self._line("grid", (-600, y), (W + 600, y), xf)
                self._text("grid_label", name, (-400, y + 300), 250, xf)
        self._text("text", "OFFICE", (W / 4, D / 2), 250, xf)
        self._text("text", "STORE", (3 * W / 4, D / 4), 250, xf)
        cursor = 900.0
        for label in labels:
            x0 = W / 2 + 600
            y0 = D / 2 + 400 + cursor
            self._rect("opening", x0, y0, x0 + 2600, y0 + 1800, xf)
            self._text("text", label, (x0 + 1300, y0 + 900), 250, xf)
            cursor += 2300.0
        if ffl:
            self._text("text", ffl, (W / 4, D - 1000), 250, xf)
        for s in extra_text:
            self._text("text", s, (W / 4, 700), 250, xf)
        if title:
            corners = [xf(p) for p in ((-600, -600), (W + 600, -600), (W + 600, D + 1200), (-600, D + 1200))]
            self._text("text", title, ((min(c[0] for c in corners) + max(c[0] for c in corners)) / 2,
                                       min(c[1] for c in corners) - title_gap * self.k), self.title_height, None)
        info = {"title": title, "origin": origin, "rotation": rotation_deg, "mirror": mirror, "width": W, "depth": D,
                "to_source": xf, "grid_points": {f"{a}{b}": (x, y) for x, a in zip(gx, "ABC") for y, b in zip(gy, "12")}}
        self.truth["plans"].append(info)
        return info

    # ---- section and elevation -----------------------------------------------------------------------------------
    def _tags(self, levels, width, xf, layer_key="level", dy=0.0):
        for name, elevation in levels:
            self._text(layer_key, name, (width + 2600, elevation), 250, xf)
            self._text(layer_key, f"{elevation:g}" if isinstance(elevation, float) and not float(elevation).is_integer()
                       else f"{int(elevation)}", (width + 6200, elevation), 250, xf)

    def section(self, origin=(0.0, 0.0), title: Optional[str] = "SECTION A-A", levels=(("GROUND FLOOR", 0), ("FIRST FLOOR", 3300),
                                                                                   ("ROOF", 6600)),
                width: float = 10000.0, partial=(), dims: bool = False, slab: float = 250.0):
        """Full-width level lines with a name and an elevation number tagged at each, walls, slabs, optional
        partial floor lines `(name, elevation, x0, x1)` and vertical dimensions between consecutive levels."""
        xf = self._xf(origin)
        top = max(e for _n, e in levels)
        self._line("section_cut", (0, 0), (0, top), xf)
        self._line("section_cut", (width, 0), (width, top), xf)
        for name, elevation in levels:
            self._line("level", (-500, elevation), (width + 7500, elevation), xf)
            self._rect("section_cut", 0, elevation - slab, width, elevation, xf)          # the slab, as a closed shape
        for name, elevation, x0, x1 in partial:
            self._line("level", (x0, elevation), (x1, elevation), xf)
            self._rect("section_cut", x0, elevation - slab, x1, elevation, xf)
            self._text("level", name, (width + 2600, elevation), 250, xf)
            self._text("level", f"{int(elevation)}", (width + 6200, elevation), 250, xf)
        for x in range(1500, int(width), 1500):
            self._line("section_cut", (x, 0), (x, top), xf)
        self._tags(levels, width, xf)
        if dims:
            ordered = sorted(levels, key=lambda l: l[1])
            for (_a, ea), (_b, eb) in zip(ordered, ordered[1:]):
                d = self.msp.add_linear_dim(base=xf((-1500, ea)), p1=xf((0, ea)), p2=xf((0, eb)), angle=90,
                                            dxfattribs={"layer": self.layers["dims"]})
                d.render()
        if title:
            self._text("text", title, (origin[0] + (width / 2) * self.k, origin[1] - 1500 * self.k), self.title_height)
        info = {"title": title, "origin": origin, "levels": list(levels), "width": width}
        self.truth["sections"].append(info)
        return info

    def elevation(self, origin=(0.0, 0.0), title: Optional[str] = "FRONT ELEVATION", levels=(("GROUND FLOOR", 0), ("FIRST FLOOR", 3300),
                                                                                         ("ROOF", 6600)),
                  width: float = 12000.0):
        """A building outline with window rectangles per storey and level tags; walls are not drawn as wall linework."""
        xf = self._xf(origin)
        top = max(e for _n, e in levels)
        self._rect("outline", 0, 0, width, top, xf)
        ordered = sorted(levels, key=lambda l: l[1])
        for (_n, lo), (_m, hi) in zip(ordered, ordered[1:]):
            self._line("outline", (0, lo), (width, lo), xf)
            for i in range(5):
                x = 1200 + i * 2200
                self._rect("window", x, lo + 900, x + 1200, lo + 2400, xf)
        for name, elevation in levels:
            self._line("level", (-500, elevation), (width + 7500, elevation), xf)
        self._tags(levels, width, xf)
        if title:
            self._text("text", title, (origin[0] + (width / 2) * self.k, origin[1] - 1500 * self.k), self.title_height)
        info = {"title": title, "origin": origin, "levels": list(levels), "width": width}
        self.truth["elevations"].append(info)
        return info

    def scatter(self, layer_key: str, count: int, origin=(0.0, 0.0), spacing: float = 900.0):
        """Unrelated short lines on one layer (for the unknown-layer and malformed cases)."""
        xf = self._xf(origin)
        for i in range(count):
            self._line(layer_key, (i * spacing, 0), (i * spacing, 400), xf)

    # ---- output --------------------------------------------------------------------------------------------------
    def document(self, name: str = "synthetic.dxf"):
        return read_ezdxf_document(self.doc, name, "0" * 64)

    def save(self, path) -> None:
        self.doc.saveas(str(path))


def three_storey_sheet(**kwargs) -> Sheet:
    """Ground, first and second floor plans side by side with the same footprint and grid, as one sheet."""
    s = Sheet(**kwargs)
    step = 20000.0
    for i, title in enumerate(("GROUND FLOOR PLAN", "FIRST FLOOR PLAN", "SECOND FLOOR PLAN")):
        s.plan((i * step, 0.0), title)
    return s
