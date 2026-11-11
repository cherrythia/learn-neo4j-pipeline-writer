# Scoring

How the writer service decides whether a candidate fact is written to Neo4j.
[ADR-0004](adr/0004-scoring-model.md). Code: `src/writer_service/scoring/`.

## 1. Hard gates first

Before any score is computed, a fact must pass the gates (issue #6):

| Gate | Fails when | Outcome |
|---|---|---|
| Schema | Class/relation not in the published ontology, wrong domain/range | **reject** |
| Constraint | Cardinality or disjointness violated | **reject** |
| Conflict | Contradicts an accepted fact (e.g. a second `FILED_BY` issuer) | **conflict**, always human review; never auto-overwritten |

A gate outcome overrides the score: a 0.99 that fails the schema gate is rejected.

## 2. The weighted score

Each component is a number in [0, 1]:

| Component | Meaning | Default weight |
|---|---|---|
| `grounding` | The quoted evidence span is really in the source (fuzzy match) | 0.25 |
| `back_translation` | An LLM judge restates the fact and checks it against the source | 0.20 |
| `stability` | Share of the N self-consistency runs that produced the fact | 0.15 |
| `source_reliability` | Per source: Superset/MySQL 0.95, Excel 0.85, PDF 0.75 | 0.20 |
| `entity_match` | Similarity to an existing node; a new entity counts as 0.5 | 0.10 |
| `self_confidence` | The extractor's own confidence (deliberately low weight) | 0.10 |

```
score = Σ weight_i × value_i        (weights sum to 1, so score ∈ [0, 1])
```

Weights are validated to sum to 1 within 1e-6 and then normalised exactly, so the score never
needs clamping and the contributions in the breakdown always add up to it.

`record_outcome(candidate, routing, score)` stores the breakdown, decision and reasons on the
`candidates` row; the writer pipeline (issue #10) calls it for every candidate.

## 3. Routing

| Score | Decision |
|---|---|
| ≥ 0.85 | **commit** to Neo4j |
| 0.60 – 0.85 | **review** queue in the admin page |
| < 0.60 | **reject** (kept, with the reason) |

Boundaries are inclusive (exactly 0.85 commits), with a tiny epsilon so float noise such as
0.8499999999 doesn't flip a decision.

## 4. Worked example

A PDF says *"Aurora Pacific Holdings (Demo) issued this annual report … on 28 April 2026"*.
The extractor proposes `(AR-2025-APH)-[:FILED_BY]->(APH)`:

| Component | Value | Why | × Weight | = |
|---|---|---|---|---|
| grounding | 0.90 | quote found with a small whitespace difference | 0.25 | 0.225 |
| back_translation | 0.80 | judge agrees, with a minor wording caveat | 0.20 | 0.160 |
| stability | 0.667 | found in 2 of 3 runs | 0.15 | 0.100 |
| source_reliability | 0.75 | PDF | 0.20 | 0.150 |
| entity_match | 0.50 | APH not in the graph yet (new entity) | 0.10 | 0.050 |
| self_confidence | 0.90 | extractor's own estimate | 0.10 | 0.090 |
| **score** | | | | **0.775** |

0.775 is between 0.60 and 0.85, so the fact goes to the **review queue**. The reviewer sees
this exact table (the `breakdown` stored with the candidate), the evidence span and the page.
The same fact from the Superset `filings` table would score higher: source 0.95, grounding
1.0 (the value is in the row) and stability 1.0 (deterministic mapping), so it commits.

## 5. Changing the policy

The policy (weights, thresholds, source reliabilities, N) is stored in `scoring_configs`.
Activating a new one keeps the old rows as history and is audited:

```python
from writer_service.scoring.store import activate_policy, active_policy

with UnitOfWork(engine) as uow:
    policy = active_policy(uow)  # design default if nothing stored
    new = ScoringPolicy.model_validate({**policy.model_dump(), "commit_threshold": 0.9})
    activate_policy(uow, new, actor="alice")  # re-validates anyway, before writing
```

Validation rejects weights that don't sum to 1, values outside [0, 1], a review threshold
that isn't below the commit threshold, and a reliability table that doesn't cover every
source (`pdf`, `excel`, `superset`). The admin page (issues #14–#15) edits this.

## 6. Guarantees (tested)

- The score is always in [0, 1] (property-based test over random inputs **and random valid
  policies**).
- The score never decreases when any component increases, including source reliability
  (monotonic; property-based).
- Contributions always add up to the score (the derivation is exact).
