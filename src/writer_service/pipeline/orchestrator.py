"""The writer pipeline: one place every source goes through before Neo4j (design §3).

    Ingest → Match → Compile (gates) → Test (signals + score) → Review (route) → Commit

``WriterPipeline.ingest`` runs a whole source batch as one ``IngestionRun``:

1. **Ingest**: candidates from the source. Documents are extracted N times
   (self-consistency); structured sources pass deterministic candidates (stability 1.0).
2. **Match**: resolve entity references to existing keys (``EntityMatcher``).
3. **Compile**: schema gate (reject) and constraint gate (conflict) over the batch.
4. **Test**: grounding, back-translation (LLM-extracted sources only), stability, entity
   match and self-confidence → weighted score with breakdown. A fact whose grounding is
   below 1.0 (quote not verbatim) is routed to review at most, whatever its score.
5. **Review**: route to commit / review / conflict / reject; every candidate is stored with
   its derivation; review and conflict create review items.
6. **Commit**: all ``commit`` facts are written to Neo4j in **one transaction** with
   provenance. If the graph write fails nothing is written and the run is marked ``failed``;
   if the governance commit fails after the graph write, the run is rolled back in the graph.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import TypeAdapter
from sqlalchemy import Engine

from writer_service.governance.models import Candidate, IngestionRun, ReviewItem, utcnow
from writer_service.governance.uow import UnitOfWork
from writer_service.llm.structured import StructuredLLM
from writer_service.ontology.service import spec_of
from writer_service.ontology.spec import OntologySpec
from writer_service.pipeline.candidates import (
    Candidate as AnyCandidateModel,
)
from writer_service.pipeline.candidates import (
    CandidateEntity,
    CandidateRelation,
    SourceType,
    fact_hash,
)
from writer_service.pipeline.gates import check_batch, schema_gate
from writer_service.pipeline.graph_store import GraphWriter, to_fact
from writer_service.pipeline.graph_view import GraphView
from writer_service.pipeline.matching import EntityMatcher, MatchIndex
from writer_service.pipeline.verification import (
    BackTranslator,
    DictSourceText,
    VotedCandidate,
    self_consistency,
)
from writer_service.scoring.engine import Routing, Signals, route, score
from writer_service.scoring.store import active_policy, record_outcome

AnyCandidate = CandidateEntity | CandidateRelation
_CANDIDATE = TypeAdapter(AnyCandidateModel)


class GraphPort(GraphView, MatchIndex, GraphWriter, Protocol):
    """Everything the pipeline needs from the graph (``InMemoryGraph`` / ``Neo4jGraph``)."""


class PipelineError(Exception):
    pass


@dataclass(frozen=True)
class SourceText:
    """Text of one page/row, used to verify evidence quotes."""

    source_ref: str
    locator: str
    text: str


@dataclass
class SourceBatch:
    source_type: SourceType
    source_ref: str
    texts: Sequence[SourceText]
    # LLM-extracted sources: extract(sample) -> candidates (called N times)
    extract: Callable[[int], Sequence[AnyCandidate]] | None = None
    # deterministic sources (column mappings): the candidates themselves
    candidates: Sequence[AnyCandidate] | None = None


@dataclass
class RunSummary:
    run_id: str
    status: str
    counts: dict[str, int] = field(default_factory=dict)
    error: str | None = None


class WriterPipeline:
    def __init__(self, engine: Engine, graph: GraphPort, llm: StructuredLLM | None = None) -> None:
        self.engine = engine
        self.graph = graph
        self.llm = llm

    # ----------------------------------------------------------------- ingest

    def ingest(self, batch: SourceBatch, actor: str) -> RunSummary:
        with UnitOfWork(self.engine) as uow:
            published = uow.ontology_versions.published()
            if published is None:
                raise PipelineError("no published ontology; run `writer-ontology bootstrap`")
            spec, version = spec_of(published), published.version
            policy = active_policy(uow)
            run = IngestionRun(
                source_type=batch.source_type,
                source_ref=batch.source_ref,
                ontology_version=version,
                started_by=actor,
            )
            uow.session.add(run)
            uow.session.flush()  # assigns the run id
            run_id = run.id
            uow.audit(actor, "run.start", "ingestion_run", run_id, {"source": batch.source_ref})

        graph_written = False
        try:
            with UnitOfWork(self.engine) as uow:
                counts, facts = self._process(uow, run_id, batch, spec, version, policy)
                self.graph.write_run(run_id, version, facts)
                graph_written = True
                run_row = uow.session.get(IngestionRun, run_id)
                assert run_row is not None
                run_row.status, run_row.counts, run_row.finished_at = "completed", counts, utcnow()
                uow.audit(actor, "run.complete", "ingestion_run", run_id, counts)
            return RunSummary(run_id, "completed", counts)
        except Exception as exc:
            if graph_written:  # SQL failed after the graph committed: undo the graph side
                try:
                    self.graph.rollback_run(run_id)
                except Exception as undo_exc:
                    exc.add_note(f"graph rollback also failed: {undo_exc}")
            with UnitOfWork(self.engine) as uow:
                row = uow.session.get(IngestionRun, run_id)
                if row is not None:
                    row.status, row.error, row.finished_at = "failed", str(exc)[:2000], utcnow()
                uow.audit(actor, "run.failed", "ingestion_run", run_id, {"error": str(exc)[:500]})
            return RunSummary(run_id, "failed", {}, str(exc))

    def _candidates(self, batch: SourceBatch, n: int) -> tuple[list[VotedCandidate], int]:
        if batch.extract is not None:
            raw_total = 0

            def counted(sample: int) -> Sequence[AnyCandidate]:
                nonlocal raw_total
                out = batch.extract(sample)  # type: ignore[misc]
                raw_total += len(out)
                return out

            voted = self_consistency(counted, n)
            return voted, raw_total
        cands = list(batch.candidates or [])
        return [VotedCandidate(c, 1, 1.0) for c in cands], len(cands)

    def _process(
        self,
        uow: UnitOfWork,
        run_id: str,
        batch: SourceBatch,
        spec: OntologySpec,
        version: int,
        policy: Any,
    ) -> tuple[dict[str, int], list[Any]]:
        n = policy.self_consistency_runs if batch.extract is not None else 1
        voted, raw_total = self._candidates(batch, n)
        matcher = EntityMatcher(self.graph, spec)
        texts = DictSourceText({(t.source_ref, t.locator): t.text for t in batch.texts})
        translator = (
            BackTranslator(self.llm, version)
            if batch.extract is not None and self.llm is not None
            else None
        )

        resolved: list[AnyCandidate] = []
        match_conf: list[float | None] = []
        notes: list[list[str]] = []
        for v in voted:
            c = v.candidate
            if isinstance(c, CandidateEntity):
                m = matcher.match_entity(c) if c.class_name in matcher.classes else None
                conf = m.confidence if m else None
                note = []
                if m is not None and m.status == "possible_duplicate":
                    note.append(f"possible duplicate of {c.class_name} {m.key!r}")
                if c.class_name in matcher.classes:
                    matcher.add_batch_entity(c)
                resolved.append(c)
            else:
                known = (
                    c.source.class_name in matcher.classes
                    and c.target.class_name in matcher.classes
                )
                if known:
                    c, conf, note = matcher.resolve_relation(c)
                else:
                    conf, note = None, []
                resolved.append(c)
            match_conf.append(conf)
            notes.append(note)

        results = check_batch(resolved, spec, self.graph)
        counts = {
            "extracted": raw_total,
            "candidates": len(voted),
            "commit": 0,
            "review": 0,
            "conflict": 0,
            "reject": 0,
        }
        facts = []
        for v, result, conf, note in zip(voted, results, match_conf, notes, strict=True):
            cand = result.candidate
            row = Candidate(
                run_id=run_id,
                kind=cand.kind,
                canonical_hash=fact_hash(cand),
                payload=cand.model_dump(mode="json"),
                evidence=[e.model_dump(mode="json") for e in cand.evidence],
            )
            uow.session.add(row)
            if result.status == "reject":
                routing = route(0.0, policy, gate_failures=result.failures)
                record_outcome(row, routing, gate_failures=result.failures)
            else:
                signals = Signals(
                    source=batch.source_type,
                    grounding=texts.grounding(cand),
                    back_translation=translator.score(cand) if translator else 1.0,
                    stability=v.stability,
                    entity_match=conf,
                    self_confidence=cand.confidence,
                )
                s = score(signals, policy)
                routing = route(s.value, policy, conflict=result.conflict)
                if routing.decision == "commit" and signals.grounding < 1.0:
                    # A non-verbatim quote is never trusted enough to auto-commit (#8):
                    # however reliable the source, a human confirms it.
                    routing = Routing(
                        "review",
                        [
                            *routing.reasons,
                            f"grounding {signals.grounding:.2f} < 1.0 (quote not verbatim): "
                            "needs review",
                        ],
                    )
                record_outcome(row, routing, s)
                if routing.decision == "commit":
                    facts.append(to_fact(cand, spec, s.value))
                elif routing.decision in ("review", "conflict"):
                    row.review_item = ReviewItem(
                        reason="conflict" if routing.decision == "conflict" else "score",
                        conflict_with={"message": result.conflict} if result.conflict else None,
                    )
            if note:
                row.breakdown = {**row.breakdown, "notes": note}
            counts[routing.decision] += 1
        uow.session.flush()
        return counts, facts

    # ----------------------------------------------------------------- review decisions

    def approve(self, item_id: int, reviewer: str, edited: dict[str, Any] | None = None) -> None:
        """Commit a reviewed candidate (optionally edited). For a conflict, approving means the
        new fact *replaces* the conflicting one."""
        with UnitOfWork(self.engine) as uow:
            item = uow.session.get(ReviewItem, item_id)
            if item is None or item.status != "open":
                raise PipelineError(f"review item {item_id} is not open")
            row = item.candidate
            published = uow.ontology_versions.published()
            assert published is not None
            spec = spec_of(published)
            cand = _CANDIDATE.validate_python(edited if edited is not None else row.payload)
            failures, cand = schema_gate(cand, spec)
            if failures:
                raise PipelineError(
                    "edited candidate fails the schema gate: " + "; ".join(failures)
                )
            fact = to_fact(cand, spec, row.score or 0.0, replace_existing=item.reason == "conflict")
            item.status = "edited" if edited is not None else "approved"
            item.reviewer, item.decided_at = reviewer, utcnow()
            row.decision = "commit"
            if edited is not None:
                row.payload = cand.model_dump(mode="json")
            uow.audit(
                reviewer,
                "review.approve",
                "review_item",
                str(item_id),
                {"edited": edited is not None, "reason": item.reason},
            )
            uow.session.flush()
            self.graph.write_run(row.run_id, published.version, [fact])

    def reject(self, item_id: int, reviewer: str, note: str | None = None) -> None:
        with UnitOfWork(self.engine) as uow:
            item = uow.session.get(ReviewItem, item_id)
            if item is None or item.status != "open":
                raise PipelineError(f"review item {item_id} is not open")
            item.status, item.reviewer, item.note, item.decided_at = (
                "rejected",
                reviewer,
                note,
                utcnow(),
            )
            item.candidate.decision = "reject"
            uow.audit(reviewer, "review.reject", "review_item", str(item_id), {"note": note})

    # ----------------------------------------------------------------- rollback

    def rollback(self, run_id: str, actor: str) -> dict[str, int]:
        with UnitOfWork(self.engine) as uow:
            run = uow.session.get(IngestionRun, run_id)
            if run is None or run.status != "completed":
                raise PipelineError(f"run {run_id} is not a completed run")
            removed = self.graph.rollback_run(run_id)
            run.status = "rolled_back"
            uow.audit(actor, "run.rollback", "ingestion_run", run_id, removed)
            return removed
