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


def test_failed_approval_restores_overwritten_entity_properties(
    engine: Engine, graph: InMemoryGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 3: compensation must undo property overwrites, not only edge removals."""
    from sqlalchemy.orm import Session

    old = APH.model_copy(update={"properties": {"ticker": "APH", "name": "Aurora Old Name"}})
    graph.write_run("seed", 1, [to_fact(old, OntologySpec(CORE_CLASSES, CORE_RELATIONS), 0.9)])
    pipe = pipeline(engine, graph, judge(0.5))
    pipe.ingest(doc([APH]), actor="bot")
    (item,) = rows(engine, ReviewItem)

    def fail(self: Session) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(Session, "commit", fail)
    with pytest.raises(RuntimeError, match="db down"):
        pipe.approve(item.id, reviewer="rita")
    monkeypatch.undo()
    aph = graph.node_properties("Issuer", "ticker", "APH")
    assert aph is not None
    assert (aph["name"], aph["run_id"], aph["runs"]) == ("Aurora Old Name", "seed", ["seed"])


@pytest.mark.parametrize(
    ("seed_props", "expected_name"),
    [
        (
            {"ticker": "APH", "name": "Aurora Pacific Holdings (Demo)"},
            "Aurora Pacific Holdings (Demo)",
        ),
        ({"ticker": "APH"}, None),  # the failed run added `name`: it must go again
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

    seed = APH.model_copy(update={"properties": seed_props})
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
    summary = pipeline(engine, graph).ingest(doc([APH]), actor="bot")
    monkeypatch.undo()
    assert summary.status == "failed"
    (changes,) = writes
    assert any(c["kind"] == "properties" for c in changes)  # the run did overwrite the entity
    aph = graph.node_properties("Issuer", "ticker", "APH")
    assert aph is not None
    assert (aph.get("name"), aph["score"], aph["run_id"], aph["runs"]) == (
        expected_name,
        0.4,
        "seed",
        ["seed"],
    )


# ---------------------------------------------------------------- possible duplicates (#38)

DUP_ROW = SourceText(
    "dataset:1", "row 1 (ticker=AUR)", "ticker: AUR; name: Aurora Pacific Holdings"
)


def sev(quote: str) -> Evidence:
    return Evidence(
        source_type="superset", source_ref="dataset:1", locator="row 1 (ticker=AUR)", quote=quote
    )


AUR = CandidateEntity(
    class_name="Issuer",
    properties={"ticker": "AUR", "name": "Aurora Pacific Holdings"},
    evidence=[sev("name: Aurora Pacific Holdings")],
)
AUR_FILED = CandidateRelation(
    relation="FILED_BY",
    source=EntityRef(class_name="Filing", key="AR-2025-APH"),
    target=EntityRef(class_name="Issuer", key="AUR"),
    evidence=[sev("ticker: AUR")],
)


def superset(*cands: Any) -> SourceBatch:
    return SourceBatch("superset", "dataset:1", [DUP_ROW], candidates=list(cands))


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
    pipe.ingest(doc([APH]), actor="bot")
    summary = pipe.ingest(superset(AUR), actor="sync")
    assert summary.counts["commit"] == 0 and summary.counts["review"] == 1
    assert graph.node_properties("Issuer", "ticker", "AUR") is None
    (item,) = dup_items(engine)
    dup = item["conflict_with"]["duplicate_of"]
    assert dup["class_name"] == "Issuer" and dup["key"] == "APH" and dup["candidate_key"] == "AUR"
    assert dup["key_property"] == "ticker" and dup["similarity"] >= 0.9
    assert "possible duplicate of Issuer 'APH'" in item["conflict_with"]["message"]
    assert any("possible duplicate" in r for r in item["reasons"])


def test_relations_to_a_possible_duplicate_wait_for_review(
    engine: Engine, graph: InMemoryGraph
) -> None:
    """Writing the relation would create the duplicate node as a stub."""
    pipe = pipeline(engine, graph)
    pipe.ingest(doc([APH, FILING]), actor="bot")
    summary = pipe.ingest(superset(AUR, AUR_FILED), actor="sync")
    assert summary.counts["commit"] == 0 and summary.counts["review"] == 2
    assert graph.node_properties("Issuer", "ticker", "AUR") is None
    rel = next(i for i in dup_items(engine) if i["kind"] == "relation")
    assert rel["conflict_with"]["duplicate_of"]["side"] == "target"
    assert rel["conflict_with"]["duplicate_of"]["key"] == "APH"


def test_approving_a_possible_duplicate_as_new_creates_it(
    engine: Engine, graph: InMemoryGraph
) -> None:
    pipe = pipeline(engine, graph)
    pipe.ingest(doc([APH]), actor="bot")
    pipe.ingest(superset(AUR), actor="sync")
    (item,) = dup_items(engine)
    pipe.approve(item["id"], reviewer="rita")
    assert graph.node_properties("Issuer", "ticker", "AUR") is not None
    assert graph.node_properties("Issuer", "ticker", "APH") is not None


def test_merging_a_possible_duplicate_writes_into_the_existing_entity(
    engine: Engine, graph: InMemoryGraph
) -> None:
    pipe = pipeline(engine, graph)
    pipe.ingest(doc([APH, FILING]), actor="bot")
    pipe.ingest(superset(AUR, AUR_FILED), actor="sync")
    for item in dup_items(engine):
        pipe.approve(item["id"], reviewer="rita", merge=True)
    assert graph.node_properties("Issuer", "ticker", "AUR") is None
    aph = graph.node_properties("Issuer", "ticker", "APH")
    assert aph is not None and aph["name"] == "Aurora Pacific Holdings (Demo)"  # existing kept
    assert graph.targets(
        "FILED_BY", ("Filing", "filing_id", "AR-2025-APH"), {"Issuer": "ticker"}
    ) == [{"Issuer": "APH"}]
    with UnitOfWork(engine) as uow:
        items = [i for i in uow.session.scalars(select(ReviewItem)) if i.reason == "duplicate"]
        assert {i.status for i in items} == {"edited"}
        entity = next(i for i in items if i.candidate.kind == "entity")
        assert entity.candidate.payload["properties"]["ticker"] == "APH"
        relation = next(i for i in items if i.candidate.kind == "relation")
        assert relation.candidate.payload["target"]["key"] == "APH"
    merged = [a for a in rows(engine, AuditLog) if a.action == "review.approve"]
    assert all(a.detail["merged_into"] == "APH" for a in merged)
    entity_audit = next(a for a in merged if a.detail["kept_existing"])
    assert entity_audit.detail["kept_existing"] == {"name": "Aurora Pacific Holdings"}


def test_merge_is_only_for_duplicates(engine: Engine, graph: InMemoryGraph) -> None:
    from writer_service.pipeline.orchestrator import PipelineError

    pipe = pipeline(engine, graph, judge(0.5))
    pipe.ingest(doc([APH]), actor="bot")
    (item,) = rows(engine, ReviewItem)
    assert item.reason == "score"
    with pytest.raises(PipelineError, match="only a possible duplicate can be merged"):
        pipe.approve(item.id, reviewer="rita", merge=True)
    assert rows(engine, ReviewItem)[0].status == "open"


def test_exact_and_unrelated_entities_are_not_flagged(engine: Engine, graph: InMemoryGraph) -> None:
    pipe = pipeline(engine, graph)
    pipe.ingest(doc([APH]), actor="bot")
    other = CandidateEntity(
        class_name="Issuer",
        properties={"ticker": "AUR", "name": "Zebra Mining"},
        evidence=[sev("ticker: AUR")],
    )
    summary = pipe.ingest(
        SourceBatch(
            "superset",
            "dataset:1",
            [SourceText("dataset:1", "row 1 (ticker=AUR)", "ticker: AUR; name: Zebra Mining")],
            candidates=[other],
        ),
        actor="sync",
    )
    assert summary.counts["commit"] == 1 and dup_items(engine) == []


# review #39 round 1


def test_a_conflicting_relation_to_a_duplicate_can_be_merged(
    engine: Engine, graph: InMemoryGraph
) -> None:
    """Finding 1: the graph has FILED_BY APH; FILED_BY AUR conflicts, and must still carry
    duplicate_of so the reviewer can merge instead of replacing the edge with a new AUR."""
    pipe = pipeline(engine, graph)
    pipe.ingest(doc([APH, FILING, FILED]), actor="bot")
    pipe.ingest(superset(AUR, AUR_FILED), actor="sync")
    rel = next(i for i in rows(engine, ReviewItem) if i.reason == "conflict")
    assert rel.conflict_with["duplicate_of"]["key"] == "APH"
    (ent,) = dup_items(engine)
    pipe.approve(ent["id"], reviewer="rita", merge=True)
    pipe.approve(rel.id, reviewer="rita", merge=True)
    assert graph.node_properties("Issuer", "ticker", "AUR") is None
    assert graph.targets(
        "FILED_BY", ("Filing", "filing_id", "AR-2025-APH"), {"Issuer": "ticker"}
    ) == [{"Issuer": "APH"}]


def test_a_relation_kept_as_new_needs_its_entity_accepted_first(
    engine: Engine, graph: InMemoryGraph
) -> None:
    """Finding 2: keeping FILED_BY -> AUR while AUR was merged away (or rejected) would
    create AUR as a stub."""
    from writer_service.pipeline.orchestrator import PipelineError

    pipe = pipeline(engine, graph)
    pipe.ingest(doc([APH, FILING]), actor="bot")
    pipe.ingest(superset(AUR, AUR_FILED), actor="sync")
    ent = next(i for i in dup_items(engine) if i["kind"] == "entity")
    rel = next(i for i in dup_items(engine) if i["kind"] == "relation")
    pipe.reject(ent["id"], reviewer="rita")
    with pytest.raises(PipelineError, match="approve its entity item first"):
        pipe.approve(rel["id"], reviewer="rita")
    assert graph.node_properties("Issuer", "ticker", "AUR") is None
    pipe.approve(rel["id"], reviewer="rita", merge=True)  # merging is still possible
    assert graph.node_properties("Issuer", "ticker", "AUR") is None


def test_keeping_entity_and_relation_as_new_in_order_works(
    engine: Engine, graph: InMemoryGraph
) -> None:
    pipe = pipeline(engine, graph)
    pipe.ingest(doc([APH, FILING]), actor="bot")
    pipe.ingest(superset(AUR_FILED, AUR), actor="sync")  # relation listed before its entity
    items = dup_items(engine)
    assert {i["kind"] for i in items} == {"entity", "relation"}
    for kind in ("entity", "relation"):
        pipe.approve(next(i["id"] for i in items if i["kind"] == kind), reviewer="rita")
    aur = graph.node_properties("Issuer", "ticker", "AUR")
    assert aur is not None and not aur["stub"]


def test_merge_target_must_be_an_accepted_entity(engine: Engine, graph: InMemoryGraph) -> None:
    """Finding 3: the matcher also compares against new entities of the same batch; merging
    into one that was never accepted must not write it."""
    from writer_service.pipeline.orchestrator import PipelineError

    first = CandidateEntity(
        class_name="Issuer",
        confidence=0.9,
        properties={"ticker": "APH", "name": "Aurora Pacific Holdings (Demo)"},
        evidence=[ev("Aurora Pacific Holdings (Demo) (ticker APH)")],
    )
    twin = first.model_copy(
        update={"properties": {"ticker": "AURX", "name": "Aurora Pacific Holdings (Demo)"}}
    )
    pipe = pipeline(engine, graph, judge(0.5))  # both land in review
    pipe.ingest(doc([first, twin]), actor="bot")
    score = next(i for i in rows(engine, ReviewItem) if i.reason == "score")
    (dup,) = dup_items(engine)
    assert dup["conflict_with"]["duplicate_of"]["key"] == "APH"
    pipe.reject(score.id, reviewer="rita")
    with pytest.raises(PipelineError, match="not in the graph"):
        pipe.approve(dup["id"], reviewer="rita", merge=True)
    assert graph.node_count() == 0


def test_merge_that_contradicts_the_graph_stays_open_unchanged(
    engine: Engine, graph: InMemoryGraph
) -> None:
    """Finding 5: AR-2025-APH is filed by BYL; merging FILED_BY -> AUR into APH contradicts
    that. Nothing is written and the item keeps its reason and original fact."""
    from writer_service.pipeline.orchestrator import PipelineError

    pipe = pipeline(engine, graph)
    pipe.ingest(doc([APH]), actor="bot")
    graph.add_edge("FILED_BY", ("Filing", "filing_id", "AR-2025-APH"), ("Issuer", "ticker", "BYL"))
    pipe.ingest(superset(AUR_FILED, AUR), actor="sync")
    rel = next(i for i in rows(engine, ReviewItem) if i.candidate_id and i.reason == "conflict")
    with pytest.raises(PipelineError, match="contradicts the graph"):
        pipe.approve(rel.id, reviewer="rita", merge=True)
    with UnitOfWork(engine) as uow:
        item = uow.session.get(ReviewItem, rel.id)
        assert item is not None and item.status == "open" and item.reason == "conflict"
        assert item.candidate.payload["target"]["key"] == "AUR"
        assert "BYL" in item.conflict_with["merge_conflict"]


def test_merge_with_an_edit_and_case_insensitive_endpoints(
    engine: Engine, graph: InMemoryGraph
) -> None:
    pipe = pipeline(engine, graph)
    pipe.ingest(doc([APH, FILING]), actor="bot")
    lower = AUR_FILED.model_copy(
        update={"target": EntityRef(class_name="Issuer", key="aur")}
    )  # same new entity, other case
    pipe.ingest(superset(AUR, lower), actor="sync")
    items = dup_items(engine)
    rel = next(i for i in items if i["kind"] == "relation")
    assert rel["conflict_with"]["duplicate_of"]["key"] == "APH"
    ent = next(i for i in items if i["kind"] == "entity")
    edited = AUR.model_copy(
        update={"properties": {**AUR.properties, "sector": "Industrials"}}
    ).model_dump(mode="json")
    pipe.approve(ent["id"], reviewer="rita", edited=edited, merge=True)
    aph = graph.node_properties("Issuer", "ticker", "APH")
    assert aph is not None and aph["sector"] == "Industrials"
    assert aph["name"] == "Aurora Pacific Holdings (Demo)"
    assert graph.node_properties("Issuer", "ticker", "AUR") is None


def test_rollback_closes_open_duplicate_items(engine: Engine, graph: InMemoryGraph) -> None:
    pipe = pipeline(engine, graph)
    pipe.ingest(doc([APH, FILING]), actor="bot")
    summary = pipe.ingest(superset(AUR, AUR_FILED), actor="sync")
    assert pipe.rollback(summary.run_id, actor="admin")["review_items_closed"] == 2
    assert {i["status"] for i in dup_items(engine)} == {"rejected"}


def test_both_flagged_endpoints_are_recorded() -> None:
    """Finding 6: a relation whose two endpoints are both possible duplicates."""
    from writer_service.pipeline.orchestrator import _duplicate_of

    dups = {
        ("Issuer", "aur"): {"class_name": "Issuer", "key": "APH", "candidate_key": "AUR"},
        ("Issuer", "byx"): {"class_name": "Issuer", "key": "BYL", "candidate_key": "BYX"},
    }
    rel = CandidateRelation(
        relation="PARTNERS_WITH",
        source=EntityRef(class_name="Issuer", key="AUR"),
        target=EntityRef(class_name="Issuer", key="BYX"),
    )
    dup = _duplicate_of(rel, dups)
    assert dup is not None and dup["side"] == "source"
    assert [(s["side"], s["key"]) for s in dup["sides"]] == [("source", "APH"), ("target", "BYL")]


# review #39 round 2


def test_a_merge_sticks_when_the_source_is_ingested_again(
    engine: Engine, graph: InMemoryGraph
) -> None:
    """Finding 1: re-syncing the same dataset must not re-open the duplicate."""
    pipe = pipeline(engine, graph)
    pipe.ingest(doc([APH, FILING]), actor="bot")
    pipe.ingest(superset(AUR, AUR_FILED), actor="sync")
    for item in dup_items(engine):
        pipe.approve(item["id"], reviewer="rita", merge=True)
    again = pipe.ingest(superset(AUR, AUR_FILED), actor="sync")
    assert again.counts["commit"] == 2 and again.counts["review"] == 0, again.counts
    assert again.counts["conflict"] == 0
    assert graph.node_properties("Issuer", "ticker", "AUR") is None
    aph = graph.node_properties("Issuer", "ticker", "APH")
    assert aph is not None and aph["name"] == "Aurora Pacific Holdings (Demo)"
    with UnitOfWork(engine) as uow:
        latest = list(
            uow.session.scalars(select(Candidate).where(Candidate.run_id == again.run_id))
        )
        assert any("was merged into 'APH'" in n for c in latest for n in c.breakdown["notes"])


def test_keep_as_new_writes_the_stored_key(engine: Engine, graph: InMemoryGraph) -> None:
    """Finding 2: Neo4j keys are case-sensitive; the relation must use the stored key."""
    pipe = pipeline(engine, graph)
    pipe.ingest(doc([APH, FILING]), actor="bot")
    lower = AUR_FILED.model_copy(update={"target": EntityRef(class_name="Issuer", key="aur")})
    pipe.ingest(superset(lower, AUR), actor="sync")
    items = dup_items(engine)
    for kind in ("entity", "relation"):
        pipe.approve(next(i["id"] for i in items if i["kind"] == kind), reviewer="rita")
    with UnitOfWork(engine) as uow:
        rel = next(
            i for i in uow.session.scalars(select(ReviewItem)) if i.candidate.kind == "relation"
        )
        assert rel.candidate.payload["target"]["key"] == "AUR"


def test_merging_a_relation_after_keeping_its_entity_is_refused(
    engine: Engine, graph: InMemoryGraph
) -> None:
    """Finding 3: that would split the entity (AUR accepted, its filing pointing at APH)."""
    from writer_service.pipeline.orchestrator import PipelineError

    pipe = pipeline(engine, graph)
    pipe.ingest(doc([APH, FILING]), actor="bot")
    pipe.ingest(superset(AUR, AUR_FILED), actor="sync")
    items = dup_items(engine)
    pipe.approve(next(i["id"] for i in items if i["kind"] == "entity"), reviewer="rita")
    rel = next(i["id"] for i in items if i["kind"] == "relation")
    with pytest.raises(PipelineError, match="was kept as a new entity"):
        pipe.approve(rel, reviewer="rita", merge=True)
    pipe.approve(rel, reviewer="rita")  # keeping it as new is fine
    assert graph.targets(
        "FILED_BY", ("Filing", "filing_id", "AR-2025-APH"), {"Issuer": "ticker"}
    ) == [{"Issuer": "AUR"}]


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
        relation="FILED_BY",
        source=EntityRef(class_name="Filing", key="AR-NEW"),
        target=EntityRef(class_name="Issuer", key="AUR"),
    )
    dup = {
        "sides": [
            {
                "side": "source",
                "class_name": "Filing",
                "candidate_key": "AR-NEW",
                "key": "AR-2025-APH",
            },
            {"side": "target", "class_name": "Issuer", "candidate_key": "AUR", "key": "APH"},
        ]
    }
    with pytest.raises(PipelineError, match="not in the graph"):
        pipe._merged(rel, dup, spec)
    pipe.ingest(doc([APH, FILING]), actor="bot")
    merged, _, sides = pipe._merged(rel, dup, spec)
    assert [s["side"] for s in sides] == ["source", "target"]
    assert isinstance(merged, CandidateRelation)
    assert (merged.source.key, merged.target.key) == ("AR-2025-APH", "APH")


# review #39 round 3


def _merge_all(pipe: WriterPipeline, engine: Engine) -> None:
    for item in dup_items(engine):
        if item["status"] == "open":
            pipe.approve(item["id"], reviewer="rita", merge=True)


def test_a_reused_key_is_not_rewritten_onto_the_merge_target(
    engine: Engine, graph: InMemoryGraph
) -> None:
    """Finding 1: after AUR was merged into APH, a row AUR "Zebra Mining Corp" is not the
    same entity: it goes to review (with the earlier merge noted), nothing lands on APH."""
    pipe = pipeline(engine, graph)
    pipe.ingest(doc([APH, FILING]), actor="bot")
    pipe.ingest(superset(AUR, AUR_FILED), actor="sync")
    _merge_all(pipe, engine)
    zebra = AUR.model_copy(
        update={"properties": {"ticker": "AUR", "name": "Zebra Mining Corp", "sector": "Mining"}}
    )
    again = pipe.ingest(superset(zebra, AUR_FILED), actor="sync")
    assert again.counts["commit"] == 0, again.counts
    aph = graph.node_properties("Issuer", "ticker", "APH")
    assert aph is not None and "sector" not in aph
    open_items = [i for i in dup_items(engine) if i["status"] == "open"]
    ent = next(i for i in open_items if i["kind"] == "entity")
    assert ent["conflict_with"]["duplicate_of"]["previously_merged"]
    assert any("doesn't match it" in r for r in ent["reasons"] + [ent["conflict_with"]["message"]])
    # the reviewer decides AUR is now a different company: allowed for this item
    pipe.approve(ent["id"], reviewer="rita")
    assert graph.node_properties("Issuer", "ticker", "AUR") is not None


def test_keep_as_new_after_the_relation_was_merged_is_refused(
    engine: Engine, graph: InMemoryGraph
) -> None:
    """Finding 2: relation merged first, then the entity kept as new would split AUR."""
    from writer_service.pipeline.orchestrator import PipelineError

    pipe = pipeline(engine, graph)
    pipe.ingest(doc([APH, FILING]), actor="bot")
    pipe.ingest(superset(AUR, AUR_FILED), actor="sync")
    items = dup_items(engine)
    pipe.approve(next(i["id"] for i in items if i["kind"] == "relation"), "rita", merge=True)
    ent = next(i["id"] for i in items if i["kind"] == "entity")
    with pytest.raises(PipelineError, match="was merged into 'APH'"):
        pipe.approve(ent, reviewer="rita")
    assert graph.node_properties("Issuer", "ticker", "AUR") is None
    pipe.approve(ent, reviewer="rita", merge=True)


def test_two_open_items_for_one_key_cannot_disagree(engine: Engine, graph: InMemoryGraph) -> None:
    """Finding 3: two syncs before review open two duplicate items for AUR."""
    from writer_service.pipeline.orchestrator import PipelineError

    pipe = pipeline(engine, graph)
    pipe.ingest(doc([APH]), actor="bot")
    pipe.ingest(superset(AUR), actor="sync")
    pipe.ingest(superset(AUR), actor="sync")
    first, second = dup_items(engine)
    pipe.approve(first["id"], reviewer="rita", merge=True)
    with pytest.raises(PipelineError, match="was merged into 'APH'"):
        pipe.approve(second["id"], reviewer="rita")
    assert graph.node_properties("Issuer", "ticker", "AUR") is None


def test_merge_skips_a_side_the_reviewer_edited(engine: Engine, graph: InMemoryGraph) -> None:
    """Finding 4: edit the target elsewhere, then merge: nothing left to merge."""
    from writer_service.pipeline.orchestrator import PipelineError

    pipe = pipeline(engine, graph)
    pipe.ingest(doc([APH, FILING]), actor="bot")
    pipe.ingest(superset(AUR, AUR_FILED), actor="sync")
    rel = next(i for i in dup_items(engine) if i["kind"] == "relation")
    edited = AUR_FILED.model_copy(
        update={"target": EntityRef(class_name="Issuer", key="APH")}
    ).model_dump(mode="json")
    with pytest.raises(PipelineError, match="nothing to merge"):
        pipe.approve(rel["id"], reviewer="rita", edited=edited, merge=True)


# review #39 round 4

ZEBRA = AUR.model_copy(
    update={"properties": {"ticker": "AUR", "name": "Zebra Mining Corp", "sector": "Mining"}}
)
NEW_FILING = CandidateRelation(
    relation="FILED_BY",
    source=EntityRef(class_name="Filing", key="AR-2026-AUR"),
    target=EntityRef(class_name="Issuer", key="AUR"),
    evidence=[sev("ticker: AUR")],
)


def _merged_aur(engine: Engine, graph: InMemoryGraph) -> WriterPipeline:
    pipe = pipeline(engine, graph)
    pipe.ingest(doc([APH, FILING]), actor="bot")
    pipe.ingest(superset(AUR, AUR_FILED), actor="sync")
    _merge_all(pipe, engine)
    return pipe


def test_a_reused_key_stays_blocked_for_relations_in_a_later_run(
    engine: Engine, graph: InMemoryGraph
) -> None:
    """Finding 1: issuers and filings are separate datasets, so separate runs."""
    pipe = _merged_aur(engine, graph)
    assert pipe.ingest(superset(ZEBRA), actor="sync").counts["commit"] == 0
    filings = pipe.ingest(superset(NEW_FILING), actor="sync")
    assert filings.counts["commit"] == 0 and filings.counts["review"] == 1, filings.counts
    assert (
        graph.targets("FILED_BY", ("Filing", "filing_id", "AR-2026-AUR"), {"Issuer": "ticker"})
        == []
    )
    # the reviewer keeps Zebra as a new AUR; then the filing can be kept for it
    open_items = [i for i in dup_items(engine) if i["status"] == "open"]
    ent = next(i for i in open_items if i["kind"] == "entity")
    rel = next(i for i in open_items if i["kind"] == "relation")
    pipe.approve(ent["id"], reviewer="rita")
    pipe.approve(rel["id"], reviewer="rita")
    assert graph.targets(
        "FILED_BY", ("Filing", "filing_id", "AR-2026-AUR"), {"Issuer": "ticker"}
    ) == [{"Issuer": "AUR"}]


def test_a_newer_merge_revokes_the_keep_as_new_exemption(
    engine: Engine, graph: InMemoryGraph
) -> None:
    """Finding 2: merge the reused key's relation into APH, then keep the entity: a split."""
    from writer_service.pipeline.orchestrator import PipelineError

    pipe = _merged_aur(engine, graph)
    pipe.ingest(superset(ZEBRA, NEW_FILING), actor="sync")
    open_items = [i for i in dup_items(engine) if i["status"] == "open"]
    rel = next(i for i in open_items if i["kind"] == "relation")
    ent = next(i for i in open_items if i["kind"] == "entity")
    pipe.approve(rel["id"], reviewer="rita", merge=True)
    with pytest.raises(PipelineError, match="'AUR' was merged into 'APH'"):
        pipe.approve(ent["id"], reviewer="rita")
    assert graph.node_properties("Issuer", "ticker", "AUR") is None


def test_the_earlier_merge_wins_over_another_name_match(
    engine: Engine, graph: InMemoryGraph
) -> None:
    """Finding 3: an accepted ZMC also matches the Zebra name; the item must still say
    'previously merged into APH', so keeping AUR as new stays possible."""
    pipe = _merged_aur(engine, graph)
    zmc = ZEBRA.model_copy(update={"properties": {"ticker": "ZMC", "name": "Zebra Mining Corp"}})
    pipe.ingest(superset(zmc), actor="sync")
    pipe.ingest(superset(ZEBRA), actor="sync")
    ent = next(i for i in dup_items(engine) if i["status"] == "open" and i["kind"] == "entity")
    assert ent["conflict_with"]["duplicate_of"]["key"] == "APH"
    assert ent["conflict_with"]["duplicate_of"]["previously_merged"]
    pipe.approve(ent["id"], reviewer="rita")
    assert graph.node_properties("Issuer", "ticker", "AUR") is not None


# review #39 round 5


def test_a_relation_alone_is_never_rewritten_through_an_alias(
    engine: Engine, graph: InMemoryGraph
) -> None:
    """Finding 1 (a): a filings sync arrives before the issuer sync; nothing in it says AUR
    is still the merged entity, so it goes to review (pointing at APH) instead of committing
    onto APH unseen."""
    pipe = _merged_aur(engine, graph)
    run = pipe.ingest(superset(NEW_FILING), actor="sync")
    assert run.counts["commit"] == 0 and run.counts["review"] == 1, run.counts
    assert (
        graph.targets("FILED_BY", ("Filing", "filing_id", "AR-2026-AUR"), {"Issuer": "ticker"})
        == []
    )
    rel = next(i for i in dup_items(engine) if i["status"] == "open")
    dup = rel["conflict_with"]["duplicate_of"]
    assert dup["key"] == "APH" and dup["merged_earlier"]
    assert "nothing in this batch confirms" in rel["conflict_with"]["message"]
    pipe.approve(rel["id"], reviewer="rita", merge=True)  # the reviewer confirms
    assert graph.targets(
        "FILED_BY", ("Filing", "filing_id", "AR-2026-AUR"), {"Issuer": "ticker"}
    ) == [{"Issuer": "APH"}]


def test_after_rejecting_a_reused_key_its_relations_still_wait(
    engine: Engine, graph: InMemoryGraph
) -> None:
    """Finding 1 (b): rejecting the Zebra item must not let later filings commit onto APH."""
    pipe = _merged_aur(engine, graph)
    pipe.ingest(superset(ZEBRA), actor="sync")
    ent = next(i for i in dup_items(engine) if i["status"] == "open")
    pipe.reject(ent["id"], reviewer="rita")
    run = pipe.ingest(superset(NEW_FILING), actor="sync")
    assert run.counts["commit"] == 0 and run.counts["review"] == 1, run.counts


def test_an_entity_kept_as_new_cannot_then_be_merged(engine: Engine, graph: InMemoryGraph) -> None:
    """Finding 2: two open items for AUR; keep A as new, then merging B would put AUR's
    values on APH while AUR also exists."""
    from writer_service.pipeline.orchestrator import PipelineError

    pipe = pipeline(engine, graph)
    pipe.ingest(doc([APH]), actor="bot")
    pipe.ingest(superset(AUR), actor="sync")
    pipe.ingest(superset(AUR), actor="sync")
    first, second = dup_items(engine)
    pipe.approve(first["id"], reviewer="rita")
    with pytest.raises(PipelineError, match="was kept as a new entity"):
        pipe.approve(second["id"], reviewer="rita", merge=True)


def test_the_other_name_match_is_kept_on_a_previously_merged_item(
    engine: Engine, graph: InMemoryGraph
) -> None:
    """Finding 3: the reviewer should see that the name also matches ZMC."""
    pipe = _merged_aur(engine, graph)
    zmc = ZEBRA.model_copy(update={"properties": {"ticker": "ZMC", "name": "Zebra Mining Corp"}})
    pipe.ingest(superset(zmc), actor="sync")
    pipe.ingest(superset(ZEBRA), actor="sync")
    ent = next(i for i in dup_items(engine) if i["status"] == "open" and i["kind"] == "entity")
    assert ent["conflict_with"]["duplicate_of"]["also_matches"] == "ZMC"
