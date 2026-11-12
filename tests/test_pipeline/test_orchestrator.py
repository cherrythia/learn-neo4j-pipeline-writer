"""End-to-end writer pipeline, in memory: SQLite governance + InMemoryGraph + FakeLLM."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select

from writer_service.governance.db import create_engine_for
from writer_service.governance.migrate import upgrade
from writer_service.governance.models import AuditLog, Candidate, IngestionRun, ReviewItem
from writer_service.governance.uow import UnitOfWork
from writer_service.llm.cache import InMemoryCache
from writer_service.llm.fake import FakeLLM
from writer_service.llm.structured import StructuredLLM
from writer_service.ontology.core import CORE_CLASSES, CORE_RELATIONS
from writer_service.ontology.neo4j_schema import InMemoryGraphSchema
from writer_service.ontology.service import OntologyService
from writer_service.ontology.spec import OntologySpec
from writer_service.pipeline.candidates import (
    CandidateEntity,
    CandidateRelation,
    EntityRef,
    Evidence,
)
from writer_service.pipeline.graph_store import InMemoryGraph, to_fact
from writer_service.pipeline.orchestrator import SourceBatch, SourceText, WriterPipeline
from writer_service.pipeline.verification import BACK_TRANSLATION_PROMPT

PAGE1 = (
    "Quillstack Docs (Demo) (software id SW-QDOC) is billed as a subscription. "
    "Quillstack Docs (Demo) is covered by licence SL-25-0201 until 31 October 2026."
)
TEXTS = [SourceText("a.pdf", "page 1", PAGE1)]


def ev(quote: str) -> Evidence:
    return Evidence(source_type="pdf", source_ref="a.pdf", locator="page 1", quote=quote)


QDOC = CandidateEntity(
    class_name="Software",
    confidence=0.9,
    properties={"software_id": "SW-QDOC", "name": "Quillstack Docs (Demo)"},
    evidence=[ev("Quillstack Docs (Demo) (software id SW-QDOC)")],
)
LICENCE = CandidateEntity(
    class_name="SoftwareLicense",
    confidence=0.9,
    properties={"asset_tag": "SL-25-0201"},
    evidence=[ev("licence SL-25-0201")],
)
LICENSED = CandidateRelation(
    relation="LICENSES",
    confidence=0.9,
    source=EntityRef(class_name="SoftwareLicense", key="SL-25-0201"),
    target=EntityRef(class_name="Software", key="SW-QDOC"),
    evidence=[ev("Quillstack Docs (Demo) is covered by licence SL-25-0201")],
)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    url = f"sqlite:///{tmp_path / 'g.sqlite'}"
    upgrade(url)
    eng = create_engine_for(url)
    OntologyService(eng, InMemoryGraphSchema()).bootstrap("admin")
    yield eng
    eng.dispose()


@pytest.fixture
def graph() -> InMemoryGraph:
    return InMemoryGraph()


def judge(score: float = 0.95, agrees: bool = True) -> FakeLLM:
    return FakeLLM(
        {BACK_TRANSLATION_PROMPT.name: [{"agrees": agrees, "score": score, "explanation": "ok"}]}
    )


def pipeline(engine: Engine, graph: InMemoryGraph, fake: FakeLLM | None = None) -> WriterPipeline:
    return WriterPipeline(engine, graph, StructuredLLM(fake or judge(), InMemoryCache()))


def doc(*runs: list[Any]) -> SourceBatch:
    return SourceBatch("pdf", "a.pdf", TEXTS, extract=lambda s: runs[min(s, len(runs) - 1)])


def rows(engine: Engine, model: type) -> list[Any]:
    with UnitOfWork(engine) as uow:
        return list(uow.session.scalars(select(model)))


# ---------------------------------------------------------------- happy path


def test_grounded_stable_facts_are_committed_with_provenance(
    engine: Engine, graph: InMemoryGraph
) -> None:
    summary = pipeline(engine, graph).ingest(doc([QDOC, LICENCE, LICENSED]), actor="ingest-bot")
    assert summary.status == "completed"
    assert summary.counts["commit"] == 3 and summary.counts["candidates"] == 3
    qdoc = graph.node_properties("Software", "software_id", "SW-QDOC")
    assert qdoc is not None and qdoc["run_id"] == summary.run_id and qdoc["ontology_version"] == 1
    assert graph.node_properties("Asset", "asset_tag", "SL-25-0201") is not None  # taxonomy label
    assert graph.targets(
        "LICENSES", ("SoftwareLicense", "asset_tag", "SL-25-0201"), {"Software": "software_id"}
    ) == [{"Software": "SW-QDOC"}]
    assert len(graph.evidence) == 3
    (run,) = rows(engine, IngestionRun)
    assert run.status == "completed" and run.counts["commit"] == 3
    cands = rows(engine, Candidate)
    assert all(c.decision == "commit" and c.breakdown["components"] for c in cands)


def test_self_consistency_drops_facts_seen_once(engine: Engine, graph: InMemoryGraph) -> None:
    ghost = CandidateEntity(
        class_name="Software",
        properties={"software_id": "SW-GHO", "name": "Ghost"},
        evidence=[ev("Ghost")],
    )
    summary = pipeline(engine, graph).ingest(doc([QDOC, ghost], [QDOC], [QDOC]), actor="bot")
    assert summary.counts["extracted"] == 4 and summary.counts["candidates"] == 1
    assert graph.node_properties("Software", "software_id", "SW-GHO") is None


def test_back_translation_is_called_once_per_fact(engine: Engine, graph: InMemoryGraph) -> None:
    fake = judge()
    pipeline(engine, graph, fake).ingest(doc([QDOC, LICENSED]), actor="bot")
    assert len([c for c in fake.calls if c.prompt_name == BACK_TRANSLATION_PROMPT.name]) == 2


# ---------------------------------------------------------------- routing


def test_ungrounded_quote_goes_to_review_not_graph(engine: Engine, graph: InMemoryGraph) -> None:
    invented = QDOC.model_copy(update={"evidence": [ev("SW-QDOC has 9000 unused seats")]})
    summary = pipeline(engine, graph).ingest(doc([invented]), actor="bot")
    assert summary.counts["commit"] == 0
    assert graph.node_count() == 0
    (cand,) = rows(engine, Candidate)
    assert cand.decision in ("review", "reject")
    assert cand.breakdown["components"]["grounding"]["value"] <= 0.1


def test_review_decision_creates_review_item(engine: Engine, graph: InMemoryGraph) -> None:
    summary = pipeline(engine, graph, judge(0.5)).ingest(doc([QDOC]), actor="bot")
    assert summary.counts["review"] == 1
    (item,) = rows(engine, ReviewItem)
    assert item.reason == "score" and item.status == "open"


def test_schema_failure_is_rejected_with_reasons(engine: Engine, graph: InMemoryGraph) -> None:
    bad = CandidateEntity(class_name="Software", properties={"name": "No Software Id"}, evidence=[])
    summary = pipeline(engine, graph).ingest(doc([bad]), actor="bot")
    assert summary.counts["reject"] == 1
    (cand,) = rows(engine, Candidate)
    assert cand.decision == "reject" and cand.score is None
    assert any("required property 'software_id'" in f for f in cand.gate_failures)


def test_contradiction_with_graph_is_a_conflict_review_item(
    engine: Engine, graph: InMemoryGraph
) -> None:
    graph.add_edge(
        "LICENSES",
        ("SoftwareLicense", "asset_tag", "SL-25-0201"),
        ("Software", "software_id", "SW-LEDG"),
    )
    summary = pipeline(engine, graph).ingest(doc([LICENSED]), actor="bot")
    assert summary.counts["conflict"] == 1
    (item,) = rows(engine, ReviewItem)
    assert item.reason == "conflict" and "SW-LEDG" in item.conflict_with["message"]


def test_relation_endpoint_resolved_by_name(engine: Engine, graph: InMemoryGraph) -> None:
    pipe = pipeline(engine, graph)
    pipe.ingest(doc([QDOC]), actor="bot")
    by_name = LICENSED.model_copy(
        update={"target": EntityRef(class_name="Software", key="Quillstack Docs")}
    )
    pipe.ingest(SourceBatch("pdf", "a.pdf", TEXTS, extract=lambda s: [by_name]), actor="bot")
    assert graph.targets(
        "LICENSES", ("SoftwareLicense", "asset_tag", "SL-25-0201"), {"Software": "software_id"}
    ) == [{"Software": "SW-QDOC"}]
    rel = [c for c in rows(engine, Candidate) if c.kind == "relation"][0]
    assert any("resolved to 'SW-QDOC'" in n for n in rel.breakdown["notes"])


# ---------------------------------------------------------------- structured sources


def test_structured_candidates_skip_llm_and_have_full_stability(
    engine: Engine, graph: InMemoryGraph
) -> None:
    row = SourceText(
        "dataset:3", "row software_id=SW-QDOC", "software_id: SW-QDOC; name: Quillstack Docs (Demo)"
    )
    cand = CandidateEntity(
        class_name="Software",
        properties={"software_id": "SW-QDOC", "name": "Quillstack Docs (Demo)"},
        evidence=[
            Evidence(
                source_type="superset",
                source_ref="dataset:3",
                locator="row software_id=SW-QDOC",
                quote="software_id: SW-QDOC",
            )
        ],
    )
    fake = judge()
    summary = pipeline(engine, graph, fake).ingest(
        SourceBatch("superset", "dataset:3", [row], candidates=[cand]), actor="sync"
    )
    assert summary.counts["commit"] == 1 and fake.calls == []
    (c,) = rows(engine, Candidate)
    assert c.breakdown["components"]["stability"]["value"] == 1.0


# ---------------------------------------------------------------- idempotency + failures


def test_reingesting_the_same_document_creates_no_duplicates(
    engine: Engine, graph: InMemoryGraph
) -> None:
    pipe = pipeline(engine, graph)
    pipe.ingest(doc([QDOC, LICENCE, LICENSED]), actor="bot")
    nodes, edges, evidence = graph.node_count(), len(graph.edges), len(graph.evidence)
    pipe.ingest(doc([QDOC, LICENCE, LICENSED]), actor="bot")
    assert (graph.node_count(), len(graph.edges), len(graph.evidence)) == (nodes, edges, evidence)


def test_graph_failure_marks_run_failed_and_stores_nothing(
    engine: Engine, graph: InMemoryGraph
) -> None:
    graph.fail_next_write = True
    summary = pipeline(engine, graph).ingest(doc([QDOC]), actor="bot")
    assert summary.status == "failed" and "graph write failed" in (summary.error or "")
    (run,) = rows(engine, IngestionRun)
    assert run.status == "failed" and rows(engine, Candidate) == []
    assert graph.node_count() == 0


def test_governance_failure_after_graph_write_undoes_the_graph(
    engine: Engine, graph: InMemoryGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.orm import Session

    real_commit = Session.commit
    calls = {"n": 0}

    def flaky(self: Session) -> None:
        calls["n"] += 1
        if calls["n"] == 2:  # 1st commit opens the run; 2nd is the run's results
            raise RuntimeError("db down")
        real_commit(self)

    monkeypatch.setattr(Session, "commit", flaky)
    summary = pipeline(engine, graph).ingest(doc([QDOC]), actor="bot")
    monkeypatch.undo()
    assert summary.status == "failed"
    assert graph.node_count() == 0 and graph.evidence == {}


def test_no_published_ontology_is_an_error(tmp_path: Path, graph: InMemoryGraph) -> None:
    from writer_service.pipeline.orchestrator import PipelineError

    url = f"sqlite:///{tmp_path / 'empty.sqlite'}"
    upgrade(url)
    with pytest.raises(PipelineError, match="no published ontology"):
        pipeline(create_engine_for(url), graph).ingest(doc([QDOC]), actor="bot")


# ---------------------------------------------------------------- rollback + review decisions


def test_rollback_removes_what_the_run_created(engine: Engine, graph: InMemoryGraph) -> None:
    pipe = pipeline(engine, graph)
    summary = pipe.ingest(doc([QDOC, LICENCE, LICENSED]), actor="bot")
    removed = pipe.rollback(summary.run_id, actor="admin")
    assert removed["nodes"] >= 3 and removed["relationships"] == 1
    assert graph.node_count() == 0 and graph.edges == [] and graph.evidence == {}
    (run,) = rows(engine, IngestionRun)
    assert run.status == "rolled_back"
    assert ("admin", "run.rollback") in [(a.actor, a.action) for a in rows(engine, AuditLog)]


def test_approving_a_review_item_writes_it(engine: Engine, graph: InMemoryGraph) -> None:
    pipe = pipeline(engine, graph, judge(0.5))
    pipe.ingest(doc([QDOC]), actor="bot")
    (item,) = rows(engine, ReviewItem)
    pipe.approve(item.id, reviewer="rita")
    assert graph.node_properties("Software", "software_id", "SW-QDOC") is not None
    with UnitOfWork(engine) as uow:
        item = uow.session.scalars(select(ReviewItem)).one()
        assert item.status == "approved" and item.reviewer == "rita"
        assert item.candidate.decision == "commit"


def test_approving_a_conflict_replaces_the_old_edge(engine: Engine, graph: InMemoryGraph) -> None:
    graph.add_edge(
        "LICENSES",
        ("SoftwareLicense", "asset_tag", "SL-25-0201"),
        ("Software", "software_id", "SW-LEDG"),
    )
    graph.edge_meta.append({"created_run": "seed", "evidence_ids": [], "run_id": "seed"})
    pipe = pipeline(engine, graph)
    pipe.ingest(doc([LICENSED]), actor="bot")
    (item,) = rows(engine, ReviewItem)
    pipe.approve(item.id, reviewer="alice")
    assert graph.targets(
        "LICENSES", ("SoftwareLicense", "asset_tag", "SL-25-0201"), {"Software": "software_id"}
    ) == [{"Software": "SW-QDOC"}]


def test_edited_approval_is_gated_and_rejection_recorded(
    engine: Engine, graph: InMemoryGraph
) -> None:
    from writer_service.pipeline.orchestrator import PipelineError

    pipe = pipeline(engine, graph, judge(0.5))
    pipe.ingest(doc([QDOC]), actor="bot")
    (item,) = rows(engine, ReviewItem)
    bad = {**QDOC.model_dump(mode="json"), "properties": {"name": "no software id"}}
    with pytest.raises(PipelineError, match="schema gate"):
        pipe.approve(item.id, reviewer="rita", edited=bad)
    pipe.reject(item.id, reviewer="rita", note="not convincing")
    with UnitOfWork(engine) as uow:
        item = uow.session.scalars(select(ReviewItem)).one()
        assert item.status == "rejected" and item.candidate.decision == "reject"
    with pytest.raises(PipelineError, match="not open"):
        pipe.approve(item.id, reviewer="rita")


def test_near_quote_never_commits_even_from_a_reliable_source(
    engine: Engine, graph: InMemoryGraph
) -> None:
    """Superset reliability 0.95 + every other signal 1.0 + grounding 0.5 scores ~0.865,
    above the commit threshold; the pipeline must still send it to review (#8 review)."""
    row = SourceText(
        "dataset:3", "row software_id=SW-QDOC", "software_id: SW-QDOC; name: Quillstack Docs (Demo)"
    )
    near = CandidateEntity(
        class_name="Software",
        properties={"software_id": "SW-QDOC", "name": "Quillstack Docs (Demo)"},
        evidence=[
            Evidence(
                source_type="superset",
                source_ref="dataset:3",
                locator="row software_id=SW-QDOC",
                quote="software_id: SW-QDOC; name: Quillstack Doc",
            )
        ],
    )
    graph.add_node(
        "Software", "software_id", "SW-QDOC", {"software_id": "SW-QDOC"}
    )  # known entity: match 1.0
    graph.add("Software", "software_id", "SW-QDOC", name="Quillstack Docs (Demo)")
    summary = pipeline(engine, graph).ingest(
        SourceBatch("superset", "dataset:3", [row], candidates=[near]), actor="sync"
    )
    assert summary.counts["commit"] == 0 and summary.counts["review"] == 1
    (cand,) = rows(engine, Candidate)
    assert cand.score is not None and cand.score >= 0.85  # the score alone would have committed
    assert any("not verbatim" in r for r in cand.breakdown["reasons"])


# ---------------------------------------------------------------- review round 1 (PR #32)


def test_rollback_keeps_facts_another_run_also_owns(engine: Engine, graph: InMemoryGraph) -> None:
    pipe = pipeline(engine, graph)
    run1 = pipe.ingest(doc([LICENSED]), actor="bot").run_id  # creates SW-QDOC as a stub endpoint
    pipe.ingest(doc([QDOC]), actor="bot")  # a later run states the full entity
    pipe.rollback(run1, actor="admin")
    qdoc = graph.node_properties("Software", "software_id", "SW-QDOC")
    assert qdoc is not None and qdoc["name"] == "Quillstack Docs (Demo)"
    assert qdoc["runs"] == [qdoc["run_id"]] and qdoc["run_id"] != run1
    assert graph.edges == []  # run1's own edge is gone
    assert any(
        v["quote"].startswith("Quillstack Docs (Demo) (software id")
        for v in graph.evidence.values()
    )


def test_rollback_of_a_repeat_run_changes_nothing_shared(
    engine: Engine, graph: InMemoryGraph
) -> None:
    pipe = pipeline(engine, graph)
    first = pipe.ingest(doc([QDOC, LICENCE, LICENSED]), actor="bot").run_id
    second = pipe.ingest(doc([QDOC, LICENCE, LICENSED]), actor="bot").run_id
    before = (graph.node_count(), len(graph.edges), len(graph.evidence))
    pipe.rollback(second, actor="admin")
    assert (graph.node_count(), len(graph.edges), len(graph.evidence)) == before
    assert graph.edge_meta[0]["run_id"] == first  # provenance falls back to the remaining run


def test_rollback_prunes_relationship_evidence_of_the_run(
    engine: Engine, graph: InMemoryGraph
) -> None:
    pipe = pipeline(engine, graph)
    pipe.ingest(doc([LICENSED]), actor="bot")
    other_quote = LICENSED.model_copy(update={"evidence": [ev("licence SL-25-0201")]})
    run2 = pipe.ingest(doc([other_quote]), actor="bot").run_id
    assert len(graph.edge_meta[0]["evidence_ids"]) == 2
    pipe.rollback(run2, actor="admin")
    (meta,) = graph.edge_meta
    assert len(meta["evidence_ids"]) == 1 and all(e in graph.evidence for e in meta["evidence_ids"])


def test_approve_regates_against_a_changed_graph(engine: Engine, graph: InMemoryGraph) -> None:
    from writer_service.pipeline.orchestrator import PipelineError

    pipe = pipeline(engine, graph, judge(0.5))
    pipe.ingest(doc([LICENSED]), actor="bot")  # score → review item
    graph.add_edge(
        "LICENSES",
        ("SoftwareLicense", "asset_tag", "SL-25-0201"),
        ("Software", "software_id", "SW-LEDG"),
    )
    graph.edge_meta.append({"runs": ["seed"], "evidence_ids": [], "run_id": "seed"})
    (item,) = rows(engine, ReviewItem)
    with pytest.raises(PipelineError, match="now a conflict"):
        pipe.approve(item.id, reviewer="rita")
    (item,) = rows(engine, ReviewItem)
    assert item.reason == "conflict" and item.status == "open"
    assert graph.targets(
        "LICENSES", ("SoftwareLicense", "asset_tag", "SL-25-0201"), {"Software": "software_id"}
    ) == [{"Software": "SW-LEDG"}]


def test_rollback_closes_open_items_and_blocks_their_approval(
    engine: Engine, graph: InMemoryGraph
) -> None:
    from writer_service.pipeline.orchestrator import PipelineError

    pipe = pipeline(engine, graph, judge(0.5))
    run = pipe.ingest(doc([QDOC]), actor="bot").run_id
    (item,) = rows(engine, ReviewItem)
    assert pipe.rollback(run, actor="admin")["review_items_closed"] == 1
    (closed,) = rows(engine, ReviewItem)
    assert closed.status == "rejected" and closed.note == "run rolled back"
    with pytest.raises(PipelineError, match="not open"):
        pipe.approve(item.id, reviewer="rita")


def test_approved_facts_survive_a_rollback_of_their_run(
    engine: Engine, graph: InMemoryGraph
) -> None:
    pipe = pipeline(engine, graph, judge(0.5))
    run = pipe.ingest(doc([QDOC]), actor="bot").run_id
    (item,) = rows(engine, ReviewItem)
    pipe.approve(item.id, reviewer="rita")
    pipe.rollback(run, actor="admin")
    qdoc = graph.node_properties("Software", "software_id", "SW-QDOC")
    assert qdoc is not None and qdoc["runs"] == [WriterPipeline.approval_run_id(item.id)]


def test_approve_commit_failure_undoes_the_graph_write(
    engine: Engine, graph: InMemoryGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.orm import Session

    pipe = pipeline(engine, graph, judge(0.5))
    pipe.ingest(doc([QDOC]), actor="bot")
    (item,) = rows(engine, ReviewItem)

    def fail(self: Session) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(Session, "commit", fail)
    with pytest.raises(RuntimeError, match="db down"):
        pipe.approve(item.id, reviewer="rita")
    monkeypatch.undo()
    assert graph.node_count() == 0
    (item,) = rows(engine, ReviewItem)
    assert item.status == "open"


def test_conflict_approval_records_replaced_edges(engine: Engine, graph: InMemoryGraph) -> None:
    graph.add_edge(
        "LICENSES",
        ("SoftwareLicense", "asset_tag", "SL-25-0201"),
        ("Software", "software_id", "SW-LEDG"),
    )
    graph.edge_meta.append({"runs": ["seed"], "evidence_ids": [], "run_id": "seed"})
    pipe = pipeline(engine, graph)
    pipe.ingest(doc([LICENSED]), actor="bot")
    (item,) = rows(engine, ReviewItem)
    pipe.approve(item.id, reviewer="alice")
    entry = next(a for a in rows(engine, AuditLog) if a.action == "review.approve")
    (rep,) = entry.detail["replaced"]
    assert (rep["relation"], rep["source"]["key"], rep["target"]["key"]) == (
        "LICENSES",
        "SL-25-0201",
        "SW-LEDG",
    )


def test_failed_conflict_approval_restores_the_replaced_edge(
    engine: Engine, graph: InMemoryGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.orm import Session

    graph.add_edge(
        "LICENSES",
        ("SoftwareLicense", "asset_tag", "SL-25-0201"),
        ("Software", "software_id", "SW-LEDG"),
    )
    graph.edge_meta.append({"runs": ["seed"], "evidence_ids": [], "run_id": "seed"})
    pipe = pipeline(engine, graph)
    pipe.ingest(doc([LICENSED]), actor="bot")
    (item,) = rows(engine, ReviewItem)

    def fail(self: Session) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(Session, "commit", fail)
    with pytest.raises(RuntimeError, match="db down"):
        pipe.approve(item.id, reviewer="alice")
    monkeypatch.undo()
    assert graph.targets(
        "LICENSES", ("SoftwareLicense", "asset_tag", "SL-25-0201"), {"Software": "software_id"}
    ) == [{"Software": "SW-LEDG"}]


def test_failed_approval_restores_overwritten_entity_properties(
    engine: Engine, graph: InMemoryGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 3: compensation must undo property overwrites, not only edge removals."""
    from sqlalchemy.orm import Session

    old = QDOC.model_copy(
        update={"properties": {"software_id": "SW-QDOC", "name": "Quillstack Old Name"}}
    )
    graph.write_run("seed", 1, [to_fact(old, OntologySpec(CORE_CLASSES, CORE_RELATIONS), 0.9)])
    pipe = pipeline(engine, graph, judge(0.5))
    pipe.ingest(doc([QDOC]), actor="bot")
    (item,) = rows(engine, ReviewItem)

    def fail(self: Session) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(Session, "commit", fail)
    with pytest.raises(RuntimeError, match="db down"):
        pipe.approve(item.id, reviewer="rita")
    monkeypatch.undo()
    qdoc = graph.node_properties("Software", "software_id", "SW-QDOC")
    assert qdoc is not None
    assert (qdoc["name"], qdoc["run_id"], qdoc["runs"]) == ("Quillstack Old Name", "seed", ["seed"])


@pytest.mark.parametrize(
    ("seed_props", "expected_name"),
    [
        (
            {"software_id": "SW-QDOC", "name": "Quillstack Docs (Demo)"},
            "Quillstack Docs (Demo)",
        ),
        ({"software_id": "SW-QDOC"}, None),  # the failed run added `name`: it must go again
    ],
    ids=["overwritten-score", "added-property"],
)
def test_failed_ingest_restores_properties_it_overwrote(
    engine: Engine,
    graph: InMemoryGraph,
    monkeypatch: pytest.MonkeyPatch,
    seed_props: dict[str, str],
    expected_name: str | None,
) -> None:
    """Review rounds 4-5: a failed ingest must not leave its score or values on another run's
    entity. The seed agrees with the ingested fact, so the gates pass and the run really
    overwrites the entity before the governance commit fails."""
    from sqlalchemy.orm import Session

    seed = QDOC.model_copy(update={"properties": seed_props})
    graph.write_run("seed", 1, [to_fact(seed, OntologySpec(CORE_CLASSES, CORE_RELATIONS), 0.4)])
    writes: list[list[dict[str, Any]]] = []
    real_write = graph.write_run

    def recording_write(*a: Any, **k: Any) -> list[dict[str, Any]]:
        changes = real_write(*a, **k)
        writes.append(changes)
        return changes

    monkeypatch.setattr(graph, "write_run", recording_write)
    real_commit = Session.commit
    calls = {"n": 0}

    def flaky(self: Session) -> None:
        calls["n"] += 1
        if calls["n"] == 2:  # 1st commit opens the run; 2nd is the run's results
            raise RuntimeError("db down")
        real_commit(self)

    monkeypatch.setattr(Session, "commit", flaky)
    summary = pipeline(engine, graph).ingest(doc([QDOC]), actor="bot")
    monkeypatch.undo()
    assert summary.status == "failed"
    (changes,) = writes
    assert any(c["kind"] == "properties" for c in changes)  # the run did overwrite the entity
    qdoc = graph.node_properties("Software", "software_id", "SW-QDOC")
    assert qdoc is not None
    assert (qdoc.get("name"), qdoc["score"], qdoc["run_id"], qdoc["runs"]) == (
        expected_name,
        0.4,
        "seed",
        ["seed"],
    )


# ---------------------------------------------------------------- possible duplicates (#38)

DUP_ROW = SourceText(
    "dataset:3", "row 1 (software_id=SW-QDX)", "software_id: SW-QDX; name: Quillstack Docs"
)


def sev(quote: str) -> Evidence:
    return Evidence(
        source_type="superset",
        source_ref="dataset:3",
        locator="row 1 (software_id=SW-QDX)",
        quote=quote,
    )


QDX = CandidateEntity(
    class_name="Software",
    properties={"software_id": "SW-QDX", "name": "Quillstack Docs"},
    evidence=[sev("name: Quillstack Docs")],
)
QDX_LICENSED = CandidateRelation(
    relation="LICENSES",
    source=EntityRef(class_name="SoftwareLicense", key="SL-25-0201"),
    target=EntityRef(class_name="Software", key="SW-QDX"),
    evidence=[sev("software_id: SW-QDX")],
)


def superset(*cands: Any) -> SourceBatch:
    return SourceBatch("superset", "dataset:3", [DUP_ROW], candidates=list(cands))


def dup_items(engine: Engine) -> list[dict[str, Any]]:
    """Duplicate review items as plain dicts (read inside the session)."""
    with UnitOfWork(engine) as uow:
        return [
            {
                "id": i.id,
                "status": i.status,
                "conflict_with": i.conflict_with,
                "kind": i.candidate.kind,
                "reasons": i.candidate.breakdown["reasons"],
            }
            for i in uow.session.scalars(select(ReviewItem))
            if i.reason == "duplicate"
        ]


def test_possible_duplicate_never_auto_commits(engine: Engine, graph: InMemoryGraph) -> None:
    """A structured row scores high enough to commit; a near-identical name under a new key
    must still go to a human, with the entity it may duplicate."""
    pipe = pipeline(engine, graph)
    pipe.ingest(doc([QDOC]), actor="bot")
    summary = pipe.ingest(superset(QDX), actor="sync")
    assert summary.counts["commit"] == 0 and summary.counts["review"] == 1
    assert graph.node_properties("Software", "software_id", "SW-QDX") is None
    (item,) = dup_items(engine)
    dup = item["conflict_with"]["duplicate_of"]
    assert (
        dup["class_name"] == "Software"
        and dup["key"] == "SW-QDOC"
        and dup["candidate_key"] == "SW-QDX"
    )
    assert dup["key_property"] == "software_id" and dup["similarity"] >= 0.9
    assert "possible duplicate of Software 'SW-QDOC'" in item["conflict_with"]["message"]
    assert any("possible duplicate" in r for r in item["reasons"])


def test_relations_to_a_possible_duplicate_wait_for_review(
    engine: Engine, graph: InMemoryGraph
) -> None:
    """Writing the relation would create the duplicate node as a stub."""
    pipe = pipeline(engine, graph)
    pipe.ingest(doc([QDOC, LICENCE]), actor="bot")
    summary = pipe.ingest(superset(QDX, QDX_LICENSED), actor="sync")
    assert summary.counts["commit"] == 0 and summary.counts["review"] == 2
    assert graph.node_properties("Software", "software_id", "SW-QDX") is None
    rel = next(i for i in dup_items(engine) if i["kind"] == "relation")
    assert rel["conflict_with"]["duplicate_of"]["side"] == "target"
    assert rel["conflict_with"]["duplicate_of"]["key"] == "SW-QDOC"


def test_approving_a_possible_duplicate_as_new_creates_it(
    engine: Engine, graph: InMemoryGraph
) -> None:
    pipe = pipeline(engine, graph)
    pipe.ingest(doc([QDOC]), actor="bot")
    pipe.ingest(superset(QDX), actor="sync")
    (item,) = dup_items(engine)
    pipe.approve(item["id"], reviewer="rita")
    assert graph.node_properties("Software", "software_id", "SW-QDX") is not None
    assert graph.node_properties("Software", "software_id", "SW-QDOC") is not None


def test_merging_a_possible_duplicate_writes_into_the_existing_entity(
    engine: Engine, graph: InMemoryGraph
) -> None:
    pipe = pipeline(engine, graph)
    pipe.ingest(doc([QDOC, LICENCE]), actor="bot")
    pipe.ingest(superset(QDX, QDX_LICENSED), actor="sync")
    for item in dup_items(engine):
        pipe.approve(item["id"], reviewer="rita", merge=True)
    assert graph.node_properties("Software", "software_id", "SW-QDX") is None
    qdoc = graph.node_properties("Software", "software_id", "SW-QDOC")
    assert qdoc is not None and qdoc["name"] == "Quillstack Docs (Demo)"  # existing kept
    assert graph.targets(
        "LICENSES", ("SoftwareLicense", "asset_tag", "SL-25-0201"), {"Software": "software_id"}
    ) == [{"Software": "SW-QDOC"}]
    with UnitOfWork(engine) as uow:
        items = [i for i in uow.session.scalars(select(ReviewItem)) if i.reason == "duplicate"]
        assert {i.status for i in items} == {"edited"}
        entity = next(i for i in items if i.candidate.kind == "entity")
        assert entity.candidate.payload["properties"]["software_id"] == "SW-QDOC"
        relation = next(i for i in items if i.candidate.kind == "relation")
        assert relation.candidate.payload["target"]["key"] == "SW-QDOC"
    merged = [a for a in rows(engine, AuditLog) if a.action == "review.approve"]
    assert all(a.detail["merged_into"] == "SW-QDOC" for a in merged)
    entity_audit = next(a for a in merged if a.detail["kept_existing"])
    assert entity_audit.detail["kept_existing"] == {"name": "Quillstack Docs"}


def test_merge_is_only_for_duplicates(engine: Engine, graph: InMemoryGraph) -> None:
    from writer_service.pipeline.orchestrator import PipelineError

    pipe = pipeline(engine, graph, judge(0.5))
    pipe.ingest(doc([QDOC]), actor="bot")
    (item,) = rows(engine, ReviewItem)
    assert item.reason == "score"
    with pytest.raises(PipelineError, match="only a possible duplicate can be merged"):
        pipe.approve(item.id, reviewer="rita", merge=True)
    assert rows(engine, ReviewItem)[0].status == "open"


def test_exact_and_unrelated_entities_are_not_flagged(engine: Engine, graph: InMemoryGraph) -> None:
    pipe = pipeline(engine, graph)
    pipe.ingest(doc([QDOC]), actor="bot")
    other = CandidateEntity(
        class_name="Software",
        properties={"software_id": "SW-QDX", "name": "Zebra Mapping"},
        evidence=[sev("software_id: SW-QDX")],
    )
    summary = pipe.ingest(
        SourceBatch(
            "superset",
            "dataset:3",
            [
                SourceText(
                    "dataset:3",
                    "row 1 (software_id=SW-QDX)",
                    "software_id: SW-QDX; name: Zebra Mapping",
                )
            ],
            candidates=[other],
        ),
        actor="sync",
    )
    assert summary.counts["commit"] == 1 and dup_items(engine) == []


# review #39 round 1


def test_a_conflicting_relation_to_a_duplicate_can_be_merged(
    engine: Engine, graph: InMemoryGraph
) -> None:
    """Finding 1: the graph has LICENSES SW-QDOC; LICENSES SW-QDX conflicts, and must still carry
    duplicate_of so the reviewer can merge instead of replacing the edge with a new SW-QDX."""
    pipe = pipeline(engine, graph)
    pipe.ingest(doc([QDOC, LICENCE, LICENSED]), actor="bot")
    pipe.ingest(superset(QDX, QDX_LICENSED), actor="sync")
    rel = next(i for i in rows(engine, ReviewItem) if i.reason == "conflict")
    assert rel.conflict_with["duplicate_of"]["key"] == "SW-QDOC"
    (ent,) = dup_items(engine)
    pipe.approve(ent["id"], reviewer="rita", merge=True)
    pipe.approve(rel.id, reviewer="rita", merge=True)
    assert graph.node_properties("Software", "software_id", "SW-QDX") is None
    assert graph.targets(
        "LICENSES", ("SoftwareLicense", "asset_tag", "SL-25-0201"), {"Software": "software_id"}
    ) == [{"Software": "SW-QDOC"}]


def test_a_relation_kept_as_new_needs_its_entity_accepted_first(
    engine: Engine, graph: InMemoryGraph
) -> None:
    """Finding 2: keeping LICENSES -> SW-QDX while SW-QDX was merged away (or rejected) would
    create SW-QDX as a stub."""
    from writer_service.pipeline.orchestrator import PipelineError

    pipe = pipeline(engine, graph)
    pipe.ingest(doc([QDOC, LICENCE]), actor="bot")
    pipe.ingest(superset(QDX, QDX_LICENSED), actor="sync")
    ent = next(i for i in dup_items(engine) if i["kind"] == "entity")
    rel = next(i for i in dup_items(engine) if i["kind"] == "relation")
    pipe.reject(ent["id"], reviewer="rita")
    with pytest.raises(PipelineError, match="approve its entity item first"):
        pipe.approve(rel["id"], reviewer="rita")
    assert graph.node_properties("Software", "software_id", "SW-QDX") is None
    pipe.approve(rel["id"], reviewer="rita", merge=True)  # merging is still possible
    assert graph.node_properties("Software", "software_id", "SW-QDX") is None


def test_keeping_entity_and_relation_as_new_in_order_works(
    engine: Engine, graph: InMemoryGraph
) -> None:
    pipe = pipeline(engine, graph)
    pipe.ingest(doc([QDOC, LICENCE]), actor="bot")
    pipe.ingest(superset(QDX_LICENSED, QDX), actor="sync")  # relation listed before its entity
    items = dup_items(engine)
    assert {i["kind"] for i in items} == {"entity", "relation"}
    for kind in ("entity", "relation"):
        pipe.approve(next(i["id"] for i in items if i["kind"] == kind), reviewer="rita")
    qdx = graph.node_properties("Software", "software_id", "SW-QDX")
    assert qdx is not None and not qdx["stub"]


def test_merge_target_must_be_an_accepted_entity(engine: Engine, graph: InMemoryGraph) -> None:
    """Finding 3: the matcher also compares against new entities of the same batch; merging
    into one that was never accepted must not write it."""
    from writer_service.pipeline.orchestrator import PipelineError

    first = CandidateEntity(
        class_name="Software",
        confidence=0.9,
        properties={"software_id": "SW-QDOC", "name": "Quillstack Docs (Demo)"},
        evidence=[ev("Quillstack Docs (Demo) (software id SW-QDOC)")],
    )
    twin = first.model_copy(
        update={"properties": {"software_id": "SW-QDX2", "name": "Quillstack Docs (Demo)"}}
    )
    pipe = pipeline(engine, graph, judge(0.5))  # both land in review
    pipe.ingest(doc([first, twin]), actor="bot")
    score = next(i for i in rows(engine, ReviewItem) if i.reason == "score")
    (dup,) = dup_items(engine)
    assert dup["conflict_with"]["duplicate_of"]["key"] == "SW-QDOC"
    pipe.reject(score.id, reviewer="rita")
    with pytest.raises(PipelineError, match="not in the graph"):
        pipe.approve(dup["id"], reviewer="rita", merge=True)
    assert graph.node_count() == 0


def test_merge_that_contradicts_the_graph_stays_open_unchanged(
    engine: Engine, graph: InMemoryGraph
) -> None:
    """Finding 5: SL-25-0201 already licenses SW-LEDG; merging LICENSES -> SW-QDX into
    SW-QDOC contradicts that. Nothing is written and the item keeps its reason and original
    fact."""
    from writer_service.pipeline.orchestrator import PipelineError

    pipe = pipeline(engine, graph)
    pipe.ingest(doc([QDOC]), actor="bot")
    graph.add_edge(
        "LICENSES",
        ("SoftwareLicense", "asset_tag", "SL-25-0201"),
        ("Software", "software_id", "SW-LEDG"),
    )
    pipe.ingest(superset(QDX_LICENSED, QDX), actor="sync")
    rel = next(i for i in rows(engine, ReviewItem) if i.candidate_id and i.reason == "conflict")
    with pytest.raises(PipelineError, match="contradicts the graph"):
        pipe.approve(rel.id, reviewer="rita", merge=True)
    with UnitOfWork(engine) as uow:
        item = uow.session.get(ReviewItem, rel.id)
        assert item is not None and item.status == "open" and item.reason == "conflict"
        assert item.candidate.payload["target"]["key"] == "SW-QDX"
        assert "SW-LEDG" in item.conflict_with["merge_conflict"]


def test_merge_with_an_edit_and_case_insensitive_endpoints(
    engine: Engine, graph: InMemoryGraph
) -> None:
    pipe = pipeline(engine, graph)
    pipe.ingest(doc([QDOC, LICENCE]), actor="bot")
    lower = QDX_LICENSED.model_copy(
        update={"target": EntityRef(class_name="Software", key="sw-qdx")}
    )  # same new entity, other case
    pipe.ingest(superset(QDX, lower), actor="sync")
    items = dup_items(engine)
    rel = next(i for i in items if i["kind"] == "relation")
    assert rel["conflict_with"]["duplicate_of"]["key"] == "SW-QDOC"
    ent = next(i for i in items if i["kind"] == "entity")
    edited = QDX.model_copy(
        update={"properties": {**QDX.properties, "publisher": "Quillstack Labs"}}
    ).model_dump(mode="json")
    pipe.approve(ent["id"], reviewer="rita", edited=edited, merge=True)
    qdoc = graph.node_properties("Software", "software_id", "SW-QDOC")
    assert qdoc is not None and qdoc["publisher"] == "Quillstack Labs"
    assert qdoc["name"] == "Quillstack Docs (Demo)"
    assert graph.node_properties("Software", "software_id", "SW-QDX") is None


def test_rollback_closes_open_duplicate_items(engine: Engine, graph: InMemoryGraph) -> None:
    pipe = pipeline(engine, graph)
    pipe.ingest(doc([QDOC, LICENCE]), actor="bot")
    summary = pipe.ingest(superset(QDX, QDX_LICENSED), actor="sync")
    assert pipe.rollback(summary.run_id, actor="admin")["review_items_closed"] == 2
    assert {i["status"] for i in dup_items(engine)} == {"rejected"}


def test_both_flagged_endpoints_are_recorded() -> None:
    """Finding 6: a relation whose two endpoints are both possible duplicates."""
    from writer_service.pipeline.orchestrator import _duplicate_of

    dups = {
        ("Software", "sw-qdx"): {
            "class_name": "Software",
            "key": "SW-QDOC",
            "candidate_key": "SW-QDX",
        },
        ("Software", "sw-ledx"): {
            "class_name": "Software",
            "key": "SW-LEDG",
            "candidate_key": "SW-LEDX",
        },
    }
    rel = CandidateRelation(
        relation="PARTNERS_WITH",
        source=EntityRef(class_name="Software", key="SW-QDX"),
        target=EntityRef(class_name="Software", key="SW-LEDX"),
    )
    dup = _duplicate_of(rel, dups)
    assert dup is not None and dup["side"] == "source"
    assert [(s["side"], s["key"]) for s in dup["sides"]] == [
        ("source", "SW-QDOC"),
        ("target", "SW-LEDG"),
    ]


# review #39 round 2


def test_a_merge_sticks_when_the_source_is_ingested_again(
    engine: Engine, graph: InMemoryGraph
) -> None:
    """Finding 1: re-syncing the same dataset must not re-open the duplicate."""
    pipe = pipeline(engine, graph)
    pipe.ingest(doc([QDOC, LICENCE]), actor="bot")
    pipe.ingest(superset(QDX, QDX_LICENSED), actor="sync")
    for item in dup_items(engine):
        pipe.approve(item["id"], reviewer="rita", merge=True)
    again = pipe.ingest(superset(QDX, QDX_LICENSED), actor="sync")
    assert again.counts["commit"] == 2 and again.counts["review"] == 0, again.counts
    assert again.counts["conflict"] == 0
    assert graph.node_properties("Software", "software_id", "SW-QDX") is None
    qdoc = graph.node_properties("Software", "software_id", "SW-QDOC")
    assert qdoc is not None and qdoc["name"] == "Quillstack Docs (Demo)"
    with UnitOfWork(engine) as uow:
        latest = list(
            uow.session.scalars(select(Candidate).where(Candidate.run_id == again.run_id))
        )
        assert any("was merged into 'SW-QDOC'" in n for c in latest for n in c.breakdown["notes"])


def test_keep_as_new_writes_the_stored_key(engine: Engine, graph: InMemoryGraph) -> None:
    """Finding 2: Neo4j keys are case-sensitive; the relation must use the stored key."""
    pipe = pipeline(engine, graph)
    pipe.ingest(doc([QDOC, LICENCE]), actor="bot")
    lower = QDX_LICENSED.model_copy(
        update={"target": EntityRef(class_name="Software", key="sw-qdx")}
    )
    pipe.ingest(superset(lower, QDX), actor="sync")
    items = dup_items(engine)
    for kind in ("entity", "relation"):
        pipe.approve(next(i["id"] for i in items if i["kind"] == kind), reviewer="rita")
    with UnitOfWork(engine) as uow:
        rel = next(
            i for i in uow.session.scalars(select(ReviewItem)) if i.candidate.kind == "relation"
        )
        assert rel.candidate.payload["target"]["key"] == "SW-QDX"


def test_merging_a_relation_after_keeping_its_entity_is_refused(
    engine: Engine, graph: InMemoryGraph
) -> None:
    """Finding 3: that would split the entity (SW-QDX accepted, its licence pointing at SW-QDOC)."""
    from writer_service.pipeline.orchestrator import PipelineError

    pipe = pipeline(engine, graph)
    pipe.ingest(doc([QDOC, LICENCE]), actor="bot")
    pipe.ingest(superset(QDX, QDX_LICENSED), actor="sync")
    items = dup_items(engine)
    pipe.approve(next(i["id"] for i in items if i["kind"] == "entity"), reviewer="rita")
    rel = next(i["id"] for i in items if i["kind"] == "relation")
    with pytest.raises(PipelineError, match="was kept as a new entity"):
        pipe.approve(rel, reviewer="rita", merge=True)
    pipe.approve(rel, reviewer="rita")  # keeping it as new is fine
    assert graph.targets(
        "LICENSES", ("SoftwareLicense", "asset_tag", "SL-25-0201"), {"Software": "software_id"}
    ) == [{"Software": "SW-QDX"}]


def test_merge_rewrites_both_flagged_endpoints(engine: Engine, graph: InMemoryGraph) -> None:
    """Finding 5: every flagged side is rewritten, and each target must exist."""
    from writer_service.ontology.service import spec_of
    from writer_service.pipeline.orchestrator import PipelineError

    pipe = pipeline(engine, graph)
    with UnitOfWork(engine) as uow:
        published = uow.ontology_versions.published()
        assert published is not None
        spec = spec_of(published)
    rel = CandidateRelation(
        relation="LICENSES",
        source=EntityRef(class_name="SoftwareLicense", key="SL-NEW"),
        target=EntityRef(class_name="Software", key="SW-QDX"),
    )
    dup = {
        "sides": [
            {
                "side": "source",
                "class_name": "SoftwareLicense",
                "candidate_key": "SL-NEW",
                "key": "SL-25-0201",
            },
            {
                "side": "target",
                "class_name": "Software",
                "candidate_key": "SW-QDX",
                "key": "SW-QDOC",
            },
        ]
    }
    with pytest.raises(PipelineError, match="not in the graph"):
        pipe._merged(rel, dup, spec)
    pipe.ingest(doc([QDOC, LICENCE]), actor="bot")
    merged, _, sides = pipe._merged(rel, dup, spec)
    assert [s["side"] for s in sides] == ["source", "target"]
    assert isinstance(merged, CandidateRelation)
    assert (merged.source.key, merged.target.key) == ("SL-25-0201", "SW-QDOC")


# review #39 round 3


def _merge_all(pipe: WriterPipeline, engine: Engine) -> None:
    for item in dup_items(engine):
        if item["status"] == "open":
            pipe.approve(item["id"], reviewer="rita", merge=True)


def test_a_reused_key_is_not_rewritten_onto_the_merge_target(
    engine: Engine, graph: InMemoryGraph
) -> None:
    """Finding 1: after SW-QDX was merged into SW-QDOC, a row SW-QDX "Zebra Mapping Suite" is
    not the same entity: it goes to review (with the earlier merge noted), nothing lands on
    SW-QDOC."""
    pipe = pipeline(engine, graph)
    pipe.ingest(doc([QDOC, LICENCE]), actor="bot")
    pipe.ingest(superset(QDX, QDX_LICENSED), actor="sync")
    _merge_all(pipe, engine)
    zebra = QDX.model_copy(
        update={
            "properties": {
                "software_id": "SW-QDX",
                "name": "Zebra Mapping Suite",
                "publisher": "Zebra Cartography",
            }
        }
    )
    again = pipe.ingest(superset(zebra, QDX_LICENSED), actor="sync")
    assert again.counts["commit"] == 0, again.counts
    qdoc = graph.node_properties("Software", "software_id", "SW-QDOC")
    assert qdoc is not None and "publisher" not in qdoc
    open_items = [i for i in dup_items(engine) if i["status"] == "open"]
    ent = next(i for i in open_items if i["kind"] == "entity")
    assert ent["conflict_with"]["duplicate_of"]["previously_merged"]
    assert any("doesn't match it" in r for r in ent["reasons"] + [ent["conflict_with"]["message"]])
    # the reviewer decides SW-QDX is now a different product: allowed for this item
    pipe.approve(ent["id"], reviewer="rita")
    assert graph.node_properties("Software", "software_id", "SW-QDX") is not None


def test_keep_as_new_after_the_relation_was_merged_is_refused(
    engine: Engine, graph: InMemoryGraph
) -> None:
    """Finding 2: relation merged first, then the entity kept as new would split SW-QDX."""
    from writer_service.pipeline.orchestrator import PipelineError

    pipe = pipeline(engine, graph)
    pipe.ingest(doc([QDOC, LICENCE]), actor="bot")
    pipe.ingest(superset(QDX, QDX_LICENSED), actor="sync")
    items = dup_items(engine)
    pipe.approve(next(i["id"] for i in items if i["kind"] == "relation"), "rita", merge=True)
    ent = next(i["id"] for i in items if i["kind"] == "entity")
    with pytest.raises(PipelineError, match="was merged into 'SW-QDOC'"):
        pipe.approve(ent, reviewer="rita")
    assert graph.node_properties("Software", "software_id", "SW-QDX") is None
    pipe.approve(ent, reviewer="rita", merge=True)


def test_two_open_items_for_one_key_cannot_disagree(engine: Engine, graph: InMemoryGraph) -> None:
    """Finding 3: two syncs before review open two duplicate items for SW-QDX."""
    from writer_service.pipeline.orchestrator import PipelineError

    pipe = pipeline(engine, graph)
    pipe.ingest(doc([QDOC]), actor="bot")
    pipe.ingest(superset(QDX), actor="sync")
    pipe.ingest(superset(QDX), actor="sync")
    first, second = dup_items(engine)
    pipe.approve(first["id"], reviewer="rita", merge=True)
    with pytest.raises(PipelineError, match="was merged into 'SW-QDOC'"):
        pipe.approve(second["id"], reviewer="rita")
    assert graph.node_properties("Software", "software_id", "SW-QDX") is None


def test_merge_skips_a_side_the_reviewer_edited(engine: Engine, graph: InMemoryGraph) -> None:
    """Finding 4: edit the target elsewhere, then merge: nothing left to merge."""
    from writer_service.pipeline.orchestrator import PipelineError

    pipe = pipeline(engine, graph)
    pipe.ingest(doc([QDOC, LICENCE]), actor="bot")
    pipe.ingest(superset(QDX, QDX_LICENSED), actor="sync")
    rel = next(i for i in dup_items(engine) if i["kind"] == "relation")
    edited = QDX_LICENSED.model_copy(
        update={"target": EntityRef(class_name="Software", key="SW-QDOC")}
    ).model_dump(mode="json")
    with pytest.raises(PipelineError, match="nothing to merge"):
        pipe.approve(rel["id"], reviewer="rita", edited=edited, merge=True)


# review #39 round 4

ZEBRA = QDX.model_copy(
    update={
        "properties": {
            "software_id": "SW-QDX",
            "name": "Zebra Mapping Suite",
            "publisher": "Zebra Cartography",
        }
    }
)
NEW_LICENCE = CandidateRelation(
    relation="LICENSES",
    source=EntityRef(class_name="SoftwareLicense", key="SL-26-0301"),
    target=EntityRef(class_name="Software", key="SW-QDX"),
    evidence=[sev("software_id: SW-QDX")],
)


def _merged_qdx(engine: Engine, graph: InMemoryGraph) -> WriterPipeline:
    pipe = pipeline(engine, graph)
    pipe.ingest(doc([QDOC, LICENCE]), actor="bot")
    pipe.ingest(superset(QDX, QDX_LICENSED), actor="sync")
    _merge_all(pipe, engine)
    return pipe


def test_a_reused_key_stays_blocked_for_relations_in_a_later_run(
    engine: Engine, graph: InMemoryGraph
) -> None:
    """Finding 1: software and licences are separate datasets, so separate runs."""
    pipe = _merged_qdx(engine, graph)
    assert pipe.ingest(superset(ZEBRA), actor="sync").counts["commit"] == 0
    licences = pipe.ingest(superset(NEW_LICENCE), actor="sync")
    assert licences.counts["commit"] == 0 and licences.counts["review"] == 1, licences.counts
    assert (
        graph.targets(
            "LICENSES", ("SoftwareLicense", "asset_tag", "SL-26-0301"), {"Software": "software_id"}
        )
        == []
    )
    # the reviewer keeps Zebra as a new SW-QDX; then the licence can be kept for it
    open_items = [i for i in dup_items(engine) if i["status"] == "open"]
    ent = next(i for i in open_items if i["kind"] == "entity")
    rel = next(i for i in open_items if i["kind"] == "relation")
    pipe.approve(ent["id"], reviewer="rita")
    pipe.approve(rel["id"], reviewer="rita")
    assert graph.targets(
        "LICENSES", ("SoftwareLicense", "asset_tag", "SL-26-0301"), {"Software": "software_id"}
    ) == [{"Software": "SW-QDX"}]


def test_a_newer_merge_revokes_the_keep_as_new_exemption(
    engine: Engine, graph: InMemoryGraph
) -> None:
    """Finding 2: merge the reused key's relation into SW-QDOC, then keep the entity: a split."""
    from writer_service.pipeline.orchestrator import PipelineError

    pipe = _merged_qdx(engine, graph)
    pipe.ingest(superset(ZEBRA, NEW_LICENCE), actor="sync")
    open_items = [i for i in dup_items(engine) if i["status"] == "open"]
    rel = next(i for i in open_items if i["kind"] == "relation")
    ent = next(i for i in open_items if i["kind"] == "entity")
    pipe.approve(rel["id"], reviewer="rita", merge=True)
    with pytest.raises(PipelineError, match="'SW-QDX' was merged into 'SW-QDOC'"):
        pipe.approve(ent["id"], reviewer="rita")
    assert graph.node_properties("Software", "software_id", "SW-QDX") is None


def test_the_earlier_merge_wins_over_another_name_match(
    engine: Engine, graph: InMemoryGraph
) -> None:
    """Finding 3: an accepted SW-ZMS also matches the Zebra name; the item must still say
    'previously merged into SW-QDOC', so keeping SW-QDX as new stays possible."""
    pipe = _merged_qdx(engine, graph)
    zms = ZEBRA.model_copy(
        update={"properties": {"software_id": "SW-ZMS", "name": "Zebra Mapping Suite"}}
    )
    pipe.ingest(superset(zms), actor="sync")
    pipe.ingest(superset(ZEBRA), actor="sync")
    ent = next(i for i in dup_items(engine) if i["status"] == "open" and i["kind"] == "entity")
    assert ent["conflict_with"]["duplicate_of"]["key"] == "SW-QDOC"
    assert ent["conflict_with"]["duplicate_of"]["previously_merged"]
    pipe.approve(ent["id"], reviewer="rita")
    assert graph.node_properties("Software", "software_id", "SW-QDX") is not None


# review #39 round 5


def test_a_relation_alone_is_never_rewritten_through_an_alias(
    engine: Engine, graph: InMemoryGraph
) -> None:
    """Finding 1 (a): a licences sync arrives before the software sync; nothing in it says SW-QDX
    is still the merged entity, so it goes to review (pointing at SW-QDOC) instead of committing
    onto SW-QDOC unseen."""
    pipe = _merged_qdx(engine, graph)
    run = pipe.ingest(superset(NEW_LICENCE), actor="sync")
    assert run.counts["commit"] == 0 and run.counts["review"] == 1, run.counts
    assert (
        graph.targets(
            "LICENSES", ("SoftwareLicense", "asset_tag", "SL-26-0301"), {"Software": "software_id"}
        )
        == []
    )
    rel = next(i for i in dup_items(engine) if i["status"] == "open")
    dup = rel["conflict_with"]["duplicate_of"]
    assert dup["key"] == "SW-QDOC" and dup["merged_earlier"]
    assert "nothing in this batch confirms" in rel["conflict_with"]["message"]
    pipe.approve(rel["id"], reviewer="rita", merge=True)  # the reviewer confirms
    assert graph.targets(
        "LICENSES", ("SoftwareLicense", "asset_tag", "SL-26-0301"), {"Software": "software_id"}
    ) == [{"Software": "SW-QDOC"}]


def test_after_rejecting_a_reused_key_its_relations_still_wait(
    engine: Engine, graph: InMemoryGraph
) -> None:
    """Finding 1 (b): rejecting the Zebra item must not let later licences commit onto SW-QDOC."""
    pipe = _merged_qdx(engine, graph)
    pipe.ingest(superset(ZEBRA), actor="sync")
    ent = next(i for i in dup_items(engine) if i["status"] == "open")
    pipe.reject(ent["id"], reviewer="rita")
    run = pipe.ingest(superset(NEW_LICENCE), actor="sync")
    assert run.counts["commit"] == 0 and run.counts["review"] == 1, run.counts


def test_an_entity_kept_as_new_cannot_then_be_merged(engine: Engine, graph: InMemoryGraph) -> None:
    """Finding 2: two open items for SW-QDX; keep A as new, then merging B would put SW-QDX's
    values on SW-QDOC while SW-QDX also exists."""
    from writer_service.pipeline.orchestrator import PipelineError

    pipe = pipeline(engine, graph)
    pipe.ingest(doc([QDOC]), actor="bot")
    pipe.ingest(superset(QDX), actor="sync")
    pipe.ingest(superset(QDX), actor="sync")
    first, second = dup_items(engine)
    pipe.approve(first["id"], reviewer="rita")
    with pytest.raises(PipelineError, match="was kept as a new entity"):
        pipe.approve(second["id"], reviewer="rita", merge=True)


def test_the_other_name_match_is_kept_on_a_previously_merged_item(
    engine: Engine, graph: InMemoryGraph
) -> None:
    """Finding 3: the reviewer should see that the name also matches SW-ZMS."""
    pipe = _merged_qdx(engine, graph)
    zms = ZEBRA.model_copy(
        update={"properties": {"software_id": "SW-ZMS", "name": "Zebra Mapping Suite"}}
    )
    pipe.ingest(superset(zms), actor="sync")
    pipe.ingest(superset(ZEBRA), actor="sync")
    ent = next(i for i in dup_items(engine) if i["status"] == "open" and i["kind"] == "entity")
    assert ent["conflict_with"]["duplicate_of"]["also_matches"] == "SW-ZMS"
