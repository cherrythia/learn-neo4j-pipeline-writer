# Superset / MySQL ingestion

Structured rows become facts through an **admin-approved column mapping**, deterministically.
The LLM can draft the mapping but never interprets rows.
Code: `src/writer_service/ingestion/superset.py`. [ADR-0008](adr/0008-superset-mapping.md).

```
POST /ingest/superset/{dataset_id}   (X-API-Key)
  │ approved mapping for the dataset?  ── no ──► 409
  ▼
Superset REST API: dataset → table (or SQL), schema, columns   (SupersetClient)
  ▼
MySQL, read-only user: rows in pages of 500                    (DatasetReader)
  ▼
mapping: each row → entities / relations, one verbatim evidence item per column
  ▼
writer pipeline (gates, scoring, Neo4j)                        source reliability 0.95, stability 1.0
```

## Mappings

```json
{"entities":  [{"class_name": "HardwareAsset",
                "columns": {"asset_tag": "asset_tag", "name": "name", "status": "status"}}],
 "relations": [{"relation": "ASSIGNED_TO",
                "source": {"class_name": "HardwareAsset", "key_column": "asset_tag"},
                "target": {"class_name": "Department", "key_column": "dept_code"}}]}
```

| Step | Function | Notes |
|---|---|---|
| Draft (LLM) | `suggest_mapping(llm, spec, version, dataset, sample_rows)` | Output schema enumerates the ontology's classes, properties and relations **and the dataset's columns**, so it can't invent names |
| Draft (manual) | `save_draft(…, suggested_by="user")` | |
| Approve | `approve_mapping(uow, id, spec, dataset, actor=…)` | Validated against the published ontology and the real columns (identity key must be mapped); the previously approved mapping is **retired**; audited |
| Use | `approved_mapping(uow, dataset_id)` | Only approved mappings are ever used |

The admin UI and API for this workflow arrive in issues #14–#15.

## Evidence and grounding

Each row is rendered as text (`asset_tag: HW-23-0002; name: fin-lt-2302; …`) with the
locator `row <position> (<first column>=<value>)`, unique within a sync (the first column
alone may repeat). Every mapped column becomes its own evidence item
(`asset_tag: HW-23-0002`): a contiguous, verbatim fragment of the row text, so grounding is exactly 1.0
under the strict verbatim rule ([verification](verification.md)).

## Safety

- Table, schema and column names come from Superset and must be plain identifiers; anything
  else is refused. SQLAlchemy quotes them, and values are never interpolated.
- Virtual (SQL) datasets run the SQL Superset already holds, wrapped as a subquery, on the
  **read-only** `reader` user.
- At most `WRITER_MAX_ROWS_PER_SYNC` rows per sync. Pages are ordered by **every** column, so
  LIMIT/OFFSET paging can't skip or repeat rows when the first column isn't unique.
- Driver values are normalised before they become candidates: a DATETIME at midnight becomes a
  date, any other DATETIME ISO text, DECIMAL an int or float.

## Validation and errors

- A mapping is approved only for **its own** dataset, and only if every column exists, every
  class and relation is active, the identity key is mapped, and each relation end fits the
  relation's domain/range (a subclass is fine).
- `POST /ingest/superset/{id}`: 409 without an approved mapping; 404 for an unknown dataset;
  502 when Superset is unreachable or the source database can't be read (the response names
  the dataset only; SQL and driver details are logged).

## Configuration

| Variable | Default | |
|---|---|---|
| `WRITER_SUPERSET_URL` | `http://127.0.0.1:8088` | Mock locally; real Superset on `:8089` with `--profile superset` |
| `WRITER_SUPERSET_USERNAME` / `_PASSWORD` | `admin` / `change-me` | **Secret** password; set both for a real Superset |
| `WRITER_SOURCE_DB_URL` | `mysql+pymysql://reader:…@127.0.0.1:3306/demo_itam` | **Read-only** user |
| `WRITER_MAX_ROWS_PER_SYNC` | 10000 | |
