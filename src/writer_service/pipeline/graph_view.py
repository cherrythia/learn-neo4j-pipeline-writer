"""Read-only view of accepted facts in the graph, used by the constraint gate.

Key matching: the schema gate canonicalises every key (``canonical_text``: whitespace
collapsed) and the writer stores keys in that form, so lookups receive canonical keys. Both
views then match case-insensitively using the same simple lower-casing (`match_key` uses
`str.lower`, mirroring Cypher's `toLower`). ``Neo4jGraphView`` first tries an exact match (served by
the identity-key uniqueness index), then the class's full-text index, and only scans with
``toLower`` when the class has no usable full-text index (see ``neo4j_lookup``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from writer_service.pipeline.candidates import match_key
from writer_service.pipeline.neo4j_lookup import Neo4jLookup

NodeRef = tuple[str, str, str]  # (label, key property, key value)
# One existing node at the far end of an edge: {label: identity-key value} for each of its
# labels that is in the requested ``keys_by_label`` (a node may carry several class labels).
FarNode = dict[str, str]


class GraphView(Protocol):
    def node_properties(self, label: str, key: str, value: str) -> dict[str, Any] | None: ...

    def targets(
        self, relation: str, source: NodeRef, keys_by_label: Mapping[str, str]
    ) -> list[FarNode]:
        """Nodes ``source`` already points to via ``relation`` whose labels are in
        ``keys_by_label`` (label -> identity key)."""
        ...

    def sources(
        self, relation: str, target: NodeRef, keys_by_label: Mapping[str, str]
    ) -> list[FarNode]:
        """Nodes already pointing to ``target`` via ``relation`` (labels as for ``targets``)."""
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
        self, relation: str, source: NodeRef, keys_by_label: Mapping[str, str]
    ) -> list[FarNode]:
        src = (source[0], match_key(source[2]))
        return [
            {t[0]: raw_t}
            for rel, s, t, _, raw_t in self.edges
            if rel == relation and s == src and t[0] in keys_by_label
        ]

    def sources(
        self, relation: str, target: NodeRef, keys_by_label: Mapping[str, str]
    ) -> list[FarNode]:
        tgt = (target[0], match_key(target[2]))
        return [
            {s[0]: raw_s}
            for rel, s, t, raw_s, _ in self.edges
            if rel == relation and t == tgt and s[0] in keys_by_label
        ]


class Neo4jGraphView(Neo4jLookup):
    """Neo4j implementation. Labels, relation types and keys come from a *validated* ontology
    (safe identifiers), so interpolating them is injection-free; values are parameters."""

    def _find(self, label: str, key: str, value: str) -> Any | None:
        return self._find_node(label, key, value)

    def node_properties(self, label: str, key: str, value: str) -> dict[str, Any] | None:
        node = self._find(label, key, value)
        return dict(node) if node is not None else None

    def _far(self, pattern: str, ref: NodeRef, keys_by_label: Mapping[str, str]) -> list[FarNode]:
        node = self._find(*ref)
        if node is None:
            return []
        rows = self._query(
            f"MATCH {pattern} WHERE elementId(n) = $id RETURN labels(m) AS ls, properties(m) AS p",
            id=node.element_id,
        )
        out: list[FarNode] = []
        for row in rows:
            far = {
                label: str(row["p"][keys_by_label[label]])
                for label in row["ls"]
                if label in keys_by_label and keys_by_label[label] in row["p"]
            }
            if far:
                out.append(far)
        return out

    def targets(
        self, relation: str, source: NodeRef, keys_by_label: Mapping[str, str]
    ) -> list[FarNode]:
        return self._far(f"(n:{source[0]})-[:{relation}]->(m)", source, keys_by_label)

    def sources(
        self, relation: str, target: NodeRef, keys_by_label: Mapping[str, str]
    ) -> list[FarNode]:
        return self._far(f"(m)-[:{relation}]->(n:{target[0]})", target, keys_by_label)
