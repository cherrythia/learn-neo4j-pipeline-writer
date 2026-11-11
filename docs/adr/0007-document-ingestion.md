# ADR-0007: LLM extraction from PDF and Excel with anchors

- Status: Accepted
- Date: 2026-11-12

## Context
Unstructured documents need to become candidate facts with traceable evidence.

## Decision
Extract text with page numbers (PDF) and sheet/row references (Excel), chunk it, and prompt the LLM with the published ontology vocabulary only. Every fact must quote its evidence span.

## Consequences
Evidence can be verified. Facts the LLM cannot ground are reported as unknown, never invented.
