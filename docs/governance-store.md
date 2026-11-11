# Governance store

The relational database that governs *how* facts reach Neo4j: ontology versions, the review
queue, scoring config, ingestion runs, mappings, users and the audit log. Neo4j holds only
accepted facts ([ADR-0006](adr/0006-governance-store.md), [ADR-0010](adr/0010-neo4j-accepted-facts-only.md)).

- **Production / demo**: MySQL `governance` database (owned by the `writer` user).
- **Tests**: SQLite. The schema is always built **from the migrations**, never `create_all`, so
  tests exercise exactly what production runs.

## Tables

| Table | Purpose |
|---|---|
| `ontology_versions` | Versioned ontology: `draft` → `published` → `superseded` / `deprecated`; `published()` returns the highest published version (publishing in #4 supersedes the previous one) |
| `ontology_classes` | Classes per version and namespace (`core` / `ext`); `parent_id` is the taxonomy (subclass-of); `identity_key` is the property used for `MERGE` |
| `property_defs` | Typed properties of a class (`string`, `int`, `float`, `date`, `bool`) |
| `relation_types` | Typed edges with `domain_class` → `range_class` and cardinality |
| `extension_proposals` | New classes/relations proposed by the LLM or a user, awaiting approval |
| `scoring_configs` | Weights, thresholds and N; `uow.scoring_configs.active()` returns the newest active row |
| `ingestion_runs` | One per source ingestion (UUID id), with status and outcome counts |
| `candidates` | Every candidate fact with evidence, score breakdown, gate failures, decision |
| `review_items` | Human review queue (`score` or `conflict`); at most one per candidate |
| `column_mappings` | Superset dataset → ontology mappings (`draft` / `approved` / `retired`) |
| `users` | Admin UI users with role `admin` or `reviewer` |
| `audit_log` | Who did what, written in the same transaction as the change |

Enumerated columns are `VARCHAR` + `CHECK` constraints, so the database rejects invalid
values on both MySQL and SQLite. Constraint names follow a naming convention that includes the
table name, because MySQL requires CHECK constraint names to be unique per schema.

## Migrations

```sh
uv run writer-db upgrade                      # to head, uses WRITER_GOVERNANCE_DB_URL
uv run writer-db downgrade base
uv run writer-db revision -m "add something"  # autogenerate after changing models.py
```

`tests/test_governance/test_migrations.py` fails if `models.py` and the migrations drift
apart, so every model change needs a migration in the same PR.

To use MySQL locally, set in `.env`:

```
WRITER_GOVERNANCE_DB_URL=mysql+pymysql://writer:<MYSQL_WRITER_PASSWORD>@127.0.0.1:3306/governance
```

## Code

```python
from writer_service.governance.db import create_engine_for
from writer_service.governance.uow import UnitOfWork

engine = create_engine_for(settings.governance_db_url)
with UnitOfWork(engine) as uow:  # commits on success, rolls back on error
    current = uow.ontology_versions.published()
    uow.audit("alice", "ontology.publish", "ontology_version", "2")
```

## Tests

- Unit (SQLite, always run): constraints, enums, FKs, taxonomy, UoW, model/migration drift.
- Integration (MySQL, `-m integration`): upgrade, CHECK enforcement, downgrade. It drops all
  governance tables, so it uses the separate **`governance_test`** database and refuses any
  database not ending in `_test`. Set
  `WRITER_TEST_MYSQL_URL=mysql+pymysql://writer:<pw>@127.0.0.1:3306/governance_test`; runs in
  the CI `demo-infra` job.

Timestamps are stored as UTC and always read back timezone-aware; naive datetimes are rejected.
