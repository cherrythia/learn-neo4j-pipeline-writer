"""Writing accepted facts to the graph, with provenance, atomically per run (ADR-0010).

Every written fact carries ``ontology_version``, ``score``, ``run_id`` and ``ingested_at``.
Entities are MERGEd on their identity key and also get their taxonomy parents' labels (an
Issuer node is also an Organisation). Evidence becomes ``(:Evidence)-[:FROM]->(:Source)``:
entities link to it with ``SUPPORTED_BY``; relationships (which can't point at nodes) keep
the evidence ids in an ``evidence_ids`` list property.

**Run ownership.** Every node, relationship and evidence item keeps a ``runs`` list of all
runs that wrote or referenced it. ``rollback_run(run)`` removes the run from those lists and
deletes only what no other run still owns, so rolling back one run never deletes facts a later
run (or a human approval) also stated. Relationship provenance is repaired: ``evidence_ids``
are pruned to evidence that still exists and ``run_id`` falls back to the latest remaining
run. Property *values* a run overwrote are not restored (there is no property history); the
governance store keeps every candidate so a previous value can be found and re-applied.
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
    cardinality: str = "many_to_many"
    replace_existing: bool = False  # approved conflict: drop the edges the cardinality forbids


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
        cardinality=next(r.cardinality for r in spec.relations if r.name == candidate.relation),
        source=(neo4j_label(s), s.identity_key, candidate.source.key),
        target=(neo4j_label(t), t.identity_key, candidate.target.key),
        replace_existing=replace_existing,
    )


def replaced_sides(cardinality: str) -> tuple[bool, bool]:
    """Which existing edges an approved conflict replaces: (source's outgoing, target's
    incoming). many_to_one allows one target per source; one_to_many one source per target."""
    return cardinality in ("many_to_one", "one_to_one"), cardinality in (
        "one_to_many",
        "one_to_one",
    )


class GraphWriter(Protocol):
    def write_run(
        self, run_id: str, ontology_version: int, facts: list[FactWrite]
    ) -> list[dict[str, Any]]:
        """Write all facts in one transaction (all or nothing). Returns the edges removed by
        conflict replacement (for the audit log)."""
        ...

    def rollback_run(self, run_id: str) -> dict[str, int]: ...


# --------------------------------------------------------------------------- in memory


class InMemoryGraph(InMemoryGraphView, InMemoryMatchIndex):
    """GraphView + MatchIndex + GraphWriter over plain dicts, for tests and the demo.
    Same ownership semantics as ``Neo4jGraph``."""

    def __init__(self) -> None:
        InMemoryGraphView.__init__(self)
        InMemoryMatchIndex.__init__(self)
        self.edge_meta: list[dict[str, Any]] = []
        self.evidence: dict[str, dict[str, Any]] = {}
        self.fail_next_write = False

    def node_count(self) -> int:
        return len(self.nodes)

    @staticmethod
    def _own(item: dict[str, Any], run_id: str) -> None:
        runs = item.setdefault("runs", [])
        if run_id not in runs:
            runs.append(run_id)

    def _merge_node(
        self, label: str, key_prop: str, key: str, run_id: str, props: dict[str, Any] | None = None
    ) -> None:
        slot = (label, match_key(key))
        if slot not in self.nodes:
            self.nodes[slot] = {key_prop: key, "stub": props is None}
            self.add(label, key_prop, key, name=None)
        node = self.nodes[slot]
        self._own(node, run_id)
        if props is not None:
            node.update(props)
            node["stub"] = False
            name = props.get("name") or props.get("title")
            if name:
                self._rows[label] = [
                    (k, n) for k, n in self._rows.get(label, []) if match_key(k) != match_key(key)
                ]
                self.add(label, key_prop, key, name=str(name))

    def _drop_edges(self, keep: list[int]) -> list[dict[str, Any]]:
        removed = [
            {"relation": e[0], "source": e[3], "target": e[4]}
            for i, e in enumerate(self.edges)
            if i not in keep
        ]
        self.edges = [self.edges[i] for i in keep]
        self.edge_meta = [self.edge_meta[i] for i in keep]
        return removed

    def write_run(
        self, run_id: str, ontology_version: int, facts: list[FactWrite]
    ) -> list[dict[str, Any]]:
        if self.fail_next_write:
            self.fail_next_write = False
            raise RuntimeError("graph write failed")
        meta = {"ontology_version": ontology_version, "run_id": run_id}
        replaced: list[dict[str, Any]] = []
        for f in facts:
            ids = [evidence_id(e) for e in f.evidence]
            for e, eid in zip(f.evidence, ids, strict=True):
                item = self.evidence.setdefault(
                    eid,
                    {
                        "run_id": run_id,
                        "locator": e.locator,
                        "quote": e.quote,
                        "source": e.source_ref,
                    },
                )
                self._own(item, run_id)
            if f.kind == "entity":
                for label in f.labels:
                    self._merge_node(
                        label, f.key_prop, f.key, run_id, {**f.properties, **meta, "score": f.score}
                    )
                continue
            self._merge_node(f.source[0], f.source[1], f.source[2], run_id)
            self._merge_node(f.target[0], f.target[1], f.target[2], run_id)
            src = (f.source[0], match_key(f.source[2]))
            tgt = (f.target[0], match_key(f.target[2]))
            if f.replace_existing:
                by_source, by_target = replaced_sides(f.cardinality)
                keep = [
                    i
                    for i, e in enumerate(self.edges)
                    if not (
                        e[0] == f.relation
                        and (
                            (by_source and e[1] == src and e[2] != tgt)
                            or (by_target and e[2] == tgt and e[1] != src)
                        )
                    )
                ]
                replaced += self._drop_edges(keep)
            existing = next(
                (
                    i
                    for i, e in enumerate(self.edges)
                    if e[0] == f.relation and e[1] == src and e[2] == tgt
                ),
                None,
            )
            if existing is None:
                self.add_edge(f.relation, f.source, f.target)
                self.edge_meta.append(
                    {
                        **meta,
                        "created_run": run_id,
                        "score": f.score,
                        "evidence_ids": ids,
                        "runs": [run_id],
                    }
                )
            else:
                m = self.edge_meta[existing]
                m.update({**meta, "score": f.score})
                m["evidence_ids"] = sorted(set(m["evidence_ids"]) | set(ids))
                self._own(m, run_id)
        return replaced

    def rollback_run(self, run_id: str) -> dict[str, int]:
        counts = {"nodes": 0, "relationships": 0, "evidence": 0}
        for eid in list(self.evidence):
            item = self.evidence[eid]
            if run_id in item.get("runs", []):
                item["runs"].remove(run_id)
                if not item["runs"]:
                    del self.evidence[eid]
                    counts["evidence"] += 1
        keep = []
        for i, m in enumerate(self.edge_meta):
            runs = m.get("runs", [])
            if run_id in runs:
                runs.remove(run_id)
                if not runs:
                    counts["relationships"] += 1
                    continue
                m["evidence_ids"] = [x for x in m["evidence_ids"] if x in self.evidence]
                if m.get("run_id") == run_id:
                    m["run_id"] = runs[-1]
            keep.append(i)
        self._drop_edges(keep)
        for slot in list(self.nodes):
            node = self.nodes[slot]
            runs = node.get("runs", [])
            if run_id not in runs:
                continue
            runs.remove(run_id)
            if runs:
                if node.get("run_id") == run_id:
                    node["run_id"] = runs[-1]
                continue
            del self.nodes[slot]
            counts["nodes"] += 1
            label, key = slot
            self._rows[label] = [
                (k, n) for k, n in self._rows.get(label, []) if match_key(k) != key
            ]
            dead = [i for i, e in enumerate(self.edges) if e[1] == slot or e[2] == slot]
            self._drop_edges([i for i in range(len(self.edges)) if i not in dead])
        return counts


# --------------------------------------------------------------------------- Neo4j

_OWN = "[x IN coalesce({v}.runs, []) WHERE x <> $run] + $run"


class Neo4jGraph(Neo4jGraphView, Neo4jMatchIndex):
    """GraphView + MatchIndex + GraphWriter on Neo4j. Identifiers come from the validated
    ontology (safe to interpolate); all values are parameters."""

    def __init__(self, driver: Any, database: str = "neo4j") -> None:
        Neo4jGraphView.__init__(self, driver, database)

    def write_run(
        self, run_id: str, ontology_version: int, facts: list[FactWrite]
    ) -> list[dict[str, Any]]:
        replaced: list[dict[str, Any]] = []

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
                evidence_cypher = (
                    "MERGE (s:Source {id: e.source}) SET s.type = e.type, s.ref = e.ref "
                    "MERGE (x:Evidence {id: e.id}) ON CREATE SET x.run_id = $run "
                    f"SET x.locator = e.locator, x.quote = e.quote, x.runs = {_OWN.format(v='x')} "
                    "MERGE (x)-[:FROM]->(s)"
                )
                if f.kind == "entity":
                    label, *parents = f.labels
                    extra = "".join(f" SET n:{p}" for p in parents)
                    tx.run(
                        f"MERGE (n:{label} {{{f.key_prop}: $key}}) "
                        f"SET n += $props, n.stub = false, n.ontology_version = $v, "
                        f"n.score = $score, n.run_id = $run, n.ingested_at = datetime(), "
                        f"n.runs = {_OWN.format(v='n')}{extra} "
                        f"WITH n UNWIND $ev AS e {evidence_cypher} MERGE (n)-[:SUPPORTED_BY]->(x)",
                        key=f.key,
                        props=f.properties,
                        **common,
                    )
                    continue
                (sl, sk, sv), (tl, tk, tv) = f.source, f.target
                if f.replace_existing:
                    by_source, by_target = replaced_sides(f.cardinality)
                    if by_source:
                        res = tx.run(
                            f"MATCH (a:{sl} {{{sk}: $sv}})-[r:{f.relation}]->(b) "
                            f"WHERE NOT (b:{tl} AND b.{tk} = $tv) "
                            "WITH r, properties(b) AS p DELETE r RETURN p",
                            sv=sv,
                            tv=tv,
                        )
                        replaced.extend(
                            {"relation": f.relation, "source": sv, "target": dict(r["p"])}
                            for r in res
                        )
                    if by_target:
                        res = tx.run(
                            f"MATCH (a)-[r:{f.relation}]->(b:{tl} {{{tk}: $tv}}) "
                            f"WHERE NOT (a:{sl} AND a.{sk} = $sv) "
                            "WITH r, properties(a) AS p DELETE r RETURN p",
                            sv=sv,
                            tv=tv,
                        )
                        replaced.extend(
                            {"relation": f.relation, "source": dict(r["p"]), "target": tv}
                            for r in res
                        )
                tx.run(
                    f"MERGE (a:{sl} {{{sk}: $sv}}) ON CREATE SET a.stub = true "
                    f"SET a.runs = {_OWN.format(v='a')} "
                    f"MERGE (b:{tl} {{{tk}: $tv}}) ON CREATE SET b.stub = true "
                    f"SET b.runs = {_OWN.format(v='b')} "
                    f"MERGE (a)-[r:{f.relation}]->(b) ON CREATE SET r.created_run = $run "
                    "SET r.ontology_version = $v, r.score = $score, r.run_id = $run, "
                    f"r.ingested_at = datetime(), r.runs = {_OWN.format(v='r')}, "
                    "r.evidence_ids = "
                    "[i IN coalesce(r.evidence_ids, []) WHERE NOT i IN $ids] + $ids "
                    f"WITH r UNWIND $ev AS e {evidence_cypher}",
                    sv=sv,
                    tv=tv,
                    ids=[e["id"] for e in ev],
                    **common,
                )

        with self.driver.session(database=self.database) as session:
            session.execute_write(work)
        return replaced

    def rollback_run(self, run_id: str) -> dict[str, int]:
        drop = "[x IN {v}.runs WHERE x <> $run]"

        def work(tx: Any) -> dict[str, int]:
            ev = tx.run(
                f"MATCH (x:Evidence) WHERE $run IN x.runs SET x.runs = {drop.format(v='x')} "
                "WITH x WHERE size(x.runs) = 0 DETACH DELETE x RETURN count(x) AS c",
                run=run_id,
            ).single()["c"]
            rels = tx.run(
                f"MATCH ()-[r]->() WHERE $run IN r.runs SET r.runs = {drop.format(v='r')} "
                "WITH r WHERE size(r.runs) = 0 DELETE r RETURN count(r) AS c",
                run=run_id,
            ).single()["c"]
            tx.run(  # repair provenance on relationships other runs still own
                "MATCH ()-[r]->() WHERE r.evidence_ids IS NOT NULL "
                "SET r.evidence_ids = "
                "[i IN r.evidence_ids WHERE EXISTS { MATCH (:Evidence {id: i}) }] "
                "WITH r WHERE r.run_id = $run SET r.run_id = last(r.runs)",
                run=run_id,
            )
            nodes = tx.run(
                f"MATCH (n) WHERE $run IN n.runs SET n.runs = {drop.format(v='n')} "
                "WITH n WHERE size(n.runs) = 0 DETACH DELETE n RETURN count(n) AS c",
                run=run_id,
            ).single()["c"]
            tx.run(
                "MATCH (n) WHERE n.run_id = $run AND size(n.runs) > 0 SET n.run_id = last(n.runs)",
                run=run_id,
            )
            tx.run("MATCH (s:Source) WHERE NOT ()-[:FROM]->(s) DELETE s")
            tx.run("MATCH (r:IngestionRun {id: $run}) DETACH DELETE r", run=run_id)
            return {"nodes": nodes, "relationships": rels, "evidence": ev}

        with self.driver.session(database=self.database) as session:
            result: dict[str, int] = session.execute_write(work)
            return result
