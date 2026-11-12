"""Neo4j constraints and indexes generated from a published ontology, and an idempotent sync.

Only schema objects whose names start with ``wsc_`` (writer-service constraint) or ``wsf_``
(writer-service full-text index) are managed: sync creates missing ones and drops stale ones,
and never touches anything else. Property-existence constraints need Neo4j Enterprise, so
only uniqueness is used.

**Full-text indexes** (#28): one per class, over its identity key (when it is a string) and
its name/title property. Entity matching queries them for the few best name candidates and
for case-insensitive key lookups, instead of reading every name or scanning the label. The
index name lists its fields, so a class whose fields change gets a new index (and the old one
is dropped).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from writer_service.ontology.spec import ClassSpec, OntologySpec

MANAGED_PREFIX = "wsc_"
FULLTEXT_PREFIX = "wsf_"
# lower-case + ASCII folding ("Müller" ~ "Muller"); matching folds fuzzy terms the same way
FULLTEXT_ANALYZER = "standard-folding"
# Properties that hold an entity's human-readable name, in order of preference.
NAME_PROPERTIES = ("name", "title")
# Provenance nodes written by the writer service for every fact (issue #10).
PROVENANCE_KEYS = {"Evidence": "id", "Source": "id", "IngestionRun": "id"}


def neo4j_label(cls: ClassSpec) -> str:
    """Core classes use their name; extension classes are namespaced as ``Ext_<Name>``."""
    return cls.name if cls.namespace == "core" else f"Ext_{cls.name}"


def name_property(cls: ClassSpec) -> str | None:
    """The class's ``name`` property, or ``title`` if it has none (None if neither)."""
    props = {p.name for p in cls.properties}
    return next((n for n in NAME_PROPERTIES if n in props), None)


def fulltext_fields(cls: ClassSpec) -> list[str]:
    """Fields of the class's full-text index: the identity key if it's a string (full-text
    indexes only hold strings), then the name property."""
    types = {p.name: p.datatype for p in cls.properties}
    fields = [cls.identity_key] if types.get(cls.identity_key, "string") == "string" else []
    name = name_property(cls)
    if name is not None and name not in fields:
        fields.append(name)
    return fields


def fulltext_index_name(label: str, fields: list[str]) -> str:
    return f"{FULLTEXT_PREFIX}{label.lower()}_{'_'.join(fields)}"


def desired_indexes(spec: OntologySpec) -> dict[str, str]:
    """Full-text index name -> Cypher, one per active class with indexable fields."""
    out: dict[str, str] = {}
    for cls in spec.active_classes():
        fields = fulltext_fields(cls)
        if not fields:
            continue
        label = neo4j_label(cls)
        name = fulltext_index_name(label, fields)
        on = ", ".join(f"n.{f}" for f in fields)
        out[name] = (
            f"CREATE FULLTEXT INDEX {name} IF NOT EXISTS FOR (n:{label}) ON EACH [{on}] "
            f"OPTIONS {{indexConfig: {{`fulltext.analyzer`: '{FULLTEXT_ANALYZER}'}}}}"
        )
    return dict(sorted(out.items()))


def desired_schema(spec: OntologySpec) -> dict[str, str]:
    """Every managed schema object: uniqueness constraints and full-text indexes."""
    return {**desired_constraints(spec), **desired_indexes(spec)}


def sync_schema(graph: GraphSchema, spec: OntologySpec) -> SyncResult:
    """Make Neo4j's managed constraints and indexes match ``spec``. Use this rather than
    passing a partial dict to ``sync_constraints``, which drops every managed object that
    isn't in it."""
    return sync_constraints(graph, desired_schema(spec))


def _constraint(label: str, key: str) -> tuple[str, str]:
    name = constraint_name(label, key)
    return name, (
        f"CREATE CONSTRAINT {name} IF NOT EXISTS FOR (n:{label}) REQUIRE n.{key} IS UNIQUE"
    )


def desired_constraints(spec: OntologySpec) -> dict[str, str]:
    """Constraint name -> Cypher. Assumes ``spec`` passed validation (safe identifiers)."""
    pairs = [_constraint(neo4j_label(c), c.identity_key) for c in spec.active_classes()]
    pairs += [_constraint(label, key) for label, key in PROVENANCE_KEYS.items()]
    return dict(sorted(pairs))


class GraphSchema(Protocol):
    def constraint_names(self) -> set[str]: ...
    def index_names(self) -> set[str]: ...
    def execute(self, statement: str) -> None: ...


def _managed(name: str) -> bool:
    return name.startswith((MANAGED_PREFIX, FULLTEXT_PREFIX))


@dataclass
class SyncResult:
    created: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)


def constraint_name(label: str, key: str) -> str:
    return f"{MANAGED_PREFIX}{label.lower()}_{key}"


def sync_constraints(graph: GraphSchema, desired: dict[str, str]) -> SyncResult:
    """Make the managed constraints and indexes equal ``desired`` (see ``desired_schema``).

    CREATEs run before DROPs, so a failure part-way leaves a *superset* of constraints (never
    removes one the current version still needs). Callers re-sync to the previous state on
    error (see ``OntologyService``).
    """
    # A uniqueness constraint's backing index has the constraint's name, so it is listed by
    # both; the set union counts it once.
    existing = graph.constraint_names() | graph.index_names()
    result = SyncResult()
    for name, statement in desired.items():
        if name not in existing:
            graph.execute(statement)
            result.created.append(name)
    for name in sorted(n for n in existing if _managed(n) and n not in desired):
        kind = "INDEX" if name.startswith(FULLTEXT_PREFIX) else "CONSTRAINT"
        graph.execute(f"DROP {kind} {name} IF EXISTS")
        result.dropped.append(name)
    return result


class InMemoryGraphSchema:
    """Test double that understands the statement shapes ``sync_constraints`` emits
    (constraints and indexes share one set of names)."""

    def __init__(self, names: set[str] | None = None) -> None:
        self.names: set[str] = set(names or ())
        self.executed: list[str] = []
        self.fail_next = False  # fail the next statement
        self.fail_on: str | None = None  # fail the next statement containing this text

    def constraint_names(self) -> set[str]:
        return set(self.names)

    def index_names(self) -> set[str]:
        return set()

    def execute(self, statement: str) -> None:
        if self.fail_next or (self.fail_on is not None and self.fail_on in statement):
            self.fail_next = False
            self.fail_on = None
            raise RuntimeError("neo4j down")
        self.executed.append(statement)
        words = statement.split()
        verb = words[0]
        kind = next(i for i, w in enumerate(words) if w in ("CONSTRAINT", "INDEX"))
        name = words[kind + 1]
        if verb == "CREATE":
            self.names.add(name)
        elif verb == "DROP":
            self.names.discard(name)


class Neo4jGraphSchema:
    def __init__(self, driver: Any, database: str = "neo4j") -> None:
        self.driver = driver
        self.database = database

    def constraint_names(self) -> set[str]:
        records, _, _ = self.driver.execute_query(
            "SHOW CONSTRAINTS YIELD name", database_=self.database
        )
        return {r["name"] for r in records}

    def index_names(self) -> set[str]:
        records, _, _ = self.driver.execute_query(
            "SHOW INDEXES YIELD name", database_=self.database
        )
        return {r["name"] for r in records}

    def execute(self, statement: str) -> None:
        self.driver.execute_query(statement, database_=self.database)
