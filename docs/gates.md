# Candidate facts and hard gates

What an extractor (LLM or column mapping) produces, and the first checks every candidate must
pass before it is scored. Code: `src/writer_service/pipeline/`. See also
[scoring](scoring.md) and [ontology](ontology.md).

## Candidates (`pipeline/candidates.py`)

```json
{"kind": "entity", "class_name": "Issuer",
 "properties": {"ticker": "APH", "name": "Aurora Pacific Holdings (Demo)"},
 "evidence": [{"source_type": "pdf", "source_ref": "samples/aurora….pdf",
               "locator": "page 1", "quote": "Aurora Pacific Holdings (Demo) (ticker APH)…"}],
 "confidence": 0.9}

{"kind": "relation", "relation": "FILED_BY",
 "source": {"class_name": "Filing", "key": "AR-2025-APH"},
 "target": {"class_name": "Issuer", "key": "APH"}, "evidence": […], "confidence": 0.8}
```

- **`fact_hash`**: what the fact says, ignoring whitespace, case, property order, evidence and
  confidence. The same fact from three LLM runs or two sources hashes identically; this is how
  self-consistency votes (issue #8) and evidence is merged.
- **`identity_hash`**: class + identity key only, i.e. "the same entity", even if properties differ.

## Schema gate → reject

Checks the candidate against the **published** ontology and normalises it:

| Check | Example failure |
|---|---|
| class exists, not deprecated | `schema: unknown class 'Company'` |
| no undeclared properties | `schema: class 'Issuer' has no property 'ceo'` |
| required properties present | `schema: required property 'ticker' is missing` |
| values coerce to declared types | `schema: property 'due_on' expects date, got '30/04/2026'` |
| relation exists, not deprecated | `schema: unknown relation 'OWNS'` |
| endpoints match domain → range (taxonomy-aware) | `schema: FILED_BY expects Filing -> Issuer, got Issuer -> Issuer` |
| endpoint keys non-empty; relations carry no properties | `schema: source key is empty` |

Coercion: strings get whitespace collapsed; `int` accepts integers or digit strings (not
booleans); `date` accepts dates or ISO `YYYY-MM-DD` strings; `bool` accepts true/false,
yes/no, 1/0. Ambiguous formats (e.g. `30/04/2026`) are rejected rather than guessed.

Taxonomy-aware endpoints: a relation whose domain is `Organisation` accepts an `Issuer`
(subclass); the reverse is rejected (a parent is not a child).

## Constraint gate → conflict

Checks against **accepted facts in Neo4j** (via `GraphView`) and against **earlier candidates
in the same batch**:

| Check | Example conflict |
|---|---|
| property contradiction | `Issuer 'APH': property 'board' is 'mainboard' in the graph, candidate says 'catalist'` |
| `many_to_one` / `one_to_one` (source side) | `FILED_BY is many_to_one: Filing 'AR-2025-APH' already points to 'APH' in the graph, candidate says 'BYL'` |
| `one_to_many` / `one_to_one` (target side) | `ISSUED is one_to_many: Filing 'F1' already has source 'APH' …` |

Comparison ignores whitespace and case, so re-asserting a known fact passes. A conflict is
**never** auto-resolved: it goes to the review queue with both facts side by side
([ADR-0010](adr/0010-neo4j-accepted-facts-only.md)).

Disjointness is structural: nodes are merged per class label on the identity key, so one node
can't become two unrelated classes.

## Using it

```python
from writer_service.pipeline.gates import check_batch
from writer_service.pipeline.graph_view import Neo4jGraphView

results = check_batch(candidates, published_spec, Neo4jGraphView(driver))
for r in results:
    r.status  # "pass" → score it; "reject" → r.failures; "conflict" → r.conflict
```
