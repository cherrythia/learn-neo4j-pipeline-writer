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
   findings as one PR comment titled `## Independent review — round N` (N = 1 … 5), each
   finding with severity (blocker/major/minor/nit), `file:line`, problem and fix.
7. **Fix and re-review**: fix every finding, push, and post a reply listing how each was
   addressed. Re-review until a round reports `No findings — review clean` (max **5** rounds,
   then escalate to a human). A conflict with `main` or missing CI on the head commit
   counts as a finding: merge `origin/main` and let CI run before the next round.
8. **Merge**: when CI is green and the review is clean, squash-merge. The issue closes
   automatically.

## Local checks

```sh
uv sync && (cd admin-ui && bun install)
./scripts/check.sh            # everything CI runs except the docker job; stops on first failure
./scripts/check.sh backend    # Python only
```

Don't pipe check commands into `tail`/`grep` without `set -o pipefail`: the pipe hides the
exit code.

Integration tests (need `docker compose up -d --wait`):

```sh
set -a; . ./.env; set +a
WRITER_TEST_MYSQL_URL="mysql+pymysql://writer:$MYSQL_WRITER_PASSWORD@127.0.0.1:3306/governance_test" \
WRITER_TEST_NEO4J_URI=bolt://127.0.0.1:7687 WRITER_TEST_NEO4J_PASSWORD="$WRITER_NEO4J_PASSWORD" \
  uv run pytest -m integration
```

## Keeping a PR branch current

Once a branch is pushed, bring in `main` with `git merge origin/main`, not rebase plus
force-push. PRs are squash-merged, so merge commits never reach `main`.

## Rules
- CI must never call a real LLM. Use `FakeLLM` or recorded fixtures.
- Never commit `.env` or secrets. Add new settings to `.env.example`.
- Integration tests that need Neo4j or MySQL are marked `@pytest.mark.integration` and skip
  when the service is unreachable.
