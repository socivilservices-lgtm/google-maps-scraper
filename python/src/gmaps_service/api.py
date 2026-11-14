"""FastAPI service: accounts, searches, and downloads."""

from __future__ import annotations

import hmac
import secrets
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

from gmaps_service.config import Config
from gmaps_service.errors import CaptchaDetectedError, ScraperError
from gmaps_service.jobs import JobNotFound, JobStore
from gmaps_service.logging_utils import get_logger
from gmaps_service.models import ScrapeResult
from gmaps_service.places_api import compose_query
from gmaps_service.saas import db
from gmaps_service.saas.auth import Actor, can_view_job, entitlement, keys_match, user_from_api_key
from gmaps_service.saas.billing import apply_event, billing_enabled, construct_event
from gmaps_service.saas.db import QuotaDenied
from gmaps_service.saas.ratelimit import RateLimiter
from gmaps_service.scraper import GoogleMapsScraper
from gmaps_service.web import mount_web

Runner = Callable[..., Awaitable[ScrapeResult]]


class SearchRequest(BaseModel):
    """Body for ``POST /search``."""

    query: str = Field(min_length=1, max_length=200)
    location: str = Field(default="", max_length=200)
    max_results: int = Field(default=20, ge=1, le=200)
    backend: Literal["auto", "playwright", "places"] = "auto"
    wait: bool = False

    @field_validator("query", "location")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("query")
    @classmethod
    def query_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query is empty")
        return value.strip()


def create_app(config: Config | None = None, runner: Runner | None = None) -> FastAPI:
    """Build the service. Pass ``runner`` in tests to avoid a live browser."""
    settings = config or Config.load()
    if not settings.database_url:
        settings.database_url = f"sqlite:///{settings.output_dir}/app.db"
    ephemeral_secret = not settings.secret_key
    if ephemeral_secret:
        settings.secret_key = secrets.token_urlsafe(32)
    logger = get_logger()
    store = JobStore(settings.output_dir)
    engine = db.get_engine(settings.database_url)
    limiter = RateLimiter()

    async def default_runner(**kwargs: object) -> ScrapeResult:
        scraper = GoogleMapsScraper(settings, logger)
        return await scraper.run(
            str(kwargs["query"]),
            str(kwargs.get("location") or ""),
            max_results=int(kwargs["max_results"]),
            backend=str(kwargs.get("backend") or "auto"),
        )

    execute = runner or default_runner
    app = FastAPI(
        title="Maps leads",
        version="0.2.0",
        summary="Search local businesses and download JSON or CSV.",
    )
    app.state.config = settings
    app.state.store = store
    app.state.engine = engine
    app.state.runner = execute
    app.state.logger = logger
    app.state.limiter = limiter

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'self'"
        )
        return response

    @app.get("/health")
    def health() -> dict[str, object]:
        return {
            "status": "ok",
            "backend_default": settings.resolve_backend(),
            "places_api_configured": settings.places_configured,
            "auth_required": True,
            "billing_enabled": billing_enabled(settings),
        }

    @app.post("/search")
    async def search(
        body: SearchRequest,
        background: BackgroundTasks,
        request: Request,
        x_api_key: str | None = Header(default=None),
    ) -> dict[str, object]:
        actor = _actor_from_request(settings, engine, request, x_api_key)
        _enforce_csrf(request, actor)
        job_id = await start_search(
            app,
            user=actor.user,
            query=body.query,
            location=body.location,
            max_results=body.max_results,
            backend=body.backend,
            source="api" if x_api_key else "session",
            actor=actor,
            wait=body.wait,
            background=background,
        )
        return store.public(job_id)

    @app.get("/jobs/{job_id}")
    def job_status(
        job_id: str,
        request: Request,
        x_api_key: str | None = Header(default=None),
    ) -> dict[str, object]:
        actor = _actor_from_request(settings, engine, request, x_api_key)
        return _authorized_job(store, engine, actor, job_id)

    @app.get("/jobs/{job_id}/download")
    def download(
        job_id: str,
        request: Request,
        format: Literal["json", "csv"] = "json",
        x_api_key: str | None = Header(default=None),
    ) -> FileResponse:
        actor = _actor_from_request(settings, engine, request, x_api_key)
        _authorized_job(store, engine, actor, job_id)
        try:
            path = store.file_for(job_id, format)
        except JobNotFound as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc
        if not path.is_file():
            raise HTTPException(status_code=404, detail="result file is not ready")
        media = "application/json" if format == "json" else "text/csv; charset=utf-8"
        return FileResponse(path, media_type=media, filename=path.name)

    @app.post("/billing/webhook")
    async def stripe_webhook(request: Request) -> JSONResponse:
        if not settings.stripe_webhook_secret or not settings.stripe_secret_key:
            return JSONResponse(
                status_code=503,
                content={"detail": "Billing is not configured. Set STRIPE_WEBHOOK_SECRET and STRIPE_SECRET_KEY."},
            )
        payload = await request.body()
        signature = request.headers.get("stripe-signature", "")
        try:
            event = construct_event(payload, signature, settings.stripe_webhook_secret)
        except Exception as exc:
            logger.error("stripe webhook rejected", url=str(request.url), error_type="stripe", detail=type(exc).__name__)
            raise HTTPException(status_code=400, detail="invalid webhook signature") from exc
        outcome = apply_event(engine, event)
        return JSONResponse({"received": True, "outcome": outcome})

    mount_web(app, settings, limiter)
    if not settings.service_api_key:
        logger.warning(
            "GMAPS_SERVICE_API_KEY is unset; API calls need a user session or per-user API key",
            error_type="config",
            url=None,
        )
    if ephemeral_secret:
        logger.warning("SECRET_KEY is unset; sessions will not survive a restart", error_type="config", url=None)
    return app


async def start_search(
    app: FastAPI,
    *,
    user: dict[str, Any] | None,
    query: str,
    location: str,
    max_results: int,
    backend: str,
    source: str,
    actor: Actor | None = None,
    wait: bool = False,
    background: BackgroundTasks | None = None,
) -> str:
    """Validate access and queue one search. Used by the JSON API and the dashboard form."""
    settings: Config = app.state.config
    store: JobStore = app.state.store
    engine = app.state.engine
    logger = app.state.logger
    limiter: RateLimiter = app.state.limiter
    if actor is None:
        actor = Actor(kind="user", user=user)
    if actor.kind != "service" and actor.user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    identity = actor.user_id or "service"
    if not limiter.allow(f"search:{identity}", settings.search_rate_limit, 3600):
        raise HTTPException(status_code=429, detail="Too many searches. Try again later.")
    try:
        settings.resolve_backend(backend)
    except ScraperError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    cleaned_query = query.strip()
    cleaned_location = location.strip()
    if not cleaned_query or len(cleaned_query) > 200 or len(cleaned_location) > 200:
        raise HTTPException(status_code=422, detail="query must be 1-200 characters")
    if backend not in {"auto", "playwright", "places"}:
        raise HTTPException(status_code=422, detail="unknown backend")
    wanted = max(1, min(int(max_results), 200))
    count_results = False
    if actor.counts_toward_quota:
        access = entitlement(actor.user)
        if not access.allowed:
            raise HTTPException(status_code=402, detail=access.reason)
        wanted = min(wanted, settings.max_results_per_search)
        try:
            db.reserve_search(
                engine,
                user_id=str(actor.user_id),
                max_results=wanted,
                search_quota=settings.monthly_search_quota,
                result_quota=settings.monthly_result_quota,
            )
        except QuotaDenied as exc:
            raise HTTPException(status_code=402, detail=str(exc)) from exc
        count_results = True
    full_query = compose_query(cleaned_query, cleaned_location)
    job = store.create(
        query=full_query,
        location=cleaned_location,
        max_results=wanted,
        backend=backend,
        user_id=actor.user_id,
    )
    job_id = str(job["job_id"])
    db.insert_job(
        engine,
        job_id=job_id,
        user_id=actor.user_id,
        query=full_query,
        location=cleaned_location,
        max_results=wanted,
        backend=backend,
    )
    body = SearchRequest(
        query=cleaned_query,
        location=cleaned_location,
        max_results=wanted,
        backend=backend,  # type: ignore[arg-type]
        wait=wait,
    )
    if wait:
        await _run_job(store, settings, logger, app.state.runner, job_id, body, actor.user_id if count_results else None)
    elif background is not None:
        background.add_task(
            _run_job,
            store,
            settings,
            logger,
            app.state.runner,
            job_id,
            body,
            actor.user_id if count_results else None,
        )
    else:
        import asyncio

        asyncio.create_task(
            _run_job(store, settings, logger, app.state.runner, job_id, body, actor.user_id if count_results else None)
        )
    logger.info("search queued", url=None, job_id=job_id, source=source)
    return job_id


def job_for_user(app: FastAPI, user: dict[str, Any], job_id: str) -> dict[str, object]:
    actor = Actor(kind="user", user=user)
    return _authorized_job(app.state.store, app.state.engine, actor, job_id)


def _authorized_job(store: JobStore, engine: Any, actor: Actor, job_id: str) -> dict[str, object]:
    record = _load_job(store, engine, job_id)
    owner = record.get("user_id")
    owner_id = str(owner) if owner else None
    if not can_view_job(actor, owner_id):
        raise HTTPException(status_code=404, detail="job not found")
    return record


def _load_job(store: JobStore, engine: Any, job_id: str) -> dict[str, object]:
    try:
        job = store.public(job_id)
        if job.get("user_id") is None:
            row = db.get_job(engine, job_id)
            if row is not None:
                job["user_id"] = row.get("user_id")
        return job
    except JobNotFound:
        row = db.get_job(engine, job_id)
        if row is None:
            raise HTTPException(status_code=404, detail="job not found") from None
        body: dict[str, object] = {
            "job_id": row["id"],
            "user_id": row.get("user_id"),
            "status": row["status"],
            "query": row["query"],
            "location": row.get("location") or "",
            "max_results": row["max_results"],
            "backend": row["backend"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "error": row.get("error"),
            "error_type": row.get("error_type"),
            "result": None,
        }
        path = store.file_for(str(row["id"]), "json")
        if path.is_file():
            import json

            body["result"] = json.loads(path.read_text(encoding="utf-8"))
            body["status"] = "completed"
            body["downloads"] = {
                "json": f"/jobs/{row['id']}/download?format=json",
                "csv": f"/jobs/{row['id']}/download?format=csv",
            }
        return body


def _actor_from_request(settings: Config, engine: Any, request: Request, x_api_key: str | None) -> Actor:
    header = (x_api_key or "").strip()
    if header and settings.service_api_key and keys_match(header, settings.service_api_key):
        return Actor(kind="service")
    if header:
        user = user_from_api_key(engine, header)
        if user is None:
            raise HTTPException(status_code=401, detail="invalid api key")
        return Actor(kind="user", user=user)
    from gmaps_service.saas.auth import read_session

    user_id = read_session(settings, request.cookies.get("gmaps_session"))
    if user_id:
        user = db.get_user_by_id(engine, user_id)
        if user is not None:
            return Actor(kind="user", user=user)
    raise HTTPException(status_code=401, detail="authentication required")


def _enforce_csrf(request: Request, actor: Actor) -> None:
    """Cookie-authenticated writes must echo the CSRF cookie. API keys do not."""
    if request.headers.get("x-api-key"):
        return
    if actor.kind != "user":
        return
    cookie = request.cookies.get("gmaps_csrf") or ""
    provided = request.headers.get("x-csrf-token") or ""
    if not cookie or not provided or not hmac.compare_digest(cookie, provided):
        raise HTTPException(status_code=403, detail="csrf check failed")


async def _run_job(
    store: JobStore,
    settings: Config,
    logger: object,
    execute: Runner,
    job_id: str,
    body: SearchRequest,
    quota_user_id: str | None,
) -> None:
    engine = db.get_engine(settings.database_url)
    store.mark_running(job_id)
    db.update_job(engine, job_id, status="running")
    try:
        result = await execute(
            query=body.query,
            location=body.location,
            max_results=body.max_results,
            backend=body.backend,
        )
        scraper = GoogleMapsScraper(settings)
        scraper.save_results(result, store.directory / job_id)
        store.complete(job_id, result)
        db.update_job(engine, job_id, status="completed", result_count=result.total_found)
        if quota_user_id:
            db.record_results(engine, user_id=quota_user_id, count=result.total_found)
    except CaptchaDetectedError as exc:
        logger.error(str(exc), url=None, error_type="captcha", job_id=job_id)  # type: ignore[attr-defined]
        store.fail(job_id, str(exc), "captcha")
        db.update_job(engine, job_id, status="failed", error=str(exc), error_type="captcha")
    except Exception as exc:
        logger.error(  # type: ignore[attr-defined]
            "job failed",
            url=None,
            error_type=type(exc).__name__,
            job_id=job_id,
            detail=str(exc),
        )
        store.fail(job_id, str(exc), type(exc).__name__)
        db.update_job(engine, job_id, status="failed", error=str(exc), error_type=type(exc).__name__)


# Importing the module builds an app from the environment for `uvicorn gmaps_service.api:app`.
app = create_app()
