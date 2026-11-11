# ADR-0006: Separate relational governance store

- Status: Accepted
- Date: 2026-11-12

## Context
The ontology needs versions, approvals, rollback, a review queue and an audit log.

## Decision
Use a MySQL governance database via SQLAlchemy 2 + Alembic (SQLite in tests). Publishing a version generates the matching Neo4j constraints. Ontology layers are core (migrations), extension (human approval) and instances (automatic after checks).

## Consequences
Simple versioning and transactions. Two stores must be kept in sync at publish time.
