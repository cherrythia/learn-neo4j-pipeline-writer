import pytest

from writer_service.evaluation.metrics import agreement, entity_key, fact_keys, score_facts
from writer_service.ontology.core import CORE_CLASSES, CORE_RELATIONS
from writer_service.ontology.spec import OntologySpec
from writer_service.pipeline.candidates import CandidateEntity, CandidateRelation, EntityRef

SPEC = OntologySpec(CORE_CLASSES, CORE_RELATIONS)
GOLDEN = {
    "entities": [
        {
            "class_name": "Issuer",
            "key": "APH",
            "properties": {"name": "Aurora", "board": "mainboard"},
        },
        {"class_name": "Rule", "key": "LR-705", "properties": {}},
    ],
    "relations": [{"relation": "CITES", "source": "F1", "target": "LR-705"}],
}


def issuer(**props: str) -> CandidateEntity:
    return CandidateEntity(class_name="Issuer", properties={"ticker": "APH", **props})


def cites(src: str, tgt: str) -> CandidateRelation:
    return CandidateRelation(
        relation="CITES",
        source=EntityRef(class_name="Filing", key=src),
        target=EntityRef(class_name="Rule", key=tgt),
    )


RULE = CandidateEntity(class_name="Rule", properties={"rule_code": "LR-705"})


def test_perfect_extraction() -> None:
    s = score_facts(
        [issuer(name="Aurora", board="mainboard"), RULE, cites("F1", "LR-705")], GOLDEN, SPEC
    )
    assert (s.precision, s.recall, s.f1, s.attribute_accuracy) == (1.0, 1.0, 1.0, 1.0)


def test_identity_matching_ignores_case_and_whitespace() -> None:
    s = score_facts(
        [
            CandidateEntity(class_name="Issuer", properties={"ticker": " aph "}),
            RULE,
            cites("f1", "lr-705"),
        ],
        GOLDEN,
        SPEC,
    )
    assert s.recall == 1.0 and s.precision == 1.0


def test_hallucination_and_miss() -> None:
    extra = CandidateEntity(class_name="Rule", properties={"rule_code": "LR-999"})
    s = score_facts([issuer(name="Aurora"), extra], GOLDEN, SPEC)
    assert (s.true_positives, s.false_positives, s.false_negatives) == (1, 1, 2)
    assert s.precision == 0.5 and s.recall == pytest.approx(1 / 3)
    assert s.f1 == pytest.approx(2 * 0.5 * (1 / 3) / (0.5 + 1 / 3))


def test_attribute_accuracy_counts_wrong_and_missing_properties() -> None:
    s = score_facts([issuer(name="Aurora Pacific", board="MAINBOARD")], GOLDEN, SPEC)
    assert s.attribute_accuracy == 0.5  # board right (case-insensitive), name wrong


def test_empty_prediction() -> None:
    s = score_facts([], GOLDEN, SPEC)
    assert (s.precision, s.recall, s.f1) == (0.0, 0.0, 0.0)
    assert s.attribute_accuracy is None  # nothing matched: not "1.00" (review round 1)


def test_entities_without_identity_key_are_ignored() -> None:
    assert (
        fact_keys([CandidateEntity(class_name="Issuer", properties={"name": "x"})], SPEC) == set()
    )


def test_agreement_is_mean_pairwise_jaccard() -> None:
    a, b = entity_key("Rule", "A"), entity_key("Rule", "B")
    assert agreement([{a, b}, {a, b}]) == 1.0
    assert agreement([{a}, {a, b}]) == 0.5
    assert agreement([{a}, {a, b}, {a, b}]) == pytest.approx((0.5 + 0.5 + 1.0) / 3)
    assert agreement([{a}]) == 1.0


def test_relation_from_the_wrong_class_is_not_a_match() -> None:
    wrong = CandidateRelation(
        relation="CITES",
        source=EntityRef(class_name="Issuer", key="F1"),  # golden source is a Filing
        target=EntityRef(class_name="Rule", key="LR-705"),
    )
    s = score_facts([RULE, wrong], GOLDEN, SPEC)
    assert (s.true_positives, s.false_positives, s.false_negatives) == (1, 1, 2)


def test_golden_can_name_endpoint_classes_explicitly() -> None:
    golden = {
        "entities": [],
        "relations": [
            {
                "relation": "CITES",
                "source": "F1",
                "source_class": "Filing",
                "target": "LR-705",
                "target_class": "Rule",
            }
        ],
    }
    assert score_facts([cites("F1", "LR-705")], golden, SPEC).f1 == 1.0
