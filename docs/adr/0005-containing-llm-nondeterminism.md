# ADR-0005: Contain LLM non-determinism

- Status: Accepted
- Date: 2026-11-12

## Context
LLM output is not reproducible run to run, even at minimum randomness.

## Decision
Pinned model, versioned prompts, structured output with an ontology-restricted vocabulary; canonicalized outputs; a cache keyed by input, prompt version, model and ontology version; N-run self-consistency (default 3, configurable) with majority vote and a stability score; a golden-set evaluation command.

## Consequences
Same input gives the same stored result, and drift is visible. Extraction costs about N times as many LLM calls.
