import pytest

from writer_service.evaluation.metrics import agreement, entity_key, fact_keys, score_facts
from writer_service.ontology.core import CORE_CLASSES, CORE_RELATIONS
from writer_service.ontology.spec import OntologySpec
from writer_service.pipeline.candidates import CandidateEntity, CandidateRelation, EntityRef

SPEC = OntologySpec(CORE_CLASSES, CORE_RELATIONS)
GOLDEN = {
    "entities": [
        {
            "class_name": "Application",
            "key": "APP-ERP",
            "properties": {"name": "Finance ERP", "criticality": "high"},
        },
        {"class_name": "Software", "key": "SW-QDOC", "properties": {}},
    ],
    "relations": [{"relation": "USES", "source": "APP-CRM", "target": "SW-QDOC"}],
}


def application(**props: str) -> CandidateEntity:
    return CandidateEntity(class_name="Application", properties={"app_code": "APP-ERP", **props})


def uses(src: str, tgt: str) -> CandidateRelation:
    return CandidateRelation(
        relation="USES",
        source=EntityRef(class_name="Application", key=src),
        target=EntityRef(class_name="Software", key=tgt),
    )


SOFTWARE = CandidateEntity(class_name="Software", properties={"software_id": "SW-QDOC"})


def test_perfect_extraction() -> None:
    s = score_facts(
        [application(name="Finance ERP", criticality="high"), SOFTWARE, uses("APP-CRM", "SW-QDOC")],
        GOLDEN,
        SPEC,
    )
    assert (s.precision, s.recall, s.f1, s.attribute_accuracy) == (1.0, 1.0, 1.0, 1.0)


def test_identity_matching_ignores_case_and_whitespace() -> None:
    s = score_facts(
        [
            CandidateEntity(class_name="Application", properties={"app_code": " app-erp "}),
            SOFTWARE,
            uses("app-crm", "sw-qdoc"),
        ],
        GOLDEN,
        SPEC,
    )
    assert s.recall == 1.0 and s.precision == 1.0


def test_hallucination_and_miss() -> None:
    extra = CandidateEntity(class_name="Software", properties={"software_id": "SW-NOPE"})
    s = score_facts([application(name="Finance ERP"), extra], GOLDEN, SPEC)
    assert (s.true_positives, s.false_positives, s.false_negatives) == (1, 1, 2)
    assert s.precision == 0.5 and s.recall == pytest.approx(1 / 3)
    assert s.f1 == pytest.approx(2 * 0.5 * (1 / 3) / (0.5 + 1 / 3))


def test_attribute_accuracy_counts_wrong_and_missing_properties() -> None:
    s = score_facts([application(name="Finance ERP Suite", criticality="HIGH")], GOLDEN, SPEC)
    assert s.attribute_accuracy == 0.5  # criticality right (case-insensitive), name wrong


def test_empty_prediction() -> None:
    s = score_facts([], GOLDEN, SPEC)
    assert (s.precision, s.recall, s.f1) == (0.0, 0.0, 0.0)
    assert s.attribute_accuracy is None  # nothing matched: not "1.00" (review round 1)


def test_entities_without_identity_key_are_ignored() -> None:
    assert (
        fact_keys([CandidateEntity(class_name="Application", properties={"name": "x"})], SPEC)
        == set()
    )


def test_agreement_is_mean_pairwise_jaccard() -> None:
    a, b = entity_key("Software", "A"), entity_key("Software", "B")
    assert agreement([{a, b}, {a, b}]) == 1.0
    assert agreement([{a}, {a, b}]) == 0.5
    assert agreement([{a}, {a, b}, {a, b}]) == pytest.approx((0.5 + 0.5 + 1.0) / 3)
    assert agreement([{a}]) == 1.0


def test_relation_from_the_wrong_class_is_not_a_match() -> None:
    wrong = CandidateRelation(
        relation="USES",
        source=EntityRef(class_name="Department", key="APP-CRM"),  # golden source: Application
        target=EntityRef(class_name="Software", key="SW-QDOC"),
    )
    s = score_facts([SOFTWARE, wrong], GOLDEN, SPEC)
    assert (s.true_positives, s.false_positives, s.false_negatives) == (1, 1, 2)


def billed_on(cls: str, tag: str, invoice: str) -> CandidateRelation:
    return CandidateRelation(
        relation="BILLED_ON",
        source=EntityRef(class_name=cls, key=tag),
        target=EntityRef(class_name="Invoice", key=invoice),
    )


def test_golden_can_name_endpoint_classes_explicitly() -> None:
    """BILLED_ON's domain is Asset; a laptop is billed as a HardwareAsset (a subclass)."""
    golden = {
        "entities": [],
        "relations": [
            {
                "relation": "BILLED_ON",
                "source": "HW-25-0101",
                "source_class": "HardwareAsset",
                "target": "INV-2025-0418",
                "target_class": "Invoice",
            }
        ],
    }
    fact = billed_on("HardwareAsset", "HW-25-0101", "INV-2025-0418")
    assert score_facts([fact], golden, SPEC).f1 == 1.0


def test_golden_endpoint_classes_default_to_domain_and_range() -> None:
    golden = {
        "entities": [],
        "relations": [{"relation": "BILLED_ON", "source": "HW-25-0101", "target": "INV-2025-0418"}],
    }
    as_asset = billed_on("Asset", "HW-25-0101", "INV-2025-0418")
    as_hardware = billed_on("HardwareAsset", "HW-25-0101", "INV-2025-0418")
    assert score_facts([as_asset], golden, SPEC).f1 == 1.0
    assert score_facts([as_hardware], golden, SPEC).f1 == 0.0  # golden must name the subclass
