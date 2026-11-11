"""Neo4jGraphView against a real Neo4j (compose). Skipped unless WRITER_TEST_NEO4J_URI is set.
Uses throwaway keys and deletes exactly the nodes it creates."""

import os
from collections.abc import Iterator

import pytest
from neo4j import Driver, GraphDatabase

from writer_service.ontology.spec import OntologySpec
from writer_service.pipeline.candidates import CandidateEntity, CandidateRelation, EntityRef
from writer_service.pipeline.gates import check_batch
from writer_service.pipeline.graph_view import Neo4jGraphView

pytestmark = pytest.mark.integration
ISSUER, FILING = "__T_ISSUER__", "__T_FILING__"


@pytest.fixture
def driver() -> Iterator[Driver]:
    uri = os.environ.get("WRITER_TEST_NEO4J_URI")
    if not uri:
        pytest.skip("WRITER_TEST_NEO4J_URI not set")
    auth = (
        os.environ.get("WRITER_TEST_NEO4J_USER", "neo4j"),
        os.environ.get("WRITER_TEST_NEO4J_PASSWORD", ""),
    )
    with GraphDatabase.driver(uri, auth=auth) as d:
        d.execute_query(
            "MERGE (i:Issuer {ticker: $i}) SET i.name = 'Test', i.board = 'mainboard' "
            "MERGE (f:Filing {filing_id: $f}) MERGE (f)-[:FILED_BY]->(i)",
            i=ISSUER,
            f=FILING,
        )
        try:
            yield d
        finally:
            d.execute_query(
                "MATCH (n) WHERE n.ticker = $i OR n.filing_id = $f DETACH DELETE n",
                i=ISSUER,
                f=FILING,
            )


def test_reads_nodes_and_edges_case_insensitively(driver: Driver) -> None:
    view = Neo4jGraphView(driver)
    assert view.node_properties("Issuer", "ticker", ISSUER.lower())["board"] == "mainboard"  # type: ignore[index]
    assert view.targets("FILED_BY", ("Filing", "filing_id", FILING), "Issuer", "ticker") == {ISSUER}
    assert view.sources("FILED_BY", ("Issuer", "ticker", ISSUER), "Filing", "filing_id") == {FILING}
    assert view.node_properties("Issuer", "ticker", "__nope__") is None


def test_gates_detect_conflicts_against_real_graph(driver: Driver, spec: OntologySpec) -> None:
    results = check_batch(
        [
            CandidateEntity(
                class_name="Issuer",
                properties={"ticker": ISSUER, "name": "Test", "board": "catalist"},
            ),
            CandidateRelation(
                relation="FILED_BY",
                source=EntityRef(class_name="Filing", key=FILING),
                target=EntityRef(class_name="Issuer", key="__OTHER__"),
            ),
        ],
        spec,
        Neo4jGraphView(driver),
    )
    assert [r.status for r in results] == ["conflict", "conflict"]
