# Local demo environment

Everything here is **fictional** and exists so you can run the whole pipeline without real
Superset data. Issue #2, [ADR-0013](adr/0013-local-demo-environment.md).

## Start it

```sh
cp .env.example .env                     # dev-only passwords; change for anything shared
docker compose up -d --wait              # Neo4j + MySQL + mock Superset
docker compose --profile superset up -d  # optional: real Apache Superset on :8089
docker compose --profile superset run --rm superset-bootstrap   # register demo datasets
./scripts/smoke_demo.sh                  # check data, grants, mock Superset, Neo4j
```

| Service | URL | Login |
|---|---|---|
| Neo4j Browser | http://localhost:7474 | `neo4j` / `WRITER_NEO4J_PASSWORD` |
| MySQL | `localhost:3306` | `writer` (governance DB) / `reader` (read-only demo data) |
| Mock Superset API | http://localhost:8088/docs | `SUPERSET_ADMIN_USERNAME` / `SUPERSET_ADMIN_PASSWORD` |
| Real Superset (optional) | http://localhost:8089 | same as above |

All ports bind to `127.0.0.1` only.

## What's inside

**MySQL `demo_compliance`** (a fictional listing-compliance scenario):

| Table | Rows | Notes |
|---|---|---|
| `issuers` | 20 | Fictional companies, every name ends in `(Demo)`; Mainboard or Catalist |
| `rules` | 6 | Listing-rule clauses (annual report timing and contents, sustainability, climate) |
| `obligations` | 6 | Which rule applies to which board, with a deadline in days |
| `filings` | 40 | One annual and one ESG report per issuer for FY2025; **3 are late** |

The late filings give the pipeline real findings to discover.

**MySQL `governance`**: empty; owned by the `writer` user. Tables arrive with issue #3.

**Sample documents** in `samples/`:
- `aurora_pacific_annual_report_2025.pdf`: 2 pages, an annual report and an ESG statement
  that mention issuers, rules and one finding (for the PDF path, issue #11).
- `disclosure_checklist_2025.xlsx`: sheet `Checklist` with issuer, rule, requirement and
  status columns (for the Excel path).

## Mock vs real Superset

The mock implements only what our connector calls, with the same response shapes as Superset:

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/security/login` | Returns `access_token` |
| `GET /api/v1/dataset/` | Lists the 4 demo datasets |
| `GET /api/v1/dataset/{id}` | Table name, schema, database backend, columns |

Switching to real Superset is a URL change (`:8088` to `:8089`, or your own server).

## One source of truth

`src/demo_env/data.py` defines all demo rows. The following are **generated** from it:

```sh
uv run python -m demo_env.seed_sql   # infra/mysql/init/01_seed.sql
uv run python -m demo_env.samples    # samples/*.pdf, samples/*.xlsx
```

A test fails if the committed seed file is out of date. MySQL runs the init scripts only on
first start; after changing data run `docker compose down -v` to reset the volumes.
