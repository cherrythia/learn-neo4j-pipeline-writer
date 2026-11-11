"""Writing accepted facts to the graph, with provenance, atomically per run (ADR-0010).

Every written fact carries ``ontology_version``, ``score``, ``run_id`` and ``ingested_at``.
Entities are MERGEd on their identity key and also get their taxonomy parents' labels (an
Issuer node is also an Organisation). Evidence becomes ``(:Evidence)-[:FROM]->(:Source)``:
entities link to it with ``SUPPORTED_BY``; relationships (which can't point at nodes) keep
the evidence ids in an ``evidence_ids`` list property.

``rollback_run`` removes what a run *created* (nodes, relationships, evidence). Properties a
run overwrote on pre-existing nodes are not restored: there is no property history; the
governance store keeps every candidate so the previous value can be found and re-applied.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from writer_service.ontology.neo4j_schema import neo4j_label
from writer_service.ontology.spec import OntologySpec
from writer_service.pipeline.candidates import (
    CandidateEntity,
    CandidateRelation,
    Evidence,
    match_key,
)
from writer_service.pipeline.graph_view import InMemoryGraphView, Neo4jGraphView
from writer_service.pipeline.matching import InMemoryMatchIndex, Neo4jMatchIndex


def evidence_id(e: Evidence) -> str:
    """Deterministic, so re-ingesting the same document merges instead of duplicating."""
    raw = f"{e.source_type}|{e.source_ref}|{e.locator}|{e.quote}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def source_id(e: Evidence) -> str:
    return hashlib.sha256(f"{e.source_type}|{e.source_ref}".encode()).hexdigest()[:32]


def _scalar(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


@dataclass(frozen=True)
class FactWrite:
    kind: Literal["entity", "relation"]
    score: float
    evidence: tuple[Evidence, ...]
    # entity
    labels: tuple[str, ...] = ()
    key_prop: str = ""
    key: str = ""
    properties: dict[str, Any] = field(default_factory=dict)
    # relation
    relation: str = ""
    source: tuple[str, str, str] = ("", "", "")  # (label, key prop, key)
    target: tuple[str, str, str] = ("", "", "")
    replace_existing: bool = False  # approved conflict: drop the source's other edges first


def _ancestor_labels(spec: OntologySpec, class_name: str) -> tuple[str, ...]:
    by_name = {c.name: c for c in spec.classes}
    labels, node = [], by_name.get(class_name)
    while node is not None and neo4j_label(node) not in labels:
        labels.append(neo4j_label(node))
        node = by_name.get(node.parent) if node.parent else None
    return tuple(labels)


def to_fact(
    candidate: CandidateEntity | CandidateRelation,
    spec: OntologySpec,
    score: float,
    *,
    replace_existing: bool = False,
) -> FactWrite:
    by_name = {c.name: c for c in spec.classes}
    if isinstance(candidate, CandidateEntity):
        cls = by_name[candidate.class_name]
        return FactWrite(
            kind="entity",
            score=score,
            evidence=tuple(candidate.evidence),
            labels=_ancestor_labels(spec, cls.name),
            key_prop=cls.identity_key,
            key=str(candidate.properties[cls.identity_key]),
            properties={k: _scalar(v) for k, v in candidate.properties.items() if v is not None},
        )
    s, t = by_name[candidate.source.class_name], by_name[candidate.target.class_name]
    return FactWrite(
        kind="relation",
        score=score,
        evidence=tuple(candidate.evidence),
        relation=candidate.relation,
        source=(neo4j_label(s), s.identity_key, candidate.source.key),
        target=(neo4j_label(t), t.identity_key, candidate.target.key),
        replace_existing=replace_existing,
    )


class GraphWriter(Protocol):
    def write_run(self, run_id: str, ontology_version: int, facts: list[FactWrite]) -> None:
        """Write all facts in one transaction (all or nothing)."""
        ...

    def rollback_run(self, run_id: str) -> dict[str, int]: ...


# --------------------------------------------------------------------------- in memory


class InMemoryGraph(InMemoryGraphView, InMemoryMatchIndex):
    """GraphView + MatchIndex + GraphWriter over plain dicts, for tests and the demo."""

    def __init__(self) -> None:
        InMemoryGraphView.__init__(self)
        InMemoryMatchIndex.__init__(self)
        self.labels: dict[tuple[str, str], set[str]] = {}
        self.created_by: dict[tuple[str, str], str] = {}
        self.edge_meta: list[dict[str, Any]] = []
        self.evidence: dict[str, dict[str, Any]] = {}
        self.fail_next_write = False

    def node_count(self) -> int:
        return len(self.nodes)

    def _merge_node(
        self, label: str, key_prop: str, key: str, run_id: str, props: dict[str, Any] | None = None
    ) -> None:
        slot = (label, match_key(key))
        if slot not in self.nodes:
            self.nodes[slot] = {key_prop: key, "stub": props is None}
            self.created_by[slot] = run_id
            self.add(label, key_prop, key, name=None)
        if props is not None:
            self.nodes[slot].update(props)
            self.nodes[slot]["stub"] = False
            name = props.get("name") or props.get("title")
            if name:
                self._rows[label] = [
                    (k, n) for k, n in self._rows.get(label, []) if match_key(k) != match_key(key)
                ]
                self.add(label, key_prop, key, name=str(name))

    def write_run(self, run_id: str, ontology_version: int, facts: list[FactWrite]) -> None:
        if self.fail_next_write:
            self.fail_next_write = False
            raise RuntimeError("graph write failed")
        meta = {"ontology_version": ontology_version, "run_id": run_id}
        for f in facts:
            ids = [evidence_id(e) for e in f.evidence]
            for e, eid in zip(f.evidence, ids, strict=True):
                self.evidence.setdefault(
                    eid,
                    {
                        "run_id": run_id,
                        "locator": e.locator,
                        "quote": e.quote,
                        "source": e.source_ref,
                    },
                )
            if f.kind == "entity":
                for label in f.labels:
                    self._merge_node(
                        label, f.key_prop, f.key, run_id, {**f.properties, **meta, "score": f.score}
                    )
                self.labels[(f.labels[0], match_key(f.key))] = set(f.labels)
                continue
            self._merge_node(f.source[0], f.source[1], f.source[2], run_id)
            self._merge_node(f.target[0], f.target[1], f.target[2], run_id)
            if f.replace_existing:
                src = (f.source[0], match_key(f.source[2]))
                keep = [
                    i for i, e in enumerate(self.edges) if not (e[0] == f.relation and e[1] == src)
                ]
                self.edges = [self.edges[i] for i in keep]
                self.edge_meta = [self.edge_meta[i] for i in keep]
            existing = next(
                (
                    i
                    for i, e in enumerate(self.edges)
                    if e[0] == f.relation
                    and e[1] == (f.source[0], match_key(f.source[2]))
                    and e[2] == (f.target[0], match_key(f.target[2]))
                ),
                None,
            )
            if existing is None:
                self.add_edge(f.relation, f.source, f.target)
                self.edge_meta.append(
                    {**meta, "created_run": run_id, "score": f.score, "evidence_ids": ids}
                )
            else:
                m = self.edge_meta[existing]
                m.update({**meta, "score": f.score})
                m["evidence_ids"] = sorted(set(m["evidence_ids"]) | set(ids))

    def rollback_run(self, run_id: str) -> dict[str, int]:
        keep = [i for i, m in enumerate(self.edge_meta) if m["created_run"] != run_id]
        removed_edges = len(self.edges) - len(keep)
        self.edges = [self.edges[i] for i in keep]
        self.edge_meta = [self.edge_meta[i] for i in keep]
        doomed = [slot for slot, r in self.created_by.items() if r == run_id]
        for slot in doomed:
            self.nodes.pop(slot, None)
            self.created_by.pop(slot, None)
            label, key = slot
            self._rows[label] = [
                (k, n) for k, n in self._rows.get(label, []) if match_key(k) != key
            ]
            self.edges = [e for e in self.edges if e[1] != slot and e[2] != slot]
        ev = [k for k, v in self.evidence.items() if v["run_id"] == run_id]
        for k in ev:
            del self.evidence[k]
        return {"nodes": len(doomed), "relationships": removed_edges, "evidence": len(ev)}


# --------------------------------------------------------------------------- Neo4j


class Neo4jGraph(Neo4jGraphView, Neo4jMatchIndex):
    """GraphView + MatchIndex + GraphWriter on Neo4j. Identifiers come from the validated
    ontology (safe to interpolate); all values are parameters."""

    def __init__(self, driver: Any, database: str = "neo4j") -> None:
        Neo4jGraphView.__init__(self, driver, database)

    def write_run(self, run_id: str, ontology_version: int, facts: list[FactWrite]) -> None:
        def work(tx: Any) -> None:
            tx.run(
                "MERGE (r:IngestionRun {id: $run}) SET r.ontology_version = $v, "
                "r.written_at = datetime()",
                run=run_id,
                v=ontology_version,
            )
            for f in facts:
                ev = [
                    {
                        "id": evidence_id(e),
                        "source": source_id(e),
                        "type": e.source_type,
                        "ref": e.source_ref,
                        "locator": e.locator,
                        "quote": e.quote,
                    }
                    for e in f.evidence
                ]
                common = {"run": run_id, "v": ontology_version, "score": f.score, "ev": ev}
                if f.kind == "entity":
                    label, *parents = f.labels
                    extra = "".join(f" SET n:{p}" for p in parents)
                    tx.run(
                        f"MERGE (n:{label} {{{f.key_prop}: $key}}) "
                        "ON CREATE SET n.created_run = $run "
                        f"SET n += $props, n.stub = false, n.ontology_version = $v, "
                        f"n.score = $score, n.run_id = $run, n.ingested_at = datetime(){extra} "
                        "WITH n UNWIND $ev AS e "
                        "MERGE (s:Source {id: e.source}) SET s.type = e.type, s.ref = e.ref "
                        "MERGE (x:Evidence {id: e.id}) ON CREATE SET x.run_id = $run "
                        "SET x.locator = e.locator, x.quote = e.quote "
                        "MERGE (x)-[:FROM]->(s) MERGE (n)-[:SUPPORTED_BY]->(x)",
                        key=f.key,
                        props=f.properties,
                        **common,
                    )
                    continue
                (sl, sk, sv), (tl, tk, tv) = f.source, f.target
                if f.replace_existing:
                    tx.run(f"MATCH (a:{sl} {{{sk}: $sv}})-[r:{f.relation}]->() DELETE r", sv=sv)
                tx.run(
                    f"MERGE (a:{sl} {{{sk}: $sv}}) "
                    "ON CREATE SET a.stub = true, a.created_run = $run "
                    f"MERGE (b:{tl} {{{tk}: $tv}}) "
                    "ON CREATE SET b.stub = true, b.created_run = $run "
                    f"MERGE (a)-[r:{f.relation}]->(b) ON CREATE SET r.created_run = $run "
                    "SET r.ontology_version = $v, r.score = $score, r.run_id = $run, "
                    "r.ingested_at = datetime(), "
                    "r.evidence_ids = "
                    "[i IN coalesce(r.evidence_ids, []) WHERE NOT i IN $ids] + $ids "
                    "WITH r UNWIND $ev AS e "
                    "MERGE (s:Source {id: e.source}) SET s.type = e.type, s.ref = e.ref "
                    "MERGE (x:Evidence {id: e.id}) ON CREATE SET x.run_id = $run "
                    "SET x.locator = e.locator, x.quote = e.quote MERGE (x)-[:FROM]->(s)",
                    sv=sv,
                    tv=tv,
                    ids=[e["id"] for e in ev],
                    **common,
                )

        with self.driver.session(database=self.database) as session:
            session.execute_write(work)

    def rollback_run(self, run_id: str) -> dict[str, int]:
        def work(tx: Any) -> dict[str, int]:
            rels = tx.run(
                "MATCH ()-[r {created_run: $run}]->() DELETE r RETURN count(r) AS c", run=run_id
            ).single()["c"]
            nodes = tx.run(
                "MATCH (n {created_run: $run}) DETACH DELETE n RETURN count(n) AS c", run=run_id
            ).single()["c"]
            ev = tx.run(
                "MATCH (x:Evidence {run_id: $run}) DETACH DELETE x RETURN count(x) AS c", run=run_id
            ).single()["c"]
            tx.run("MATCH (r:IngestionRun {id: $run}) DETACH DELETE r", run=run_id)
            return {"nodes": nodes, "relationships": rels, "evidence": ev}

        with self.driver.session(database=self.database) as session:
            result: dict[str, int] = session.execute_write(work)
            return result
