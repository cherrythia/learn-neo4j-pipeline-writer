# Admin UI

React + TypeScript + [Mantine](https://mantine.dev) on Bun (design §9, ADR-0009). Code:
`admin-ui/`. It talks only to the [admin API](admin-api.md) and is served by FastAPI at
**`/admin/`** once built.

| Library | Used for |
|---|---|
| `@mantine/core`, `@mantine/hooks`, `@mantine/notifications` | components, error notifications |
| `@tanstack/react-query` | server state (queries, mutations, invalidation) |
| `react-router` (`HashRouter`) | screens at `/admin/#/review`, `/admin/#/ontology`, … |
| `@xyflow/react` (React Flow) | ontology graph |
| `openapi-typescript` + `openapi-fetch` | typed API client generated from the OpenAPI schema |

**Hash routing** means FastAPI serves plain static files (`StaticFiles(html=True)`) with no
server-side fallback route: every screen is `/admin/#/<screen>`.

## Screens

The current user comes from `GET /api/auth/me`. The navigation is role-aware: a `reviewer`
doesn't see admin-only pages, and opening one directly shows a permission message.

| Screen | Role | What it does |
|---|---|---|
| Login | — | username/password; shows *invalid credentials* (401) and *login throttled* (429) |
| Review queue | reviewer | open items, filter by reason (score/conflict); detail with candidate, evidence quotes and locators, **score breakdown** (component, value, weight, contribution), reasons, matcher notes, conflict message; Approve, Edit then approve (property editor or JSON), Reject with note |
| Ontology | reviewer (read), admin (edit) | versions with status; classes (taxonomy parent, identity key, properties) and relations (domain → range, cardinality); **graph** (classes as nodes, relations as labelled edges, is-a edges dashed). Admin, on drafts: create draft, add class with properties, add relation, add property, deprecate/restore class or relation, publish (422 gate reasons listed), roll back (with confirm) |
| Proposals | reviewer (create), admin (decide) | list by status, propose a class/relation (JSON payload), approve/reject |
| Mappings | admin | list; LLM suggestion for a Superset dataset id; manual draft (JSON); approve (422 reasons listed) |
| Scoring | reviewer (read), admin (edit) | weights, thresholds, source reliability, N; validated in the browser (weights sum to 1 within 1e-6, review < commit, ranges) before the PUT |
| Runs | reviewer (read), admin (rollback) | runs with counts and status; detail with every candidate, decision, score and why; rollback with confirm |
| Audit | admin | newest first |
| Users | admin | list, create, change role, activate/deactivate (not yourself) |

## API client

`admin-ui/src/api/client.ts` wraps `openapi-fetch`. Every request:

- sends the session cookie (`credentials: "include"`),
- sends `X-Requested-With: writer-admin` (the service's CSRF check, see [auth](auth.md);
  login needs it too).

Errors are mapped in one place (a client middleware):

| Status | What the user sees |
|---|---|
| 401 | back to the Login screen (except a failed login, which the form explains) |
| 403 | "Permission denied" notification |
| 429 | "Login throttled" notification (and on the login form) |
| other | notification with the server's `detail`; screens also show 422 reasons inline |

The base URL is the page's origin, or `<meta name="writer-api-base" content="…">` if set.

### Regenerating the types

The types in `src/api/schema.d.ts` are generated from `src/api/openapi.json`, which is
exported from the FastAPI app:

```sh
uv run writer-openapi                 # backend -> admin-ui/src/api/openapi.json
cd admin-ui && bun run gen:api        # openapi.json -> src/api/schema.d.ts
```

Both files are committed. `tests/test_api/test_openapi.py` fails if `openapi.json` is stale,
and `src/api/schema.test.ts` fails if `schema.d.ts` is stale. (`openapi-typescript` needs the
TypeScript 5 compiler API, so the UI pins `typescript@^5.9`.)

## Running it

**Demo server** (no Docker, no API key): the real app with a temporary SQLite store, an
in-memory graph, a scripted `FakeLLM`, the mock Superset and a seeded review queue:

```sh
cd admin-ui && bun install && bun run build && cd ..
uv run writer-demo-server             # http://127.0.0.1:8000/admin/
```

It prints the passwords for `alice` (admin) and `rita` (reviewer); set
`WRITER_DEMO_ADMIN_PASSWORD` / `WRITER_DEMO_REVIEWER_PASSWORD` (12+ characters) to choose
them, and `WRITER_DEMO_PORT` / `WRITER_DEMO_HOST` to move it. Everything is discarded on exit:
the temporary directory (SQLite store with the demo password hashes) is deleted on shutdown.

**Against the real service**: `bun run build`, then start the service as usual; it serves
`admin-ui/dist` at `/admin/` (or `WRITER_ADMIN_UI_DIR`). Nothing is mounted if the UI hasn't
been built.

**Dev server with HMR**:

```sh
uv run writer-demo-server             # or the real service, on :8000
cd admin-ui && bun run dev            # http://127.0.0.1:3000
```

`server.ts` binds to loopback and forwards `/api/*` and `/ingest/*` to `WRITER_API_URL`
(default `http://localhost:8000`) with the path unchanged, so the cookie stays same-origin.

## Tests

```sh
cd admin-ui
bun test                              # component and unit tests (happy-dom + Testing Library)
bunx playwright install chromium      # once
bunx playwright test                  # browser smoke test (builds the UI, starts the demo server)
```

- `bun test` (root `src/`): client headers/credentials and 401/403/429 mapping, login error
  states, review breakdown and approve/edit/reject calls, scoring validation, ontology graph
  nodes/edges and publish 422 reasons, role-aware navigation, dev proxy paths, generated
  types up to date. `fetch` is scripted (`src/testing.tsx`); no server is needed.
- `e2e/smoke.spec.ts` (Playwright, Chromium): `rita` logs in, opens a review item, approves
  it; `alice` logs in, opens Ontology, creates a draft and publishes it.
- Backend: `tests/test_api/test_admin_ui_static.py` (the `/admin/` mount) and
  `tests/test_api/test_demo_server.py` (the demo factory).

CI runs `bun test`/typecheck/build in the `admin-ui` job and the Playwright test in `e2e`.
