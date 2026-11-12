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
HW, DEPT, INVOICE = "__T_HW__", "__T_DEPT__", "__T_INVOICE__"


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
            "MERGE (h:HardwareAsset:Asset {asset_tag: $h}) "
            "SET h.name = 'Test', h.status = 'in_use' "
            "MERGE (d:Department {dept_code: $d}) SET d.name = 'Test' "
            "MERGE (v:Invoice {invoice_number: $v}) "
            "MERGE (h)-[:ASSIGNED_TO]->(d) MERGE (h)-[:BILLED_ON]->(v)",
            h=HW,
            d=DEPT,
            v=INVOICE,
        )
        try:
            yield d
        finally:
            d.execute_query(
                "MATCH (n) WHERE n.asset_tag = $h OR n.dept_code = $d OR n.invoice_number = $v "
                "DETACH DELETE n",
                h=HW,
                d=DEPT,
                v=INVOICE,
            )


def test_reads_nodes_and_edges_case_insensitively(driver: Driver) -> None:
    view = Neo4jGraphView(driver)
    assert view.node_properties("HardwareAsset", "asset_tag", HW.lower())["status"] == "in_use"  # type: ignore[index]
    assert view.targets(
        "ASSIGNED_TO", ("HardwareAsset", "asset_tag", HW), {"Department": "dept_code"}
    ) == [{"Department": DEPT}]
    assert view.sources(
        "ASSIGNED_TO", ("Department", "dept_code", DEPT), {"HardwareAsset": "asset_tag"}
    ) == [{"HardwareAsset": HW}]
    assert view.node_properties("HardwareAsset", "asset_tag", "__nope__") is None


def test_gates_detect_conflicts_against_real_graph(driver: Driver, spec: OntologySpec) -> None:
    results = check_batch(
        [
            CandidateEntity(
                class_name="HardwareAsset",
                properties={"asset_tag": HW, "name": "Test", "status": "retired"},
            ),
            CandidateRelation(
                relation="ASSIGNED_TO",
                source=EntityRef(class_name="HardwareAsset", key=HW),
                target=EntityRef(class_name="Department", key="__OTHER__"),
            ),
        ],
        spec,
        Neo4jGraphView(driver),
    )
    assert [r.status for r in results] == ["conflict", "conflict"]


def test_subclass_source_and_whitespace_variant_key(driver: Driver, spec: OntologySpec) -> None:
    """The gate canonicalises '__T_INVOICE__' typed with doubled spaces etc.; the edge's far
    node is reported per label so a parent-domain relation (BILLED_ON from Asset) sees a
    HardwareAsset source under both of its labels."""
    view = Neo4jGraphView(driver)
    far = view.sources(
        "BILLED_ON",
        ("Invoice", "invoice_number", f"  {INVOICE} "),
        {"HardwareAsset": "asset_tag", "Asset": "asset_tag"},
    )
    assert far == [{"HardwareAsset": HW, "Asset": HW}]
