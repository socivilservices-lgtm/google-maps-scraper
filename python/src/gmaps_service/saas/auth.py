"""Sessions, API keys, and who is allowed to run a search."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from gmaps_service.config import Config
from gmaps_service.saas import db
from gmaps_service.saas.passwords import verify_password

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SESSION_MAX_AGE = 60 * 60 * 24 * 14
PAID_STATUSES = {"active", "trialing"}


class AuthError(Exception):
    """Login or signup failed."""


@dataclass
class Actor:
    """The caller of an HTTP request."""

    kind: str
    user: dict[str, Any] | None = None

    @property
    def user_id(self) -> str | None:
        if self.user is None:
            return None
        return str(self.user["id"])

    @property
    def is_admin(self) -> bool:
        if self.kind == "service":
            return True
        return bool(self.user and self.user.get("role") == "admin")

    @property
    def counts_toward_quota(self) -> bool:
        return self.kind == "user" and not self.is_admin


@dataclass
class Entitlement:
    allowed: bool
    reason: str
    plan: str
    unlimited: bool = False


def normalize_email(email: str) -> str:
    return email.strip().lower()


def validate_email(email: str) -> str:
    cleaned = normalize_email(email)
    if not _EMAIL.fullmatch(cleaned) or len(cleaned) > 200:
        raise AuthError("Enter a valid email address.")
    return cleaned


def validate_password(password: str) -> str:
    if len(password) < 10 or len(password) > 200:
        raise AuthError("Use a password between 10 and 200 characters.")
    return password


def session_serializer(config: Config) -> URLSafeTimedSerializer:
    secret = config.secret_key or "dev-only-change-me"
    return URLSafeTimedSerializer(secret, salt="gmaps-session")


def issue_session(config: Config, user_id: str) -> str:
    return session_serializer(config).dumps({"uid": user_id})


def read_session(config: Config, token: str | None) -> str | None:
    if not token:
        return None
    try:
        data = session_serializer(config).loads(token, max_age=_SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    user_id = data.get("uid") if isinstance(data, dict) else None
    return str(user_id) if user_id else None


def new_api_key() -> tuple[str, str, str]:
    """Return the raw key, a display prefix, and the sha256 hash to store."""
    raw = "gms_" + secrets.token_urlsafe(32)
    prefix = raw[:12]
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return raw, prefix, digest


def hash_api_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def signup(
    engine: Any,
    config: Config,
    *,
    email: str,
    password: str,
    invite_code: str | None,
) -> dict[str, Any]:
    cleaned = validate_email(email)
    validate_password(password)
    if db.get_user_by_email(engine, cleaned):
        raise AuthError("An account with that email already exists.")
    return db.create_user(
        engine,
        email=cleaned,
        password=password,
        admin_email=config.admin_email,
        invite_code=invite_code or None,
    )


def login(engine: Any, config: Config, *, email: str, password: str) -> dict[str, Any]:
    cleaned = validate_email(email)
    user = db.get_user_by_email(engine, cleaned)
    if user is None or not verify_password(password, user["password_hash"]):
        raise AuthError("Email or password is incorrect.")
    if config.admin_email and cleaned == config.admin_email.strip().lower() and user["role"] != "admin":
        db.promote_admin(engine, user["id"])
        refreshed = db.get_user_by_id(engine, user["id"])
        if refreshed is not None:
            return refreshed
    return user


def user_from_api_key(engine: Any, raw_key: str) -> dict[str, Any] | None:
    record = db.find_api_key(engine, hash_api_key(raw_key))
    if record is None or record.get("revoked_at"):
        return None
    return db.get_user_by_id(engine, record["user_id"])


def entitlement(user: dict[str, Any] | None, *, now: datetime | None = None) -> Entitlement:
    """Admin is unlimited. Invite trials last 14 days. Otherwise a paid subscription is required."""
    moment = now or datetime.now(timezone.utc)
    if user is None:
        return Entitlement(False, "Sign in to run a search.", "none")
    if user.get("role") == "admin":
        return Entitlement(True, "Admin access does not expire.", "admin", unlimited=True)
    status = str(user.get("subscription_status") or "none")
    if status in PAID_STATUSES:
        return Entitlement(True, "Paid subscription is active.", "pro")
    trial_end = db.parse_iso(user.get("trial_ends_at"))
    if trial_end is not None and trial_end > moment:
        return Entitlement(True, "Invite trial is active.", "trial")
    if trial_end is not None:
        return Entitlement(False, "Your 14-day trial has ended. Subscribe to keep searching.", "expired")
    return Entitlement(False, "An active subscription is required. Ask an admin for an invite to start a trial.", "none")


def can_view_job(actor: Actor, job_user_id: str | None) -> bool:
    """Owners see their jobs. Admins and the operator key see every job."""
    if actor.is_admin:
        return True
    if actor.user_id and job_user_id and actor.user_id == job_user_id:
        return True
    return False


def keys_match(provided: str, expected: str) -> bool:
    if not expected:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))
