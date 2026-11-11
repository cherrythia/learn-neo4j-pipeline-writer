# Changelog

All notable changes are recorded here, one entry per merged issue.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- **#3 Governance store**
  - SQLAlchemy 2 models for ontology versions, classes (with taxonomy parent), properties,
    relation types, extension proposals, scoring configs, ingestion runs, candidates, review
    items, column mappings, users and audit log (`writer_service.governance`).
  - Enumerations enforced by the database (VARCHAR + CHECK) with MySQL-safe constraint names.
  - UTC timestamps that read back timezone-aware (`UTCDateTime`).
  - Compose MySQL gains a `governance_test` database for integration tests.
  - Alembic migration `0001` and the `writer-db` CLI (`upgrade`, `downgrade`, `revision`).
  - `UnitOfWork` (commit or rollback per operation) with repositories and an `audit()` helper.
  - Tests: CRUD and CHECK constraints for all 12 models, cascades, FKs, timestamps, model and
    migration drift, UoW; MySQL integration test in CI.
  - Docs: `docs/governance-store.md`.
- **#2 Local demo infrastructure**
  - `docker-compose.yml`: Neo4j 5, MySQL 8.4 and a mock Superset API, all bound to localhost;
    optional `--profile superset` runs real Apache Superset 4.1 plus a bootstrap job that
    registers the demo datasets.
  - Fictional compliance data in `src/demo_env/data.py` (20 issuers, 6 rules, 6 obligations,
    40 filings with 3 late), the single source for the generated MySQL seed and the samples.
  - MySQL users from env: `writer` (governance DB) and read-only `reader` (demo data).
  - Mock Superset (`demo_env.mock_superset`): login and dataset endpoints with Superset's
    response shapes.
  - Sample PDF annual report and Excel disclosure checklist in `samples/` (byte-reproducible).
  - `scripts/smoke_demo.sh` and CI job `demo-infra`: start the stack and check data, grants,
    mock Superset and Neo4j.
  - Docs: `docs/demo-environment.md`.
- **#1 Project scaffolding, CI and design spec**
  - Python 3.12 FastAPI service skeleton (`src/writer_service/`) with `GET /health` and a
    version header.
  - Typed `Settings` (env prefix `WRITER_`) with validation, including
    `WRITER_SELF_CONSISTENCY_RUNS` (1–10, default 3), secret masking, and empty values
    treated as unset. `create_app()` validates settings at startup (fail fast).
  - `admin-ui/` Bun + React + TypeScript skeleton with a component test; dev server bound to
    loopback.
  - GitHub Actions CI: ruff, mypy (strict), pytest, plus UI typecheck, test and build.
  - Design spec (`docs/design.md`), ADRs 0001–0013 (`docs/adr/`), `CONTRIBUTING.md`,
    `.env.example`.
