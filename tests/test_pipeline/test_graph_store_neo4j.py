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
DEPT, HW, OTHER = "__T_W_DEPT__", "__T_W_HW__", "__T_W_OTH__"


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
                "MATCH (n) WHERE n.dept_code IN $d OR n.asset_tag = $h OR n.source_ref = 't.pdf' "
                "DETACH DELETE n",
                d=[DEPT, OTHER],
                h=HW,
            )
            d.execute_query("MATCH (n:Evidence) WHERE n.quote STARTS WITH '__T_' DETACH DELETE n")
            d.execute_query("MATCH (n:Source {ref: 't.pdf'}) DETACH DELETE n")
            d.execute_query("MATCH (r:IngestionRun) WHERE r.id STARTS WITH 'test-' DETACH DELETE r")


def ev(quote: str) -> Evidence:
    return Evidence(source_type="pdf", source_ref="t.pdf", locator="page 1", quote=quote)


def facts() -> list:  # type: ignore[type-arg]
    dept = CandidateEntity(
        class_name="Department",
        properties={"dept_code": DEPT, "name": "Test Dept"},
        evidence=[ev("__T_ department quote")],
    )
    assigned = CandidateRelation(
        relation="ASSIGNED_TO",
        source=EntityRef(class_name="HardwareAsset", key=HW),
        target=EntityRef(class_name="Department", key=DEPT),
        evidence=[ev("__T_ assigned quote")],
    )
    return [to_fact(dept, SPEC, 0.9), to_fact(assigned, SPEC, 0.88)]


def count(driver: Driver, cypher: str, **params: object) -> int:
    records, _, _ = driver.execute_query(cypher, **params)
    return int(records[0]["c"])


def test_write_is_idempotent_with_provenance_and_taxonomy(driver: Driver) -> None:
    graph, run = Neo4jGraph(driver), f"test-{uuid.uuid4()}"
    hardware = CandidateEntity(
        class_name="HardwareAsset",
        properties={"asset_tag": HW, "name": "Test Laptop"},
        evidence=[ev("__T_ hardware quote")],
    )
    batch = [*facts(), to_fact(hardware, SPEC, 0.9)]
    graph.write_run(run, 1, batch)
    graph.write_run(run, 1, batch)  # same facts again: no duplicates
    assert count(driver, "MATCH (n:Department {dept_code: $d}) RETURN count(n) AS c", d=DEPT) == 1
    # taxonomy: a HardwareAsset entity also carries its parent's Asset label
    assert (
        count(driver, "MATCH (n:HardwareAsset:Asset {asset_tag: $h}) RETURN count(n) AS c", h=HW)
        == 1
    )
    assert (
        count(
            driver,
            "MATCH (:HardwareAsset {asset_tag: $h})-[r:ASSIGNED_TO]->(:Department {dept_code: $d}) "
            "RETURN count(r) AS c",
            h=HW,
            d=DEPT,
        )
        == 1
    )
    assert (
        count(
            driver,
            "MATCH (:Department {dept_code: $d})-[:SUPPORTED_BY]->(:Evidence)-[:FROM]->"
            "(:Source {ref: 't.pdf'}) RETURN count(*) AS c",
            d=DEPT,
        )
        == 1
    )
    records, _, _ = driver.execute_query(
        "MATCH (n:Department {dept_code: $d}) RETURN n.run_id AS run, n.score AS s, n.stub AS stub",
        d=DEPT,
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
            "MATCH (n) WHERE n.dept_code = $d OR n.asset_tag = $h RETURN count(n) AS c",
            d=DEPT,
            h=HW,
        )
        == 0
    )


def test_replace_existing_swaps_the_target(driver: Driver) -> None:
    graph, run = Neo4jGraph(driver), f"test-{uuid.uuid4()}"
    graph.write_run(run, 1, facts())
    replacement = CandidateRelation(
        relation="ASSIGNED_TO",
        source=EntityRef(class_name="HardwareAsset", key=HW),
        target=EntityRef(class_name="Department", key=OTHER),
        evidence=[ev("__T_ replacement quote")],
    )
    graph.write_run(run, 1, [to_fact(replacement, SPEC, 0.9, replace_existing=True)])
    records, _, _ = driver.execute_query(
        "MATCH (:HardwareAsset {asset_tag: $h})-[:ASSIGNED_TO]->(i:Department) "
        "RETURN i.dept_code AS t",
        h=HW,
    )
    assert [r["t"] for r in records] == [OTHER]
    graph.rollback_run(run)


def test_rollback_keeps_what_another_run_owns(driver: Driver) -> None:
    graph = Neo4jGraph(driver)
    run1, run2 = f"test-{uuid.uuid4()}", f"test-{uuid.uuid4()}"
    dept_fact, assigned_fact = facts()
    graph.write_run(run1, 1, [assigned_fact])  # Department DEPT exists only as a stub endpoint
    graph.write_run(run2, 1, [dept_fact])  # a later run states the full entity
    graph.rollback_run(run1)
    records, _, _ = driver.execute_query(
        "MATCH (n:Department {dept_code: $d}) "
        "RETURN n.name AS name, n.runs AS runs, n.run_id AS run",
        d=DEPT,
    )
    assert records and records[0]["name"] == "Test Dept"
    assert records[0]["runs"] == [run2] and records[0]["run"] == run2
    assert (
        count(
            driver,
            "MATCH (:HardwareAsset {asset_tag: $h})-[r:ASSIGNED_TO]->() RETURN count(r) AS c",
            h=HW,
        )
        == 0
    )
    assert (
        count(
            driver,
            "MATCH (:Department {dept_code: $d})-[:SUPPORTED_BY]->(x:Evidence) "
            "RETURN count(x) AS c",
            d=DEPT,
        )
        == 1
    )
    graph.rollback_run(run2)
    assert count(driver, "MATCH (n:Department {dept_code: $d}) RETURN count(n) AS c", d=DEPT) == 0


def test_rollback_repairs_relationship_provenance(driver: Driver) -> None:
    graph = Neo4jGraph(driver)
    run1, run2 = f"test-{uuid.uuid4()}", f"test-{uuid.uuid4()}"
    _, assigned_fact = facts()
    graph.write_run(run1, 1, [assigned_fact])
    again = CandidateRelation(
        relation="ASSIGNED_TO",
        source=EntityRef(class_name="HardwareAsset", key=HW),
        target=EntityRef(class_name="Department", key=DEPT),
        evidence=[ev("__T_ second quote")],
    )
    graph.write_run(run2, 1, [to_fact(again, SPEC, 0.7)])
    graph.rollback_run(run2)
    records, _, _ = driver.execute_query(
        "MATCH (:HardwareAsset {asset_tag: $h})-[r:ASSIGNED_TO]->() "
        "RETURN r.run_id AS run, r.runs AS runs, size(r.evidence_ids) AS n",
        h=HW,
    )
    assert (records[0]["run"], records[0]["runs"], records[0]["n"]) == (run1, [run1], 1)
    graph.rollback_run(run1)


def test_one_to_many_replacement_clears_the_targets_other_sources(driver: Driver) -> None:
    from writer_service.ontology.spec import RelationSpec

    spec = OntologySpec(
        CORE_CLASSES,
        (*CORE_RELATIONS, RelationSpec("ZZ_LINK", "Department", "HardwareAsset", "one_to_many")),
    )
    graph, run = Neo4jGraph(driver), f"test-{uuid.uuid4()}"

    def link(src: str) -> CandidateRelation:
        return CandidateRelation(
            relation="ZZ_LINK",
            source=EntityRef(class_name="Department", key=src),
            target=EntityRef(class_name="HardwareAsset", key=HW),
        )

    graph.write_run(run, 1, [to_fact(link(DEPT), spec, 1.0)])
    replaced = graph.write_run(run, 1, [to_fact(link(OTHER), spec, 1.0, replace_existing=True)])
    records, _, _ = driver.execute_query(
        "MATCH (i:Department)-[:ZZ_LINK]->(:HardwareAsset {asset_tag: $h}) RETURN i.dept_code AS t",
        h=HW,
    )
    assert [r["t"] for r in records] == [OTHER]
    assert len(replaced) == 1 and replaced[0]["source"] == {"label": "Department", "key": DEPT}
    graph.rollback_run(run)


def test_rollback_of_asserting_run_demotes_to_stub_on_neo4j(driver: Driver) -> None:
    graph = Neo4jGraph(driver)
    run1, run2 = f"test-{uuid.uuid4()}", f"test-{uuid.uuid4()}"
    dept_fact, assigned_fact = facts()
    graph.write_run(run1, 1, [dept_fact])  # asserts the department
    graph.write_run(run2, 1, [assigned_fact])  # only references it
    graph.rollback_run(run1)
    records, _, _ = driver.execute_query(
        "MATCH (n:Department {dept_code: $d}) OPTIONAL MATCH (n)-[l:SUPPORTED_BY]->() "
        "RETURN n.stub AS stub, n.name AS name, n.runs AS runs, n.run_id AS run, count(l) AS links",
        d=DEPT,
    )
    r = records[0]
    assert (r["stub"], r["name"], r["runs"], r["run"], r["links"]) == (True, None, [run2], run2, 0)
    graph.rollback_run(run2)


def test_conflict_approval_end_to_end_on_neo4j(
    driver: Driver, tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Orchestrator + real Neo4j: replacing a full-entity edge must serialise to the audit log
    (Neo4j DateTime values) and swap the target (review round 2 blocker)."""
    from pathlib import Path

    from sqlalchemy import select

    from writer_service.governance.db import create_engine_for
    from writer_service.governance.migrate import upgrade
    from writer_service.governance.models import AuditLog, ReviewItem
    from writer_service.governance.uow import UnitOfWork
    from writer_service.llm.cache import InMemoryCache
    from writer_service.llm.fake import FakeLLM
    from writer_service.llm.structured import StructuredLLM
    from writer_service.ontology.neo4j_schema import InMemoryGraphSchema
    from writer_service.ontology.service import OntologyService
    from writer_service.pipeline.orchestrator import SourceBatch, SourceText, WriterPipeline
    from writer_service.pipeline.verification import BACK_TRANSLATION_PROMPT

    # Only ever touch test-prefixed runs on the shared demo Neo4j.
    monkeypatch.setattr(
        WriterPipeline, "approval_run_id", staticmethod(lambda i: f"test-approval-{i}")
    )
    url = f"sqlite:///{Path(str(tmp_path)) / 'g.sqlite'}"
    upgrade(url)
    engine = create_engine_for(url)
    OntologyService(engine, InMemoryGraphSchema()).bootstrap("t")
    graph = Neo4jGraph(driver)
    seed = f"test-{uuid.uuid4()}"
    other = CandidateEntity(
        class_name="Department",
        properties={"dept_code": OTHER, "name": "Other Dept"},
        evidence=[ev("__T_ other quote")],
    )
    old_edge = CandidateRelation(
        relation="ASSIGNED_TO",
        source=EntityRef(class_name="HardwareAsset", key=HW),
        target=EntityRef(class_name="Department", key=OTHER),
        evidence=[ev("__T_ old assigned quote")],
    )
    graph.write_run(seed, 1, [to_fact(other, SPEC, 0.9), to_fact(old_edge, SPEC, 0.9)])
    text = f"Asset {HW} is assigned to department {DEPT}."
    new_edge = CandidateRelation(
        relation="ASSIGNED_TO",
        source=EntityRef(class_name="HardwareAsset", key=HW),
        target=EntityRef(class_name="Department", key=DEPT),
        evidence=[Evidence(source_type="pdf", source_ref="t.pdf", locator="page 1", quote=text)],
    )
    fake = FakeLLM(
        {BACK_TRANSLATION_PROMPT.name: [{"agrees": True, "score": 1.0, "explanation": "ok"}]}
    )
    pipe = WriterPipeline(engine, graph, StructuredLLM(fake, InMemoryCache()))
    run = pipe.ingest(
        SourceBatch(
            "pdf", "t.pdf", [SourceText("t.pdf", "page 1", text)], extract=lambda s: [new_edge]
        ),
        actor="t",
    )
    assert run.counts["conflict"] == 1
    with UnitOfWork(engine) as uow:
        item_id = uow.session.scalars(select(ReviewItem)).one().id
    pipe.approve(item_id, reviewer="alice")
    records, _, _ = driver.execute_query(
        "MATCH (:HardwareAsset {asset_tag: $h})-[:ASSIGNED_TO]->(i:Department) "
        "RETURN i.dept_code AS t",
        h=HW,
    )
    assert [r["t"] for r in records] == [DEPT]
    with UnitOfWork(engine) as uow:
        entry = next(
            a for a in uow.session.scalars(select(AuditLog)) if a.action == "review.approve"
        )
        assert entry.detail["replaced"][0]["target"]["key"] == OTHER
    for r_id in (WriterPipeline.approval_run_id(item_id), run.run_id, seed):
        graph.rollback_run(r_id)
    engine.dispose()


def test_undo_restores_overwritten_properties_with_their_types(driver: Driver) -> None:
    """Review round 3: compensation must put back overwritten entity properties, and restoring
    removed edges must keep Neo4j temporal values (not their JSON-safe strings)."""
    from neo4j.time import DateTime

    graph = Neo4jGraph(driver)
    run1, run2 = f"test-{uuid.uuid4()}", f"test-{uuid.uuid4()}"
    dept_fact, assigned_fact = facts()
    graph.write_run(run1, 1, [dept_fact, assigned_fact])
    node_at = driver.execute_query(
        "MATCH (i:Department {dept_code: $d}) RETURN i.ingested_at AS at", d=DEPT
    ).records[0]["at"]
    renamed = CandidateEntity(
        class_name="Department",
        properties={"dept_code": DEPT, "name": "Renamed Dept"},
        evidence=[ev("__T_ renamed quote")],
    )
    spec = SPEC
    moved = CandidateRelation(
        relation="ASSIGNED_TO",
        source=EntityRef(class_name="HardwareAsset", key=HW),
        target=EntityRef(class_name="Department", key=OTHER),
        evidence=[ev("__T_ moved quote")],
    )
    changes = graph.write_run(
        run2, 1, [to_fact(renamed, spec, 0.7), to_fact(moved, spec, 0.9, replace_existing=True)]
    )
    kinds = sorted(c["kind"] for c in changes)
    assert kinds == ["edge", "properties"]
    graph.undo(changes)
    records, _, _ = driver.execute_query(
        "MATCH (i:Department {dept_code: $d}) "
        "OPTIONAL MATCH (:HardwareAsset {asset_tag: $h})-[r:ASSIGNED_TO]->(i) "
        "RETURN i.name AS name, i.run_id AS run, i._label AS label, r.ingested_at AS at, "
        "i.score AS score, i.ingested_at AS node_at",
        d=DEPT,
        h=HW,
    )
    r = records[0]
    assert (r["name"], r["run"], r["label"], r["score"]) == ("Test Dept", run1, "Department", 0.9)
    assert r["node_at"] == node_at  # entity timestamp restored too (review round 4)
    assert isinstance(r["at"], DateTime)  # restored edge keeps a temporal, not a string
    graph.rollback_run(run2)
    graph.rollback_run(run1)
