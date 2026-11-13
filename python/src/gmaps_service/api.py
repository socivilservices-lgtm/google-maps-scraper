"""FastAPI service: submit a search, poll it, and download JSON or CSV."""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from gmaps_service.config import Config
from gmaps_service.errors import CaptchaDetectedError, ScraperError
from gmaps_service.jobs import JobNotFound, JobStore
from gmaps_service.logging_utils import get_logger
from gmaps_service.models import ScrapeResult
from gmaps_service.places_api import compose_query
from gmaps_service.scraper import GoogleMapsScraper

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
    logger = get_logger()
    store = JobStore(settings.output_dir)

    async def default_runner(**kwargs: object) -> ScrapeResult:
        scraper = GoogleMapsScraper(settings, logger)
        return await scraper.run(
            str(kwargs["query"]),
            str(kwargs.get("location") or ""),
            max_results=int(kwargs["max_results"]),
            backend=str(kwargs.get("backend") or "auto"),
        )

    execute = runner or default_runner

    def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
        expected = settings.service_api_key
        if not expected:
            return
        provided = x_api_key or ""
        if not hmac.compare_digest(provided.encode(), expected.encode()):
            raise HTTPException(status_code=401, detail="invalid api key")

    app = FastAPI(
        title="Google Maps business scraper",
        version="0.1.0",
        summary="Search local businesses and download JSON or CSV.",
    )
    app.state.config = settings
    app.state.store = store

    @app.get("/health")
    def health() -> dict[str, object]:
        return {
            "status": "ok",
            "backend_default": settings.resolve_backend(),
            "places_api_configured": settings.places_configured,
            "auth_required": bool(settings.service_api_key),
        }

    @app.post("/search", dependencies=[Depends(require_api_key)])
    async def search(body: SearchRequest, background: BackgroundTasks) -> dict[str, object]:
        try:
            settings.resolve_backend(body.backend)
        except ScraperError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        full_query = compose_query(body.query, body.location)
        job = store.create(
            query=full_query,
            location=body.location,
            max_results=body.max_results,
            backend=body.backend,
        )
        job_id = str(job["job_id"])
        if body.wait:
            await _run_job(store, settings, logger, execute, job_id, body)
        else:
            background.add_task(_run_job, store, settings, logger, execute, job_id, body)
        return store.public(job_id)

    @app.get("/jobs/{job_id}", dependencies=[Depends(require_api_key)])
    def job_status(job_id: str) -> dict[str, object]:
        try:
            return store.public(job_id)
        except JobNotFound as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc

    @app.get("/jobs/{job_id}/download", dependencies=[Depends(require_api_key)])
    def download(job_id: str, format: Literal["json", "csv"] = "json") -> FileResponse:
        try:
            path = store.file_for(job_id, format)
        except JobNotFound as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc
        if not path.is_file():
            raise HTTPException(status_code=404, detail="result file is not ready")
        media = "application/json" if format == "json" else "text/csv; charset=utf-8"
        return FileResponse(path, media_type=media, filename=path.name)

    if not settings.service_api_key:
        logger.warning(
            "GMAPS_SERVICE_API_KEY is unset; the HTTP API is open",
            error_type="config",
            url=None,
        )
    return app


async def _run_job(
    store: JobStore,
    settings: Config,
    logger: object,
    execute: Runner,
    job_id: str,
    body: SearchRequest,
) -> None:
    store.mark_running(job_id)
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
    except CaptchaDetectedError as exc:
        logger.error(str(exc), url=None, error_type="captcha", job_id=job_id)  # type: ignore[attr-defined]
        store.fail(job_id, str(exc), "captcha")
    except Exception as exc:
        logger.error(  # type: ignore[attr-defined]
            "job failed",
            url=None,
            error_type=type(exc).__name__,
            job_id=job_id,
            detail=str(exc),
        )
        store.fail(job_id, str(exc), type(exc).__name__)


# Importing the module builds an app from the environment for `uvicorn gmaps_service.api:app`.
app = create_app()
