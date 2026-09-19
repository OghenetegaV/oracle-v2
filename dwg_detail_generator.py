"""Oracle — Detail Drawing Generator

Purpose:
    Draws the structural plan, column schedule and beam BBS tables into a DXF with ezdxf and
    converts it to DWG with the ODA File Converter (output_dwg/).

Role in Oracle:
    Legacy drawing output layer. No live AutoCAD session is needed.

Dependencies:
    ezdxf; ODA File Converter (optional, for DWG); lisp_detail_generator; config.

Consumers:
    oracle_wizard.

Status:
    Legacy / Transitional.

Migration:
    Retained. To be rebuilt as a drawing adapter that takes oracle.core objects.

Details (original module notes, retained):
    Oracle Phase 5b: Complete Detail Drawing (DWG)

    Builds one drawing containing the structural plan (columns, beams, grid,
    bar marks) plus the Column Reinforcement Schedule and Beam Bar Bending
    Schedule tables, using ezdxf (no live AutoCAD session needed -- see
    lisp_detail_generator.py's LISP route for that alternative). Saves as DXF,
    then converts to a real DWG via the ODA File Converter already detected in
    config.py, since ezdxf itself only writes DXF.
"""

import shutil
import subprocess
from datetime import datetime

import ezdxf
from ezdxf.enums import TextEntityAlignment

from config import ODA_CONVERTER_PATH, PROJECT_ROOT
from lisp_detail_generator import (
    build_beam_schedule,
    build_bbs,
    build_column_schedule,
    build_steel_schedule,
    grid_bounds_from_ga,
    load_design,
    load_ga,
    load_standards,
)

OUTPUT_DWG_DIR = PROJECT_ROOT / "output_dwg"
OUTPUT_DWG_DIR.mkdir(parents=True, exist_ok=True)

DXF_PATH = OUTPUT_DWG_DIR / "oracle_detail_drawing.dxf"
DWG_NAME = "oracle_detail_drawing.dwg"

LAYERS = {
    "GRID": 8, "COLUMNS": 1, "BEAMS": 5, "BARMARKS": 1,
    "TABLE": 7, "TITLEBLOCK": 7, "STEEL": 6,
}


def add_text(msp, text, point, height, layer, align=TextEntityAlignment.MIDDLE_CENTER, color=None):
    attribs = {"layer": layer}
    if color:
        attribs["color"] = color
    e = msp.add_text(text, height=height, dxfattribs=attribs)
    e.set_placement(point, align=align)
    return e


def draw_grid(msp, ga, bounds):
    x_min, x_max, y_min, y_max = bounds
    dims = ga.get("dimensions", {})
    xs = sorted({c["x"] for c in ga["columns"]})
    ys = sorted({c["y"] for c in ga["columns"]})
    labels_x = [str(i + 1) for i in range(len(xs))]
    labels_y = [chr(ord("A") + i) for i in range(len(ys))]

    pad = 1.5
    for x, label in zip(xs, labels_x):
        msp.add_line((x, y_min - pad + 0.6), (x, y_max + pad - 0.6), dxfattribs={"layer": "GRID", "linetype": "DASHED"})
        msp.add_circle((x, y_min - pad), radius=0.35, dxfattribs={"layer": "GRID"})
        add_text(msp, label, (x, y_min - pad), 0.25, "GRID")
    for y, label in zip(ys, labels_y):
        msp.add_line((x_min - pad + 0.6, y), (x_max + pad - 0.6, y), dxfattribs={"layer": "GRID", "linetype": "DASHED"})
        msp.add_circle((x_min - pad, y), radius=0.35, dxfattribs={"layer": "GRID"})
        add_text(msp, label, (x_min - pad, y), 0.25, "GRID")


def _steel_box_dims_m(section_str):
    """UC/UB designations are literally 'depth x width x mass-per-metre' (e.g.
    'UC 203x203x46'), so the same two-number parse used for RC WxD sections
    gives a reasonable footprint box for the plan -- not exact flange/web
    geometry, just enough to show the member on the layout."""
    import re
    nums = re.findall(r"\d+", section_str)
    d, w = int(nums[0]), int(nums[1])
    return w / 1000, d / 1000


def draw_plan(msp, ga, column_schedule, beam_summaries, steel_columns, steel_beams):
    col_positions = {c["name"]: (c["x"], c["y"]) for c in ga["columns"]}

    for beam in beam_summaries:
        ga_beam = next((b for b in ga["beams"] if b["name"] == beam["beam"]), None)
        if not ga_beam:
            continue
        p1 = col_positions[ga_beam["start_col"]]
        p2 = col_positions[ga_beam["end_col"]]
        horizontal = p1[1] == p2[1]
        msp.add_line(p1, p2, dxfattribs={"layer": "BEAMS", "lineweight": 35})

        # Distribute the beam name + each of its bar marks along the span at
        # 1/6, 1/2, 5/6 instead of stacking one crammed label at the midpoint --
        # matches how real BBS call-outs spread marks along a member's length.
        fracs = [1 / 6, 1 / 2, 5 / 6]
        labels = [beam["beam"]] + [entry[5] for entry in beam["entries"][:2]]
        for frac, label in zip(fracs, labels):
            px = p1[0] + (p2[0] - p1[0]) * frac
            py = p1[1] + (p2[1] - p1[1]) * frac
            offset = (0.0, 0.28) if horizontal else (0.28, 0.0)
            align = TextEntityAlignment.MIDDLE_CENTER if horizontal else TextEntityAlignment.MIDDLE_LEFT
            add_text(msp, label, (px + offset[0], py + offset[1]), 0.11, "BARMARKS",
                     align=align, color=1)

    for beam in steel_beams:
        ga_beam = next((b for b in ga["beams"] if b["name"] == beam["name"]), None)
        if not ga_beam:
            continue
        p1 = col_positions[ga_beam["start_col"]]
        p2 = col_positions[ga_beam["end_col"]]
        horizontal = p1[1] == p2[1]
        msp.add_line(p1, p2, dxfattribs={"layer": "STEEL", "lineweight": 53, "linetype": "DASHED"})
        mid = ((p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2)
        offset = (0.0, 0.28) if horizontal else (0.28, 0.0)
        label = f"{beam['name']}: {beam['section']} {beam.get('grade', '')}"
        align = TextEntityAlignment.MIDDLE_CENTER if horizontal else TextEntityAlignment.MIDDLE_LEFT
        add_text(msp, label, (mid[0] + offset[0], mid[1] + offset[1]), 0.11, "STEEL", align=align, color=6)

    for col in column_schedule:
        x, y = col_positions[col["column"]]
        w, d = (int(v) for v in col["section"].split("x"))
        half_w, half_d = (w / 1000) / 2, (d / 1000) / 2
        pts = [(x - half_w, y - half_d), (x + half_w, y - half_d),
               (x + half_w, y + half_d), (x - half_w, y + half_d)]
        msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": "COLUMNS"})
        add_text(msp, col["column"], (x, y + half_d + 0.55), 0.18, "COLUMNS", color=7)
        marks = " ".join(row["bar_mark"] for row in col["bm_rows"])
        add_text(msp, marks, (x, y - half_d - 0.55), 0.13, "BARMARKS", color=1)

    for col in steel_columns:
        x, y = col_positions[col["name"]]
        w, d = _steel_box_dims_m(col["section"])
        half_w, half_d = w / 2, d / 2
        pts = [(x - half_w, y - half_d), (x + half_w, y - half_d),
               (x + half_w, y + half_d), (x - half_w, y + half_d)]
        msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": "STEEL", "linetype": "DASHED"})
        add_text(msp, col["name"], (x, y + half_d + 0.55), 0.18, "STEEL", color=6)
        add_text(msp, f"{col['section']} {col.get('grade', '')}", (x, y - half_d - 0.55), 0.13,
                  "STEEL", color=6)


def draw_table(msp, anchor, title, headers, rows, col_widths, row_h=0.4):
    x0, y0 = anchor
    total_w = sum(col_widths)
    add_text(msp, title, (x0, y0 + row_h * 0.6), 0.22, "TABLE", align=TextEntityAlignment.LEFT, color=7)

    header_y = y0
    n_rows = len(rows) + 1
    for i in range(n_rows + 1):
        y = header_y - row_h * i
        msp.add_line((x0, y), (x0 + total_w, y), dxfattribs={"layer": "TABLE"})
    x = x0
    for w in [0] + col_widths:
        x += w
        msp.add_line((x, header_y), (x, header_y - row_h * n_rows), dxfattribs={"layer": "TABLE"})

    x = x0
    for h, w in zip(headers, col_widths):
        add_text(msp, h, (x + 0.05, header_y - row_h * 0.5), 0.13, "TABLE",
                 align=TextEntityAlignment.MIDDLE_LEFT, color=7)
        x += w
    for r, row in enumerate(rows, start=1):
        x = x0
        for val, w in zip(row, col_widths):
            add_text(msp, str(val), (x + 0.05, header_y - row_h * (r + 0.5)), 0.12, "TABLE",
                     align=TextEntityAlignment.MIDDLE_LEFT)
            x += w
    return header_y - row_h * n_rows


def draw_title_block(msp, anchor, width, bbs, steel_schedule=None):
    x0, y0 = anchor
    height = 1.6
    msp.add_lwpolyline(
        [(x0, y0), (x0 + width, y0), (x0 + width, y0 - height), (x0, y0 - height)],
        close=True, dxfattribs={"layer": "TITLEBLOCK"},
    )
    msp.add_line((x0, y0 - 0.5), (x0 + width, y0 - 0.5), dxfattribs={"layer": "TITLEBLOCK"})
    add_text(msp, "Oracle - AI-Powered Structural Design Automation", (x0 + 0.15, y0 - 0.32), 0.22,
             "TITLEBLOCK", align=TextEntityAlignment.LEFT, color=7)
    add_text(msp, "FIRST FLOOR - COLUMN & BEAM REINFORCEMENT DETAIL",
             (x0 + 0.15, y0 - 0.75), 0.16, "TITLEBLOCK", align=TextEntityAlignment.LEFT)
    code_line = "Design code: BS 8110-1:1997 / BS 4466 & BS 8666 / BS 4449"
    if steel_schedule:
        code_line += "  +  BS 5950-1:2000 (steel members)"
    add_text(msp, code_line, (x0 + 0.15, y0 - 1.0), 0.13, "TITLEBLOCK", align=TextEntityAlignment.LEFT)
    add_text(msp, f"Total steel weight: {bbs['grand_total_steel_weight_kg']:.1f} kg  |  "
                  f"Scale NTS  |  Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}",
             (x0 + 0.15, y0 - 1.25), 0.13, "TITLEBLOCK", align=TextEntityAlignment.LEFT)
    add_text(msp, "Run: APPLOAD output_lisp/oracle_details.lsp, then ORACLEDETAIL, to place these "
                  "marks on a live AutoCAD drawing instead.",
             (x0 + 0.15, y0 - 1.48), 0.10, "TITLEBLOCK", align=TextEntityAlignment.LEFT, color=8)


def build_drawing():
    standards = load_standards()
    design = load_design()
    ga = load_ga()

    column_schedule, steel_columns = build_column_schedule(design, ga, standards)
    beam_summaries, beam_rows, steel_beams = build_beam_schedule(design, ga, standards)
    steel_schedule = build_steel_schedule(steel_columns, steel_beams)
    bbs = build_bbs(column_schedule, beam_rows, steel_schedule)
    bounds = grid_bounds_from_ga(ga)

    doc = ezdxf.new("R2018", setup=True)
    doc.header["$INSUNITS"] = 6  # meters
    for name, color in LAYERS.items():
        doc.layers.add(name, color=color)
    if "DASHED" not in doc.linetypes:
        doc.linetypes.add("DASHED", pattern="A,.5,-.25")
    msp = doc.modelspace()

    draw_grid(msp, ga, bounds)
    draw_plan(msp, ga, column_schedule, beam_summaries, steel_columns, steel_beams)

    table_x = bounds[1] + 3.5
    col_rows = [
        (c["column"], c["section"], row["bm"], f"Y{row['size']}", row["no_of_bars"],
         row["length_mm"], row["bar_mark"], f"{row['total_weight_kg']:.2f}")
        for c in column_schedule for row in c["bm_rows"]
    ]
    y_cursor = bounds[3]
    if col_rows:
        y_cursor = draw_table(
            msp, (table_x, y_cursor), "COLUMN REINFORCEMENT SCHEDULE",
            ["COL", "SECTION", "BM", "SIZE", "BARS", "LEN(mm)", "MARK", "WT(kg)"],
            col_rows, [0.9, 1.3, 0.6, 0.7, 0.7, 0.9, 2.2, 1.0],
        ) - 1.0

    beam_rows_table = [
        (r["no"], f"Y{r['size']}", r["qua"], r["length_mm"], r["form"], f"{r['total_weight_kg']:.2f}")
        for r in beam_rows
    ]
    if beam_rows_table:
        y_cursor = draw_table(
            msp, (table_x, y_cursor), "BEAM BAR BENDING SCHEDULE (BBS)",
            ["NO", "SIZE", "QUA", "LEN(mm)", "FORM", "WT(kg)"],
            beam_rows_table, [0.7, 0.7, 0.7, 1.0, 1.6, 1.0],
        ) - 1.0

    if steel_schedule:
        steel_rows_table = [
            (r["name"], r["type"], r["section"], r["grade"],
             f"{r['load_kn']:.1f}" if r["load_kn"] is not None else "-", r["capacity_check"])
            for r in steel_schedule
        ]
        y_cursor = draw_table(
            msp, (table_x, y_cursor), "STRUCTURAL STEEL SCHEDULE (BS 5950-1)",
            ["MEMBER", "TYPE", "SECTION", "GRADE", "LOAD(kN)", "CAPACITY CHECK"],
            steel_rows_table, [0.9, 0.9, 1.6, 0.7, 1.0, 3.0],
        ) - 1.0

    draw_title_block(msp, (bounds[0] - 1.5, bounds[2] - 2.5), table_x + 8.5 - (bounds[0] - 1.5), bbs, steel_schedule)

    doc.saveas(str(DXF_PATH))
    print(f"DXF saved to: {DXF_PATH}")
    return bbs


def convert_to_dwg():
    if not ODA_CONVERTER_PATH:
        raise RuntimeError(
            "ODA File Converter not found (see config.py's ODA_PATHS). "
            f"DXF is still available at {DXF_PATH}; convert it manually in AutoCAD "
            "(SAVEAS -> DWG) if needed."
        )

    args = [ODA_CONVERTER_PATH, str(OUTPUT_DWG_DIR), str(OUTPUT_DWG_DIR),
            "ACAD2018", "DWG", "0", "0", "*.dxf"]
    result = subprocess.run(args, capture_output=True, text=True, timeout=60)

    dwg_path = OUTPUT_DWG_DIR / DWG_NAME
    if not dwg_path.exists():
        raise RuntimeError(
            f"ODA File Converter did not produce {dwg_path} "
            f"(exit {result.returncode}). stdout: {result.stdout!r} stderr: {result.stderr!r}"
        )
    print(f"DWG saved to: {dwg_path}")
    return dwg_path


def main():
    bbs = build_drawing()
    try:
        convert_to_dwg()
    except Exception as e:
        print(f"Warning: DWG conversion failed ({e}). DXF is available at {DXF_PATH}.")
    return bbs


if __name__ == "__main__":
    main()
