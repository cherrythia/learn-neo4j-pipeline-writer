# Ontology and taxonomy

The governed standard every ingestion follows: which classes exist, how they are identified,
how they relate, and how the standard changes safely over time.
([ADR-0006](adr/0006-governance-store.md), [design §4](design.md#4-ontology-layers-adr-0006))

## Core ontology (v1)

```
Organisation ◄─is-a─ Issuer ◄─FILED_BY─ Filing ─is-a─► Document
                       ▲                  │  │
            CONCERNS   │       CITES      │  │ SATISFIES
 Finding ──────────────┘   ┌──────────────┘  ▼
    │ BREACHES             ▼             Obligation ─DERIVES_FROM─► Rule
    └────────────────────► Rule
```

| Class | Identity key | Parent (taxonomy) |
|---|---|---|
| Organisation | `name` | |
| Issuer | `ticker` | Organisation |
| Document | `document_id` | |
| Filing | `filing_id` | Document |
| Rule | `rule_code` | |
| Obligation | `obligation_id` | |
| Finding | `finding_id` | |

| Relation | Domain → Range | Cardinality |
|---|---|---|
| `FILED_BY` | Filing → Issuer | many_to_one |
| `DERIVES_FROM` | Obligation → Rule | many_to_one |
| `CITES` | Filing → Rule | many_to_many |
| `SATISFIES` | Filing → Obligation | many_to_many |
| `CONCERNS` | Finding → Issuer | many_to_one |
| `BREACHES` | Finding → Rule | many_to_many |

Defined in `src/writer_service/ontology/core.py`. `parent` is the taxonomy (subclass-of);
each class carries its own identity key and properties.

## Layers

| Layer | Namespace | Neo4j label | How it changes |
|---|---|---|---|
| Core | `core` | `Issuer` | New version through draft → publish |
| Extension | `ext` | `Ext_ESGMetric` | Proposed (LLM or admin), human-approved, published the same way |
| Instances | — | — | Written by the pipeline after checks (issue #10) |

## Lifecycle

```
bootstrap ──► v1 published
draft     ──► v2 draft (copy of the published version; only one draft at a time)
   edit the draft (admin API/UI, issues #14–#15)
publish 2 ──► gate ──► Neo4j constraints synced ──► v2 published, v1 superseded
rollback  ──► v2 deprecated, v1 published again, constraints synced back
```

**The publish gate** (`ontology/validation.py`) rejects a version when:
- an identity key is missing or not a required property;
- a parent or relation endpoint does not exist, or an endpoint is deprecated;
- the taxonomy has a cycle, or a class or relation name is duplicated;
- a name is not a safe identifier: classes are `CamelCase`, relations `UPPER_SNAKE`,
  properties `lower_snake`. Names go into Cypher, so this keeps generated Cypher injection-free;
- two classes would get the same Neo4j constraint name (names are lower-cased, so `FooBar`
  and `Foobar` collide), or a class collides with a provenance constraint;
- a class or relation of the published version was **removed**, or a published class changed
  its identity key or namespace or lost a property, or a published relation changed its
  endpoints. Deprecate and add instead; existing graph data must keep a valid definition.

**Neo4j constraints and indexes** (`ontology/neo4j_schema.py`): one uniqueness constraint per
active class identity key (`wsc_issuer_ticker`), plus `Evidence.id`, `Source.id` and
`IngestionRun.id` for provenance; and one **full-text index** per active class over its
identity key (if a string) and its `name`/`title` (`wsf_issuer_ticker_name`), used by entity
matching ([matching](matching.md)). The index name lists its fields, so a changed class gets a
new index and the old one is dropped. Only objects named `wsc_*` / `wsf_*` are managed: sync
creates missing ones, drops stale ones and leaves everything else alone. Property-existence
constraints need Neo4j Enterprise, so required properties are enforced by the pipeline's gates.

**Atomicity.** Publish locks the version rows, validates, writes the new statuses, syncs Neo4j
and then commits the governance transaction. Neo4j DDL can't be part of that SQL transaction, so:
- sync **creates before it drops**, so a failure part-way only ever leaves extra constraints;
- every SQL write is flushed *before* the sync, so after it only the commit remains;
- if anything fails once the sync has started (the sync itself, the audit update or the
  commit), the service **re-syncs Neo4j back to the still-published version** and the
  governance transaction rolls back. Tested for publish and rollback, with failures on a
  CREATE, on a DROP and on the commit;
- only a process crash between sync and commit can leave Neo4j ahead; re-running the command
  fixes it, because constraint DDL is idempotent.

**Concurrency.** Publish, rollback and new-draft take `SELECT … FOR UPDATE` locks on
`ontology_versions` (MySQL), so two admins can't publish at once. SQLite has no row locks, so
use MySQL for anything multi-user.

Every publish, draft and rollback is in the audit log with the constraint changes it made.

## CLI

```sh
uv run writer-db upgrade                 # governance tables
uv run writer-ontology bootstrap         # seed + publish v1, create Neo4j constraints
uv run writer-ontology show              # print the published version
uv run writer-ontology draft             # v2 draft
uv run writer-ontology publish 2
uv run writer-ontology rollback
uv run writer-ontology sync              # re-apply the published version's constraints and
                                         # indexes (after an upgrade adds managed indexes)
```

`--actor <name>` sets who is recorded in the audit log (default: your OS user).
