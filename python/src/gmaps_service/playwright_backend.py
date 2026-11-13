"""Async Playwright backend.

Place data is read from the JSON responses Maps loads (and from
``APP_INITIALIZATION_STATE``, the same document the Go scraper reads).
DOM parsing fills fields the JSON did not include. A CAPTCHA or
unusual-traffic page stops a headless job instead of being sent to a solver.
"""

from __future__ import annotations

import asyncio
import random
import re
from pathlib import Path
from urllib.parse import quote

from gmaps_service.captcha import ensure_not_blocked
from gmaps_service.config import Config, playwright_proxy
from gmaps_service.dedupe import dedupe_review_lists, fill_missing
from gmaps_service.errors import ScraperError
from gmaps_service.logging_utils import EventLogger, get_logger
from gmaps_service.models import Business, Review
from gmaps_service.normalize import finalize_business
from gmaps_service.parsers import (
    extract_place_id,
    parse_maps_payloads,
    parse_place_html,
    parse_reviews_html,
    parse_reviews_rpc,
)
from gmaps_service.places_api import compose_query
from gmaps_service.retry import RETRYABLE_STATUS, RetryableHTTPError, async_retry, retry_after_seconds

# Same document the Go place job evaluates. The string at those indexes is the
# XSSI-prefixed place JSON.
# The place document is still the XSSI-prefixed array the Go scraper reads, but
# Maps now nests it somewhere under APP_INITIALIZATION_STATE instead of a fixed
# index. Return the longest such string.
_APP_STATE_JS = """
() => {
  const state = window.APP_INITIALIZATION_STATE;
  if (!state) return null;
  const stack = [state];
  let seen = 0;
  let best = null;
  while (stack.length && seen < 8000) {
    const current = stack.pop();
    seen++;
    if (typeof current === 'string' && current.startsWith(")]}'") && current.length > 500) {
      if (!best || current.length > best.length) best = current;
    }
    if (Array.isArray(current)) {
      const limit = Math.min(current.length, 80);
      for (let index = limit - 1; index >= 0; index--) stack.push(current[index]);
    } else if (current && typeof current === 'object') {
      const keys = Object.keys(current);
      const limit = Math.min(keys.length, 40);
      for (let index = limit - 1; index >= 0; index--) stack.push(current[keys[index]]);
    }
  }
  return best;
}
"""

_MAX_CAPTURED = 30
_MAX_BODY = 2_000_000


class PlaywrightBackend:
    """One browser context per scrape session."""

    def __init__(self, config: Config, logger: EventLogger | None = None) -> None:
        self.config = config
        self.logger = logger or get_logger()
        self.captured: list[tuple[str, str]] = []
        self._playwright: object | None = None
        self._browser: object | None = None
        self._context: object | None = None
        self._page: object | None = None
        self._proxy_index = 0

    async def start(self) -> None:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise ScraperError(
                "Playwright is not installed. Install dependencies and run "
                "`python -m playwright install chromium`."
            ) from exc
        self._playwright = await async_playwright().start()
        chromium_args = ["--disable-dev-shm-usage"]
        # Containers that run Chromium as root cannot use the sandbox.
        # This is not a CAPTCHA bypass; Chromium refuses to start without it.
        if self.config.chromium_no_sandbox:
            chromium_args.append("--no-sandbox")
        launch_kwargs: dict[str, object] = {
            "headless": self.config.headless,
            "args": chromium_args,
        }
        proxy_url = self._next_proxy()
        if proxy_url:
            launch_kwargs["proxy"] = playwright_proxy(proxy_url)
            self.logger.info("using proxy", proxy=_proxy_label(proxy_url))
        locale = "en-US" if self.config.language.lower().startswith("en") else self.config.language
        if self.config.user_data_dir:
            Path(self.config.user_data_dir).mkdir(parents=True, exist_ok=True)
            self._context = await self._playwright.chromium.launch_persistent_context(
                self.config.user_data_dir,
                viewport={"width": 1366, "height": 900},
                locale=locale,
                **launch_kwargs,
            )
        else:
            self._browser = await self._playwright.chromium.launch(**launch_kwargs)
            self._context = await self._browser.new_context(
                viewport={"width": 1366, "height": 900},
                locale=locale,
            )
        self._page = await self._context.new_page()
        self._page.set_default_timeout(self.config.timeout_seconds * 1000)
        self._page.on("response", self._on_response)

    async def stop(self) -> None:
        for closer in (self._context, self._browser):
            if closer is None:
                continue
            try:
                await closer.close()
            except Exception as exc:  # pragma: no cover - shutdown noise
                self.logger.warning("browser close failed", detail=str(exc))
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception as exc:  # pragma: no cover - shutdown noise
                self.logger.warning("playwright stop failed", detail=str(exc))
        self._context = None
        self._browser = None
        self._playwright = None
        self._page = None

    async def search(self, query: str, location: str, max_results: int) -> list[Business]:
        self._require_page()
        url = maps_search_url(query, location, self.config.language)
        self.captured.clear()
        await self._goto(url)
        if "/maps/place/" in (self._page.url or "") and "/maps/search/" not in (self._page.url or ""):
            business = await self._business_from_open_page()
            return [business] if business.name else []
        try:
            links = await self._collect_links(max_results)
        except Exception as exc:
            parsed = _mark_enriched(self._businesses_from_capture())
            if parsed:
                self.logger.info("parsed search from network responses", url=url, count=len(parsed))
                return parsed[:max_results]
            self.logger.error("results feed was not found", url=self._page.url, error_type="navigation", detail=str(exc))
            raise ScraperError(f"Google Maps did not show a results feed for {url}") from exc
        prefilled = {item.place_id: item for item in self._businesses_from_capture() if item.place_id}
        businesses: list[Business] = []
        for link in links:
            place_id = extract_place_id(link)
            known = prefilled.get(place_id) if place_id else None
            if known is not None:
                known.maps_url = known.maps_url or link
                if not known.place_id:
                    known.place_id = place_id
                businesses.append(known)
                continue
            businesses.append(Business(name="", maps_url=link, place_id=place_id))
        self.logger.info("collected place links", url=url, count=len(businesses))
        return businesses

    async def extract_business_data(self, business_url: str) -> Business:
        self._require_page()
        url = business_url
        if not business_url.startswith("http"):
            url = (
                "https://www.google.com/maps/search/?api=1"
                f"&query_place_id={quote(business_url)}&hl={quote(self.config.language)}"
            )
        self.captured.clear()
        await self._goto(url)
        business = await self._business_from_open_page()
        if len(business.reviews) < min(3, self.config.max_reviews):
            extra = await self._reviews_from_open_page()
            business.reviews = dedupe_review_lists(business.reviews, extra)
        business.enriched = True
        return finalize_business(
            business,
            default_region=self.config.region_hint,
            max_reviews=self.config.max_reviews,
        )

    async def scrape_reviews(self, business_url: str) -> list[Review]:
        business = await self.extract_business_data(business_url)
        return business.reviews

    async def _business_from_open_page(self) -> Business:
        assert self._page is not None
        await self._wait_for_place_panel()
        parsed: Business | None = None
        try:
            raw = await self._page.evaluate(_APP_STATE_JS)
        except Exception as exc:
            self.logger.warning("APP_INITIALIZATION_STATE was not readable", url=self._page.url, detail=str(exc))
            raw = None
        if isinstance(raw, str) and raw:
            found = parse_maps_payloads(raw)
            parsed = found[0] if found else None
        if parsed is None:
            for item in self._businesses_from_capture():
                if item.name:
                    parsed = item
                    break
        html = await self._page.content()
        dom = parse_place_html(html, self._page.url)
        if parsed is None:
            return dom
        return fill_missing(parsed, dom)

    async def _wait_for_place_panel(self) -> None:
        """The place title and address render after DOMContentLoaded."""
        assert self._page is not None
        timeout_ms = int(self.config.timeout_seconds * 1000)
        try:
            await self._page.wait_for_selector("h1", state="attached", timeout=timeout_ms)
        except Exception as exc:
            self.logger.warning("place title did not attach", url=self._page.url, detail=str(exc))
            return
        try:
            await self._page.wait_for_selector(
                'button[data-item-id="address"], button[data-item-id^="phone:"], a[data-item-id="authority"]',
                state="attached",
                timeout=min(timeout_ms, 8000),
            )
        except Exception:
            return

    async def _reviews_from_open_page(self) -> list[Review]:
        assert self._page is not None
        try:
            tab = self._page.get_by_role("tab", name=re.compile(r"reviews", re.IGNORECASE))
            if await tab.count():
                await tab.first.click(timeout=3000)
                await self._page.wait_for_timeout(800)
        except Exception as exc:
            self.logger.warning("reviews tab was not clickable", url=self._page.url, detail=str(exc))
        reviews: list[Review] = []
        for url, body in self.captured:
            if "listugcposts" in url or "review" in url:
                reviews.extend(parse_reviews_rpc(body))
        reviews.extend(parse_reviews_html(await self._page.content()))
        return reviews

    async def _collect_links(self, max_results: int) -> list[str]:
        assert self._page is not None
        feed = self._page.locator('div[role="feed"]')
        await feed.first.wait_for(state="visible")
        seen: list[str] = []
        seen_ids: set[str] = set()
        stale = 0
        while len(seen) < max_results and stale < 4:
            hrefs = await self._page.eval_on_selector_all(
                'div[role="feed"] a[href*="/maps/place/"]',
                "elements => elements.map(element => element.href)",
            )
            before = len(seen)
            for href in hrefs:
                if not isinstance(href, str):
                    continue
                identity = extract_place_id(href) or href.split("&", 1)[0]
                if identity in seen_ids:
                    continue
                seen_ids.add(identity)
                seen.append(href)
                if len(seen) >= max_results:
                    break
            end = self._page.get_by_text("You've reached the end of the list")
            if await end.count():
                break
            stale = stale + 1 if len(seen) == before else 0
            await feed.first.evaluate("element => element.scrollBy(0, Math.max(element.scrollHeight, 800))")
            await self._scroll_pause()
        return seen

    async def _goto(self, url: str) -> None:
        assert self._page is not None
        timeout_ms = int(self.config.timeout_seconds * 1000)

        async def once() -> None:
            assert self._page is not None
            try:
                response = await self._page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            except Exception as exc:
                name = type(exc).__name__
                if "Timeout" in name or "TargetClosed" in name:
                    raise RetryableHTTPError(503, url) from exc
                raise
            status = response.status if response is not None else 0
            if status in RETRYABLE_STATUS:
                header = response.headers.get("retry-after") if response is not None else None
                raise RetryableHTTPError(status, url, retry_after_seconds(header))

        try:
            await async_retry(
                once,
                retries=self.config.retries,
                base_delay=self.config.retry_base_delay,
                logger=self.logger,
                url=url,
            )
        except Exception as exc:
            self.logger.error("navigation failed", url=url, error_type=type(exc).__name__, detail=str(exc))
            raise
        await ensure_not_blocked(self._page, self.config, self.logger)
        await self._dismiss_consent()

    async def _dismiss_consent(self) -> None:
        assert self._page is not None
        for name in ("Accept all", "Reject all", "I agree"):
            button = self._page.get_by_role("button", name=name)
            try:
                if await button.count():
                    await button.first.click(timeout=1500)
                    return
            except Exception:
                continue

    async def _on_response(self, response: object) -> None:
        url = getattr(response, "url", "") or ""
        status = getattr(response, "status", 0)
        if status in RETRYABLE_STATUS:
            self.logger.warning("upstream HTTP status", url=url, type="http", status=status)
        if not _interesting(url) or status >= 400:
            return
        try:
            body = await response.text()
        except Exception:
            return
        if not body or len(body) > _MAX_BODY:
            return
        self.captured.append((url, body))
        if len(self.captured) > _MAX_CAPTURED:
            del self.captured[: len(self.captured) - _MAX_CAPTURED]

    def _businesses_from_capture(self) -> list[Business]:
        found: list[Business] = []
        seen: set[str] = set()
        for _url, body in self.captured:
            for business in parse_maps_payloads(body):
                identity = business.place_id or business.name
                if not identity or identity in seen:
                    continue
                seen.add(identity)
                found.append(business)
        return found

    async def _scroll_pause(self) -> None:
        if self.config.delay_max <= 0:
            return
        await asyncio.sleep(random.uniform(0.5, 1.2))

    def _next_proxy(self) -> str | None:
        if not self.config.proxies:
            return None
        proxy = self.config.proxies[self._proxy_index % len(self.config.proxies)]
        self._proxy_index += 1
        return proxy

    def _require_page(self) -> None:
        if self._page is None:
            raise ScraperError("browser session is not started")


def maps_search_url(query: str, location: str, language: str) -> str:
    text = compose_query(query, location)
    return f"https://www.google.com/maps/search/{quote(text)}?hl={quote(language or 'en')}"


def _mark_enriched(businesses: list[Business]) -> list[Business]:
    for business in businesses:
        if business.name and business.address and len(business.reviews) >= 3:
            business.enriched = True
    return businesses


def _interesting(url: str) -> bool:
    markers = ("/maps/preview", "listugcposts", "/maps/rpc", "tbm=map", "/maps/place")
    return any(marker in url for marker in markers)


def _proxy_label(proxy_url: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(proxy_url)
    return f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
