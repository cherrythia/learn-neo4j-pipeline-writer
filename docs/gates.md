# Candidate facts and hard gates

What an extractor (LLM or column mapping) produces, and the first checks every candidate must
pass before it is scored. Code: `src/writer_service/pipeline/`. See also
[scoring](scoring.md) and [ontology](ontology.md).

## Candidates (`pipeline/candidates.py`)

```json
{"kind": "entity", "class_name": "HardwareAsset",
 "properties": {"asset_tag": "HW-25-0101", "hardware_type": "laptop", "model": "Strata 14"},
 "evidence": [{"source_type": "pdf", "source_ref": "samples/zorblax_invoice….pdf",
               "locator": "page 1", "quote": "HW-25-0101 Laptop, Corvane (Demo) Strata 14…"}],
 "confidence": 0.9}

{"kind": "relation", "relation": "ASSIGNED_TO",
 "source": {"class_name": "HardwareAsset", "key": "HW-25-0101"},
 "target": {"class_name": "Department", "key": "FIN"}, "evidence": […], "confidence": 0.8}
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
| no undeclared properties | `schema: class 'HardwareAsset' has no property 'colour'` |
| required properties present | `schema: required property 'asset_tag' is missing` |
| values coerce to declared types | `schema: property 'expiry_date' expects date, got '31/10/2026'` |
| relation exists, not deprecated | `schema: unknown relation 'OWNS'` |
| endpoints match domain → range (taxonomy-aware) | `schema: ASSIGNED_TO expects HardwareAsset -> Department, got Department -> Department` |
| endpoint keys non-empty; relations carry no properties | `schema: source key is empty` |

Coercion: strings get whitespace collapsed; `int` accepts integers or digit strings (not
booleans); `float` accepts numbers or numeric strings without separators (`2150.00`, not
`2,150.00`); `date` accepts dates or ISO `YYYY-MM-DD` strings; `bool` accepts true/false,
yes/no, 1/0. Ambiguous formats (e.g. `31/10/2026`) are rejected rather than guessed.

Taxonomy-aware endpoints: `BILLED_ON`, whose domain is `Asset`, accepts a `HardwareAsset` or
a `SoftwareLicense` (subclasses); the reverse is rejected (`ASSIGNED_TO` expects a
`HardwareAsset`, and a plain `Asset` is not one).

## Constraint gate → conflict

Checks against **accepted facts in Neo4j** (via `GraphView`) and against **earlier candidates
in the same batch**:

| Check | Example conflict |
|---|---|
| property contradiction | `HardwareAsset 'HW-25-0101': property 'status' is 'in_use' in the graph, candidate says 'retired'` |
| `many_to_one` / `one_to_one` (source side) | `ASSIGNED_TO is many_to_one: HardwareAsset 'HW-25-0101' already points to Department 'FIN' in the graph, candidate says Department 'ENG'` |
| `one_to_many` / `one_to_one` (target side) | `SHIPPED is one_to_many: Invoice 'INV-1' already has source Vendor 'V1' …` |

Cardinality looks at existing edges to **any subclass** of the relation's range (or domain):
a node is stored with its taxonomy parents' labels (a `HardwareAsset` node is also an
`Asset`), so a relation over a parent class such as `BILLED_ON` (domain `Asset`) sees the
edges of every kind of asset.

Keys: the schema gate canonicalises them (whitespace collapsed) and the writer stores them that
way; matching then ignores case. In Neo4j the lookup tries the exact key first (served by the
identity-key uniqueness index), then the class's full-text index for a case-insensitive match
([matching](matching.md#lifecycle-and-performance)); a label scan only without that index.
Re-asserting a known fact passes. A conflict is
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
