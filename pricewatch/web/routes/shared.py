"""Read-only pages opened with a share link. No account, no session, nothing to change."""
from __future__ import annotations

from fastapi import APIRouter, Request

from pricewatch import access
from pricewatch.repos import settings as settings_repo
from pricewatch.repos import watches as watches_repo
from pricewatch.web import views
from pricewatch.web.deps import render, services

router = APIRouter()

MAX_TOKEN_LENGTH = 100


@router.get("/s/{token}")
def shared_watch(request: Request, token: str):
    svc = services(request)
    today = svc.clock().astimezone(svc.config.tz).date()
    with svc.db.connect() as conn:
        watch = watches_repo.get_by_share_token(conn, token) if len(token) <= MAX_TOKEN_LENGTH else None
        if watch is None or not access.link_is_active(watch):
            # One answer for "never existed", "was replaced" and "was made private".
            return render(
                request, "error.html", status_code=404, stateless=True, title="This link does not work",
                message="It may have been replaced or turned off by the person who shared it.", public=True,
            )
        page = views.watch_page(conn, watch, settings_repo.load(conn), today, show_alert_rule=False)
    return render(request, "shared.html", stateless=True, mine=False, **page)
