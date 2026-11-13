"""Places API client tests with a mocked HTTP transport."""

import json

import httpx
import pytest

from gmaps_service.config import Config
from gmaps_service.logging_utils import EventLogger
from gmaps_service.places_api import PlacesBackend

from paths import FIXTURES


class _Null:
    def write(self, _text):
        return None

    def flush(self):
        return None


def _config():
    return Config(
        backend="places",
        google_places_api_key="test-key",
        delay_min=0,
        delay_max=0,
        retries=2,
        retry_base_delay=0,
    )


@pytest.mark.asyncio
async def test_search_uses_saved_places_payload_and_retries_429():
    payload = json.loads((FIXTURES / "places_search.json").read_text(encoding="utf-8"))
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert request.headers["x-goog-api-key"] == "test-key"
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "0"}, json={"error": {"message": "slow down"}})
        body = json.loads(request.content)
        assert body["textQuery"] == "coffee shops in New York"
        return httpx.Response(200, json=payload)

    client = PlacesBackend(_config(), logger=EventLogger(stream=_Null()), transport=httpx.MockTransport(handler))
    found = await client.search("coffee shops", "New York", max_results=10)
    await client.aclose()
    assert calls["n"] == 2
    assert found[0].name == "Blue Bottle Coffee"
    assert found[0].reviews[0].author == "Sam Lee"
    assert found[1].website is None


@pytest.mark.asyncio
async def test_details_fill_reviews_when_search_omits_them():
    search_payload = json.loads((FIXTURES / "places_search.json").read_text(encoding="utf-8"))
    for place in search_payload["places"]:
        place.pop("reviews", None)
        if place["id"] != "ChIJneedsdetails":
            pass
    search_payload["places"] = [
        {
            "id": "ChIJneedsdetails",
            "displayName": {"text": "Detail Coffee"},
            "formattedAddress": "10 Pine St, New York, NY",
            "location": {"latitude": 40.7, "longitude": -74.0},
            "types": ["coffee_shop"],
        }
    ]
    details = json.loads((FIXTURES / "places_details.json").read_text(encoding="utf-8"))
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith(":searchText"):
            mask = request.headers.get("x-goog-fieldmask", "")
            if "reviews" in mask:
                return httpx.Response(400, json={"error": {"message": "reviews is not a valid field"}})
            return httpx.Response(200, json=search_payload)
        return httpx.Response(200, json=details)

    client = PlacesBackend(_config(), logger=EventLogger(stream=_Null()), transport=httpx.MockTransport(handler))
    found = await client.search("coffee", "New York", max_results=5)
    await client.aclose()
    assert any(path.endswith(":searchText") for path in paths)
    assert any(path.endswith("/ChIJneedsdetails") for path in paths)
    assert found[0].name == "Detail Coffee"
    assert found[0].reviews[0].author == "Riley"
    assert found[0].phone == "+12123439000"
