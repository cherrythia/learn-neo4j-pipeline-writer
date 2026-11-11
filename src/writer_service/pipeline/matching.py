"""Entity matching: resolve what an extractor wrote to entities that already exist.

LLMs often reference an entity by its name ("Aurora Pacific Holdings") rather than its identity
key ("APH"), or propose an entity that duplicates an existing one under a new key. The matcher:

1. **exact** - the identity key exists (case-insensitive), confidence 1.0;
2. **fuzzy** - the text closely matches the *name* (or *title*) of exactly one existing entity
   of that class; relation endpoints are rewritten to that entity's key, confidence = similarity;
3. **ambiguous** - several entities match about equally well, or the best match is only
   partial (between ``AMBIGUITY_FLOOR`` and the threshold); nothing is rewritten and the
   confidence is low, so the fact goes to human review;
4. **new** - nothing matches; confidence is None (the scoring engine treats it as neutral 0.5).

Entity candidates whose key is new but whose name matches someone else are flagged
**possible_duplicate** with a confidence of ``1 - similarity``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from rapidfuzz import fuzz

from writer_service.ontology.neo4j_schema import neo4j_label
from writer_service.ontology.spec import ClassSpec, OntologySpec
from writer_service.pipeline.candidates import (
    CandidateEntity,
    CandidateRelation,
    EntityRef,
    canonical_text,
    match_key,
)

NAME_PROPERTIES = ("name", "title")
FUZZY_THRESHOLD = 0.90
AMBIGUITY_FLOOR = 0.75  # a weaker partial match is not 'new': it needs a human
AMBIGUITY_MARGIN = 0.05
AMBIGUOUS_CONFIDENCE = 0.3
_PUNCT = re.compile(r"[^\w\s]")

Status = Literal["exact", "fuzzy", "ambiguous", "new", "possible_duplicate"]


def _norm(text: str) -> str:
    return canonical_text(_PUNCT.sub(" ", text)).lower()


def name_similarity(a: str, b: str) -> float:
    """Average of token-set and token-sort similarity, in [0, 1].

    Token-set alone scores a single shared word as a perfect match ("Pacific" vs "Aurora
    Pacific Holdings"); averaging with token-sort keeps that low while still tolerating extra
    words such as a "(Demo)" or "Ltd" suffix.
    """
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    return (fuzz.token_set_ratio(na, nb) + fuzz.token_sort_ratio(na, nb)) / 200.0


class MatchIndex(Protocol):
    def find_key(self, label: str, key_prop: str, value: str) -> str | None:
        """The stored identity key equal to ``value`` (case-insensitive), if any."""
        ...

    def names(self, label: str, key_prop: str, name_prop: str) -> list[tuple[str, str]]:
        """(identity key, name) for every node of ``label`` that has ``name_prop``."""
        ...


class InMemoryMatchIndex:
    def __init__(self) -> None:
        self._rows: dict[str, list[tuple[str, str | None]]] = {}

    def add(self, label: str, key_prop: str, key: str, name: str | None = None) -> None:
        self._rows.setdefault(label, []).append((key, name))

    def find_key(self, label: str, key_prop: str, value: str) -> str | None:
        return next(
            (k for k, _ in self._rows.get(label, []) if match_key(k) == match_key(value)), None
        )

    def names(self, label: str, key_prop: str, name_prop: str) -> list[tuple[str, str]]:
        return [(k, n) for k, n in self._rows.get(label, []) if n]


class Neo4jMatchIndex:
    """Labels and property names come from the validated ontology (safe to interpolate)."""

    def __init__(self, driver: Any, database: str = "neo4j") -> None:
        self.driver = driver
        self.database = database

    def _query(self, cypher: str, **params: Any) -> list[Any]:
        records, _, _ = self.driver.execute_query(cypher, database_=self.database, **params)
        return list(records)

    def find_key(self, label: str, key_prop: str, value: str) -> str | None:
        rows = self._query(
            f"MATCH (n:{label}) WHERE n.{key_prop} = $v RETURN n.{key_prop} AS k",
            v=canonical_text(value),
        )
        if not rows:
            rows = self._query(
                f"MATCH (n:{label}) WHERE toLower(toString(n.{key_prop})) = $v "
                f"RETURN n.{key_prop} AS k LIMIT 1",
                v=match_key(value),
            )
        return str(rows[0]["k"]) if rows else None

    def names(self, label: str, key_prop: str, name_prop: str) -> list[tuple[str, str]]:
        rows = self._query(
            f"MATCH (n:{label}) WHERE n.{name_prop} IS NOT NULL "
            f"RETURN n.{key_prop} AS k, n.{name_prop} AS name"
        )
        return [(str(r["k"]), str(r["name"])) for r in rows]


@dataclass(frozen=True)
class MatchResult:
    status: Status
    key: str | None  # the existing entity's identity key (None for new/ambiguous)
    confidence: float | None  # feeds the scoring engine's entity_match (None = neutral)
    alternatives: list[tuple[str, float]] = field(default_factory=list)


class EntityMatcher:
    def __init__(
        self,
        index: MatchIndex,
        spec: OntologySpec,
        *,
        threshold: float = FUZZY_THRESHOLD,
        margin: float = AMBIGUITY_MARGIN,
        floor: float = AMBIGUITY_FLOOR,
    ) -> None:
        self.index = index
        self.classes = {c.name: c for c in spec.classes}
        self.threshold = threshold
        self.margin = margin
        self.floor = floor
        self._batch: dict[str, list[tuple[str, str | None]]] = {}
        self._names: dict[str, list[tuple[str, str]]] = {}  # per-label cache for one batch

    def reset_batch(self) -> None:
        """Forget batch-local entities and cached names. Call between batches (or create one
        matcher per batch) so a later batch can't resolve to an earlier batch's unaccepted
        entities, and so names are re-read after the graph changed."""
        self._batch.clear()
        self._names.clear()

    @staticmethod
    def _name_prop(cls: ClassSpec) -> str | None:
        props = {p.name for p in cls.properties}
        return next((n for n in NAME_PROPERTIES if n in props), None)

    def add_batch_entity(self, candidate: CandidateEntity) -> None:
        """Make an entity proposed earlier in this batch resolvable by later candidates."""
        cls = self.classes[candidate.class_name]
        key = candidate.properties.get(cls.identity_key)
        if key is None:
            return
        name_prop = self._name_prop(cls)
        name = candidate.properties.get(name_prop) if name_prop else None
        self._batch.setdefault(cls.name, []).append((str(key), str(name) if name else None))

    def _ranked(
        self, cls: ClassSpec, text: str, exclude: str | None = None
    ) -> list[tuple[str, float]]:
        name_prop = self._name_prop(cls)
        if name_prop is None:
            return []
        label = neo4j_label(cls)
        if label not in self._names:  # one name scan per label per batch, not per reference
            self._names[label] = self.index.names(label, cls.identity_key, name_prop)
        pool = list(self._names[label])
        pool += [(k, n) for k, n in self._batch.get(cls.name, []) if n]
        best: dict[str, float] = {}
        for key, name in pool:
            if exclude is not None and match_key(key) == match_key(exclude):
                continue
            best[key] = max(best.get(key, 0.0), name_similarity(text, name))
        return sorted(best.items(), key=lambda kv: (-kv[1], kv[0]))

    def resolve(self, class_name: str, text: str) -> MatchResult:
        """Resolve a reference (key or name) to an entity of ``class_name``."""
        cls = self.classes[class_name]
        found = self.index.find_key(neo4j_label(cls), cls.identity_key, text)
        if found is None:
            found = next(
                (k for k, _ in self._batch.get(cls.name, []) if match_key(k) == match_key(text)),
                None,
            )
        if found is not None:
            return MatchResult("exact", found, 1.0)
        ranked = self._ranked(cls, text)
        if not ranked or ranked[0][1] < self.floor:
            return MatchResult("new", None, None)
        best_key, best = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
        if best < self.threshold or best - runner_up < self.margin:
            close = [(k, s) for k, s in ranked if s >= self.floor][:5]
            return MatchResult("ambiguous", None, AMBIGUOUS_CONFIDENCE, close)
        return MatchResult("fuzzy", best_key, min(best, 0.99), ranked[:3])

    def match_entity(self, candidate: CandidateEntity) -> MatchResult:
        """Match an entity candidate by its own key. ``exact`` returns the *stored* key (which may
        differ in case: the writer must use it, because Neo4j keys are case-sensitive).
        A new key whose name matches another entity is flagged ``possible_duplicate``."""
        cls = self.classes[candidate.class_name]
        key = str(candidate.properties.get(cls.identity_key, ""))
        stored = self.index.find_key(neo4j_label(cls), cls.identity_key, key)
        if stored is not None:
            return MatchResult("exact", stored, 1.0)
        name_prop = self._name_prop(cls)
        name = candidate.properties.get(name_prop) if name_prop else None
        if name:
            ranked = self._ranked(cls, str(name), exclude=key)
            if ranked and ranked[0][1] >= self.threshold:
                other, sim = ranked[0]
                return MatchResult("possible_duplicate", other, round(1 - sim, 6), ranked[:3])
        return MatchResult("new", None, None)

    def resolve_relation(
        self, relation: CandidateRelation
    ) -> tuple[CandidateRelation, float | None, list[str]]:
        """Resolve both endpoints. Returns (relation with rewritten keys, confidence, notes).

        Confidence is the weakest *known* endpoint (new endpoints are neutral); None when both
        endpoints are new.
        """
        notes: list[str] = []
        confidences: list[float] = []
        ends: dict[str, EntityRef] = {}
        for side, ref in (("source", relation.source), ("target", relation.target)):
            result = self.resolve(ref.class_name, ref.key)
            if result.status == "fuzzy" and result.key is not None:
                notes.append(
                    f"{side} {ref.class_name} {ref.key!r} resolved to {result.key!r} "
                    f"(similarity {result.confidence:.2f})"
                )
                ref = EntityRef(class_name=ref.class_name, key=result.key)
            elif result.status == "exact" and result.key is not None and result.key != ref.key:
                # same entity, different spelling of the key (e.g. case): use the stored key
                notes.append(f"{side} {ref.class_name} {ref.key!r} normalised to {result.key!r}")
                ref = EntityRef(class_name=ref.class_name, key=result.key)
            elif result.status == "ambiguous":
                options = ", ".join(k for k, _ in result.alternatives)
                notes.append(f"{side} {ref.class_name} {ref.key!r} is ambiguous ({options})")
            if result.confidence is not None:
                confidences.append(result.confidence)
            ends[side] = ref
        resolved = relation.model_copy(update=ends)
        return resolved, (min(confidences) if confidences else None), notes
