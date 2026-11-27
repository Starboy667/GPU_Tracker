"""Home page: every watch at a glance, plus the status of the checks."""
from __future__ import annotations

from fastapi import APIRouter, Request

from pricewatch import access
from pricewatch.models import Run, SiteHealth, User
from pricewatch.repos import alerts as alerts_repo
from pricewatch.repos import channels as channels_repo
from pricewatch.repos import runs as runs_repo
from pricewatch.repos import settings as settings_repo
from pricewatch.repos import watches as watches_repo
from pricewatch.scan import PRODUCT_HEALTH_PREFIX
from pricewatch.web import views
from pricewatch.web.deps import current_user, flash, read_form, redirect, render, services

router = APIRouter()


def scraper_label(svc, key: str) -> str:
    return svc.checker.site_label(key)


def health_scope(user: User) -> str:
    """Which scraper alerts ``user`` sees: all of them, or only those about the shared shops."""
    return alerts_repo.HEALTH_ALL if user.is_admin else alerts_repo.HEALTH_SHOPS


def visible_health(user: User, health: list[SiteHealth]) -> list[SiteHealth]:
    """The hosts of product pages tell what people watch: only administrators see them."""
    return [h for h in health if user.is_admin or not h.site.startswith(PRODUCT_HEALTH_PREFIX)]


def visible_problems(user: User, run: Run, visible_ids: set[int]) -> list[tuple[str, str, str]]:
    """The problems of a check that concern watches ``user`` may see (and the run as a whole)."""
    kept = []
    for index, problem in enumerate(run.errors):
        watch_id = run.error_watch_ids[index] if index < len(run.error_watch_ids) else None
        if watch_id is None:
            # About the run itself, or recorded before problems kept their watch: the former
            # is for everyone, the latter (it names a watch) for administrators only.
            if not problem[0] or user.is_admin:
                kept.append(problem)
        elif watch_id in visible_ids:
            kept.append(problem)
    return kept


@router.get("/")
def dashboard(request: Request):
    user = current_user(request)
    svc = services(request)
    today = svc.clock().astimezone(svc.config.tz).date()
    with svc.db.connect() as conn:
        settings = settings_repo.load(conn)
        rows = views.watch_rows(conn, settings, today, user)
        recent_alerts = alerts_repo.recent_visible(
            conn, [row.watch.id for row in rows], health=health_scope(user), limit=6
        )
        last_runs = runs_repo.latest(conn, 1)
        failing = [h for h in visible_health(user, runs_repo.health_all(conn)) if h.consecutive_failures >= 2]
        channel_count = len(channels_repo.list_owned(conn, svc.box, user.id))
    return render(
        request,
        "dashboard.html",
        user=user,
        nav="watches",
        rows=[row for row in rows if row.mine],
        other_rows=[row for row in rows if not row.mine],
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
    user = current_user(request)
    await read_form(request)
    svc = services(request)
    if user.is_admin:
        svc.scheduler.request_run()
    else:
        with svc.db.connect() as conn:
            mine = tuple(w.id for w in watches_repo.list_all(conn, enabled_only=True) if w.owner_id == user.id)
        if not mine:
            flash(request, "You have no active watch to check.", "info")
            return redirect("/")
        svc.scheduler.request_run(mine)
    flash(request, "Check started. This page updates by itself while it runs.", "info")
    return redirect("/")


@router.get("/status")
def status(request: Request):
    user = current_user(request)
    svc = services(request)
    with svc.db.connect() as conn:
        runs = runs_repo.latest(conn, 25)
        health = visible_health(user, runs_repo.health_all(conn))
        settings = settings_repo.load(conn)
        visible_ids = {w.id for w in access.visible(user, watches_repo.list_all(conn))}
    return render(
        request,
        "status.html",
        user=user,
        nav="status",
        runs=[(run, visible_problems(user, run, visible_ids)) for run in runs],
        health=[(scraper_label(svc, h.site), h) for h in health],
        settings=settings,
        next_due=svc.scheduler.next_due if svc.config.scheduler_enabled else None,
        scheduler_enabled=svc.config.scheduler_enabled,
        outbound=svc.outbound.current(),
        refresh=svc.scheduler.is_busy,
    )
