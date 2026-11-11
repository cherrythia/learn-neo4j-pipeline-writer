import pytest
from hypothesis import given
from hypothesis import strategies as st

from writer_service.scoring.engine import (
    COMPONENTS,
    Signals,
    route,
    score,
)
from writer_service.scoring.policy import ScoringPolicy, Weights, default_policy

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


@pytest.mark.parametrize(
    "value,expected",
    [
        (0.85 - 1e-12, "commit"),  # float noise below the threshold: epsilon absorbs it
        (0.60 - 1e-12, "review"),
        (0.85 - 1e-6, "review"),  # a real difference: epsilon must not swallow it
        (0.60 - 1e-6, "reject"),
    ],
)
def test_epsilon_absorbs_float_noise_only(value: float, expected: str) -> None:
    assert route(value, POLICY).decision == expected


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
    assert route(0.7, POLICY).reasons == ["score 0.7000 is between 0.6 and 0.85: needs review"]


prob = st.floats(min_value=0, max_value=1, allow_nan=False)


@st.composite
def policies(draw: st.DrawFn) -> ScoringPolicy:
    """Random valid policies: positive weights normalised to sum to 1, random reliabilities."""
    raw = [draw(st.floats(min_value=0.01, max_value=1)) for _ in COMPONENTS]
    total = sum(raw)
    weights = Weights(**{n: w / total for n, w in zip(COMPONENTS, raw, strict=True)})
    reliability = {s: draw(prob) for s in ("pdf", "excel", "superset")}
    return default_policy().model_copy(
        update={"weights": weights, "source_reliability": reliability}
    )


SIGNAL_FIELDS = ["grounding", "back_translation", "stability", "entity_match", "self_confidence"]


@given(
    policies(),
    prob,
    prob,
    prob,
    st.one_of(st.none(), prob),
    prob,
    st.sampled_from(["pdf", "excel", "superset"]),
)
def test_score_in_unit_interval_and_contributions_add_up(
    policy: ScoringPolicy, g: float, b: float, s: float, e: float | None, c: float, src: str
) -> None:
    result = score(
        Signals(
            source=src,
            grounding=g,
            back_translation=b,
            stability=s,
            entity_match=e,
            self_confidence=c,
        ),
        policy,
    )
    assert 0.0 <= result.value <= 1.0 + 1e-12
    assert sum(x.contribution for x in result.breakdown.values()) == pytest.approx(result.value)


@given(policies(), st.sampled_from(SIGNAL_FIELDS), prob, prob)
def test_score_monotonic_in_each_signal(
    policy: ScoringPolicy, component: str, lo: float, hi: float
) -> None:
    lo, hi = min(lo, hi), max(lo, hi)
    low = score(signals(**{component: lo}), policy).value
    high = score(signals(**{component: hi}), policy).value
    assert high >= low - 1e-12


@given(policies(), prob, prob)
def test_score_monotonic_in_source_reliability(policy: ScoringPolicy, lo: float, hi: float) -> None:
    lo, hi = min(lo, hi), max(lo, hi)

    def with_pdf(r: float) -> ScoringPolicy:
        return policy.model_copy(
            update={"source_reliability": {**policy.source_reliability, "pdf": r}}
        )

    assert score(signals(), with_pdf(hi)).value >= score(signals(), with_pdf(lo)).value - 1e-12


def test_reasons_show_exact_thresholds() -> None:
    policy = POLICY.model_validate({**POLICY.model_dump(), "commit_threshold": 0.875})
    assert route(0.877, policy).reasons == ["score 0.8770 >= 0.875: commit"]
    assert route(0.87, policy).reasons == ["score 0.8700 is between 0.6 and 0.875: needs review"]
