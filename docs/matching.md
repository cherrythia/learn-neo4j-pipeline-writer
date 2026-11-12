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

In Neo4j (`pipeline/neo4j_lookup.py`, #28) both lookups use the class's **full-text index**
(`wsf_<label>_<fields>`, created with the ontology's constraints, see [ontology](ontology.md)):

The indexes use the `standard-folding` analyzer (lower-case + ASCII folding, so "Müller" is
stored as "muller"; English stop words are dropped). Analysed name clauses are only
lower-cased in Python ("İ" mapped to "i"; with a final "ς" also sent as "σ", which is how the
index stores an upper-case "Σ"), never normalised or decomposed, so the analyzer sees the
text as it saw the stored names (Hangul syllables, kana voicing marks, "™", NFD text). Only
the fuzzy terms, which Lucene doesn't analyse, are ASCII-folded in Python.

- **Name candidates**: for each distinct reference (cached per batch) the index returns the
  `TOP_K` = 50 best hits for the whole reference, analysed like the stored names (apostrophes,
  CJK, accents), **or** for its ASCII words of 4+ characters within one edit (typos; at most 16
  words). The matcher ranks the hits with `name_similarity` as before. Nothing reads every
  name.
- **Keys**: the exact key is tried first (uniqueness index); on a miss, a phrase query on the
  key field, then an exact case-insensitive check of the hits in Python (`match_key`). The
  index is only trusted for keys it handles exactly like the old scan: **ASCII keys with at
  least one non-stop-word token**. Other keys (non-ASCII, where the analyzer and Python/Cypher
  lower-case some scripts differently, or only stop words like `IT`, `ON`, `A`) are looked up
  by the scan, as is a key whose result was capped at `TOP_K` without an exact hit. Identity
  keys are overwhelmingly ASCII codes, so this stays rare.

The old scans are used without an ONLINE full-text index for the field (not synced yet,
still populating, or a non-string identity key, which full-text indexes can't hold) and when
a full-text query fails. **Limit:** name candidates are the index's best 50, so a name the
full scan would have ranked highest can only be missed if more than 50 names share the
reference's words and all outrank it in the index's own (BM25) scoring.
`tests/test_pipeline/test_fulltext_neo4j.py` checks against a real Neo4j, over the demo
issuers, near-twins, 120 near-identical names, apostrophe/CJK/Hangul/kana/accented/all-caps
Greek/NFD names, dotted-İ, Hangul, Greek, NFD and "™" keys and a key with more than 50 phrase
hits, that the full-text path gives the same decisions as the
full scan (status, key, confidence, best alternative and the alternatives' scores; among
*equally* scored names the listed keys may differ), and that ASCII lookups never scan.

If the analyzer or other index options ever change, change the index name too (sync creates
missing names; it doesn't compare options).
