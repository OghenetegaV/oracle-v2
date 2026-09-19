"""Oracle — Multi-Floor Sketch Renderer

Purpose:
    Renders per-floor PNG plan sketches of the parsed multi-floor model for visual QA.

Role in Oracle:
    Legacy preview for the multi-floor path. ga_sketch.py is the single-floor equivalent.

Dependencies:
    matplotlib (Agg backend); the model dict from ga_dxf_parser.

Consumers:
    oracle_wizard.

Status:
    Legacy / Transitional.

Migration:
    Retained; a candidate to merge with ga_sketch.py once both read the core model.

Details (original module notes, retained):
    Renders a schematic per-floor plan sketch of a parsed multi-level GA model
    (ga_dxf_parser.py's output): beam lines, column markers, each detected slab
    panel shaded and labeled PANEL, and any VOID-layer point marked with a cross
    and labeled VOID. Gives the engineer a visual QA check that panel/void
    detection matched the real drawing intent -- the same role ga_sketch.py plays
    for the AI-designed single-floor layout path.

    Draws from the FINAL, corrected model (model["members"] + model["joints"]),
    not raw DXF geometry -- so what's shown already reflects floor-alignment
    correction and duplicate-member removal, which is the point: this is meant to
    confirm what Oracle actually built, not just what the drawing raw-contains.

    Uses matplotlib's non-interactive Agg backend explicitly -- this runs inside
    oracle_wizard.py's Tkinter process.
"""

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def render_multilevel_sketch(levels, model, storey_heights_m, void_centroids_m, output_path,
                              subplot_size_in=(3.4, 3.0), dpi=105):
    """One PNG, one subplot per non-ground level, left to right ground-up."""
    coords = model["joints"].coordinates()
    non_ground = levels[1:]
    if not non_ground:
        return None

    fig, axes = plt.subplots(1, len(non_ground),
                              figsize=(subplot_size_in[0] * len(non_ground), subplot_size_in[1]),
                              dpi=dpi)
    if len(non_ground) == 1:
        axes = [axes]
    fig.patch.set_facecolor("white")

    for ax, level in zip(axes, non_ground):
        _render_one_level(ax, level, model, storey_heights_m, coords, void_centroids_m.get(level, []))

    fig.tight_layout()
    fig.savefig(output_path, facecolor="white")
    plt.close(fig)
    return output_path


def _render_one_level(ax, level, model, storey_heights_m, coords, void_points_m):
    ax.set_aspect("equal")
    ax.set_facecolor("white")
    y_m = storey_heights_m[level]

    beam_lines = []
    column_points = set()
    for no, j1, j2, kind, lvl, size_key in model["members"]:
        if kind == "beam" and lvl == level:
            x1, _, z1 = coords[j1]
            x2, _, z2 = coords[j2]
            beam_lines.append(((x1, z1), (x2, z2)))
        elif kind == "column":
            for j in (j1, j2):
                x, y, z = coords[j]
                if abs(y - y_m) < 1e-6:
                    column_points.add((round(x, 4), round(z, 4)))

    panels = model["slab_panels"].get(level, [])
    for panel in panels:
        verts = panel["vertices"]
        xs = [v[0] for v in verts] + [verts[0][0]]
        zs = [v[1] for v in verts] + [verts[0][1]]
        ax.fill(xs, zs, color="#dbeafe", edgecolor="#93c5fd", linewidth=0.8, zorder=0)
        cx, cz = panel["centroid"]
        ax.text(cx, cz, "PANEL", ha="center", va="center", fontsize=6.5,
                color="#1e3a8a", zorder=3)

    for x1z1, x2z2 in beam_lines:
        (x1, z1), (x2, z2) = x1z1, x2z2
        ax.plot([x1, x2], [z1, z2], color="#1f2937", linewidth=1.6, zorder=1,
                solid_capstyle="round")

    xs_all = [p[0] for p in column_points] or [0]
    zs_all = [p[1] for p in column_points] or [0]
    span = max(max(xs_all) - min(xs_all), max(zs_all) - min(zs_all), 1)
    box = span * 0.025
    for x, z in column_points:
        ax.add_patch(plt.Rectangle((x - box / 2, z - box / 2), box, box,
                                    facecolor="#111827", edgecolor="none", zorder=2))

    # VOID markers: a cross at each void point (from the VOID layer, if the drawing
    # has one) -- the region it fell inside was already excluded from `panels` above,
    # so this just marks where and labels it, rather than re-deriving that shape.
    mark = span * 0.02
    for vx, vz in void_points_m:
        ax.plot([vx - mark, vx + mark], [vz - mark, vz + mark], color="#b91c1c", linewidth=1.4, zorder=3)
        ax.plot([vx - mark, vx + mark], [vz + mark, vz - mark], color="#b91c1c", linewidth=1.4, zorder=3)
        ax.text(vx, vz - mark * 2.2, "VOID", ha="center", va="top", fontsize=6.5,
                color="#b91c1c", fontweight="bold", zorder=3)

    pad = span * 0.1
    ax.set_xlim(min(xs_all) - pad, max(xs_all) + pad)
    ax.set_ylim(min(zs_all) - pad, max(zs_all) + pad)
    ax.set_title(f"Level {level}  (Y={y_m:.2f}m)", fontsize=9)
    ax.tick_params(labelsize=6)
    ax.grid(True, linestyle=":", alpha=0.3)
