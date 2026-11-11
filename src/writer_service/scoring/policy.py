"""Scoring policy: weights, thresholds and per-source reliability. Editable from the admin page."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Every ingestion source must have a reliability, or scoring would fail for it.
SOURCE_TYPES = frozenset({"pdf", "excel", "superset"})


class Weights(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    grounding: float = Field(ge=0, le=1)
    back_translation: float = Field(ge=0, le=1)
    stability: float = Field(ge=0, le=1)
    source_reliability: float = Field(ge=0, le=1)
    entity_match: float = Field(ge=0, le=1)
    self_confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _sum_to_one(self) -> Weights:
        total = sum(self.model_dump().values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"weights must sum to 1 (got {total:.4f})")
        return self


class ScoringPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    weights: Weights
    commit_threshold: float = Field(ge=0, le=1)
    review_threshold: float = Field(ge=0, le=1)
    source_reliability: dict[str, float]
    self_consistency_runs: int = Field(ge=1, le=10)

    @field_validator("source_reliability")
    @classmethod
    def _reliability_in_range(cls, value: dict[str, float]) -> dict[str, float]:
        missing = SOURCE_TYPES - set(value)
        if missing:
            raise ValueError(f"source reliability missing for: {', '.join(sorted(missing))}")
        for source, r in value.items():
            if not 0 <= r <= 1:
                raise ValueError(f"source reliability for {source!r} must be in [0, 1]")
        return value

    @model_validator(mode="after")
    def _ordered_thresholds(self) -> ScoringPolicy:
        if not self.review_threshold < self.commit_threshold:
            raise ValueError("review_threshold must be lower than commit_threshold")
        return self


def default_policy() -> ScoringPolicy:
    """The starting heuristic from the design; calibrate with the golden set (issue #16)."""
    return ScoringPolicy(
        weights=Weights(
            grounding=0.25,
            back_translation=0.20,
            stability=0.15,
            source_reliability=0.20,
            entity_match=0.10,
            self_confidence=0.10,
        ),
        commit_threshold=0.85,
        review_threshold=0.60,
        source_reliability={"superset": 0.95, "excel": 0.85, "pdf": 0.75},
        self_consistency_runs=3,
    )
