# Design: Verified Writer Pipeline into Neo4j

Status: **Accepted** (2026-11-12). Agreed in a design interview; each decision has an ADR in
[`docs/adr/`](adr/).

## 1. Intent

Knowledge arrives from several places: uploaded **PDF** and **Excel** documents (read by an
LLM) and **MySQL tables behind Superset datasets**. Every source goes through a single
**writer service**. Before anything reaches Neo4j, the writer service:

1. checks it against a **governed ontology and taxonomy** (one shared standard for all sources),
2. **verifies** it (is it grounded in the source? does it survive back-translation? is it stable
   across repeated LLM runs?),
3. computes a transparent **score**, and
4. routes it: **commit**, **human review**, or **reject**.

The project is also meant for learning, so every step should be easy to inspect.

Inspiration (an example, not a spec): a "dynamic ontology + neuro-symbolic engine" slide with
three ontology layers, a six-step pipeline (Ingest, Match, Compile, Test, Review, Commit) and
the principle *"the LLM reads, the checks prove; it can miss, never invent"*.

## 2. Goals / non-goals / anti-goals

**Goals**
- One writer path for all sources; the same ontology standard everywhere.
- Every write is explainable: score breakdown, evidence span, source, run, ontology version.
- LLM non-determinism is measured and contained, not ignored.
- Admin page to manage ontology classes, relation types and taxonomy, and to review facts.
- Every change is tested; CI never calls a real LLM.

**Non-goals (for now)**
- SSO / enterprise identity (API key + local admin login only).
- A formal SMT/OWL reasoner. Constraint checks are explicit Python rules; a solver can be added
  behind the same gate interface later.
- Scaling to millions of documents.

**Anti-goals (a failure even if it "works")**
- Unverified or guessed facts silently written to Neo4j.
- Each source inventing its own class or relation names.
- Scores that cannot be explained.

## 3. Architecture

```
 PDF / Excel upload ──► Document extractor (LLM, N runs) ─┐
                                                          │  CandidateFacts + Evidence
 Superset dataset ──► MySQL reader ──► Column mapping ────┤
                                                          ▼
                         ┌──────────── Writer service pipeline ────────────┐
                         │ 1 Ingest   normalize + canonicalize + hash        │
                         │ 2 Match    resolve against existing Neo4j nodes   │
                         │ 3 Compile  schema gate (ontology), constraint gate│
                         │ 4 Test     grounding, back-translation, stability │
                         │            ──► weighted score + breakdown         │
                         │ 5 Review   route: commit / review queue / reject  │
                         │ 6 Commit   atomic MERGE per run + provenance      │
                         └───────────────────────────────────────────────────┘
                                 │                         │
                       Governance DB (MySQL)          Neo4j (accepted facts only)
            ontology versions, review queue, mappings,
            scoring config, runs, audit log
                                 ▲
                       Admin UI (React + Mantine) via Admin JSON API
```

## 4. Ontology layers (ADR-0006)

| Layer | Contents | Gate |
|---|---|---|
| Core | Issuer, Filing, Rule, Obligation, Finding; identity keys; provenance | Versioned migrations, tested |
| Extension | New classes/relations proposed by LLM or admin; own namespace; deprecate, never delete | Human approval |
| Instances | Entities and relations with evidence, timestamps, confidence | Automatic after checks |

Publishing an ontology version is atomic, generates the matching Neo4j uniqueness constraints
and indexes, and can be rolled back.

## 5. Scoring (ADR-0004)

**Hard gates** (any failure blocks the write regardless of score):
- *Schema gate*: the class or relation exists in the published ontology; domain and range valid.
- *Constraint gate*: cardinality and disjointness hold; no contradiction with accepted facts.
  A contradiction becomes a **conflict** in the review queue and is never auto-overwritten.

**Weighted score** in [0, 1]:

| Component | Meaning | Default weight |
|---|---|---|
| Evidence grounding | Quoted span really appears in the source (fuzzy match) | 0.25 |
| Back-translation agreement | LLM restates the fact; judge compares with source | 0.20 |
| Stability | Share of N extraction runs that produced the fact | 0.15 |
| Source reliability | Superset/MySQL 0.95, Excel 0.85, PDF 0.75 | 0.20 |
| Entity match confidence | Similarity to an existing node (new entity = 0.5) | 0.10 |
| Extractor self-confidence | LLM's own confidence (deliberately low weight) | 0.10 |

**Routing**: score ≥ 0.85 → commit; 0.60–0.85 → review queue; < 0.60 → reject (kept with the
reason). Weights, thresholds and N are editable in the admin page; weights must sum to 1.

## 6. Containing LLM non-determinism (ADR-0005)

An LLM is not bit-for-bit repeatable, even at minimum randomness. So:
1. **Reduce**: pinned model, versioned prompts, structured JSON output, and a vocabulary
   restricted to the published ontology.
2. **Make repeatable**: canonicalize outputs; cache by `(input hash, prompt version, model,
   ontology version)`; idempotent `MERGE` writes.
3. **Measure**: self-consistency with `WRITER_SELF_CONSISTENCY_RUNS` (default 3). A fact is kept only if
   a majority of runs produce it, and stability feeds the score.
4. **Verify over time**: a golden dataset plus `writer-eval` reporting precision, recall and
   run-to-run agreement. CI replays recorded responses.

## 7. Sources

- **Documents (ADR-0007)**: PDF with page anchors, Excel with sheet/row anchors; chunked; the
  extraction prompt is constrained to the ontology vocabulary.
- **Superset (ADR-0008)**: the Superset REST API resolves a dataset to a table or SQL; rows are
  read in pages from MySQL with a read-only user; a saved **column mapping** (LLM-suggested,
  admin-approved) turns rows into facts deterministically.

## 8. Neo4j model (ADR-0010)

Only **accepted** facts. Every node and relationship carries `ontology_version`, `score`,
`run_id`, `ingested_at`, and optional `valid_from`/`valid_to`. Provenance:
`(fact)-[:SUPPORTED_BY]->(:Evidence {span, page|row})-[:FROM]->(:Source)` and
`(:IngestionRun)`. Each run is written in one transaction with `MERGE` on identity keys; a run
can be rolled back.

## 9. Admin (ADR-0009, ADR-0011)

React + TypeScript + Mantine on Bun, with a React Flow graph and a typed client generated from
OpenAPI, served by FastAPI at `/admin`. Screens: Ontology, Extension proposals, Review queue
(including conflicts), Mappings, Scoring config, Runs and audit. Roles: `admin` and `reviewer`.
Ingestion uses an `X-API-Key`.

## 10. Stack (ADR-0002)

Python 3.12, FastAPI, pydantic v2, SQLAlchemy 2 + Alembic, `neo4j` driver, `anthropic` SDK;
`uv`, pytest, ruff, mypy. Admin UI: Bun, React, Mantine. Local infra: docker compose (Neo4j,
MySQL, mock Superset, optional real Superset).

## 11. Delivery (ADR-0001, ADR-0012)

Issue-driven: one GitHub issue → branch `engineering/<#>-<slug>` → PR (`Closes #N`) →
independent agent review posted as a PR comment → fixes → clean re-review (max 3 rounds) → CI
green → squash-merge. See [`CONTRIBUTING.md`](../CONTRIBUTING.md).

## 12. Left to the builder

Exact prompt wording, fuzzy-match algorithm and thresholds, chunk sizes, demo data contents,
and UI layout details.
