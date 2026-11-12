"""Neo4j entity lookups shared by the graph view and the entity matcher (#28).

Both used to scan a whole label: the matcher read every name of a class once per batch, and a
key that missed the exact (uniqueness-indexed) match was looked up with
``toLower(n.key) = $v`` over every node. With the ontology's full-text indexes
(``ontology.neo4j_schema.desired_indexes``) they ask the index instead:

* **key lookup**: exact match first (served by the uniqueness constraint); otherwise, for
  ASCII keys with a non-stop-word token, a field-scoped phrase query on the full-text index,
  then an exact case-insensitive check in Python (``match_key``) on the few hits. Other keys
  use the old scan (see ``key_query``).
* **name candidates**: the ``TOP_K`` best full-text hits for the reference: the whole text
  through the index's analyzer (so apostrophes, CJK and accents tokenise exactly as the
  stored names did) OR typo-tolerant fuzzy terms (``_fuzzy_terms``); the matcher ranks the
  hits with its own similarity.

The indexes use the ``standard-folding`` analyzer (lower-case + ASCII folding: "Müller" is
stored as "muller"). Analysed clauses are sent only lower-cased and the analyzer folds them;
fuzzy terms (Latin/Greek/Cyrillic words, joined pairs, prefixes), which Lucene doesn't
analyse, are built in Python (``_fuzzy_terms``).

The old scan is used for classes under ``SCAN_BELOW`` nodes (identical results where a scan
is cheap), when the class has no ONLINE full-text index for the field (not yet synced, still
populating, or a non-string identity key), when the query fails (e.g. too many clauses), and
for a key lookup whose result was capped without an exact hit. Above ``SCAN_BELOW`` the name
candidates are a recall approximation (see docs/matching.md, "Limit").
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
MAX_WORDS = 16  # fuzzy words per query; at most 4 terms each (Lucene caps clauses at 1024)
MAX_TEXT_WORDS = 64  # words of the analysed clause
_WORD = re.compile(r"\w+")
_ASCII_WORD = re.compile(r"[a-z0-9]+")
_FUZZY_MIN_LEN = 4
_PREFIX_MIN_LEN = 8  # words this long also match as a prefix (names written as one word)
_PREFIX_LEN = 5
# Below this many nodes a class is matched by the old full scan (exact old behaviour, and
# cheap); the full-text index is for the scale where scanning is the problem.
SCAN_BELOW = 5000
# Lucene's English stop words, which the ``standard-folding`` analyzer drops: a key made only
# of these has no tokens in the index, so it can only be found by the scan
STOP_WORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "for",
        "if",
        "in",
        "into",
        "is",
        "it",
        "no",
        "not",
        "of",
        "on",
        "or",
        "such",
        "that",
        "the",
        "their",
        "then",
        "there",
        "these",
        "they",
        "this",
        "to",
        "was",
        "will",
        "with",
    ]
)
# Lucene query syntax characters, escaped with a backslash
_LUCENE_SPECIAL = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/])')


def lower(text: str) -> str:
    """Lower-case for a query the analyzer will see. Only lower-cased (so ``AND``/``OR`` are
    never operators): the analyzer does its own folding, and decomposing here would break
    what it keeps whole (Hangul syllables, kana voicing marks, "™"), and so would
    normalising (NFD text is stored decomposed). Python lower-cases "İ" to "i" + a combining
    dot, which the index never stores, so that becomes "i"."""
    return text.lower().replace("i\u0307", "i")


def fold(text: str) -> str:
    """Lower-case and strip accents, approximating ``standard-folding`` for the ASCII fuzzy
    terms only (Lucene doesn't analyse fuzzy terms)."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def key_query(field: str, value: str) -> str | None:
    """Lucene phrase query for ``value`` in ``field``, or None when the index can't be
    trusted to find it the way the old ``toLower`` scan would, and the caller scans: keys
    with non-ASCII characters (the analyzer and Python/Cypher lower-case some scripts
    differently, e.g. a final Greek sigma) and keys made only of stop words (``IT``, ``ON``,
    ``A``: no tokens in the index). Identity keys are overwhelmingly ASCII codes, so the
    scan stays rare."""
    words = _ASCII_WORD.findall(value.lower())
    if not value.isascii() or not words or all(w in STOP_WORDS for w in words):
        return None
    # the analyzer folds; the caller checks the hits exactly with match_key
    escaped = value.lower().replace("\\", "\\\\").replace('"', '\\"')
    return f'{field}:"{escaped}"'


def _alphabetic(word: str) -> bool:
    """Latin, Greek or Cyrillic letters (and digits): scripts the analyzer keeps as whole
    words, so a fuzzy term can match them (CJK, for one, is split per character)."""
    return all(
        ch.isdigit() or unicodedata.name(ch, "").startswith(("LATIN", "GREEK", "CYRILLIC"))
        for ch in word
    )


def _fuzzy_terms(text: str) -> list[str]:
    """Typo-tolerant terms for ``text``, as Lucene sees them unanalysed:

    * each alphabetic word of 4+ characters, lower-cased, with "ς" as "σ" and with accents
      folded (``Μυτιληναίος`` vs ``ΜΥΤΙΛΗΝΑΙΟΣ``), within one edit;
    * adjacent words of the reference joined (reference "Sing Tel" ~ stored "SingTel";
      not the reverse), within one edit;
    * a prefix of each long word ("StarHubLtd" -> "starh*" ~ "StarHub Ltd").
    """
    words = [w for w in _WORD.findall(lower(text)) if _alphabetic(w)]
    terms: list[str] = []
    for w in words:
        if len(w) >= _FUZZY_MIN_LEN:
            for form in (w, w.replace("ς", "σ"), fold(w).replace("ς", "σ")):
                terms.append(f"{form}~1")
    for a, b in zip(words, words[1:], strict=False):
        joined = fold(a + b).replace("ς", "σ")
        if len(joined) >= _FUZZY_MIN_LEN:
            terms.append(f"{joined}~1")
    for w in words:
        if len(w) >= _PREFIX_MIN_LEN:
            terms.append(f"{fold(w)[:_PREFIX_LEN]}*")
    return list(dict.fromkeys(terms))[: MAX_WORDS * 4]


def name_query(field: str, text: str) -> str | None:
    """Lucene query for names like ``text``: the whole text, analysed by the index's analyzer
    (escaped; with a final sigma also as "σ", as the index stores an upper-case "Σ"), OR the
    typo-tolerant terms of ``_fuzzy_terms``. Words are lower-case, so ``and``/``or`` are
    never operators."""
    if not _WORD.search(text):
        return None
    head = " ".join(lower(canonical_text(text)).split()[:MAX_TEXT_WORDS])
    variants = [head]
    if "ς" in head:  # the index lower-cases "Σ" to "σ"; Python gives a final "ς"
        variants.append(head.replace("ς", "σ"))
    clauses = [f"{field}:({_LUCENE_SPECIAL.sub(r'\\\1', v)})" for v in variants]
    terms = _fuzzy_terms(canonical_text(text))
    if terms:
        clauses.append(f"{field}:({' OR '.join(terms)})")
    return " OR ".join(clauses)


class Neo4jLookup:
    """Mixin for classes that read entities from Neo4j (``driver`` + ``database``)."""

    driver: Any
    database: str
    scan_below: int = SCAN_BELOW

    def __init__(self, driver: Any, database: str = "neo4j") -> None:
        self.driver = driver
        self.database = database

    def _query(self, cypher: str, **params: Any) -> list[Any]:
        records, _, _ = self.driver.execute_query(cypher, database_=self.database, **params)
        return list(records)

    def _small(self, label: str) -> bool:
        """Is the label small enough to scan (``scan_below``)? Counted once per instance
        (Neo4j answers a label count from its count store)."""
        if self.scan_below <= 0:
            return False
        counts: dict[str, int] = self.__dict__.setdefault("_label_counts", {})
        if label not in counts:
            rows = self._query(f"MATCH (n:{label}) RETURN count(n) AS c")
            counts[label] = int(rows[0]["c"]) if rows else 0
        return counts[label] < self.scan_below

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
        index = None if self._small(label) else self._fulltext_index(label, key_prop)
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
        index = None if self._small(label) else self._fulltext_index(label, name_prop)
        query = name_query(name_prop, text) if index else None
        hits = self._fulltext(index, query) if index and query else None
        if hits is not None:
            pairs = [(h["node"].get(key_prop), h["node"].get(name_prop)) for h in hits]
        else:  # no usable index: every name of the label, read once per instance (one batch)
            cache: dict[tuple[str, str, str], list[Any]] = self.__dict__.setdefault(
                "_all_names", {}
            )
            slot = (label, key_prop, name_prop)
            if slot not in cache:
                rows = self._query(
                    f"MATCH (n:{label}) WHERE n.{name_prop} IS NOT NULL "
                    f"RETURN n.{key_prop} AS k, n.{name_prop} AS name"
                )
                cache[slot] = [(r["k"], r["name"]) for r in rows]
            pairs = cache[slot]
        return [(str(k), str(n)) for k, n in pairs if k is not None and n is not None]
