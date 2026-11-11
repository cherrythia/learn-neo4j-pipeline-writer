from typing import Any

import pytest
from pydantic import BaseModel

from writer_service.llm.cache import InMemoryCache
from writer_service.llm.fake import FakeLLM
from writer_service.llm.prompts import PromptTemplate
from writer_service.llm.structured import StructuredLLM
from writer_service.pipeline.candidates import (
    CandidateEntity,
    CandidateRelation,
    EntityRef,
    Evidence,
)
from writer_service.pipeline.verification import (
    BACK_TRANSLATION_PROMPT,
    BackTranslator,
    DictSourceText,
    describe,
    grounding_score,
    self_consistency,
)

PAGE1 = (
    "Aurora Pacific Holdings (Demo) issued this annual report for the financial year ended "
    "31 December 2025 on 28 April 2026, in accordance with Listing Rule LR-705."
)


def ev(quote: str, locator: str = "page 1") -> Evidence:
    return Evidence(source_type="pdf", source_ref="a.pdf", locator=locator, quote=quote)


def issuer(ticker: str = "APH", **kw: Any) -> CandidateEntity:
    return CandidateEntity(
        class_name="Issuer", properties={"ticker": ticker, "name": "Aurora"}, **kw
    )


FILED = CandidateRelation(
    relation="FILED_BY",
    source=EntityRef(class_name="Filing", key="AR-2025-APH"),
    target=EntityRef(class_name="Issuer", key="APH"),
)


# ---------------------------------------------------------------- grounding


def test_exact_quote_after_normalisation_scores_one() -> None:
    assert grounding_score("issued  this annual REPORT", PAGE1) == 1.0


def test_near_quote_scores_high_but_below_one() -> None:
    # typo + abbreviated date, as OCR or a slightly paraphrasing extractor produces
    s = grounding_score("issued this anual report for the financial year ended 31 Dec 2025", PAGE1)
    assert 0.8 <= s < 1.0


def test_elided_quote_is_not_grounded() -> None:
    # words cut from the middle ("...for the financial year ended 31 December 2025...") hide
    # the context the date belongs to: quotes must be contiguous source text
    assert grounding_score("issued the annual report on 28 April 2026", PAGE1) <= 0.1


def test_contiguous_partial_quote_scores_in_between() -> None:
    s = grounding_score("the annual reports for financial years ended", PAGE1)
    assert 0.3 <= s < 0.9


def test_hallucinated_quote_scores_about_zero() -> None:
    assert grounding_score("The board approved a dividend of 5 cents per share", PAGE1) < 0.1


@pytest.mark.parametrize(
    "quote",
    [
        "issued this annual report for the financial year ended 31 December 2024",  # year
        "in accordance with Listing Rule LR-707",  # rule number
        "Banyan Logistics (Demo) issued this annual report",  # other issuer
        "Aurora Pacific Holdings (Demo) did not issue this annual report",  # negation
        "Aurora Pacific Holdings (Demo) didn't issue this annual report",  # contraction
        "for the financial year ended 31 December 2026",  # 2026 is elsewhere on the page
        "issued this annual report on 28 April 2025",  # 2025 is elsewhere on the page
        "in accordance with Listing Rule LR-70",  # prefix of the real identifier
    ],
)
def test_altered_critical_detail_is_capped(quote: str) -> None:
    assert grounding_score(quote, PAGE1) <= 0.1


def test_abbreviated_names_are_not_treated_as_altered() -> None:
    from writer_service.pipeline.verification import ALTERED_CAP, _missing_critical

    assert _missing_critical("financial year ended 31 Dec 2025", PAGE1) == []
    assert grounding_score("financial year ended 31 Dec 2025", PAGE1) > 2 * ALTERED_CAP


def test_missing_source_text_scores_zero() -> None:
    assert grounding_score("anything", None) == 0.0
    assert grounding_score("anything", "") == 0.0


def test_candidate_grounding_takes_best_evidence() -> None:
    texts = DictSourceText({("a.pdf", "page 1"): PAGE1})
    cand = issuer(
        evidence=[ev("made up words entirely"), ev("in accordance with Listing Rule LR-705")]
    )
    assert texts.grounding(cand) == 1.0
    assert texts.grounding(issuer(evidence=[])) == 0.0
    assert texts.grounding(issuer(evidence=[ev("LR-705", locator="page 9")])) == 0.0


# ---------------------------------------------------------------- describe


def test_describe_entity_and_relation() -> None:
    assert describe(issuer()) == "Issuer with ticker 'APH' and name 'Aurora'"
    assert describe(FILED) == "Filing 'AR-2025-APH' FILED_BY Issuer 'APH'"


# ---------------------------------------------------------------- back-translation


def judge_llm(score: float, agrees: bool = True) -> tuple[StructuredLLM, FakeLLM]:
    fake = FakeLLM(
        {
            BACK_TRANSLATION_PROMPT.name: [
                {"agrees": agrees, "score": score, "explanation": "checked"}
            ]
        }
    )
    return StructuredLLM(fake, InMemoryCache()), fake


def test_back_translation_uses_judge_score_and_sends_fact_and_quotes() -> None:
    llm, fake = judge_llm(0.9)
    bt = BackTranslator(llm, ontology_version=1)
    assert bt.score(FILED.model_copy(update={"evidence": [ev("issued this annual report")]})) == 0.9
    (req,) = fake.calls
    assert "Filing 'AR-2025-APH' FILED_BY Issuer 'APH'" in req.user
    assert "issued this annual report" in req.user


def test_disagreement_caps_the_score() -> None:
    llm, _ = judge_llm(0.95, agrees=False)
    assert BackTranslator(llm, ontology_version=1).score(issuer(evidence=[ev("x")])) <= 0.3


def test_no_evidence_means_nothing_to_judge() -> None:
    llm, fake = judge_llm(1.0)
    assert BackTranslator(llm, ontology_version=1).score(issuer()) == 0.0
    assert fake.calls == []


def test_judge_answers_are_cached_per_fact() -> None:
    llm, fake = judge_llm(0.8)
    bt = BackTranslator(llm, ontology_version=1)
    cand = issuer(evidence=[ev("Aurora")])
    bt.score(cand)
    bt.score(cand)
    assert len(fake.calls) == 1


# ---------------------------------------------------------------- self-consistency


def runs(*batches: list[Any]) -> Any:
    def extract(sample: int) -> list[Any]:
        return batches[sample]

    return extract


def test_majority_vote_with_three_runs() -> None:
    a1 = issuer(evidence=[ev("Aurora", "page 1")], confidence=0.9)
    a2 = issuer(evidence=[ev("Aurora Pacific", "page 2")], confidence=0.7)  # same fact
    b = issuer("BYL")
    voted = self_consistency(runs([a1, FILED], [a2, b], [FILED]), n=3)
    by_key = {v.candidate.properties.get("ticker", "rel"): v for v in voted}
    assert set(by_key) == {"APH", "rel"}  # BYL appeared once: dropped
    aph = by_key["APH"]
    assert aph.votes == 2 and aph.stability == pytest.approx(2 / 3)
    assert {e.locator for e in aph.candidate.evidence} == {"page 1", "page 2"}  # merged
    assert aph.candidate.confidence == pytest.approx(0.8)  # mean
    assert by_key["rel"].votes == 2


def test_duplicates_within_one_run_count_once() -> None:
    voted = self_consistency(runs([FILED, FILED], [], []), n=3)
    assert voted == []


def test_single_run_keeps_everything_with_full_stability() -> None:
    voted = self_consistency(runs([issuer(), FILED]), n=1)
    assert [v.stability for v in voted] == [1.0, 1.0]


def test_order_is_deterministic_by_first_appearance() -> None:
    voted = self_consistency(runs([FILED, issuer()], [issuer(), FILED]), n=2)
    assert [v.candidate.kind for v in voted] == ["relation", "entity"]


def test_n_must_be_positive() -> None:
    with pytest.raises(ValueError):
        self_consistency(runs(), n=0)


# ---------------------------------------------------------------- self-consistency via the LLM


class Extraction(BaseModel):
    entities: list[CandidateEntity]


EXTRACT = PromptTemplate(name="extract", version=1, system="Extract.", user="Text: {text}")


def entity_json(ticker: str) -> dict[str, Any]:
    return {
        "kind": "entity",
        "class_name": "Issuer",
        "properties": {"ticker": ticker, "name": f"{ticker} Co"},
        "confidence": 0.9,
    }


def llm_extractor(llm: StructuredLLM) -> Any:
    def extract(sample: int) -> list[CandidateEntity]:
        result = llm.ask(EXTRACT, {"text": PAGE1}, Extraction, ontology_version=1, sample=sample)
        return list(result.value.entities)

    return extract


def test_self_consistency_through_structured_llm_3_2_1_agreement() -> None:
    # A in 3/3 runs, B in 2/3, C in 1/3
    fake = FakeLLM(
        {
            "extract": [
                {"entities": [entity_json("AAA"), entity_json("BBB")]},
                {"entities": [entity_json("AAA"), entity_json("BBB"), entity_json("CCC")]},
                {"entities": [entity_json("AAA")]},
            ]
        }
    )
    cache = InMemoryCache()
    voted = self_consistency(llm_extractor(StructuredLLM(fake, cache)), n=3)
    assert len(fake.calls) == 3  # one real call per sample, not one cached answer reused
    stability = {v.candidate.properties["ticker"]: v.stability for v in voted}
    assert stability == {"AAA": 1.0, "BBB": pytest.approx(2 / 3)}  # CCC (1/3) dropped

    # Replaying from the cache makes no new calls and reproduces the same votes.
    replay = self_consistency(llm_extractor(StructuredLLM(fake, cache)), n=3)
    assert len(fake.calls) == 3
    assert [(v.candidate.properties["ticker"], v.votes) for v in replay] == [
        (v.candidate.properties["ticker"], v.votes) for v in voted
    ]


def test_negation_dropped_from_the_source_is_caught() -> None:
    source = "Banyan Logistics (Demo) did not issue a sustainability report for 2025."
    assert (
        grounding_score("Banyan Logistics (Demo) did issue a sustainability report", source) <= 0.1
    )
    assert (
        grounding_score("Banyan Logistics (Demo) did not issue a sustainability report", source)
        == 1.0
    )


def test_amount_prefix_is_not_verbatim() -> None:
    source = "The fine was $1,200,000 under the rule."
    assert grounding_score("The fine was $1,200", source) <= 0.1
    assert grounding_score("The fine was $1,200,000", source) == 1.0


@pytest.mark.parametrize(
    "quote,source",
    [
        (
            "approved the final dividend",
            "The board approved the final dividend and not the special dividend.",
        ),
        (
            "The board approved the final dividend",
            "Objections: none. The board approved the final dividend.",
        ),
        (
            "in accordance with Listing Rule 705",
            "Filed in accordance with Listing Rule 705. No exceptions were noted.",
        ),
        ("issued on 28 April 2026", "The report was issued on 28 April 2026, not earlier."),
    ],
)
def test_negation_just_outside_a_verbatim_quote_is_context(quote: str, source: str) -> None:
    assert grounding_score(quote, source) >= 0.9


@pytest.mark.parametrize(
    "quote,source",
    [
        ("Aurora did issue the report", "Aurora didn\u2019t issue the report."),
        ("Aurora didn\u2019t issue the report", "Aurora did issue the report."),
    ],
)
def test_curly_apostrophe_contractions_are_negations(quote: str, source: str) -> None:
    assert grounding_score(quote, source) <= 0.1


def test_quote_and_source_fold_case_the_same_way() -> None:
    assert grounding_score("Straße", "Haupt Straße 5") == 1.0
