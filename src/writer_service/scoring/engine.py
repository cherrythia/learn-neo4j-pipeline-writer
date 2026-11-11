"""Pure scoring and routing functions: no I/O, fully deterministic."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal

from writer_service.scoring.policy import ScoringPolicy

COMPONENTS = (
    "grounding",
    "back_translation",
    "stability",
    "source_reliability",
    "entity_match",
    "self_confidence",
)
NEW_ENTITY_MATCH = 0.5  # a brand-new entity is neither confirmed nor contradicted
EPSILON = 1e-9  # absorbs float noise at threshold boundaries

Decision = Literal["commit", "review", "reject", "conflict"]


def _check_probability(name: str, value: float) -> None:
    if math.isnan(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be in [0, 1], got {value}")


@dataclass(frozen=True)
class Signals:
    """Verification results for one candidate fact."""

    source: str  # "pdf" | "excel" | "superset" (keys of policy.source_reliability)
    grounding: float  # evidence span really appears in the source
    back_translation: float  # LLM judge: restated fact matches the source
    stability: float  # share of self-consistency runs that produced the fact
    entity_match: float | None  # similarity to an existing node; None = new entity
    self_confidence: float  # the extractor's own confidence

    def __post_init__(self) -> None:
        for name in ("grounding", "back_translation", "stability", "self_confidence"):
            _check_probability(name, getattr(self, name))
        if self.entity_match is not None:
            _check_probability("entity_match", self.entity_match)


@dataclass(frozen=True)
class Component:
    value: float
    weight: float
    contribution: float


@dataclass(frozen=True)
class Score:
    value: float
    breakdown: dict[str, Component] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready derivation, stored with every candidate."""
        return {
            "value": round(self.value, 6),
            "components": {
                name: {
                    "value": round(c.value, 6),
                    "weight": c.weight,
                    "contribution": round(c.contribution, 6),
                }
                for name, c in self.breakdown.items()
            },
        }


def score(signals: Signals, policy: ScoringPolicy) -> Score:
    if signals.source not in policy.source_reliability:
        raise ValueError(f"unknown source '{signals.source}'")
    values = {
        "grounding": signals.grounding,
        "back_translation": signals.back_translation,
        "stability": signals.stability,
        "source_reliability": policy.source_reliability[signals.source],
        "entity_match": (
            NEW_ENTITY_MATCH if signals.entity_match is None else signals.entity_match
        ),
        "self_confidence": signals.self_confidence,
    }
    weights = policy.weights.model_dump()
    breakdown = {
        name: Component(values[name], weights[name], values[name] * weights[name])
        for name in COMPONENTS
    }
    total = sum(c.contribution for c in breakdown.values())
    return Score(min(1.0, max(0.0, total)), breakdown)


@dataclass(frozen=True)
class Routing:
    decision: Decision
    reasons: list[str]


def route(
    value: float,
    policy: ScoringPolicy,
    *,
    gate_failures: list[str] | None = None,
    conflict: str | None = None,
) -> Routing:
    """Hard gates first (reject), then conflicts (always human review), then thresholds."""
    if gate_failures:
        return Routing("reject", list(gate_failures))
    if conflict:
        return Routing("conflict", [conflict])
    c, r = policy.commit_threshold, policy.review_threshold
    if value + EPSILON >= c:
        return Routing("commit", [f"score {value:.4f} >= {c:.2f}: commit"])
    if value + EPSILON >= r:
        return Routing(
            "review", [f"score {value:.4f} is between {r:.2f} and {c:.2f}: needs review"]
        )
    return Routing("reject", [f"score {value:.4f} < {r:.2f}: reject"])
