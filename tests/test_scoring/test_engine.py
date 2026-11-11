import pytest
from hypothesis import given
from hypothesis import strategies as st

from writer_service.scoring.engine import (
    COMPONENTS,
    Signals,
    route,
    score,
)
from writer_service.scoring.policy import default_policy

POLICY = default_policy()


def signals(**kw: float | None) -> Signals:
    base: dict[str, float | None] = dict(
        grounding=1.0, back_translation=1.0, stability=1.0, entity_match=1.0, self_confidence=1.0
    )
    base.update(kw)
    return Signals(source="pdf", **base)  # type: ignore[arg-type]


def test_worked_example_from_docs() -> None:
    s = Signals(
        source="pdf",
        grounding=0.9,
        back_translation=0.8,
        stability=2 / 3,
        entity_match=None,
        self_confidence=0.9,
    )
    result = score(s, POLICY)
    # 0.25*0.9 + 0.20*0.8 + 0.15*0.6667 + 0.20*0.75 + 0.10*0.5 + 0.10*0.9 = 0.775
    assert result.value == pytest.approx(0.775, abs=1e-4)
    assert result.breakdown["entity_match"].value == 0.5  # new entity is neutral
    assert result.breakdown["source_reliability"].value == 0.75
    assert sum(c.contribution for c in result.breakdown.values()) == pytest.approx(result.value)
    assert set(result.breakdown) == set(COMPONENTS)


def test_breakdown_is_json_ready() -> None:
    d = score(signals(), POLICY).as_dict()
    assert d["value"] == pytest.approx(0.95)  # all 1.0 except pdf source 0.75
    assert d["components"]["grounding"] == {"value": 1.0, "weight": 0.25, "contribution": 0.25}


def test_unknown_source_is_an_error() -> None:
    with pytest.raises(ValueError, match="unknown source 'fax'"):
        score(
            Signals(
                source="fax",
                grounding=1,
                back_translation=1,
                stability=1,
                entity_match=1,
                self_confidence=1,
            ),
            POLICY,
        )


@pytest.mark.parametrize("bad", [-0.01, 1.01, float("nan")])
def test_signals_must_be_probabilities(bad: float) -> None:
    with pytest.raises(ValueError):
        signals(grounding=bad)


@pytest.mark.parametrize(
    "value,expected",
    [
        (0.85, "commit"),
        (0.8499, "review"),
        (0.60, "review"),
        (0.5999, "reject"),
        (1.0, "commit"),
        (0.0, "reject"),
    ],
)
def test_routing_boundaries(value: float, expected: str) -> None:
    assert route(value, POLICY).decision == expected


def test_float_noise_at_threshold_still_commits() -> None:
    assert route(0.1 + 0.2 + 0.55, POLICY).decision == "commit"  # 0.8500000000000001 / 0.849999...


def test_gate_failures_reject_regardless_of_score() -> None:
    r = route(0.99, POLICY, gate_failures=["schema: unknown class 'Foo'"])
    assert r.decision == "reject"
    assert r.reasons == ["schema: unknown class 'Foo'"]


def test_conflict_goes_to_review_queue_regardless_of_score() -> None:
    r = route(0.99, POLICY, conflict="FILED_BY already points to another Issuer")
    assert r.decision == "conflict"
    assert "FILED_BY" in r.reasons[0]


def test_gate_failure_beats_conflict() -> None:
    assert route(0.99, POLICY, gate_failures=["x"], conflict="y").decision == "reject"


def test_reasons_explain_threshold_decisions() -> None:
    assert route(0.7, POLICY).reasons == ["score 0.7000 is between 0.60 and 0.85: needs review"]


prob = st.floats(min_value=0, max_value=1, allow_nan=False)


@given(
    prob,
    prob,
    prob,
    st.one_of(st.none(), prob),
    prob,
    st.sampled_from(["pdf", "excel", "superset"]),
)
def test_score_always_in_unit_interval(
    g: float, b: float, s: float, e: float | None, c: float, src: str
) -> None:
    v = score(
        Signals(
            source=src,
            grounding=g,
            back_translation=b,
            stability=s,
            entity_match=e,
            self_confidence=c,
        ),
        POLICY,
    ).value
    assert 0.0 <= v <= 1.0


@given(
    st.sampled_from(
        ["grounding", "back_translation", "stability", "entity_match", "self_confidence"]
    ),
    prob,
    prob,
    prob,
)
def test_score_monotonic_in_each_component(
    component: str, base: float, lo: float, hi: float
) -> None:
    lo, hi = min(lo, hi), max(lo, hi)
    low = score(signals(**{"grounding": base, component: lo}), POLICY).value
    high = score(signals(**{"grounding": base, component: hi}), POLICY).value
    assert high >= low - 1e-12
