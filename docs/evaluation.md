# Evaluation: accuracy against a golden set

How we know the extractor is getting better or worse when a prompt, model or ontology
changes. Code: `src/writer_service/evaluation/`. Design: [ADR-0005](adr/0005-containing-llm-nondeterminism.md).

```sh
uv run writer-eval                          # replay scripted recordings (offline)
uv run writer-eval --runs 3 --min-f1 0.95   # fail (exit 1) below a threshold
uv run writer-eval --mode live              # call the real LLM; records to evaluation/recordings-live
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
| attribute accuracy | for matched entities, share of golden properties reproduced (`n/a` if no labelled entity was matched) |
| run-to-run agreement | mean pairwise Jaccard of the fact sets of the N runs (1.0 = identical) |

Facts are matched on **identity** (class + key, or relation + endpoint classes + endpoint
keys), ignoring case and whitespace in keys. A golden relation's endpoint classes default to the
relation's domain and range; set `source_class`/`target_class` to override. Matching on
identity means wording differences in other properties don't count as errors; attribute
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

There is no API key in CI, so `evaluation/recordings/` holds **scripted stand-in answers**
(`writer_service/evaluation/scripted.py`), not real Claude output. They are written in the
same cache format a live run produces, keyed under the default model, and designed to behave
like an imperfect extractor. **The numbers above show how the evaluation works, not how
accurate Claude is on this document.**

What that means for CI:

- The step *"Evaluation harness self-test (scripted recordings)"* only protects the scoring,
  voting and threshold code. It **cannot catch a prompt or model regression**: after a prompt
  or ontology change the scripted answers are regenerated
  (`uv run python -m writer_service.evaluation.scripted`; a test fails until you do), which
  gives the same scores by construction.
- Real protection needs **live recordings**:
  1. set `WRITER_ANTHROPIC_API_KEY` and run `uv run writer-eval --mode live --runs 3`.
     Live mode writes to `evaluation/recordings-live/` and refuses a folder that already has
     entries (the cache is read first, so old answers would be reported as a live run);
     delete the folder to re-record.
  2. review the report and commit `evaluation/recordings-live/`.
  3. CI then also runs *"Accuracy (golden set, committed live recordings)"*, which replays
     them with `--min-f1 0.95`. After a prompt, ontology or model change the replay misses and
     the step fails, so the change can't merge until someone records and reviews a new live
     run.

The scripted recordings are keyed under the default `llm_model`. With `WRITER_LLM_MODEL` set
to another model, replay misses and fails loudly rather than scoring stale answers.
