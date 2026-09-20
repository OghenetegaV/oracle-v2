"""Oracle — Interpretation Advisor Hook (AI assistance, bounded)

Purpose:
    The one place an AI (Claude or anything else) may take part in architectural interpretation, and the
    rules that keep it from becoming the authority. A LayerAdvisor is any object with
    advise(layer_name, sample_entities) -> [Advice]. Its advice is recorded ONLY as a PROPOSED
    EngineeringDecision with source AI_ASSISTANT (a recommendation, never accepted by itself), naming the
    layer, the field ('semantic_class'), the proposed value, the confidence and the reasoning. The engineer
    then accepts or overrides it through the ordinary decision system (project.set_value with responds_to).

Role in Oracle:
    Enforces "AI must never bypass provenance or directly modify the model". Deterministic geometry and
    naming come first; the advisor is consulted only for layers those left below 0.6 confidence. This module
    makes no network call and imports no AI client; a Claude-backed advisor is a separate, optional
    component that implements the small protocol below.

Dependencies:
    oracle.core (decisions, targets).

Consumers:
    oracle.interpretation.pipeline (when InterpretationConfig.advisor is set); tests.

Status:
    Interpretation (Phase 3).

Migration/Notes:
    Advice for titles, level names and alternative interpretations follows the same pattern and is not
    implemented yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from oracle.core import DecisionCategory, DecisionSource, DecisionStatus, EngineeringDecision, OracleProject, Target


@dataclass(frozen=True)
class Advice:
    semantic_class: str
    confidence: float
    reasoning: str


class LayerAdvisor(Protocol):
    def advise(self, layer_name: str, sample: Sequence) -> Sequence[Advice]: ...


def record_layer_advice(project: OracleProject, layer_id: str, layer_name: str, advisor: LayerAdvisor, sample: Sequence) -> list:
    """Ask the advisor and store each piece of advice as a PROPOSED AI recommendation. Returns the decisions."""
    recorded = []
    for advice in advisor.advise(layer_name, sample) or ():
        decision = EngineeringDecision(
            f"AI-{len(project.decisions) + 1:04d}", "AI assistant", DecisionSource.AI_ASSISTANT, Target.architectural(layer_id),
            DecisionCategory.OTHER,
            f"Layer {layer_name!r} may be {advice.semantic_class!r} (confidence {advice.confidence:.2f}): {advice.reasoning}",
            reason=advice.reasoning, status=DecisionStatus.PROPOSED, field="semantic_class", value=advice.semantic_class)
        recorded.append(project.add_decision(decision))
    return recorded
