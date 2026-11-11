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

EV = Evidence(source_type="pdf", source_ref="samples/a.pdf", locator="page 1", quote="APH ...")


def issuer(**props: object) -> CandidateEntity:
    base: dict[str, object] = {"ticker": "APH", "name": "Aurora Pacific Holdings (Demo)"}
    base.update(props)
    return CandidateEntity(class_name="Issuer", properties=base, evidence=[EV])  # type: ignore[arg-type]


def test_canonical_text_collapses_whitespace() -> None:
    assert canonical_text("  Aurora   Pacific\n Holdings ") == "Aurora Pacific Holdings"


def test_fact_hash_ignores_whitespace_case_order_and_evidence() -> None:
    a = issuer()
    b = CandidateEntity(
        class_name="Issuer",
        properties={"name": " aurora pacific  HOLDINGS (demo)", "ticker": "aph "},
        evidence=[],
        confidence=0.2,
    )
    assert fact_hash(a) == fact_hash(b)
    assert fact_hash(a) != fact_hash(issuer(board="catalist"))


def test_identity_hash_uses_only_class_and_key(spec: OntologySpec) -> None:
    assert identity_hash(issuer(), spec) == identity_hash(issuer(board="x"), spec)
    other = CandidateEntity(class_name="Issuer", properties={"ticker": "BYL", "name": "B"})
    assert identity_hash(issuer(), spec) != identity_hash(other, spec)


def test_relation_hash_normalises_endpoints() -> None:
    r1 = CandidateRelation(
        relation="FILED_BY",
        source=EntityRef(class_name="Filing", key="AR-2025-APH"),
        target=EntityRef(class_name="Issuer", key="APH"),
    )
    r2 = CandidateRelation(
        relation="FILED_BY",
        source=EntityRef(class_name="Filing", key=" ar-2025-aph"),
        target=EntityRef(class_name="Issuer", key="aph"),
    )
    assert fact_hash(r1) == fact_hash(r2)


def test_dates_hash_like_their_iso_string() -> None:
    a = CandidateEntity(
        class_name="Filing", properties={"filing_id": "X", "due_on": date(2026, 4, 30)}
    )
    b = CandidateEntity(class_name="Filing", properties={"filing_id": "X", "due_on": "2026-04-30"})
    assert fact_hash(a) == fact_hash(b)


def test_discriminated_union_parses_llm_json() -> None:
    adapter: TypeAdapter[Candidate] = TypeAdapter(Candidate)
    rel = adapter.validate_python(
        {
            "kind": "relation",
            "relation": "FILED_BY",
            "source": {"class_name": "Filing", "key": "AR-2025-APH"},
            "target": {"class_name": "Issuer", "key": "APH"},
        }
    )
    assert isinstance(rel, CandidateRelation)


@pytest.mark.parametrize("confidence", [-0.1, 1.1])
def test_confidence_is_a_probability(confidence: float) -> None:
    with pytest.raises(ValidationError):
        CandidateEntity(class_name="Issuer", properties={}, confidence=confidence)


def test_evidence_requires_quote() -> None:
    with pytest.raises(ValidationError):
        Evidence(source_type="pdf", source_ref="a", locator="p1", quote="")


def test_match_key_lowercases_like_cypher_tolower() -> None:
    from writer_service.pipeline.candidates import match_key

    assert match_key("  STRASSE ") == "strasse"
    assert match_key("Straße") == "straße"  # not folded to 'strasse' (Cypher wouldn't either)
