from datetime import date

import pytest

from writer_service.ontology.spec import OntologySpec
from writer_service.pipeline.candidates import CandidateEntity, CandidateRelation, EntityRef
from writer_service.pipeline.gates import schema_gate


def ent(cls: str, **props: object) -> CandidateEntity:
    return CandidateEntity(class_name=cls, properties=props)  # type: ignore[arg-type]


def rel(
    name: str, src: tuple[str, str], tgt: tuple[str, str], **props: object
) -> CandidateRelation:
    return CandidateRelation(
        relation=name,
        source=EntityRef(class_name=src[0], key=src[1]),
        target=EntityRef(class_name=tgt[0], key=tgt[1]),
        properties=props,
    )  # type: ignore[arg-type]


def test_valid_entity_is_normalised(spec: OntologySpec) -> None:
    failures, out = schema_gate(
        ent("Filing", filing_id=" AR-2025-APH ", fiscal_year="2025", due_on="2026-04-30"), spec
    )
    assert failures == []
    assert isinstance(out, CandidateEntity)
    assert out.properties == {
        "filing_id": "AR-2025-APH",
        "fiscal_year": 2025,
        "due_on": date(2026, 4, 30),
    }


@pytest.mark.parametrize(
    "candidate,message",
    [
        (ent("Company", name="x"), "unknown class 'Company'"),
        (ent("Issuer", name="Aurora"), "required property 'ticker' is missing"),
        (ent("Issuer", ticker="  ", name="A"), "required property 'ticker' is missing"),
        (ent("Issuer", ticker="APH", name="A", ceo="x"), "class 'Issuer' has no property 'ceo'"),
        (ent("Filing", filing_id="F", fiscal_year="twenty"), "'fiscal_year' expects int"),
        (ent("Filing", filing_id="F", due_on="30/04/2026"), "'due_on' expects date"),
        (ent("Filing", filing_id="F", fiscal_year=True), "'fiscal_year' expects int"),
    ],
)
def test_entity_failures(spec: OntologySpec, candidate: CandidateEntity, message: str) -> None:
    failures, _ = schema_gate(candidate, spec)
    assert any(message in f for f in failures), failures
    assert all(f.startswith("schema: ") for f in failures)


def test_deprecated_class_rejected(spec: OntologySpec) -> None:
    from dataclasses import replace

    deprecated = OntologySpec(
        tuple(replace(c, deprecated=True) if c.name == "Finding" else c for c in spec.classes),
        spec.relations,
    )
    failures, _ = schema_gate(ent("Finding", finding_id="F1"), deprecated)
    assert any("class 'Finding' is deprecated" in f for f in failures)


def test_valid_relation(spec: OntologySpec) -> None:
    failures, out = schema_gate(
        rel("FILED_BY", ("Filing", " AR-2025-APH"), ("Issuer", "APH")), spec
    )
    assert failures == []
    assert isinstance(out, CandidateRelation) and out.source.key == "AR-2025-APH"


def test_relation_accepts_subclass_endpoint_via_taxonomy(spec: OntologySpec) -> None:
    from writer_service.ontology.spec import RelationSpec

    with_org = OntologySpec(
        spec.classes, (*spec.relations, RelationSpec("AUDITS", "Organisation", "Filing"))
    )
    failures, _ = schema_gate(rel("AUDITS", ("Issuer", "APH"), ("Filing", "F")), with_org)
    assert failures == []  # an Issuer is-a Organisation


@pytest.mark.parametrize(
    "candidate,message",
    [
        (rel("OWNS", ("Issuer", "A"), ("Issuer", "B")), "unknown relation 'OWNS'"),
        (
            rel("FILED_BY", ("Issuer", "A"), ("Issuer", "B")),
            "FILED_BY expects Filing -> Issuer, got Issuer -> Issuer",
        ),
        (
            rel("FILED_BY", ("Filing", "F"), ("Organisation", "O")),
            "FILED_BY expects Filing -> Issuer, got Filing -> Organisation",
        ),  # parent is not a child
        (rel("FILED_BY", ("Filing", ""), ("Issuer", "A")), "source key is empty"),
        (rel("FILED_BY", ("Ghost", "F"), ("Issuer", "A")), "unknown class 'Ghost'"),
        (
            rel("FILED_BY", ("Filing", "F"), ("Issuer", "A"), weight=1),
            "relations carry no properties",
        ),
    ],
)
def test_relation_failures(spec: OntologySpec, candidate: CandidateRelation, message: str) -> None:
    failures, _ = schema_gate(candidate, spec)
    assert any(message in f for f in failures), failures
