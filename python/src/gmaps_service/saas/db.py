"""SQLite and Postgres access for accounts, invites, jobs, and quotas."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from importlib.resources import files
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from gmaps_service.saas.passwords import hash_password

_ENGINES: dict[str, Engine] = {}
TRIAL_DAYS = 14
_USER_SELECT = (
    "SELECT id, email, password_hash, role, created_at, invite_id, trial_ends_at, "
    "stripe_customer_id, stripe_subscription_id, subscription_status, plan FROM users "
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def get_engine(url: str) -> Engine:
    """Return a migrated engine. One engine is cached per URL."""
    existing = _ENGINES.get(url)
    if existing is not None:
        return existing
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=connect_args, pool_pre_ping=True)
    migrate(engine)
    _ENGINES[url] = engine
    return engine


def migrate(engine: Engine) -> None:
    """Apply packaged SQL migrations once."""
    script = (files("gmaps_service") / "migrations" / "001_init.sql").read_text(encoding="utf-8")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
        )
        applied = {
            row[0]
            for row in conn.execute(text("SELECT version FROM schema_migrations"))
        }
        if "001_init" not in applied:
            for statement in _statements(script):
                conn.execute(text(statement))
            conn.execute(
                text("INSERT INTO schema_migrations (version, applied_at) VALUES (:v, :t)"),
                {"v": "001_init", "t": iso(utc_now())},
            )


def _statements(script: str) -> list[str]:
    parts: list[str] = []
    for chunk in script.split(";"):
        statement = chunk.strip()
        if statement:
            parts.append(statement)
    return parts


def _row(mapping: Any) -> dict[str, Any]:
    return dict(mapping)


def create_user(
    engine: Engine,
    *,
    email: str,
    password: str,
    admin_email: str,
    invite_code: str | None,
) -> dict[str, Any]:
    """Create a user. A valid invite starts a 14-day trial. ADMIN_EMAIL is always an admin."""
    normalized = email.strip().lower()
    now = utc_now()
    role = "admin" if admin_email and normalized == admin_email.strip().lower() else "user"
    user_id = uuid.uuid4().hex
    with engine.begin() as conn:
        invite = _lock_invite(conn, invite_code, now) if invite_code and role != "admin" else None
        trial_ends = None
        invite_id = None
        if invite is not None:
            updated = conn.execute(
                text("UPDATE invites SET uses = uses + 1 WHERE id = :id AND uses < max_uses"),
                {"id": invite["id"]},
            )
            if updated.rowcount == 1:
                invite_id = invite["id"]
                trial_ends = now + timedelta(days=TRIAL_DAYS)
        conn.execute(
            text(
                "INSERT INTO users ("
                "id, email, password_hash, role, created_at, invite_id, trial_ends_at, "
                "subscription_status, plan) "
                "VALUES (:id, :email, :password_hash, :role, :created_at, :invite_id, "
                ":trial_ends_at, 'none', 'none')"
            ),
            {
                "id": user_id,
                "email": normalized,
                "password_hash": hash_password(password),
                "role": role,
                "created_at": iso(now),
                "invite_id": invite_id,
                "trial_ends_at": iso(trial_ends),
            },
        )
    user = get_user_by_id(engine, user_id)
    if user is None:
        raise RuntimeError("user insert failed")
    return user


def _lock_invite(conn: Any, code: str, now: datetime) -> dict[str, Any] | None:
    row = conn.execute(
        text(
            "SELECT id, code, expires_at, max_uses, uses FROM invites "
            "WHERE code = :code"
        ),
        {"code": code.strip()},
    ).mappings().first()
    if row is None:
        return None
    invite = _row(row)
    expires = parse_iso(invite.get("expires_at"))
    if expires is not None and expires <= now:
        return None
    if int(invite["uses"]) >= int(invite["max_uses"]):
        return None
    return invite


def get_user_by_email(engine: Engine, email: str) -> dict[str, Any] | None:
    with engine.connect() as conn:
        row = conn.execute(
            text(_USER_SELECT + "WHERE email = :email"),
            {"email": email.strip().lower()},
        ).mappings().first()
    return _row(row) if row else None


def get_user_by_id(engine: Engine, user_id: str) -> dict[str, Any] | None:
    with engine.connect() as conn:
        row = conn.execute(
            text(_USER_SELECT + "WHERE id = :id"),
            {"id": user_id},
        ).mappings().first()
    return _row(row) if row else None


def promote_admin(engine: Engine, user_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(text("UPDATE users SET role = 'admin' WHERE id = :id"), {"id": user_id})


def list_users(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, email, role, created_at, trial_ends_at, subscription_status, plan "
                "FROM users ORDER BY created_at DESC"
            )
        ).mappings()
        return [_row(row) for row in rows]


def create_invite(
    engine: Engine,
    *,
    created_by: str,
    max_uses: int = 1,
    expires_at: datetime | None = None,
) -> dict[str, Any]:
    import secrets

    code = secrets.token_urlsafe(9)
    invite_id = uuid.uuid4().hex
    now = iso(utc_now())
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO invites (id, code, created_by, created_at, expires_at, max_uses, uses) "
                "VALUES (:id, :code, :created_by, :created_at, :expires_at, :max_uses, 0)"
            ),
            {
                "id": invite_id,
                "code": code,
                "created_by": created_by,
                "created_at": now,
                "expires_at": iso(expires_at),
                "max_uses": max_uses,
            },
        )
    return {"id": invite_id, "code": code, "max_uses": max_uses, "uses": 0, "expires_at": iso(expires_at)}


def list_invites(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, code, created_by, created_at, expires_at, max_uses, uses "
                "FROM invites ORDER BY created_at DESC"
            )
        ).mappings()
        return [_row(row) for row in rows]


def insert_api_key(engine: Engine, *, user_id: str, name: str, prefix: str, key_hash: str) -> str:
    key_id = uuid.uuid4().hex
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO api_keys (id, user_id, name, prefix, key_hash, created_at) "
                "VALUES (:id, :user_id, :name, :prefix, :key_hash, :created_at)"
            ),
            {
                "id": key_id,
                "user_id": user_id,
                "name": name[:80],
                "prefix": prefix,
                "key_hash": key_hash,
                "created_at": iso(utc_now()),
            },
        )
    return key_id


def find_api_key(engine: Engine, key_hash: str) -> dict[str, Any] | None:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT id, user_id, name, prefix, revoked_at FROM api_keys "
                "WHERE key_hash = :key_hash"
            ),
            {"key_hash": key_hash},
        ).mappings().first()
    return _row(row) if row else None


def list_api_keys(engine: Engine, user_id: str) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, name, prefix, created_at, revoked_at FROM api_keys "
                "WHERE user_id = :user_id ORDER BY created_at DESC"
            ),
            {"user_id": user_id},
        ).mappings()
        return [_row(row) for row in rows]


def revoke_api_key(engine: Engine, *, user_id: str, key_id: str) -> bool:
    with engine.begin() as conn:
        result = conn.execute(
            text(
                "UPDATE api_keys SET revoked_at = :now "
                "WHERE id = :id AND user_id = :user_id AND revoked_at IS NULL"
            ),
            {"now": iso(utc_now()), "id": key_id, "user_id": user_id},
        )
    return bool(result.rowcount)


def insert_job(
    engine: Engine,
    *,
    job_id: str,
    user_id: str | None,
    query: str,
    location: str,
    max_results: int,
    backend: str,
) -> None:
    now = iso(utc_now())
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO jobs (id, user_id, status, query, location, max_results, backend, "
                "created_at, updated_at, result_count) "
                "VALUES (:id, :user_id, 'queued', :query, :location, :max_results, :backend, "
                ":created_at, :updated_at, 0)"
            ),
            {
                "id": job_id,
                "user_id": user_id,
                "query": query,
                "location": location,
                "max_results": max_results,
                "backend": backend,
                "created_at": now,
                "updated_at": now,
            },
        )


def update_job(
    engine: Engine,
    job_id: str,
    *,
    status: str,
    error: str | None = None,
    error_type: str | None = None,
    result_count: int | None = None,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE jobs SET status = :status, updated_at = :updated_at, "
                "error = :error, error_type = :error_type, "
                "result_count = COALESCE(:result_count, result_count) "
                "WHERE id = :id"
            ),
            {
                "id": job_id,
                "status": status,
                "updated_at": iso(utc_now()),
                "error": error,
                "error_type": error_type,
                "result_count": result_count,
            },
        )


def get_job(engine: Engine, job_id: str) -> dict[str, Any] | None:
    with engine.connect() as conn:
        row = conn.execute(text("SELECT * FROM jobs WHERE id = :id"), {"id": job_id}).mappings().first()
    return _row(row) if row else None


def list_jobs(engine: Engine, *, user_id: str | None, include_all: bool) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        if include_all:
            rows = conn.execute(text("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 200")).mappings()
        else:
            rows = conn.execute(
                text("SELECT * FROM jobs WHERE user_id = :user_id ORDER BY created_at DESC LIMIT 200"),
                {"user_id": user_id},
            ).mappings()
        return [_row(row) for row in rows]


def period_key(now: datetime | None = None) -> str:
    moment = now or utc_now()
    return moment.strftime("%Y-%m")


def reserve_search(
    engine: Engine,
    *,
    user_id: str,
    max_results: int,
    search_quota: int,
    result_quota: int,
) -> None:
    """Count one search if the monthly search and result quotas can cover it."""
    period = period_key()
    with engine.begin() as conn:
        _ensure_usage(conn, user_id, period)
        if conn.dialect.name == "postgresql":
            updated = conn.execute(
                text(
                    "UPDATE usage_months SET searches_used = searches_used + 1 "
                    "WHERE user_id = :user_id AND period = :period "
                    "AND searches_used + 1 <= :search_quota "
                    "AND results_used + :wanted <= :result_quota"
                ),
                {
                    "user_id": user_id,
                    "period": period,
                    "search_quota": search_quota,
                    "wanted": max_results,
                    "result_quota": result_quota,
                },
            )
        else:
            updated = conn.execute(
                text(
                    "UPDATE usage_months SET searches_used = searches_used + 1 "
                    "WHERE user_id = :user_id AND period = :period "
                    "AND searches_used + 1 <= :search_quota "
                    "AND results_used + :wanted <= :result_quota"
                ),
                {
                    "user_id": user_id,
                    "period": period,
                    "search_quota": search_quota,
                    "wanted": max_results,
                    "result_quota": result_quota,
                },
            )
        if updated.rowcount != 1:
            raise QuotaDenied("Monthly search or result quota is exhausted.")


class QuotaDenied(Exception):
    """The account cannot start another search."""


def record_results(engine: Engine, *, user_id: str, count: int) -> None:
    if count <= 0:
        return
    period = period_key()
    with engine.begin() as conn:
        _ensure_usage(conn, user_id, period)
        conn.execute(
            text(
                "UPDATE usage_months SET results_used = results_used + :count "
                "WHERE user_id = :user_id AND period = :period"
            ),
            {"count": count, "user_id": user_id, "period": period},
        )


def usage_for(engine: Engine, user_id: str) -> dict[str, int]:
    period = period_key()
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT searches_used, results_used FROM usage_months "
                "WHERE user_id = :user_id AND period = :period"
            ),
            {"user_id": user_id, "period": period},
        ).mappings().first()
    if row is None:
        return {"searches_used": 0, "results_used": 0}
    return {"searches_used": int(row["searches_used"]), "results_used": int(row["results_used"])}


def _ensure_usage(conn: Any, user_id: str, period: str) -> None:
    if conn.dialect.name == "postgresql":
        statement = (
            "INSERT INTO usage_months (user_id, period, searches_used, results_used) "
            "VALUES (:user_id, :period, 0, 0) ON CONFLICT (user_id, period) DO NOTHING"
        )
    else:
        statement = (
            "INSERT OR IGNORE INTO usage_months (user_id, period, searches_used, results_used) "
            "VALUES (:user_id, :period, 0, 0)"
        )
    conn.execute(text(statement), {"user_id": user_id, "period": period})


def set_subscription(
    engine: Engine,
    *,
    user_id: str | None = None,
    customer_id: str | None = None,
    subscription_id: str | None = None,
    status: str,
) -> bool:
    """Update billing fields. Match the user by id or by the Stripe customer id."""
    with engine.begin() as conn:
        if user_id:
            result = conn.execute(
                text(
                    "UPDATE users SET subscription_status = :status, plan = :plan, "
                    "stripe_customer_id = COALESCE(:customer_id, stripe_customer_id), "
                    "stripe_subscription_id = COALESCE(:subscription_id, stripe_subscription_id) "
                    "WHERE id = :user_id"
                ),
                {
                    "status": status,
                    "plan": "pro" if status in {"active", "trialing"} else "none",
                    "customer_id": customer_id,
                    "subscription_id": subscription_id,
                    "user_id": user_id,
                },
            )
            return result.rowcount == 1
        if customer_id:
            result = conn.execute(
                text(
                    "UPDATE users SET subscription_status = :status, plan = :plan, "
                    "stripe_subscription_id = COALESCE(:subscription_id, stripe_subscription_id) "
                    "WHERE stripe_customer_id = :customer_id"
                ),
                {
                    "status": status,
                    "plan": "pro" if status in {"active", "trialing"} else "none",
                    "subscription_id": subscription_id,
                    "customer_id": customer_id,
                },
            )
            return result.rowcount == 1
    return False


def set_trial_end(engine: Engine, user_id: str, when: datetime | None) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE users SET trial_ends_at = :trial_ends_at WHERE id = :id"),
            {"id": user_id, "trial_ends_at": iso(when)},
        )
