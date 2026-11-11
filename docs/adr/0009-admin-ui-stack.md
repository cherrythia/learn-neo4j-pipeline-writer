# ADR-0009: React + Mantine admin UI built with Bun

- Status: Accepted
- Date: 2026-11-12

## Context
Admins need to manage the ontology and review facts in the browser.

## Decision
React + TypeScript + Mantine, React Flow for the graph view, a typed client generated from OpenAPI; built and tested with Bun (`bun test`, React Testing Library, happy-dom); static build served by FastAPI at `/admin`.

## Consequences
A rich UI and a single deployable. Two languages in the repo.
