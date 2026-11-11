import pytest
from pydantic import ValidationError

from writer_service.scoring.policy import ScoringPolicy, Weights, default_policy


def test_default_policy_matches_design() -> None:
    p = default_policy()
    assert p.weights.model_dump() == {
        "grounding": 0.25,
        "back_translation": 0.20,
        "stability": 0.15,
        "source_reliability": 0.20,
        "entity_match": 0.10,
        "self_confidence": 0.10,
    }
    assert (p.commit_threshold, p.review_threshold) == (0.85, 0.60)
    assert p.source_reliability == {"superset": 0.95, "excel": 0.85, "pdf": 0.75}
    assert p.self_consistency_runs == 3


def test_weights_must_sum_to_one() -> None:
    with pytest.raises(ValidationError, match="sum to 1"):
        Weights(
            grounding=0.5,
            back_translation=0.5,
            stability=0.5,
            source_reliability=0,
            entity_match=0,
            self_confidence=0,
        )


def test_weights_tolerate_float_rounding() -> None:
    Weights(
        grounding=0.1,
        back_translation=0.2,
        stability=0.3,
        source_reliability=0.1,
        entity_match=0.2,
        self_confidence=0.1,
    )


def test_negative_weight_rejected() -> None:
    with pytest.raises(ValidationError):
        Weights(
            grounding=1.1,
            back_translation=-0.1,
            stability=0,
            source_reliability=0,
            entity_match=0,
            self_confidence=0,
        )


@pytest.mark.parametrize("commit,review", [(0.6, 0.6), (0.5, 0.7), (1.1, 0.5), (0.8, -0.1)])
def test_thresholds_must_be_ordered_and_in_range(commit: float, review: float) -> None:
    with pytest.raises(ValidationError):
        default_policy().model_copy(update={}).model_validate(
            {
                **default_policy().model_dump(),
                "commit_threshold": commit,
                "review_threshold": review,
            }
        )


def test_source_reliability_values_in_range() -> None:
    data = default_policy().model_dump()
    data["source_reliability"] = {"pdf": 1.5}
    with pytest.raises(ValidationError):
        ScoringPolicy.model_validate(data)


def test_unknown_weight_key_rejected() -> None:
    data = default_policy().model_dump()
    data["weights"]["vibes"] = 0.0
    with pytest.raises(ValidationError):
        ScoringPolicy.model_validate(data)
