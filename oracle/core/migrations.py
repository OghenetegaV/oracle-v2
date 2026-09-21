"""Oracle — Core Project-File Migrations

Purpose:
    Upgrades an older project-file dictionary to the current schema before it is loaded, one version
    step at a time. Today: 0.1.0 -> 0.2.0 -> 0.3.0 -> 0.4.0. Unknown or newer versions are refused with a
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
    under tests/fixtures/. The 0.1.0 fixture was produced by the Phase 1 code (tag v2.0.0-phase1) and
    the 0.2.0 fixture by the Phase 2 code and the 0.3.0 fixture by the Phase 3 code, each before the next
    schema existed.
"""

from __future__ import annotations

import copy
import re
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


def _0_2_0_to_0_3_0(data: dict) -> dict:
    """0.3.0 adds the optional architectural interpretation section (absent in every older project)."""
    if "architecture" in data:  # a 0.2.0 file cannot have this; overwriting it would silently discard data
        raise ValidationError("A schema 0.2.0 project has unknown field(s): ['architecture'].")
    data["architecture"] = None
    data["schema_version"] = "0.3.0"
    return data


_OBSERVATION_KIND_RENAMES = {"existing_column": "column_symbol", "beam_shown": "beam_symbol"}
_HINT_RENAMES = {"existing_column": "column_candidate", "existing_beam": "beam_candidate"}
_SOURCE_ID = re.compile(r"^DWG-(\d+)$")


def _rename_source_ids(node: Any) -> Any:
    """Whole-string ids only: the drawing's id prefix DWG- became SRC-; free text is never rewritten."""
    if isinstance(node, str):
        m = _SOURCE_ID.match(node)
        return f"SRC-{m.group(1)}" if m else node
    if isinstance(node, list):
        return [_rename_source_ids(x) for x in node]
    if isinstance(node, dict):
        return {k: _rename_source_ids(v) for k, v in node.items()}
    return node


def _0_3_0_to_0_4_0(data: dict) -> dict:
    """0.4.0: the architectural interpretation becomes a list (one entry per drawing source), the drawing source is
    format-neutral (DXF-specific unit_code / format_version / layouts move, verbatim, into source_metadata; the id prefix
    is SRC-; revision and interpretation identity are added), observations that implied a structural meaning are renamed
    to what the drawing shows (column_symbol, beam_symbol) and their hints to proposals (column_candidate,
    beam_candidate), evidence links appear (none in an older project), and interpretation alternatives, levels and issues
    gain optional fields that are simply absent. Nothing is invented: declared_unit stays absent because turning a format's
    unit code into a unit is the reader's job, and the raw code is kept in source_metadata."""
    for key in ("architectures", "evidence_links"):
        if key in data:  # a 0.3.0 file cannot have these; overwriting them would silently discard data
            raise ValidationError(f"A schema 0.3.0 project has unknown field(s): [{key!r}].")
    if "architecture" not in data:
        raise ValidationError("A schema 0.3.0 project must have an 'architecture' field.")
    arch = data.pop("architecture")
    if arch is not None:
        if not isinstance(arch, dict) or not isinstance(arch.get("drawing"), dict):
            raise ValidationError("The architecture section of a schema 0.3.0 project is malformed.")
        old = arch["drawing"]
        drawing = {k: v for k, v in old.items() if k not in ("format_version", "unit_code", "layouts")}
        meta = {k: old[k] for k in ("format_version", "unit_code", "layouts") if old.get(k) not in (None, [], ())}
        drawing["source_metadata"] = meta
        arch["drawing"] = drawing
        for o in arch.get("observations") or []:
            if isinstance(o, dict):
                o["kind"] = _OBSERVATION_KIND_RENAMES.get(o.get("kind"), o.get("kind"))
                if o.get("hint") in _HINT_RENAMES:
                    o["hint"] = _HINT_RENAMES[o["hint"]]
    data["architectures"] = [arch] if arch is not None else []
    data["evidence_links"] = []
    data = _rename_source_ids(data)
    data["schema_version"] = "0.4.0"
    return data


def _0_4_0_to_0_5_0(data: dict) -> dict:
    """0.5.0 adds the `clarifications` registry (the engineer's free-form input, kept verbatim). Decisions gain an optional
    `reason_code` and interpretation alternatives an optional `origin`; both are simply absent in an older project."""
    if "clarifications" in data:  # a 0.4.0 file cannot have this; overwriting it would silently discard data
        raise ValidationError("A schema 0.4.0 project has unknown field(s): ['clarifications'].")
    data["clarifications"] = []
    data["schema_version"] = "0.5.0"
    return data


MIGRATIONS = {"0.1.0": _0_1_0_to_0_2_0, "0.2.0": _0_2_0_to_0_3_0, "0.3.0": _0_3_0_to_0_4_0, "0.4.0": _0_4_0_to_0_5_0}


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
