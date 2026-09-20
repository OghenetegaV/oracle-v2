"""Oracle — Level-Name and View-Title Normalisation

Purpose:
    Turns the free text architects put on drawings into canonical, comparable meaning: which building
    level a text names (GROUND, FLOOR:1, BASEMENT:1, MEZZANINE, ROOF, PENTHOUSE, DATUM), and what kind of
    view a title announces (floor plan, section, elevation, detail, schedule, notes, legend), with its
    variant (blow-up, furniture, dimension drawing), section label ("A-A"), and elevation orientation.
    "GF", "G/F PLAN", "GROUND FLR", "LEVEL 00", "L00" all give GROUND; "02-FIRST FLOOR", "1ST FLR", "FF"
    all give FLOOR:1. Matching tolerates one typo in a keyword ("VIOD" for "VOID", "FRIST" for "FIRST"),
    and says so in the confidence and method.

Role in Oracle:
    The normalisation layer required so Oracle does not depend on any one architect's or country's wording.
    It is extensible: a LevelNamer takes configured aliases (exact text -> level) and a numeric offset for
    conventions where "Level 1" is the ground floor; the vocabulary tables are plain module data. It reads
    text only, never geometry, and always returns a confidence and the method used: a match is a
    proposal for the engineer to review, not a fact.

Dependencies:
    oracle.core (ViewType); standard library.

Consumers:
    oracle.interpretation.segmentation, .vertical, .observations, .reconcile, .pipeline; tests.

Status:
    Interpretation (Phase 3).

Migration/Notes:
    Vocabularies are English/UK-centred with common abbreviations; other languages and local conventions
    are added through configured aliases, not code changes. "LEVEL n" is taken as index n; a project that
    counts the ground floor as Level 1 sets numeric_offset=-1 (or configures aliases).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Optional

from oracle.core import ValidationError, ViewType
from oracle.core.architecture import check_level_key

_ORDINALS = {"FIRST": 1, "SECOND": 2, "THIRD": 3, "FOURTH": 4, "FIFTH": 5, "SIXTH": 6, "SEVENTH": 7, "EIGHTH": 8,
             "NINTH": 9, "TENTH": 10, "ELEVENTH": 11, "TWELFTH": 12}
_FUZZY_WORDS = {**{w: ("ordinal", n) for w, n in _ORDINALS.items()}, "GROUND": ("key", "GROUND"),
                "BASEMENT": ("key", "BASEMENT:1"), "MEZZANINE": ("key", "MEZZANINE"), "PENTHOUSE": ("key", "PENTHOUSE"),
                "ROOF": ("key", "ROOF")}


def normalise(text: str) -> str:
    """Upper case, punctuation to spaces, quotes dropped, runs of spaces collapsed."""
    s = (text or "").upper().replace("'", "").replace('"', "")
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def edit_distance(a: str, b: str) -> int:
    """Optimal string alignment distance (insert, delete, substitute, swap neighbours)."""
    la, lb = len(a), len(b)
    d = [[0] * (lb + 1) for _ in range(la + 1)]
    for i in range(la + 1):
        d[i][0] = i
    for j in range(lb + 1):
        d[0][j] = j
    for i in range(1, la + 1):
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[la][lb]


def fuzzy_match(token: str, word: str) -> bool:
    """True for the exact word, or one typo away. A 4-letter word tolerates only swapped letters (VIOD for VOID), so
    ROOM is not ROOF and AVOID is not VOID; longer words tolerate one insertion, deletion, substitution or swap."""
    if token == word:
        return True
    if len(word) == 4:
        return len(token) == 4 and sorted(token) == sorted(word) and edit_distance(token, word) == 1
    return len(word) >= 5 and abs(len(token) - len(word)) <= 1 and edit_distance(token, word) <= 1


def has_word(text: str, word: str) -> Optional[bool]:
    """None if absent, True if exact, False if only fuzzy (one typo)."""
    tokens = normalise(text).split()
    if word in tokens:
        return True
    return False if any(fuzzy_match(t, word) for t in tokens) else None


@dataclass(frozen=True)
class LevelName:
    key: str
    label: str                 # the text that was matched
    confidence: float
    method: str                # "alias", "keyword", "ordinal", "numeric", "abbreviation", "fuzzy"


_DEFAULT_LEVEL_DISPLAY = {"DATUM": "Natural Ground Level", "GROUND": "Ground Floor", "ROOF": "Roof",
                          "PENTHOUSE": "Penthouse", "MEZZANINE": "Mezzanine"}


def level_display_name(key: str) -> str:
    if key in _DEFAULT_LEVEL_DISPLAY:
        return _DEFAULT_LEVEL_DISPLAY[key]
    kind, _, n = key.partition(":")
    if kind == "NAMED":
        return n.replace("_", " ").title()
    if kind == "FLOOR":
        ordinal = {v: k.title() for k, v in _ORDINALS.items()}.get(int(n), f"Level {n}")
        return f"{ordinal} Floor"
    if kind == "BASEMENT":
        return f"Basement {n}" if n != "1" else "Basement"
    if kind == "MEZZANINE":
        return f"Mezzanine {n}"
    return key


def level_id_for(key: str) -> str:
    """A BuildingModel level ID for a level key: GROUND -> GF, FLOOR:2 -> L2, BASEMENT:1 -> B1."""
    fixed = {"DATUM": "NGL", "GROUND": "GF", "ROOF": "ROOF", "PENTHOUSE": "PH", "MEZZANINE": "MZ"}
    if key in fixed:
        return fixed[key]
    kind, _, n = key.partition(":")
    if kind == "NAMED":
        return n
    return {"FLOOR": f"L{n}", "BASEMENT": f"B{n}", "MEZZANINE": f"MZ{n}"}.get(kind, key.replace(":", ""))


def level_sort_key(key: str) -> tuple:
    """Bottom-to-top order for keys; MEZZANINE sits between the floors around it, so it sorts after GROUND."""
    kind, _, n = key.partition(":")
    n = int(n) if n.isdigit() else 0
    order = {"BASEMENT": (-1, -n), "DATUM": (-2, 0), "GROUND": (0, 0), "MEZZANINE": (0, 0.5 + n / 10),
             "FLOOR": (1, n), "PENTHOUSE": (2, 0), "ROOF": (3, 0)}
    return order.get(kind, (9, 0))


def level_slug(label: str) -> str:
    """The stable identity slug of an unfamiliar level label: 'Podium level' -> 'PODIUM_LEVEL'."""
    slug = re.sub(r"[^A-Z0-9]+", "_", (label or "").upper()).strip("_")[:40].strip("_")
    if not slug:
        raise ValueError(f"A level label must contain a letter or digit, got {label!r}.")
    return slug


def _alias_key(value: str) -> str:
    """An alias may name a standard level (GROUND, FLOOR:1 ...) or ANY other label, which then keeps its own identity
    (NAMED:PODIUM) instead of being forced onto a floor number."""
    try:
        return check_level_key(value)
    except ValidationError:
        return "NAMED:" + level_slug(value)


class LevelNamer:
    def __init__(self, aliases: Optional[Mapping[str, str]] = None, numeric_offset: int = 0):
        self.aliases = {normalise(k): _alias_key(v) for k, v in (aliases or {}).items()}
        self.numeric_offset = numeric_offset

    def parse_all(self, text: str) -> list:
        """Every distinct level the text names (a title like "GROUND & FIRST FLOOR PLAN" names two)."""
        s = normalise(text)
        if not s:
            return []
        if s in self.aliases:
            return [LevelName(self.aliases[s], text, 0.99, "alias")]
        contained = {self.aliases[a] for a in self.aliases if re.search(rf"\b{re.escape(a)}\b", s)}
        if contained:                        # a configured phrase inside a longer title ("MAIN HALL PLAN") overrides the defaults
            return sorted((LevelName(k, text, 0.95, "alias") for k in contained), key=lambda l: l.key)
        found: dict = {}
        raw = (text or "").upper()

        def add(key, conf, method):
            if key not in found or found[key].confidence < conf:
                found[key] = LevelName(key, text, conf, method)

        if re.search(r"\b(NGL|NATURAL GROUND LEVEL|DATUM|EXISTING GROUND LEVEL)\b", s):
            add("DATUM", 0.9, "keyword")
        for m in re.finditer(r"\b(BASEMENT|BSMT|BMT)\b(?: (\d))?", s):
            add(f"BASEMENT:{m.group(2) or 1}", 0.9, "keyword")
        if re.search(r"\b(MEZZANINE|MEZZ|MEZ)\b", s):
            add("MEZZANINE", 0.9, "keyword")
        if re.search(r"\b(PENTHOUSE|PENT HOUSE)\b", s):
            add("PENTHOUSE", 0.9, "keyword")
        elif re.search(r"\bPH\b", s):
            add("PENTHOUSE", 0.6, "abbreviation")
        if re.search(r"\bROOF\b", s):
            add("ROOF", 0.9, "keyword")
        elif re.search(r"\bRF\b", s):
            add("ROOF", 0.6, "abbreviation")
        if re.search(r"\b(GROUND|GRND|GRD)\b", s):
            add("GROUND", 0.92, "keyword")
        elif re.search(r"\bG ?F(LR|LOOR)?\b|\bG (FLR|FLOOR)\b", s):
            add("GROUND", 0.85, "abbreviation")
        for word, n in _ORDINALS.items():
            if re.search(rf"\b{word}\b", s):
                add(f"FLOOR:{n}", 0.92 if re.search(rf"\b{word} (FLOOR|FLR|LEVEL|STOREY|STORY)\b", s) else 0.75, "ordinal")
        for m in re.finditer(r"\b(\d{1,2})(ST|ND|RD|TH)\b", s):
            add(f"FLOOR:{int(m.group(1))}", 0.9, "ordinal")
        for short, n in (("FF", 1), ("SF", 2), ("TF", 3)):
            if re.search(rf"\b{short}\b", s):
                add(f"FLOOR:{n}", 0.7, "abbreviation")
        for m in re.finditer(r"\b(?:LEVEL|LVL|LEV|L) ?(\d{1,2})\b", s):
            n = int(m.group(1)) + self.numeric_offset
            written = str(int(m.group(1)))
            if re.search(rf"\b(?:LEVEL|LVL|LEV)\s+[-\u2212]\s*0*{written}\b", raw) and n != 0:
                add(f"BASEMENT:{abs(n)}", 0.8, "numeric")            # "LEVEL -1" is below ground; "LEVEL-1" and "L-01" are not
            else:
                add("GROUND" if n == 0 else f"FLOOR:{n}", 0.85, "numeric")
        for m in re.finditer(r"(?<![\d.])(\d{1,2})\s*/\s*F\b", raw):        # 1/F, 2/F
            add(f"FLOOR:{int(m.group(1))}", 0.85, "abbreviation")
        if re.search(r"\bR\s*/\s*F\b", raw):
            add("ROOF", 0.85, "abbreviation")
        for m in re.finditer(r"\b(?:FLOOR|FLR|STOREY|STORY) (\d{1,2})\b", s):
            add(f"FLOOR:{int(m.group(1))}", 0.8, "ordinal")
        if not found:  # one typo in a keyword
            for token in s.split():
                for word, (kind, value) in _FUZZY_WORDS.items():
                    if token != word and len(word) >= 5 and fuzzy_match(token, word):
                        add(f"FLOOR:{value}" if kind == "ordinal" else value, 0.7, "fuzzy")
        return sorted(found.values(), key=lambda l: (-l.confidence, l.key))

    def parse(self, text: str) -> Optional[LevelName]:
        """The one level a text names, or None if it names none or several."""
        levels = self.parse_all(text)
        return levels[0] if len(levels) == 1 else None


@dataclass(frozen=True)
class TitleInfo:
    view_type: Optional[ViewType]
    confidence: float
    levels: tuple = ()
    variant: Optional[str] = None
    section_label: Optional[str] = None
    orientation: Optional[str] = None
    method: str = "keyword"


_ORIENT_WORDS = ("NORTH", "SOUTH", "EAST", "WEST", "FRONT", "REAR", "BACK", "LEFT", "RIGHT", "APPROACH", "SIDE")


def classify_title(text: str, namer: Optional[LevelNamer] = None) -> TitleInfo:
    """What a title announces. view_type is None when the text is not recognisably a view title."""
    namer = namer or LevelNamer()
    s = normalise(re.sub(r"\(.*?\)", " ", text or ""))
    if not s:
        return TitleInfo(None, 0.0)
    levels = tuple(namer.parse_all(text))
    exact, fuzzy = 0, 0

    def word(w):
        nonlocal exact, fuzzy
        r = has_word(s, w)
        exact += r is True
        fuzzy += r is False
        return r is not None

    variant = None
    if word("BLOWUP") or word("BLOWOUT") or re.search(r"\bBLOW (UP|OUT)\b", s) or word("ENLARGED"):
        variant = "blowup"
    elif word("FURNITURE"):
        variant = "furniture"
    elif re.search(r"\bDIMENSION(ED)? DRAWING\b|\bDIMENSIONED\b", s):
        variant = "dimension"
    elif re.search(r"\bSITE\b", s):
        variant = "site"
    view_type = None
    if word("SECTION") or word("SECTIONAL"):
        view_type = ViewType.SECTION
    elif word("ELEVATION") or word("ELEV"):
        view_type = ViewType.ELEVATION
    elif word("PLAN") or word("LAYOUT") or re.search(r"\bGENERAL ARRANGEMENT\b", s):
        view_type = ViewType.DETAIL if variant == "site" else ViewType.FLOOR_PLAN
    elif re.search(r"\bSHEET LIST\b|\bSCHEDULE\b|\bSCHEDULES\b", s):
        view_type = ViewType.SCHEDULE
    elif re.search(r"\b(GENERAL )?NOTES\b", s):
        view_type = ViewType.NOTES
    elif re.search(r"\b(LEGEND|SYMBOLS|KEY)\b", s):
        view_type = ViewType.LEGEND
    elif word("DETAIL") or word("DETAILS") or variant == "blowup" or re.search(r"\b3D\b|\bISOMETRIC\b|\bPERSPECTIVE\b", s):
        view_type = ViewType.DETAIL
    if view_type is None:
        return TitleInfo(None, 0.0, levels)
    label = None
    if view_type == ViewType.SECTION:
        m = re.search(r"\bSECTION ([A-Z0-9]{1,3}) ([A-Z0-9]{1,3})\b", s)
        label = f"{m.group(1)}-{m.group(2)}" if m else None
    elif variant == "blowup":
        m = re.search(r"\bBLOW ?(?:UP|OUT)? ([A-Z])\b", s)
        label = m.group(1) if m else None
    orientation = None
    if view_type == ViewType.ELEVATION:
        parts = [w for w in _ORIENT_WORDS if re.search(rf"\b{w}\b", s)]
        orientation = "_".join(w.lower() for w in parts if w != "SIDE") + ("_side" if "SIDE" in parts else "") or None
        orientation = orientation.strip("_") if orientation else None
    confidence = 0.9 if not fuzzy else 0.72
    if levels and view_type == ViewType.FLOOR_PLAN:
        confidence = min(0.97, confidence + 0.05)
    if view_type in (ViewType.DETAIL, ViewType.FLOOR_PLAN) and variant == "blowup" and not re.search(r"\bPLAN\b", s):
        confidence = min(confidence, 0.6)
    return TitleInfo(view_type, confidence, levels, variant, label, orientation, "fuzzy" if fuzzy else "keyword")


_NUMBER_RE = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)\s*(MM|M)?\s*$", re.I)


def parse_elevation_mm(text: str) -> Optional[float]:
    """An elevation written as a bare number: '3450' (mm), '+3.300' or '3.3m' (metres). None if it is not a number."""
    m = _NUMBER_RE.match(text or "")
    if not m:
        return None
    value, unit = float(m.group(1)), (m.group(2) or "").upper()
    if unit == "M":
        return value * 1000.0
    if unit == "MM":
        return value
    if "." in m.group(1) and abs(value) < 100:
        return value * 1000.0
    return value
