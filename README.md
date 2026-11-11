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
Currently in place: service skeleton, settings, admin UI skeleton, CI.

## Quick start

Requirements: [uv](https://docs.astral.sh/uv/), [Bun](https://bun.sh), Docker (for the demo
infrastructure, added in #2).

```sh
cp .env.example .env              # fill in WRITER_ANTHROPIC_API_KEY to use Claude
uv sync
uv run uvicorn writer_service.app:create_app --factory --reload   # http://localhost:8000/health
uv run pytest

cd admin-ui && bun install && bun run dev        # http://localhost:3000
```

## Configuration

All settings use the `WRITER_` prefix. See [`.env.example`](.env.example).

| Variable | Default | Purpose |
|---|---|---|
| `WRITER_APP_ENV` | `dev` | `dev`, `test` or `prod` |
| `WRITER_LLM_PROVIDER` | `anthropic` | `anthropic` or `fake` (tests) |
| `WRITER_LLM_MODEL` | `claude-sonnet-5-5` | Model id |
| `WRITER_ANTHROPIC_API_KEY` | *(none)* | **Secret.** Needed for real LLM calls; not checked at startup yet (enforced by the LLM adapter, #5) |
| `WRITER_SELF_CONSISTENCY_RUNS` | `3` | Extraction runs per input (1 to 10) |
| `WRITER_NEO4J_URI` | `bolt://localhost:7687` | Neo4j connection |
| `WRITER_NEO4J_USER` | `neo4j` | Neo4j user |
| `WRITER_NEO4J_PASSWORD` | `change-me` | **Secret.** Neo4j password |
| `WRITER_GOVERNANCE_DB_URL` | `sqlite:///./governance.sqlite` | Governance store |

Empty values in `.env` are treated as unset. Invalid values stop the service at startup.
