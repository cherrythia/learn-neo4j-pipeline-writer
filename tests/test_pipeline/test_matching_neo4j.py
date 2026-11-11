"""Neo4jMatchIndex against a real Neo4j (compose). Skipped unless WRITER_TEST_NEO4J_URI is set.
Creates and deletes its own throwaway Issuer nodes."""

import os
from collections.abc import Iterator

import pytest
from neo4j import Driver, GraphDatabase

from writer_service.ontology.spec import OntologySpec
from writer_service.pipeline.matching import EntityMatcher, Neo4jMatchIndex

pytestmark = pytest.mark.integration
KEYS = ("__T_AUR__", "__T_BAN__")


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
            "MERGE (a:Issuer {ticker: $a}) SET a.name = 'Zzyzx Aurora Test Holdings' "
            "MERGE (b:Issuer {ticker: $b}) SET b.name = 'Zzyzx Banyan Test Logistics'",
            a=KEYS[0],
            b=KEYS[1],
        )
        try:
            yield d
        finally:
            d.execute_query("MATCH (n:Issuer) WHERE n.ticker IN $k DETACH DELETE n", k=list(KEYS))


def test_exact_and_fuzzy_resolution_on_real_graph(driver: Driver, spec: OntologySpec) -> None:
    matcher = EntityMatcher(Neo4jMatchIndex(driver), spec)
    assert matcher.resolve("Issuer", KEYS[0].lower()).key == KEYS[0]
    fuzzy = matcher.resolve("Issuer", "zzyzx aurora test holdings ltd")
    assert (fuzzy.status, fuzzy.key) == ("fuzzy", KEYS[0])
