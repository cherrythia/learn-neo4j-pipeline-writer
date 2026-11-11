# Evaluation: accuracy against a golden set

How we know the extractor is getting better or worse when a prompt, model or ontology
changes. Code: `src/writer_service/evaluation/`. Design: [ADR-0005](adr/0005-containing-llm-nondeterminism.md).

```sh
uv run writer-eval                          # replay recordings (offline; what CI runs)
uv run writer-eval --runs 3 --min-f1 0.95   # fail (exit 1) below a threshold
uv run writer-eval --mode live              # call the real LLM and record its answers
uv run writer-eval --json report.json
```

## Golden sets

`evaluation/golden/*.json`: hand-labelled expected facts for a document:

```json
{"document": "samples/aurora_pacific_annual_report_2025.pdf",
 "entities":  [{"class_name": "Issuer", "key": "APH", "properties": {"board": "mainboard"}}],
 "relations": [{"relation": "FILED_BY", "source": "AR-2025-APH", "target": "APH"}]}
```

Only facts the text actually states are labelled. Findings are left out because the sample
gives them no identity key.

## Metrics

| Metric | Meaning |
|---|---|
| precision | of the facts extracted, how many are in the golden set (hallucinations lower it) |
| recall | of the golden facts, how many were extracted (misses lower it) |
| F1 | harmonic mean of the two |
| attribute accuracy | for matched entities, share of golden properties reproduced |
| run-to-run agreement | mean pairwise Jaccard of the fact sets of the N runs (1.0 = identical) |

Facts are matched on **identity** (class + key, or relation + endpoints), ignoring case and
whitespace, so wording differences in other properties don't count as errors; attribute
accuracy measures those separately.

The report shows every run and the **voted** result (self-consistency, the same majority vote
the pipeline uses):

```
aurora_annual_report_2025.json  (runs: 3, run-to-run agreement: 0.85)
             precision  recall     f1   attr  tp fp fn
  run 0           0.93    1.00   0.96   1.00  13  1  0
  run 1           1.00    0.85   0.92   1.00  11  0  2
  run 2           1.00    1.00   1.00   1.00  13  0  0
  voted           1.00    1.00   1.00   1.00  13  0  0
```

Run 0 invented a rule and run 1 missed one; the 2-of-3 vote removes both errors.

## Recordings: read this

There is no API key in CI, so `evaluation/recordings/` currently holds **scripted stand-in
answers** (`writer_service/evaluation/scripted.py`), not real Claude output. They are written in
the same cache format a live run produces and keyed under the configured model, and they are
designed to behave like an imperfect extractor so the pipeline and metrics are exercised end
to end. **The numbers above therefore show how the evaluation works, not how accurate Claude
is on this document.**

To measure the real model: set `WRITER_ANTHROPIC_API_KEY`, run
`uv run writer-eval --mode live --recordings evaluation/recordings-live`, review the report,
and commit the recordings if you want CI to replay them. A test fails if the scripted
recordings no longer match the current prompt, ontology or golden set; regenerate them with
`uv run python -m writer_service.evaluation.scripted`.
