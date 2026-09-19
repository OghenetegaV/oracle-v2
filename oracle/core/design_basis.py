"""Oracle — Core Design Basis

Purpose:
    The project's engineering assumptions: design codes, material grades, cover, per-level
    loading, wind and seismic assumptions, exposure, fire resistance and free-text assumptions.
    Only the design code and the concrete and reinforcement grades are required; anything else is
    None until the engineer states it, and missing_items() lists what is open.

Role in Oracle:
    The engineer-controlled statement of what the design is done to. Holds data and validation
    only; no code equations and no defaulted 'typical' values.

Dependencies:
    oracle.core.common, oracle.core.elements (ElementKind).

Consumers:
    oracle.core.project.

Status:
    Core.

Migration:
    Remains. Legacy sources of these values (company_standards.json, the wizard's occupancy and
    grade fields, constants in ga_dxf_parser) map onto it in a future adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from .common import (
    ValidationError, check_id, check_keys, check_optional_non_negative, check_optional_positive,
    check_optional_text, check_positive, check_text,
)
from .elements import ElementKind

_MAX_COVER_MM = 150.0


@dataclass
class LevelLoading:
    """Gravity loading assumptions. level_id None means the default for every level without its own entry."""

    level_id: Optional[str] = None
    occupancy: Optional[str] = None
    imposed_kn_m2: Optional[float] = None
    finishes_kn_m2: Optional[float] = None  # superimposed dead load (finishes, screed, ceilings, services)
    partitions_kn_m2: Optional[float] = None
    notes: Optional[str] = None

    def __post_init__(self):
        if self.level_id is not None:
            check_id(self.level_id, "loading level_id")
        check_optional_text(self.occupancy, "loading occupancy")
        check_optional_non_negative(self.imposed_kn_m2, "imposed_kn_m2")
        check_optional_non_negative(self.finishes_kn_m2, "finishes_kn_m2")
        check_optional_non_negative(self.partitions_kn_m2, "partitions_kn_m2")
        check_optional_text(self.notes, "loading notes")

    def to_dict(self) -> dict:
        return {"level_id": self.level_id, "occupancy": self.occupancy, "imposed_kn_m2": self.imposed_kn_m2,
                "finishes_kn_m2": self.finishes_kn_m2, "partitions_kn_m2": self.partitions_kn_m2,
                "notes": self.notes}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "LevelLoading":
        check_keys(data, required=set(),
                   optional={"level_id", "occupancy", "imposed_kn_m2", "finishes_kn_m2", "partitions_kn_m2", "notes"},
                   where="level loading")
        return cls(**data)


@dataclass
class WindBasis:
    applicable: bool
    basic_wind_speed_m_s: Optional[float] = None
    terrain_category: Optional[str] = None
    notes: Optional[str] = None

    def __post_init__(self):
        if not isinstance(self.applicable, bool):
            raise ValidationError("Wind 'applicable' must be true or false.")
        check_optional_positive(self.basic_wind_speed_m_s, "basic_wind_speed_m_s")
        check_optional_text(self.terrain_category, "terrain_category")
        check_optional_text(self.notes, "wind notes")

    def to_dict(self) -> dict:
        return {"applicable": self.applicable, "basic_wind_speed_m_s": self.basic_wind_speed_m_s,
                "terrain_category": self.terrain_category, "notes": self.notes}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WindBasis":
        check_keys(data, required={"applicable"}, optional={"basic_wind_speed_m_s", "terrain_category", "notes"},
                   where="wind basis")
        return cls(**data)


@dataclass
class SeismicBasis:
    applicable: bool
    peak_ground_acceleration_g: Optional[float] = None
    ground_type: Optional[str] = None
    notes: Optional[str] = None

    def __post_init__(self):
        if not isinstance(self.applicable, bool):
            raise ValidationError("Seismic 'applicable' must be true or false.")
        check_optional_positive(self.peak_ground_acceleration_g, "peak_ground_acceleration_g")
        check_optional_text(self.ground_type, "ground_type")
        check_optional_text(self.notes, "seismic notes")

    def to_dict(self) -> dict:
        return {"applicable": self.applicable, "peak_ground_acceleration_g": self.peak_ground_acceleration_g,
                "ground_type": self.ground_type, "notes": self.notes}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SeismicBasis":
        check_keys(data, required={"applicable"}, optional={"peak_ground_acceleration_g", "ground_type", "notes"},
                   where="seismic basis")
        return cls(**data)


@dataclass
class DesignBasis:
    design_code: str                       # e.g. "BS 8110-1:1997" -- free text, the engineer's choice
    concrete_grade: str                    # e.g. "C25/30"
    reinforcement_grade: str               # e.g. "Y (high-yield, BS 4449)"
    concrete_fcu_n_mm2: Optional[float] = None
    reinforcement_fy_n_mm2: Optional[float] = None
    steel_grade: Optional[str] = None      # structural steel, e.g. "S275"
    steel_fy_n_mm2: Optional[float] = None
    steel_design_code: Optional[str] = None
    loading_code: Optional[str] = None
    concrete_unit_weight_kn_m3: Optional[float] = None
    cover_mm: dict = field(default_factory=dict)  # keyed by element kind ("column", "beam", ...)
    loadings: list = field(default_factory=list)  # LevelLoading entries
    wind: Optional[WindBasis] = None
    seismic: Optional[SeismicBasis] = None
    exposure_class: Optional[str] = None
    durability_notes: Optional[str] = None
    fire_resistance_min: Optional[int] = None
    default_storey_height_mm: Optional[float] = None
    assumptions: list = field(default_factory=list)  # other engineer-defined assumptions, as text

    def __post_init__(self):
        self.validate()

    def validate(self) -> None:
        check_text(self.design_code, "design_code")
        check_text(self.concrete_grade, "concrete_grade")
        check_text(self.reinforcement_grade, "reinforcement_grade")
        check_optional_positive(self.concrete_fcu_n_mm2, "concrete_fcu_n_mm2")
        check_optional_positive(self.reinforcement_fy_n_mm2, "reinforcement_fy_n_mm2")
        check_optional_text(self.steel_grade, "steel_grade")
        check_optional_positive(self.steel_fy_n_mm2, "steel_fy_n_mm2")
        check_optional_text(self.steel_design_code, "steel_design_code")
        check_optional_text(self.loading_code, "loading_code")
        check_optional_positive(self.concrete_unit_weight_kn_m3, "concrete_unit_weight_kn_m3")
        check_optional_text(self.exposure_class, "exposure_class")
        check_optional_text(self.durability_notes, "durability_notes")
        check_optional_positive(self.default_storey_height_mm, "default_storey_height_mm")
        if self.fire_resistance_min is not None:
            if isinstance(self.fire_resistance_min, bool) or not isinstance(self.fire_resistance_min, int) \
                    or self.fire_resistance_min <= 0:
                raise ValidationError(f"fire_resistance_min must be a positive whole number of minutes, "
                                      f"got {self.fire_resistance_min!r}.")
        kinds = {k.value for k in ElementKind}
        for kind, cover in self.cover_mm.items():
            if kind not in kinds:
                raise ValidationError(f"Cover given for unknown element kind {kind!r}; expected one of {sorted(kinds)}.")
            check_positive(cover, f"cover_mm[{kind}]")
            if cover > _MAX_COVER_MM:
                raise ValidationError(f"cover_mm[{kind}] = {cover} mm is implausibly large (limit {_MAX_COVER_MM:g} mm).")
        seen = set()
        for loading in self.loadings:
            if not isinstance(loading, LevelLoading):
                raise ValidationError(f"Loadings must be LevelLoading entries, got {loading!r}.")
            if loading.level_id in seen:
                where = "the default" if loading.level_id is None else f"level {loading.level_id!r}"
                raise ValidationError(f"More than one loading entry for {where}.")
            seen.add(loading.level_id)
        if self.wind is not None and not isinstance(self.wind, WindBasis):
            raise ValidationError("wind must be a WindBasis.")
        if self.seismic is not None and not isinstance(self.seismic, SeismicBasis):
            raise ValidationError("seismic must be a SeismicBasis.")
        for a in self.assumptions:
            check_text(a, "assumption")

    def loading_for_level(self, level_id: str) -> Optional[LevelLoading]:
        """The level's own loading entry, else the default entry, else None."""
        by_level = {ld.level_id: ld for ld in self.loadings}
        return by_level.get(level_id, by_level.get(None))

    def missing_items(self) -> list:
        """Design-basis items the engineer has not defined yet, in plain words. Not an error --
        a project can exist before its basis is complete -- but analysis/design should not
        silently proceed while these are open."""
        missing = []
        if self.concrete_fcu_n_mm2 is None:
            missing.append("concrete characteristic strength (fcu)")
        if self.reinforcement_fy_n_mm2 is None:
            missing.append("reinforcement yield strength (fy)")
        if self.concrete_unit_weight_kn_m3 is None:
            missing.append("concrete unit weight")
        if not self.cover_mm:
            missing.append("nominal cover")
        if not self.loadings:
            missing.append("imposed and superimposed dead loading")
        if self.wind is None:
            missing.append("wind assumption (state 'not applicable' if so)")
        if self.seismic is None:
            missing.append("seismic assumption (state 'not applicable' if so)")
        if self.exposure_class is None:
            missing.append("exposure class")
        if self.fire_resistance_min is None:
            missing.append("fire resistance requirement")
        return missing

    def to_dict(self) -> dict:
        return {
            "design_code": self.design_code,
            "concrete_grade": self.concrete_grade,
            "concrete_fcu_n_mm2": self.concrete_fcu_n_mm2,
            "reinforcement_grade": self.reinforcement_grade,
            "reinforcement_fy_n_mm2": self.reinforcement_fy_n_mm2,
            "steel_grade": self.steel_grade,
            "steel_fy_n_mm2": self.steel_fy_n_mm2,
            "steel_design_code": self.steel_design_code,
            "loading_code": self.loading_code,
            "concrete_unit_weight_kn_m3": self.concrete_unit_weight_kn_m3,
            "cover_mm": {k: self.cover_mm[k] for k in sorted(self.cover_mm)},
            "loadings": [ld.to_dict() for ld in self.loadings],
            "wind": self.wind.to_dict() if self.wind else None,
            "seismic": self.seismic.to_dict() if self.seismic else None,
            "exposure_class": self.exposure_class,
            "durability_notes": self.durability_notes,
            "fire_resistance_min": self.fire_resistance_min,
            "default_storey_height_mm": self.default_storey_height_mm,
            "assumptions": list(self.assumptions),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DesignBasis":
        optional = {"concrete_fcu_n_mm2", "reinforcement_fy_n_mm2", "steel_grade", "steel_fy_n_mm2",
                    "steel_design_code", "loading_code", "concrete_unit_weight_kn_m3", "cover_mm", "loadings",
                    "wind", "seismic", "exposure_class", "durability_notes", "fire_resistance_min",
                    "default_storey_height_mm", "assumptions"}
        check_keys(data, required={"design_code", "concrete_grade", "reinforcement_grade"}, optional=optional,
                   where="design basis")
        kwargs = {k: data[k] for k in optional if k in data}
        kwargs["loadings"] = [LevelLoading.from_dict(d) for d in data.get("loadings", [])]
        kwargs["wind"] = WindBasis.from_dict(data["wind"]) if data.get("wind") is not None else None
        kwargs["seismic"] = SeismicBasis.from_dict(data["seismic"]) if data.get("seismic") is not None else None
        kwargs["cover_mm"] = dict(data.get("cover_mm", {}))
        kwargs["assumptions"] = list(data.get("assumptions", []))
        return cls(data["design_code"], data["concrete_grade"], data["reinforcement_grade"], **kwargs)
