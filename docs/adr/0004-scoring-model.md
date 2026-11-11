# ADR-0004: Hard gates plus weighted scoring with routing

- Status: Accepted
- Date: 2026-11-12

## Context
We need an explainable decision on whether a candidate fact is written.

## Decision
Schema and constraint gates block invalid facts. Otherwise the score is a weighted sum of grounding (0.25), back-translation (0.20), stability (0.15), source reliability (0.20), entity match (0.10) and self-confidence (0.10). At least 0.85 commits, 0.60 to 0.85 goes to review, and below 0.60 is rejected. All values are configurable; the breakdown is stored.

## Consequences
Transparent and tunable. The weights are a starting heuristic and should be calibrated with the golden set.
