# learn-neo4j-pipeline-writer

A learning project: a **writer service** that ingests knowledge from multiple sources
(PDF, Excel, Superset/MySQL), runs every candidate fact through an LLM-assisted
**verification and scoring pipeline**, and only then writes accepted facts into **Neo4j**,
following a governed ontology and taxonomy managed from an admin page.

```
sources ─► Ingest ─► Match ─► Compile (gates) ─► Test (score) ─► Review ─► Commit ─► Neo4j
```

- **Design**: [`docs/design.md`](docs/design.md)
- **Decisions**: [`docs/adr/`](docs/adr/README.md)
- **What changed**: [`CHANGELOG.md`](CHANGELOG.md)
- **How work is done**: [`CONTRIBUTING.md`](CONTRIBUTING.md) and the GitHub issue list

## Status

Built issue by issue; see the [issues](https://github.com/cherrythia/learn-neo4j-pipeline-writer/issues).
Currently in place: service skeleton, settings, admin UI skeleton, CI, local demo environment,
governance store ([docs](docs/governance-store.md)), LLM layer ([docs](docs/llm.md)).

## Quick start

Requirements: [uv](https://docs.astral.sh/uv/), [Bun](https://bun.sh), Docker (for the demo
infrastructure).

```sh
cp .env.example .env              # fill in WRITER_ANTHROPIC_API_KEY to use Claude
uv sync
uv run uvicorn writer_service.app:create_app --factory --reload   # http://localhost:8000/health
uv run writer-db upgrade                          # create governance tables
uv run pytest

cd admin-ui && bun install && bun run dev        # http://localhost:3000
```

## Demo environment

```sh
cp .env.example .env
docker compose up -d --wait        # Neo4j :7474/:7687, MySQL :3306, mock Superset :8088
```

Fictional compliance data, sample PDF and Excel files, and an optional real Superset. See
[`docs/demo-environment.md`](docs/demo-environment.md).

## Configuration

All settings use the `WRITER_` prefix. See [`.env.example`](.env.example).

| Variable | Default | Purpose |
|---|---|---|
| `WRITER_APP_ENV` | `dev` | `dev`, `test` or `prod` |
| `WRITER_LLM_PROVIDER` | `anthropic` | `anthropic` or `fake` (tests) |
| `WRITER_LLM_MODEL` | `claude-sonnet-5-5` | Model id |
| `WRITER_ANTHROPIC_API_KEY` | *(none)* | **Secret.** Required when provider is `anthropic` (the LLM factory fails fast without it) |
| `WRITER_LLM_EFFORT` | `high` | Claude effort: `low` to `max` |
| `WRITER_LLM_FALLBACKS` | `true` | Server-side fallback when Claude declines a request |
| `WRITER_LLM_MAX_ATTEMPTS` | `2` | Attempts per call when output fails schema validation |
| `WRITER_LLM_CACHE_DIR` | `data/cache/llm` | Cache of validated LLM answers |
| `WRITER_SELF_CONSISTENCY_RUNS` | `3` | Extraction runs per input (1 to 10) |
| `WRITER_NEO4J_URI` | `bolt://localhost:7687` | Neo4j connection |
| `WRITER_NEO4J_USER` | `neo4j` | Neo4j user |
| `WRITER_NEO4J_PASSWORD` | `change-me-neo4j` | **Secret.** Neo4j password (min 8 chars) |
| `WRITER_GOVERNANCE_DB_URL` | `sqlite:///./governance.sqlite` | Governance store |

Empty values in `.env` are treated as unset. Invalid values stop the service at startup.
