"""Accounts, trials, quotas, billing webhooks, and job ownership."""

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from gmaps_service.api import create_app
from gmaps_service.config import Config
from gmaps_service.models import Business, ScrapeResult
from gmaps_service.saas import db
from gmaps_service.saas.billing import apply_event


def _config(tmp_path, **overrides):
    data = dict(
        output_dir=str(tmp_path),
        database_url=f"sqlite:///{tmp_path}/app.db",
        secret_key="test-secret-key-with-enough-length",
        admin_email="owner@example.com",
        service_api_key="",
        delay_min=0,
        delay_max=0,
        headless=True,
        backend="playwright",
        monthly_result_quota=2,
        monthly_search_quota=5,
        max_results_per_search=2,
        login_rate_limit=8,
        stripe_secret_key="",
        stripe_webhook_secret="",
        stripe_price_id="",
    )
    data.update(overrides)
    return Config(**data)


async def _runner(**kwargs):
    return ScrapeResult(
        query="coffee shops in New York",
        location=str(kwargs.get("location") or ""),
        backend="playwright",
        businesses=[
            Business(name="Cafe", address="1 Main", place_id="p1", rating=4.5, review_count=3),
        ],
    )


def _client(tmp_path, **overrides):
    config = _config(tmp_path, **overrides)
    return TestClient(create_app(config, runner=_runner), follow_redirects=False), config


def _csrf(client: TestClient, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200
    token = client.cookies.get("gmaps_csrf")
    assert token
    return token


def test_signup_hashes_password_and_invite_starts_trial(tmp_path):
    client, config = _client(tmp_path)
    engine = db.get_engine(config.database_url)
    admin_token = _csrf(client, "/signup?invite=")
    created = client.post(
        "/signup",
        data={"email": "owner@example.com", "password": "correct-horse", "invite": "", "csrf_token": admin_token},
    )
    assert created.status_code == 303
    admin = db.get_user_by_email(engine, "owner@example.com")
    assert admin["role"] == "admin"
    assert admin["password_hash"] != "correct-horse"
    assert admin["password_hash"].startswith("$2")

    invite = db.create_invite(engine, created_by=admin["id"], max_uses=1)
    client.post("/logout", data={"csrf_token": client.cookies.get("gmaps_csrf")})
    token = _csrf(client, f"/signup?invite={invite['code']}")
    signed = client.post(
        "/signup",
        data={
            "email": "trial@example.com",
            "password": "another-password",
            "invite": invite["code"],
            "csrf_token": token,
        },
    )
    assert signed.status_code == 303
    trial_user = db.get_user_by_email(engine, "trial@example.com")
    assert trial_user["trial_ends_at"]
    assert db.parse_iso(trial_user["trial_ends_at"]) > datetime.now(timezone.utc)


def test_expired_trial_is_blocked_and_admin_is_not(tmp_path):
    client, config = _client(tmp_path)
    engine = db.get_engine(config.database_url)
    token = _csrf(client, "/signup")
    client.post(
        "/signup",
        data={"email": "owner@example.com", "password": "correct-horse", "invite": "", "csrf_token": token},
    )
    admin = db.get_user_by_email(engine, "owner@example.com")
    invite = db.create_invite(engine, created_by=admin["id"])
    client.post("/logout", data={"csrf_token": client.cookies.get("gmaps_csrf")})
    token = _csrf(client, "/signup")
    client.post(
        "/signup",
        data={"email": "trial@example.com", "password": "another-password", "invite": invite["code"], "csrf_token": token},
    )
    user = db.get_user_by_email(engine, "trial@example.com")
    db.set_trial_end(engine, user["id"], datetime.now(timezone.utc) - timedelta(days=1))
    token = client.cookies.get("gmaps_csrf")
    blocked = client.post(
        "/app/search",
        data={"query": "coffee shops", "location": "New York", "max_results": 1, "backend": "playwright", "csrf_token": token},
        follow_redirects=False,
    )
    assert blocked.status_code == 303
    assert "trial" in blocked.headers["location"].lower() or "subscription" in blocked.headers["location"].lower() or "error" in blocked.headers["location"]

    client.post("/logout", data={"csrf_token": token})
    login_token = _csrf(client, "/login")
    admin_login = client.post(
        "/login",
        data={"email": "owner@example.com", "password": "correct-horse", "csrf_token": login_token},
        follow_redirects=False,
    )
    assert admin_login.status_code == 303
    allowed = client.post(
        "/app/search",
        data={
            "query": "coffee shops",
            "location": "New York",
            "max_results": 1,
            "backend": "playwright",
            "csrf_token": client.cookies.get("gmaps_csrf"),
        },
        follow_redirects=False,
    )
    assert allowed.status_code == 303
    assert "/app/jobs/" in allowed.headers["location"]


def test_quota_blocks_the_next_search(tmp_path):
    client, config = _client(tmp_path, monthly_result_quota=1, monthly_search_quota=5)
    engine = db.get_engine(config.database_url)
    token = _csrf(client, "/signup")
    client.post(
        "/signup",
        data={"email": "paid@example.com", "password": "correct-horse", "invite": "", "csrf_token": token},
    )
    user = db.get_user_by_email(engine, "paid@example.com")
    db.set_subscription(engine, user_id=user["id"], status="active")
    raw, prefix, digest = __import__("gmaps_service.saas.auth", fromlist=["new_api_key"]).new_api_key()
    db.insert_api_key(engine, user_id=user["id"], name="ci", prefix=prefix, key_hash=digest)
    first = client.post(
        "/search",
        headers={"X-API-Key": raw},
        json={"query": "coffee shops", "location": "New York", "max_results": 1, "backend": "playwright", "wait": True},
    )
    assert first.status_code == 200
    second = client.post(
        "/search",
        headers={"X-API-Key": raw},
        json={"query": "coffee shops", "location": "New York", "max_results": 1, "backend": "playwright", "wait": True},
    )
    assert second.status_code == 402


def test_job_is_hidden_from_another_user(tmp_path):
    client, _config = _client(tmp_path, monthly_result_quota=10)
    token = _csrf(client, "/signup")
    client.post(
        "/signup",
        data={"email": "owner@example.com", "password": "correct-horse", "invite": "", "csrf_token": token},
    )
    created = client.post(
        "/app/search",
        data={
            "query": "coffee shops",
            "location": "New York",
            "max_results": 1,
            "backend": "playwright",
            "csrf_token": client.cookies.get("gmaps_csrf"),
        },
        follow_redirects=False,
    )
    job_id = created.headers["location"].rsplit("/", 1)[-1]
    client.post("/logout", data={"csrf_token": client.cookies.get("gmaps_csrf")})
    token = _csrf(client, "/signup")
    client.post(
        "/signup",
        data={"email": "other@example.com", "password": "correct-horse", "invite": "", "csrf_token": token},
    )
    hidden = client.get(f"/jobs/{job_id}")
    assert hidden.status_code == 404
    page = client.get(f"/app/jobs/{job_id}")
    assert page.status_code == 404


def test_stripe_webhook_activates_and_cancels(tmp_path, monkeypatch):
    client, config = _client(
        tmp_path,
        stripe_secret_key="sk_test_dummy",
        stripe_webhook_secret="whsec_dummy",
        stripe_price_id="price_dummy",
    )
    engine = db.get_engine(config.database_url)
    token = _csrf(client, "/signup")
    client.post(
        "/signup",
        data={"email": "buyer@example.com", "password": "correct-horse", "invite": "", "csrf_token": token},
    )
    user = db.get_user_by_email(engine, "buyer@example.com")
    disabled = client.post("/billing/webhook", content=b"{}", headers={"stripe-signature": "bad"})
    # A configured secret that fails verification is a 400, not a silent accept.
    assert disabled.status_code == 400

    def fake_event(payload, signature, secret):
        assert secret == "whsec_dummy"
        assert signature == "signed"
        return {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "client_reference_id": user["id"],
                    "customer": "cus_123",
                    "subscription": "sub_123",
                }
            },
        }

    monkeypatch.setattr("gmaps_service.api.construct_event", fake_event)
    accepted = client.post("/billing/webhook", content=b"{}", headers={"stripe-signature": "signed"})
    assert accepted.status_code == 200
    assert accepted.json()["outcome"] == "activated"
    refreshed = db.get_user_by_id(engine, user["id"])
    assert refreshed["subscription_status"] == "active"
    assert refreshed["stripe_customer_id"] == "cus_123"

    def canceled_event(payload, signature, secret):
        return {
            "type": "customer.subscription.deleted",
            "data": {"object": {"id": "sub_123", "customer": "cus_123", "status": "canceled", "metadata": {}}},
        }

    monkeypatch.setattr("gmaps_service.api.construct_event", canceled_event)
    client.post("/billing/webhook", content=b"{}", headers={"stripe-signature": "signed"})
    canceled = db.get_user_by_id(engine, user["id"])
    assert canceled["subscription_status"] == "canceled"
    apply_event(
        engine,
        {
            "type": "customer.subscription.updated",
            "data": {"object": {"id": "sub_123", "customer": "cus_123", "status": "active", "metadata": {"user_id": user["id"]}}},
        },
    )
    assert db.get_user_by_id(engine, user["id"])["subscription_status"] == "active"


def test_billing_webhook_without_stripe_is_explicit(tmp_path):
    client, _config = _client(tmp_path)
    response = client.post("/billing/webhook", content=b"{}")
    assert response.status_code == 503
    assert "Billing is not configured" in response.json()["detail"]


def test_login_rate_limit(tmp_path):
    client, _config = _client(tmp_path, login_rate_limit=2)
    token = _csrf(client, "/login")
    for _ in range(2):
        response = client.post(
            "/login",
            data={"email": "nobody@example.com", "password": "correct-horse", "csrf_token": token},
        )
        assert response.status_code == 400
    limited = client.post(
        "/login",
        data={"email": "nobody@example.com", "password": "correct-horse", "csrf_token": token},
    )
    assert limited.status_code == 429


def test_login_without_csrf_is_rejected(tmp_path):
    client, _config = _client(tmp_path)
    response = client.post(
        "/login",
        data={"email": "owner@example.com", "password": "correct-horse", "csrf_token": "nope"},
    )
    assert response.status_code == 403
