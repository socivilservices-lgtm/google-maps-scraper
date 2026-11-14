# Security and quality scan

Date: 14 November 2026. Scope: the Python service in `python/` (the new multi-user app and the scraper it calls). The Go scraper was not modified in this pass.

## What ran

| Check | Command | Result |
| --- | --- | --- |
| Unit tests | `python3 -m pytest` in `python/` | 32 passed |
| Ruff | `python3 -m ruff check src tests` | Passed. Rules: E, F, I, B, S. Binding `0.0.0.0`, delay randomness, and long lines are ignored on purpose and noted below. |
| Bandit | `python3 -m bandit -r src -ll` | No medium or high findings after the bind-address note. |
| Dependency audit | `pip-audit` against the installed direct packages (FastAPI, Starlette, httpx, Jinja2, Stripe, SQLAlchemy, bcrypt, Playwright, Pydantic, itsdangerous, python-multipart, uvicorn, Beautiful Soup, phonenumbers, and their small runtime deps) with `--disable-pip --no-deps` | No known vulnerabilities. |

`pip-audit --local` against the whole machine also reported old system packages (ansible, an older urllib3, the distro pip). Those are not imported by this service and are not in `requirements.txt`. They were not "fixed" inside the app because they are not part of the app.

Jinja2 on the machine was 3.1.2, which has published fixes in 3.1.3 through 3.1.6. The requirement is now `jinja2>=3.1.6`.

## Findings and what changed

### Unauthenticated API

Previously, if `GMAPS_SERVICE_API_KEY` was empty, `POST /search` and `GET /jobs/{id}` were open. That is closed. A caller needs a per-user API key, the optional operator key, or a session cookie. `/health` stays public and does not return secrets.

### Job access (IDOR)

Jobs are stored with a `user_id`. `GET /jobs/{id}`, the download route, and `/app/jobs/{id}` return 404 unless the caller owns the job or is an admin (including the operator key). A test signs up two users and checks that the second cannot read the first user's job.

### Passwords and API keys

Passwords are hashed with bcrypt. The plaintext is not stored or logged. API keys are random `gms_` tokens. Only the SHA-256 hash and a short prefix are stored. The full key is shown once, on the account page. Revocation sets `revoked_at` and the hash no longer authenticates.

### Sessions

The session cookie is HMAC-signed (`itsdangerous`), HttpOnly, and `SameSite=Lax`. Set `GMAPS_COOKIE_SECURE=true` on HTTPS. Set `SECRET_KEY` to a long random value. If it is missing, the process generates one and logs a warning; sessions die on restart.

### CSRF

Browser forms send a token that must match the `gmaps_csrf` cookie. State-changing `/search` calls that rely on the session cookie also need `X-CSRF-Token`. Requests that send `X-API-Key` are not cookie-authenticated and skip CSRF. A test posts a login without the cookie and expects 403.

### Rate limits

Login is limited per client address (default 8 attempts / 15 minutes). Search is limited per user (default 30 / hour). Signup is 5 / hour / address. The counters live in memory, so they reset on deploy and are not shared across multiple instances. `X-Forwarded-For` is ignored unless `GMAPS_TRUST_PROXY=true`, so a client cannot pick their own rate-limit key. Turn that flag on behind DigitalOcean's proxy, where the socket address is the proxy.

### CORS and headers

There is no wildcard CORS middleware. Responses set `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`, and a Content-Security-Policy of `default-src 'self'`.

### Billing

Stripe webhooks are rejected with 400 when the signature does not match. If `STRIPE_WEBHOOK_SECRET` or `STRIPE_SECRET_KEY` is unset, the webhook returns 503 and says billing is not configured. The secret is not written to logs. Checkout and the customer portal are not called when the keys are missing.

### SQL

Queries use bound parameters. The user lookup SQL is a fixed string, not string-built from request data.

### Input

Emails must look like emails and be at most 200 characters. Passwords must be 10–200 characters. Search text is stripped and length-capped. `max_results` is bounded by the plan (default 100) for non-admin accounts, and by 200 on the API schema.

### CAPTCHA policy

Unchanged. A CAPTCHA or unusual-traffic page stops a headless job. Nothing in this tree sends the challenge to a solving service.

### Bind address

Bandit B104 flags `0.0.0.0`. The service listens on every interface so a container can receive traffic. The host firewall, or DigitalOcean's routing, is what exposes the port. The finding is marked `nosec` with that reason.

### Random delays

`random.uniform` is only used to space requests. Tokens and passwords use `secrets`.

## Left as accepted risk

- The rate limiter is per process.
- Job CSV/JSON files live on local disk. On App Platform they disappear when the container is replaced unless a volume is mounted at `/data`. The database still records the job. Put `DATABASE_URL` on Supabase (or another Postgres) so accounts survive.
- The operator key `GMAPS_SERVICE_API_KEY`, if set, can read every job. Leave it unset unless a server-side client needs it.
- Playwright against Google can be blocked by a CAPTCHA. That is a stop, not a bypass.
