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
{"document": "samples/zorblax_invoice_INV-2025-0418.pdf",
 "entities":  [{"class_name": "Department", "key": "FIN", "properties": {"name": "Finance"}}],
 "relations": [{"relation": "ASSIGNED_TO", "source": "HW-25-0101", "target": "FIN"},
               {"relation": "BILLED_ON", "source_class": "HardwareAsset",
                "source": "HW-25-0101", "target": "INV-2025-0418"}]}
```

Only facts the text actually states are labelled. The golden set
`evaluation/golden/zorblax_invoice_2025_0418.json` (issue #41) labels the sample vendor
invoice: 14 entities (the vendor, the invoice, 3 departments, 5 hardware assets, 2 software
licences, 2 software products) and 17 relations (`ISSUED_BY`, 7 × `BILLED_ON`,
5 × `ASSIGNED_TO`, 2 × `LICENSES`, 2 × `ALLOCATED_TO`). Hostnames, purchase dates and
publishers are not in the text, so they are not labelled. `BILLED_ON`'s domain is `Asset`,
so its golden relations name the subclass (`source_class`) the invoice line really is.

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
zorblax_invoice_2025_0418.json  (runs: 3, run-to-run agreement: 0.92)
             precision  recall     f1   attr  tp fp fn
  run 0           0.97    1.00   0.98   1.00  31  1  0
  run 1           1.00    0.90   0.95   1.00  28  0  3
  run 2           1.00    1.00   1.00   1.00  31  0  0
  voted           1.00    1.00   1.00   1.00  31  0  0
```

Run 0 invented a laptop (`HW-25-0104`) and run 1 missed one (`HW-25-0103` with its two
relations); the 2-of-3 vote removes both errors.

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
- Real protection needs **live recordings**. **None are committed at the moment**: the demo
  domain switched to ITAM in issue #41 (any answers recorded for the old annual-report sample
  are void), so the owner re-records against the invoice:
  1. set `WRITER_ANTHROPIC_API_KEY` and run `uv run writer-eval --mode live --runs 3`.
     Live mode writes to `evaluation/recordings-live/` and refuses a folder that already has
     entries (the cache is read first, so old answers would be reported as a live run);
     delete the folder to re-record.
  2. review the report and commit `evaluation/recordings-live/`.
  3. CI then also runs *"Accuracy (golden set, committed live recordings)"*, which replays
     them with `--min-f1 0.95` (replay only: CI never calls a real LLM). After a prompt,
     ontology or model change the replay misses and the step fails, so the change can't
     merge until someone records and reviews a new live run.

The scripted recordings are keyed under the default `llm_model`. With `WRITER_LLM_MODEL` set
to another model, replay misses and fails loudly rather than scoring stale answers.
