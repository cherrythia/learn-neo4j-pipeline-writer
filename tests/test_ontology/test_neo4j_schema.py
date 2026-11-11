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
