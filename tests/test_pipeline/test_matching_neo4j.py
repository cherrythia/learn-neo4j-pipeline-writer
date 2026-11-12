"""Neo4jMatchIndex against a real Neo4j (compose). Skipped unless WRITER_TEST_NEO4J_URI is set.
Creates and deletes its own throwaway Department nodes."""

import os
from collections.abc import Iterator

import pytest
from neo4j import Driver, GraphDatabase

from writer_service.ontology.spec import OntologySpec
from writer_service.pipeline.matching import EntityMatcher, Neo4jMatchIndex

pytestmark = pytest.mark.integration
KEYS = ("__T_FIN__", "__T_OPS__")


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
            "MERGE (a:Department {dept_code: $a}) SET a.name = 'Zzyzx Finance Test Department' "
            "MERGE (b:Department {dept_code: $b}) SET b.name = 'Zzyzx Harbor Test Operations'",
            a=KEYS[0],
            b=KEYS[1],
        )
        try:
            yield d
        finally:
            d.execute_query(
                "MATCH (n:Department) WHERE n.dept_code IN $k DETACH DELETE n", k=list(KEYS)
            )


def test_exact_and_fuzzy_resolution_on_real_graph(driver: Driver, spec: OntologySpec) -> None:
    matcher = EntityMatcher(Neo4jMatchIndex(driver), spec)
    assert matcher.resolve("Department", KEYS[0].lower()).key == KEYS[0]
    fuzzy = matcher.resolve("Department", "zzyzx finance test department ltd")
    assert (fuzzy.status, fuzzy.key) == ("fuzzy", KEYS[0])
