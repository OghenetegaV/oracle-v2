"""Oracle — Adapter Result

Purpose:
    AdaptationResult: what every import adapter returns. It wraps the OracleProject the adapter built
    (which now holds all provenance, value statuses, decisions and issues itself), a count of items the
    adapter dropped or failed to build, and read-only convenience views over the project's provenance.

Role in Oracle:
    The common contract between an import adapter (today the legacy GA parser adapter; later an
    architectural-DWG interpreter) and the rest of Oracle. It carries no data of its own: provenance and
    value status live in oracle.core (schema 0.2.0) so they are saved with the project.

    `complete` means the adapter built everything it was given and dropped nothing. It does NOT mean the
    model is confirmed or fit for output: that is project.readiness() / result.readiness(), which also
    weighs open blocking issues, unresolved interpretations and assumed values.

    SourceRef and Basis are a compatibility view kept for the Phase 2 API and its tests: SourceRef is
    assembled on demand from a ProvenanceRecord and the object's ValueStatus, and Basis is the older
    four-way name for the value statuses (EXTRACTED=SOURCE, INFERRED, ASSUMED, ENGINEER_INPUT=
    ENGINEER_DEFINED). They are not a second model; new code should read project.provenance and
    project.value_statuses directly.

Dependencies:
    oracle.core (OracleProject, Target, ValueStatus, ProjectReadiness).

Consumers:
    oracle.adapters.legacy_ga; tests; future adapters and the wizard.

Status:
    Adapter (Phase 2, revised for schema 0.2.0).

Migration/Notes:
    The SourceRef/Basis views can be deleted once callers read the core registries.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from oracle.core import BuildingModel, ProjectReadiness, OracleProject, Target, ValueStatus


class Basis(str, Enum):
    EXTRACTED = "extracted"
    INFERRED = "inferred"
    ASSUMED = "assumed"
    ENGINEER_INPUT = "engineer_input"


_BASIS_OF_STATUS = {
    ValueStatus.SOURCE: Basis.EXTRACTED,
    ValueStatus.INFERRED: Basis.INFERRED,
    ValueStatus.DERIVED: Basis.INFERRED,
    ValueStatus.CALCULATED: Basis.INFERRED,
    ValueStatus.ASSUMED: Basis.ASSUMED,
    ValueStatus.ENGINEER_DEFINED: Basis.ENGINEER_INPUT,
    ValueStatus.ENGINEER_OVERRIDE: Basis.ENGINEER_INPUT,
}


@dataclass(frozen=True)
class SourceRef:
    """Read-only view of an object's whole-object ProvenanceRecord and the status of its geometry."""

    system: str
    source_type: str
    source_id: str
    basis: Basis
    source_layer: Optional[str] = None
    note: Optional[str] = None

    def to_dict(self) -> dict:
        return {"system": self.system, "source_type": self.source_type, "source_id": self.source_id,
                "source_layer": self.source_layer, "basis": self.basis.value, "note": self.note}


def _target_for(kind: str, object_id: str) -> Target:
    if kind == "level":
        return Target.level(object_id)
    if kind == "node":
        return Target.node(object_id)
    return Target.element(object_id)


@dataclass
class AdaptationResult:
    project: OracleProject
    failures: int = 0  # items dropped or that could not be built (not structural observations)

    @property
    def building(self) -> Optional[BuildingModel]:
        return self.project.building

    @property
    def issues(self) -> list:
        return self.project.issues

    @property
    def complete(self) -> bool:
        """A building was built and nothing was dropped. Not the same as confirmed or ready for output."""
        return self.building is not None and self.failures == 0

    def readiness(self) -> ProjectReadiness:
        return self.project.readiness()

    def _view(self, target: Target) -> Optional[SourceRef]:
        record = next((r for r in self.project.provenance_for(target) if r.field is None), None)
        if record is None:
            return None
        status = (self.project.value_status_of(target, "geometry") or self.project.value_status_of(target, "id"))
        basis = _BASIS_OF_STATUS[status.status] if status else Basis.INFERRED
        return SourceRef(record.producer, record.source.entity_type or record.method, record.source.source_id or "",
                         basis, record.source.layer, record.note)

    def provenance_for(self, kind: str, object_id: str) -> Optional[SourceRef]:
        return self._view(_target_for(kind, object_id))

    def provenance_records(self) -> list:
        """Deterministic list of {"key", ...SourceRef fields}, one per object, sorted by key."""
        rows = {}
        for r in self.project.provenance:
            if r.field is None:
                kind = "level" if r.target.scope.value == "level" else "node" if r.target.scope.value == "node" else None
                if kind is None:
                    kind = self.building.get_element(r.target.id).kind.value
                view = self._view(r.target)
                rows[f"{kind}:{r.target.id}"] = {"key": f"{kind}:{r.target.id}", **view.to_dict()}
        return [rows[k] for k in sorted(rows)]
