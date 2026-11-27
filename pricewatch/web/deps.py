"""Request plumbing: services, authentication, CSRF, flashes, rendering."""
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from fastapi import Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.datastructures import FormData

from pricewatch import __version__, security
from pricewatch.checker import Checker
from pricewatch.config import Config
from pricewatch.db import Database
from pricewatch.delivery import Dispatcher
from pricewatch.models import User
from pricewatch.outbound import OutboundMonitor
from pricewatch.repos import users as users_repo
from pricewatch.scheduler import Scheduler
from pricewatch.security import RateLimiter, SecretBox

SESSION_USER = "uid"
SESSION_VERSION = "sv"
SESSION_CSRF = "csrf"
SESSION_FLASH = "flash"
MAX_FORM_FIELDS = 300
MAX_FLASHES = 3
MAX_FLASH_LENGTH = 300  # flashes travel in the session cookie, which has to stay small


@dataclass
class Services:
    config: Config
    db: Database
    box: SecretBox
    dispatcher: Dispatcher
    checker: Checker
    scheduler: Scheduler
    limiter: RateLimiter  # failed sign-ins per client address
    account_limiter: RateLimiter  # failed sign-ins per account, from anywhere
    templates: Jinja2Templates
    clock: Callable[[], datetime]
    preview_slots: threading.BoundedSemaphore
    outbound: OutboundMonitor
    test_slots: threading.BoundedSemaphore


class LoginRequired(Exception):
    """No valid session: send the visitor to the login page."""


class Forbidden(Exception):
    """Signed in, but not allowed (wrong role or bad CSRF token)."""

    def __init__(self, message: str = "You are not allowed to do that."):
        super().__init__(message)
        self.message = message


class NotFound(Exception):
    """The thing the URL points at does not exist (any more)."""


def services(request: Request) -> Services:
    return request.app.state.services


def current_user(request: Request) -> User:
    session = request.session
    user_id = session.get(SESSION_USER)
    if user_id is None:
        raise LoginRequired
    svc = services(request)
    with svc.db.connect() as conn:
        user = users_repo.get(conn, user_id)
    if user is None or user.session_version != session.get(SESSION_VERSION):
        session.clear()  # account deleted, or password changed elsewhere
        raise LoginRequired
    return user


def require_admin(request: Request) -> User:
    user = current_user(request)
    if not user.is_admin:
        raise Forbidden("Only an administrator can open this page.")
    return user


def sign_in(request: Request, user: User) -> None:
    """Start a fresh session for ``user`` (new CSRF token, nothing carried over)."""
    request.session.clear()
    request.session[SESSION_USER] = user.id
    request.session[SESSION_VERSION] = user.session_version
    request.session[SESSION_CSRF] = security.new_token()


def csrf_token(request: Request) -> str:
    token = request.session.get(SESSION_CSRF)
    if not token:
        token = security.new_token()
        request.session[SESSION_CSRF] = token
    return token


async def read_form(request: Request) -> FormData:
    """Parse the submitted form and check its CSRF token."""
    form = await request.form(max_fields=MAX_FORM_FIELDS)
    if not security.tokens_match(request.session.get(SESSION_CSRF), str(form.get("csrf_token") or "")):
        raise Forbidden("This form has expired. Go back, reload the page and try again.")
    return form


def flash(request: Request, message: str, kind: str = "ok") -> None:
    """Queue a one-time message for the next page. ``kind``: ok | error | info."""
    queued = [*request.session.get(SESSION_FLASH, []), [kind, message[:MAX_FLASH_LENGTH]]]
    request.session[SESSION_FLASH] = queued[-MAX_FLASHES:]


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def safe_next(target: str | None) -> str:
    """Only same-site paths are accepted as a post-login destination."""
    if target and target.startswith("/") and not target.startswith(("//", "/\\")):
        return target
    return "/"


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def render(
    request: Request,
    template: str,
    *,
    status_code: int = 200,
    user: User | None = None,
    stateless: bool = False,
    **context,
) -> Response:
    """Render a page. ``stateless`` is for pages served to people without an account (shared
    links): the session is not read or started, so no cookie is set."""
    svc = services(request)
    flashes = []
    if not stateless:
        flashes = request.session.pop(SESSION_FLASH, []) if SESSION_FLASH in request.session else []
    base = {
        "user": user,
        "csrf_token": "" if stateless else csrf_token(request),
        "flashes": flashes,
        "busy": svc.scheduler.is_busy,
        "app_version": __version__,
        "now": svc.clock(),
    }
    return svc.templates.TemplateResponse(request, template, {**base, **context}, status_code=status_code)
