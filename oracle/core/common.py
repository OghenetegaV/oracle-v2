"""Oracle — Core Shared Primitives

Purpose:
    Errors (ValidationError, SchemaVersionError), SCHEMA_VERSION, ID rules, the Target reference
    (project / level / element / node / grid line / architectural interpretation object) and the small validators and strict key-checker used for
    deserialisation.

Role in Oracle:
    Lowest layer of the core: it defines what 'valid' means for IDs, numbers, enums, timestamps
    and JSON shape, so every other core module validates the same way.

Dependencies:
    Standard library only.

Consumers:
    Every other module in oracle.core.

Status:
    Core.

Migration/Notes:
    Remains. SCHEMA_VERSION is the project-file schema version (0.2.0), separate from the application
    version; change it only with a migration in oracle.core.migrations. 0.2.0 added the NODE and GRID
    target scopes and the confidence, field-path and JSON-value validators; 0.3.0 added the
    ARCHITECTURAL target scope.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Mapping, Optional

SCHEMA_VERSION = "0.4.0"

# Two points/nodes closer than this on the same level are treated as the same point.
POSITION_TOL_MM = 1.0

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")


class ValidationError(ValueError):
    """Structurally or geometrically invalid engineering data."""


class SchemaVersionError(ValidationError):
    """A project file was written with a schema this version of Oracle cannot read."""


class TargetScope(str, Enum):
    PROJECT = "project"
    LEVEL = "level"
    ELEMENT = "element"
    NODE = "node"
    GRID = "grid"
    ARCHITECTURAL = "architectural"  # a view, layer classification, observation, frame or the drawing itself


@dataclass(frozen=True)
class Target:
    """What a decision, issue, provenance record or value status is about: the whole project, or one
    level, element, node or grid line."""

    scope: TargetScope = TargetScope.PROJECT
    id: Optional[str] = None

    def __post_init__(self):
        if self.scope == TargetScope.PROJECT:
            if self.id is not None:
                raise ValidationError("A project-wide target must not carry an id.")
        else:
            check_id(self.id, f"{self.scope.value} target id")

    @classmethod
    def project(cls) -> "Target":
        return cls(TargetScope.PROJECT, None)

    @classmethod
    def level(cls, level_id: str) -> "Target":
        return cls(TargetScope.LEVEL, level_id)

    @classmethod
    def node(cls, node_id: str) -> "Target":
        return cls(TargetScope.NODE, node_id)

    @classmethod
    def architectural(cls, object_id: str) -> "Target":
        return cls(TargetScope.ARCHITECTURAL, object_id)

    @classmethod
    def grid(cls, label: str) -> "Target":
        return cls(TargetScope.GRID, label)

    @classmethod
    def element(cls, element_id: str) -> "Target":
        return cls(TargetScope.ELEMENT, element_id)

    def to_dict(self) -> dict:
        return {"scope": self.scope.value, "id": self.id}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Target":
        check_keys(data, required={"scope"}, optional={"id"}, where="target")
        return cls(parse_enum(TargetScope, data["scope"], "target scope"), data.get("id"))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_timestamp(value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{what} must be an ISO 8601 UTC timestamp string, got {value!r}.")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise ValidationError(f"{what} must look like 2026-01-31T09:30:00Z, got {value!r}.") from None
    return value


def check_id(value: Any, what: str = "id") -> str:
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise ValidationError(
            f"Invalid {what} {value!r}: use 1-64 characters, starting with a letter or digit, "
            "then letters, digits, '_', '-' or '.'."
        )
    return value


def check_text(value: Any, what: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{what} must be text, got {value!r}.")
    if not allow_empty and not value.strip():
        raise ValidationError(f"{what} must not be empty.")
    return value


def check_optional_text(value: Any, what: str) -> Optional[str]:
    return None if value is None else check_text(value, what)


def check_number(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValidationError(f"{what} must be a finite number, got {value!r}.")
    return value


def check_positive(value: Any, what: str) -> float:
    check_number(value, what)
    if value <= 0:
        raise ValidationError(f"{what} must be greater than zero, got {value!r}.")
    return value


def check_non_negative(value: Any, what: str) -> float:
    check_number(value, what)
    if value < 0:
        raise ValidationError(f"{what} must not be negative, got {value!r}.")
    return value


def check_optional_positive(value: Any, what: str) -> Optional[float]:
    return None if value is None else check_positive(value, what)


def check_optional_non_negative(value: Any, what: str) -> Optional[float]:
    return None if value is None else check_non_negative(value, what)


def parse_enum(enum_cls, value: Any, what: str):
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(value)
    except ValueError:
        allowed = ", ".join(m.value for m in enum_cls)
        raise ValidationError(f"Unknown {what} {value!r}; expected one of: {allowed}.") from None


def check_confidence(value: Any, what: str = "confidence") -> float:
    """A degree of belief between 0 and 1 inclusive. Not a probability model: callers decide what it means."""
    check_number(value, what)
    if not 0.0 <= value <= 1.0:
        raise ValidationError(f"{what} must be between 0 and 1, got {value!r}.")
    return value


_FIELD_RE = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z0-9_]+)*$")


def check_field_path(value: Any, what: str = "field") -> str:
    """A dotted path such as 'section' or 'section.width_mm'. Whether it names a real property is
    checked by the project, which knows the object."""
    if not isinstance(value, str) or not _FIELD_RE.match(value):
        raise ValidationError(f"Invalid {what} {value!r}: use lower-case names joined by dots, e.g. 'section.width_mm'.")
    return value


def check_json_value(value: Any, what: str = "value") -> Any:
    """Anything that survives a JSON round trip unchanged (no tuples, sets, NaN, custom objects)."""
    try:
        again = json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError):
        raise ValidationError(f"{what} must be JSON-serialisable (numbers, text, lists, objects), got {value!r}.") from None
    if again != value:
        raise ValidationError(f"{what} does not survive a JSON round trip unchanged: {value!r}.")
    return value


def check_unique_ids(ids: Iterable[str], what: str) -> None:
    seen = set()
    for i in ids:
        if i in seen:
            raise ValidationError(f"Duplicate {what} {i!r}.")
        seen.add(i)


def check_keys(data: Any, *, required: set, optional: set = frozenset(), where: str) -> None:
    """Reject non-objects, missing keys and unknown keys -- a same-version file with
    unknown keys would otherwise silently lose data on the next save."""
    if not isinstance(data, Mapping):
        raise ValidationError(f"{where} must be a JSON object, got {type(data).__name__}.")
    missing = required - set(data)
    if missing:
        raise ValidationError(f"{where} is missing required field(s): {sorted(missing)}.")
    unknown = set(data) - required - optional
    if unknown:
        raise ValidationError(f"{where} has unknown field(s): {sorted(unknown)}.")
