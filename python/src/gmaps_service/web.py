"""Server-rendered pages: landing, auth, dashboard, billing, and admin."""

from __future__ import annotations

import hmac
import secrets
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from gmaps_service.config import Config
from gmaps_service.saas import db
from gmaps_service.saas.auth import (
    AuthError,
    entitlement,
    issue_session,
    login,
    new_api_key,
    read_session,
    signup,
)
from gmaps_service.saas.billing import (
    PLAN_MAX_RESULTS_PER_SEARCH,
    PLAN_MONTHLY_RESULTS,
    PLAN_MONTHLY_SEARCHES,
    PLAN_NAME,
    PLAN_PRICE_USD,
    BillingDisabled,
    billing_enabled,
    billing_message,
    create_checkout,
    create_portal,
)
from gmaps_service.saas.ratelimit import RateLimiter

_PACKAGE = files("gmaps_service")


class LoginRequired(Exception):
    """The browser page needs a signed-in user."""
templates = Jinja2Templates(directory=str(Path(str(_PACKAGE / "templates"))))


def mount_web(app: FastAPI, config: Config, limiter: RateLimiter) -> None:
    static_dir = Path(str(_PACKAGE / "static"))
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    def engine() -> Any:
        return app.state.engine

    def render(request: Request, name: str, status: int = 200, **extra: Any) -> HTMLResponse:
        token = _csrf_token(request)
        response = templates.TemplateResponse(
            request,
            name,
            {
                "request": request,
                "csrf_token": token,
                "config": config,
                "plan_name": PLAN_NAME,
                "plan_price": PLAN_PRICE_USD,
                "plan_results": config.monthly_result_quota or PLAN_MONTHLY_RESULTS,
                "plan_searches": config.monthly_search_quota or PLAN_MONTHLY_SEARCHES,
                "plan_per_search": config.max_results_per_search or PLAN_MAX_RESULTS_PER_SEARCH,
                "billing_enabled": billing_enabled(config),
                "billing_message": billing_message(config),
                **extra,
            },
            status_code=status,
        )
        _set_csrf(response, config, token)
        return response

    def current_user(request: Request) -> dict[str, Any] | None:
        user_id = read_session(config, request.cookies.get("gmaps_session"))
        if not user_id:
            return None
        return db.get_user_by_id(engine(), user_id)

    def require_user(request: Request) -> dict[str, Any]:
        user = current_user(request)
        if user is None:
            raise LoginRequired()
        return user

    @app.exception_handler(LoginRequired)
    def need_login(_request: Request, _exc: LoginRequired) -> RedirectResponse:
        return RedirectResponse("/login", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    def landing(request: Request) -> HTMLResponse:
        return render(request, "landing.html", user=current_user(request))

    @app.get("/signup", response_class=HTMLResponse)
    def signup_form(request: Request, invite: str = "") -> HTMLResponse:
        return render(request, "signup.html", invite=invite, error="", user=None)

    @app.post("/signup")
    def signup_submit(
        request: Request,
        email: str = Form(""),
        password: str = Form(""),
        invite: str = Form(""),
        csrf_token: str = Form(""),
    ) -> HTMLResponse:
        _check_csrf(request, csrf_token)
        ip = _client_ip(request)
        if not limiter.allow(f"signup:{ip}", 5, 3600):
            return render(request, "signup.html", invite=invite, error="Too many sign-up attempts. Try again later.", user=None, status=429)
        try:
            user = signup(engine(), config, email=email, password=password, invite_code=invite.strip() or None)
        except AuthError as exc:
            return render(request, "signup.html", invite=invite, error=str(exc), user=None, status=400)
        response = RedirectResponse("/dashboard", status_code=303)
        _set_session(response, config, user["id"])
        return response

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request) -> HTMLResponse:
        return render(request, "login.html", error="", user=None)

    @app.post("/login")
    def login_submit(
        request: Request,
        email: str = Form(""),
        password: str = Form(""),
        csrf_token: str = Form(""),
    ) -> HTMLResponse:
        _check_csrf(request, csrf_token)
        ip = _client_ip(request)
        if not limiter.allow(f"login:{ip}", config.login_rate_limit, 900):
            return render(request, "login.html", error="Too many login attempts. Try again in a few minutes.", user=None, status=429)
        try:
            user = login(engine(), config, email=email, password=password)
        except AuthError as exc:
            return render(request, "login.html", error=str(exc), user=None, status=400)
        response = RedirectResponse("/dashboard", status_code=303)
        _set_session(response, config, user["id"])
        return response

    @app.post("/logout")
    def logout(request: Request, csrf_token: str = Form("")) -> RedirectResponse:
        _check_csrf(request, csrf_token)
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie("gmaps_session")
        return response

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard(request: Request) -> HTMLResponse:
        user = require_user(request)
        access = entitlement(user)
        jobs = db.list_jobs(engine(), user_id=user["id"], include_all=user["role"] == "admin")
        usage = db.usage_for(engine(), user["id"])
        return render(
            request,
            "dashboard.html",
            user=user,
            access=access,
            jobs=jobs,
            usage=usage,
            error=request.query_params.get("error", ""),
        )

    @app.post("/app/search")
    async def app_search(
        request: Request,
        background: BackgroundTasks,
        query: str = Form(""),
        location: str = Form(""),
        max_results: int = Form(20),
        backend: str = Form("auto"),
        csrf_token: str = Form(""),
    ) -> RedirectResponse:
        _check_csrf(request, csrf_token)
        user = require_user(request)
        from gmaps_service.api import start_search

        try:
            job_id = await start_search(
                app,
                user=user,
                query=query,
                location=location,
                max_results=max_results,
                backend=backend,
                source="session",
                background=background,
            )
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, str) else "Search was rejected."
            return RedirectResponse(f"/dashboard?error={quote(detail)}", status_code=303)
        return RedirectResponse(f"/app/jobs/{job_id}", status_code=303)

    @app.get("/app/jobs/{job_id}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: str) -> HTMLResponse:
        user = require_user(request)
        from gmaps_service.api import job_for_user

        try:
            job = job_for_user(app, user, job_id)
        except HTTPException as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        return render(request, "job.html", user=user, job=job)

    @app.get("/account", response_class=HTMLResponse)
    def account(request: Request) -> HTMLResponse:
        user = require_user(request)
        access = entitlement(user)
        usage = db.usage_for(engine(), user["id"])
        keys = db.list_api_keys(engine(), user["id"])
        return render(
            request,
            "account.html",
            user=user,
            access=access,
            usage=usage,
            keys=keys,
            fresh_key=request.query_params.get("fresh_key", ""),
            notice=request.query_params.get("notice", ""),
            error=request.query_params.get("error", ""),
        )

    @app.post("/account/api-keys")
    def create_key(request: Request, name: str = Form("default"), csrf_token: str = Form("")) -> RedirectResponse:
        _check_csrf(request, csrf_token)
        user = require_user(request)
        raw, prefix, digest = new_api_key()
        db.insert_api_key(engine(), user_id=user["id"], name=name.strip() or "default", prefix=prefix, key_hash=digest)
        return RedirectResponse(f"/account?fresh_key={quote(raw)}", status_code=303)

    @app.post("/account/api-keys/{key_id}/revoke")
    def revoke_key(request: Request, key_id: str, csrf_token: str = Form("")) -> RedirectResponse:
        _check_csrf(request, csrf_token)
        user = require_user(request)
        db.revoke_api_key(engine(), user_id=user["id"], key_id=key_id)
        return RedirectResponse("/account?notice=API+key+revoked", status_code=303)

    @app.post("/account/checkout")
    def checkout(request: Request, csrf_token: str = Form("")) -> RedirectResponse:
        _check_csrf(request, csrf_token)
        user = require_user(request)
        try:
            url = create_checkout(config, user)
        except BillingDisabled as exc:
            return RedirectResponse(f"/account?error={quote(str(exc))}", status_code=303)
        return RedirectResponse(url, status_code=303)

    @app.post("/account/portal")
    def portal(request: Request, csrf_token: str = Form("")) -> RedirectResponse:
        _check_csrf(request, csrf_token)
        user = require_user(request)
        try:
            url = create_portal(config, user)
        except BillingDisabled as exc:
            return RedirectResponse(f"/account?error={quote(str(exc))}", status_code=303)
        return RedirectResponse(url, status_code=303)

    @app.get("/admin", response_class=HTMLResponse)
    def admin(request: Request) -> HTMLResponse:
        user = require_user(request)
        if user.get("role") != "admin":
            raise HTTPException(status_code=403, detail="admin only")
        return render(
            request,
            "admin.html",
            user=user,
            users=db.list_users(engine()),
            invites=db.list_invites(engine()),
            error=request.query_params.get("error", ""),
        )

    @app.post("/admin/invites")
    def create_invite(request: Request, max_uses: int = Form(1), csrf_token: str = Form("")) -> RedirectResponse:
        _check_csrf(request, csrf_token)
        user = require_user(request)
        if user.get("role") != "admin":
            raise HTTPException(status_code=403, detail="admin only")
        bounded = min(max(max_uses, 1), 100)
        db.create_invite(engine(), created_by=user["id"], max_uses=bounded)
        return RedirectResponse("/admin", status_code=303)


def _csrf_token(request: Request) -> str:
    current = request.cookies.get("gmaps_csrf") or ""
    if len(current) >= 20:
        return current
    return secrets.token_urlsafe(32)


def _set_csrf(response: HTMLResponse, config: Config, token: str) -> None:
    response.set_cookie(
        "gmaps_csrf",
        token,
        httponly=True,
        samesite="lax",
        secure=config.cookie_secure,
        max_age=60 * 60 * 24 * 14,
        path="/",
    )


def _set_session(response: RedirectResponse, config: Config, user_id: str) -> None:
    response.set_cookie(
        "gmaps_session",
        issue_session(config, user_id),
        httponly=True,
        samesite="lax",
        secure=config.cookie_secure,
        max_age=60 * 60 * 24 * 14,
        path="/",
    )


def _check_csrf(request: Request, provided: str) -> None:
    cookie = request.cookies.get("gmaps_csrf") or ""
    if not cookie or not provided or not hmac.compare_digest(cookie, provided):
        raise HTTPException(status_code=403, detail="csrf check failed")


def _client_ip(request: Request) -> str:
    # Only trust X-Forwarded-For when the process is explicitly behind a proxy.
    # Otherwise a client can spoof the header and dodge the login limit.
    if request.app.state.config.trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()[:80]
    if request.client is None:
        return "unknown"
    return request.client.host
