"""Constraint sync against a real Neo4j (compose stack). Skipped unless configured:

    WRITER_TEST_NEO4J_URI=bolt://127.0.0.1:7687 WRITER_TEST_NEO4J_PASSWORD=... \\
        uv run pytest -m integration

It creates the core ``wsc_*`` constraints and ``wsf_*`` full-text indexes (the ones the
app wants anyway), checks that
uniqueness is enforced, and removes only the throwaway node and constraint it adds.
"""

import os
from collections.abc import Iterator

import pytest
from neo4j import Driver, GraphDatabase
from neo4j.exceptions import ConstraintError

from writer_service.ontology.core import CORE_CLASSES, CORE_RELATIONS
from writer_service.ontology.neo4j_schema import (
    Neo4jGraphSchema,
    desired_schema,
    sync_constraints,
)
from writer_service.ontology.spec import OntologySpec

pytestmark = pytest.mark.integration


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
        yield d


def test_sync_enforces_uniqueness_and_drops_stale(driver: Driver) -> None:
    graph = Neo4jGraphSchema(driver)
    graph.execute(
        "CREATE CONSTRAINT wsc_zz_stale_test IF NOT EXISTS "
        "FOR (n:ZzStaleTest) REQUIRE n.k IS UNIQUE"
    )
    result = sync_constraints(graph, desired_schema(OntologySpec(CORE_CLASSES, CORE_RELATIONS)))
    names = graph.constraint_names()
    assert "wsc_zz_stale_test" in result.dropped and "wsc_zz_stale_test" not in names
    assert {"wsc_department_dept_code", "wsc_hardwareasset_asset_tag", "wsc_evidence_id"} <= names
    assert "wsf_department_dept_code_name" in graph.index_names()

    again = sync_constraints(graph, desired_schema(OntologySpec(CORE_CLASSES, CORE_RELATIONS)))
    assert again.created == [] and again.dropped == []

    dept_code = "__writer_integration_test__"
    try:
        driver.execute_query("CREATE (:Department {dept_code: $t})", t=dept_code)
        with pytest.raises(ConstraintError):
            driver.execute_query("CREATE (:Department {dept_code: $t})", t=dept_code)
    finally:
        driver.execute_query("MATCH (n:Department {dept_code: $t}) DETACH DELETE n", t=dept_code)
