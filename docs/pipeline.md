# The writer pipeline

Every source (PDF, Excel, Superset) goes through one pipeline before anything reaches Neo4j.
Code: `src/writer_service/pipeline/orchestrator.py`, `graph_store.py`. Design: [§3](design.md#3-architecture).

```
                 ┌──────────────────────── one IngestionRun ────────────────────────┐
 source ──► 1 Ingest ──► 2 Match ──► 3 Compile ──► 4 Test ──► 5 Review ──► 6 Commit ──► Neo4j
            extract ×N    resolve      schema gate   signals    route        one tx,
            + vote        entity refs  constraint    + score    store all    MERGE +
                                       gate                     candidates   provenance
                 └───────────────── governance DB: run, candidates, review items, audit ─┘
```

| Step | What happens | Code |
|---|---|---|
| 1 Ingest | Documents: extraction runs **N times** (`WRITER_SELF_CONSISTENCY_RUNS`); facts in a majority survive, with `stability = votes/N`. Structured sources: deterministic candidates, stability 1.0 | [verification](verification.md) |
| 2 Match | Entity references resolved to existing keys (fuzzy names, case), duplicates flagged | [matching](matching.md) |
| 3 Compile | Schema gate → **reject**; constraint gate → **conflict** | [gates](gates.md) |
| 4 Test | Grounding (exact page/row), back-translation (LLM-extracted only), stability, entity match, self-confidence → weighted score | [scoring](scoring.md) |
| 5 Review | Route: commit / review / conflict / reject. A **non-verbatim quote (grounding < 1.0) is never auto-committed**, whatever the score. Every candidate is stored with its breakdown, and review/conflict items open in the queue | |
| 6 Commit | All `commit` facts written in **one Neo4j transaction** | `graph_store.py` |

## What lands in Neo4j

Only accepted facts ([ADR-0010](adr/0010-neo4j-accepted-facts-only.md)):

```
(:Issuer:Organisation {ticker, name, …, ontology_version, score, run_id, ingested_at})
   -[:SUPPORTED_BY]->(:Evidence {id, locator, quote, run_id})-[:FROM]->(:Source {id, type, ref})
(:Filing)-[:FILED_BY {score, run_id, ontology_version, evidence_ids: [...]}]->(:Issuer)
(:IngestionRun {id, ontology_version})
```

- Entities are `MERGE`d on their identity key and get their taxonomy parents' labels.
- Relationship endpoints that don't exist yet are created as `stub: true` nodes with just
  their key.
- Evidence ids are content hashes, so **re-ingesting the same document creates no duplicates**.

## Failure handling

| Failure | Result |
|---|---|
| Graph write fails | Nothing written (one transaction); candidates not stored; run `failed` with the error |
| Governance commit fails after the graph write | The run's graph changes are rolled back; run `failed` |
| No published ontology | Refused before anything starts |

## Operations

```python
pipeline.ingest(batch, actor)  # → RunSummary(run_id, status, counts)
pipeline.approve(review_item_id, reviewer, edited=None)  # commit a reviewed fact
pipeline.reject(review_item_id, reviewer, note)
pipeline.rollback(run_id, actor)  # remove what the run created
```

- **Approving a conflict** means the new fact *replaces* the old one (the source's other edges
  of that relation are removed first).
- **Edited approvals** go through the schema gate again.
- **Rollback** removes the nodes, relationships and evidence the run *created*. Properties it
  overwrote on pre-existing nodes are not restored (no property history); the governance store
  keeps every candidate if they need to be re-applied.

Every action is in the audit log (`run.start`, `run.complete`, `run.failed`, `run.rollback`,
`review.approve`, `review.reject`).

## HTTP

`POST /ingest/documents` (`X-API-Key`) uploads a `.pdf` or `.xlsx` (max `WRITER_MAX_UPLOAD_MB`,
default 20) and returns `{run_id, status, counts}` when the run finishes:

```sh
curl -H "X-API-Key: $WRITER_INGEST_API_KEY" -F file=@samples/aurora_pacific_annual_report_2025.pdf \
     http://localhost:8000/ingest/documents
```

Errors: 401 (key), 413 (size), 415 (type), 422 (unreadable), 409 (no published ontology),
503 (LLM not configured).
