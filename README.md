# learn-neo4j-pipeline-writer

A learning project: a **writer service** that ingests knowledge from multiple sources
(PDF, Excel, Superset/MySQL), runs every candidate fact through an LLM-assisted
**verification and scoring pipeline**, and only then writes accepted facts into **Neo4j**,
following a governed ontology and taxonomy managed from an admin page.

```
 PDF / Excel upload ─┐                      ┌────────────── Admin UI (/admin/) ───────────────┐
 POST /ingest/...    │                      │ review queue · ontology editor + graph · runs   │
 Superset dataset ───┤                      │ mappings · scoring policy · audit · users       │
                     ▼                      └───────────────────────┬─────────────────────────┘
          ┌─────────────────────── writer service (FastAPI) ────────┴───────────────────────┐
          │ 1 Extract   LLM × N self-consistency runs (documents) │ approved column mapping │
          │ 2 Match     entity resolution against the graph (keys, aliases, fuzzy names)  │
          │ 3 Gates     schema · constraints · conflicts  (fail → reject / conflict)       │
          │ 4 Signals   grounding · back-translation judge · stability · source · match   │
          │ 5 Score     weighted, explained breakdown → commit / review / reject           │
          │ 6 Write     one transaction per run, accepted facts only, full provenance      │
          └────────────┬─────────────────────────────────────────────────┬────────────────┘
                       ▼                                                 ▼
             Governance store (MySQL)                              Neo4j (facts only)
             ontology versions · candidates · scores               (:Issuer)-[:FILED_BY]-(:Filing)
             review items · mappings · runs · audit log            + :Evidence, :Source, :IngestionRun
```

- **Design**: [`docs/design.md`](docs/design.md)
- **Decisions**: [`docs/adr/`](docs/adr/README.md)
- **What changed**: [`CHANGELOG.md`](CHANGELOG.md)
- **How work is done**: [`CONTRIBUTING.md`](CONTRIBUTING.md) and the GitHub issue list

## Try it in one command

No Docker, no API key: the demo server uses in-memory stores and a scripted LLM.

```sh
uv sync && ./scripts/demo.sh
```

It walks the whole story and checks every step (CI runs it too):

1. **upload** the sample annual report PDF → 3 extraction runs, voting, gates, scoring
2. the **reviewer** reads the review queue: evidence, page, score breakdown
3. the reviewer **approves** a fact → written to the graph under its own approval run
4. the **admin** has the LLM draft a Superset **column mapping**, then approves it
5. **structured sync** of the Superset dataset → rows become facts deterministically and commit
6. the admin **rolls back** the document run → its open review items close, the approval stays
7. the **audit log** shows every action

To click through the same thing in the browser:
`(cd admin-ui && bun install && bun run build) && uv run writer-demo-server` and open
http://127.0.0.1:8000/admin/ (the passwords and ingestion key are printed on startup).

## How a fact gets into Neo4j

Take one sentence from the sample PDF: *"Aurora Pacific Holdings (Demo) issued this annual
report AR-2025-APH"*.

1. **Extract.** The LLM is asked for facts that fit the *published ontology* only (its classes,
   properties and relations are enums in the output schema), each with a verbatim quote and a
   page locator. This runs N times (`WRITER_SELF_CONSISTENCY_RUNS`, default 3); facts are
   voted on, so a one-off hallucination is dropped. Superset rows skip the LLM: an
   admin-approved column mapping turns them into facts deterministically.
2. **Match.** `AR-2025-APH` and `Aurora Pacific Holdings (Demo)` are resolved to existing nodes
   (exact key, alias or fuzzy name), so the same issuer is never created twice.
3. **Gates.** Hard checks that no score can override: the class/relation must exist with the
   right domain and range; cardinality must hold; and a fact that **contradicts** an accepted
   one (a filing with a second issuer) becomes a *conflict* for a human, never an overwrite.
4. **Signals.** Is the quote really in the page (grounding)? Does an independent LLM judge,
   reading the source, agree (back-translation)? How many of the N runs found it (stability)?
   How reliable is the source? How well did it match?
5. **Score and route** (below). 6. **Write**: one Neo4j transaction per run, accepted facts
   only, each carrying `score`, `ontology_version`, `run_id` and links to its `:Evidence`.
   Any run can be **rolled back**; it removes only what no other run also stated.

Details: [pipeline](docs/pipeline.md) · [gates](docs/gates.md) ·
[verification](docs/verification.md) · [matching](docs/matching.md).

## Scoring, explained

```
score = 0.25·grounding + 0.20·back_translation + 0.15·stability
      + 0.20·source_reliability + 0.10·entity_match + 0.10·self_confidence

  ≥ 0.85  commit to Neo4j      0.60–0.85  review queue      < 0.60  reject (kept, with reasons)
```

| | grounding | back-transl. | stability | source | match | self-conf. | **score** | |
|---|---|---|---|---|---|---|---|---|
| from the PDF | 1.00 | 0.80 | 0.67 (2/3) | 0.75 | 0.50 (new) | 0.90 | **0.80** | review |
| from Superset | 1.00 | 1.00 (no LLM involved) | 1.00 | 0.95 | 0.50 | 1.00 | **0.94** | commit |

The reviewer sees this breakdown for every item. Weights, thresholds and source reliabilities
are a versioned policy the admin can change (audited). Hard gates always win over the score.
More: [`docs/scoring.md`](docs/scoring.md). Accuracy against a hand-labelled golden set:
`uv run writer-eval` ([`docs/evaluation.md`](docs/evaluation.md)).

## Using a different LLM

Claude (Sonnet) is the default. Everything LLM-related goes through one small protocol, so
switching is configuration plus, for a new provider, one adapter class:

```sh
WRITER_LLM_MODEL=claude-opus-5-5          # another Claude model: just set it
WRITER_LLM_PROVIDER=fake                  # offline: replay recorded answers only
```

For another vendor: implement `complete_json(request) -> LLMResponse` (and a `model`
property), register it in `llm/factory.py` and `LLMProvider` in `config.py`, and set
`WRITER_LLM_PROVIDER`. Prompts, JSON-schema validation, retries, caching and the pipeline
don't change. See [`docs/llm.md`](docs/llm.md#switching-provider).

## What's in it

Built issue by issue (see the [issues](https://github.com/cherrythia/learn-neo4j-pipeline-writer/issues)),
each with its own docs page:

- service skeleton, settings, CI and the admin UI skeleton
- local demo environment ([docs](docs/demo-environment.md))
- governance store ([docs](docs/governance-store.md))
- LLM layer ([docs](docs/llm.md))
- core ontology and publish workflow ([docs](docs/ontology.md))
- scoring engine ([docs](docs/scoring.md))
- authentication and roles ([docs](docs/auth.md))
- candidate facts and hard gates ([docs](docs/gates.md))
- verification signals ([docs](docs/verification.md))
- accuracy evaluation against a golden set, `writer-eval` ([docs](docs/evaluation.md))
- entity matching ([docs](docs/matching.md))
- document ingestion: PDF/Excel extraction ([docs](docs/document-ingestion.md))
- the writer pipeline and Neo4j writer, `POST /ingest/documents` ([docs](docs/pipeline.md))
- Superset/MySQL ingestion with approved column mappings, `POST /ingest/superset/{id}`
  ([docs](docs/superset-ingestion.md))
- admin JSON API for ontology, review queue, mappings, scoring, runs, audit and users
  ([docs](docs/admin-api.md))
- admin UI (React + Mantine) at `/admin/`: review queue with score breakdowns, ontology
  editor and graph, proposals, mappings, scoring, runs, audit, users ([docs](docs/admin-ui.md))

## Running the real service

Requirements: [uv](https://docs.astral.sh/uv/), [Bun](https://bun.sh), Docker.

```sh
cp .env.example .env                 # set WRITER_ANTHROPIC_API_KEY (and change the dev secrets)
docker compose up -d --wait          # Neo4j :7474/:7687, MySQL :3306, mock Superset :8088
uv sync
uv run writer-db upgrade             # governance tables
uv run writer-ontology bootstrap     # publish the core ontology + Neo4j constraints
uv run writer-users create alice --role admin
uv run writer-users create rita --role reviewer
(cd admin-ui && bun install && bun run build)
uv run uvicorn writer_service.app:create_app --factory   # http://localhost:8000/admin/

curl -F file=@samples/aurora_pacific_annual_report_2025.pdf \
     -H "X-API-Key: $WRITER_INGEST_API_KEY" http://localhost:8000/ingest/documents
```

The compose stack holds fictional compliance data, the sample PDF and Excel files, and an
optional real Superset (`--profile superset`): [`docs/demo-environment.md`](docs/demo-environment.md).
Use the MySQL governance URL from `.env.example` to keep governance data in MySQL rather
than SQLite. For UI development, `cd admin-ui && bun run dev` (http://localhost:3000, proxies
the API).

Checks: `./scripts/check.sh` (lint, types, tests, UI). Accuracy: `uv run writer-eval`.

## Configuration

All settings use the `WRITER_` prefix. See [`.env.example`](.env.example).

| Variable | Default | Purpose |
|---|---|---|
| `WRITER_APP_ENV` | `dev` | `dev`, `test` or `prod` |
| `WRITER_LLM_PROVIDER` | `anthropic` | `anthropic`, or `fake` to replay recorded answers offline |
| `WRITER_LLM_MODEL` | `claude-sonnet-5-5` | Model id |
| `WRITER_ANTHROPIC_API_KEY` | *(none)* | **Secret.** Required when provider is `anthropic` (the LLM factory fails fast without it) |
| `WRITER_LLM_EFFORT` | `high` | Claude effort: `low` to `max` |
| `WRITER_LLM_FALLBACKS` | `true` | Server-side fallback when Claude declines a request |
| `WRITER_LLM_MAX_ATTEMPTS` | `2` | Attempts per call when output is not JSON, truncated or fails the schema |
| `WRITER_LLM_CACHE_DIR` | `data/cache/llm` | Cache of validated LLM answers |
| `WRITER_SELF_CONSISTENCY_RUNS` | `3` | Extraction runs per input (1 to 10) |
| `WRITER_NEO4J_URI` | `bolt://localhost:7687` | Neo4j connection |
| `WRITER_NEO4J_USER` | `neo4j` | Neo4j user |
| `WRITER_NEO4J_PASSWORD` | `change-me-neo4j` | **Secret.** Neo4j password (min 8 chars) |
| `WRITER_INGEST_API_KEY` | *(none)* | **Secret.** `X-API-Key` for ingestion; unset = ingestion refused |
| `WRITER_ADMIN_JWT_SECRET` | *(random in dev)* | **Secret.** Signs admin sessions; 32+ chars, required in prod |
| `WRITER_SESSION_TTL_MINUTES` | `480` | Admin session length |
| `WRITER_COOKIE_SECURE` | `false` | Secure cookie flag; must be `true` in prod |
| `WRITER_MAX_UPLOAD_MB` | `20` | Upload size limit for `/ingest/documents` |
| `WRITER_ADMIN_UI_DIR` | `admin-ui/dist` | Built admin UI served at `/admin/` (not mounted if missing) |
| `WRITER_GOVERNANCE_DB_URL` | `sqlite:///./governance.sqlite` | Governance store |

Empty values in `.env` are treated as unset. Invalid values stop the service at startup.
