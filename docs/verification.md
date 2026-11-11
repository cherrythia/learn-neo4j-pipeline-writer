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

- **1.0** if the quote appears in the source text, ignoring whitespace and case;
- otherwise the best fuzzy *partial* match (`rapidfuzz.fuzz.partial_ratio`) **rescaled above
  a noise floor of 0.5** (raw 0.5 → 0, raw 1.0 → 1), capped at 0.99;
- **capped at 0.1** when a *critical token* of the quote is missing from the source: anything
  with a digit (years, amounts, `LR-705`), a capitalised word (names, months; abbreviations such
  as "Dec" for "December" are accepted), or a negation ("not", "never", …). Changing one of these
  changes the fact, however similar the wording;
- **0** when there is no source text for that location (nothing could be checked).

`DictSourceText` maps `(source_ref, locator)` (for example `("report.pdf", "page 2")`) to the
text of that page or row; a candidate's grounding is its **best** evidence.

| Quote vs. source | Score |
|---|---|
| verbatim (spacing/case differences) | 1.0 |
| typo-level difference ("anual", "31 Dec") | ~0.7 – 0.99 |
| half a clause missing | ~0.3 – 0.6 |
| a year, rule number, issuer or negation changed | ≤ 0.1 |
| invented sentence | ~0 |

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
