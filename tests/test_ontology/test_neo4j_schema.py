from writer_service.ontology.core import CORE_CLASSES, CORE_RELATIONS
from writer_service.ontology.neo4j_schema import (
    MANAGED_PREFIX,
    InMemoryGraphSchema,
    desired_constraints,
    neo4j_label,
    sync_constraints,
)
from writer_service.ontology.spec import ClassSpec, OntologySpec, PropertySpec

CORE = OntologySpec(classes=CORE_CLASSES, relations=CORE_RELATIONS)


def test_labels() -> None:
    assert neo4j_label(ClassSpec("Department", "dept_code", ())) == "Department"
    assert neo4j_label(ClassSpec("Warranty", "id", (), namespace="ext")) == "Ext_Warranty"


def test_desired_constraints_cover_classes_and_provenance() -> None:
    desired = desired_constraints(CORE)
    assert desired["wsc_department_dept_code"] == (
        "CREATE CONSTRAINT wsc_department_dept_code IF NOT EXISTS "
        "FOR (n:Department) REQUIRE n.dept_code IS UNIQUE"
    )
    # parent and subclasses share the key name but each label gets its own constraint
    for name in (
        "wsc_asset_asset_tag",
        "wsc_hardwareasset_asset_tag",
        "wsc_softwarelicense_asset_tag",
    ):
        assert name in desired
    for name in ("wsc_evidence_id", "wsc_source_id", "wsc_ingestionrun_id"):
        assert name in desired
    assert all(n.startswith(MANAGED_PREFIX) for n in desired)


def test_deprecated_classes_get_no_constraint() -> None:
    spec = OntologySpec(
        classes=(ClassSpec("Old", "id", (PropertySpec("id", "string", True),), deprecated=True),),
        relations=(),
    )
    assert not any("old" in n for n in desired_constraints(spec))


def test_sync_creates_missing_drops_stale_managed_and_keeps_foreign() -> None:
    graph = InMemoryGraphSchema({"wsc_stale_id", "someone_elses_constraint"})
    result = sync_constraints(graph, desired_constraints(CORE))
    assert "wsc_department_dept_code" in result.created
    assert result.dropped == ["wsc_stale_id"]
    assert "someone_elses_constraint" in graph.names
    assert "wsc_stale_id" not in graph.names
    assert any(s.startswith("DROP CONSTRAINT wsc_stale_id IF EXISTS") for s in graph.executed)


def test_sync_is_idempotent() -> None:
    graph = InMemoryGraphSchema()
    sync_constraints(graph, desired_constraints(CORE))
    again = sync_constraints(graph, desired_constraints(CORE))
    assert again.created == [] and again.dropped == []


# ---------------------------------------------------------------- full-text indexes (#28)


def test_fulltext_index_per_class_covers_string_key_and_name() -> None:
    from writer_service.ontology.neo4j_schema import desired_indexes

    idx = desired_indexes(CORE)
    assert idx["wsf_department_dept_code_name"] == (
        "CREATE FULLTEXT INDEX wsf_department_dept_code_name IF NOT EXISTS "
        "FOR (n:Department) ON EACH [n.dept_code, n.name] "
        "OPTIONS {indexConfig: {`fulltext.analyzer`: 'standard-folding'}}"
    )
    # identity key *is* the name: one field
    assert "FOR (n:Vendor) ON EACH [n.name]" in idx["wsf_vendor_name"]
    assert "ON EACH [n.software_id, n.name]" in idx["wsf_software_software_id_name"]
    assert "ON EACH [n.invoice_number]" in idx["wsf_invoice_invoice_number"]  # no name property


def test_fulltext_index_skips_non_string_keys_and_deprecated_classes() -> None:
    from writer_service.ontology.neo4j_schema import desired_indexes

    spec = OntologySpec(
        classes=(
            ClassSpec("Num", "n", (PropertySpec("n", "int", True),), namespace="ext"),
            ClassSpec(
                "Named",
                "n",
                (PropertySpec("n", "int", True), PropertySpec("title", "string")),
                namespace="ext",
            ),
            ClassSpec("Old", "id", (PropertySpec("id", "string", True),), deprecated=True),
        ),
        relations=(),
    )
    assert desired_indexes(spec) == {
        "wsf_ext_named_title": (
            "CREATE FULLTEXT INDEX wsf_ext_named_title IF NOT EXISTS "
            "FOR (n:Ext_Named) ON EACH [n.title] "
            "OPTIONS {indexConfig: {`fulltext.analyzer`: 'standard-folding'}}"
        )
    }


def test_sync_manages_indexes_and_drops_stale_ones_as_indexes() -> None:
    from writer_service.ontology.neo4j_schema import desired_schema

    graph = InMemoryGraphSchema({"wsf_department_dept_code", "wsc_stale_id", "foreign_index"})
    result = sync_constraints(graph, desired_schema(CORE))
    assert "wsf_department_dept_code_name" in result.created
    assert "wsc_department_dept_code" in result.created
    assert set(result.dropped) == {"wsf_department_dept_code", "wsc_stale_id"}
    assert "DROP INDEX wsf_department_dept_code IF EXISTS" in graph.executed
    assert "DROP CONSTRAINT wsc_stale_id IF EXISTS" in graph.executed
    assert "foreign_index" in graph.names and "wsf_department_dept_code_name" in graph.names
    assert sync_constraints(graph, desired_schema(CORE)).created == []
