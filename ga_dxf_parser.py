"""Oracle — Multi-Floor GA DXF Parser

Purpose:
    Parses an already-designed multi-floor structural general-arrangement DXF: detects levels
    from layer names, extracts beams, columns and VOID regions, builds joints and members,
    infers slab panels, and generates a STAAD.Pro command file.

Role in Oracle:
    Legacy CAD interpretation layer for the multi-floor path. Produces its own dict/tuple model
    and ParseIssue objects, not oracle.core objects. During V2 migration it is expected to feed
    oracle.core.BuildingModel through an adapter.

Dependencies:
    ezdxf (imported inside parse_multilevel_ga); shapely (slab-panel detection).

Consumers:
    oracle_wizard (multi-floor import, model build and analysis steps).

Status:
    Legacy / Transitional.

Migration:
    Retained during V2 migration. Do not replace until the new DXF adapter and regression tests
    are proven.

Details (original module notes, retained):
    Multi-level GA DXF -> STAAD.Pro command file parser.

    Implements a "detect the pattern, don't hard-code the levels" workflow:
    given a DXF containing an ALREADY-DESIGNED multi-floor structural GA (real
    beam/column layers per floor, e.g. "F.F BEAMS" / "COLUMN G-1" / "R. BEAMS"),
    extract joint coordinates and member connectivity directly from the DXF
    geometry, infer slab panels from enclosed beam-bounded regions (excluding
    any VOID-layer areas), and produce a ready-to-run STAAD SPACE command file.

    This is a DIFFERENT path through Oracle from claude_ga_generator.py: that
    module has Claude DESIGN a new layout from a simple architectural drawing.
    This module converts an ALREADY-DESIGNED multi-floor GA directly into an
    analysis model -- no AI layout design involved, no Claude API call at all.

    Known scope/limitations (also surfaced per-project in validate()'s warnings):
    - Member cross-sections use the standard sizes in DEFAULT_SIZES uniformly.
      Real GA drawings commonly draw beams as bare centerlines with no width
      geometry and uniform lineweight/color (true of the reference file this
      was built against), leaving no reliable secondary signal to detect
      non-standard depths from geometry alone -- override sections by hand
      where the drawing shows something deeper (e.g. a transfer beam).
    - Slab panel detection uses a rectilinear grid-cell test (candidate bays
      from the level's unique beam X/Y coordinates, kept only if all 4 edges
      are covered by real beam segments), not a fully general planar-
      subdivision face finder. This covers the overwhelmingly common case of
      an orthogonal structural grid; a bay with a non-rectangular (angled)
      boundary will not be detected as a single panel.
    - Beam-layer-to-level-position matching uses the layer name's first
      letter against common ordinal abbreviations (G/F/S/T/Fo/Fi/Si/Se/E/N/Te),
      with "R..." always placed last. An unrecognised scheme falls back to the
      layer table's file order and is flagged in validate() rather than
      silently trusted.
"""

import re
from itertools import combinations

MM_PER_M = 1000.0
JOINT_TOLERANCE_MM = 1.0  # per spec: dedupe coincident points within ~1mm
# A column's plan position comes from its cross-section outline's centroid, while a
# beam's joint comes from its drawn endpoint -- in a real, not-perfectly-drafted GA
# these are frequently a few mm to a few cm apart even though they're meant to be the
# same physical connection. JOINT_TOLERANCE_MM is deliberately tight (an exact-match
# dedup for genuinely coincident points); this is the separate, looser tolerance used
# only when placing a column joint, to snap it onto a nearby beam-derived joint instead
# of creating a spurious near-duplicate node. Too large risks merging two genuinely
# distinct, closely-spaced columns (e.g. either side of a stair core) into one node --
# 75mm comfortably covers realistic drafting slop while staying well under any normal
# column-to-column spacing.
COLUMN_SNAP_TOL_MM = 75.0

ORDINAL_FIRST_LETTER_RANK = {
    "G": 0, "F": 1, "S": 2, "T": 3, "N": 9,  # N only matches here if not "Ninth" ambiguity; see note below
}
# Explicit two-letter forms take priority over the bare first-letter table above,
# since single letters alone can't disambiguate Fourth/Fifth/Sixth/Seventh/Eighth.
ORDINAL_PREFIX_RANK = {
    "G": 0, "F": 1, "S": 2, "T": 3, "FO": 4, "FI": 5, "SI": 6, "SE": 7, "E": 8, "N": 9, "TE": 10,
}

DEFAULT_SIZES = {
    "column_mm": (225, 225),
    "beam_mm": (225, 450),
    "roof_beam_mm": (225, 300),
}
SLAB_THICKNESS_MM = 150
MATERIAL = {"fcu_n_mm2": 25, "unit_weight_kn_m3": 24, "fy_n_mm2": 410}

# Fallback per-level loading, used only for any level the caller doesn't supply an
# explicit value for in per_level_loading (see parse_multilevel_ga) -- real loads
# should come from the engineer (occupancy per floor, actual slab thickness), not
# these placeholders. Matches the office/general-use default used elsewhere in Oracle.
DEFAULT_IMPOSED_KN_M2 = 2.5
DEFAULT_FINISHES_KN_M2 = 1.5


class ParseIssue:
    def __init__(self, kind, message):
        self.kind = kind  # "warning" (proceed) or "blocking" (must ask before continuing)
        self.message = message

    def __repr__(self):
        return f"[{self.kind}] {self.message}"


# ---------------------------------------------------------------- level detection

def detect_levels(doc):
    """Scan every layer name for the beam/column pattern and build the ordered
    level sequence from whatever tags are actually present. Returns
    (levels, beam_layer_by_level, column_layer_by_boundary, issues) where
    `levels` is an ordered list of level-tag strings from Ground to Roof."""
    issues = []
    layer_names = [l.dxf.name for l in doc.layers]

    column_pairs = []  # (lo_tag, hi_tag, layer_name), in file order
    beam_layers = []   # (tag, layer_name), in file order
    for name in layer_names:
        m = re.match(r"^COLUMN\s+(\S+)-(\S+)$", name, re.IGNORECASE)
        if m:
            column_pairs.append((m.group(1).upper(), m.group(2).upper(), name))
            continue
        m = re.match(r"^(.+?)\.?\s*BEAMS?$", name, re.IGNORECASE)
        if m and "COLUMN" not in name.upper():
            tag = m.group(1).strip().rstrip(".").strip()
            if tag:
                beam_layers.append((tag.upper(), name))

    if not column_pairs:
        issues.append(ParseIssue("blocking", "No 'COLUMN <a>-<b>' layers found -- can't determine the level sequence."))
        return [], {}, {}, issues

    # Chain the column tag-pairs into an ordered sequence, starting from whichever
    # tag never appears as a "hi" (that's Ground) and flag any break in the chain
    # rather than guessing what's missing.
    los = {lo for lo, hi, _ in column_pairs}
    his = {hi for lo, hi, _ in column_pairs}
    starts = los - his
    if len(starts) != 1:
        issues.append(ParseIssue(
            "blocking",
            f"Column layer tags don't chain into a single sequence (candidate start points: "
            f"{sorted(starts) or 'none'}). Check for a typo or a missing 'COLUMN a-b' layer."
        ))
        return [], {}, {}, issues

    chain_map = {lo: (hi, layer) for lo, hi, layer in column_pairs}
    levels = [next(iter(starts))]
    column_layer_by_boundary = {}
    seen = set()
    current = levels[0]
    while current in chain_map:
        if current in seen:
            issues.append(ParseIssue("blocking", f"Column layer tag chain loops back on itself at '{current}'."))
            break
        seen.add(current)
        nxt, layer = chain_map[current]
        column_layer_by_boundary[(current, nxt)] = layer
        levels.append(nxt)
        current = nxt

    if len(column_layer_by_boundary) != len(column_pairs):
        unused = len(column_pairs) - len(column_layer_by_boundary)
        issues.append(ParseIssue(
            "warning",
            f"{unused} 'COLUMN a-b' layer(s) don't chain onto the main sequence {levels} -- "
            "check for a duplicate or disconnected level range."
        ))

    # Match beam layers to the non-ground levels positionally, by rank.
    non_ground_levels = levels[1:]

    def rank(tag, file_index):
        key = tag.upper().replace(".", "").replace(" ", "")
        if key.startswith("R"):
            return (1, 0)  # Roof: always last
        for length in (2, 1):
            prefix = key[:length]
            if prefix in ORDINAL_PREFIX_RANK:
                return (0, ORDINAL_PREFIX_RANK[prefix])
        return (0, 1000 + file_index)  # unrecognised scheme -- fall back to file order, flagged below

    ranked_beams = sorted(
        ((rank(tag, i), tag, layer) for i, (tag, layer) in enumerate(beam_layers)),
        key=lambda r: r[0],
    )
    for _, tag, layer in ranked_beams:
        if rank(tag, 0)[1] >= 1000:
            issues.append(ParseIssue(
                "warning",
                f"Couldn't confidently place beam layer '{layer}' in the level sequence by its name "
                "-- placed by file order instead. Confirm this lines up with the right floor."
            ))

    beam_layer_by_level = {}
    if len(ranked_beams) != len(non_ground_levels):
        issues.append(ParseIssue(
            "blocking",
            f"{len(non_ground_levels)} non-ground level(s) detected from column layers ({levels}) but "
            f"{len(ranked_beams)} beam layer(s) found ({[l for _, _, l in ranked_beams]}). "
            "These should match one-to-one."
        ))
    else:
        for level, (_, tag, layer) in zip(non_ground_levels, ranked_beams):
            beam_layer_by_level[level] = layer

    return levels, beam_layer_by_level, column_layer_by_boundary, issues


# ---------------------------------------------------------------- geometry extraction

def _polyline_segments(entity):
    """Every consecutive vertex pair of a LINE or (LW)POLYLINE, as ((x1,y1),(x2,y2))
    in mm. A multi-vertex polyline (a beam run with a bend) yields one segment
    per bend-to-bend span, not one segment for the whole entity."""
    t = entity.dxftype()
    if t == "LINE":
        s, e = entity.dxf.start, entity.dxf.end
        return [((s.x, s.y), (e.x, e.y))]
    if t in ("LWPOLYLINE", "POLYLINE"):
        pts = [(p[0], p[1]) for p in entity.get_points("xy")] if t == "LWPOLYLINE" else \
              [(v.dxf.location.x, v.dxf.location.y) for v in entity.vertices]
        segs = [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
        if entity.closed and len(pts) > 2:
            segs.append((pts[-1], pts[0]))
        return segs
    return []


def _entities_on_layer(msp, layer_name, dxftypes):
    """ezdxf's query() mini-language doesn't take a comma-separated OR of
    filtered entity types cleanly across versions -- just filter directly."""
    return [e for e in msp if e.dxftype() in dxftypes and e.dxf.layer == layer_name]


def extract_beam_segments_mm(msp, layer_name):
    segments = []
    for e in _entities_on_layer(msp, layer_name, {"LINE", "LWPOLYLINE", "POLYLINE"}):
        segments.extend(_polyline_segments(e))
    return segments


def extract_column_positions_mm(msp, layer_name):
    """Each column is drawn as its cross-section outline (a closed rectangle) --
    return each one's centroid as the column's plan position."""
    positions = []
    for e in _entities_on_layer(msp, layer_name, {"LWPOLYLINE", "POLYLINE"}):
        segs = _polyline_segments(e)
        pts = {p for seg in segs for p in seg}
        if len(pts) < 3:
            continue
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        positions.append((cx, cy))
    return positions


def extract_void_regions_mm(msp, layer_name="VOID"):
    """Point-in-polygon test targets: centroid of each closed shape on the VOID
    layer (if present at all -- most GA drawings that don't use this convention
    will simply have zero results here, which is fine)."""
    if layer_name not in {l.dxf.name for l in msp.doc.layers}:
        return []
    centroids = []
    for e in _entities_on_layer(msp, layer_name, {"LWPOLYLINE", "POLYLINE"}):
        segs = _polyline_segments(e)
        pts = {p for seg in segs for p in seg}
        if pts:
            centroids.append((sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)))
    return centroids


# ---------------------------------------------------------------- joints/members

class JointRegistry:
    """Dedupes joints by (x, y, z) within JOINT_TOLERANCE_MM, converting to metres
    on the way in. Joint numbers are assigned in first-seen order starting at 1."""

    def __init__(self):
        self._by_key = {}
        self._coords = []  # index -> (x_m, y_m, z_m)
        self._next = 1

    def _key(self, x_mm, y_mm, z_mm):
        tol = JOINT_TOLERANCE_MM
        return (round(x_mm / tol), round(y_mm / tol), round(z_mm / tol))

    def get_or_create(self, x_mm, y_mm, z_mm):
        key = self._key(x_mm, y_mm, z_mm)
        if key in self._by_key:
            return self._by_key[key]
        joint_no = self._next
        self._next += 1
        self._by_key[key] = joint_no
        self._coords.append((x_mm / MM_PER_M, y_mm / MM_PER_M, z_mm / MM_PER_M))
        return joint_no

    def find_nearby(self, x_mm, y_mm, z_mm, tol_mm):
        """Nearest existing joint at the same elevation within tol_mm (plan distance
        only), or None. Elevation (y) is matched exactly -- it comes from the same
        storey-height value for every joint at a level, not imprecise CAD geometry,
        so there's no reason to fuzz it the way x/z need to be."""
        y_m = y_mm / MM_PER_M
        tol_m = tol_mm / MM_PER_M
        best_no, best_dist = None, tol_m
        for i, (x_m, y2_m, z_m) in enumerate(self._coords):
            if y2_m != y_m:
                continue
            dist = ((x_m - x_mm / MM_PER_M) ** 2 + (z_m - z_mm / MM_PER_M) ** 2) ** 0.5
            if dist <= best_dist:
                best_no, best_dist = i + 1, dist
        return best_no

    def get_or_create_near(self, x_mm, y_mm, z_mm, snap_tol_mm):
        """Like get_or_create, but first tries to reuse a nearby existing joint
        (within snap_tol_mm) rather than only an exact match -- returns
        (joint_no, snapped_distance_mm_or_None), the distance only set when an
        existing joint was reused instead of an exact/new one, for reporting."""
        exact = self._by_key.get(self._key(x_mm, y_mm, z_mm))
        if exact is not None:
            return exact, None
        nearby = self.find_nearby(x_mm, y_mm, z_mm, snap_tol_mm)
        if nearby is not None:
            nx, ny, nz = self._coords[nearby - 1]
            dist_mm = ((nx * MM_PER_M - x_mm) ** 2 + (nz * MM_PER_M - z_mm) ** 2) ** 0.5
            return nearby, dist_mm
        return self.get_or_create(x_mm, y_mm, z_mm), None

    def coordinates(self):
        """{joint_no: (x_m, y_m, z_m)}, 1-indexed to match assignment order."""
        return {i + 1: c for i, c in enumerate(self._coords)}


def _bbox_min(points):
    return min(p[0] for p in points), min(p[1] for p in points)


def compute_floor_alignment(doc, levels, beam_layer_by_level, column_layer_by_boundary):
    """Real GA sheets commonly draw every floor's plan at its own location on the
    sheet -- side by side, for presentation -- even though every floor shares the
    same real-world X/Z footprint (that's the entire point of a structural grid:
    column line "2" is the same X position on every floor). Taking each layer's
    raw DXF coordinates at face value, as the rest of this module otherwise does,
    scatters the floors across the sheet's X/Y instead of stacking them at a
    shared footprint with only Y (elevation) differing.

    This detects that per-layer offset and corrects for it: take one level's beam
    layer as the reference footprint, then for every other beam layer and every
    column layer, match its own geometry's bounding-box min-corner to the
    reference's min-corner and record the (dx, dy) translation needed. This
    assumes each floor's drawn footprint shares a common reference corner --
    true whenever the same grid intersection (e.g. gridline 1/A) is that floor's
    plan origin, which is the normal case for a regular structural grid. A floor
    genuinely stepped back or extended at that corner (not just elsewhere) will
    misalign -- there is no grid-label text matching here, only bounding-box
    geometry, so this is flagged as a warning rather than applied silently.

    Returns ({"beam": {level: (dx, dy)}, "column": {(lo, hi): (dx, dy)}}, issues)."""
    msp = doc.modelspace()
    issues = []
    reference_level = levels[1] if len(levels) > 1 else None
    ref_layer = beam_layer_by_level.get(reference_level) if reference_level else None
    ref_points = [p for seg in extract_beam_segments_mm(msp, ref_layer) for p in seg] if ref_layer else []
    if not ref_points:
        return {"beam": {}, "column": {}}, issues
    ref_min = _bbox_min(ref_points)

    beam_offsets = {reference_level: (0.0, 0.0)}
    for level, layer in beam_layer_by_level.items():
        if level == reference_level:
            continue
        pts = [p for seg in extract_beam_segments_mm(msp, layer) for p in seg]
        if not pts:
            continue
        min_x, min_y = _bbox_min(pts)
        dx, dy = ref_min[0] - min_x, ref_min[1] - min_y
        beam_offsets[level] = (dx, dy)
        if abs(dx) > 1.0 or abs(dy) > 1.0:  # >1mm: floors were genuinely offset, not already stacked
            issues.append(ParseIssue(
                "warning",
                f"Floor \"{level}\" was drawn offset from floor \"{reference_level}\" on the sheet by "
                f"({dx/MM_PER_M:.2f}m, {dy/MM_PER_M:.2f}m) in plan -- aligned by matching each floor's "
                f"bounding-box corner. Verify this against the real grid if the floors' footprints "
                f"aren't identical (e.g. a stepped-back roof)."
            ))

    column_offsets = {}
    for boundary, layer in column_layer_by_boundary.items():
        pts = extract_column_positions_mm(msp, layer)
        if not pts:
            continue
        min_x, min_y = _bbox_min(pts)
        dx, dy = ref_min[0] - min_x, ref_min[1] - min_y
        column_offsets[boundary] = (dx, dy)

    return {"beam": beam_offsets, "column": column_offsets}, issues


def build_model(doc, levels, beam_layer_by_level, column_layer_by_boundary, storey_heights_m):
    """storey_heights_m: {level_tag: cumulative_elevation_m_from_ground}, one
    entry per level in `levels` (levels[0], Ground, is always 0.0).
    Returns a dict: joints, members (list of (no, j1, j2, kind, level, size_key)),
    slab panels per level, and any ParseIssues raised along the way (column
    positions with no matching joint at one of their two boundary elevations,
    floor-alignment offsets applied, etc.) -- these are added to whatever issue
    list the caller passes in."""
    msp = doc.modelspace()
    joints = JointRegistry()
    offsets, issues = compute_floor_alignment(doc, levels, beam_layer_by_level, column_layer_by_boundary)

    # ---- beams: one member per polyline segment, all endpoints at that level's Y ----
    # A duplicate member (same joint pair, same kind) means two entities in the source
    # drawing produced the identical line -- a retraced beam, or an overlapping
    # LINE + LWPOLYLINE on the same layer, both observed in real files. Deduped by
    # (kind, {j1,j2}) as each member is added, for any drawing, not tuned to one file.
    members = []
    member_no = 1
    seen_member_keys = set()
    duplicate_member_count = 0
    level_beam_segments_m = {}  # level -> [((x1,z1),(x2,z2))] in metres, for slab detection
    for level in levels[1:]:
        layer = beam_layer_by_level.get(level)
        if not layer:
            continue
        y_m = storey_heights_m[level]
        dx, dy = offsets["beam"].get(level, (0.0, 0.0))
        segs_mm = extract_beam_segments_mm(msp, layer)
        segs_m = []
        for (x1, z1), (x2, z2) in segs_mm:
            x1, z1, x2, z2 = x1 + dx, z1 + dy, x2 + dx, z2 + dy
            j1 = joints.get_or_create(x1, y_m * MM_PER_M, z1)
            j2 = joints.get_or_create(x2, y_m * MM_PER_M, z2)
            if j1 == j2:
                continue
            key = ("beam", frozenset((j1, j2)))
            if key in seen_member_keys:
                duplicate_member_count += 1
                continue
            seen_member_keys.add(key)
            size_key = "roof_beam_mm" if level == levels[-1] else "beam_mm"
            members.append((member_no, j1, j2, "beam", level, size_key))
            member_no += 1
            segs_m.append(((x1 / MM_PER_M, z1 / MM_PER_M), (x2 / MM_PER_M, z2 / MM_PER_M)))
        level_beam_segments_m[level] = segs_m

    # ---- columns: match plan positions across the two layers bounding each range ----
    # Columns snap onto a nearby existing (beam-derived) joint within COLUMN_SNAP_TOL_MM
    # instead of always creating one from the column outline's own centroid -- see
    # JointRegistry.get_or_create_near and the constant's docstring for why.
    snapped_column_joints = []  # (distance_mm,) for the summary issue below
    for (lo, hi), layer in column_layer_by_boundary.items():
        y_lo, y_hi = storey_heights_m[lo], storey_heights_m[hi]
        dx, dy = offsets["column"].get((lo, hi), (0.0, 0.0))
        positions_mm = extract_column_positions_mm(msp, layer)
        for x_mm, z_mm in positions_mm:
            x_mm, z_mm = x_mm + dx, z_mm + dy
            j_lo, dist_lo = joints.get_or_create_near(x_mm, y_lo * MM_PER_M, z_mm, COLUMN_SNAP_TOL_MM)
            j_hi, dist_hi = joints.get_or_create_near(x_mm, y_hi * MM_PER_M, z_mm, COLUMN_SNAP_TOL_MM)
            for d in (dist_lo, dist_hi):
                if d is not None:
                    snapped_column_joints.append(d)
            if j_lo == j_hi:
                continue
            key = ("column", frozenset((j_lo, j_hi)))
            if key in seen_member_keys:
                duplicate_member_count += 1
                continue
            seen_member_keys.add(key)
            members.append((member_no, j_lo, j_hi, "column", (lo, hi), "column_mm"))
            member_no += 1

    if duplicate_member_count:
        issues.append(ParseIssue(
            "warning",
            f"{duplicate_member_count} duplicate member(s) (same two joints as an existing member, "
            "typically a retraced or overlapping line in the source drawing) were detected and dropped."
        ))
    if snapped_column_joints:
        issues.append(ParseIssue(
            "warning",
            f"{len(snapped_column_joints)} column endpoint(s) were snapped onto a nearby beam joint "
            f"instead of their own drawn centroid (up to {max(snapped_column_joints):.0f}mm away, within "
            f"the {COLUMN_SNAP_TOL_MM:.0f}mm tolerance) -- normal drafting imprecision, but check these "
            "line up with the real grid if that maximum looks large."
        ))

    # ---- slab panels: rectilinear grid-cell test per level ----
    slab_panels = {}
    for level in levels[1:]:
        segs = level_beam_segments_m.get(level, [])
        panels, panel_issues = _detect_slab_panels(segs)
        slab_panels[level] = panels
        issues.extend(panel_issues)

    return {
        "joints": joints,
        "members": members,
        "next_member_no": member_no,
        "slab_panels": slab_panels,
        "issues": issues,
    }


def _snap_coord(v, grid_m):
    return round(round(v / grid_m) * grid_m, 6)


def _detect_slab_panels(segments_m, purity_tol_m=0.02, snap_grid_m=0.005):
    """Extracts enclosed regions from this level's beam centerlines via noded
    polygon extraction (shapely.ops.polygonize, after unary_union nodes the
    segments -- splitting them at every real intersection/T-junction, which
    plain polygonize does not do on its own). This is a proper planar-graph
    face finder, not a small-bay grid heuristic: it correctly handles a
    stepped/L-shaped/notched floor outline as a single panel, as well as a
    regular multi-bay grid, or any mix of the two on the same level.

    Real GA drawings are not drafted to machine precision -- a beam meant to
    be perfectly vertical commonly lands a few mm off dead-straight. Both the
    horizontal/vertical purity check (purity_tol_m, default 20mm) and the
    coordinate snapping (snap_grid_m, default 5mm) are deliberately loose to
    absorb that, well beyond the 1mm joint-dedup tolerance used elsewhere --
    this step only needs "this is meant to be one straight edge", not exact
    geometry. A segment that is neither purely horizontal nor purely vertical
    within that tolerance (a diagonal/bracing line) is excluded and flagged,
    since the panel outline is assumed rectilinear."""
    if not segments_m:
        return [], []

    from shapely.geometry import LineString
    from shapely.ops import polygonize, unary_union

    lines = []
    skipped = 0
    for (x1, z1), (x2, z2) in segments_m:
        if abs(z1 - z2) < purity_tol_m and abs(x1 - x2) >= snap_grid_m:
            z1 = z2 = (z1 + z2) / 2  # snap near-horizontal to exactly level
        elif abs(x1 - x2) < purity_tol_m and abs(z1 - z2) >= snap_grid_m:
            x1 = x2 = (x1 + x2) / 2  # snap near-vertical to exactly plumb
        else:
            skipped += 1
            continue
        p1 = (_snap_coord(x1, snap_grid_m), _snap_coord(z1, snap_grid_m))
        p2 = (_snap_coord(x2, snap_grid_m), _snap_coord(z2, snap_grid_m))
        if p1 != p2:
            lines.append(LineString([p1, p2]))

    issues = []
    if skipped:
        issues.append(ParseIssue(
            "warning",
            f"{skipped} beam segment(s) at this level are neither purely horizontal nor purely "
            "vertical (diagonal/bracing lines?) -- excluded from slab panel detection, which only "
            "handles a rectilinear outline."
        ))
    if not lines:
        return [], issues

    noded = unary_union(lines)  # splits every segment at its real intersections/T-junctions
    panels = []
    for poly in polygonize(noded):
        verts = _simplify_collinear(list(poly.exterior.coords)[:-1])  # shapely repeats point 0 at the end
        if len(verts) <= 4:
            panels.append({"vertices": verts, "centroid": (poly.centroid.x, poly.centroid.y)})
        else:
            # STAAD's ELEMENT INCIDENCES SHELL only takes 3- or 4-noded plates. A panel
            # still non-rectangular after removing T-junction noding artifacts is a
            # genuinely concave/many-sided room (e.g. an L-shape) -- split it into
            # triangles rather than emit an invalid >4-node element.
            for tri in _triangulate_polygon(poly):
                panels.append({"vertices": tri, "centroid": _centroid(tri)})
    return panels, issues


def _centroid(vertices):
    n = len(vertices)
    return (sum(v[0] for v in vertices) / n, sum(v[1] for v in vertices) / n)


def _simplify_collinear(vertices, tol=1e-4):
    """Drop any vertex that lies on the straight line between its two neighbours
    (within tol) -- these are T-junction nodes injected by unary_union() where an
    adjacent bay's beam meets this panel's edge partway along, not a real corner
    of this panel's shape."""
    n = len(vertices)
    if n <= 3:
        return vertices
    kept = []
    for i in range(n):
        a, b, c = vertices[(i - 1) % n], vertices[i], vertices[(i + 1) % n]
        cross = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        if abs(cross) > tol:
            kept.append(b)
    return kept if len(kept) >= 3 else vertices


def _triangulate_polygon(poly):
    """Delaunay-triangulate poly's vertex set, keeping only the triangles that
    actually fall inside poly (shapely.ops.triangulate works off the convex hull
    of the input points, which overshoots for a concave polygon -- filtering by
    "is the triangle's centroid inside the original polygon" discards those)."""
    from shapely.ops import triangulate

    triangles = []
    for tri in triangulate(poly):
        c = tri.centroid
        if poly.contains(c) or poly.boundary.contains(c):
            triangles.append(list(tri.exterior.coords)[:-1])
    return triangles


def exclude_void_panels(slab_panels, void_centroids_by_level):
    """Drop any panel whose polygon contains a VOID-layer centroid. Modifies
    and returns slab_panels; also returns the count of panels excluded."""

    def point_in_poly(pt, verts):
        x, z = pt
        inside = False
        n = len(verts)
        for i in range(n):
            x1, z1 = verts[i]
            x2, z2 = verts[(i + 1) % n]
            if ((z1 > z) != (z2 > z)) and (x < (x2 - x1) * (z - z1) / (z2 - z1 + 1e-12) + x1):
                inside = not inside
        return inside

    excluded = 0
    for level, panels in slab_panels.items():
        voids = void_centroids_by_level.get(level, [])
        if not voids:
            continue
        kept = []
        for panel in panels:
            if any(point_in_poly(v, panel["vertices"]) for v in voids):
                excluded += 1
                continue
            kept.append(panel)
        slab_panels[level] = kept
    return excluded


# ---------------------------------------------------------------- STAAD file generation

def _fmt(n):
    return f"{n:.4f}".rstrip("0").rstrip(".") if "." in f"{n:.4f}" else f"{n:.4f}"


def _wrap_staad_list(numbers, suffix="", max_len=64):
    """STAAD's command-file input has a per-line length limit; a line past it gets
    auto-split by STAAD.Pro itself, and STAAD's own on-screen warning for that
    ("this may not work for all commands") is not theoretical -- observed in
    practice to corrupt an ELEMENT PROPERTY list, silently dropping the
    trailing THICKNESS keyword and producing "INVALID INPUT FOR ELEMENT
    THICKNESS" on an otherwise-correct model. Wrap long id-lists ourselves,
    using STAAD's own explicit line-continuation syntax (a trailing ' -'), so
    the split point is always a safe one and keeps a trailing suffix (a
    keyword like "THICKNESS 0.15") on the final line, never orphaned."""
    tokens = [str(n) for n in numbers]
    lines, current, current_len = [], [], 0
    for tok in tokens:
        add_len = len(tok) + 1
        if current and current_len + add_len > max_len:
            lines.append(" ".join(current) + " -")
            current, current_len = [], 0
        current.append(tok)
        current_len += add_len
    if suffix:
        current.append(suffix)
    lines.append(" ".join(current))
    return lines


def _resolve_level_loading(levels, per_level_loading):
    """Fill in DEFAULT_* for any level (or any field) the caller didn't supply --
    see parse_multilevel_ga's docstring for the expected per_level_loading shape."""
    per_level_loading = per_level_loading or {}
    resolved = {}
    for level in levels[1:]:
        given = per_level_loading.get(level, {})
        resolved[level] = {
            "imposed_kn_m2": given.get("imposed_kn_m2", DEFAULT_IMPOSED_KN_M2),
            "finishes_kn_m2": given.get("finishes_kn_m2", DEFAULT_FINISHES_KN_M2),
            "slab_thickness_mm": given.get("slab_thickness_mm", SLAB_THICKNESS_MM),
        }
    return resolved


def generate_staad_file(levels, model, storey_heights_m, per_level_loading=None, project_title="Oracle imported GA"):
    joints = model["joints"]
    loading = _resolve_level_loading(levels, per_level_loading)
    members = model["members"]
    slab_panels = model["slab_panels"]

    # Build ELEMENT INCIDENCES (and, via joints.get_or_create(), any new panel-corner
    # joints that weren't already an existing beam/column endpoint) BEFORE reading out
    # the joint coordinates below -- get_or_create() mutates the registry, so reading
    # coordinates() first would miss those new joints and reference them in
    # ELEMENT INCIDENCES without ever declaring them in JOINT COORDINATES (the exact
    # "JOINT NO ... DOES NOT EXIST" error STAAD raises when that happens).
    element_start = ((model["next_member_no"] // 1000) + 1) * 1000
    element_no = element_start
    element_lines = []
    level_element_ids = {}
    for level in levels[1:]:
        ids = []
        for panel in slab_panels.get(level, []):
            corner_nos = []
            y_m = storey_heights_m[level]
            for (x_m, z_m) in panel["vertices"]:
                corner_nos.append(joints.get_or_create(x_m * MM_PER_M, y_m * MM_PER_M, z_m * MM_PER_M))
            element_lines.append(f"{element_no} " + " ".join(str(n) for n in corner_nos))
            ids.append(element_no)
            element_no += 1
        level_element_ids[level] = ids

    coords = joints.coordinates()  # read only after every get_or_create() above has run

    lines = ["STAAD SPACE", f"START JOB INFORMATION", f"ENGINEER DATE {__import__('datetime').date.today().isoformat()}",
              "END JOB INFORMATION", "UNIT METER KN", "JOINT COORDINATES"]
    for no in sorted(coords):
        x, y, z = coords[no]
        lines.append(f"{no} {_fmt(x)} {_fmt(y)} {_fmt(z)}")

    lines.append("MEMBER INCIDENCES")
    for no, j1, j2, kind, level, size_key in members:
        lines.append(f"{no} {j1} {j2}")

    if element_lines:
        lines.append("ELEMENT INCIDENCES SHELL")
        lines.extend(element_lines)

    lines += ["DEFINE MATERIAL START", "ISOTROPIC CONCRETE",
              f"E {MATERIAL['fcu_n_mm2'] * 1000:.0f}",  # placeholder E derivation, refined below
              "POISSON 0.17", f"DENSITY {MATERIAL['unit_weight_kn_m3']}", "ALPHA 1e-005",
              "END DEFINE MATERIAL"]
    # Replace the placeholder E with a proper short-term static modulus estimate (BS 8110-2 cl 7.2):
    # Ec ~ 20 + 0.2*fcu (kN/mm^2) for normal-weight concrete -- expressed here in kN/m^2 for STAAD.
    ec_kn_mm2 = 20 + 0.2 * MATERIAL["fcu_n_mm2"]
    for i, ln in enumerate(lines):
        if ln.startswith("E "):
            lines[i] = f"E {ec_kn_mm2 * 1e6:.0f}"

    cw, cd = DEFAULT_SIZES["column_mm"]
    bw, bd = DEFAULT_SIZES["beam_mm"]
    rw, rd = DEFAULT_SIZES["roof_beam_mm"]
    lines.append("MEMBER PROPERTY")
    for no, j1, j2, kind, level, size_key in members:
        w, d = {"column_mm": (cw, cd), "beam_mm": (bw, bd), "roof_beam_mm": (rw, rd)}[size_key]
        lines.append(f"{no} PRIS YD {_fmt(d / 1000)} ZD {_fmt(w / 1000)}")

    if element_lines:
        lines.append("ELEMENT PROPERTY")
        for level in levels[1:]:
            ids = level_element_ids.get(level, [])
            if not ids:
                continue
            thickness_m = loading[level]["slab_thickness_mm"] / MM_PER_M
            lines.extend(_wrap_staad_list(ids, f"THICKNESS {_fmt(thickness_m)}"))

    lines.append("CONSTANTS")
    lines.append("MATERIAL CONCRETE ALL")

    ground_level = levels[0]
    base_joints = sorted({
        (j1 if isinstance(level, tuple) and level[0] == ground_level else None)
        for no, j1, j2, kind, level, size_key in members if kind == "column"
    } - {None})
    lines.append("SUPPORTS")
    if base_joints:
        lines.extend(_wrap_staad_list(base_joints, "PINNED"))

    # Two real load cases (dead, imposed) plus a proper ULS combination, rather than a
    # single pre-factored case -- this lets the engineer inspect unfactored dead/live
    # reactions separately (needed for serviceability checks), not just the combined
    # ULS result. Slab dead/imposed load is applied as a uniform pressure directly on
    # the plate elements (global Y, so it acts downward regardless of each plate's own
    # local-axis/node-winding orientation) -- STAAD distributes it to the supporting
    # beams through the plates' own stiffness, which is more realistic than an
    # estimated tributary-width UDL. Member self-weight (beams/columns) AND each
    # plate's own concrete self-weight are both already covered by SELFWEIGHT, since
    # STAAD derives element self-weight from THICKNESS x DENSITY automatically -- only
    # finishes (screed, services, etc, not part of the plate's own material) needs an
    # explicit dead-load pressure alongside it.
    lines.append("LOAD 1 DEAD (finishes, BS 6399-1)")
    lines.append("SELFWEIGHT Y -1")
    dead_lines = []
    for level in levels[1:]:
        ids = level_element_ids.get(level, [])
        finishes = loading[level]["finishes_kn_m2"]
        if ids and finishes:
            dead_lines.extend(_wrap_staad_list(ids, f"PRESSURE GY {-finishes:.3f}"))
    if dead_lines:
        lines.append("ELEMENT LOAD")
        lines.extend(dead_lines)

    lines.append("LOAD 2 IMPOSED (BS 6399-1, per floor occupancy)")
    live_lines = []
    for level in levels[1:]:
        ids = level_element_ids.get(level, [])
        imposed = loading[level]["imposed_kn_m2"]
        if ids and imposed:
            live_lines.extend(_wrap_staad_list(ids, f"PRESSURE GY {-imposed:.3f}"))
    if live_lines:
        lines.append("ELEMENT LOAD")
        lines.extend(live_lines)

    lines.append("LOAD COMBINATION 3 ULS 1.4DL+1.6LL (BS 8110-1 cl 2.4.3)")
    lines.append("1 1.4 2 1.6")

    lines += ["PERFORM ANALYSIS", "FINISH"]

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- validation

def validate(levels, model, storey_heights_m, beam_layer_by_level):
    issues = list(model["issues"])
    members = model["members"]
    coords = model["joints"].coordinates()

    member_ids = [m[0] for m in members]
    if len(member_ids) != len(set(member_ids)):
        issues.append(ParseIssue("blocking", "Duplicate member numbers were generated -- internal error, please report."))

    seen_pairs = {}
    for no, j1, j2, kind, level, size_key in members:
        key = frozenset((j1, j2))
        if key in seen_pairs:
            issues.append(ParseIssue("warning", f"Members {seen_pairs[key]} and {no} connect the same two joints."))
        else:
            seen_pairs[key] = no
        if j1 not in coords or j2 not in coords:
            issues.append(ParseIssue("blocking", f"Member {no} references a joint that doesn't exist."))

    for level in levels:
        if level not in storey_heights_m:
            issues.append(ParseIssue("blocking", f"No storey height given for level '{level}'."))

    base_columns = [m for m in members if m[3] == "column" and m[4][0] == levels[0]]
    if not base_columns:
        issues.append(ParseIssue("blocking", f"No ground-level ('{levels[0]}') column bases found -- nothing to support."))

    for level in levels[1:]:
        if level not in beam_layer_by_level:
            issues.append(ParseIssue("blocking", f"No beam layer matched to level '{level}'."))

    return issues


# ---------------------------------------------------------------- orchestration

def parse_multilevel_ga(dxf_path, storey_heights_m=None, per_level_loading=None,
                         project_title="Oracle imported GA"):
    """storey_heights_m: {level_tag: cumulative_elevation_m}. If None, only
    level detection runs (use this first to find out what levels/heights to
    ask the engineer for); pass it back in once you have real values to get
    the full model + STAAD file.

    per_level_loading: {level_tag: {"imposed_kn_m2": float, "finishes_kn_m2": float,
    "slab_thickness_mm": float}}, one entry per non-ground level -- this is the
    engineer's real input (occupancy per floor, actual slab thickness), not something
    derivable from the drawing. Any level or field left out falls back to
    DEFAULT_IMPOSED_KN_M2 / DEFAULT_FINISHES_KN_M2 / SLAB_THICKNESS_MM, which is fine
    for a first look but should be confirmed before the analysis is relied on.

    Returns a dict: levels, beam_layer_by_level, column_layer_by_boundary,
    issues, and (only once storey_heights_m is supplied) model, std_text."""
    import ezdxf

    doc = ezdxf.readfile(dxf_path)
    levels, beam_layer_by_level, column_layer_by_boundary, issues = detect_levels(doc)

    result = {
        "levels": levels,
        "beam_layer_by_level": beam_layer_by_level,
        "column_layer_by_boundary": column_layer_by_boundary,
        "issues": issues,
    }
    if not levels or storey_heights_m is None:
        return result

    missing = [lv for lv in levels if lv not in storey_heights_m]
    if missing:
        issues.append(ParseIssue("blocking", f"Missing storey height(s) for: {missing}"))
        return result

    model = build_model(doc, levels, beam_layer_by_level, column_layer_by_boundary, storey_heights_m)

    void_centroids = {}
    msp = doc.modelspace()
    for level in levels[1:]:
        void_centroids[level] = [
            (x / MM_PER_M, z / MM_PER_M) for x, z in extract_void_regions_mm(msp)
        ]
    excluded = exclude_void_panels(model["slab_panels"], void_centroids)
    if excluded:
        model["issues"].append(ParseIssue(
            "warning", f"{excluded} candidate slab panel(s) excluded due to VOID markers."))

    all_issues = validate(levels, model, storey_heights_m, beam_layer_by_level)
    result["model"] = model
    result["issues"] = issues + [i for i in all_issues if i not in issues]
    result["std_text"] = generate_staad_file(levels, model, storey_heights_m, per_level_loading, project_title)
    result["loading"] = _resolve_level_loading(levels, per_level_loading)
    result["void_centroids"] = void_centroids
    return result
