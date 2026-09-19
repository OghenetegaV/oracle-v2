"""Oracle — Import Adapters: Package

Purpose:
    Groups the adapters that translate an external or legacy representation of a building into
    the canonical oracle.core BuildingModel / OracleProject. Translation only: no structural
    design, no Claude calls, no STAAD, no drawing generation.

Role in Oracle:
    The boundary between "how a source describes a building" and "what Oracle knows about it".
    Each adapter depends on oracle.core; oracle.core depends on no adapter.

Dependencies:
    oracle.core.

Consumers:
    tests; the wizard and future importers (not connected yet).

Status:
    Adapter (Phase 2).

Migration:
    Remains. A future architectural-DWG interpreter is another adapter here and must satisfy the
    boundary in docs/PHASE_2_ADAPTER.md.
"""

from .legacy_ga import adapt_legacy_ga
from .result import AdaptationResult, Basis, SourceRef

__all__ = ["adapt_legacy_ga", "AdaptationResult", "Basis", "SourceRef"]
