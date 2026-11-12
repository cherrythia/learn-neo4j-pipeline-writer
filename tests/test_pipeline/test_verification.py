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
    "Zorblax Systems Supply (Demo) issued this invoice for the support period ended "
    "31 December 2025 on 28 January 2026, for laptop HW-25-0101 assigned to Finance (FIN)."
)


def ev(quote: str, locator: str = "page 1") -> Evidence:
    return Evidence(
        source_type="pdf",
        source_ref="zorblax_invoice_INV-2025-0418.pdf",
        locator=locator,
        quote=quote,
    )


def department(dept_code: str = "FIN", **kw: Any) -> CandidateEntity:
    return CandidateEntity(
        class_name="Department", properties={"dept_code": dept_code, "name": "Finance"}, **kw
    )


ASSIGNED = CandidateRelation(
    relation="ASSIGNED_TO",
    source=EntityRef(class_name="HardwareAsset", key="HW-25-0101"),
    target=EntityRef(class_name="Department", key="FIN"),
)


# ---------------------------------------------------------------- grounding


def test_exact_quote_after_normalisation_scores_one() -> None:
    assert grounding_score("issued  this INVOICE for the support", PAGE1) == 1.0


def test_near_quote_scores_high_but_below_one() -> None:
    # typo + abbreviated date, as OCR or a slightly paraphrasing extractor produces
    s = grounding_score("issued this invoce for the support period ended 31 Dec 2025", PAGE1)
    assert 0.3 <= s <= 0.5  # near-quote: partial support, never enough to auto-commit


def test_elided_quote_is_not_grounded() -> None:
    # words cut from the middle ("...for the support period ended 31 December 2025...") hide
    # the context the date belongs to: quotes must be contiguous source text
    assert grounding_score("issued the invoice on 28 January 2026", PAGE1) <= 0.1


def test_contiguous_partial_quote_scores_in_between() -> None:
    s = grounding_score("the invoices for support periods ended", PAGE1)
    assert 0.15 <= s <= 0.5


def test_hallucinated_quote_scores_about_zero() -> None:
    assert grounding_score("The vendor granted a discount of 5 percent per unit", PAGE1) < 0.1


@pytest.mark.parametrize(
    "quote",
    [
        "issued this invoice for the support period ended 31 December 2024",  # year
        "for laptop HW-25-0102 assigned to Finance",  # asset tag
        "Harbor Sync Supply (Demo) issued this invoice",  # other vendor
        "Zorblax Systems Supply (Demo) did not issue this invoice",  # negation
        "Zorblax Systems Supply (Demo) didn't issue this invoice",  # contraction
        "for the support period ended 31 December 2026",  # 2026 is elsewhere on the page
        "issued this invoice on 28 January 2025",  # 2025 is elsewhere on the page
        "for laptop HW-25-010 assigned to Finance",  # prefix of the real asset tag
        "for laptop HW-25-0101 assigned to Facilities",  # other department
    ],
)
def test_altered_critical_detail_is_capped(quote: str) -> None:
    assert grounding_score(quote, PAGE1) <= 0.1


def test_abbreviated_names_are_not_treated_as_altered() -> None:
    from writer_service.pipeline.verification import ALTERED_CAP, _missing_critical

    assert _missing_critical("support period ended 31 Dec 2025", PAGE1) == []
    assert grounding_score("support period ended 31 Dec 2025", PAGE1) > ALTERED_CAP


def test_missing_source_text_scores_zero() -> None:
    assert grounding_score("anything", None) == 0.0
    assert grounding_score("anything", "") == 0.0


def test_candidate_grounding_takes_best_evidence() -> None:
    texts = DictSourceText({("zorblax_invoice_INV-2025-0418.pdf", "page 1"): PAGE1})
    cand = department(
        evidence=[ev("made up words entirely"), ev("laptop HW-25-0101 assigned to Finance (FIN)")]
    )
    assert texts.grounding(cand) == 1.0
    assert texts.grounding(department(evidence=[])) == 0.0
    assert texts.grounding(department(evidence=[ev("HW-25-0101", locator="page 9")])) == 0.0


# ---------------------------------------------------------------- describe


def test_describe_entity_and_relation() -> None:
    assert describe(department()) == "Department with dept_code 'FIN' and name 'Finance'"
    assert describe(ASSIGNED) == "HardwareAsset 'HW-25-0101' ASSIGNED_TO Department 'FIN'"


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
    quote = "laptop HW-25-0101 is assigned to Finance (FIN)"
    assert bt.score(ASSIGNED.model_copy(update={"evidence": [ev(quote)]})) == 0.9
    (req,) = fake.calls
    assert "HardwareAsset 'HW-25-0101' ASSIGNED_TO Department 'FIN'" in req.user
    assert quote in req.user


def test_disagreement_caps_the_score() -> None:
    llm, _ = judge_llm(0.95, agrees=False)
    assert BackTranslator(llm, ontology_version=1).score(department(evidence=[ev("x")])) <= 0.3


def test_no_evidence_means_nothing_to_judge() -> None:
    llm, fake = judge_llm(1.0)
    assert BackTranslator(llm, ontology_version=1).score(department()) == 0.0
    assert fake.calls == []


def test_judge_answers_are_cached_per_fact() -> None:
    llm, fake = judge_llm(0.8)
    bt = BackTranslator(llm, ontology_version=1)
    cand = department(evidence=[ev("Finance")])
    bt.score(cand)
    bt.score(cand)
    assert len(fake.calls) == 1


# ---------------------------------------------------------------- self-consistency


def runs(*batches: list[Any]) -> Any:
    def extract(sample: int) -> list[Any]:
        return batches[sample]

    return extract


def test_majority_vote_with_three_runs() -> None:
    a1 = department(evidence=[ev("Finance", "page 1")], confidence=0.9)
    a2 = department(evidence=[ev("Finance (FIN)", "page 2")], confidence=0.7)  # same fact
    b = department("HRM")
    voted = self_consistency(runs([a1, ASSIGNED], [a2, b], [ASSIGNED]), n=3)
    by_key = {v.candidate.properties.get("dept_code", "rel"): v for v in voted}
    assert set(by_key) == {"FIN", "rel"}  # HRM appeared once: dropped
    fin = by_key["FIN"]
    assert fin.votes == 2 and fin.stability == pytest.approx(2 / 3)
    assert {e.locator for e in fin.candidate.evidence} == {"page 1", "page 2"}  # merged
    assert fin.candidate.confidence == pytest.approx(0.8)  # mean
    assert by_key["rel"].votes == 2


def test_duplicates_within_one_run_count_once() -> None:
    voted = self_consistency(runs([ASSIGNED, ASSIGNED], [], []), n=3)
    assert voted == []


def test_single_run_keeps_everything_with_full_stability() -> None:
    voted = self_consistency(runs([department(), ASSIGNED]), n=1)
    assert [v.stability for v in voted] == [1.0, 1.0]


def test_order_is_deterministic_by_first_appearance() -> None:
    voted = self_consistency(runs([ASSIGNED, department()], [department(), ASSIGNED]), n=2)
    assert [v.candidate.kind for v in voted] == ["relation", "entity"]


def test_n_must_be_positive() -> None:
    with pytest.raises(ValueError):
        self_consistency(runs(), n=0)


# ---------------------------------------------------------------- self-consistency via the LLM


class Extraction(BaseModel):
    entities: list[CandidateEntity]


EXTRACT = PromptTemplate(name="extract", version=1, system="Extract.", user="Text: {text}")


def entity_json(dept_code: str) -> dict[str, Any]:
    return {
        "kind": "entity",
        "class_name": "Department",
        "properties": {"dept_code": dept_code, "name": f"{dept_code} Department"},
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
    stability = {v.candidate.properties["dept_code"]: v.stability for v in voted}
    assert stability == {"AAA": 1.0, "BBB": pytest.approx(2 / 3)}  # CCC (1/3) dropped

    # Replaying from the cache makes no new calls and reproduces the same votes.
    replay = self_consistency(llm_extractor(StructuredLLM(fake, cache)), n=3)
    assert len(fake.calls) == 3
    assert [(v.candidate.properties["dept_code"], v.votes) for v in replay] == [
        (v.candidate.properties["dept_code"], v.votes) for v in voted
    ]


def test_negation_dropped_from_the_source_is_caught() -> None:
    source = "Harbor Sync Supply (Demo) did not issue a credit note for 2025."
    assert grounding_score("Harbor Sync Supply (Demo) did issue a credit note", source) <= 0.1
    assert grounding_score("Harbor Sync Supply (Demo) did not issue a credit note", source) == 1.0


def test_amount_prefix_is_not_verbatim() -> None:
    source = "The invoice total was SGD 44,482.90 including GST."
    assert grounding_score("The invoice total was SGD 44,482", source) <= 0.1
    assert grounding_score("The invoice total was SGD 44", source) <= 0.1
    assert grounding_score("The invoice total was SGD 44,482.90", source) == 1.0


@pytest.mark.parametrize(
    "quote,source",
    [
        (
            "approved the replacement laptop",
            "Finance approved the replacement laptop and not the spare monitor.",
        ),
        (
            "Finance approved the replacement laptop",
            "Objections: none. Finance approved the replacement laptop.",
        ),
        (
            "in accordance with purchase order 705",
            "Billed in accordance with purchase order 705. No exceptions were noted.",
        ),
        ("issued on 28 January 2026", "The invoice was issued on 28 January 2026, not earlier."),
    ],
)
def test_negation_just_outside_a_verbatim_quote_is_context(quote: str, source: str) -> None:
    assert grounding_score(quote, source) >= 0.9


@pytest.mark.parametrize(
    "quote,source",
    [
        ("Zorblax did issue the invoice", "Zorblax didn\u2019t issue the invoice."),
        ("Zorblax didn\u2019t issue the invoice", "Zorblax did issue the invoice."),
    ],
)
def test_curly_apostrophe_contractions_are_negations(quote: str, source: str) -> None:
    assert grounding_score(quote, source) <= 0.1


def test_quote_and_source_fold_case_the_same_way() -> None:
    assert grounding_score("Straße", "Haupt Straße 5") == 1.0


@pytest.mark.parametrize(
    "quote,source",
    [
        ("Class A licences were renewed", "Class B licences were renewed in 2025."),
        ("as listed in Schedule I", "as listed in Schedule II of the contract."),
        ("signed by Mr Tan", "signed by Mr Tang on behalf of the vendor."),
        ("under clause 705(1)(a)", "under clause 705(1)(b) of the contract."),
        ("the laptop is standard", "the laptop is non-standard under HW-25-0101."),
        ("spend increased in 2025", "spend decreased in 2025."),
        ("ordered a replacement laptop", "ordered a loaner laptop."),
        ("issued its interim invoice", "issued its final invoice."),
        ("ordered three laptops", "ordered five laptops."),
        ("the purchase was approved", "the purchase was rejected."),
    ],
)
def test_one_word_changes_never_reach_commit_level_grounding(quote: str, source: str) -> None:
    assert grounding_score(quote, source) <= 0.5


@pytest.mark.parametrize(
    "quote,source",
    [
        ("match purchase order 705", "The invoice did not match purchase order 705."),
        ("issue the invoice", "Zorblax didn\u2019t issue the invoice."),
    ],
)
def test_verbatim_quote_dropping_a_preceding_negation_is_capped(quote: str, source: str) -> None:
    assert grounding_score(quote, source) <= 0.1


@pytest.mark.parametrize(
    "quote,source",
    [
        ("for 2024-2025", "Support for 2024\u20132025 was billed."),  # en dash
        ("for HW-25-0101", "for HW\u201125\u20110101 the laptop"),  # non-breaking hyphens
        ("the field engineer", "the \ufb01eld engineer"),  # 'fi' ligature in source
        ('the "replacement laptop"', "the \u201creplacement laptop\u201d was billed"),  # quotes
        ("software licences", "soft\u00adware licences"),  # soft hyphen
    ],
)
def test_pdf_typography_does_not_break_verbatim_quotes(quote: str, source: str) -> None:
    assert grounding_score(quote, source) == 1.0


def test_year_is_not_verbatim_inside_a_fiscal_range() -> None:
    assert grounding_score("for 2025", "the licences for 2025/26 were") <= 0.5


@pytest.mark.parametrize("quote", ["\u00ad", "-", '"', "  ", "..."])
def test_empty_or_punctuation_only_quotes_score_zero(quote: str) -> None:
    assert grounding_score(quote, "Zorblax issued the invoice .") == 0.0


def test_any_unnegated_occurrence_counts() -> None:
    a = "Zorblax did not issue the invoice in March. It did issue the invoice in April."
    b = "It did issue the invoice in April. Zorblax did not issue the invoice in March."
    assert grounding_score("issue the invoice", a) == 1.0
    assert grounding_score("issue the invoice", b) == 1.0


@pytest.mark.parametrize(
    "source",
    [
        "The invoice did not, in all respects, match purchase order 705.",
        "The invoice did not at any time match purchase order 705.",
    ],
)
def test_negation_earlier_in_the_sentence_sends_to_review(source: str) -> None:
    assert grounding_score("match purchase order 705", source) == 0.5
