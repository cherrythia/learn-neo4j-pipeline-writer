# ADR-0012: Tests in every PR, no real LLM in CI

- Status: Accepted
- Date: 2026-11-12

## Context
Every change must be verifiable and CI must be deterministic and free.

## Decision
Tests are written first. Unit tests use FakeLLM and in-memory fakes; integration tests marked `integration` run against docker compose services and are skipped when they are unavailable; the UI uses bun test plus one Playwright smoke test.

## Consequences
Fast, reliable CI. Live-model accuracy is checked on demand with the evaluation command.
