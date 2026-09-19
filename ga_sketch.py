"""Oracle — Layout Sketch Renderer (AI layout path)

Purpose:
    Renders a PNG plan sketch of the AI-generated single-floor layout.

Role in Oracle:
    Legacy preview for the single-floor path. ml_sketch.py is the multi-floor equivalent.

Dependencies:
    matplotlib (Agg backend).

Consumers:
    oracle_wizard.

Status:
    Legacy / Transitional.

Migration:
    Retained; a candidate to merge with ml_sketch.py once both read the core model.

Details (original module notes, retained):
    Renders a simple schematic plan sketch of a generated GA (general
    arrangement) as a PNG, so the engineer can see the layout at a glance instead
    of only reading column/beam counts. Uses matplotlib's non-interactive Agg
    backend explicitly -- this is called from inside oracle_wizard.py's Tkinter
    process, so it must never touch a GUI backend.
"""

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle


def render_ga_sketch(ga_data, output_path, figsize_in=(3.6, 3.6), dpi=100):
    columns = ga_data.get("columns", [])
    beams = ga_data.get("beams", [])
    col_positions = {c["name"]: (c["x"], c["y"]) for c in columns}

    fig, ax = plt.subplots(figsize=figsize_in, dpi=dpi)
    ax.set_aspect("equal")
    ax.set_facecolor("white")
    fig.patch.set_facecolor("white")

    for slab in ga_data.get("slabs", []):
        verts = slab.get("vertices")
        if verts:
            xs = [v[0] for v in verts] + [verts[0][0]]
            ys = [v[1] for v in verts] + [verts[0][1]]
            ax.fill(xs, ys, color="#dbeafe", alpha=0.5, zorder=0)

    for beam in beams:
        p1 = col_positions.get(beam.get("start_col"))
        p2 = col_positions.get(beam.get("end_col"))
        if not p1 or not p2:
            continue
        ax.plot([p1[0], p2[0]], [p1[1], p2[1]], color="#1f6feb", linewidth=2.2, zorder=1,
                solid_capstyle="round")

    xs_all = [c["x"] for c in columns] or [0]
    ys_all = [c["y"] for c in columns] or [0]
    span = max(max(xs_all) - min(xs_all), max(ys_all) - min(ys_all), 1)
    box = span * 0.03

    for col in columns:
        x, y = col["x"], col["y"]
        ax.add_patch(Rectangle((x - box / 2, y - box / 2), box, box,
                                facecolor="#111827", edgecolor="none", zorder=2))
        ax.annotate(col["name"], (x, y), textcoords="offset points", xytext=(0, 9),
                    ha="center", fontsize=7.5, color="#111827")

    pad = span * 0.12
    ax.set_xlim(min(xs_all) - pad, max(xs_all) + pad)
    ax.set_ylim(min(ys_all) - pad, max(ys_all) + pad)
    ax.set_title("Structural layout (sketch)", fontsize=10)
    ax.set_xlabel("metres", fontsize=8)
    ax.set_ylabel("metres", fontsize=8)
    ax.tick_params(labelsize=7)
    ax.grid(True, linestyle=":", alpha=0.4)
    fig.tight_layout()
    fig.savefig(output_path, facecolor="white")
    plt.close(fig)
    return output_path
