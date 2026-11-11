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

## Run ownership and rollback

Every node, relationship and evidence item keeps a `runs` list of all runs that wrote or
referenced it. `rollback(run)` removes the run from those lists and deletes **only what no other
run still owns**:

- rolling back an old run never deletes an entity a later run (or an approval) also stated;
- shared evidence (the same quote ingested twice) stays while any run still has it;
- relationship provenance is repaired: `evidence_ids` are pruned to evidence that still
  exists, and `run_id` falls back to the latest remaining run.

Property *values* a run overwrote are not restored (there is no property history); the
governance store keeps every candidate, so a previous value can be found and re-applied.
Rolling back a run also **closes its open review items** (`rejected`, note "run rolled back").

## Review decisions

```python
pipeline.approve(review_item_id, reviewer, edited=None)
pipeline.reject(review_item_id, reviewer, note)
pipeline.rollback(run_id, actor)
```

- An approval is written under its **own graph run** (`approval-<item id>`), so it doesn't depend
  on, and isn't removed by, a rollback of the ingestion run that proposed it.
- The **schema gate** runs again (edits must still fit the ontology). For a score-routed item
  the **constraint gate** runs again too: if the graph changed since ingest and the fact now
  contradicts it, the item becomes a `conflict` (still open) and nothing is written.
- **Approving a conflict** means the new fact *replaces* the old one, as far as the relation's
  cardinality requires: `many_to_one` drops the source's other targets, `one_to_many` the
  target's other sources, `one_to_one` both. The removed edges are recorded in the audit entry.
- If the governance commit fails after the graph write, the approval's graph run is rolled back.
- Items of a run that isn't `completed` can't be approved or rejected.

Every action is in the audit log (`run.start`, `run.complete`, `run.failed`, `run.rollback`,
`review.approve`, `review.reject`, `review.reconflict`).

## HTTP

`POST /ingest/documents` (`X-API-Key`) uploads a `.pdf` or `.xlsx` (max `WRITER_MAX_UPLOAD_MB`,
default 20) and returns `{run_id, status, counts}` when the run finishes:

```sh
curl -H "X-API-Key: $WRITER_INGEST_API_KEY" -F file=@samples/aurora_pacific_annual_report_2025.pdf \
     http://localhost:8000/ingest/documents
```

Errors: 401 (key), 413 (size), 415 (type), 422 (unreadable), 409 (no published ontology),
503 (LLM not configured). A failed run returns `status: failed` with a generic message; the
internal error is stored with the run (`ingestion_runs.error`), not sent to the client.
The Neo4j driver is created once (thread-safe) and closed on shutdown.
