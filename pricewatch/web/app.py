"""FastAPI application factory."""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from pricewatch import security
from pricewatch.checker import Checker
from pricewatch.config import Config, ConfigError, load_config
from pricewatch.db import Database, iso, utcnow
from pricewatch.delivery import Dispatcher
from pricewatch.outbound import OutboundMonitor
from pricewatch.repos import users as users_repo
from pricewatch.scan import polite_pause
from pricewatch.scheduler import Scheduler
from pricewatch.web import views
from pricewatch.web.charts import fmt_money, money_parts
from pricewatch.web.deps import Forbidden, LoginRequired, NotFound, Services, redirect, render
from pricewatch.web.routes import account, alerts, auth, channels, dashboard, settings, shared, watches
from pricewatch.web.static_files import StaticAssets, static_version

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
SESSION_COOKIE = "pricewatch_session"
MAX_CONCURRENT_PREVIEWS = 2  # each one scrapes several shops in a worker thread
MAX_CONCURRENT_CHANNEL_TESTS = 2

SECURITY_HEADERS = {
    "content-security-policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
    ),
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "same-origin",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
    "cross-origin-opener-policy": "same-origin",
}
HSTS = "max-age=31536000; includeSubDomains"


class SecurityHeaders:
    """Adds response headers to every reply; pages are never cached by shared caches."""

    def __init__(self, app, *, hsts: bool):
        self.app = app
        self.headers = {**SECURITY_HEADERS, **({"strict-transport-security": HSTS} if hsts else {})}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        is_static = scope.get("path", "").startswith("/static/")

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                present = {name.lower() for name, _ in message.get("headers", [])}
                extra = dict(self.headers)
                if not is_static:
                    extra["cache-control"] = "no-store"
                message = {
                    **message,
                    "headers": [
                        *message.get("headers", []),
                        *((k.encode(), v.encode()) for k, v in extra.items() if k.encode() not in present),
                    ],
                }
            await send(message)

        await self.app(scope, receive, send_with_headers)


def build_templates(config: Config) -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    env = templates.env
    env.filters["money"] = fmt_money
    env.filters["money_parts"] = money_parts
    env.filters["ago"] = lambda value, now: views.ago(value, now)
    env.filters["localtime"] = lambda value: views.local_time(value, config.tz)
    env.filters["localday"] = lambda value: views.local_day(value, config.tz)
    env.filters["calendar_day"] = views.calendar_day
    env.filters["http_url"] = views.http_url
    env.filters["percent"] = views.percent
    env.globals["until"] = views.until
    version = static_version(HERE / "static")
    env.globals["static"] = lambda path: f"/static/v-{version}/{path}"
    return templates


def build_services(
    config: Config,
    *,
    scrapers=None,
    product_scraper=None,
    send=None,
    pause: Callable[[], None] = polite_pause,
    clock: Callable[[], datetime] = utcnow,
) -> Services:
    db = Database(config.db_path)
    box = security.SecretBox(config.secret_key)
    dispatcher = Dispatcher(db, box, config, send=send, clock=clock)
    checker = Checker(
        db, box, config, dispatcher,
        scrapers=scrapers, product_scraper=product_scraper, pause=pause, clock=clock,
    )
    return Services(
        config=config,
        db=db,
        box=box,
        dispatcher=dispatcher,
        checker=checker,
        scheduler=Scheduler(checker, db, clock=clock),
        limiter=security.RateLimiter(max_failures=5, window_seconds=900),
        account_limiter=security.RateLimiter(max_failures=50, window_seconds=900),
        preview_slots=threading.BoundedSemaphore(MAX_CONCURRENT_PREVIEWS),
        outbound=OutboundMonitor(config.outbound_check_url, clock=clock),
        test_slots=threading.BoundedSemaphore(MAX_CONCURRENT_CHANNEL_TESTS),
        templates=build_templates(config),
        clock=clock,
    )


def bootstrap_admin(svc: Services) -> None:
    """Create the first account from ADMIN_EMAIL / ADMIN_PASSWORD when no user exists yet."""
    config = svc.config
    with svc.db.connect() as conn:
        if users_repo.count(conn):
            return
        email = security.normalize_email(config.admin_email)
        if not email and not config.admin_password:
            log.warning("no account exists: set ADMIN_EMAIL and ADMIN_PASSWORD, or run `pricewatch create-user`")
            return
        if not security.is_valid_email(email):
            raise ConfigError("ADMIN_EMAIL is not a valid email address")
        problem = security.password_problem(config.admin_password)
        if problem:
            raise ConfigError(f"ADMIN_PASSWORD: {problem}")
        users_repo.create(conn, email, security.hash_password(config.admin_password), True, iso(svc.clock()))
        log.info("created the first administrator account")


def adopt_ownerless(svc: Services) -> None:
    """Give watches and channels that have no owner to the first administrator.

    Such rows only come from an upgrade made while no account existed (data imported from the
    shell). Left alone they would be checked but alert nobody, and their channels would show up
    on no one's page. A watch whose name the administrator already uses stays ownerless (any
    administrator can rename or reassign it).
    """
    with svc.db.connect() as conn:
        admin = users_repo.first_admin(conn)
        if admin is None:
            return
        watches = conn.execute("UPDATE OR IGNORE watches SET owner_id = ? WHERE owner_id IS NULL", (admin.id,)).rowcount
        channels = conn.execute("UPDATE channels SET owner_id = ? WHERE owner_id IS NULL", (admin.id,)).rowcount
    if watches or channels:
        log.info("gave %d ownerless watches and %d channels to the first administrator", watches, channels)


def create_app(config: Config | None = None, services: Services | None = None) -> FastAPI:
    config = config or (services.config if services else load_config())
    svc = services or build_services(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc.db.migrate()
        bootstrap_admin(svc)
        adopt_ownerless(svc)
        # The loop always runs so "Check now" works; SCHEDULER_ENABLED only gates automatic checks.
        svc.scheduler.start(scheduled=config.scheduler_enabled)
        try:
            yield
        finally:
            svc.scheduler.stop()

    app = FastAPI(title="Price Watch", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.services = svc
    app.add_middleware(
        SessionMiddleware,
        secret_key=config.secret_key,
        session_cookie=SESSION_COOKIE,
        max_age=config.session_days * 86400,
        same_site="lax",
        https_only=config.cookie_secure,
    )
    app.add_middleware(SecurityHeaders, hsts=config.cookie_secure)
    app.mount("/static", StaticAssets(HERE / "static"), name="static")

    for module in (auth, dashboard, watches, shared, channels, alerts, settings, account):
        app.include_router(module.router)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> JSONResponse:
        with svc.db.connect() as conn:
            conn.execute("SELECT 1").fetchone()
        return JSONResponse({"status": "ok"})

    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, exc: LoginRequired):
        if request.method == "GET" and request.url.path != "/":
            query = f"?{request.url.query}" if request.url.query else ""
            from urllib.parse import quote

            return redirect("/login?next=" + quote(request.url.path + query, safe=""))
        return redirect("/login")

    @app.exception_handler(Forbidden)
    async def forbidden(request: Request, exc: Forbidden):
        return render(request, "error.html", status_code=403, title="Not allowed", message=exc.message,
                      user=_user_or_none(request))

    @app.exception_handler(NotFound)
    async def not_found(request: Request, exc: NotFound):
        return _not_found_page(request)

    @app.exception_handler(404)
    async def http_not_found(request: Request, exc):
        return _not_found_page(request)

    @app.exception_handler(OverflowError)
    async def number_too_large(request: Request, exc: OverflowError):
        return _not_found_page(request)  # an id no database row can have

    @app.exception_handler(RequestValidationError)
    async def bad_address(request: Request, exc: RequestValidationError):
        return _not_found_page(request)  # e.g. /watches/abc: not a page that can exist

    return app


def _user_or_none(request: Request):
    from pricewatch.web.deps import current_user

    try:
        return current_user(request)
    except LoginRequired:
        return None


def _not_found_page(request: Request):
    return render(request, "error.html", status_code=404, title="Page not found",
                  message="This page does not exist, or the item was deleted.", user=_user_or_none(request))
