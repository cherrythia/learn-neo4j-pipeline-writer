"""Neo4j entity lookups shared by the graph view and the entity matcher (#28).

Both used to scan a whole label: the matcher read every name of a class once per batch, and a
key that missed the exact (uniqueness-indexed) match was looked up with
``toLower(n.key) = $v`` over every node. With the ontology's full-text indexes
(``ontology.neo4j_schema.desired_indexes``) they ask the index instead:

* **key lookup**: exact match first (served by the uniqueness constraint); otherwise a
  field-scoped phrase query on the full-text index, then an exact case-insensitive check in
  Python (``match_key``) on the few hits, so results are the same as the old scan.
* **name candidates**: the ``TOP_K`` best full-text hits for the reference's words (longer
  words fuzzy, to tolerate typos); the matcher ranks them with its own similarity.

When a class has no ONLINE full-text index for the field (not yet synced, still populating,
or a non-string identity key) the old scan is used, so results never depend on the index.
Labels and property names come from the validated ontology (safe to interpolate).
"""

from __future__ import annotations

import re
from typing import Any

from writer_service.pipeline.candidates import canonical_text, match_key

TOP_K = 50  # name candidates per reference; the matcher only needs the best few
_WORD = re.compile(r"\w+")
_FUZZY_MIN_LEN = 4


def key_query(field: str, value: str) -> str | None:
    """Lucene phrase query for ``value`` in ``field`` (None if it has no word characters)."""
    if not _WORD.search(value):
        return None
    escaped = value.lower().replace("\\", "\\\\").replace('"', '\\"')
    return f'{field}:"{escaped}"'


def name_query(field: str, text: str) -> str | None:
    """Lucene query matching any word of ``text`` in ``field``; words of 4+ characters match
    within one edit. Lower-cased, so words like ``and``/``or`` are never operators."""
    words = _WORD.findall(text.lower())
    if not words:
        return None
    terms = [f"{w}~1" if len(w) >= _FUZZY_MIN_LEN else w for w in words]
    return f"{field}:({' OR '.join(terms)})"


class Neo4jLookup:
    """Mixin for classes that read entities from Neo4j (``driver`` + ``database``)."""

    driver: Any
    database: str

    def __init__(self, driver: Any, database: str = "neo4j") -> None:
        self.driver = driver
        self.database = database

    def _query(self, cypher: str, **params: Any) -> list[Any]:
        records, _, _ = self.driver.execute_query(cypher, database_=self.database, **params)
        return list(records)

    def _fulltext_index(self, label: str, field: str) -> str | None:
        """Name of an ONLINE managed full-text index on ``label`` covering ``field``. Read
        once per instance (the service builds one graph per request)."""
        cache: dict[tuple[str, str], str] | None = getattr(self, "_fulltext_cache", None)
        if cache is None:
            cache = {}
            rows = self._query(
                "SHOW FULLTEXT INDEXES YIELD name, labelsOrTypes, properties, state "
                "WHERE state = 'ONLINE' AND name STARTS WITH 'wsf_' "
                "RETURN name, labelsOrTypes AS labels, properties"
            )
            for r in rows:
                for lab in r["labels"]:
                    for prop in r["properties"]:
                        cache.setdefault((lab, prop), r["name"])
            self._fulltext_cache = cache
        return cache.get((label, field))

    def _find_node(self, label: str, key_prop: str, value: str) -> Any | None:
        """The node of ``label`` whose ``key_prop`` equals ``value`` case-insensitively."""
        rows = self._query(
            f"MATCH (n:{label}) WHERE n.{key_prop} = $v RETURN n LIMIT 1",
            v=canonical_text(value),
        )
        if rows:
            return rows[0]["n"]
        index = self._fulltext_index(label, key_prop)
        query = key_query(key_prop, canonical_text(value)) if index else None
        if index is None or query is None:  # no usable index: scan the label
            rows = self._query(
                f"MATCH (n:{label}) WHERE toLower(toString(n.{key_prop})) = $v RETURN n LIMIT 1",
                v=match_key(value),
            )
            return rows[0]["n"] if rows else None
        rows = self._query(
            "CALL db.index.fulltext.queryNodes($index, $q) YIELD node "
            f"WHERE node:{label} RETURN node AS n LIMIT $k",
            index=index,
            q=query,
            k=TOP_K,
        )
        want = match_key(value)
        return next(
            (
                r["n"]
                for r in rows
                if r["n"].get(key_prop) is not None and match_key(str(r["n"][key_prop])) == want
            ),
            None,
        )

    def _name_candidates(
        self, label: str, key_prop: str, name_prop: str, text: str
    ) -> list[tuple[str, str]]:
        index = self._fulltext_index(label, name_prop)
        query = name_query(name_prop, text) if index else None
        if index is None or query is None:  # no usable index: every name of the label
            rows = self._query(
                f"MATCH (n:{label}) WHERE n.{name_prop} IS NOT NULL "
                f"RETURN n.{key_prop} AS k, n.{name_prop} AS name"
            )
        else:
            rows = self._query(
                "CALL db.index.fulltext.queryNodes($index, $q) YIELD node, score "
                f"WHERE node:{label} AND node.{name_prop} IS NOT NULL "
                f"RETURN node.{key_prop} AS k, node.{name_prop} AS name "
                "ORDER BY score DESC LIMIT $k",
                index=index,
                q=query,
                k=TOP_K,
            )
        return [(str(r["k"]), str(r["name"])) for r in rows if r["k"] is not None]
