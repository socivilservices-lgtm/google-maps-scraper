"""GoogleMapsScraper coordinates the browser engine and the Places API."""

from __future__ import annotations

import asyncio
import csv
import json
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from gmaps_service.config import Config
from gmaps_service.dedupe import dedupe_businesses, fill_missing
from gmaps_service.emails import fetch_public_emails
from gmaps_service.errors import CaptchaDetectedError
from gmaps_service.logging_utils import EventLogger, get_logger
from gmaps_service.models import Business, Review, ScrapeResult, utc_now
from gmaps_service.normalize import finalize_business, is_usable
from gmaps_service.places_api import PlacesBackend, compose_query
from gmaps_service.playwright_backend import PlaywrightBackend

_CSV_FIELDS = (
    "place_id",
    "name",
    "address",
    "phone",
    "website",
    "rating",
    "review_count",
    "latitude",
    "longitude",
    "categories",
    "opening_hours",
    "open_now",
    "price_level",
    "claimed",
    "reviews",
    "emails",
    "maps_url",
)


class GoogleMapsScraper:
    """Search Maps and save deduplicated business records.

    ``backend`` is ``auto`` (Places API when a key is set, otherwise
    Playwright), ``places``, or ``playwright``.
    """

    def __init__(
        self,
        config: Config | None = None,
        logger: EventLogger | None = None,
        engine_factory: Any = None,
    ) -> None:
        self.config = config or Config.load()
        self.logger = logger or get_logger()
        self._engine_factory = engine_factory
        if self.config.proxies_file and not Path(self.config.proxies_file).is_file():
            self.logger.warning(
                "proxy list file was not found; continuing without that file",
                path=self.config.proxies_file,
            )

    async def search(
        self,
        query: str,
        location: str = "",
        max_results: int | None = None,
        backend: str | None = None,
    ) -> list[Business]:
        """Return places discovered for a query. Records may still need a detail visit."""
        limit = max_results or self.config.max_results
        async with self.session(backend) as engine:
            return await engine.search(query, location, limit)

    async def extract_business_data(self, business_url: str, backend: str | None = None) -> Business:
        """Open one place URL or place id and return its normalized record."""
        async with self.session(backend) as engine:
            business = await engine.extract_business_data(business_url)
            return await self._with_emails(business)

    async def scrape_reviews(self, business_url: str, backend: str | None = None) -> list[Review]:
        """Return up to the configured number of recent reviews for one place."""
        async with self.session(backend) as engine:
            reviews = await engine.scrape_reviews(business_url)
            return reviews[: self.config.max_reviews]

    def save_results(self, data: ScrapeResult | list[Business] | dict[str, Any], filename: str) -> dict[str, str]:
        """Write JSON and CSV siblings. ``filename`` may omit the extension."""
        payload = _payload(data)
        path = Path(filename)
        if path.suffix.lower() in {".json", ".csv"}:
            path = path.with_suffix("")
        path.parent.mkdir(parents=True, exist_ok=True)
        json_path = path.with_suffix(".json")
        csv_path = path.with_suffix(".csv")
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        _write_csv(csv_path, payload.get("businesses") or [])
        self.logger.info(
            "saved results",
            json=str(json_path),
            csv=str(csv_path),
            total_found=payload.get("total_found"),
        )
        return {"json": str(json_path), "csv": str(csv_path)}

    async def run(
        self,
        query: str,
        location: str = "",
        max_results: int | None = None,
        backend: str | None = None,
        filename: str | None = None,
    ) -> ScrapeResult:
        """Search, enrich, dedupe, and optionally save."""
        backend_name = self.config.resolve_backend(backend)
        limit = max_results or self.config.max_results
        full_query = compose_query(query, location)
        self.logger.info("search started", query=full_query, backend=backend_name, max_results=limit)
        async with self.session(backend_name) as engine:
            found = await engine.search(query, location, limit)
            detailed: list[Business] = []
            for index, item in enumerate(found):
                if len(detailed) >= limit:
                    break
                current = item
                if _needs_visit(current):
                    target = current.maps_url or current.place_id or ""
                    try:
                        extracted = await engine.extract_business_data(target)
                        current = fill_missing(extracted, current)
                    except CaptchaDetectedError:
                        raise
                    except Exception as exc:
                        self.logger.error(
                            "failed to extract a business",
                            url=target,
                            error_type=type(exc).__name__,
                            detail=str(exc),
                        )
                current = await self._with_emails(current)
                current = finalize_business(
                    current,
                    default_region=self.config.region_hint,
                    max_reviews=self.config.max_reviews,
                )
                if is_usable(current):
                    detailed.append(current)
                elif current.maps_url or current.name:
                    self.logger.warning("dropped a record that failed validation", url=current.maps_url, name=current.name)
                if index < len(found) - 1 and len(detailed) < limit:
                    await _polite_delay(self.config)
            businesses = dedupe_businesses(detailed)[:limit]
        result = ScrapeResult(
            query=full_query,
            location=location.strip(),
            businesses=businesses,
            backend=backend_name,
        )
        if filename:
            self.save_results(result, filename)
        self.logger.info("search finished", query=full_query, total_found=result.total_found, backend=backend_name)
        return result

    @asynccontextmanager
    async def session(self, backend: str | None = None) -> AsyncIterator[Any]:
        """Open the selected backend for one or more calls."""
        name = self.config.resolve_backend(backend)
        if self._engine_factory is not None:
            async with self._engine_factory(name) as engine:
                yield engine
            return
        if name == "places":
            engine = PlacesBackend(self.config, self.logger)
            try:
                yield engine
            finally:
                await engine.aclose()
            return
        engine = PlaywrightBackend(self.config, self.logger)
        try:
            await engine.start()
            yield engine
        finally:
            await engine.stop()

    async def _with_emails(self, business: Business) -> Business:
        if not self.config.extract_emails or not business.website or business.emails:
            return business
        proxy = self.config.proxies[0] if self.config.proxies else None
        try:
            business.emails = await fetch_public_emails(business.website, logger=self.logger, proxy=proxy)
        except Exception as exc:
            self.logger.error(
                "email extraction failed",
                url=business.website,
                error_type="email",
                detail=str(exc),
            )
        return business


def _needs_visit(business: Business) -> bool:
    if not (business.maps_url or business.place_id):
        return False
    if business.enriched and business.name:
        return False
    if business.name and business.address and len(business.reviews) >= 3:
        return False
    return True


async def _polite_delay(config: Config) -> None:
    if config.delay_max <= 0:
        return
    low = min(config.delay_min, config.delay_max)
    high = max(config.delay_min, config.delay_max)
    await asyncio.sleep(random.uniform(low, high))


def _payload(data: ScrapeResult | list[Business] | dict[str, Any]) -> dict[str, Any]:
    if isinstance(data, ScrapeResult):
        return data.to_dict()
    if isinstance(data, list):
        businesses = [item.to_dict() if isinstance(item, Business) else item for item in data]
        return {
            "timestamp": utc_now(),
            "query": "",
            "location": "",
            "total_found": len(businesses),
            "backend": "",
            "businesses": businesses,
        }
    if isinstance(data, dict):
        payload = dict(data)
        payload.setdefault("timestamp", utc_now())
        payload.setdefault("total_found", len(payload.get("businesses") or []))
        return payload
    raise TypeError(f"unsupported results type: {type(data).__name__}")


def _write_csv(path: Path, businesses: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for business in businesses:
            row = {field: business.get(field) for field in _CSV_FIELDS}
            row["categories"] = "; ".join(business.get("categories") or [])
            row["emails"] = "; ".join(business.get("emails") or [])
            row["opening_hours"] = json.dumps(business.get("opening_hours") or {}, ensure_ascii=False)
            row["reviews"] = json.dumps(business.get("reviews") or [], ensure_ascii=False)
            for key, value in list(row.items()):
                if value is None:
                    row[key] = ""
            writer.writerow(row)
