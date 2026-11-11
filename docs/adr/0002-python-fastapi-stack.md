# ADR-0002: Python + FastAPI backend

- Status: Accepted
- Date: 2026-11-12

## Context
The writer service needs PDF/Excel parsing, an LLM SDK, a Neo4j driver and an HTTP API.

## Decision
Python 3.12, FastAPI, pydantic v2, managed with `uv`; tests with pytest; ruff and mypy (strict) for quality.

## Consequences
Rich data and document ecosystem. A second toolchain (Bun) is needed for the admin UI.
