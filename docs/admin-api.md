# Admin API

The JSON API behind the [admin UI](admin-ui.md). Code: `src/writer_service/api/admin.py`. Interactive docs at
`http://localhost:8000/docs` once the service runs. The schema is exported to
`admin-ui/src/api/openapi.json` (`uv run writer-openapi`; a test fails if it's stale).

**Auth**: log in with `POST /api/auth/login` ([auth](auth.md)). Every state-changing call needs
`X-Requested-With: writer-admin`. `reviewer` can read and work the review queue; everything
that changes the standard or removes data needs `admin`. Every change is audited.

## Ontology ([ontology](ontology.md))

| Method | Path | Role | |
|---|---|---|---|
| GET | `/api/ontology/versions` | reviewer | all versions with status |
| GET | `/api/ontology/versions/{n}` | reviewer | classes (taxonomy, properties) and relations |
| POST | `/api/ontology/drafts` | admin | copy the published version into a new draft |
| POST | `/api/ontology/versions/{n}/classes` | admin | add a class to a draft |
| PATCH | `/api/ontology/versions/{n}/classes/{name}` | admin | description, deprecate, add properties |
| POST | `/api/ontology/versions/{n}/relations` | admin | add a relation to a draft |
| PATCH | `/api/ontology/versions/{n}/relations/{name}` | admin | description, deprecate |
| POST | `/api/ontology/versions/{n}/publish` | admin | publish gate + Neo4j constraints (422 with reasons) |
| POST | `/api/ontology/rollback` | admin | re-publish the previous version |

Only drafts can be edited (409 otherwise). Removing things isn't offered: deprecate instead.

## Extension proposals

| Method | Path | Role | |
|---|---|---|---|
| GET | `/api/proposals?status=pending` | reviewer | |
| POST | `/api/proposals` | reviewer | `{kind: class\|relation, payload, rationale}` (payload validated) |
| POST | `/api/proposals/{id}/approve` | admin | added to the current draft (created if needed) in the `ext` namespace, in one transaction: on any error (e.g. 422 unknown parent) no draft is left behind and the proposal stays pending |
| POST | `/api/proposals/{id}/reject` | admin | |

An approved proposal reaches the graph when the draft is published.

## Review queue ([pipeline](pipeline.md))

| Method | Path | Role | |
|---|---|---|---|
| GET | `/api/review?status=open&reason=conflict` | reviewer | candidate, evidence, score breakdown, conflict, source |
| POST | `/api/review/{id}/approve` | reviewer | `{edited?: candidate}`: re-gated, written to Neo4j |
| POST | `/api/review/{id}/reject` | reviewer | `{note?}` |

## Column mappings ([superset](superset-ingestion.md))

| Method | Path | Role | |
|---|---|---|---|
| GET | `/api/mappings` | admin | |
| POST | `/api/mappings` | admin | manual draft `{dataset_id, mapping}` |
| POST | `/api/mappings/suggest/{dataset_id}` | admin | LLM draft from columns and 5 sample rows |
| POST | `/api/mappings/{id}/approve` | admin | validated (422 with reasons); retires the previous mapping |

## Scoring ([scoring](scoring.md)), runs, audit, users

| Method | Path | Role | |
|---|---|---|---|
| GET / PUT | `/api/scoring` | reviewer / admin | the active policy; PUT is validated (422) and keeps history |
| GET | `/api/runs`, `/api/runs/{id}` | reviewer | runs with counts; detail lists every candidate with its decision |
| POST | `/api/runs/{id}/rollback` | admin | ownership-aware rollback; closes the run's open review items |
| GET | `/api/audit?limit=` | admin | newest first |
| GET / POST | `/api/users` | admin | create `{username, password, role}` |
| PATCH | `/api/users/{username}` | admin | `{role?, active?}`; deactivation ends their sessions; you can't demote or deactivate yourself |

## Tests

`tests/test_api/test_admin.py` builds the **permission matrix from the OpenAPI schema**: every
`/api` route is called without login (401), as a reviewer on admin routes (403), and as a
reviewer on reviewer routes (not 401/403). Each workflow above has an end-to-end test,
including *a reviewer cannot publish or roll back the ontology, and nothing changes*.
