# ADR-0008: Superset datasets via saved column mappings

- Status: Accepted
- Date: 2026-11-12

## Context
Structured rows should not be re-interpreted by an LLM on every sync.

## Decision
The Superset REST API resolves a dataset to its MySQL table or SQL; rows are read in pages with a read-only user. An admin-approved column mapping (LLM-suggested first draft) turns rows into facts deterministically. Local development uses a mock Superset API; real Superset is an optional compose profile.

## Consequences
Fast, deterministic and cheap. Mappings must be maintained when schemas change.
