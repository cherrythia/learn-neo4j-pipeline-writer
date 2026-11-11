# ADR-0010: Neo4j holds accepted facts only, with provenance

- Status: Accepted
- Date: 2026-11-12

## Context
Observations and accepted facts must be kept apart.

## Decision
Only committed or approved facts are written. Every write carries ontology version, score, run id and timestamps, plus Evidence, Source and IngestionRun nodes. Each run is one transaction using MERGE on identity keys. Conflicts go to review and are never auto-overwritten.

## Consequences
The graph stays trustworthy and runs can be rolled back. Candidates live in the governance DB.
