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
| 5 Review | Route: commit / review / conflict / reject. A **non-verbatim quote (grounding < 1.0) is never auto-committed**, whatever the score, and neither is a **possible duplicate** (or a relation to one; reason `duplicate`). Every candidate is stored with its breakdown, and review/conflict items open in the queue | [matching](matching.md) |
| 6 Commit | All `commit` facts written in **one Neo4j transaction** | `graph_store.py` |

## What lands in Neo4j

Only accepted facts ([ADR-0010](adr/0010-neo4j-accepted-facts-only.md)):

```
(:HardwareAsset:Asset {asset_tag, hardware_type, …, ontology_version, score, run_id, ingested_at})
   -[:SUPPORTED_BY]->(:Evidence {id, locator, quote, run_id})-[:FROM]->(:Source {id, type, ref})
(:HardwareAsset)-[:ASSIGNED_TO {score, run_id, ontology_version, evidence_ids: [...]}]->(:Department)
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

Every node, relationship, evidence item and `SUPPORTED_BY` link keeps a `runs` list of all
runs that wrote or referenced it. Entity nodes also keep `asserted_by`: the runs that *stated
the entity itself*, as opposed to only pointing a relationship at it. `rollback(run)` removes
the run from those lists and:

- deletes **only what no other run still owns**, so rolling back an old run never deletes an
  entity, edge or quote a later run (or an approval) also stated;
- **demotes an entity to a key-only stub** when no asserting run remains. If run1 stated
  *Department FIN, name Finance* and run2 only assigned a laptop to FIN, rolling back run1
  keeps FIN as run2's endpoint but drops run1's name and evidence links;
- repairs provenance: `evidence_ids` are pruned to evidence that still exists, and `run_id` on
  nodes, relationships and evidence falls back to the latest remaining run.

**Limitation:** `rollback(run_id)` does not restore property *values* a run overwrote on an
entity another run also asserts, and it doesn't remove properties the run *added* to such an
entity (there is no general property history). The governance store keeps every candidate,
so a previous value can be found and re-applied. (A *failed* ingest or approval is different:
when the governance commit fails after the graph write, its overwrites are undone exactly with
`undo(changes)`; see below.)
Rolling back a run also **closes its open review items** (`rejected`, note "run rolled back").

## Review decisions

```python
pipeline.approve(review_item_id, reviewer, edited=None, merge=False)
pipeline.reject(review_item_id, reviewer, note)
pipeline.rollback(run_id, actor)
```

- An approval is written under its **own graph run** (`approval-<item id>`), so it doesn't depend
  on, and isn't removed by, a rollback of the ingestion run that proposed it.
- Review reasons: `score` (between the thresholds, or grounding < 1.0), `conflict`
  (contradicts an accepted fact) and `duplicate` (a possible duplicate, see below).
- The **schema gate** runs again (edits must still fit the ontology). For a `score` or
  `duplicate` item the **constraint gate** runs again too: if the graph changed since ingest and the fact now
  contradicts it, the item becomes a `conflict` (still open) and nothing is written.
- **Approving a conflict** means the new fact *replaces* the old one, as far as the relation's
  cardinality requires: `many_to_one` drops the source's other targets, `one_to_many` the
  target's other sources, `one_to_one` both. The removed edges are recorded in the audit entry.
- **Duplicates.** Approving a `duplicate` item keeps the candidate as a **new** entity.
  `merge=True` instead rewrites its identity key (for a relation: every flagged endpoint) to the
  existing entity's key, so the fact lands on that node. Where the existing entity already has
  a different value (typically the variant name that made it look like a duplicate), the
  existing value is kept; the audit entry records `merged_into` and the values not written
  (`kept_existing`). The item is stored as `edited`.
  - Merge works on any item that carries `duplicate_of`: a relation to a possible duplicate
    that also contradicts the graph is a `conflict` item with `duplicate_of`.
  - The merge target must be an accepted (non-stub) entity in the graph: the matcher also
    compares against new entities of the same batch, which may never be accepted (409).
  - A merged fact always goes through the constraint gate; if it contradicts the graph,
    nothing is written and the item stays as it was, with `merge_conflict` (409).
  - Keeping a relation to a possible duplicate as new needs that entity accepted first
    (approve its entity item); otherwise the relation would create it as a stub (409).
  - Merging an item without `duplicate_of` is an error (409), and so is merging a relation
    whose new entity was kept as new (that would split the entity).
  - Keeping a relation as new rewrites the endpoint to the entity's stored key (Neo4j keys are
    case-sensitive).
  - **Merges stick.** An approved merge records the keys it rewrote (`merged`) on its item.
    Later ingests rewrite such a key to the merged entity before matching, the way the merge
    did (differing values of the merged entity are kept; the note lists them), so re-syncing
    the same dataset doesn't re-open the duplicate. An alias is used only while
    - its target is still an accepted entity and the merged-away key is *not* one (once a
      reviewer keeps that key as a real entity, the alias is ignored), and
    - the incoming entity's name still matches the target (similarity ≥ 0.90). Otherwise (a
      reused key, or no name to compare) it is **not** rewritten: it goes to review as a
      duplicate marked `previously_merged`. While that item is open the key stays blocked,
      so relations to it go to review too, in the same run or a later one (departments and
      assets are usually separate datasets). Keeping that item as new is the reviewer's
      decision that the key now names a different entity; it is allowed only if no newer
      merge of the key happened since (e.g. one of its relations merged into the target).
    - **Relations** are rewritten through an alias only when the same batch carries the
      entity under that key and it still fits the target. A relation on its own (e.g. an
      assets dataset synced separately) has no name to check, so it goes to review pointing
      at the target (`merged_earlier`): the reviewer merges it (one click) or, once the key's
      entity has been kept as new, keeps it. Nothing is ever rewritten onto the target unseen.
    - When the name also matches another entity, a `previously_merged` item records it as
      `also_matches`.
  - Merging an entity whose own key was already kept as a new entity is refused (409).
  - Keeping an entity as new under a key that has a valid merge alias is refused (409),
    except on a `previously_merged` item flagged against that same (latest) merge: in any
    order, one key can't be both merged and kept (relation merged first, or two items for
    the same key).
  - With `merge` and `edited` together, only endpoints that still point at the flagged key
    are rewritten; if none do, 409 "nothing to merge".
  - The approval audit records `merged_into`, every rewritten side (`merged`) and
    `kept_existing`.
- `write_run` returns **change records** for everything it destroyed: `edge` records for
  edges removed by conflict replacement, and `properties` records with the previous values of
  properties it overwrote on an existing entity (`null` = the property didn't exist).
  The audit entry's `replaced` field stores them JSON-safe (`audit_view`).
- If the governance commit fails after the graph write, the approval's graph run is rolled back
  **and `undo(changes)` puts back the removed edges and the overwritten property values**.
  Undo uses the raw values, so Neo4j temporal properties keep their type.
- Items of a run that isn't `completed` can't be approved or rejected.

Every action is in the audit log (`run.start`, `run.complete`, `run.failed`, `run.rollback`,
`review.approve`, `review.reject`, `review.reconflict`).

## HTTP

`POST /ingest/documents` (`X-API-Key`) uploads a `.pdf` or `.xlsx` (max `WRITER_MAX_UPLOAD_MB`,
default 20) and returns `{run_id, status, counts}` when the run finishes:

```sh
curl -H "X-API-Key: $WRITER_INGEST_API_KEY" -F file=@samples/zorblax_invoice_INV-2025-0418.pdf \
     http://localhost:8000/ingest/documents
```

Errors: 401 (key), 413 (size), 415 (type), 422 (unreadable), 409 (no published ontology),
503 (LLM not configured). A failed run returns `status: failed` with a generic message; the
internal error is stored with the run (`ingestion_runs.error`), not sent to the client.
The Neo4j driver is created once (thread-safe) and closed on shutdown. Without LLM
configuration the service still starts: review, rollback, ontology admin and Superset sync
work; only LLM-backed routes (document upload, mapping suggestions) answer 503.
