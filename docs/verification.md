# Verification signals

Three checks that turn an LLM's claim into numbers the [scoring engine](scoring.md) can weigh.
Code: `src/writer_service/pipeline/verification.py`. Design: [ADR-0005](adr/0005-containing-llm-nondeterminism.md).

| Signal | Question | Feeds score component |
|---|---|---|
| Evidence grounding | Is the quoted span really in the source? | `grounding` |
| Back-translation | Does an independent judge agree the quote says this? | `back_translation` |
| Self-consistency | Do repeated extraction runs agree? | `stability` |

## Evidence grounding

`grounding_score(quote, source_text)`: *it can miss, never invent*.

1. **Normalise both sides**: Unicode NFKC (ligatures such as "ﬁ"), typographic dashes, quotes
   and apostrophes to ASCII, soft hyphens removed, whitespace collapsed, lower case.
2. **Verbatim → 1.0.** The quote must appear on word *and number* boundaries (`HW-25-010` is not
   inside `HW-25-0101`, `$1,200` not inside `$1,200,000`, `2025` not inside `2025/26`) and must not
   drop a negation in the two words right before it (*"did not | comply with …"* → 0.1).
   A negation further back in the same sentence (*"did not, in all respects, comply …"*)
   caps it at 0.5. Text after the quote, or in other sentences, is context. If the quote
   occurs several times, the best occurrence counts. Empty or punctuation-only quotes score 0.
3. **Anything else is a near-quote, capped at 0.5**, however similar. String similarity can't
   tell a typo from a one-word change of meaning ("Class A"/"Class B", "increased"/"decreased",
   "approved"/"rejected"), so a near-quote is never treated as fully grounded. With weight 0.25
   it costs at least 0.125 of the overall score. On its own that isn't always enough (a very
   reliable source can still reach 0.85), so the **writer pipeline also routes any fact whose
   grounding is below 1.0 to review at most** (issue #10).
   Within the cap the score is the fuzzy `partial_ratio` rescaled above a noise floor of 0.5.
4. **Altered → at most 0.1** when the best-matching passage lacks a critical token of the quote
   (digits, capitalised names with abbreviations allowed, negations including `didn’t`) or
   negates what the quote states. A year copied from elsewhere on the page doesn't count.
5. **0** when there is no source text for that location.

`DictSourceText` maps `(source_ref, locator)` (for example `("report.pdf", "page 2")`) to the
text of that page or row; a candidate's grounding is its **best** evidence.

| Quote vs. source | Score |
|---|---|
| verbatim (after normalisation) | 1.0 |
| typo, abbreviation, or one changed lower-case word | ≤ 0.5 → review |
| a year, amount, rule number, name or negation changed; elided quote | ≤ 0.1 |
| invented sentence | ~0 |

This is the main defence against an LLM "quoting" text that isn't there.

## Back-translation

The fact is rendered in plain language (`describe()`, e.g.
*"HardwareAsset 'HW-25-0101' ASSIGNED_TO Department 'FIN'"*) and sent with its quotes to an LLM judge
(prompt `back_translation` v1) that answers `{agrees, score, explanation}` as validated JSON.
The judge may only use the quotes, never outside knowledge. If it disagrees, the score is
capped at 0.3. No evidence means a score of 0, and the LLM isn't called. Answers are cached per
fact (see [LLM layer](llm.md)).

## Self-consistency

`self_consistency(extract, n)` calls the extractor with `sample = 0 … n-1`
(`WRITER_SELF_CONSISTENCY_RUNS`, default 3):

1. group candidates by `fact_hash` (ignores whitespace, case, property order, evidence);
2. a fact repeated inside one run counts **once**;
3. keep facts produced by a **strict majority** (2 of 3, 1 of 1, 2 of 2);
4. merge their evidence (deduplicated) and average their confidence;
5. `stability = votes / n`.

Facts that appear only once in three runs are exactly the unstable, possibly invented ones,
so they are dropped before scoring. Output order is first appearance (deterministic).

Each sample goes through `StructuredLLM.ask(..., sample=i)`, whose cache key includes the
sample index. The first ingestion makes N real calls; re-ingesting the same document replays the
same N answers and the same votes (tested end to end with `FakeLLM`).
