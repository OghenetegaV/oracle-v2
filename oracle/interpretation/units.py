"""Oracle — Drawing Unit Detection

Purpose:
    Estimates what one drawing unit means (mm, cm, m, inch, foot) and how sure that is, from several
    independent kinds of evidence: the file's own unit metadata; whether the drawn distance between a
    dimension's definition points equals its stated value (so dimension values are in drawing units); how
    big the stated dimension values are; and how long typical drawn lines are. Each evidence source is
    reported. The metadata is treated as a claim to check, not the truth: a header that says inches over a
    drawing whose dimensions read 900, 230 and 3000 is reported as a conflict, and the engineer is asked.

Role in Oracle:
    Stage 2 of the architectural pipeline. Every later length depends on it, so it never guesses silently:
    the result says whether the unit is SOURCE (metadata present and corroborated), or only ASSUMED
    (metadata missing, contradicted, or evidence weak), lists the alternatives with confidences, and states
    whether the engineer must confirm. Oracle's canonical unit is millimetres; this decides the factor.

Dependencies:
    oracle.core (UnitEstimate, ValueStatus); oracle.ingestion (DrawingDocument); standard library.

Consumers:
    oracle.interpretation.pipeline; tests.

Status:
    Interpretation (Phase 3).

Migration/Notes:
    The plausibility ranges (an architectural dimension is 150-20000 mm, a typical drawn line 50-10000 mm)
    are heuristics for buildings and are documented in docs/PHASE_3_ARCHITECTURAL_INTERPRETATION.md.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Optional

from oracle.core import UnitEstimate, ValueStatus

_FACTORS = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "inch": 25.4, "foot": 304.8}
_PRIOR = {"mm": 1.0, "m": 0.9, "foot": 0.6, "inch": 0.5, "cm": 0.3}


@dataclass
class UnitVerdict:
    estimate: UnitEstimate
    basis: ValueStatus                       # SOURCE only when the metadata is corroborated; otherwise ASSUMED
    alternatives: list = field(default_factory=list)      # other UnitEstimates, most likely first
    evidence: list = field(default_factory=list)          # (description, weight) pairs, for provenance
    conflicts: list = field(default_factory=list)
    needs_engineer: bool = False


def _plausibility(value_mm: Optional[float], low: float, high: float) -> float:
    if value_mm is None or value_mm <= 0:
        return 1.0
    if low <= value_mm <= high:
        return 1.0
    bound = low if value_mm < low else high
    return math.exp(-2.0 * abs(math.log10(value_mm / bound)))


def _drawn_versus_stated(document) -> Optional[float]:
    """Share of linear dimensions whose drawn extension-point distance equals the stated value (in drawing units)."""
    total = agree = 0
    for e in document.entities:
        if e.kind != "DIMENSION" or not e.value or len(e.points) < 3:
            continue
        (x2, y2), (x3, y3) = e.points[1], e.points[2]
        a = math.radians(e.rotation)
        drawn = abs((x3 - x2) * math.cos(a) + (y3 - y2) * math.sin(a))
        if drawn <= 0:
            continue
        total += 1
        agree += abs(drawn - e.value) <= 0.02 * e.value + 1e-6
    return agree / total if total >= 5 else None


def detect_units(document) -> UnitVerdict:
    dims = [e.value for e in document.entities if e.kind == "DIMENSION" and e.value and e.value > 0]
    lengths = []
    for e in document.entities:
        if e.kind == "LINE" and len(e.points) == 2:
            d = math.hypot(e.points[1][0] - e.points[0][0], e.points[1][1] - e.points[0][1])
            if d > 0:
                lengths.append(d)
    median_dim = statistics.median(dims) if len(dims) >= 5 else None
    median_line = statistics.median(lengths) if len(lengths) >= 20 else None
    agreement = _drawn_versus_stated(document)
    meta = document.declared_unit

    box = document.geometry_box() or document.extents
    span = max(box[2] - box[0], box[3] - box[1]) if box else None
    evidence, conflicts = [], []
    scores = {}
    for unit, factor in _FACTORS.items():
        s = _PRIOR[unit]
        s *= _plausibility(median_dim * factor if median_dim else None, 150.0, 20000.0) ** 2
        s *= _plausibility(median_line * factor if median_line else None, 50.0, 10000.0)
        if span:
            # the drawn extent is weaker evidence than dimensions or line lengths, but it is always available
            s *= _plausibility(span * factor, 3000.0, 500000.0) ** (1.0 if median_dim is None and median_line is None else 0.5)
        if meta == unit:
            s *= 3.0
        elif meta is not None:
            s *= 0.6
        scores[unit] = s
    total = sum(scores.values()) or 1.0
    ranked = sorted(scores, key=lambda u: -scores[u])
    best = ranked[0]
    confidence = scores[best] / total

    if median_dim is not None:
        evidence.append((f"median stated dimension is {median_dim:g} drawing units ({len(dims)} dimensions)", 0.5))
    if agreement is not None:
        evidence.append((f"{agreement:.0%} of dimensions are drawn at their stated length, so dimension values are in "
                         "drawing units", 0.3))
    if median_line is not None:
        evidence.append((f"median drawn line is {median_line:.4g} drawing units", 0.2))
    if meta:
        evidence.append((f"the file's unit metadata says {meta} (the source declares it)", 0.4))
    else:
        evidence.append((f"the file states no usable unit", 0.0))

    if meta and best != meta:
        seen = [f"median dimension {median_dim:.4g}" if median_dim is not None else None,
                f"median line {median_line:.4g}" if median_line is not None else None]
        conflicts.append(f"The file's unit metadata says {meta}, but the drawn dimensions and line lengths indicate "
                         f"{best} ({', '.join(x for x in seen if x) or 'overall size'}).")
    estimates = [UnitEstimate(u, _FACTORS[u], round(scores[u] / total, 3), "extent_and_dimension_plausibility")
                 for u in ranked]
    corroborated = meta is not None and best == meta and (median_dim is not None or median_line is not None)
    if corroborated:
        method, basis = "header_metadata_corroborated", ValueStatus.SOURCE
        confidence = max(confidence, 0.9)
    elif meta and best == meta:
        method, basis = "header_metadata", ValueStatus.SOURCE
        confidence = min(max(confidence, 0.6), 0.75)
    elif meta:
        method, basis = "dimension_plausibility_over_metadata", ValueStatus.ASSUMED
    else:
        method, basis = "extent_and_dimension_plausibility", ValueStatus.ASSUMED
    if basis == ValueStatus.ASSUMED:                 # nothing in the file vouches for it: never present it as near-certain
        confidence = min(confidence, 0.6 if meta else 0.75)
    note = "; ".join(conflicts) if conflicts else None
    estimate = UnitEstimate(best, _FACTORS[best], round(min(confidence, 0.99), 3), method, note)
    needs = bool(conflicts) or basis == ValueStatus.ASSUMED or confidence < 0.8
    return UnitVerdict(estimate, basis, [e for e in estimates if e.unit != best and e.confidence >= 0.05], evidence,
                       conflicts, needs)
