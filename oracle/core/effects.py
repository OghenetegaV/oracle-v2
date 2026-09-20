"""Oracle — Structured Resolution Effects

Purpose:
    The machine-readable meaning of accepting one alternative of an InterpretationSet. An Effect names WHAT
    changes when an engineer accepts an interpretation, as data, not prose: set a field of a domain object,
    accept a storey height between two levels, align a plan into the building frame, merge or split views, or
    acknowledge an answer that changes no state. Each kind has a fixed, strictly validated parameter set; an
    unknown kind, a missing parameter or an extra one is rejected at construction and on load.

Role in Oracle:
    Makes "resolving an interpretation" real. Before this, accepting an alternative only flipped a status and the
    model, the linked issue and the readiness gate all stayed as they were. Effects are carried on the
    Interpretation, applied by OracleProject.accept_interpretation() under the engineer's decision (through the
    ordinary set_value / review machinery, never around it), and recorded in Interpretation.applied so the
    result can be checked against the payload on load. An Effect never changes anything by itself.

Dependencies:
    oracle.core.common, oracle.core.architecture (level key validation).

Consumers:
    oracle.core.interpretations (stores effects), oracle.core.resolution (applies them), oracle.interpretation
    (attaches them to the alternatives it proposes), tests.

Status:
    Core (added in schema 0.4.0).

Migration/Notes:
    New in schema 0.4.0. Alternatives written by older versions carry no effects (an empty tuple) and behave as
    before: accepting them records the choice and changes nothing else. New kinds are added here, with a
    schema version, never as free-form JSON.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from .architecture import check_bbox, check_kind_id, check_level_key
from .common import Target, ValidationError, check_keys, check_optional_text, check_text, parse_enum


class EffectKind(str, Enum):
    SET_VALUE = "set_value"            # {target, field, value}
    ACCEPT_HEIGHT = "accept_height"    # {from_level, to_level, height_mm}
    ALIGN_VIEW = "align_view"          # {view_id, translation}
    MERGE_VIEWS = "merge_views"        # {view_ids, new_id}
    SPLIT_VIEW = "split_view"          # {view_id, parts}
    ACKNOWLEDGE = "acknowledge"        # {note?}: the answer is recorded; no state changes


_REQUIRED = {
    EffectKind.SET_VALUE: {"target", "field", "value"},
    EffectKind.ACCEPT_HEIGHT: {"from_level", "to_level", "height_mm"},
    EffectKind.ALIGN_VIEW: {"view_id", "translation"},
    EffectKind.MERGE_VIEWS: {"view_ids", "new_id"},
    EffectKind.SPLIT_VIEW: {"view_id", "parts"},
    EffectKind.ACKNOWLEDGE: set(),
}
_OPTIONAL = {EffectKind.ACKNOWLEDGE: {"note"}}


def _number(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValidationError(f"{what} must be a finite number, got {value!r}.")
    return float(value)


@dataclass(frozen=True)
class Effect:
    kind: EffectKind
    params: Mapping

    def __post_init__(self):
        object.__setattr__(self, "kind", parse_enum(EffectKind, self.kind, "effect kind"))
        if not isinstance(self.params, Mapping):
            raise ValidationError("Effect parameters must be an object.")
        params = dict(self.params)
        check_keys(params, required=_REQUIRED[self.kind], optional=_OPTIONAL.get(self.kind, set()),
                   where=f"{self.kind.value} effect")
        k = self.kind
        if k == EffectKind.SET_VALUE:
            target = Target.from_dict(params["target"])
            if target.scope.value == "project":
                raise ValidationError("A set_value effect needs a specific target.")
            check_text(params["field"], "effect field")
            if "." in params["field"]:
                raise ValidationError("A set_value effect changes a top-level field.")
            params["target"] = target.to_dict()
        elif k == EffectKind.ACCEPT_HEIGHT:
            check_level_key(params["from_level"], "from_level")
            check_level_key(params["to_level"], "to_level")
            if params["from_level"] == params["to_level"]:
                raise ValidationError("An accepted height needs two different levels.")
            if _number(params["height_mm"], "height_mm") <= 0:
                raise ValidationError("An accepted height must be positive.")
        elif k == EffectKind.ALIGN_VIEW:
            check_kind_id("view", params["view_id"])
            t = params["translation"]
            if not isinstance(t, (list, tuple)) or len(t) != 2:
                raise ValidationError("translation must be [x, y].")
            params["translation"] = [_number(t[0], "translation"), _number(t[1], "translation")]
        elif k == EffectKind.MERGE_VIEWS:
            ids = params["view_ids"]
            if not isinstance(ids, (list, tuple)) or len(ids) < 2:
                raise ValidationError("merge_views needs at least two view ids.")
            params["view_ids"] = [check_kind_id("view", v) for v in ids]
            check_kind_id("view", params["new_id"])
        elif k == EffectKind.SPLIT_VIEW:
            check_kind_id("view", params["view_id"])
            parts = params["parts"]
            if not isinstance(parts, Mapping) or len(parts) < 2:
                raise ValidationError("split_view needs at least two parts.")
            clean = {}
            for new_id, part in parts.items():
                check_kind_id("view", new_id)
                check_keys(part, required={"bbox", "entity_ids"}, where="split part")
                clean[new_id] = {"bbox": list(check_bbox(part["bbox"], "part bbox")),
                                 "entity_ids": [check_text(e, "entity id") for e in part["entity_ids"]]}
            params["parts"] = clean
        elif k == EffectKind.ACKNOWLEDGE:
            check_optional_text(params.get("note"), "acknowledge note")
        object.__setattr__(self, "params", params)

    # ---- convenient, validated constructors ----
    @classmethod
    def set_value(cls, target: Target, field: str, value: Any) -> "Effect":
        return cls(EffectKind.SET_VALUE, {"target": target.to_dict(), "field": field, "value": value})

    @classmethod
    def accept_height(cls, from_level: str, to_level: str, height_mm: float) -> "Effect":
        return cls(EffectKind.ACCEPT_HEIGHT, {"from_level": from_level, "to_level": to_level, "height_mm": height_mm})

    @classmethod
    def align_view(cls, view_id: str, translation) -> "Effect":
        return cls(EffectKind.ALIGN_VIEW, {"view_id": view_id, "translation": list(translation)})

    @classmethod
    def merge_views(cls, view_ids, new_id: str) -> "Effect":
        return cls(EffectKind.MERGE_VIEWS, {"view_ids": list(view_ids), "new_id": new_id})

    @classmethod
    def split_view(cls, view_id: str, parts: Mapping) -> "Effect":
        return cls(EffectKind.SPLIT_VIEW, {"view_id": view_id, "parts": dict(parts)})

    @classmethod
    def acknowledge(cls, note: str = None) -> "Effect":
        return cls(EffectKind.ACKNOWLEDGE, {"note": note} if note else {})

    @property
    def target(self) -> Target:
        """The domain object a set_value effect changes."""
        return Target.from_dict(self.params["target"])

    def to_dict(self) -> dict:
        return {"kind": self.kind.value, "params": dict(self.params)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Effect":
        check_keys(data, required={"kind", "params"}, where="effect")
        return cls(data["kind"], data["params"])
