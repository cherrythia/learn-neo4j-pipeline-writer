"""Neo4jGraph write/rollback against a real Neo4j (compose). Skipped unless
WRITER_TEST_NEO4J_URI is set. Uses throwaway keys and a throwaway run id."""

import os
import uuid
from collections.abc import Iterator

import pytest
from neo4j import Driver, GraphDatabase

from writer_service.ontology.core import CORE_CLASSES, CORE_RELATIONS
from writer_service.ontology.spec import OntologySpec
from writer_service.pipeline.candidates import (
    CandidateEntity,
    CandidateRelation,
    EntityRef,
    Evidence,
)
from writer_service.pipeline.graph_store import Neo4jGraph, to_fact

pytestmark = pytest.mark.integration
SPEC = OntologySpec(CORE_CLASSES, CORE_RELATIONS)
TICKER, FILING, OTHER = "__T_W_ISS__", "__T_W_FIL__", "__T_W_OTH__"


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
        try:
            yield d
        finally:
            d.execute_query(
                "MATCH (n) WHERE n.ticker IN $t OR n.filing_id = $f OR n.source_ref = 't.pdf' "
                "DETACH DELETE n",
                t=[TICKER, OTHER],
                f=FILING,
            )
            d.execute_query("MATCH (n:Evidence) WHERE n.quote STARTS WITH '__T_' DETACH DELETE n")
            d.execute_query("MATCH (n:Source {ref: 't.pdf'}) DETACH DELETE n")


def ev(quote: str) -> Evidence:
    return Evidence(source_type="pdf", source_ref="t.pdf", locator="page 1", quote=quote)


def facts() -> list:  # type: ignore[type-arg]
    issuer = CandidateEntity(
        class_name="Issuer",
        properties={"ticker": TICKER, "name": "Test Co"},
        evidence=[ev("__T_ issuer quote")],
    )
    filed = CandidateRelation(
        relation="FILED_BY",
        source=EntityRef(class_name="Filing", key=FILING),
        target=EntityRef(class_name="Issuer", key=TICKER),
        evidence=[ev("__T_ filed quote")],
    )
    return [to_fact(issuer, SPEC, 0.9), to_fact(filed, SPEC, 0.88)]


def count(driver: Driver, cypher: str, **params: object) -> int:
    records, _, _ = driver.execute_query(cypher, **params)
    return int(records[0]["c"])


def test_write_is_idempotent_with_provenance_and_taxonomy(driver: Driver) -> None:
    graph, run = Neo4jGraph(driver), f"test-{uuid.uuid4()}"
    graph.write_run(run, 1, facts())
    graph.write_run(run, 1, facts())  # same facts again: no duplicates
    assert (
        count(driver, "MATCH (n:Issuer:Organisation {ticker: $t}) RETURN count(n) AS c", t=TICKER)
        == 1
    )
    assert (
        count(
            driver,
            "MATCH (:Filing {filing_id: $f})-[r:FILED_BY]->(:Issuer {ticker: $t}) "
            "RETURN count(r) AS c",
            f=FILING,
            t=TICKER,
        )
        == 1
    )
    assert (
        count(
            driver,
            "MATCH (:Issuer {ticker: $t})-[:SUPPORTED_BY]->(:Evidence)-[:FROM]->"
            "(:Source {ref: 't.pdf'}) RETURN count(*) AS c",
            t=TICKER,
        )
        == 1
    )
    records, _, _ = driver.execute_query(
        "MATCH (n:Issuer {ticker: $t}) RETURN n.run_id AS run, n.score AS s, n.stub AS stub",
        t=TICKER,
    )
    assert (records[0]["run"], records[0]["s"], records[0]["stub"]) == (run, 0.9, False)
    graph.rollback_run(run)


def test_rollback_removes_only_what_the_run_created(driver: Driver) -> None:
    graph, run = Neo4jGraph(driver), f"test-{uuid.uuid4()}"
    graph.write_run(run, 1, facts())
    removed = graph.rollback_run(run)
    assert removed["nodes"] >= 2 and removed["relationships"] == 1 and removed["evidence"] == 2
    assert (
        count(
            driver,
            "MATCH (n) WHERE n.ticker = $t OR n.filing_id = $f RETURN count(n) AS c",
            t=TICKER,
            f=FILING,
        )
        == 0
    )


def test_replace_existing_swaps_the_target(driver: Driver) -> None:
    graph, run = Neo4jGraph(driver), f"test-{uuid.uuid4()}"
    graph.write_run(run, 1, facts())
    replacement = CandidateRelation(
        relation="FILED_BY",
        source=EntityRef(class_name="Filing", key=FILING),
        target=EntityRef(class_name="Issuer", key=OTHER),
        evidence=[ev("__T_ replacement quote")],
    )
    graph.write_run(run, 1, [to_fact(replacement, SPEC, 0.9, replace_existing=True)])
    records, _, _ = driver.execute_query(
        "MATCH (:Filing {filing_id: $f})-[:FILED_BY]->(i:Issuer) RETURN i.ticker AS t", f=FILING
    )
    assert [r["t"] for r in records] == [OTHER]
    graph.rollback_run(run)
