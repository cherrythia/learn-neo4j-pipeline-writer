"""Read-only view of accepted facts in the graph, used by the constraint gate.

Keys are compared with ``match_key`` (whitespace/case-insensitive) on both sides.
"""

from __future__ import annotations

from typing import Any, Protocol

from writer_service.pipeline.candidates import match_key

NodeRef = tuple[str, str, str]  # (label, key property, key value)


class GraphView(Protocol):
    def node_properties(self, label: str, key: str, value: str) -> dict[str, Any] | None: ...

    def targets(
        self, relation: str, source: NodeRef, target_label: str, target_key: str
    ) -> set[str]:
        """Key values (raw) of nodes ``source`` already points to via ``relation``."""
        ...

    def sources(
        self, relation: str, target: NodeRef, source_label: str, source_key: str
    ) -> set[str]:
        """Key values (raw) of nodes already pointing to ``target`` via ``relation``."""
        ...


class InMemoryGraphView:
    def __init__(self) -> None:
        self.nodes: dict[tuple[str, str], dict[str, Any]] = {}
        self.edges: list[tuple[str, tuple[str, str], tuple[str, str], str, str]] = []

    def add_node(self, label: str, key: str, value: str, properties: dict[str, Any]) -> None:
        self.nodes[(label, match_key(value))] = dict(properties)

    def add_edge(self, relation: str, source: NodeRef, target: NodeRef) -> None:
        self.edges.append(
            (
                relation,
                (source[0], match_key(source[2])),
                (target[0], match_key(target[2])),
                source[2],
                target[2],
            )
        )

    def node_properties(self, label: str, key: str, value: str) -> dict[str, Any] | None:
        return self.nodes.get((label, match_key(value)))

    def targets(
        self, relation: str, source: NodeRef, target_label: str, target_key: str
    ) -> set[str]:
        src = (source[0], match_key(source[2]))
        return {
            raw_t
            for rel, s, t, _, raw_t in self.edges
            if rel == relation and s == src and t[0] == target_label
        }

    def sources(
        self, relation: str, target: NodeRef, source_label: str, source_key: str
    ) -> set[str]:
        tgt = (target[0], match_key(target[2]))
        return {
            raw_s
            for rel, s, t, raw_s, _ in self.edges
            if rel == relation and t == tgt and s[0] == source_label
        }


class Neo4jGraphView:
    """Neo4j implementation. Labels, relation types and keys come from a *validated* ontology
    (safe identifiers), so interpolating them is injection-free; values are parameters."""

    def __init__(self, driver: Any, database: str = "neo4j") -> None:
        self.driver = driver
        self.database = database

    def _query(self, cypher: str, **params: Any) -> list[Any]:
        records, _, _ = self.driver.execute_query(cypher, database_=self.database, **params)
        return list(records)

    def node_properties(self, label: str, key: str, value: str) -> dict[str, Any] | None:
        rows = self._query(
            f"MATCH (n:{label}) WHERE toLower(trim(toString(n.{key}))) = $v RETURN n LIMIT 1",
            v=match_key(value),
        )
        return dict(rows[0]["n"]) if rows else None

    def targets(
        self, relation: str, source: NodeRef, target_label: str, target_key: str
    ) -> set[str]:
        s_label, s_key, s_value = source
        rows = self._query(
            f"MATCH (s:{s_label})-[:{relation}]->(t:{target_label}) "
            f"WHERE toLower(trim(toString(s.{s_key}))) = $v RETURN t.{target_key} AS k",
            v=match_key(s_value),
        )
        return {str(r["k"]) for r in rows}

    def sources(
        self, relation: str, target: NodeRef, source_label: str, source_key: str
    ) -> set[str]:
        t_label, t_key, t_value = target
        rows = self._query(
            f"MATCH (s:{source_label})-[:{relation}]->(t:{t_label}) "
            f"WHERE toLower(trim(toString(t.{t_key}))) = $v RETURN s.{source_key} AS k",
            v=match_key(t_value),
        )
        return {str(r["k"]) for r in rows}
