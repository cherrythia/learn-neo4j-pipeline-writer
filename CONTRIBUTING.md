# Contributing

This repository uses **issue-driven development**. The issue list is the project's record of
what was built and why.

## The loop (one issue at a time)

1. **Issue**: every change starts as a GitHub issue with scope, acceptance criteria and a test
   checklist.
2. **Branch**: `engineering/<issue#>-<short-slug>` from `main`.
3. **Tests first**: write failing tests for the acceptance criteria, then implement until green.
4. **Docs**: update `CHANGELOG.md` and the relevant `docs/` page or ADR in the same PR.
5. **PR**: title `<type>: <summary> (#N)`, body includes `Closes #N`.
6. **Independent review**: a fresh review agent, given only the issue and the diff, posts its
   findings as one PR comment titled `## Independent review — round N` (N = 1, 2, 3), each
   finding with severity (blocker/major/minor/nit), `file:line`, problem and fix.
7. **Fix and re-review**: fix every finding, push, and post a reply listing how each was
   addressed. Re-review until a round reports `No findings — review clean` (max 3 rounds,
   then escalate to a human).
8. **Merge**: when CI is green and the review is clean, squash-merge. The issue closes
   automatically.

## Local checks

```sh
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest

cd admin-ui && bun install && bun run typecheck && bun test && bun run build
```

## Rules
- CI must never call a real LLM. Use `FakeLLM` or recorded fixtures.
- Never commit `.env` or secrets. Add new settings to `.env.example`.
- Integration tests that need Neo4j or MySQL are marked `@pytest.mark.integration` and skip
  when the service is unreachable.
