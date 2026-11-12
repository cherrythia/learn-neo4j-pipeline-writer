from datetime import date

import pytest
from pydantic import TypeAdapter, ValidationError

from writer_service.ontology.spec import OntologySpec
from writer_service.pipeline.candidates import (
    Candidate,
    CandidateEntity,
    CandidateRelation,
    EntityRef,
    Evidence,
    canonical_text,
    fact_hash,
    identity_hash,
)

EV = Evidence(source_type="pdf", source_ref="samples/a.pdf", locator="page 1", quote="APP-ERP ...")


def application(**props: object) -> CandidateEntity:
    base: dict[str, object] = {"app_code": "APP-ERP", "name": "Finance ERP System (Demo)"}
    base.update(props)
    return CandidateEntity(class_name="Application", properties=base, evidence=[EV])  # type: ignore[arg-type]


def test_canonical_text_collapses_whitespace() -> None:
    assert canonical_text("  Finance   ERP\n System ") == "Finance ERP System"


def test_fact_hash_ignores_whitespace_case_order_and_evidence() -> None:
    a = application()
    b = CandidateEntity(
        class_name="Application",
        properties={"name": " finance erp  SYSTEM (demo)", "app_code": "app-erp "},
        evidence=[],
        confidence=0.2,
    )
    assert fact_hash(a) == fact_hash(b)
    assert fact_hash(a) != fact_hash(application(criticality="low"))


def test_identity_hash_uses_only_class_and_key(spec: OntologySpec) -> None:
    assert identity_hash(application(), spec) == identity_hash(application(criticality="x"), spec)
    other = CandidateEntity(
        class_name="Application", properties={"app_code": "APP-CRM", "name": "B"}
    )
    assert identity_hash(application(), spec) != identity_hash(other, spec)


def test_relation_hash_normalises_endpoints() -> None:
    r1 = CandidateRelation(
        relation="ASSIGNED_TO",
        source=EntityRef(class_name="HardwareAsset", key="HW-0001"),
        target=EntityRef(class_name="Department", key="FIN"),
    )
    r2 = CandidateRelation(
        relation="ASSIGNED_TO",
        source=EntityRef(class_name="HardwareAsset", key=" hw-0001"),
        target=EntityRef(class_name="Department", key="fin"),
    )
    assert fact_hash(r1) == fact_hash(r2)


def test_dates_hash_like_their_iso_string() -> None:
    a = CandidateEntity(
        class_name="HardwareAsset",
        properties={"asset_tag": "X", "purchase_date": date(2026, 4, 30)},
    )
    b = CandidateEntity(
        class_name="HardwareAsset", properties={"asset_tag": "X", "purchase_date": "2026-04-30"}
    )
    assert fact_hash(a) == fact_hash(b)


def test_discriminated_union_parses_llm_json() -> None:
    adapter: TypeAdapter[Candidate] = TypeAdapter(Candidate)
    rel = adapter.validate_python(
        {
            "kind": "relation",
            "relation": "ASSIGNED_TO",
            "source": {"class_name": "HardwareAsset", "key": "HW-0001"},
            "target": {"class_name": "Department", "key": "FIN"},
        }
    )
    assert isinstance(rel, CandidateRelation)


@pytest.mark.parametrize("confidence", [-0.1, 1.1])
def test_confidence_is_a_probability(confidence: float) -> None:
    with pytest.raises(ValidationError):
        CandidateEntity(class_name="Application", properties={}, confidence=confidence)


def test_evidence_requires_quote() -> None:
    with pytest.raises(ValidationError):
        Evidence(source_type="pdf", source_ref="a", locator="p1", quote="")


def test_match_key_lowercases_like_cypher_tolower() -> None:
    from writer_service.pipeline.candidates import match_key

    assert match_key("  STRASSE ") == "strasse"
    assert match_key("Straße") == "straße"  # not folded to 'strasse' (Cypher wouldn't either)
