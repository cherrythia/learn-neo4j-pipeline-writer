# Verification signals

Three checks that turn an LLM's claim into numbers the [scoring engine](scoring.md) can weigh.
Code: `src/writer_service/pipeline/verification.py`. Design: [ADR-0005](adr/0005-containing-llm-nondeterminism.md).

| Signal | Question | Feeds score component |
|---|---|---|
| Evidence grounding | Is the quoted span really in the source? | `grounding` |
| Back-translation | Does an independent judge agree the quote says this? | `back_translation` |
| Self-consistency | Do repeated extraction runs agree? | `stability` |

## Evidence grounding

`grounding_score(quote, source_text)`:

1. **Verbatim → 1.0.** The quote appears in the source on word *and number* boundaries
   (so `LR-70` doesn't match inside `LR-705`, nor `$1,200` inside `$1,200,000`), ignoring
   whitespace and case. Words around it are context, not part of the quoted fact.
2. Otherwise find the best-matching passage with the fuzzy `partial_ratio` alignment and
   **score** the ratio rescaled above a noise floor of 0.5 (max 0.99).
3. **Cap at 0.1** when:
   - a *critical token* of the quote is missing from the matched passage (plus about one word
     of slack, so "Dec" can find "December"): anything with a digit (years, amounts,
     `LR-705`), capitalised words (names, months), negations; or
   - a negation inside the *exactly matched span* is absent from the quote (dropped "not").
   Typographic apostrophes are normalised, so `didn’t` counts as a negation. Checking the
   passage rather than the whole page means a year copied from elsewhere on the page doesn't
   count. Quotes must be **contiguous** source text; elided quotes are treated as unsupported.
4. **0** when there is no source text for that location.

`DictSourceText` maps `(source_ref, locator)` (for example `("report.pdf", "page 2")`) to the
text of that page or row; a candidate's grounding is its **best** evidence.

| Quote vs. source | Score |
|---|---|
| verbatim (spacing/case differences) | 1.0 |
| typo-level difference ("anual", "31 Dec") | ~0.7 – 0.99 |
| contiguous but loosely worded | ~0.3 – 0.9 |
| a year, amount, rule number, issuer or negation changed (even if it appears elsewhere on the page) | ≤ 0.1 |
| elided quote; invented sentence | ≤ 0.1 |

This is the main defence against an LLM "quoting" text that isn't there.

## Back-translation

The fact is rendered in plain language (`describe()`, e.g.
*"Filing 'AR-2025-APH' FILED_BY Issuer 'APH'"*) and sent with its quotes to an LLM judge
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
