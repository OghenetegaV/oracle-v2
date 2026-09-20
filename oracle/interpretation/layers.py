"""Oracle — Layer Semantic Classification

Purpose:
    Decides what each CAD layer is probably for (wall, external wall, door, window, stair, grid, dimension,
    text, level marker, furniture...), with a confidence and the method that produced it. It works from
    configured aliases first, then from the layer NAME broken into tokens (so "A-WALL-EXT", "WALLS",
    "Beam Line", "A-GLAZ-CWMG" and "S-STRS" all resolve without a list of exact names), then from what is
    actually drawn on the layer (all text, all dimensions, block names like "Door 1 - Single"). Qualifiers
    change the class instead of being ignored: EXT/INT split walls, FINISH/PLASTER/TILE make a layer
    "unknown_wall_related" (0.52) rather than a wall, PATT marks a hatch pattern, IDEN marks labels.

Role in Oracle:
    Stage 3 of the architectural pipeline. It exists so Oracle adapts to any architect's layer conventions
    without a giant hard-coded list, and so it never overstates: an unrecognised layer is "unknown" with
    confidence 0, a name that supports two readings scores low, and a structural-discipline prefix on an
    architectural class is noted. A classification is a proposal (INFERRED) the engineer can override; an
    optional advisor (oracle.interpretation.advisor) may propose alternatives for low-confidence layers, but
    only as recommendations that need the engineer's acceptance.

Dependencies:
    oracle.ingestion (DrawingDocument, DrawnEntity); standard library.

Consumers:
    oracle.interpretation.segmentation, .observations, .pipeline; tests.

Status:
    Interpretation (Phase 3).

Migration/Notes:
    The vocabulary is module data and can be extended per project through LayerConfig without code changes.
    Discipline letters follow the common US/UK CAD convention (A architecture, S structure, I interiors,
    G general, P plumbing, E electrical, M mechanical, Q equipment).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Mapping, Optional

_DISCIPLINES = {"A": "architecture", "S": "structure", "I": "interiors", "G": "general", "P": "plumbing",
                "E": "electrical", "M": "mechanical", "Q": "equipment", "L": "landscape", "C": "civil", "F": "fire"}
_ARCHITECTURAL_BASES = {"wall", "door", "window", "stair", "floor", "roof", "ceiling", "balcony", "lift", "ramp"}

_BASES = {
    "wall": ("WALL", "WALLS"), "wall_internal": ("PARTITION", "PARTITIONS"),
    "door": ("DOOR", "DOORS", "DR"), "window": ("WINDOW", "WINDOWS", "WIN", "WDW", "GLAZ", "GLAZING", "GLASS", "CURT", "CURTAIN"),
    "stair": ("STAIR", "STAIRS", "STAIRCASE", "STRS"), "ramp": ("RAMP",), "lift": ("LIFT", "ELEVATOR", "LFT"),
    "handrail": ("HRAL", "HANDRAIL", "RAIL", "BALUSTRADE"),
    "column": ("COL", "COLS", "COLUMN", "COLUMNS"), "beam": ("BEAM", "BEAMS"), "slab": ("SLAB",),
    "foundation": ("FOOTING", "FDN", "FOUNDATION"), "grid": ("GRID", "GRIDS", "AXIS", "AXES", "GRIDLINE", "GRIDLINES"),
    "dimension": ("DIM", "DIMS", "DIMENSION", "DIMENSIONS"), "text_annotation": ("TEXT", "TXT", "NOTE", "NOTES", "ANNOTATION", "ANNOTATIONS"),
    "symbol": ("SYMB", "SYMBOL", "SYMBOLS"), "schedule": ("SCHD", "SCHED", "SCHEDULE"),
    "title_block": ("TITL", "TITLE", "TTLB", "TITLEBLOCK", "BORDER", "SHEET"), "floor": ("FLOR", "FLOOR"),
    "level_marker": ("LEVL", "LEVEL", "LVL"), "roof": ("ROOF",), "ceiling": ("CEIL", "CEILING"),
    "furniture": ("FURN", "FURNITURE"), "casework": ("CASE", "CASEWORK"),
    "sanitary": ("FIXT", "FIXTURE", "SANR", "SANITARY", "PLUMB", "WC", "TOILET"), "services": ("PIPE", "PIPES", "DUCT"),
    "equipment": ("LITE", "LIGHT", "EQPM", "EQUIP"), "area": ("AREA",), "detail_linework": ("DETL", "DETAIL"),
    "generic_model": ("GENM", "GENERIC"), "void": ("VOID",), "balcony": ("BALC", "BALCONY"), "site": ("SITE",),
}
_TOKEN_BASE = {tok: cls for cls, toks in _BASES.items() for tok in toks}
_QUALIFIERS = {
    "ext": ("EXT", "EXTERNAL", "EXTR", "OUTER", "FACADE", "OUTSIDE"),
    "int": ("INT", "INTERNAL", "INTR", "INNER"),
    "finish": ("FINISH", "FINISHES", "FIN", "PLASTER", "TILE", "TILES", "PAINT", "CLADDING", "CLAD", "SKIRTING", "SCREED"),
    "pattern": ("PATT", "PATTERN", "HATCH"), "label": ("IDEN", "IDENT", "TAG", "TAGS", "LABEL", "LBL", "NUMB"),
    "hidden": ("HDLN", "HIDDEN", "HIDD"),
}
_TOKEN_QUALIFIER = {tok: q for q, toks in _QUALIFIERS.items() for tok in toks}
_IGNORED = {"THIN", "MEDM", "MEDIUM", "HEAVY", "WIDE", "FINE", "OVHD", "OVERHEAD", "MBND", "MCUT", "OTLN", "FRAM", "SILL",
            "FULL", "NEW", "EXST", "EXIST", "EXISTING", "DEMO", "ANNO", "CWMG", "GENF", "MRKR", "LINE", "LINES"}
_WEAK_BASES = {"detail_linework", "generic_model", "area", "site"}
# A major group refined by a minor one (NCS style: A-FLOR-LEVL is a level marker within the floor group) is not a conflict.
_REFINEMENTS = {("floor", "level_marker"): "level_marker", ("floor", "handrail"): "handrail", ("door", "window"): "door",
                ("roof", "level_marker"): "level_marker"}
_TEXT_NATURAL = {"text_annotation", "dimension", "schedule", "symbol", "level_marker", "area", "title_block", "unknown",
                 "non_plotting"}


@dataclass(frozen=True)
class LayerConfig:
    """Project-specific knowledge: exact layer names (any spelling of case/punctuation) -> semantic class."""
    aliases: Mapping = field(default_factory=dict)


@dataclass(frozen=True)
class LayerVerdict:
    name: str
    semantic_class: str
    confidence: float
    method: str
    note: Optional[str] = None
    entity_count: int = 0
    alternatives: tuple = ()          # ((class, confidence), ...) other readings the evidence allows


@dataclass
class LayerStats:
    kinds: Counter = field(default_factory=Counter)
    blocks: Counter = field(default_factory=Counter)

    @property
    def total(self) -> int:
        return sum(self.kinds.values())


def _norm(name: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", (name or "").upper())


def tokenise(name: str) -> list:
    """Words of a layer name: split on punctuation and spaces and camelCase; digits-only parts dropped."""
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", name or "")
    return [t for t in re.split(r"[^A-Za-z0-9]+", spaced.upper()) if t and not t.isdigit()]


def _lookup(token: str):
    if token in _TOKEN_BASE:
        return "base", _TOKEN_BASE[token], 0.95
    if token in _TOKEN_QUALIFIER:
        return "qual", _TOKEN_QUALIFIER[token], 1.0
    if len(token) > 3 and token.endswith("S"):
        stem = token[:-1]
        if stem in _TOKEN_BASE:
            return "base", _TOKEN_BASE[stem], 0.88
    return None


def classify_name(name: str, config: Optional[LayerConfig] = None) -> LayerVerdict:
    """Classify from the layer name alone."""
    config = config or LayerConfig()
    aliases = {_norm(k): v for k, v in config.aliases.items()}
    if _norm(name) in aliases:
        return LayerVerdict(name, aliases[_norm(name)], 0.99, "configured_alias", "matches a configured alias")
    tokens = tokenise(name)
    discipline = None
    if len(tokens) > 1 and len(tokens[0]) == 1 and tokens[0] in _DISCIPLINES:
        discipline, tokens = tokens[0], tokens[1:]
    bases, quals, unknown = [], [], []
    for tok in tokens:
        hit = _lookup(tok)
        if hit is None:
            (unknown if tok not in _IGNORED else []).append(tok)
        elif hit[0] == "base":
            bases.append((hit[1], hit[2], tok))
        else:
            quals.append(hit[1])
    if not bases:
        note = f"no recognised word in {tokens}" if tokens else "no recognisable name"
        return LayerVerdict(name, "unknown", 0.0, "none", note)
    specific = [b for b in bases if b[0] not in _WEAK_BASES] or bases
    base, conf, _tok = specific[-1]
    note = None
    distinct = {b[0] for b in specific}
    alternatives = ()
    refined = _REFINEMENTS.get((specific[-2][0], specific[-1][0])) if len(specific) >= 2 else None
    if len(distinct) > 1 and refined and len(distinct) == 2:
        base, conf = refined, min(conf, 0.9)
    elif len(distinct) > 1:
        conf = min(conf, 0.6)
        alternatives = tuple((c, 0.4) for c in sorted(distinct - {base}))
        note = f"the name mentions {sorted(distinct)}; taken as {base}"
    if "ext" in quals and "int" in quals:
        return LayerVerdict(name, base, 0.5, "token_match", "the name says both external and internal", 0, (("wall_external", 0.3), ("wall_internal", 0.3)))
    if base == "wall_internal" and "int" not in quals:
        quals.append("int")
        base = "wall"
    cls = base
    if base == "wall":
        if "finish" in quals:
            return LayerVerdict(name, "unknown_wall_related", 0.52, "token_match",
                                "a wall-related finish layer, not necessarily a structural wall")
        if "pattern" in quals:
            cls = "wall_pattern"
        elif "ext" in quals:
            cls = "wall_external"
        elif "int" in quals or discipline == "I":
            cls = "wall_internal"
            if discipline == "I" and "int" not in quals:
                conf = min(conf, 0.85)
                note = "interiors-discipline wall, taken as internal"
    elif "finish" in quals:
        cls, conf = f"{base}_finish", min(conf, 0.6)
    elif "pattern" in quals:
        cls = f"{base}_pattern"
    if "label" in quals:
        cls = f"{cls}_label"
    elif "hidden" in quals:
        cls = f"{cls}_hidden"
    if discipline and base in _ARCHITECTURAL_BASES and discipline not in ("A", "I"):
        conf = min(conf, 0.8)
        note = (note + "; " if note else "") + f"an architectural class on a {_DISCIPLINES.get(discipline, discipline)}-discipline layer, verify"
    return LayerVerdict(name, cls, round(conf, 2), "token_match", note, 0, alternatives)


def _block_tokens(block_name: str) -> Optional[str]:
    for tok in tokenise(block_name):
        hit = _lookup(tok)
        if hit and hit[0] == "base" and hit[1] in ("door", "window", "stair", "lift", "column", "furniture", "casework",
                                                     "sanitary", "equipment"):
            return hit[1]
    return None


def classify_layers(document, config: Optional[LayerConfig] = None) -> list:
    """One verdict per layer that has entities (and per layer in the table), ordered by name."""
    stats: dict = {}
    for e in document.entities:
        s = stats.setdefault(e.layer, LayerStats())
        s.kinds[e.kind] += 1
        if e.kind == "INSERT" and e.block:
            s.blocks[e.block] += 1
    names = sorted(set(document.layers) | set(stats))
    out = []
    for name in names:
        info = document.layers.get(name)
        if info is not None and info.non_plotting and _norm(name) not in {_norm(k) for k in (config.aliases if config else {})}:
            out.append(LayerVerdict(name, "non_plotting", 0.95, "source_flag", "the source marks this layer as not plotting",
                                    stats.get(name, LayerStats()).total))
            continue
        verdict = classify_name(name, config)
        s = stats.get(name, LayerStats())
        total = s.total
        geometry_only = {k for k in s.kinds if k not in ("TEXT", "DIMENSION")}
        note, conf, cls, method, alternatives = verdict.note, verdict.confidence, verdict.semantic_class, verdict.method, verdict.alternatives
        if total and not geometry_only and cls not in _TEXT_NATURAL and not cls.endswith("_label") \
                and not cls.endswith("_finish"):
            conf = min(conf, 0.55)
            note = (note + "; " if note else "") + "the layer holds only text/dimensions, not the drawn objects its name suggests"
        if conf < 0.6 and total:
            if set(s.kinds) == {"TEXT"}:
                cls, conf, method, note = "text_annotation", 0.85, "geometry", "the layer holds only text"
            elif set(s.kinds) == {"DIMENSION"}:
                cls, conf, method, note = "dimension", 0.9, "geometry", "the layer holds only dimensions"
            elif s.kinds.get("INSERT", 0) >= 0.6 * total and s.blocks:
                votes = Counter(filter(None, (_block_tokens(b) for b in s.blocks.elements())))
                if votes:
                    top, n = votes.most_common(1)[0]
                    if n >= 0.5 * sum(s.blocks.values()):
                        cls, conf, method, note = top, 0.75, "block_name", f"most blocks on the layer are named like a {top}"
        out.append(LayerVerdict(name, cls, conf, method, note, total, alternatives))
    return out
