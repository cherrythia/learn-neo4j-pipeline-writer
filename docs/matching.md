# Entity matching

Resolves what an extractor wrote to entities that already exist, so the graph doesn't fill
up with duplicates. Code: `src/writer_service/pipeline/matching.py`. Feeds the scoring
component `entity_match` ([scoring](scoring.md)).

## Why

LLMs often reference an entity by its *name* ("Aurora Pacific Holdings") rather than its
identity key (`APH`), or propose an entity that duplicates an existing one under a new key.

## Outcomes

| Status | When | Effect | `entity_match` |
|---|---|---|---|
| `exact` | identity key exists (case-insensitive) | key **rewritten to the stored spelling** if it differs (Neo4j keys are case-sensitive) | 1.0 |
| `fuzzy` | text matches one entity's name/title ≥ 0.90, clear of the runner-up by ≥ 0.05 | relation endpoint **rewritten** to that entity's key | the similarity (≤ 0.99) |
| `ambiguous` | best match is only partial (0.75–0.90), or two entities are within 0.05 | nothing rewritten; listed alternatives | 0.3 (sends the fact to review) |
| `new` | nothing ≥ 0.75 | none | None (neutral 0.5 in scoring) |
| `possible_duplicate` | an entity candidate's key is new but its name matches an existing entity | flagged with the other key | `1 − similarity` (near 0) |

Thresholds are constructor arguments (`threshold`, `margin`, `floor`).

## Name similarity

`name_similarity(a, b)` = average of rapidfuzz **token-set** and **token-sort** ratios after
lower-casing and removing punctuation.

| Pair | Score |
|---|---|
| "aurora pacific holdings" vs "Aurora Pacific Holdings (Demo)" | ≈ 0.94 |
| "Pacific" vs "Aurora Pacific Holdings (Demo)" | ≈ 0.7: one shared word isn't a match |
| "Granite Bay Bank" vs "Aurora Pacific Holdings" | < 0.5 |

Token-set alone would score "Pacific" as a perfect match; averaging with token-sort prevents that.

The class's `name` property is used, or `title` if it has none (e.g. `Rule`). Classes with
neither match only by exact key.

## Relations

`resolve_relation(rel)` resolves both endpoints and returns the rewritten relation, a
confidence equal to the **weakest known endpoint** (new endpoints are neutral; None if both
are new), and human-readable notes such as
*"target Issuer 'Aurora Pacific Holdings' resolved to 'APH' (similarity 0.94)"* for the
reviewer.

Entities proposed earlier in the same batch are matchable too (`add_batch_entity`), so a PDF
that introduces an issuer and then references it by name resolves correctly.

## Lifecycle and performance

Use **one matcher per batch**, or call `reset_batch()` between batches. Batch-local entities
are proposals, not accepted facts, so a later batch must not resolve to them.

Within a batch, the name list of each class is read **once** and cached. Each lookup of a key
that isn't an exact match still costs one case-insensitive scan in Neo4j (the exact match uses
the uniqueness index). That's fine at demo scale; for large graphs, a full-text index on names
is the next step (issue #28).
