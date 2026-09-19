"""Oracle — Core Project-File Migrations

Purpose:
    Upgrades an older project-file dictionary to the current schema before it is loaded, one version
    step at a time. Today: 0.1.0 -> 0.2.0. Unknown or newer versions are refused with a
    SchemaVersionError rather than guessed at.

Role in Oracle:
    Keeps old projects openable as the schema grows. A migration only reshapes data: it adds the empty
    registries a newer schema requires and never fabricates content. A project migrated from 0.1.0
    therefore has NO provenance, value statuses or interpretations, which is the truth, and it is saved
    back as the current schema. The strict-keys policy of the loader is unchanged: unknown fields are
    still rejected after migration.

Dependencies:
    oracle.core.common (SCHEMA_VERSION, SchemaVersionError).

Consumers:
    oracle.core.project (OracleProject.from_dict).

Status:
    Core.

Migration/Notes:
    Add one function per schema bump to MIGRATIONS, and a fixture of a real file of the old version
    under tests/fixtures/. The 0.1.0 fixture was produced by the Phase 1 code (tag v2.0.0-phase1).
"""

from __future__ import annotations

import copy
from typing import Any, Mapping

from .common import SCHEMA_VERSION, SchemaVersionError, ValidationError


def _0_1_0_to_0_2_0(data: dict) -> dict:
    """0.2.0 adds project-level registries for provenance, value statuses and interpretation sets."""
    present = [k for k in ("provenance", "value_status", "interpretations") if k in data]
    if present:  # a 0.1.0 file cannot have these; overwriting them would silently discard data
        raise ValidationError(f"A schema 0.1.0 project has unknown field(s): {present}.")
    data["provenance"] = []
    data["value_status"] = []
    data["interpretations"] = []
    data["schema_version"] = "0.2.0"
    return data


MIGRATIONS = {"0.1.0": _0_1_0_to_0_2_0}


def migrate(data: Mapping[str, Any]) -> dict:
    """Return `data` upgraded to SCHEMA_VERSION (a copy; the argument is not modified)."""
    if not isinstance(data, Mapping):
        raise ValidationError("A project file must contain a JSON object.")
    current = copy.deepcopy(dict(data))
    version = current.get("schema_version")
    while version != SCHEMA_VERSION:
        step = MIGRATIONS.get(version)
        if step is None:
            raise SchemaVersionError(
                f"Project schema_version {version!r} is not supported by this Oracle "
                f"(current is {SCHEMA_VERSION!r}; upgradable from {sorted(MIGRATIONS)}).")
        current = step(current)
        version = current.get("schema_version")
    return current
