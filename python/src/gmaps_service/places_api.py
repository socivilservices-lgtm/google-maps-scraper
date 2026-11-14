"""Official Google Places API (New) backend.

Text Search and Place Details are used when ``GOOGLE_PLACES_API_KEY`` is set.
Reviews are requested on the search call and, if the key's SKU rejects that
field, the search is repeated without them and details are loaded per place.
"""

from __future__ import annotations

import random
from typing import Any

import httpx

from gmaps_service.config import Config
from gmaps_service.errors import PlacesAPIError
from gmaps_service.logging_utils import EventLogger, get_logger
from gmaps_service.models import Business, Review
from gmaps_service.parsers import business_from_places
from gmaps_service.retry import RETRYABLE_STATUS, RetryableHTTPError, async_retry, retry_after_seconds

_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
_DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"
_SEARCH_FIELDS = (
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.nationalPhoneNumber",
    "places.internationalPhoneNumber",
    "places.websiteUri",
    "places.rating",
    "places.userRatingCount",
    "places.location",
    "places.types",
    "places.regularOpeningHours",
    "places.currentOpeningHours",
    "places.priceLevel",
    "places.businessStatus",
    "places.googleMapsUri",
    "places.reviews",
)
_SEARCH_FIELDS_CORE = tuple(field for field in _SEARCH_FIELDS if field != "places.reviews")
_DETAIL_FIELDS = ",".join(field.removeprefix("places.") for field in _SEARCH_FIELDS)


def compose_query(query: str, location: str) -> str:
    text = query.strip()
    place = location.strip()
    if place and place.casefold() not in text.casefold():
        return f"{text} in {place}"
    return text


class PlacesBackend:
    """Search and detail calls against Places API (New)."""

    def __init__(
        self,
        config: Config,
        logger: EventLogger | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not config.google_places_api_key:
            raise PlacesAPIError("GOOGLE_PLACES_API_KEY is not set")
        self.config = config
        self.logger = logger or get_logger()
        client_kwargs: dict[str, Any] = {
            "timeout": config.timeout_seconds,
            "headers": {
                "Content-Type": "application/json",
                "X-Goog-Api-Key": config.google_places_api_key,
            },
        }
        if transport is not None:
            client_kwargs["transport"] = transport
        if config.proxies:
            client_kwargs["proxy"] = config.proxies[0]
        self._client = httpx.AsyncClient(**client_kwargs)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def search(self, query: str, location: str, max_results: int) -> list[Business]:
        text_query = compose_query(query, location)
        found: list[Business] = []
        page_token = ""
        include_reviews = True
        while len(found) < max_results:
            body: dict[str, Any] = {
                "textQuery": text_query,
                "pageSize": min(20, max_results - len(found)),
                "languageCode": self.config.language or "en",
            }
            if len(self.config.region_hint) == 2:
                body["regionCode"] = self.config.region_hint.upper()
            if page_token:
                body["pageToken"] = page_token
                await self._polite()
            payload, include_reviews = await self._search_page(body, include_reviews)
            for place in payload.get("places") or []:
                if isinstance(place, dict):
                    found.append(business_from_places(place))
            page_token = str(payload.get("nextPageToken") or "")
            if not page_token:
                break
        detailed: list[Business] = []
        for index, business in enumerate(found[:max_results]):
            if business.place_id and not business.reviews:
                if index:
                    await self._polite()
                try:
                    detailed.append(await self.extract_business_data(business.place_id))
                except PlacesAPIError as exc:
                    self.logger.error(
                        "place details failed",
                        url=_DETAILS_URL.format(place_id=business.place_id),
                        error_type="places",
                        detail=str(exc),
                    )
                    detailed.append(business)
            else:
                detailed.append(business)
        return detailed

    async def extract_business_data(self, business_url: str) -> Business:
        place_id = business_url
        if business_url.startswith("http"):
            from gmaps_service.parsers import extract_place_id

            place_id = extract_place_id(business_url) or ""
            if not place_id:
                raise PlacesAPIError("could not read a place id from the Maps URL")
        url = _DETAILS_URL.format(place_id=place_id)
        payload = await self._request("GET", url, None, _DETAIL_FIELDS)
        business = business_from_places(payload)
        business.enriched = True
        return business

    async def scrape_reviews(self, business_url: str) -> list[Review]:
        business = await self.extract_business_data(business_url)
        return business.reviews

    async def _search_page(self, body: dict[str, Any], include_reviews: bool) -> tuple[dict[str, Any], bool]:
        mask = ",".join(_SEARCH_FIELDS if include_reviews else _SEARCH_FIELDS_CORE)
        try:
            payload = await self._request("POST", _SEARCH_URL, body, mask)
            return payload, include_reviews
        except PlacesAPIError as exc:
            if not include_reviews:
                raise
            self.logger.warning(
                "Places API rejected the search field mask; retrying without reviews",
                url=_SEARCH_URL,
                type="places",
                detail=str(exc),
            )
            payload = await self._request("POST", _SEARCH_URL, body, ",".join(_SEARCH_FIELDS_CORE))
            return payload, False

    async def _request(self, method: str, url: str, body: dict[str, Any] | None, field_mask: str) -> dict[str, Any]:
        async def once() -> dict[str, Any]:
            response = await self._client.request(
                method,
                url,
                json=body,
                headers={"X-Goog-FieldMask": field_mask},
            )
            if response.status_code in RETRYABLE_STATUS:
                raise RetryableHTTPError(
                    response.status_code,
                    url,
                    retry_after_seconds(response.headers.get("retry-after")),
                )
            if response.status_code >= 400:
                message = _error_message(response)
                self.logger.error(
                    "Places API request failed",
                    url=url,
                    error_type="places",
                    status=response.status_code,
                )
                raise PlacesAPIError(f"Places API HTTP {response.status_code}: {message}")
            data = response.json()
            if not isinstance(data, dict):
                raise PlacesAPIError("Places API returned a non-object payload")
            return data

        return await async_retry(
            once,
            retries=self.config.retries,
            base_delay=self.config.retry_base_delay,
            logger=self.logger,
            url=url,
        )

    async def _polite(self) -> None:
        if self.config.delay_max <= 0:
            return
        low = min(self.config.delay_min, self.config.delay_max)
        high = max(self.config.delay_min, self.config.delay_max)
        await self._sleep(random.uniform(low, high))

    async def _sleep(self, seconds: float) -> None:
        import asyncio

        await asyncio.sleep(seconds)


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])[:300]
    return response.text[:300]

