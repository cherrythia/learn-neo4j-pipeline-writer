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

**MySQL `demo_itam`** (a fictional IT asset management CMDB; issue #41). Superset dataset ids
follow this order:

| Dataset | Table | Rows | Notes |
|---|---|---|---|
| 1 | `departments` | 9 | `dept_code`, `name` (e.g. `FIN` Finance, `ITS` IT Services) |
| 2 | `assets` | 29 | Hardware: asset tag, hostname, type (laptop/desktop/server/network), fictional manufacturer and model, serial, status, purchase date, cost, department; **3 are retired**, 1 in repair. The 5 assets bought on the sample invoice are included |
| 3 | `software` | 6 | Software products with fictional publishers |
| 4 | `applications` | 7 | Business applications with criticality, status and owning department |
| 5 | `software_installs` | 32 | Which software is installed on which (non-retired) asset (source of `INSTALLED_ON`) |
| 6 | `application_hosts` | 8 | Which application runs on which server (source of `RUNS_ON`) |
| 7 | `application_software` | 9 | Which software an application uses (source of `USES`) |

Every company-like name (vendor, customer, manufacturer, publisher) is invented and ends in
`(Demo)`; the vendor's address is "1 Example Way, Demo City 000000". The demo
walkthrough (`scripts/demo.sh`) syncs dataset 1 through an approved `Department` mapping; every
row commits with no review.

**MySQL `governance`**: empty; owned by the `writer` user. Tables arrive with issue #3.

**Sample documents** in `samples/`:
- `zorblax_invoice_INV-2025-0418.pdf`: a 3-page tax invoice from the fictional vendor
  Zorblax Systems Supply (Demo): hardware lines (3 laptops, a server, a network switch) with
  asset tags, serials and prices; two software licences with seats and software ids; totals;
  and delivery notes assigning each item to a department (for the PDF path). The golden
  evaluation set is labelled from it ([evaluation](evaluation.md)).
- `asset_register_2025.xlsx`: sheet `Register`, one row per hardware asset with asset tag,
  type, manufacturer, model, serial number, department and status (for the Excel path).

## Mock vs real Superset

The mock implements only what our connector calls, with the same response shapes as Superset:

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/security/login` | Returns `access_token` |
| `GET /api/v1/dataset/` | Lists the 5 demo datasets |
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

**Upgrading from the compliance demo (before #41).** An existing stack still has the old
`demo_compliance` schema, and the governance store still has the compliance ontology
published; `writer-ontology bootstrap` is a no-op once any version is published. Reset once:

```sh
rm -f governance.sqlite              # default WRITER_GOVERNANCE_DB_URL; if .env points at the
                                     # MySQL governance DB, drop and recreate that database instead
docker compose down -v && docker compose up -d --wait   # new MySQL seed, empty Neo4j
# in .env: WRITER_SOURCE_DB_URL=...:3306/demo_itam
uv run writer-db upgrade && uv run writer-ontology bootstrap
```
