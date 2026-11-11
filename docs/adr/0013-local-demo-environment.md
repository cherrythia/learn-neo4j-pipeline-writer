# ADR-0013: Local demo environment with seeded data

- Status: Accepted
- Date: 2026-11-12

## Context
There is no real Superset data available yet.

## Decision
docker compose starts Neo4j, MySQL with a fictional compliance dataset, and a mock Superset API; a real Superset is available through `--profile superset`. Sample PDF and Excel files are provided.

## Consequences
Anyone can run the whole flow. Demo data is fictional and small.
