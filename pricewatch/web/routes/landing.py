"""The public home page: what this server does, for visitors who are not signed in."""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import Response

from pricewatch.notify import CHANNEL_TYPES
from pricewatch.repos import settings as settings_repo
from pricewatch.scrapers import SCRAPERS
from pricewatch.web.deps import render, services

CATCH_ALL_CHANNEL = "apprise"  # "Other (Apprise)": a gateway to many services, described in a sentence


def landing_page(request: Request) -> Response:
    """Rendered without touching the session: looking at the home page sets no cookie.

    Everything on it is public knowledge about the software (which shops it reads, how often,
    which channels exist). Nothing comes from anyone's watches.
    """
    svc = services(request)
    with svc.db.connect() as conn:
        settings = settings_repo.load(conn)
    return render(
        request,
        "landing.html",
        stateless=True,
        shops=[scraper.label for scraper in SCRAPERS.values()],
        channels=[kind.label for key, kind in CHANNEL_TYPES.items() if key != CATCH_ALL_CHANNEL],
        settings=settings,
        access_request_url=svc.config.access_request_url,
    )
