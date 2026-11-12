"""Neo4j entity lookups shared by the graph view and the entity matcher (#28).

Both used to scan a whole label: the matcher read every name of a class once per batch, and a
key that missed the exact (uniqueness-indexed) match was looked up with
``toLower(n.key) = $v`` over every node. With the ontology's full-text indexes
(``ontology.neo4j_schema.desired_indexes``) they ask the index instead:

* **key lookup**: exact match first (served by the uniqueness constraint); otherwise a
  field-scoped phrase query on the full-text index, then an exact case-insensitive check in
  Python (``match_key``) on the few hits, so results are the same as the old scan.
* **name candidates**: the ``TOP_K`` best full-text hits for the reference: the whole text
  through the index's analyzer (so apostrophes, CJK and accents tokenise exactly as the
  stored names did) OR fuzzy terms for its ASCII-folded words (typos); the matcher ranks the
  hits with its own similarity.

The indexes use the ``standard-folding`` analyzer (lower-case + ASCII folding: "Müller" is
stored as "muller"). Analysed clauses are sent only lower-cased and the analyzer folds them;
fuzzy terms, which Lucene doesn't analyse, are folded in Python (ASCII words only).

The old scan is used when the class has no ONLINE full-text index for the field (not yet
synced, still populating, or a non-string identity key), when the query fails (e.g. too many
clauses), and for a key lookup whose result was capped without an exact hit. Name candidates
are the index's best ``TOP_K``: a name the scan would have found can only be missed if more
than ``TOP_K`` names share the reference's words and outrank it, which the golden test
checks doesn't happen on realistic data.
Labels and property names come from the validated ontology (safe to interpolate).
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any

from neo4j.exceptions import ClientError

from writer_service.pipeline.candidates import canonical_text, match_key

log = logging.getLogger(__name__)

TOP_K = 50  # name candidates per reference; the matcher only needs the best few
MAX_WORDS = 16  # fuzzy terms per query (Lucene caps a query at 1024 clauses)
MAX_TEXT_WORDS = 64  # words of the analysed clause
_WORD = re.compile(r"\w+")
_ASCII_WORD = re.compile(r"[a-z0-9]+")
_FUZZY_MIN_LEN = 4
# Lucene query syntax characters, escaped with a backslash
_LUCENE_SPECIAL = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/])')


def lower(text: str) -> str:
    """Lower-case for a query the analyzer will see. Only lower-cased (so ``AND``/``OR`` are
    never operators): the analyzer does its own folding, and decomposing here would break
    what it keeps whole (Hangul syllables, kana voicing marks, "™"). Python lower-cases "İ"
    to "i" + a combining dot, which the index never stores, so that becomes "i"."""
    return unicodedata.normalize("NFC", text.lower().replace("i\u0307", "i"))


def fold(text: str) -> str:
    """Lower-case and strip accents, approximating ``standard-folding`` for the ASCII fuzzy
    terms only (Lucene doesn't analyse fuzzy terms)."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def key_query(field: str, value: str) -> str | None:
    """Lucene phrase query for ``value`` in ``field`` (None if it has no word characters)."""
    if not _WORD.search(value):
        return None
    # the analyzer folds; the caller checks the hits exactly with match_key
    escaped = lower(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'{field}:"{escaped}"'


def name_query(field: str, text: str) -> str | None:
    """Lucene query for names like ``text``: the whole text, analysed by the index's analyzer
    (escaped), OR fuzzy terms for its first ``MAX_WORDS`` distinct ASCII-folded words of 4+
    characters (within one edit). Words are lower-case, so ``and``/``or`` are never
    operators."""
    if not _WORD.search(text):
        return None
    head = " ".join(lower(canonical_text(text)).split()[:MAX_TEXT_WORDS])
    clauses = [f"{field}:({_LUCENE_SPECIAL.sub(r'\\\1', head)})"]
    words = list(
        dict.fromkeys(w for w in _ASCII_WORD.findall(fold(text)) if len(w) >= _FUZZY_MIN_LEN)
    )
    if words:
        clauses.append(f"{field}:({' OR '.join(f'{w}~1' for w in words[:MAX_WORDS])})")
    return " OR ".join(clauses)


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
        hits = self._fulltext(index, query) if index and query else None
        want = match_key(value)
        if hits is not None:
            hit = next(
                (
                    r["node"]
                    for r in hits
                    if r["node"].get(key_prop) is not None
                    and match_key(str(r["node"][key_prop])) == want
                ),
                None,
            )
            if hit is not None or len(hits) < TOP_K:
                return hit
            # capped without an exact hit: the key may be further down; scan to be sure
        rows = self._query(
            f"MATCH (n:{label}) WHERE toLower(toString(n.{key_prop})) = $v RETURN n LIMIT 1",
            v=want,
        )
        return rows[0]["n"] if rows else None

    def _fulltext(self, index: str, query: str) -> list[Any] | None:
        """The index's best ``TOP_K`` hits, or None if the query fails (the caller scans)."""
        try:
            return self._query(
                "CALL db.index.fulltext.queryNodes($index, $q, {limit: $k}) YIELD node RETURN node",
                index=index,
                q=query,
                k=TOP_K,
            )
        except ClientError as exc:  # e.g. too many clauses, or a malformed query
            log.warning("full-text query on %s failed, scanning instead: %s", index, exc)
            return None

    def _name_candidates(
        self, label: str, key_prop: str, name_prop: str, text: str
    ) -> list[tuple[str, str]]:
        index = self._fulltext_index(label, name_prop)
        query = name_query(name_prop, text) if index else None
        hits = self._fulltext(index, query) if index and query else None
        if hits is not None:
            pairs = [(h["node"].get(key_prop), h["node"].get(name_prop)) for h in hits]
        else:  # no usable index: every name of the label
            rows = self._query(
                f"MATCH (n:{label}) WHERE n.{name_prop} IS NOT NULL "
                f"RETURN n.{key_prop} AS k, n.{name_prop} AS name"
            )
            pairs = [(r["k"], r["name"]) for r in rows]
        return [(str(k), str(n)) for k, n in pairs if k is not None and n is not None]
