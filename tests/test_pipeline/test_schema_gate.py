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
        ent(
            "SoftwareLicense",
            asset_tag=" SL-25-0042 ",
            seats="250",
            expiry_date="2026-04-30",
            cost="44482.90",
        ),
        spec,
    )
    assert failures == []
    assert isinstance(out, CandidateEntity)
    assert out.properties == {
        "asset_tag": "SL-25-0042",
        "seats": 250,
        "expiry_date": date(2026, 4, 30),
        "cost": 44482.90,
    }


@pytest.mark.parametrize(
    "candidate,message",
    [
        (ent("Company", name="x"), "unknown class 'Company'"),
        (ent("Department", name="Finance"), "required property 'dept_code' is missing"),
        (ent("Department", dept_code="  ", name="F"), "required property 'dept_code' is missing"),
        (ent("Software", software_id="SW-QDOC"), "required property 'name' is missing"),
        (
            ent("Department", dept_code="FIN", name="F", head="x"),
            "class 'Department' has no property 'head'",
        ),
        (ent("SoftwareLicense", asset_tag="SL", seats="twenty"), "'seats' expects int"),
        (
            ent("SoftwareLicense", asset_tag="SL", expiry_date="30/04/2026"),
            "'expiry_date' expects date",
        ),
        (ent("SoftwareLicense", asset_tag="SL", seats=True), "'seats' expects int"),
        (ent("HardwareAsset", asset_tag="HW", cost="cheap"), "'cost' expects float"),
        (ent("HardwareAsset", asset_tag="HW", cost=True), "'cost' expects float"),
    ],
)
def test_entity_failures(spec: OntologySpec, candidate: CandidateEntity, message: str) -> None:
    failures, _ = schema_gate(candidate, spec)
    assert any(message in f for f in failures), failures
    assert all(f.startswith("schema: ") for f in failures)


def test_deprecated_class_rejected(spec: OntologySpec) -> None:
    from dataclasses import replace

    deprecated = OntologySpec(
        tuple(replace(c, deprecated=True) if c.name == "Application" else c for c in spec.classes),
        spec.relations,
    )
    failures, _ = schema_gate(
        ent("Application", app_code="APP-ERP", name="Finance ERP"), deprecated
    )
    assert any("class 'Application' is deprecated" in f for f in failures)


def test_valid_relation(spec: OntologySpec) -> None:
    failures, out = schema_gate(
        rel("ASSIGNED_TO", ("HardwareAsset", " HW-25-0101"), ("Department", "FIN")), spec
    )
    assert failures == []
    assert isinstance(out, CandidateRelation) and out.source.key == "HW-25-0101"


@pytest.mark.parametrize("source_class", ["Asset", "HardwareAsset", "SoftwareLicense"])
def test_relation_accepts_subclass_endpoint_via_taxonomy(
    spec: OntologySpec, source_class: str
) -> None:
    """BILLED_ON's domain is Asset; a HardwareAsset or SoftwareLicense is-a Asset."""
    failures, _ = schema_gate(
        rel("BILLED_ON", (source_class, "HW-25-0101"), ("Invoice", "INV-2025-0418")), spec
    )
    assert failures == []


@pytest.mark.parametrize(
    "candidate,message",
    [
        (rel("OWNS", ("Department", "A"), ("Department", "B")), "unknown relation 'OWNS'"),
        (
            rel("ASSIGNED_TO", ("Department", "A"), ("Department", "B")),
            "ASSIGNED_TO expects HardwareAsset -> Department, got Department -> Department",
        ),
        (
            rel("ASSIGNED_TO", ("Asset", "HW"), ("Department", "FIN")),
            "ASSIGNED_TO expects HardwareAsset -> Department, got Asset -> Department",
        ),  # parent is not a child
        (
            rel("BILLED_ON", ("Software", "SW-QDOC"), ("Invoice", "INV-2025-0418")),
            "BILLED_ON expects Asset -> Invoice, got Software -> Invoice",
        ),  # not an Asset at all
        (rel("ASSIGNED_TO", ("HardwareAsset", ""), ("Department", "A")), "source key is empty"),
        (rel("ASSIGNED_TO", ("Ghost", "HW"), ("Department", "A")), "unknown class 'Ghost'"),
        (
            rel("ASSIGNED_TO", ("HardwareAsset", "HW"), ("Department", "A"), weight=1),
            "relations carry no properties",
        ),
    ],
)
def test_relation_failures(spec: OntologySpec, candidate: CandidateRelation, message: str) -> None:
    failures, _ = schema_gate(candidate, spec)
    assert any(message in f for f in failures), failures


def test_deprecated_relation_rejected(spec: OntologySpec) -> None:
    from dataclasses import replace

    dep = OntologySpec(
        spec.classes,
        tuple(
            replace(r, deprecated=True) if r.name == "INSTALLED_ON" else r for r in spec.relations
        ),
    )
    failures, _ = schema_gate(
        rel("INSTALLED_ON", ("Software", "SW-QDOC"), ("HardwareAsset", "HW-25-0101")), dep
    )
    assert failures == ["schema: relation 'INSTALLED_ON' is deprecated"]


def test_empty_target_key_rejected(spec: OntologySpec) -> None:
    failures, _ = schema_gate(
        rel("ASSIGNED_TO", ("HardwareAsset", "HW"), ("Department", "   ")), spec
    )
    assert "schema: target key is empty" in failures


def test_deprecated_endpoint_class_rejected(spec: OntologySpec) -> None:
    from dataclasses import replace

    dep = OntologySpec(
        tuple(replace(c, deprecated=True) if c.name == "Department" else c for c in spec.classes),
        spec.relations,
    )
    failures, _ = schema_gate(
        rel("ASSIGNED_TO", ("HardwareAsset", "HW-25-0101"), ("Department", "FIN")), dep
    )
    assert "schema: class 'Department' is deprecated" in failures
