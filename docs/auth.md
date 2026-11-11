# Authentication and roles

[ADR-0011](adr/0011-auth.md). Code: `src/writer_service/auth/`.

## Two kinds of caller

| Caller | How it authenticates | Used for |
|---|---|---|
| Ingestion clients (upload scripts, Superset sync jobs) | `X-API-Key: <WRITER_INGEST_API_KEY>` | `/ingest/*` routes |
| People using the admin UI | Login → httpOnly session cookie | `/api/*` admin routes |

- **API key**: compared in constant time. If `WRITER_INGEST_API_KEY` is unset, ingestion is
  **refused** (503), never left open.
- **Session**: a JWT (HS256, `WRITER_SESSION_TTL_MINUTES`, default 8h) in the
  `writer_session` cookie: `HttpOnly`, `SameSite=Lax`, and `Secure` when
  `WRITER_COOKIE_SECURE=true`. Each request re-checks the user in the database, so
  deactivating a user takes effect immediately.

## Roles

| Role | Can |
|---|---|
| `reviewer` | Work the review queue (approve/edit/reject candidates and conflicts) |
| `admin` | Everything a reviewer can, plus ontology publish/rollback, extension approval, mappings, scoring policy, users |

Routes declare what they need with `Depends(require_role("admin"))`. Every decision records
the user in the audit log.

## Sessions

Each user has a `session_version` (migration `0003`) embedded in their token. **Logout bumps
it**, which revokes every session of that user, including copied tokens and other browsers.
Deactivating a user or changing their role takes effect on the next request, because both
are read from the database rather than the token.

## Login throttling

| Budget | Default | Purpose |
|---|---|---|
| username + client | 5 failures / 15 min | guessing one account |
| client | 20 attempts / 15 min | password spraying across usernames |

Further attempts get **429**. Budgets are per username+client, so an attacker elsewhere can't
lock the real admin out. Each attempt is reserved *before* the password check (under a lock),
so parallel requests can't exceed the budget. A successful login releases its budget.
Usernames are compared ignoring case and accents, like MySQL's default collation, so `ädmin`
shares `admin`'s budget. Usernames are limited to 100 characters, and memory is bounded
(at most 10,000 keys, empty ones pruned).

The limiter is in memory and per process, which suits a single-instance admin service.
Behind several instances, use a shared store such as Redis. **Behind a reverse proxy**, run
uvicorn with `--proxy-headers --forwarded-allow-ips=<proxy address>`; otherwise every user
appears to come from the proxy and shares one client budget.

## Passwords

`scrypt` (stdlib, memory-hard) with a random salt per password; at least 12 characters. Login
errors are identical for "unknown user" and "wrong password", and both cost one hash
computation, so responses don't reveal which usernames exist. Every login, failed login and
logout is audited. Failed logins are recorded with actor `anonymous`; the attempted username
goes in the detail, never in the actor column.

## CSRF

The session is a cookie, so a malicious site could try to make your browser send a request.
Two layers stop that:
1. `SameSite=Lax`: browsers don't attach the cookie to cross-site POSTs.
2. Every state-changing request that uses the session (and login itself) must send
   `X-Requested-With: writer-admin`; this is enforced in `current_user`, so no route can
   forget it. Other sites can't add custom headers to cross-site requests without CORS
   approval, which the service never grants. The admin UI (issue #15) sets it on every request.

## Setup

```sh
# .env
WRITER_INGEST_API_KEY=<long random string>
WRITER_ADMIN_JWT_SECRET=<at least 32 random characters>   # unset in dev = random per process
WRITER_COOKIE_SECURE=false                               # true behind HTTPS

uv run writer-users create alice --role admin      # prompts for the password twice
```

In production (`WRITER_APP_ENV=prod`) the service refuses to start without a 32+ character
JWT secret, with `WRITER_COOKIE_SECURE=false`, or with an ingest key shorter than 32
characters or equal to the `.env.example` value.

**Scope note:** this issue provides the building blocks. The real admin routes (ontology
publish, review queue, …) attach `require_role(...)` in issue #14, whose acceptance criteria
include a 401/403 test for every route and "a reviewer cannot publish".

## Endpoints

| Method | Path | Notes |
|---|---|---|
| POST | `/api/auth/login` | `{username, password}` → sets the cookie, returns `{username, role}` |
| POST | `/api/auth/logout` | ends all of the user's sessions and clears the cookie |
| GET | `/api/auth/me` | current user, or 401 |

Out of scope for now: SSO (Azure AD/Keycloak). It would replace `/api/auth/login` and
`current_user`; roles and audit stay the same.
