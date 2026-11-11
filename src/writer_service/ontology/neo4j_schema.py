"""Neo4j constraints generated from a published ontology, and an idempotent sync.

Only constraints whose names start with ``wsc_`` (writer-service constraint) are managed:
sync creates missing ones and drops stale ones, and never touches anything else.
Property-existence constraints need Neo4j Enterprise, so only uniqueness is used.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from writer_service.ontology.spec import ClassSpec, OntologySpec

MANAGED_PREFIX = "wsc_"
# Provenance nodes written by the writer service for every fact (issue #10).
PROVENANCE_KEYS = {"Evidence": "id", "Source": "id", "IngestionRun": "id"}


def neo4j_label(cls: ClassSpec) -> str:
    """Core classes use their name; extension classes are namespaced as ``Ext_<Name>``."""
    return cls.name if cls.namespace == "core" else f"Ext_{cls.name}"


def _constraint(label: str, key: str) -> tuple[str, str]:
    name = f"{MANAGED_PREFIX}{label.lower()}_{key}"
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
    def execute(self, statement: str) -> None: ...


@dataclass
class SyncResult:
    created: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)


def sync_constraints(graph: GraphSchema, desired: dict[str, str]) -> SyncResult:
    existing = graph.constraint_names()
    result = SyncResult()
    for name in sorted(n for n in existing if n.startswith(MANAGED_PREFIX) and n not in desired):
        graph.execute(f"DROP CONSTRAINT {name} IF EXISTS")
        result.dropped.append(name)
    for name, statement in desired.items():
        if name not in existing:
            graph.execute(statement)
            result.created.append(name)
    return result


class InMemoryGraphSchema:
    """Test double that understands the two statement shapes ``sync_constraints`` emits."""

    def __init__(self, names: set[str] | None = None) -> None:
        self.names: set[str] = set(names or ())
        self.executed: list[str] = []
        self.fail_next = False

    def constraint_names(self) -> set[str]:
        return set(self.names)

    def execute(self, statement: str) -> None:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("neo4j down")
        self.executed.append(statement)
        verb, _, name = statement.split()[:3]
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

    def execute(self, statement: str) -> None:
        self.driver.execute_query(statement, database_=self.database)
