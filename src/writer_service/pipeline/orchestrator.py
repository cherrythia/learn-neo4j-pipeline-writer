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
   its derivation; review and conflict create review items. A **possible duplicate** (a new
   key whose name matches an existing entity), and any relation to it, is routed to review
   (reason ``duplicate``) whatever its score: the reviewer keeps it as a new entity or merges
   it into the existing one (#38).
6. **Commit**: all ``commit`` facts are written to Neo4j in **one transaction** with
   provenance. If the graph write fails nothing is written and the run is marked ``failed``;
   if the governance commit fails after the graph write, the run is rolled back in the graph.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import TypeAdapter
from sqlalchemy import Engine, select

from writer_service.governance.models import Candidate, IngestionRun, ReviewItem, utcnow
from writer_service.governance.uow import UnitOfWork
from writer_service.llm.structured import StructuredLLM
from writer_service.ontology.neo4j_schema import neo4j_label
from writer_service.ontology.service import spec_of
from writer_service.ontology.spec import OntologySpec
from writer_service.pipeline.candidates import (
    Candidate as AnyCandidateModel,
)
from writer_service.pipeline.candidates import (
    CandidateEntity,
    CandidateRelation,
    EntityRef,
    SourceType,
    fact_hash,
    match_key,
)
from writer_service.pipeline.gates import check_batch, schema_gate
from writer_service.pipeline.graph_store import GraphWriter, audit_view, to_fact
from writer_service.pipeline.graph_view import GraphView
from writer_service.pipeline.matching import (
    FUZZY_THRESHOLD,
    EntityMatcher,
    MatchIndex,
    name_similarity,
)
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
        changes: list[dict[str, Any]] = []
        try:
            with UnitOfWork(self.engine) as uow:
                counts, facts = self._process(uow, run_id, batch, spec, version, policy)
                changes = self.graph.write_run(run_id, version, facts)
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
                    if changes:  # values it overwrote on entities other runs also assert
                        self.graph.undo(changes)
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
        # (class, match key of the new entity) -> the existing entity it may duplicate
        duplicates: dict[tuple[str, str], dict[str, Any]] = {}
        aliases = self._merge_aliases(uow, spec)
        # merged-away keys whose incoming entity no longer looks like the merge target (e.g. a
        # reused key): not rewritten, sent to review instead (and so are relations to them)
        blocked = {
            slot
            for v in voted
            if isinstance(v.candidate, CandidateEntity)
            and (slot := _slot(v.candidate, spec)) in aliases
            and not self._alias_fits(v.candidate, aliases[slot][0], spec)
        }
        # ... and keys an earlier run already sent to review for that reason, still undecided:
        # relations to them (often a separate dataset, so a separate run) wait as well
        for slot, info in self._pending_reused_keys(uow).items():
            blocked.add(slot)
            duplicates.setdefault(slot, info)
        # Relations are only rewritten through an alias when this batch carries the entity
        # and it still fits the merge target; a relation alone has no name to check, so it
        # goes to review pointing at the target instead of being rewritten unseen (review #39).
        fitting = {
            slot
            for v in voted
            if isinstance(v.candidate, CandidateEntity)
            and (slot := _slot(v.candidate, spec)) in aliases
            and slot not in blocked
        }
        for slot, (target, item_id, original) in self._alias_details(uow, spec).items():
            if slot in aliases and slot not in fitting and slot not in blocked:
                cls = next(c for c in spec.classes if c.name == slot[0])
                duplicates.setdefault(
                    slot,
                    {
                        "class_name": slot[0],
                        "key_property": cls.identity_key,
                        "candidate_key": original,
                        "key": target,
                        "similarity": 0.0,
                        "merged_earlier": item_id,
                    },
                )
        for v in voted:
            c, alias_note, forced = self._apply_aliases(v.candidate, aliases, fitting, spec)
            if isinstance(c, CandidateEntity):
                m = matcher.match_entity(c) if c.class_name in matcher.classes else None
                conf = m.confidence if m else None
                note = list(alias_note)
                if m is not None and m.status == "possible_duplicate" and m.key is not None:
                    note.append(f"possible duplicate of {c.class_name} {m.key!r}")
                    cls = matcher.classes[c.class_name]
                    new_key = str(c.properties.get(cls.identity_key, ""))
                    duplicates[(c.class_name, match_key(new_key))] = {
                        "class_name": c.class_name,
                        "key_property": cls.identity_key,
                        "candidate_key": new_key,
                        "key": m.key,
                        "similarity": round(1 - (m.confidence or 0.0), 4),
                    }
                if forced is not None:  # the earlier merge is what the reviewer must see
                    slot = (c.class_name, match_key(forced["candidate_key"]))
                    other = duplicates.get(slot)
                    if other is not None and other.get("key") != forced["key"]:
                        forced = {**forced, "also_matches": other["key"]}
                    duplicates[slot] = forced
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
                note = [*alias_note, *note]
                resolved.append(c)
            match_conf.append(conf)
            notes.append(note)

        duplicate_of = [_duplicate_of(c, duplicates) for c in resolved]
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
        for v, result, conf, note, dup in zip(
            voted, results, match_conf, notes, duplicate_of, strict=True
        ):
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
                dup_message = _duplicate_message(dup) if dup else None
                if dup_message and routing.decision in ("commit", "review"):
                    # However strong the fact, a possible duplicate never auto-commits (#38).
                    routing = Routing("review", [*routing.reasons, dup_message])
                record_outcome(row, routing, s)
                if routing.decision == "commit":
                    facts.append(to_fact(cand, spec, s.value))
                elif routing.decision == "conflict":
                    # a conflict can also involve a possible duplicate: keep it, so the
                    # reviewer can merge instead of creating the duplicate (review #39)
                    row.review_item = ReviewItem(
                        reason="conflict",
                        conflict_with={
                            "message": result.conflict,
                            **({"duplicate_of": dup} if dup else {}),
                        },
                    )
                elif dup_message:
                    row.review_item = ReviewItem(
                        reason="duplicate",
                        conflict_with={"message": dup_message, "duplicate_of": dup},
                    )
                elif routing.decision == "review":
                    row.review_item = ReviewItem(reason="score")
            if note:
                row.breakdown = {**row.breakdown, "notes": note}
            counts[routing.decision] += 1
        uow.session.flush()
        return counts, facts

    # ----------------------------------------------------------------- review decisions

    @staticmethod
    def approval_run_id(item_id: int) -> str:
        """Approved facts are written under their own run id, so they don't depend on (and
        aren't removed by a rollback of) the ingestion run that proposed them."""
        return f"approval-{item_id}"

    def _open_item(self, uow: UnitOfWork, item_id: int) -> ReviewItem:
        item = uow.session.get(ReviewItem, item_id)
        if item is None or item.status != "open":
            raise PipelineError(f"review item {item_id} is not open")
        if item.candidate.run.status != "completed":
            raise PipelineError(
                f"review item {item_id} belongs to a run that is {item.candidate.run.status}"
            )
        return item

    def approve(
        self,
        item_id: int,
        reviewer: str,
        edited: dict[str, Any] | None = None,
        *,
        merge: bool = False,
    ) -> None:
        """Commit a reviewed candidate (optionally edited).

        * For a ``duplicate`` item, approving keeps the candidate as a **new** entity;
          ``merge=True`` instead rewrites its key (or the relation's endpoints) to the existing
          entity it may duplicate, so its properties and evidence go onto that entity. Merge
          is allowed on any item that carries ``duplicate_of`` (a conflict can too), only into
          an entity that is in the graph, and always re-runs the constraint gate.
        * A relation to a possible duplicate kept as new needs that entity in the graph first
          (approve its entity item), so approving the relation never creates it as a stub.

        * The schema gate runs again (edits must still fit the ontology).
        * For a ``score`` or ``duplicate`` item (and any merge) the constraint gate runs again
          too: if the graph changed since
          ingest and the fact now contradicts it, the item becomes a ``conflict`` (still open)
          and nothing is written.
        * For a ``conflict`` item, approving means the new fact **replaces** the old one; the
          replaced edges are recorded in the audit log.
        * If the governance commit fails after the graph write, the approval's graph changes
          are rolled back.
        """
        reconflict: str | None = None
        approval_run = self.approval_run_id(item_id)
        graph_written = False
        replaced: list[dict[str, Any]] = []
        try:
            with UnitOfWork(self.engine) as uow:
                item = self._open_item(uow, item_id)
                row = item.candidate
                dup: dict[str, Any] = (item.conflict_with or {}).get("duplicate_of") or {}
                if merge and not dup:
                    raise PipelineError(
                        f"only a possible duplicate can be merged (item {item_id} is a "
                        f"{item.reason} item)"
                    )
                published = uow.ontology_versions.published()
                assert published is not None
                spec = spec_of(published)
                cand = _CANDIDATE.validate_python(edited if edited is not None else row.payload)
                failures, cand = schema_gate(cand, spec)
                if failures:
                    raise PipelineError(
                        "edited candidate fails the schema gate: " + "; ".join(failures)
                    )
                kept: dict[str, Any] = {}
                merged_sides: list[dict[str, Any]] = []
                if merge:
                    cand, kept, merged_sides = self._merged(cand, dup, spec)
                else:
                    self._refuse_keeping_a_merged_key(uow, cand, dup, spec)
                    if dup:
                        cand = self._require_kept_entities(cand, dup, spec)
                if merge or item.reason in ("score", "duplicate"):
                    (result,) = check_batch([cand], spec, self.graph)
                    if result.status == "reject":
                        raise PipelineError(
                            "candidate fails the gates: " + "; ".join(result.failures)
                        )
                    if result.status == "conflict" and merge:
                        # the merged fact contradicts the existing entity: nothing written,
                        # the item stays a duplicate so keeping it as new is still possible
                        item.conflict_with = {
                            **(item.conflict_with or {}),
                            "merge_conflict": result.conflict,
                        }
                        uow.audit(
                            reviewer,
                            "review.reconflict",
                            "review_item",
                            str(item_id),
                            {"conflict": result.conflict, "merge": True},
                        )
                        reconflict = result.conflict
                    elif result.status == "conflict":
                        item.reason = "conflict"
                        item.conflict_with = {
                            **(item.conflict_with or {}),
                            "message": result.conflict,
                        }
                        uow.audit(
                            reviewer,
                            "review.reconflict",
                            "review_item",
                            str(item_id),
                            {"conflict": result.conflict},
                        )
                        reconflict = result.conflict
                if reconflict is None:
                    fact = to_fact(
                        cand,
                        spec,
                        row.score or 0.0,
                        replace_existing=item.reason == "conflict" and not merge,
                    )
                    changed = edited is not None or merge
                    if merge:  # later ingests of the same key go to the merged entity (if it fits)
                        item.conflict_with = {
                            **(item.conflict_with or {}),
                            "merged_into": dup["key"],
                            "merged": merged_sides,
                        }
                    item.status = "edited" if changed else "approved"
                    item.reviewer, item.decided_at = reviewer, utcnow()
                    row.decision = "commit"
                    written = cand.model_dump(mode="json")
                    if written != row.payload:  # edited, merged or key spelling normalised
                        row.payload = written
                    entry = uow.audit(
                        reviewer,
                        "review.approve",
                        "review_item",
                        str(item_id),
                        {
                            "edited": edited is not None,
                            "reason": item.reason,
                            "graph_run": approval_run,
                            **(
                                {
                                    "merged_into": dup["key"],
                                    "merged": merged_sides,
                                    "kept_existing": kept,
                                }
                                if merge
                                else {}
                            ),
                        },
                    )
                    uow.session.flush()
                    replaced = self.graph.write_run(approval_run, published.version, [fact])
                    graph_written = True
                    if replaced:
                        entry.detail = {**entry.detail, "replaced": audit_view(replaced)}
        except Exception as exc:
            if graph_written:  # undo the approval *and* put back what it replaced
                try:
                    self.graph.rollback_run(approval_run)
                    if replaced:
                        self.graph.undo(replaced)
                except Exception as undo_exc:
                    exc.add_note(f"graph compensation also failed: {undo_exc}")
            raise
        if reconflict is not None and merge:
            raise PipelineError(f"merging item {item_id} contradicts the graph: {reconflict}")
        if reconflict is not None:
            raise PipelineError(
                f"the graph changed since ingest; item {item_id} is now a conflict: {reconflict}"
            )

    def _accepted(self, spec: OntologySpec, class_name: str, key: str) -> bool:
        """Is this entity in the graph as an accepted (non-stub) node?"""
        return self._stored_key(spec, class_name, key) is not None

    def _stored_key(self, spec: OntologySpec, class_name: str, key: str) -> str | None:
        """The stored spelling of an accepted (non-stub) entity's key, or None."""
        cls = next(c for c in spec.classes if c.name == class_name)
        props = self.graph.node_properties(neo4j_label(cls), cls.identity_key, key)
        if props is None or props.get("stub", False):
            return None
        return str(props[cls.identity_key])

    def _require_kept_entities(
        self, cand: AnyCandidate, dup: dict[str, Any], spec: OntologySpec
    ) -> AnyCandidate:
        """Keeping a relation to a possible duplicate as new needs that entity accepted
        first; otherwise writing the relation would create it as a stub (review #39). The
        endpoint is rewritten to the stored key: Neo4j keys are case-sensitive."""
        if not isinstance(cand, CandidateRelation):
            return cand
        update: dict[str, EntityRef] = {}
        for side in _sides(dup):
            ref: EntityRef = getattr(cand, side["side"])
            if match_key(ref.key) != match_key(side["candidate_key"]):
                continue  # edited to point elsewhere
            stored = self._stored_key(spec, ref.class_name, ref.key)
            if stored is None:
                raise PipelineError(
                    f"{ref.class_name} {ref.key!r} is not in the graph: approve its entity "
                    f"item first, or merge this relation into {side['key']!r}"
                )
            update[side["side"]] = EntityRef(class_name=ref.class_name, key=stored)
        return cand.model_copy(update=update) if update else cand

    def _merged(
        self, cand: AnyCandidate, dup: dict[str, Any], spec: OntologySpec
    ) -> tuple[AnyCandidate, dict[str, Any], list[dict[str, Any]]]:
        """Point the candidate at the existing entity (every flagged endpoint, for a relation)
        instead of the new key. The target must be an accepted entity in the graph: the
        matcher also compares against new entities of the same batch, which may never have
        been accepted.

        For an entity, where the existing entity already has a different value (the variant
        name that made it look like a duplicate, typically) the existing value is kept; those
        candidate values are returned so the audit log records what was not written. Also
        returned: the rewritten keys (``{class_name, candidate_key, key, side?}``), which later
        ingests use as aliases."""
        if isinstance(cand, CandidateRelation):
            # only sides that still point at the flagged new key (an edit may have changed one)
            targets = [
                t
                for t in _sides(dup)
                if match_key(getattr(cand, t["side"]).key) == match_key(t["candidate_key"])
            ]
            if not targets:
                raise PipelineError("nothing to merge: no endpoint points at the flagged key")
        else:
            targets = [dup]
            cls = next(c for c in spec.classes if c.name == cand.class_name)
            own = str(cand.properties.get(cls.identity_key, ""))
            if match_key(own) != match_key(dup["key"]) and self._accepted(
                spec, cand.class_name, own
            ):
                raise PipelineError(
                    f"{cand.class_name} {own!r} was kept as a new entity: keep this one as new "
                    "too, or edit it"
                )
        for t in targets:
            if not self._accepted(spec, t["class_name"], t["key"]):
                raise PipelineError(
                    f"cannot merge into {t['class_name']} {t['key']!r}: it is not in the graph"
                )
            if (
                isinstance(cand, CandidateRelation)
                and "candidate_key" in t
                and self._accepted(spec, t["class_name"], t["candidate_key"])
            ):
                # its entity item was kept as new: merging the relation would split it
                raise PipelineError(
                    f"{t['class_name']} {t['candidate_key']!r} was kept as a new entity: keep "
                    "this relation as new too, or edit it"
                )
        if isinstance(cand, CandidateRelation):
            update = {
                t["side"]: EntityRef(class_name=t["class_name"], key=t["key"]) for t in targets
            }
            sides = [
                {k: t[k] for k in ("class_name", "candidate_key", "key", "side")} for t in targets
            ]
            return cand.model_copy(update=update), {}, sides
        cls = next(c for c in spec.classes if c.name == cand.class_name)
        key_prop = cls.identity_key
        own_key = str(cand.properties.get(key_prop, ""))
        existing = self.graph.node_properties(neo4j_label(cls), key_prop, dup["key"]) or {}
        props: dict[str, Any] = {key_prop: dup["key"]}
        kept: dict[str, Any] = {}
        for name, value in cand.properties.items():
            if name == key_prop or value is None:
                continue
            if existing.get(name) is not None and match_key(existing[name]) != match_key(value):
                kept[name] = value  # not written: the existing entity's value stays
                props[name] = existing[name]
            else:
                props[name] = value
        side = {"class_name": cand.class_name, "candidate_key": own_key, "key": dup["key"]}
        return cand.model_copy(update={"properties": props}), kept, [side]

    def _merge_aliases(
        self, uow: UnitOfWork, spec: OntologySpec
    ) -> dict[tuple[str, str], tuple[str, int]]:
        """(class, match key of a merged-away key) -> (entity it was merged into, id of the
        latest merging item),
        from approved merges. An alias is only valid while its target is an accepted entity
        and the merged-away key is *not* (once someone keeps that key as a real entity, the
        merge no longer says anything about it)."""
        out: dict[tuple[str, str], tuple[str, int, str]] = {}
        q = select(ReviewItem).where(
            ReviewItem.status == "edited", ReviewItem.reason.in_(("duplicate", "conflict"))
        )
        known = {c.name for c in spec.classes}
        for item in uow.session.scalars(q.order_by(ReviewItem.id)):
            for side in (item.conflict_with or {}).get("merged") or []:
                if side.get("class_name") not in known or not side.get("candidate_key"):
                    continue
                out[(side["class_name"], match_key(side["candidate_key"]))] = (
                    side["key"],
                    item.id,
                    side["candidate_key"],  # original spelling: Neo4j's exact lookup is indexed
                )
        return {
            slot: (key, item_id)
            for slot, (key, item_id, _) in self._alias_details(uow, spec, out).items()
        }

    def _alias_details(
        self,
        uow: UnitOfWork,
        spec: OntologySpec,
        recorded: dict[tuple[str, str], tuple[str, int, str]] | None = None,
    ) -> dict[tuple[str, str], tuple[str, int, str]]:
        """Valid aliases with the merged-away key's original spelling."""
        if recorded is None:
            recorded = {}
            q = select(ReviewItem).where(
                ReviewItem.status == "edited", ReviewItem.reason.in_(("duplicate", "conflict"))
            )
            known = {c.name for c in spec.classes}
            for item in uow.session.scalars(q.order_by(ReviewItem.id)):
                for side in (item.conflict_with or {}).get("merged") or []:
                    if side.get("class_name") in known and side.get("candidate_key"):
                        recorded[(side["class_name"], match_key(side["candidate_key"]))] = (
                            side["key"],
                            item.id,
                            side["candidate_key"],
                        )
        return {
            slot: (key, item_id, original)
            for slot, (key, item_id, original) in recorded.items()
            if self._accepted(spec, slot[0], key) and not self._accepted(spec, slot[0], original)
        }

    @staticmethod
    def _pending_reused_keys(uow: UnitOfWork) -> dict[tuple[str, str], dict[str, Any]]:
        """Keys with an open entity item sent to review because they no longer match an earlier
        merge (``previously_merged``) -> that item's ``duplicate_of``."""
        out: dict[tuple[str, str], dict[str, Any]] = {}
        q = select(ReviewItem).where(
            ReviewItem.status == "open", ReviewItem.reason.in_(("duplicate", "conflict"))
        )
        for item in uow.session.scalars(q):
            dup = (item.conflict_with or {}).get("duplicate_of") or {}
            if dup.get("previously_merged") and "side" not in dup and dup.get("candidate_key"):
                out[(dup["class_name"], match_key(dup["candidate_key"]))] = dict(dup)
        return out

    def _alias_fits(self, cand: CandidateEntity, target: str, spec: OntologySpec) -> bool:
        """Does an incoming entity under a merged-away key still look like the merge target?
        (Same name, as far as the matcher is concerned.) Without names it can't tell: no."""
        cls = next(c for c in spec.classes if c.name == cand.class_name)
        name_prop = EntityMatcher._name_prop(cls)
        name = cand.properties.get(name_prop) if name_prop else None
        existing = self.graph.node_properties(neo4j_label(cls), cls.identity_key, target) or {}
        if not name or not name_prop or not existing.get(name_prop):
            return False
        return name_similarity(str(name), str(existing[name_prop])) >= FUZZY_THRESHOLD

    def _apply_aliases(
        self,
        cand: AnyCandidate,
        aliases: dict[tuple[str, str], tuple[str, int]],
        fitting: set[tuple[str, str]],
        spec: OntologySpec,
    ) -> tuple[AnyCandidate, list[str], dict[str, Any] | None]:
        """Rewrite keys a reviewer already merged into another entity, the way the merge did
        (an entity keeps the existing entity's differing values). A blocked key (the entity
        no longer fits the target) is not rewritten: the entity is returned with a forced
        ``duplicate_of`` so it goes to review, and relations to it follow (via the batch's
        duplicates)."""
        if not aliases:
            return cand, [], None
        if isinstance(cand, CandidateEntity):
            slot = _slot(cand, spec)
            hit = aliases.get(slot) if slot else None
            if slot is None or hit is None:
                return cand, [], None
            cls = next(c for c in spec.classes if c.name == cand.class_name)
            key = str(cand.properties.get(cls.identity_key, ""))
            if slot not in fitting:
                note = (
                    f"{key!r} was merged into {hit[0]!r} earlier (review item {hit[1]}), but "
                    "this entity doesn't match it: needs review"
                )
                forced = {
                    "class_name": cand.class_name,
                    "key_property": cls.identity_key,
                    "candidate_key": key,
                    "key": hit[0],
                    "similarity": 0.0,
                    "previously_merged": hit[1],
                }
                return cand, [note], forced
            dup = {"class_name": cand.class_name, "key_property": cls.identity_key, "key": hit[0]}
            merged, kept, _ = self._merged(cand, dup, spec)
            note = f"{key!r} was merged into {hit[0]!r} (review item {hit[1]})"
            if kept:
                note += f"; kept the existing values of: {', '.join(sorted(kept))}"
            return merged, [note], None
        notes: list[str] = []
        update: dict[str, EntityRef] = {}
        for side in ("source", "target"):
            ref: EntityRef = getattr(cand, side)
            slot = (ref.class_name, match_key(ref.key))
            hit = aliases.get(slot)
            if hit is not None and slot in fitting:
                update[side] = EntityRef(class_name=ref.class_name, key=hit[0])
                notes.append(
                    f"{side} {ref.key!r} was merged into {hit[0]!r} (review item {hit[1]})"
                )
        return (cand.model_copy(update=update) if update else cand), notes, None

    def _refuse_keeping_a_merged_key(
        self, uow: UnitOfWork, cand: AnyCandidate, dup: dict[str, Any], spec: OntologySpec
    ) -> None:
        """Keeping an entity as new under a key a reviewer already merged away would split
        it (the relations went to the merge target). Allowed only on an item that was sent to
        review *because* it no longer matches the merge (``previously_merged``): the reviewer
        then decides the key now names a different entity."""
        if not isinstance(cand, CandidateEntity):
            return
        slot = _slot(cand, spec)
        hit = self._merge_aliases(uow, spec).get(slot) if slot else None
        if hit is None or hit[1] == dup.get("previously_merged"):
            # no merge, or exactly the merge this item was flagged against: the reviewer is
            # deciding the key now names a different entity
            return
        cls = next(c for c in spec.classes if c.name == cand.class_name)
        key = cand.properties.get(cls.identity_key)
        raise PipelineError(
            f"{cand.class_name} {key!r} was merged into {hit[0]!r} (review item {hit[1]}): "
            "merge this one too, or reject it"
        )

    def reject(self, item_id: int, reviewer: str, note: str | None = None) -> None:
        with UnitOfWork(self.engine) as uow:
            item = self._open_item(uow, item_id)
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
        """Remove what the run alone owns from the graph and close its open review items.
        Facts that later runs or approvals also wrote stay (see ``graph_store``)."""
        with UnitOfWork(self.engine) as uow:
            run = uow.session.get(IngestionRun, run_id)
            if run is None or run.status != "completed":
                raise PipelineError(f"run {run_id} is not a completed run")
            removed = self.graph.rollback_run(run_id)
            closed = 0
            for cand in run.candidates:
                item = cand.review_item
                if item is not None and item.status == "open":
                    item.status, item.reviewer, item.decided_at = "rejected", actor, utcnow()
                    item.note = "run rolled back"
                    closed += 1
            run.status = "rolled_back"
            uow.audit(
                actor,
                "run.rollback",
                "ingestion_run",
                run_id,
                {**removed, "review_items_closed": closed},
            )
            return {**removed, "review_items_closed": closed}


# --------------------------------------------------------------------------- duplicates (#38)


def _duplicate_of(
    cand: AnyCandidate, duplicates: dict[tuple[str, str], dict[str, Any]]
) -> dict[str, Any] | None:
    """The existing entity this candidate may duplicate: its own key for an entity, or an
    endpoint for a relation (writing the relation would create the duplicate node)."""
    if isinstance(cand, CandidateEntity):
        return next(
            (
                d
                for (cls, k), d in duplicates.items()
                if cls == cand.class_name
                and k == match_key(str(cand.properties.get(d["key_property"], "")))
            ),
            None,
        )
    sides = [
        {**found, "side": side}
        for side, ref in (("source", cand.source), ("target", cand.target))
        if (found := duplicates.get((ref.class_name, match_key(ref.key)))) is not None
    ]
    # the first flagged side at the top level (shown to the reviewer); all of them in "sides"
    return {**sides[0], "sides": sides} if sides else None


def _sides(dup: dict[str, Any]) -> list[dict[str, Any]]:
    return list(dup.get("sides") or ([dup] if "side" in dup else []))


def _duplicate_message(dup: dict[str, Any]) -> str:
    what = f"{dup['class_name']} {dup['candidate_key']!r}"
    if "side" in dup:
        what = f"{dup['side']} {what}"
    if dup.get("merged_earlier"):
        return (
            f"{what} was merged into {dup['key']!r} earlier (review item "
            f"{dup['merged_earlier']}); nothing in this batch confirms it is the same entity: "
            "needs review"
        )
    if dup.get("previously_merged"):
        return (
            f"{what} was merged into {dup['key']!r} earlier (review item "
            f"{dup['previously_merged']}), but this entity doesn't match it: needs review"
        )
    return (
        f"{what} is a possible duplicate of {dup['class_name']} {dup['key']!r} "
        f"(name similarity {dup['similarity']:.2f}): needs review"
    )


def _slot(cand: CandidateEntity, spec: OntologySpec) -> tuple[str, str] | None:
    cls = next((c for c in spec.classes if c.name == cand.class_name), None)
    if cls is None:
        return None
    return cand.class_name, match_key(str(cand.properties.get(cls.identity_key, "")))
