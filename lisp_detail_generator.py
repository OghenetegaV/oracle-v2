"""Oracle — Reinforcement Schedules, BBS and LISP Output

Purpose:
    Turns design_output.json into column/beam/steel schedules, a bar bending schedule with bar
    marks, and an AutoCAD LISP file (output_lisp/), following company_standards.json.

Role in Oracle:
    Legacy reinforcement/BBS layer. Its schedule and BBS builders are also the data source for
    dwg_detail_generator.

Dependencies:
    config; company_standards.json; design_output.json and ga_output.json.

Consumers:
    dwg_detail_generator (imports its builders), oracle_wizard.

Status:
    Legacy / Transitional.

Migration:
    Retained. Reads free-text bar strings (e.g. '4-16mm'); BS 8666 bar shapes are not modelled.
    To be moved onto structured reinforcement data later.

Details (original module notes, retained):
    Oracle Phase 5: LISP Detail Generation
    Converts Phase 4 element design (design_output.json) into AutoCAD LISP
    bar-marking annotations plus bar bending schedules (BBS), following the
    company's detailing standards (company_standards.json) -- BS 8110 / BS 4466
    / BS 8666 / BS 4449, matching the layout of Compiled_Structural_Drawings.pdf.
"""

import json
import re
from datetime import datetime
from pathlib import Path

from config import OUTPUT_JSON_DIR, PROJECT_ROOT

OUTPUT_LISP_DIR = PROJECT_ROOT / "output_lisp"
OUTPUT_LISP_DIR.mkdir(parents=True, exist_ok=True)

STANDARDS_PATH = PROJECT_ROOT / "company_standards.json"

MAIN_BAR_RE = re.compile(r"(\d+)\s*-\s*(\d+)\s*mm", re.IGNORECASE)
STIRRUP_RE = re.compile(r"(\d+)\s*mm\s*@\s*(\d+)", re.IGNORECASE)
SECTION_RE = re.compile(r"(\d+)\s*[xX]\s*(\d+)")

# Fallback beam span (m) used only if a beam's columns can't be located in the GA data
DEFAULT_BAY_SPAN_M = 5.0
DEFAULT_STOREY_HEIGHT_M = 3.0
HOOK_ALLOWANCE_DIAMETERS = 9  # generic hook allowance per leg; BS 8666 bend-code geometry is not modelled


def load_json(path):
    with open(path, 'r') as f:
        return json.load(f)


def load_standards(path=STANDARDS_PATH):
    return load_json(path)


def load_design(json_file="design_output.json"):
    return load_json(OUTPUT_JSON_DIR / json_file)


def load_ga(json_file="ga_output.json"):
    return load_json(OUTPUT_JSON_DIR / json_file)


def parse_main_bars(bar_str):
    """Parse strings like '8-12mm' or '4-20mm' into (qty, diameter_mm)."""
    match = MAIN_BAR_RE.search(bar_str)
    if not match:
        raise ValueError(f"Could not parse bar spec: {bar_str!r}")
    qty, dia = match.groups()
    return int(qty), int(dia)


def parse_stirrups(stirrup_str):
    """Parse strings like '8mm@150' into (diameter_mm, spacing_mm)."""
    match = STIRRUP_RE.search(stirrup_str)
    if not match:
        raise ValueError(f"Could not parse stirrup spec: {stirrup_str!r}")
    dia, spacing = match.groups()
    return int(dia), int(spacing)


def parse_section(section_str):
    """Parse strings like '300x450' into (width_mm, depth_mm)."""
    match = SECTION_RE.search(section_str)
    if not match:
        raise ValueError(f"Could not parse section spec: {section_str!r}")
    width, depth = match.groups()
    return int(width), int(depth)


def bar_weight_kg_per_m(diameter_mm, standards):
    """Look up unit weight from company standards, else fall back to the BS 4449 formula."""
    table = standards["bar_weight_kg_per_m"]
    key = f"Y{diameter_mm}"
    if key in table:
        return table[key]
    return round((diameter_mm ** 2) / 162, 3)


def grid_bounds_from_ga(ga_data):
    xs = [c["x"] for c in ga_data["columns"]]
    ys = [c["y"] for c in ga_data["columns"]]
    return min(xs), max(xs), min(ys), max(ys)


def classify_column_location(x, y, bounds, standards):
    """External columns sit on the outer grid boundary; everything else is internal."""
    x_min, x_max, y_min, y_max = bounds
    covers = standards["cover_requirements_mm"]
    if x in (x_min, x_max) or y in (y_min, y_max):
        return "external_column", covers["external_column"]
    return "internal_column", covers.get("internal_column", covers["external_column"])


def stirrup_length_m(width_mm, depth_mm, cover_mm, dia_mm):
    """Approximate closed-link cutting length: clear perimeter + generic hook allowance."""
    clear_width = width_mm - 2 * cover_mm
    clear_depth = depth_mm - 2 * cover_mm
    perimeter_mm = 2 * (clear_width + clear_depth)
    hook_allowance_mm = 2 * HOOK_ALLOWANCE_DIAMETERS * dia_mm
    return round((perimeter_mm + hook_allowance_mm) / 1000, 3)


def beam_span_m(beam, col_positions):
    """Compute beam span from actual GA column coordinates; falls back to a default bay size."""
    try:
        x1, y1 = col_positions[beam["start_col"]]
        x2, y2 = col_positions[beam["end_col"]]
        return round(((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5, 3)
    except KeyError:
        print(f"  ⚠️  Could not locate columns for beam {beam.get('name')}, using default span")
        return DEFAULT_BAY_SPAN_M


class MarkRegistry:
    """Assigns one running serial mark number per distinct bar shape (diameter + length
    + shape) within a schedule, matching the company template where the same shape
    recurring on different members shares a single bar mark."""

    def __init__(self):
        self._shapes = {}
        self._next = 1

    def register(self, diameter_mm, length_mm, shape, member_ref, qty):
        key = (diameter_mm, round(length_mm), shape)
        if key not in self._shapes:
            self._shapes[key] = {
                "mark_no": self._next,
                "diameter_mm": diameter_mm,
                "length_mm": round(length_mm),
                "shape": shape,
                "qty": 0,
                "members": [],
            }
            self._next += 1
        row = self._shapes[key]
        row["qty"] += qty
        row["members"].append(member_ref)
        return row["mark_no"]

    def rows(self):
        return sorted(self._shapes.values(), key=lambda r: r["mark_no"])


def bar_mark_label(qty, diameter_mm, mark_no, member_ref, spacing_mm=None):
    """Company format: [Qty]Y[Diameter]-[SerialMark][-Spacing]-([MemberRef])"""
    label = f"{qty}Y{diameter_mm}-{mark_no}"
    if spacing_mm:
        label += f"-{spacing_mm}"
    label += f"-({member_ref})"
    return label


def build_column_schedule(design_data, ga_data, standards):
    """One MarkRegistry per column, restarting BM numbering at 01 per column --
    matches the source drawings' per-column table layout (page 7).
    Only concrete columns get an RC bar-bending entry here -- there's no rebar to
    schedule for an off-the-shelf rolled steel section. Steel columns are
    returned separately for build_steel_schedule() instead."""
    bounds = grid_bounds_from_ga(ga_data)
    col_positions = {c["name"]: (c["x"], c["y"]) for c in ga_data["columns"]}
    storey_height_m = ga_data.get("dimensions", {}).get("storey_height_m", DEFAULT_STOREY_HEIGHT_M)
    length_mm = storey_height_m * 1000

    columns = []
    steel_columns = []
    for col in design_data["columns"]:
        if col.get("material") == "steel":
            steel_columns.append(col)
            continue
        name = col["name"]
        qty, dia = parse_main_bars(col["bars"])
        x, y = col_positions.get(name, (None, None))

        if x is None:
            location, cover_mm = "internal_column", standards["cover_requirements_mm"].get(
                "internal_column", standards["cover_requirements_mm"]["external_column"]
            )
        else:
            location, cover_mm = classify_column_location(x, y, bounds, standards)

        registry = MarkRegistry()
        mark_no = registry.register(dia, length_mm, "STRAIGHT", name, qty)
        unit_weight = bar_weight_kg_per_m(dia, standards)
        total_length_m = round(qty * storey_height_m, 3)
        total_weight_kg = round(total_length_m * unit_weight, 3)

        columns.append({
            "column": name,
            "section": col["section"],
            "location": location,
            "cover_mm": cover_mm,
            "bm_rows": [{
                "bm": f"{1:02d}",
                "size": dia,
                "no_of_bars": qty,
                "no_thus": 1,
                "total_no": qty,
                "length_mm": round(length_mm),
                "form": "STRAIGHT",
                "mark_no": mark_no,
                "bar_mark": bar_mark_label(qty, dia, mark_no, name),
                "unit_weight_kg_m": unit_weight,
                "total_length_m": total_length_m,
                "total_weight_kg": total_weight_kg,
            }],
        })
    return columns, steel_columns


def build_beam_schedule(design_data, ga_data, standards):
    """One shared MarkRegistry across all beams so identical bar shapes (same
    diameter + length) are deduplicated into a single running mark -- matches
    the source drawings' floor-wide beam quantity takeoff (pages 19, 24).
    Steel beams (no rebar) are returned separately for build_steel_schedule()."""
    col_positions = {c["name"]: (c["x"], c["y"]) for c in ga_data["columns"]}
    ga_beams = {b["name"]: b for b in ga_data["beams"]}
    cover_mm = standards["cover_requirements_mm"]["external_beams_slabs"]
    registry = MarkRegistry()
    beam_summaries = []
    steel_beams = []

    for beam in design_data["beams"]:
        if beam.get("material") == "steel":
            steel_beams.append(beam)
            continue
        name = beam["name"]
        qty, dia = parse_main_bars(beam["main_bars"])
        stirrup_dia, spacing_mm = parse_stirrups(beam["stirrups"])
        width_mm, depth_mm = parse_section(beam["section"])

        ga_beam = ga_beams.get(name)
        span_m = beam_span_m(ga_beam, col_positions) if ga_beam else DEFAULT_BAY_SPAN_M
        span_mm = span_m * 1000

        top_qty = qty // 2
        bottom_qty = qty - top_qty
        main_unit_weight = bar_weight_kg_per_m(dia, standards)

        entries = []
        if top_qty:
            mark_no = registry.register(dia, span_mm, "STRAIGHT-TOP", f"{name}T", top_qty)
            entries.append(("top", top_qty, dia, span_mm, mark_no,
                             bar_mark_label(top_qty, dia, mark_no, f"{name}T")))
        if bottom_qty:
            mark_no = registry.register(dia, span_mm, "STRAIGHT-BOTTOM", f"{name}B", bottom_qty)
            entries.append(("bottom", bottom_qty, dia, span_mm, mark_no,
                             bar_mark_label(bottom_qty, dia, mark_no, f"{name}B")))

        num_stirrups = int(span_mm // spacing_mm) + 1
        stirrup_len_mm = stirrup_length_m(width_mm, depth_mm, cover_mm, stirrup_dia) * 1000
        stirrup_mark_no = registry.register(stirrup_dia, stirrup_len_mm, "LINK", name, num_stirrups)
        entries.append(("stirrup", num_stirrups, stirrup_dia, stirrup_len_mm, stirrup_mark_no,
                         bar_mark_label(num_stirrups, stirrup_dia, stirrup_mark_no, name, spacing_mm=spacing_mm)))

        beam_summaries.append({"beam": name, "section": beam["section"], "cover_mm": cover_mm, "entries": entries})

    rows = []
    for shape in registry.rows():
        unit_weight = bar_weight_kg_per_m(shape["diameter_mm"], standards)
        total_length_m = round(shape["qty"] * shape["length_mm"] / 1000, 3)
        total_weight_kg = round(total_length_m * unit_weight, 3)
        rows.append({
            "no": shape["mark_no"],
            "size": shape["diameter_mm"],
            "qua": shape["qty"],
            "length_mm": shape["length_mm"],
            "form": shape["shape"],
            "members": shape["members"],
            "unit_weight_kg_m": unit_weight,
            "total_length_m": total_length_m,
            "total_weight_kg": total_weight_kg,
        })

    return beam_summaries, rows, steel_beams


def build_steel_schedule(steel_columns, steel_beams):
    """Simple section schedule for BS 5950 steel members -- no bar bending applies
    to an off-the-shelf rolled section, so this is a much shorter table than the
    RC BBS: designation, grade, and the capacity check Claude reported."""
    rows = []
    for col in steel_columns:
        rows.append({
            "name": col["name"], "type": "Column", "section": col["section"],
            "grade": col.get("grade", "S275"), "load_kn": col.get("load_kn"),
            "capacity_check": col.get("capacity_check", ""),
        })
    for beam in steel_beams:
        rows.append({
            "name": beam["name"], "type": "Beam", "section": beam["section"],
            "grade": beam.get("grade", "S275"), "load_kn": None,
            "capacity_check": beam.get("capacity_check", ""),
        })
    return rows


def schedule_totals_by_diameter(rows, weight_key="total_weight_kg", length_key="total_length_m"):
    totals = {}
    for row in rows:
        dia_key = f"Y{row['size']}"
        bucket = totals.setdefault(dia_key, {"length_m": 0.0, "weight_kg": 0.0})
        bucket["length_m"] = round(bucket["length_m"] + row[length_key], 3)
        bucket["weight_kg"] = round(bucket["weight_kg"] + row[weight_key], 3)
    return totals


def build_bbs(column_schedule, beam_rows, steel_schedule=None):
    column_rows = [row for col in column_schedule for row in col["bm_rows"]]
    column_rows_flat = [{"size": r["size"], "total_length_m": r["total_length_m"],
                          "total_weight_kg": r["total_weight_kg"]} for r in column_rows]
    beam_rows_flat = [{"size": r["size"], "total_length_m": r["total_length_m"],
                        "total_weight_kg": r["total_weight_kg"]} for r in beam_rows]

    column_totals = schedule_totals_by_diameter(column_rows_flat)
    beam_totals = schedule_totals_by_diameter(beam_rows_flat)

    grand_total_kg = round(
        sum(b["weight_kg"] for b in column_totals.values()) +
        sum(b["weight_kg"] for b in beam_totals.values()), 3
    )

    return {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "standards_reference": "BS 8110-1:1997 / BS 4466 & BS 8666 / BS 4449",
        "column_schedule": column_schedule,
        "column_totals_by_diameter": column_totals,
        "beam_schedule_rows": beam_rows,
        "beam_totals_by_diameter": beam_totals,
        "grand_total_steel_weight_kg": grand_total_kg,
        "steel_schedule": steel_schedule or [],
    }


def save_bbs(bbs, output_file="bbs_output.json"):
    output_path = OUTPUT_JSON_DIR / output_file
    with open(output_path, 'w') as f:
        json.dump(bbs, f, indent=2)
    print(f"✓ BBS saved to: {output_path}")
    return output_path


def lisp_text_command(insertion_point, text, height=0.2, rotation=0.0):
    """Build a single AutoLISP command to place one line of text at a point (model units)."""
    x, y = insertion_point
    escaped = text.replace('"', "'")
    return f'  (command "_TEXT" (list {x} {y} 0.0) {height} {rotation} "{escaped}")'


def build_column_annotation_commands(column_schedule, ga_data):
    col_positions = {c["name"]: (c["x"], c["y"]) for c in ga_data["columns"]}
    commands = []
    for col in column_schedule:
        point = col_positions.get(col["column"])
        if point is None:
            continue
        label = " / ".join(row["bar_mark"] for row in col["bm_rows"])
        commands.append(lisp_text_command(point, label))
    return commands


def build_beam_annotation_commands(beam_summaries, design_data, ga_data):
    col_positions = {c["name"]: (c["x"], c["y"]) for c in ga_data["columns"]}
    ga_beams = {b["name"]: b for b in ga_data["beams"]}
    commands = []

    for summary in beam_summaries:
        ga_beam = ga_beams.get(summary["beam"])
        if not ga_beam:
            continue
        try:
            x1, y1 = col_positions[ga_beam["start_col"]]
            x2, y2 = col_positions[ga_beam["end_col"]]
        except KeyError:
            continue

        midpoint = ((x1 + x2) / 2, (y1 + y2) / 2)
        label = f"{summary['beam']}: " + " / ".join(entry[5] for entry in summary["entries"])
        commands.append(lisp_text_command(midpoint, label))

    return commands


def build_column_schedule_table_commands(bbs, anchor_point, row_height=0.35):
    x0, y0 = anchor_point
    commands = [lisp_text_command((x0, y0), "COLUMN REINFORCEMENT SCHEDULE", height=0.3)]
    commands.append(lisp_text_command((x0, y0 - row_height),
                                       "COLUMN / BM / SIZE / No.of bars / No.Thus / Total No. / Length(mm) / FORM", height=0.16))

    row_i = 2
    for col in bbs["column_schedule"]:
        for r in col["bm_rows"]:
            line = (f"{col['column']} / {r['bm']} / Y{r['size']} / {r['no_of_bars']} / "
                    f"{r['no_thus']} / {r['total_no']} / {r['length_mm']} / {r['form']} "
                    f"({r['bar_mark']})")
            commands.append(lisp_text_command((x0, y0 - row_height * row_i), line, height=0.15))
            row_i += 1

    row_i += 1
    for dia_key, totals in bbs["column_totals_by_diameter"].items():
        line = f"{dia_key}: LENGTH {totals['length_m']} m, WEIGHT {totals['weight_kg']} kg"
        commands.append(lisp_text_command((x0, y0 - row_height * row_i), line, height=0.18))
        row_i += 1

    return commands, row_i


def build_beam_schedule_table_commands(bbs, anchor_point, row_height=0.35):
    x0, y0 = anchor_point
    commands = [lisp_text_command((x0, y0), "BEAM REINFORCEMENT SCHEDULE (BBS)", height=0.3)]
    commands.append(lisp_text_command((x0, y0 - row_height),
                                       "NO / SIZE / QUA / LENGTH(mm) / FORM", height=0.16))

    row_i = 2
    for r in bbs["beam_schedule_rows"]:
        members = ",".join(r["members"])
        line = f"{r['no']} / Y{r['size']} / {r['qua']} / {r['length_mm']} / {r['form']} ({members})"
        commands.append(lisp_text_command((x0, y0 - row_height * row_i), line, height=0.15))
        row_i += 1

    row_i += 1
    for dia_key, totals in bbs["beam_totals_by_diameter"].items():
        line = f"{dia_key}: LENGTH {totals['length_m']} m, WEIGHT {totals['weight_kg']} kg"
        commands.append(lisp_text_command((x0, y0 - row_height * row_i), line, height=0.18))
        row_i += 1

    row_i += 1
    commands.append(lisp_text_command((x0, y0 - row_height * row_i),
                                       f"T.WEIGHT (ALL SCHEDULES): {bbs['grand_total_steel_weight_kg']} kg", height=0.22))

    return commands


def write_lisp_file(column_commands, beam_commands, column_table_commands, beam_table_commands,
                     output_file="oracle_details.lsp"):
    output_path = OUTPUT_LISP_DIR / output_file
    timestamp = datetime.now().isoformat(timespec="seconds")

    lines = [
        ";; Auto-generated by Oracle lisp_detail_generator.py",
        f";; Generated: {timestamp}",
        ";; Load in AutoCAD (APPLOAD) then run the command: ORACLEDETAIL",
        "",
        "(defun c:ORACLEDETAIL ( / )",
        '  (command "_LAYER" "_M" "ORACLE-BARMARKS" "_C" "2" "" "")',
    ]
    lines.extend(column_commands)
    lines.extend(beam_commands)
    lines.append('  (command "_LAYER" "_M" "ORACLE-BBS" "_C" "3" "" "")')
    lines.extend(column_table_commands)
    lines.extend(beam_table_commands)
    lines.append('  (princ "\\nOracle bar marks and BBS tables placed.")')
    lines.append("  (princ)")
    lines.append(")")
    lines.append("")

    with open(output_path, 'w') as f:
        f.write("\n".join(lines))

    print(f"✓ LISP file saved to: {output_path}")
    return output_path


def main():
    print("=== Oracle Phase 5: LISP Detail Generation ===\n")

    standards = load_standards()
    design_data = load_design()
    ga_data = load_ga()

    print("Building column reinforcement schedule...")
    column_schedule, steel_columns = build_column_schedule(design_data, ga_data, standards)
    print(f"  {len(column_schedule)} concrete columns, {len(steel_columns)} steel columns")

    print("Building beam reinforcement schedule (deduplicated bar marks)...")
    beam_summaries, beam_rows, steel_beams = build_beam_schedule(design_data, ga_data, standards)
    print(f"  {len(beam_rows)} distinct bar shapes across {len(beam_summaries)} concrete beams, "
          f"{len(steel_beams)} steel beams")

    steel_schedule = build_steel_schedule(steel_columns, steel_beams)

    print("\nGenerating bar bending schedules...")
    bbs = build_bbs(column_schedule, beam_rows, steel_schedule)
    print(f"  Grand total RC steel weight: {bbs['grand_total_steel_weight_kg']} kg")
    if steel_schedule:
        print(f"  {len(steel_schedule)} structural steel members (see steel_schedule)")
    for dia, totals in bbs["column_totals_by_diameter"].items():
        print(f"    Columns {dia}: {totals['weight_kg']} kg")
    for dia, totals in bbs["beam_totals_by_diameter"].items():
        print(f"    Beams   {dia}: {totals['weight_kg']} kg")
    save_bbs(bbs)

    print("\nGenerating LISP annotations...")
    bounds = grid_bounds_from_ga(ga_data)

    column_commands = build_column_annotation_commands(column_schedule, ga_data)
    beam_commands = build_beam_annotation_commands(beam_summaries, design_data, ga_data)

    column_table_anchor = (bounds[1] + 3.0, bounds[3])
    column_table_commands, rows_used = build_column_schedule_table_commands(bbs, column_table_anchor)
    beam_table_anchor = (bounds[1] + 3.0, bounds[3] - 0.35 * (rows_used + 2))
    beam_table_commands = build_beam_schedule_table_commands(bbs, beam_table_anchor)

    write_lisp_file(column_commands, beam_commands, column_table_commands, beam_table_commands)

    print("\n✓ Phase 5 complete!")
    return bbs


if __name__ == "__main__":
    main()
