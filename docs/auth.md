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

## Passwords

`scrypt` (stdlib, memory-hard) with a random salt per password; at least 12 characters. Login
errors are identical for "unknown user" and "wrong password", and both cost one hash
computation, so responses don't reveal which usernames exist. Every login and failed login is
audited (`auth.login`, `auth.login_failed`).

## CSRF

The session is a cookie, so a malicious site could try to make your browser send a request.
Two layers stop that:
1. `SameSite=Lax`: browsers don't attach the cookie to cross-site POSTs.
2. Every state-changing request (and login/logout) must send
   `X-Requested-With: writer-admin`. Other sites can't add custom headers to cross-site
   requests without CORS approval, which the service never grants. The admin UI sends it
   automatically.

## Setup

```sh
# .env
WRITER_INGEST_API_KEY=<long random string>
WRITER_ADMIN_JWT_SECRET=<at least 32 random characters>   # unset in dev = random per process
WRITER_COOKIE_SECURE=false                               # true behind HTTPS

uv run writer-users create alice --role admin      # prompts for the password twice
```

In production (`WRITER_APP_ENV=prod`) the service refuses to start without a 32+ character
JWT secret and with `WRITER_COOKIE_SECURE=false`.

## Endpoints

| Method | Path | Notes |
|---|---|---|
| POST | `/api/auth/login` | `{username, password}` → sets the cookie, returns `{username, role}` |
| POST | `/api/auth/logout` | clears the cookie |
| GET | `/api/auth/me` | current user, or 401 |

Out of scope for now: SSO (Azure AD/Keycloak). It would replace `/api/auth/login` and
`current_user`; roles and audit stay the same.
