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
from writer_service.ontology.neo4j_schema import InMemoryGraphSchema
from writer_service.ontology.service import OntologyService
from writer_service.pipeline.candidates import (
    CandidateEntity,
    CandidateRelation,
    EntityRef,
    Evidence,
)
from writer_service.pipeline.graph_store import InMemoryGraph
from writer_service.pipeline.orchestrator import SourceBatch, SourceText, WriterPipeline
from writer_service.pipeline.verification import BACK_TRANSLATION_PROMPT

PAGE1 = (
    "Aurora Pacific Holdings (Demo) (ticker APH) is listed on the Mainboard. "
    "Aurora Pacific Holdings (Demo) issued this annual report AR-2025-APH on 28 April 2026."
)
TEXTS = [SourceText("a.pdf", "page 1", PAGE1)]


def ev(quote: str) -> Evidence:
    return Evidence(source_type="pdf", source_ref="a.pdf", locator="page 1", quote=quote)


APH = CandidateEntity(
    class_name="Issuer",
    confidence=0.9,
    properties={"ticker": "APH", "name": "Aurora Pacific Holdings (Demo)"},
    evidence=[ev("Aurora Pacific Holdings (Demo) (ticker APH)")],
)
FILING = CandidateEntity(
    class_name="Filing",
    confidence=0.9,
    properties={"filing_id": "AR-2025-APH"},
    evidence=[ev("annual report AR-2025-APH")],
)
FILED = CandidateRelation(
    relation="FILED_BY",
    confidence=0.9,
    source=EntityRef(class_name="Filing", key="AR-2025-APH"),
    target=EntityRef(class_name="Issuer", key="APH"),
    evidence=[ev("Aurora Pacific Holdings (Demo) issued this annual report AR-2025-APH")],
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
    summary = pipeline(engine, graph).ingest(doc([APH, FILING, FILED]), actor="ingest-bot")
    assert summary.status == "completed"
    assert summary.counts["commit"] == 3 and summary.counts["candidates"] == 3
    aph = graph.node_properties("Issuer", "ticker", "APH")
    assert aph is not None and aph["run_id"] == summary.run_id and aph["ontology_version"] == 1
    assert graph.node_properties("Organisation", "ticker", "APH") is not None  # taxonomy label
    assert graph.targets(
        "FILED_BY", ("Filing", "filing_id", "AR-2025-APH"), {"Issuer": "ticker"}
    ) == [{"Issuer": "APH"}]
    assert len(graph.evidence) == 3
    (run,) = rows(engine, IngestionRun)
    assert run.status == "completed" and run.counts["commit"] == 3
    cands = rows(engine, Candidate)
    assert all(c.decision == "commit" and c.breakdown["components"] for c in cands)


def test_self_consistency_drops_facts_seen_once(engine: Engine, graph: InMemoryGraph) -> None:
    ghost = CandidateEntity(
        class_name="Issuer", properties={"ticker": "GHO", "name": "Ghost"}, evidence=[ev("Ghost")]
    )
    summary = pipeline(engine, graph).ingest(doc([APH, ghost], [APH], [APH]), actor="bot")
    assert summary.counts["extracted"] == 4 and summary.counts["candidates"] == 1
    assert graph.node_properties("Issuer", "ticker", "GHO") is None


def test_back_translation_is_called_once_per_fact(engine: Engine, graph: InMemoryGraph) -> None:
    fake = judge()
    pipeline(engine, graph, fake).ingest(doc([APH, FILED]), actor="bot")
    assert len([c for c in fake.calls if c.prompt_name == BACK_TRANSLATION_PROMPT.name]) == 2


# ---------------------------------------------------------------- routing


def test_ungrounded_quote_goes_to_review_not_graph(engine: Engine, graph: InMemoryGraph) -> None:
    invented = APH.model_copy(update={"evidence": [ev("APH reported record profits of $9bn")]})
    summary = pipeline(engine, graph).ingest(doc([invented]), actor="bot")
    assert summary.counts["commit"] == 0
    assert graph.node_count() == 0
    (cand,) = rows(engine, Candidate)
    assert cand.decision in ("review", "reject")
    assert cand.breakdown["components"]["grounding"]["value"] <= 0.1


def test_review_decision_creates_review_item(engine: Engine, graph: InMemoryGraph) -> None:
    summary = pipeline(engine, graph, judge(0.5)).ingest(doc([APH]), actor="bot")
    assert summary.counts["review"] == 1
    (item,) = rows(engine, ReviewItem)
    assert item.reason == "score" and item.status == "open"


def test_schema_failure_is_rejected_with_reasons(engine: Engine, graph: InMemoryGraph) -> None:
    bad = CandidateEntity(class_name="Issuer", properties={"name": "No Ticker"}, evidence=[])
    summary = pipeline(engine, graph).ingest(doc([bad]), actor="bot")
    assert summary.counts["reject"] == 1
    (cand,) = rows(engine, Candidate)
    assert cand.decision == "reject" and cand.score is None
    assert any("required property 'ticker'" in f for f in cand.gate_failures)


def test_contradiction_with_graph_is_a_conflict_review_item(
    engine: Engine, graph: InMemoryGraph
) -> None:
    graph.add_edge("FILED_BY", ("Filing", "filing_id", "AR-2025-APH"), ("Issuer", "ticker", "BYL"))
    summary = pipeline(engine, graph).ingest(doc([FILED]), actor="bot")
    assert summary.counts["conflict"] == 1
    (item,) = rows(engine, ReviewItem)
    assert item.reason == "conflict" and "BYL" in item.conflict_with["message"]


def test_relation_endpoint_resolved_by_name(engine: Engine, graph: InMemoryGraph) -> None:
    pipe = pipeline(engine, graph)
    pipe.ingest(doc([APH]), actor="bot")
    by_name = FILED.model_copy(
        update={"target": EntityRef(class_name="Issuer", key="Aurora Pacific Holdings")}
    )
    pipe.ingest(SourceBatch("pdf", "a.pdf", TEXTS, extract=lambda s: [by_name]), actor="bot")
    assert graph.targets(
        "FILED_BY", ("Filing", "filing_id", "AR-2025-APH"), {"Issuer": "ticker"}
    ) == [{"Issuer": "APH"}]
    rel = [c for c in rows(engine, Candidate) if c.kind == "relation"][0]
    assert any("resolved to 'APH'" in n for n in rel.breakdown["notes"])


# ---------------------------------------------------------------- structured sources


def test_structured_candidates_skip_llm_and_have_full_stability(
    engine: Engine, graph: InMemoryGraph
) -> None:
    row = SourceText(
        "dataset:1", "row ticker=APH", "ticker: APH; name: Aurora Pacific Holdings (Demo)"
    )
    cand = CandidateEntity(
        class_name="Issuer",
        properties={"ticker": "APH", "name": "Aurora Pacific Holdings (Demo)"},
        evidence=[
            Evidence(
                source_type="superset",
                source_ref="dataset:1",
                locator="row ticker=APH",
                quote="ticker: APH",
            )
        ],
    )
    fake = judge()
    summary = pipeline(engine, graph, fake).ingest(
        SourceBatch("superset", "dataset:1", [row], candidates=[cand]), actor="sync"
    )
    assert summary.counts["commit"] == 1 and fake.calls == []
    (c,) = rows(engine, Candidate)
    assert c.breakdown["components"]["stability"]["value"] == 1.0


# ---------------------------------------------------------------- idempotency + failures


def test_reingesting_the_same_document_creates_no_duplicates(
    engine: Engine, graph: InMemoryGraph
) -> None:
    pipe = pipeline(engine, graph)
    pipe.ingest(doc([APH, FILING, FILED]), actor="bot")
    nodes, edges, evidence = graph.node_count(), len(graph.edges), len(graph.evidence)
    pipe.ingest(doc([APH, FILING, FILED]), actor="bot")
    assert (graph.node_count(), len(graph.edges), len(graph.evidence)) == (nodes, edges, evidence)


def test_graph_failure_marks_run_failed_and_stores_nothing(
    engine: Engine, graph: InMemoryGraph
) -> None:
    graph.fail_next_write = True
    summary = pipeline(engine, graph).ingest(doc([APH]), actor="bot")
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
    summary = pipeline(engine, graph).ingest(doc([APH]), actor="bot")
    monkeypatch.undo()
    assert summary.status == "failed"
    assert graph.node_count() == 0 and graph.evidence == {}


def test_no_published_ontology_is_an_error(tmp_path: Path, graph: InMemoryGraph) -> None:
    from writer_service.pipeline.orchestrator import PipelineError

    url = f"sqlite:///{tmp_path / 'empty.sqlite'}"
    upgrade(url)
    with pytest.raises(PipelineError, match="no published ontology"):
        pipeline(create_engine_for(url), graph).ingest(doc([APH]), actor="bot")


# ---------------------------------------------------------------- rollback + review decisions


def test_rollback_removes_what_the_run_created(engine: Engine, graph: InMemoryGraph) -> None:
    pipe = pipeline(engine, graph)
    summary = pipe.ingest(doc([APH, FILING, FILED]), actor="bot")
    removed = pipe.rollback(summary.run_id, actor="admin")
    assert removed["nodes"] >= 3 and removed["relationships"] == 1
    assert graph.node_count() == 0 and graph.edges == [] and graph.evidence == {}
    (run,) = rows(engine, IngestionRun)
    assert run.status == "rolled_back"
    assert ("admin", "run.rollback") in [(a.actor, a.action) for a in rows(engine, AuditLog)]


def test_approving_a_review_item_writes_it(engine: Engine, graph: InMemoryGraph) -> None:
    pipe = pipeline(engine, graph, judge(0.5))
    pipe.ingest(doc([APH]), actor="bot")
    (item,) = rows(engine, ReviewItem)
    pipe.approve(item.id, reviewer="rita")
    assert graph.node_properties("Issuer", "ticker", "APH") is not None
    with UnitOfWork(engine) as uow:
        item = uow.session.scalars(select(ReviewItem)).one()
        assert item.status == "approved" and item.reviewer == "rita"
        assert item.candidate.decision == "commit"


def test_approving_a_conflict_replaces_the_old_edge(engine: Engine, graph: InMemoryGraph) -> None:
    graph.add_edge("FILED_BY", ("Filing", "filing_id", "AR-2025-APH"), ("Issuer", "ticker", "BYL"))
    graph.edge_meta.append({"created_run": "seed", "evidence_ids": [], "run_id": "seed"})
    pipe = pipeline(engine, graph)
    pipe.ingest(doc([FILED]), actor="bot")
    (item,) = rows(engine, ReviewItem)
    pipe.approve(item.id, reviewer="alice")
    assert graph.targets(
        "FILED_BY", ("Filing", "filing_id", "AR-2025-APH"), {"Issuer": "ticker"}
    ) == [{"Issuer": "APH"}]


def test_edited_approval_is_gated_and_rejection_recorded(
    engine: Engine, graph: InMemoryGraph
) -> None:
    from writer_service.pipeline.orchestrator import PipelineError

    pipe = pipeline(engine, graph, judge(0.5))
    pipe.ingest(doc([APH]), actor="bot")
    (item,) = rows(engine, ReviewItem)
    bad = {**APH.model_dump(mode="json"), "properties": {"name": "no ticker"}}
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
        "dataset:1", "row ticker=APH", "ticker: APH; name: Aurora Pacific Holdings (Demo)"
    )
    near = CandidateEntity(
        class_name="Issuer",
        properties={"ticker": "APH", "name": "Aurora Pacific Holdings (Demo)"},
        evidence=[
            Evidence(
                source_type="superset",
                source_ref="dataset:1",
                locator="row ticker=APH",
                quote="ticker: APH; name: Aurora Pacific Holding",
            )
        ],
    )
    graph.add_node("Issuer", "ticker", "APH", {"ticker": "APH"})  # known entity: match 1.0
    graph.add("Issuer", "ticker", "APH", name="Aurora Pacific Holdings (Demo)")
    summary = pipeline(engine, graph).ingest(
        SourceBatch("superset", "dataset:1", [row], candidates=[near]), actor="sync"
    )
    assert summary.counts["commit"] == 0 and summary.counts["review"] == 1
    (cand,) = rows(engine, Candidate)
    assert cand.score is not None and cand.score >= 0.85  # the score alone would have committed
    assert any("not verbatim" in r for r in cand.breakdown["reasons"])


# ---------------------------------------------------------------- review round 1 (PR #32)


def test_rollback_keeps_facts_another_run_also_owns(engine: Engine, graph: InMemoryGraph) -> None:
    pipe = pipeline(engine, graph)
    run1 = pipe.ingest(doc([FILED]), actor="bot").run_id  # creates APH as a stub endpoint
    pipe.ingest(doc([APH]), actor="bot")  # a later run states the full entity
    pipe.rollback(run1, actor="admin")
    aph = graph.node_properties("Issuer", "ticker", "APH")
    assert aph is not None and aph["name"] == "Aurora Pacific Holdings (Demo)"
    assert aph["runs"] == [aph["run_id"]] and aph["run_id"] != run1
    assert graph.edges == []  # run1's own edge is gone
    assert any(
        v["quote"].startswith("Aurora Pacific Holdings (Demo) (ticker")
        for v in graph.evidence.values()
    )


def test_rollback_of_a_repeat_run_changes_nothing_shared(
    engine: Engine, graph: InMemoryGraph
) -> None:
    pipe = pipeline(engine, graph)
    first = pipe.ingest(doc([APH, FILING, FILED]), actor="bot").run_id
    second = pipe.ingest(doc([APH, FILING, FILED]), actor="bot").run_id
    before = (graph.node_count(), len(graph.edges), len(graph.evidence))
    pipe.rollback(second, actor="admin")
    assert (graph.node_count(), len(graph.edges), len(graph.evidence)) == before
    assert graph.edge_meta[0]["run_id"] == first  # provenance falls back to the remaining run


def test_rollback_prunes_relationship_evidence_of_the_run(
    engine: Engine, graph: InMemoryGraph
) -> None:
    pipe = pipeline(engine, graph)
    pipe.ingest(doc([FILED]), actor="bot")
    other_quote = FILED.model_copy(update={"evidence": [ev("annual report AR-2025-APH")]})
    run2 = pipe.ingest(doc([other_quote]), actor="bot").run_id
    assert len(graph.edge_meta[0]["evidence_ids"]) == 2
    pipe.rollback(run2, actor="admin")
    (meta,) = graph.edge_meta
    assert len(meta["evidence_ids"]) == 1 and all(e in graph.evidence for e in meta["evidence_ids"])


def test_approve_regates_against_a_changed_graph(engine: Engine, graph: InMemoryGraph) -> None:
    from writer_service.pipeline.orchestrator import PipelineError

    pipe = pipeline(engine, graph, judge(0.5))
    pipe.ingest(doc([FILED]), actor="bot")  # score → review item
    graph.add_edge("FILED_BY", ("Filing", "filing_id", "AR-2025-APH"), ("Issuer", "ticker", "BYL"))
    graph.edge_meta.append({"runs": ["seed"], "evidence_ids": [], "run_id": "seed"})
    (item,) = rows(engine, ReviewItem)
    with pytest.raises(PipelineError, match="now a conflict"):
        pipe.approve(item.id, reviewer="rita")
    (item,) = rows(engine, ReviewItem)
    assert item.reason == "conflict" and item.status == "open"
    assert graph.targets(
        "FILED_BY", ("Filing", "filing_id", "AR-2025-APH"), {"Issuer": "ticker"}
    ) == [{"Issuer": "BYL"}]


def test_rollback_closes_open_items_and_blocks_their_approval(
    engine: Engine, graph: InMemoryGraph
) -> None:
    from writer_service.pipeline.orchestrator import PipelineError

    pipe = pipeline(engine, graph, judge(0.5))
    run = pipe.ingest(doc([APH]), actor="bot").run_id
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
    run = pipe.ingest(doc([APH]), actor="bot").run_id
    (item,) = rows(engine, ReviewItem)
    pipe.approve(item.id, reviewer="rita")
    pipe.rollback(run, actor="admin")
    aph = graph.node_properties("Issuer", "ticker", "APH")
    assert aph is not None and aph["runs"] == [WriterPipeline.approval_run_id(item.id)]


def test_approve_commit_failure_undoes_the_graph_write(
    engine: Engine, graph: InMemoryGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.orm import Session

    pipe = pipeline(engine, graph, judge(0.5))
    pipe.ingest(doc([APH]), actor="bot")
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
    graph.add_edge("FILED_BY", ("Filing", "filing_id", "AR-2025-APH"), ("Issuer", "ticker", "BYL"))
    graph.edge_meta.append({"runs": ["seed"], "evidence_ids": [], "run_id": "seed"})
    pipe = pipeline(engine, graph)
    pipe.ingest(doc([FILED]), actor="bot")
    (item,) = rows(engine, ReviewItem)
    pipe.approve(item.id, reviewer="alice")
    entry = next(a for a in rows(engine, AuditLog) if a.action == "review.approve")
    (rep,) = entry.detail["replaced"]
    assert (rep["relation"], rep["source"]["key"], rep["target"]["key"]) == (
        "FILED_BY",
        "AR-2025-APH",
        "BYL",
    )


def test_failed_conflict_approval_restores_the_replaced_edge(
    engine: Engine, graph: InMemoryGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.orm import Session

    graph.add_edge("FILED_BY", ("Filing", "filing_id", "AR-2025-APH"), ("Issuer", "ticker", "BYL"))
    graph.edge_meta.append({"runs": ["seed"], "evidence_ids": [], "run_id": "seed"})
    pipe = pipeline(engine, graph)
    pipe.ingest(doc([FILED]), actor="bot")
    (item,) = rows(engine, ReviewItem)

    def fail(self: Session) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(Session, "commit", fail)
    with pytest.raises(RuntimeError, match="db down"):
        pipe.approve(item.id, reviewer="alice")
    monkeypatch.undo()
    assert graph.targets(
        "FILED_BY", ("Filing", "filing_id", "AR-2025-APH"), {"Issuer": "ticker"}
    ) == [{"Issuer": "BYL"}]
