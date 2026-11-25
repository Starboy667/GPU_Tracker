"""Home page: every watch at a glance, plus the status of the checks."""
from __future__ import annotations

from fastapi import APIRouter, Request

from pricewatch.repos import alerts as alerts_repo
from pricewatch.repos import channels as channels_repo
from pricewatch.repos import runs as runs_repo
from pricewatch.repos import settings as settings_repo
from pricewatch.web import views
from pricewatch.web.deps import current_user, flash, read_form, redirect, render, services

router = APIRouter()


def scraper_label(svc, key: str) -> str:
    return svc.checker.site_label(key)


@router.get("/")
def dashboard(request: Request):
    user = current_user(request)
    svc = services(request)
    today = svc.clock().astimezone(svc.config.tz).date()
    with svc.db.connect() as conn:
        settings = settings_repo.load(conn)
        rows = views.watch_rows(conn, settings, today)
        recent_alerts = alerts_repo.recent(conn, limit=6)
        last_runs = runs_repo.latest(conn, 1)
        failing = [h for h in runs_repo.health_all(conn) if h.consecutive_failures >= 2]
        channel_count = len(channels_repo.list_all(conn, svc.box))
    return render(
        request,
        "dashboard.html",
        user=user,
        nav="watches",
        rows=rows,
        settings=settings,
        recent_alerts=recent_alerts,
        last_run=last_runs[0] if last_runs else None,
        next_due=svc.scheduler.next_due if svc.config.scheduler_enabled else None,
        failing=[(scraper_label(svc, h.site), h) for h in failing],
        channel_count=channel_count,
        refresh=svc.scheduler.is_busy,
    )


@router.post("/check")
async def check_now(request: Request):
    current_user(request)
    await read_form(request)
    services(request).scheduler.request_run()
    flash(request, "Check started. This page updates by itself while it runs.", "info")
    return redirect("/")


@router.get("/status")
def status(request: Request):
    user = current_user(request)
    svc = services(request)
    with svc.db.connect() as conn:
        runs = runs_repo.latest(conn, 25)
        health = runs_repo.health_all(conn)
        settings = settings_repo.load(conn)
    return render(
        request,
        "status.html",
        user=user,
        nav="status",
        runs=runs,
        health=[(scraper_label(svc, h.site), h) for h in health],
        settings=settings,
        next_due=svc.scheduler.next_due if svc.config.scheduler_enabled else None,
        scheduler_enabled=svc.config.scheduler_enabled,
        outbound=svc.outbound.current(),
        refresh=svc.scheduler.is_busy,
    )
