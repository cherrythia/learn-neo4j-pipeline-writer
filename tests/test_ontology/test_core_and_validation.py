import pytest

from writer_service.ontology.core import CORE_CLASSES, CORE_RELATIONS
from writer_service.ontology.spec import ClassSpec, OntologySpec, PropertySpec, RelationSpec
from writer_service.ontology.validation import validate

CORE = OntologySpec(classes=CORE_CLASSES, relations=CORE_RELATIONS)


def test_core_ontology_is_valid() -> None:
    assert validate(CORE) == []


def test_core_contains_expected_classes_relations_and_taxonomy() -> None:
    names = {c.name for c in CORE_CLASSES}
    assert {"Issuer", "Filing", "Rule", "Obligation", "Finding"} <= names
    parents = {c.name: c.parent for c in CORE_CLASSES}
    assert parents["Issuer"] == "Organisation" and parents["Filing"] == "Document"
    rel = {r.name: (r.domain, r.range) for r in CORE_RELATIONS}
    assert rel["FILED_BY"] == ("Filing", "Issuer")
    assert rel["DERIVES_FROM"] == ("Obligation", "Rule")


def cls(name: str, key: str = "id", parent: str | None = None, **kw: object) -> ClassSpec:
    return ClassSpec(
        name=name,
        identity_key=key,
        parent=parent,
        properties=(PropertySpec("id", "string", True),),
        **kw,
    )  # type: ignore[arg-type]


def errors(
    *classes: ClassSpec,
    relations: tuple[RelationSpec, ...] = (),
    previous: OntologySpec | None = None,
) -> list[str]:
    return validate(OntologySpec(classes=classes, relations=relations), previous=previous)


def test_identity_key_must_be_a_required_property() -> None:
    bad = ClassSpec(name="A", identity_key="code", properties=(PropertySpec("id", "string", True),))
    assert any("identity key 'code'" in e for e in errors(bad))
    optional = ClassSpec(
        name="A", identity_key="id", properties=(PropertySpec("id", "string", False),)
    )
    assert any("must be required" in e for e in errors(optional))


def test_unknown_parent_and_cycle() -> None:
    assert any("unknown parent 'Nope'" in e for e in errors(cls("A", parent="Nope")))
    assert any("cycle" in e for e in errors(cls("A", parent="B"), cls("B", parent="A")))


def test_relation_endpoints_must_exist_and_not_be_deprecated() -> None:
    r = RelationSpec(name="LINKS", domain="A", range="Z")
    assert any("range 'Z'" in e for e in errors(cls("A"), relations=(r,)))
    dep = cls("Z", deprecated=True)
    assert any("deprecated" in e for e in errors(cls("A"), dep, relations=(r,)))


@pytest.mark.parametrize(
    "name",
    ["issuer", "Issuer) DETACH DELETE n //", "Is suer", "Ext_Thing", ""],
)
def test_class_names_are_strict_identifiers(name: str) -> None:
    assert any("invalid class name" in e for e in errors(cls(name)))


@pytest.mark.parametrize("name", ["filedBy", "FILED BY", "X`]->()", "_X"])
def test_relation_names_are_strict(name: str) -> None:
    r = RelationSpec(name=name, domain="A", range="A")
    assert any("invalid relation name" in e for e in errors(cls("A"), relations=(r,)))


def test_property_names_are_strict() -> None:
    bad = ClassSpec(
        name="A",
        identity_key="id",
        properties=(PropertySpec("id", "string", True), PropertySpec("Bad Name", "string")),
    )
    assert any("invalid property name" in e for e in errors(bad))


def test_duplicate_names_rejected() -> None:
    assert any("duplicate class" in e for e in errors(cls("A"), cls("A")))


def test_removing_a_published_class_is_rejected_deprecate_instead() -> None:
    previous = OntologySpec(classes=(cls("A"), cls("B")), relations=())
    errs = errors(cls("A"), previous=previous)
    assert any("'B' was removed" in e and "deprecate" in e for e in errs)
    assert errors(cls("A"), cls("B", deprecated=True), previous=previous) == []


def test_removing_a_published_relation_is_rejected() -> None:
    r = RelationSpec(name="LINKS", domain="A", range="A")
    previous = OntologySpec(classes=(cls("A"),), relations=(r,))
    assert any("relation 'LINKS' was removed" in e for e in errors(cls("A"), previous=previous))


def test_ext_namespace_allowed() -> None:
    ext = ClassSpec(
        name="ESGMetric",
        identity_key="id",
        namespace="ext",
        properties=(PropertySpec("id", "string", True),),
    )
    assert errors(cls("A"), ext) == []


def k(name: str, key: str = "k", namespace: str = "core") -> ClassSpec:
    return ClassSpec(
        name=name,
        identity_key=key,
        namespace=namespace,  # type: ignore[arg-type]
        properties=(PropertySpec(key, "string", True),),
    )


def test_constraint_name_collision_case_only() -> None:
    errs = errors(k("FooBar"), k("Foobar"))
    assert any("collides" in e for e in errs)


def test_constraint_name_collision_across_namespace_split() -> None:
    errs = errors(k("Ext", key="foo_bar"), k("Foo", key="bar", namespace="ext"))
    assert any("wsc_ext_foo_bar" in e for e in errs)


def test_constraint_name_collision_with_provenance() -> None:
    assert any("provenance Evidence" in e for e in errors(k("Evidence", key="id")))


def test_published_class_cannot_change_identity_key_namespace_or_lose_properties() -> None:
    old = ClassSpec("A", "id", (PropertySpec("id", "string", True), PropertySpec("x")))
    previous = OntologySpec(classes=(old,), relations=())
    rekeyed = ClassSpec(
        "A", "x", (PropertySpec("id", "string", True), PropertySpec("x", required=True))
    )
    assert any("identity key changed" in e for e in errors(rekeyed, previous=previous))
    moved = ClassSpec("A", "id", old.properties, namespace="ext")
    assert any("namespace changed" in e for e in errors(moved, previous=previous))
    trimmed = ClassSpec("A", "id", (PropertySpec("id", "string", True),))
    assert any("property 'x' was removed" in e for e in errors(trimmed, previous=previous))
    extended = ClassSpec("A", "id", (*old.properties, PropertySpec("y")))
    assert errors(extended, previous=previous) == []  # adding is fine


def test_published_relation_cannot_change_endpoints() -> None:
    previous = OntologySpec(
        classes=(cls("A"), cls("B")), relations=(RelationSpec("LINKS", "A", "B"),)
    )
    errs = errors(
        cls("A"), cls("B"), relations=(RelationSpec("LINKS", "B", "A"),), previous=previous
    )
    assert any("endpoints changed" in e for e in errs)
