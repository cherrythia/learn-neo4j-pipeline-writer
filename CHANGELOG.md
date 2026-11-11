# Changelog

All notable changes are recorded here, one entry per merged issue.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- **#12 Superset/MySQL connector and column mappings** (`writer_service.ingestion.superset`)
  - `SupersetClient` (login, dataset metadata; identifiers validated), `DatasetReader`
    (paged, read-only; physical and virtual SQL datasets).
  - Column mappings: LLM-drafted with ontology/column enums or manual; validated against the
    ontology and real columns; admin approval retires the previous mapping; audited.
  - Deterministic rows → candidates with one verbatim evidence item per column.
  - `POST /ingest/superset/{dataset_id}` (API key; 409 without an approved mapping).
  - Tests against the mock Superset and the demo seed; MySQL integration test in CI.
    Docs: `docs/superset-ingestion.md`.
- **#10 Writer pipeline and atomic Neo4j writer** (`writer_service.pipeline.orchestrator`,
  `graph_store`)
  - One run per source: N-run extraction with voting (documents), matching, gates, signals,
    scoring and routing; every candidate stored with its derivation; review/conflict items.
  - Non-verbatim quotes (grounding < 1.0) are never auto-committed.
  - Neo4j writes in one transaction: MERGE on identity keys, taxonomy labels, provenance
    (`:Evidence`, `:Source`, `:IngestionRun`, score/run/version on every fact); idempotent.
  - Run ownership (`runs` on every node, relationship and evidence): rollback deletes only
    what no other run owns and repairs provenance; it also closes the run's open review items.
  - Approvals as their own graph runs, re-gated (schema + constraint), cardinality-aware
    conflict replacement recorded in the audit log, compensated on commit failure.
  - Failure handling for both stores; generic client errors; driver closed on shutdown.
  - `POST /ingest/documents` (API key; PDF/XLSX; size limit) runs the pipeline.
  - In-memory graph for tests/demo; Neo4j integration tests. Docs: `docs/pipeline.md`.
- **#11 Document ingestion library** (`writer_service.ingestion.documents`)
  - PDF → one chunk per page (`page N`); Excel → one chunk per row (`sheet S row N`), from
    paths or uploaded bytes; windowing without splitting chunks.
  - LLM extraction whose structured-output schema enumerates the published ontology's class,
    relation and property names; evidence anchored to existing locators only.
  - `pypdf` and `openpyxl` are now runtime dependencies. Docs: `docs/document-ingestion.md`.
- **#8 Verification signals** (`writer_service.pipeline.verification`)
  - Evidence grounding: only verbatim quotes (after Unicode/typography normalisation, on word
    and number boundaries, without a dropped preceding negation) score 1.0; near-quotes are
    capped at 0.5 (human review); altered numbers, names or negations at 0.1.
  - Back-translation: plain-language fact + quotes judged by an LLM (prompt v1, validated,
    cached); disagreement caps the score at 0.3.
  - Self-consistency: N extraction runs, strict-majority vote on `fact_hash`, merged evidence,
    `stability = votes / N`.
  - Docs: `docs/verification.md`.
- **#9 Entity matching** (`writer_service.pipeline.matching`)
  - Exact (case-insensitive) key matches, fuzzy name/title resolution (token-set + token-sort
    average), ambiguity detection (partial or near-tie matches), possible duplicates.
  - Relation endpoints rewritten to existing keys with notes for reviewers; confidence feeds
    `entity_match`. Batch-local entities are resolvable.
  - In-memory and Neo4j match indexes (Neo4j integration test). Docs: `docs/matching.md`.
- **#13 Authentication and roles** (`writer_service.auth`)
  - `X-API-Key` dependency for ingestion (constant-time; refused with 503 when unset).
  - Admin login: scrypt password hashes, JWT session in an httpOnly SameSite=Lax cookie,
    `/api/auth/login|logout|me`; roles `admin` ⊇ `reviewer` via `require_role()`.
  - CSRF header requirement on state-changing cookie requests; uniform login errors;
    login/failed-login audit; deactivated users lose access immediately.
  - Login throttling: per username+client and per client budgets, reserved before the
    password check (race-free), bounded memory → 429.
  - Revocable sessions: `users.session_version` (migration `0003`) in every token; logout
    ends all of a user's sessions; role and active flag always read from the database.
  - `writer-users create` CLI; prod refuses weak JWT secrets, insecure cookies and weak or
    example ingest keys.
  - Docs: `docs/auth.md`; ADR-0011 updated.
- **#6 Candidate facts and hard gates** (`writer_service.pipeline`)
  - Pydantic candidate models (entity / relation, discriminated) with `Evidence`;
    `fact_hash` (whitespace/case/order-insensitive, evidence-free) and `identity_hash`.
  - Schema gate: classes, relations, deprecation, undeclared/required properties, strict
    datatype coercion, taxonomy-aware domain/range. Failure → reject.
  - Constraint gate: property contradictions and cardinality breaches against the graph and
    within the batch. Failure → conflict (human review, never overwritten).
  - `GraphView` port with in-memory and Neo4j implementations (Neo4j integration test in CI).
  - Docs: `docs/gates.md`.
- **#7 Scoring engine** (`writer_service.scoring`)
  - `ScoringPolicy` (weights that must sum to 1, ordered thresholds, per-source reliability, N)
    with the design defaults.
  - Pure `score()` with a full per-component breakdown, and `route()`: gates reject, conflicts
    go to review, then thresholds (inclusive, float-noise safe).
  - `record_outcome()` stores the score breakdown, decision and reasons on each candidate.
  - Policy storage: `active_policy()` / `activate_policy()` (re-validated, history kept, audited);
    migration `0002` adds `scoring_configs.source_reliability`.
  - Tests: table-driven routing boundaries, property-based range and monotonicity
    (hypothesis), worked example, storage round-trip. Docs: `docs/scoring.md`.
- **#4 Core ontology, versioning and Neo4j constraints** (`writer_service.ontology`)
  - Core ontology v1 (Issuer, Filing, Rule, Obligation, Finding, with Organisation and Document
    as taxonomy parents) and six typed relations.
  - Publish gate: identity keys, parents, relation endpoints, cycles, duplicates, safe Cypher
    identifiers, constraint-name collisions, and "deprecate, never delete or redefine" (no
    removed classes, relations or properties; no changed identity keys, namespaces or relation
    endpoints).
  - Neo4j sync creates before dropping and is compensated on failure; versions are row-locked
    during publish, rollback and draft.
  - Lifecycle service: bootstrap, one draft at a time, publish (supersedes previous), rollback;
    all audited.
  - Neo4j uniqueness constraints generated per class plus provenance nodes; idempotent sync
    that manages only `wsc_*` constraints.
  - `writer-ontology` CLI; `scripts/check.sh` runs every CI check locally.
  - Tests: validation rules, constraint generation and sync, lifecycle (including Neo4j failure
    rollback), CLI; Neo4j integration test in CI. Docs: `docs/ontology.md`.
- **#5 LLM abstraction** (`writer_service.llm`)
  - Provider-neutral `LLMClient` protocol; `StructuredLLM.ask()` renders a versioned prompt,
    requests JSON matching a pydantic model, validates, retries with feedback, and caches.
  - Claude adapter (default `claude-sonnet-5-5`) using structured outputs, configurable effort,
    and server-side refusal fallback (on by default).
  - Cache keyed by model, prompt version, input, schema, ontology version and sample index;
    `InMemoryCache` and `FileCache` (usable as recorded fixtures with `ReplayOnlyLLM`).
  - `FakeLLM` for tests; factory fails fast when the Anthropic key is missing.
  - Unusable output (non-JSON, truncated) is retried like schema mismatches; refusals and
    replay misses are not. Fallback turns are parsed from the serving model only.
  - New settings: `WRITER_LLM_EFFORT`, `WRITER_LLM_FALLBACKS`, `WRITER_LLM_MAX_ATTEMPTS`,
    `WRITER_LLM_CACHE_DIR`. Docs: `docs/llm.md`; ADR-0003/0005 updated.
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

### Changed
- **#30 Review process**: up to 5 independent review rounds per PR (was 3) before escalating
  (CONTRIBUTING, design §11, ADR-0001 amendment).

