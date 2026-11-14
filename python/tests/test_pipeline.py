"""Normalization, dedupe, email, CAPTCHA, retry, and config tests."""

import json
from contextlib import asynccontextmanager

import httpx
import pytest
from paths import FIXTURES

from gmaps_service.captcha import ensure_not_blocked, looks_like_captcha
from gmaps_service.config import Config, playwright_proxy
from gmaps_service.dedupe import dedupe_businesses
from gmaps_service.emails import extract_emails_from_html, fetch_public_emails, find_contact_url
from gmaps_service.errors import CAPTCHA_MESSAGE, CaptchaDetectedError, ConfigError
from gmaps_service.logging_utils import EventLogger
from gmaps_service.models import Business, Review
from gmaps_service.normalize import normalize_phone, normalize_website
from gmaps_service.retry import RetryableHTTPError, async_retry
from gmaps_service.scraper import GoogleMapsScraper


def test_phone_and_website_normalization():
    assert normalize_phone("25 101555", "CY") == "+35725101555"
    assert normalize_phone("+1 (212) 343-9000", "US") == "+12123439000"
    assert normalize_website("https://www.google.com/url?q=https://bluebottlecoffee.com/&opi=1") == (
        "https://bluebottlecoffee.com/"
    )
    assert normalize_website("https://www.instagram.com/bluebottle") is None
    assert normalize_website("https://maps.google.com/maps?q=cafe") is None


def test_dedupe_by_place_id_and_coordinates():
    first = Business(name="Cafe", address="1 Main", place_id="p1", phone="+15555550100", latitude=40.7, longitude=-74.0)
    duplicate = Business(
        name="Cafe",
        address="",
        place_id="p1",
        emails=["a@cafe.test"],
        website="https://cafe.test",
        latitude=40.7,
        longitude=-74.0,
    )
    nearby = Business(name="Cafe", address="1 Main St", latitude=40.70001, longitude=-74.00001, reviews=[Review("hi", "Ann", 5, "today")])
    other = Business(name="Other", address="9 Side", place_id="p2", latitude=40.7, longitude=-74.0)
    merged = dedupe_businesses([first, duplicate, nearby, other])
    assert [item.place_id for item in merged] == ["p1", "p2"]
    assert merged[0].emails == ["a@cafe.test"]
    assert merged[0].website == "https://cafe.test"
    assert merged[0].reviews[0].author == "Ann"


def test_emails_from_homepage_and_contact_fixtures():
    home = (FIXTURES / "homepage.html").read_text(encoding="utf-8")
    contact = (FIXTURES / "contact.html").read_text(encoding="utf-8")
    assert extract_emails_from_html(home) == ["hello@cafe.test"]
    assert find_contact_url(home, "https://cafe.test/") == "https://cafe.test/contact"
    assert "info@cafe.test" in extract_emails_from_html(contact)


@pytest.mark.asyncio
async def test_fetch_public_emails_stops_after_contact_page():
    home = (FIXTURES / "homepage.html").read_text(encoding="utf-8")
    contact = (FIXTURES / "contact.html").read_text(encoding="utf-8")
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/contact":
            return httpx.Response(200, text=contact)
        return httpx.Response(200, text=home)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    emails = await fetch_public_emails("https://cafe.test/", client=client)
    await client.aclose()
    assert emails == ["hello@cafe.test", "info@cafe.test"]
    assert seen == ["/", "/contact"]


def test_captcha_detector_ignores_ordinary_pages():
    html = (FIXTURES / "unusual_traffic.html").read_text(encoding="utf-8")
    assert looks_like_captcha("https://www.google.com/sorry/index", html, "Sorry")
    assert looks_like_captcha("https://maps.google.com/maps", html, "")
    assert not looks_like_captcha(
        "https://www.google.com/maps/place/Cafe",
        "<div>A review that mentions unusual traffic as a phrase in passing is not a block page.</div>",
        "Cafe",
    )


@pytest.mark.asyncio
async def test_headless_captcha_stops_without_solving():
    class Page:
        url = "https://www.google.com/sorry/index"

        async def content(self):
            return (FIXTURES / "unusual_traffic.html").read_text(encoding="utf-8")

        async def title(self):
            return "Sorry"

    config = Config(headless=True, delay_min=0, delay_max=0, google_places_api_key="x")
    with pytest.raises(CaptchaDetectedError, match="Automated CAPTCHA solving is not supported"):
        await ensure_not_blocked(Page(), config, EventLogger(stream=_Null()))
    assert "unusual-traffic" in CAPTCHA_MESSAGE


class _Null:
    def write(self, _text):
        return None

    def flush(self):
        return None


@pytest.mark.asyncio
async def test_retry_backs_off_then_succeeds(monkeypatch):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("gmaps_service.retry.asyncio.sleep", fake_sleep)
    attempts = {"n": 0}

    async def operation():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RetryableHTTPError(429, "https://maps.example/search", retry_after=0)
        return "ok"

    result = await async_retry(operation, retries=2, base_delay=0, logger=EventLogger(stream=_Null()), url="https://maps.example/search")
    assert result == "ok"
    assert attempts["n"] == 2
    assert sleeps == []


def test_config_proxy_and_backend(tmp_path):
    proxy_file = tmp_path / "proxies.txt"
    proxy_file.write_text("# comment\n\nsocks5://proxy.example:1080\n", encoding="utf-8")
    config = Config.load(
        proxies_file=str(proxy_file),
        google_places_api_key="test-key",
        backend="auto",
        delay_min=0,
        delay_max=0,
    )
    assert config.proxies == ["socks5://proxy.example:1080"]
    assert config.resolve_backend() == "places"
    assert playwright_proxy("http://user:p%40ss@proxy.example:8080") == {
        "server": "http://proxy.example:8080",
        "username": "user",
        "password": "p@ss",
    }
    with pytest.raises(ConfigError):
        Config(backend="places", google_places_api_key="").resolve_backend()
    with pytest.raises(ConfigError):
        playwright_proxy("ftp://proxy.example:21")


@pytest.mark.asyncio
async def test_scraper_run_dedupes_and_saves(tmp_path):
    class Engine:
        def __init__(self):
            self.extracted = []

        async def search(self, query, location, max_results):
            return [
                Business(name="Cafe", address="1 Main", place_id="p1", enriched=True, website=None),
                Business(name="Cafe", address="1 Main", place_id="p1", emails=["a@cafe.test"], enriched=True),
                Business(name="", maps_url="https://maps.example/place", place_id="p2"),
            ][:max_results]

        async def extract_business_data(self, url):
            self.extracted.append(url)
            return Business(name="Other", address="2 Main", place_id="p2", maps_url=url, enriched=True)

        async def scrape_reviews(self, url):
            return []

    engine = Engine()

    @asynccontextmanager
    async def factory(_backend):
        yield engine

    config = Config(
        backend="playwright",
        delay_min=0,
        delay_max=0,
        extract_emails=True,
        output_dir=str(tmp_path),
        google_places_api_key="",
    )
    scraper = GoogleMapsScraper(config, logger=EventLogger(stream=_Null()), engine_factory=factory)
    result = await scraper.run("coffee shops", "New York", max_results=10, filename=tmp_path / "nyc")
    assert result.query == "coffee shops in New York"
    assert result.total_found == 2
    assert result.backend == "playwright"
    assert [item.name for item in result.businesses] == ["Cafe", "Other"]
    assert result.businesses[0].emails == ["a@cafe.test"]
    assert engine.extracted == ["https://maps.example/place"]
    saved = json.loads((tmp_path / "nyc.json").read_text(encoding="utf-8"))
    assert saved["total_found"] == 2
    assert saved["timestamp"]
    assert "Cafe" in (tmp_path / "nyc.csv").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_scraper_propagates_captcha():
    class Engine:
        async def search(self, query, location, max_results):
            raise CaptchaDetectedError(CAPTCHA_MESSAGE)

    @asynccontextmanager
    async def factory(_backend):
        yield Engine()

    config = Config(backend="playwright", delay_min=0, delay_max=0, google_places_api_key="")
    scraper = GoogleMapsScraper(config, logger=EventLogger(stream=_Null()), engine_factory=factory)
    with pytest.raises(CaptchaDetectedError):
        await scraper.run("coffee shops", "New York", max_results=1)
