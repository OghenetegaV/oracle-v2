"""Oracle — Core Readiness

Purpose:
    ProjectReadiness and Blocker: the answer to "may this project be presented as ready for final
    engineering output?", with the reasons when it may not. A project is NOT ready while any of these
    stands: an open BLOCKING issue; an open interpretation set (unresolved ambiguity); an ASSUMED value
    (a placeholder nobody has confirmed); or no building model at all. INFERRED values do not block but
    are listed as unconfirmed, because an Oracle inference is not an engineer-approved fact.

Role in Oracle:
    The gate that stops the system from presenting guesses as a finished design. Later phases (final
    drawings, BBS, calculation packages) are expected to check `project.readiness().ready` before
    producing final output, and to say why when it is False. The rule is deliberately strict; loosening
    it is a decision for the engineer, recorded as accepting the issue or confirming the value, never a
    silent default.

Dependencies:
    Standard library only. Built by oracle.core.project, which owns the data it looks at.

Consumers:
    oracle.core.project (OracleProject.readiness); tests; future output generators and the GUI.

Status:
    Core (schema 0.2.0).

Migration/Notes:
    Not persisted: it is computed from the project every time. Whether ASSUMED should always block, or
    only for values that reach a given output, is an open architectural question.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class BlockerKind(str, Enum):
    BLOCKING_ISSUE = "blocking_issue"
    OPEN_INTERPRETATION = "open_interpretation"
    ASSUMED_VALUE = "assumed_value"
    NO_BUILDING = "no_building"


@dataclass(frozen=True)
class Blocker:
    kind: BlockerKind
    reference: str          # the issue / interpretation set ID, or "<kind> <id>.<field>" for a value
    message: str


@dataclass
class ProjectReadiness:
    blockers: list = field(default_factory=list)
    unconfirmed: list = field(default_factory=list)  # INFERRED values: not blocking, not engineer-confirmed

    @property
    def ready(self) -> bool:
        return not self.blockers

    def by_kind(self, kind: BlockerKind) -> list:
        return [b for b in self.blockers if b.kind == kind]

    def summary(self) -> str:
        if self.ready:
            return "Ready for final engineering output" + (
                f" ({len(self.unconfirmed)} inferred value(s) not engineer-confirmed)." if self.unconfirmed else ".")
        labels = {BlockerKind.BLOCKING_ISSUE: "blocking issue(s)", BlockerKind.OPEN_INTERPRETATION:
                  "unresolved interpretation(s)", BlockerKind.ASSUMED_VALUE: "assumed value(s)",
                  BlockerKind.NO_BUILDING: "missing building model"}
        counts = {}
        for b in self.blockers:
            counts[b.kind] = counts.get(b.kind, 0) + 1
        return "NOT ready for final engineering output: " + ", ".join(f"{n} {labels[k]}" for k, n in counts.items()) + "."
