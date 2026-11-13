"""HTTP API tests. The runner is fake, so no browser or Places key is required."""

from gmaps_service.api import create_app
from gmaps_service.config import Config
from gmaps_service.errors import CaptchaDetectedError
from gmaps_service.models import Business, ScrapeResult

from fastapi.testclient import TestClient


def _config(tmp_path, api_key="test-key"):
    return Config(
        service_api_key=api_key,
        google_places_api_key="places-key",
        backend="places",
        output_dir=str(tmp_path),
        delay_min=0,
        delay_max=0,
        headless=True,
    )


async def _runner(**kwargs):
    return ScrapeResult(
        query="coffee shops in New York",
        location=kwargs["location"],
        backend="places",
        businesses=[
            Business(
                name="Blue Bottle Coffee",
                address="76 N 4th St, Brooklyn, NY 11249",
                phone="+12123439000",
                place_id="ChIJbluebottleexample",
                rating=4.6,
                review_count=842,
            )
        ],
    )


def test_health_search_and_download(tmp_path):
    app = create_app(_config(tmp_path), runner=_runner)
    client = TestClient(app)
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["places_api_configured"] is True

    denied = client.post("/search", json={"query": "coffee shops", "location": "New York"})
    assert denied.status_code == 401

    created = client.post(
        "/search",
        headers={"X-API-Key": "test-key"},
        json={"query": "coffee shops", "location": "New York", "max_results": 10, "backend": "places", "wait": True},
    )
    assert created.status_code == 200
    body = created.json()
    assert body["status"] == "completed"
    assert body["result"]["total_found"] == 1
    assert body["result"]["query"] == "coffee shops in New York"
    job_id = body["job_id"]

    status = client.get(f"/jobs/{job_id}", headers={"X-API-Key": "test-key"})
    assert status.json()["result"]["businesses"][0]["name"] == "Blue Bottle Coffee"

    downloaded = client.get(f"/jobs/{job_id}/download?format=csv", headers={"X-API-Key": "test-key"})
    assert downloaded.status_code == 200
    assert "Blue Bottle Coffee" in downloaded.text
    assert "timestamp" not in downloaded.headers.get("content-type", "")

    missing = client.get("/jobs/not-a-job", headers={"X-API-Key": "test-key"})
    assert missing.status_code == 404


def test_background_job_and_captcha_failure(tmp_path):
    async def fail(**kwargs):
        raise CaptchaDetectedError("Google showed a CAPTCHA or unusual-traffic page")

    app = create_app(_config(tmp_path, api_key=""), runner=fail)
    client = TestClient(app)
    created = client.post("/search", json={"query": "coffee shops", "location": "New York", "wait": False})
    assert created.status_code == 200
    job_id = created.json()["job_id"]
    # TestClient runs the background task before returning control.
    status = client.get(f"/jobs/{job_id}")
    assert status.status_code == 200
    assert status.json()["status"] == "failed"
    assert status.json()["error_type"] == "captcha"
    assert "CAPTCHA" in status.json()["error"]


def test_places_backend_requires_a_key(tmp_path):
    config = Config(
        service_api_key="test-key",
        google_places_api_key="",
        backend="playwright",
        output_dir=str(tmp_path),
        delay_min=0,
        delay_max=0,
    )
    app = create_app(config, runner=_runner)
    client = TestClient(app)
    response = client.post(
        "/search",
        headers={"X-API-Key": "test-key"},
        json={"query": "coffee shops", "location": "New York", "backend": "places"},
    )
    assert response.status_code == 400
    assert "GOOGLE_PLACES_API_KEY" in response.json()["detail"]
