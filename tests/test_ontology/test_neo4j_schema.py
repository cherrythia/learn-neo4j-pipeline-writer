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
    assert neo4j_label(ClassSpec("Issuer", "ticker", ())) == "Issuer"
    assert neo4j_label(ClassSpec("ESGMetric", "id", (), namespace="ext")) == "Ext_ESGMetric"


def test_desired_constraints_cover_classes_and_provenance() -> None:
    desired = desired_constraints(CORE)
    assert desired["wsc_issuer_ticker"] == (
        "CREATE CONSTRAINT wsc_issuer_ticker IF NOT EXISTS "
        "FOR (n:Issuer) REQUIRE n.ticker IS UNIQUE"
    )
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
    assert "wsc_issuer_ticker" in result.created
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
    assert idx["wsf_issuer_ticker_name"] == (
        "CREATE FULLTEXT INDEX wsf_issuer_ticker_name IF NOT EXISTS "
        "FOR (n:Issuer) ON EACH [n.ticker, n.name] "
        "OPTIONS {indexConfig: {`fulltext.analyzer`: 'standard-folding'}}"
    )
    # identity key *is* the name: one field
    assert "FOR (n:Organisation) ON EACH [n.name]" in idx["wsf_organisation_name"]
    assert "ON EACH [n.rule_code, n.title]" in idx["wsf_rule_rule_code_title"]
    assert "ON EACH [n.finding_id]" in idx["wsf_finding_finding_id"]  # no name property


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

    graph = InMemoryGraphSchema({"wsf_issuer_ticker", "wsc_stale_id", "foreign_index"})
    result = sync_constraints(graph, desired_schema(CORE))
    assert "wsf_issuer_ticker_name" in result.created and "wsc_issuer_ticker" in result.created
    assert set(result.dropped) == {"wsf_issuer_ticker", "wsc_stale_id"}
    assert "DROP INDEX wsf_issuer_ticker IF EXISTS" in graph.executed
    assert "DROP CONSTRAINT wsc_stale_id IF EXISTS" in graph.executed
    assert "foreign_index" in graph.names and "wsf_issuer_ticker_name" in graph.names
    assert sync_constraints(graph, desired_schema(CORE)).created == []
