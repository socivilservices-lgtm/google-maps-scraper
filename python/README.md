# Python Maps business service

This package searches for local businesses and writes deduplicated JSON and CSV.
It sits next to the Go scraper in this repository and reuses the same Maps JSON
layout (see `gmaps/entry.go`) so the checked-in fixtures keep parsing.

Two backends:

- **places** — the official [Places API (New)](https://developers.google.com/maps/documentation/places/web-service/op-overview). This is the accurate, stable source. It is used automatically when `GOOGLE_PLACES_API_KEY` is set.
- **playwright** — an async Chromium session. It reads the JSON responses Maps itself loads, including `APP_INITIALIZATION_STATE`, and falls back to the place-panel DOM. Use it when you do not have an API key.

The browser backend does **not** call a CAPTCHA-solving service. A CAPTCHA or "unusual traffic" page is logged and, in headless or service mode, the job stops with a clear error. In headed mode the browser stays open so a person can solve it.

## What a record contains

`name`, full `address`, `phone` (E.164 when the number can be parsed), official `website` (social and Google links are dropped), `rating`, `review_count`, `latitude`, `longitude`, `categories`, `opening_hours` (weekday text, periods, and `open_now`), `price_level`, `claimed` when the listing shows an owner or a "Claim this business" link, up to five recent `reviews` (`text`, `author`, `rating`, `date`), and public `emails` from the business's own homepage and one contact page.

Saved JSON includes `timestamp`, `query`, `location`, `total_found`, and `backend`.

## Setup

```bash
cd python
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium   # only for the browser backend
```

Copy `.env.example` to `.env` if you want the CLI and the API to pick up keys automatically.

```bash
export GOOGLE_PLACES_API_KEY="your-key"   # optional; selects the places backend in auto mode
export GMAPS_SERVICE_API_KEY="change-me"  # required before exposing the HTTP port
```

## Usage

Search coffee shops in New York and write both files:

```bash
python -m gmaps_service search "coffee shops" "New York" --max-results 10 -o output/coffee-nyc
```

The same call as a library:

```python
import asyncio
from gmaps_service import GoogleMapsScraper
from gmaps_service.config import Config

async def main():
    scraper = GoogleMapsScraper(Config.load())
    result = await scraper.run("coffee shops", "New York", max_results=10)
    print(scraper.save_results(result, "output/coffee-nyc"))

asyncio.run(main())
```

Force a backend:

```bash
python -m gmaps_service search "coffee shops" "New York" --backend places --max-results 10
python -m gmaps_service search "coffee shops" "New York" --backend playwright --max-results 10
python -m gmaps_service search "coffee shops" "New York" --backend playwright --headed
```

`--headed` is how you clear a CAPTCHA by hand. Headless mode will not try to solve it.

Run the HTTP service:

```bash
python -m gmaps_service serve --host 0.0.0.0 --port 8000
```

```bash
curl -s http://127.0.0.1:8000/health

curl -s -X POST http://127.0.0.1:8000/search \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $GMAPS_SERVICE_API_KEY" \
  -d '{"query":"coffee shops","location":"New York","max_results":10,"backend":"auto"}'

curl -s http://127.0.0.1:8000/jobs/JOB_ID \
  -H "X-API-Key: $GMAPS_SERVICE_API_KEY"

curl -L "http://127.0.0.1:8000/jobs/JOB_ID/download?format=csv" \
  -H "X-API-Key: $GMAPS_SERVICE_API_KEY" -o coffee-nyc.csv
```

`POST /search` returns a job id immediately. Add `"wait": true` to block until the job finishes (or fails). `GET /jobs/{id}` returns the status and, when completed, the result. `GET /health` does not require the API key.

## Configuration

Environment variables override a JSON file passed with `--config` or `GMAPS_CONFIG`. CLI flags override both.

| Variable | Default | Purpose |
| --- | --- | --- |
| `GMAPS_BACKEND` | `auto` | `auto`, `playwright`, or `places` |
| `GOOGLE_PLACES_API_KEY` | empty | Places API (New) key. `auto` uses it when set |
| `GMAPS_SERVICE_API_KEY` | empty | `X-API-Key` for the HTTP API. Set this before exposing the port |
| `GMAPS_MAX_RESULTS` | `20` | Cap per search. The API accepts at most 200 |
| `GMAPS_HEADLESS` | `true` | Browser visibility. `false` allows a manual CAPTCHA solve |
| `GMAPS_DELAY_MIN` / `GMAPS_DELAY_MAX` | `2` / `5` | Randomized seconds between place visits and Places detail calls |
| `GMAPS_TIMEOUT` | `30` | Navigation and HTTP timeout, in seconds |
| `GMAPS_RETRIES` | `3` | Extra attempts after HTTP 429 or 5xx, with exponential backoff |
| `GMAPS_PROXIES_FILE` | empty | One `http://` or `socks5://` proxy URL per line |
| `GMAPS_PROXIES` | empty | Comma-separated proxy URLs |
| `GMAPS_USER_DATA_DIR` | empty | Persistent Chromium profile directory |
| `GMAPS_OUTPUT_DIR` | `output` | Default directory for CLI output and job files |
| `GMAPS_EXTRACT_EMAILS` | `true` | Fetch the official site homepage and one contact page |
| `GMAPS_REGION` | empty | Hint for phone parsing, such as `US` |
| `GMAPS_MANUAL_CAPTCHA_TIMEOUT` | `180` | Seconds to wait for a person in headed mode |
| `GMAPS_CHROMIUM_NO_SANDBOX` | `false` | Set `true` when Chromium runs as root in a container. The image does this already |

Proxy file example: `proxies.txt.example`. Blank lines and `#` comments are ignored. A missing proxies file does not stop the process.

## Tests

```bash
cd python
pip install -r requirements-dev.txt
pytest
```

Parser tests use `testdata/raw.json`, `gmaps/testdata/review_*.json`, and the fixtures in `python/tests/fixtures/`. They do not open a browser or call Google.

## Accounts, trials, and billing

The HTTP service is a small multi-user app. The CLI still runs a search on the machine without an account.

- Sign up and log in with email and a password. Passwords are stored as bcrypt hashes. The browser session is an HttpOnly cookie. Each user can create API keys; only the hash is kept.
- `ADMIN_EMAIL` is the owner. That account is free and is not subject to the monthly quota.
- An admin invite (`/admin`, or `/signup?invite=CODE`) starts a 14-day trial with the same search quota as the paid plan. After the trial, or with no invite, searching requires an active Stripe subscription.
- The paid plan is **$29/month**, **3,000 results**, **100 searches**, **100 results per search**. The comparison and the cost math are in [docs/PRICING.md](../docs/PRICING.md).
- Without `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, and `STRIPE_PRICE_ID`, Checkout and the webhook stay off and the account page says billing is not configured. Admin access and unexpired invite trials still work.
- Postgres comes from `DATABASE_URL` (a Supabase connection string is fine). If that variable is empty, the service uses SQLite at `$GMAPS_OUTPUT_DIR/app.db`. Migrations run on startup from `src/gmaps_service/migrations/`.

Pages: `/` (pricing), `/signup`, `/login`, `/dashboard`, `/app/jobs/{id}`, `/account`, `/admin`.

`POST /search` and `GET /jobs/{id}` accept `X-API-Key` (a user key or the optional operator key). They no longer run anonymously when the operator key is unset.

## Deploy

### DigitalOcean App Platform

1. Merge this service to `main`.
2. Create a Postgres database (Supabase is fine) and copy its URI.
3. In Stripe, create a $29/month recurring price and a webhook to `https://YOUR_HOST/billing/webhook` for `checkout.session.completed`, `customer.subscription.updated`, and `customer.subscription.deleted`.
4. From the repo root, create the app (or paste `.do/app.yaml` into the App Platform UI):

```bash
doctl apps create --spec .do/app.yaml
```

5. Set the SECRET values in the App Platform dashboard. The spec already points at `python/Dockerfile` on `main` with source directory `python`.
6. Open the app URL, sign up with the same address as `ADMIN_EMAIL`, then create an invite from `/admin`.

The instance size in the spec is `professional-s`. Increase it if Chromium is the backend and the process is killed. Job files live on the container disk at `/data` and are lost when the container is replaced; accounts live in Postgres. Mount a volume at `/data` if downloads must survive a restart.

### Compose on a VM

```bash
docker compose -f python/docker-compose.yml up --build
```

From the repository root the same stack is `docker compose -f docker-compose.python.yml up --build`.

## Environment variables the owner must set

| Variable | Required | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | Production | Postgres URI. Empty uses SQLite under the output dir. |
| `SECRET_KEY` | Production | Signs session cookies. |
| `ADMIN_EMAIL` | Yes, for the owner account | That email is the unlimited admin. |
| `APP_BASE_URL` | Production | Public `https://` origin for Stripe return URLs. |
| `STRIPE_SECRET_KEY` | To charge | Stripe secret key. Omit to disable billing. |
| `STRIPE_WEBHOOK_SECRET` | To charge | Webhook signing secret. |
| `STRIPE_PRICE_ID` | To charge | The $29/month price id. |
| `GOOGLE_PLACES_API_KEY` | Only for the Places backend | Official Places API (New). |
| `GMAPS_SERVICE_API_KEY` | No | Optional operator key that can read every job. |
| `GMAPS_COOKIE_SECURE` | Production | `true` so the session cookie is HTTPS-only. |
| `GMAPS_TRUST_PROXY` | Behind App Platform | `true` so login limits use `X-Forwarded-For`. |
| `GMAPS_HEADLESS` | Containers | `true` |
| `GMAPS_CHROMIUM_NO_SANDBOX` | Containers running as root | `true` |
| `GMAPS_BACKEND` | No | `auto`, `playwright`, or `places`. |
| `GMAPS_OUTPUT_DIR` | No | Default `output`. Use `/data` in the container. |

Accounts to create: a Postgres database, a Stripe account (when you want to charge), a DigitalOcean app (or another host), and optionally a Google Cloud project for the Places API. The security review is in [docs/SECURITY_SCAN.md](../docs/SECURITY_SCAN.md).

## Deploy (container image)

Build from this directory so the Go scraper's Dockerfile is left unchanged:

```bash
docker compose -f python/docker-compose.yml up --build
```

From the repository root the same stack is `docker compose -f docker-compose.python.yml up --build`.

The image is `mcr.microsoft.com/playwright/python` (override with `--build-arg PLAYWRIGHT_IMAGE=...`). Chromium is installed again during the build so it matches the Playwright package. Give the container about 1 GB of `/dev/shm` (`shm_size` in Compose). Job files and the browser profile are stored on the `maps-data` volume at `/data`.

Set `SECRET_KEY` and `ADMIN_EMAIL` before publishing the port. Put `GOOGLE_PLACES_API_KEY` in the environment when you want the official API. The health check path is `/health`.

**Render / Railway / Fly.io.** Point the service at `python/Dockerfile` with build context `python/`. These hosts are a good fit for `GMAPS_BACKEND=places`. The Playwright backend needs more RAM (2 GB is a practical floor) and still stops cleanly when Google shows a CAPTCHA, which is common from datacenter addresses. Mount a disk at `/data` if job downloads should survive a restart.

**DigitalOcean droplet (or any VM).** Install Docker, clone the repo, create `python/.env`, and run the Compose file above. Put Caddy or nginx in front of port 8000 for TLS. This is the straightforward place to run the Playwright backend. For a manual CAPTCHA, run the CLI on a machine with a display:

```bash
python -m gmaps_service search "coffee shops" "New York" --backend playwright --headed --max-results 10
```

Exit code `2` means the job stopped on a CAPTCHA. Exit code `1` is any other scrape error.

## Limits

Records are deduped by place id. Listings without a place id are deduped by rounded coordinates plus name. Different place ids stay separate even when the pins overlap.

`claimed` is only filled when the Maps page shows owner information or a claim link. The Places API does not expose that flag, so it stays null on the `places` backend.

The current Maps place panel sometimes includes only today's hours and leaves the review count and review text out of the first document. Those fields are still parsed when Maps includes them, which the saved fixtures cover. The Places API returns up to five reviews when the key's SKU allows that field.

Emails come only from the official website's homepage and a single same-host contact, about, or impressum link. Social profiles are not crawled.

Google can change the browser JSON without notice. The Places API backend is the one to run in production. Scraping Maps may conflict with Google's terms; the API is the supported interface.
