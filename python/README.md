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

## Deploy

Build from this directory so the Go scraper's Dockerfile is left unchanged:

```bash
docker compose -f python/docker-compose.yml up --build
```

From the repository root the same stack is `docker compose -f docker-compose.python.yml up --build`.

The image is `mcr.microsoft.com/playwright/python` (override with `--build-arg PLAYWRIGHT_IMAGE=...`). Chromium is installed again during the build so it matches the Playwright package. Give the container about 1 GB of `/dev/shm` (`shm_size` in Compose). Job files and the browser profile are stored on the `maps-data` volume at `/data`.

Set `GMAPS_SERVICE_API_KEY` before publishing the port. Put `GOOGLE_PLACES_API_KEY` in the environment when you want the official API. The health check path is `/health`.

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
