"""Oracle — View Frames and Multi-Floor Alignment

Purpose:
    Builds each view's coordinate frame (source -> view-local: rotated so its dominant walls run along the
    axes and its footprint's minimum corner is the origin) and proposes how the views of different floors
    line up in one building frame. Alignment evidence is, in order: matching GRID LABELS (each label that
    appears in both views votes for a translation; agreement across several labels is strong), then the
    FOOTPRINT (if the two footprints have the same size the corners coincide; if not, min-corner, centre and
    max-corner alignments are different, plausible answers). It reports every candidate with a confidence.

Role in Oracle:
    Stage 7, "multi-floor alignment". Plans arranged side by side on one sheet share one building footprint
    but not one set of coordinates; Oracle proposes the transformation for each and does NOT silently merge
    when the evidence is weak: disagreeing candidates become alternatives for the engineer. Original
    coordinates are untouched; the result is a pair of CoordinateFrames per view (source -> view-local and
    building -> view-local).

Dependencies:
    oracle.core (CoordinateFrame); oracle.interpretation.segmentation (Region); standard library.

Consumers:
    oracle.interpretation.pipeline; tests.

Status:
    Interpretation (Phase 3).

Migration/Notes:
    Only rotation about the dominant wall direction and translation are estimated. Mirroring, a plan rotated
    a further quarter turn from another, and scale differences between views are not detected yet.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import Optional

_TOLERANCE_SIZE = 0.01         # footprints within 1% in both directions are "the same size"


@dataclass
class ViewGeometry:
    rotation_deg: float
    origin_source: tuple            # where the view-local origin is, in source coordinates
    size: tuple                     # (width, depth) of the footprint in view-local coordinates


def _rot(p, deg):
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return (c * p[0] - s * p[1], s * p[0] + c * p[1])


def view_geometry(region, rotation_deg: float) -> ViewGeometry:
    """Rotate the region's geometry by -rotation and put the minimum corner of its footprint at the origin."""
    pts = []
    for e in region.geometry:
        pts.extend(e.points if e.points else [(e.box[0], e.box[1]), (e.box[2], e.box[3])])
    local = [_rot(p, -rotation_deg) for p in pts] or [(0.0, 0.0)]
    lo = (min(p[0] for p in local), min(p[1] for p in local))
    hi = (max(p[0] for p in local), max(p[1] for p in local))
    return ViewGeometry(rotation_deg, _rot(lo, rotation_deg), (hi[0] - lo[0], hi[1] - lo[1]))


def to_local(geo: ViewGeometry, p: tuple) -> tuple:
    q = _rot((p[0] - geo.origin_source[0], p[1] - geo.origin_source[1]), -geo.rotation_deg)
    return q


def grid_label_positions(region, layer_class: dict, geo: ViewGeometry) -> dict:
    """label text -> [positions in view-local coordinates] for short texts on grid-label layers."""
    out: dict = {}
    for t in region.texts:
        if layer_class.get(t.layer) == "grid_label" and 0 < len(t.text.strip()) <= 3:
            out.setdefault(t.text.strip().upper(), []).append(to_local(geo, t.centre))
    return out


def grid_vote(reference: dict, target: dict, scale: float) -> Optional[tuple]:
    """(translation, labels_agreeing, labels_shared) from labels present in both views, or None."""
    shared = set(reference) & set(target)
    if len(shared) < 3:
        return None
    votes: Counter = Counter()
    members: dict = {}
    for label in shared:
        for a in reference[label]:
            for b in target[label]:
                d = (a[0] - b[0], a[1] - b[1])
                key = (round(d[0] / (0.005 * scale)), round(d[1] / (0.005 * scale)))
                votes[key] += 1
                members.setdefault(key, []).append((label, d))
    key, _n = votes.most_common(1)[0]
    labels = {l for l, _d in members[key]}
    ds = [d for _l, d in members[key]]
    return (sum(d[0] for d in ds) / len(ds), sum(d[1] for d in ds) / len(ds)), len(labels), len(shared)


def alignment_candidates(ref_geo: ViewGeometry, ref_grid: dict, geo: ViewGeometry, grid: dict) -> list:
    """[(translation view-local -> building, method, confidence)], most likely first."""
    (rw, rd), (w, d) = ref_geo.size, geo.size
    scale = max(rw, rd, 1e-9)
    same_size = abs(rw - w) <= _TOLERANCE_SIZE * rw and abs(rd - d) <= _TOLERANCE_SIZE * rd
    out = []
    vote = grid_vote(ref_grid, grid, scale)
    if vote is not None:
        translation, agreeing, shared = vote
        out.append((translation, f"grid_labels ({agreeing} of {shared} shared labels agree)",
                    round(0.5 + 0.45 * agreeing / shared, 2)))
    if same_size:
        if out and abs(out[0][0][0]) <= 0.01 * scale and abs(out[0][0][1]) <= 0.01 * scale:
            # the grid labels and the equal footprints say the same thing: one corroborated candidate
            t, m, c = out[0]
            out[0] = (t, m + "; same footprint size", round(min(0.98, c + 0.03), 2))
        else:
            out.append(((0.0, 0.0), "same_footprint_size", 0.8 if not out else 0.4))
    else:
        base = 0.5 if not out else 0.25
        out.append(((0.0, 0.0), "footprint_min_corner", base))
        out.append((((rw - w) / 2, (rd - d) / 2), "footprint_centre", round(base * 0.6, 2)))
        out.append(((rw - w, rd - d), "footprint_max_corner", round(base * 0.4, 2)))
    return sorted(out, key=lambda c: -c[2])
