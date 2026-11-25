"""Watches: create, edit, test, inspect, import and export."""
from __future__ import annotations

import sqlite3
from datetime import timedelta

import anyio
from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from pricewatch import deals, forms, portability
from pricewatch.db import iso
from pricewatch.models import Watch
from pricewatch.presets import DEFAULT_PRESET, PRESETS, PRESETS_BY_KEY
from pricewatch.repos import alerts as alerts_repo
from pricewatch.repos import channels as channels_repo
from pricewatch.repos import listings as listings_repo
from pricewatch.repos import settings as settings_repo
from pricewatch.repos import watches as watches_repo
from pricewatch.scrapers import SCRAPERS, validate_site_url
from pricewatch.web import charts, views
from pricewatch.web.deps import NotFound, current_user, flash, read_form, redirect, render, services

router = APIRouter()

PREVIEW_DEADLINE_SECONDS = 75
CHART_DAYS = 90
NAME_TAKEN = "Another watch already has this name."


def _load(conn, watch_id: int) -> Watch:
    watch = watches_repo.get(conn, watch_id)
    if watch is None:
        raise NotFound
    return watch


def _form_page(request, user, *, values, errors=None, watch=None, preset=None, preview=None, status_code=200):
    svc = services(request)
    with svc.db.connect() as conn:
        channels = channels_repo.list_all(conn, svc.box)
        settings = settings_repo.load(conn)
    return render(
        request,
        "watch_form.html",
        status_code=status_code,
        user=user,
        nav="watches",
        watch=watch,
        preset=preset,
        values=values,
        errors=errors or {},
        sites=list(SCRAPERS.values()),
        channels=channels,
        settings=settings,
        preview=preview,
        auto_match=deals.auto_match(str(values.get("name") or "")) if values.get("name") else "",
    )


def _parse(request, form, existing: Watch | None):
    svc = services(request)
    with svc.db.connect() as conn:
        channel_ids = [c.id for c in channels_repo.list_all(conn, svc.box)]
    return forms.parse_watch(
        form, site_keys=SCRAPERS, channel_ids=channel_ids, validate_site_url=validate_site_url, existing=existing
    )


# ------------------------------------------------------------------ create

@router.get("/watches/new")
def new_watch(request: Request, preset: str = ""):
    user = current_user(request)
    if preset not in PRESETS_BY_KEY:
        return render(request, "watch_presets.html", user=user, nav="watches", presets=PRESETS)
    chosen = PRESETS_BY_KEY[preset]
    return _form_page(request, user, values=views.preset_form_values(chosen), preset=chosen)


@router.post("/watches")
async def create_watch(request: Request):
    user = current_user(request)
    form = await read_form(request)
    preset = PRESETS_BY_KEY.get(str(form.get("preset") or ""), DEFAULT_PRESET)
    watch, errors = _parse(request, form, None)
    if watch is None:
        return _form_page(request, user, values=dict(form), errors=errors, preset=preset, status_code=422)
    svc = services(request)
    try:
        with svc.db.connect() as conn:
            saved = watches_repo.create(conn, watch, iso(svc.clock()))
    except sqlite3.IntegrityError:
        return _form_page(request, user, values=dict(form), errors={"name": NAME_TAKEN}, preset=preset, status_code=422)
    if saved.enabled:
        svc.scheduler.request_run((saved.id,))
        flash(request, f"Watch “{saved.name}” created. The first check is running now.")
    else:
        flash(request, f"Watch “{saved.name}” created, paused.")
    return redirect(f"/watches/{saved.id}")


# ------------------------------------------------------------------ preview (the Test button)

@router.post("/watches/preview")
async def preview_watch(request: Request):
    user = current_user(request)
    form = await read_form(request)
    svc = services(request)
    existing = None
    watch_id = str(form.get("watch_id") or "")
    if watch_id.isascii() and watch_id.isdigit():
        with svc.db.connect() as conn:
            existing = watches_repo.get(conn, int(watch_id))
    preset = PRESETS_BY_KEY.get(str(form.get("preset") or ""), DEFAULT_PRESET)
    watch, errors = _parse(request, form, existing)
    if watch is None:
        return _form_page(request, user, values=dict(form), errors=errors, watch=existing, preset=preset, status_code=422)

    if not svc.preview_slots.acquire(blocking=False):
        return _form_page(request, user, values=dict(form), watch=existing, preset=preset, status_code=429,
                          errors={"": "Other tests are still running. Try again in a minute."})

    def run_preview():
        try:
            return svc.checker.preview(watch)
        finally:
            svc.preview_slots.release()  # only when the scraping really ends, even if we stopped waiting

    scan = None
    with anyio.move_on_after(PREVIEW_DEADLINE_SECONDS):
        scan = await anyio.to_thread.run_sync(run_preview, abandon_on_cancel=True)
    today = svc.clock().astimezone(svc.config.tz).date()
    with svc.db.connect() as conn:
        settings = settings_repo.load(conn)
        lows = listings_repo.daily_lows(conn, existing.id) if existing else {}
    preview = {
        "scan": scan,
        "pricing": views.pricing_for(watch, lows, settings, today),
        "timed_out": scan is None,
    }
    return _form_page(request, user, values=dict(form), watch=existing, preset=preset, preview=preview)


# ------------------------------------------------------------------ import / export

@router.get("/watches/import")
def import_page(request: Request):
    user = current_user(request)
    return render(request, "watch_import.html", user=user, nav="watches", text="", problems=[])


@router.post("/watches/import")
async def import_watches(request: Request):
    user = current_user(request)
    form = await read_form(request)
    text = str(form.get("yaml") or "")
    svc = services(request)
    try:
        watches = portability.import_yaml(text, site_keys=SCRAPERS, validate_site_url=validate_site_url)
    except portability.ImportError_ as exc:
        return render(request, "watch_import.html", status_code=422, user=user, nav="watches", text=text,
                      problems=exc.problems)
    created, skipped = [], []
    now = iso(svc.clock())
    with svc.db.connect() as conn:
        for watch in watches:
            if watches_repo.get_by_name(conn, watch.name):
                skipped.append(watch.name)
            else:
                created.append(watches_repo.create(conn, watch, now))
    if created:
        svc.scheduler.request_run(tuple(w.id for w in created))
    message = f"Imported {len(created)} watch{'es' if len(created) != 1 else ''}."
    if skipped:
        message += f" Skipped {len(skipped)} that already exist: {', '.join(skipped[:8])}."
    flash(request, message, "ok" if created else "info")
    return redirect("/")


@router.get("/watches/export.yaml")
def export_watches(request: Request):
    current_user(request)
    svc = services(request)
    with svc.db.connect() as conn:
        text = portability.export_yaml(watches_repo.list_all(conn))
    return PlainTextResponse(
        text, media_type="application/yaml",
        headers={"content-disposition": 'attachment; filename="pricewatch-watches.yaml"'},
    )


# ------------------------------------------------------------------ one watch

@router.get("/watches/{watch_id}")
def watch_detail(request: Request, watch_id: int):
    user = current_user(request)
    svc = services(request)
    today = svc.clock().astimezone(svc.config.tz).date()
    with svc.db.connect() as conn:
        watch = _load(conn, watch_id)
        settings = settings_repo.load(conn)
        window = max(CHART_DAYS, settings.baseline_days + 1)
        lows = listings_repo.daily_lows(conn, watch.id, since=(today - timedelta(days=window)).isoformat())
        listings = listings_repo.for_watch(conn, watch.id)
        watch_alerts = alerts_repo.recent(conn, limit=10, watch_id=watch.id)
        channels = channels_repo.list_all(conn, svc.box)
    pricing = views.pricing_for(watch, lows, settings, today)
    chart_args = dict(
        today=today, days=CHART_DAYS,
        usual=pricing.baseline.price if pricing.baseline else None, alert=pricing.threshold,
    )
    available = [item for item in listings if item.in_stock is not False]
    return render(
        request,
        "watch_detail.html",
        user=user,
        nav="watches",
        watch=watch,
        settings=settings,
        pricing=pricing,
        listings=listings,
        deal_ids={item.id for item in listings if views.is_deal(item, pricing)},
        best=available[0] if available else None,
        lowest=min(lows.items(), key=lambda item: item[1]) if lows else None,
        lows=sorted(lows.items(), reverse=True),
        # One drawing per layout width, so text keeps a readable size at every screen size.
        charts=[
            (name, charts.line_chart(lows, width=width, height=height, **chart_args))
            for name, width, height in (("chart-wide", 1040, 300), ("chart-medium", 700, 270), ("chart-narrow", 360, 230))
        ],
        chart_days=CHART_DAYS,
        alerts=watch_alerts,
        site_labels={key: scraper.label for key, scraper in SCRAPERS.items()},
        channel_names=[c.name for c in channels if watch.channel_ids is None or c.id in watch.channel_ids],
        refresh=svc.scheduler.is_busy,
    )


@router.get("/watches/{watch_id}/edit")
def edit_watch(request: Request, watch_id: int):
    user = current_user(request)
    with services(request).db.connect() as conn:
        watch = _load(conn, watch_id)
    return _form_page(request, user, values=views.watch_form_values(watch), watch=watch)


@router.post("/watches/{watch_id}")
async def update_watch(request: Request, watch_id: int):
    user = current_user(request)
    form = await read_form(request)
    svc = services(request)
    with svc.db.connect() as conn:
        existing = _load(conn, watch_id)
    watch, errors = _parse(request, form, existing)
    if watch is None:
        return _form_page(request, user, values=dict(form), errors=errors, watch=existing, status_code=422)
    try:
        with svc.db.connect() as conn:
            watches_repo.update(conn, watch, iso(svc.clock()))
    except sqlite3.IntegrityError:
        return _form_page(request, user, values=dict(form), errors={"name": NAME_TAKEN}, watch=existing, status_code=422)
    if watch.enabled:
        svc.scheduler.request_run((watch.id,))
    flash(request, "Saved. Checking it again with the new settings." if watch.enabled else "Saved.")
    return redirect(f"/watches/{watch.id}")


@router.post("/watches/{watch_id}/toggle")
async def toggle_watch(request: Request, watch_id: int):
    current_user(request)
    await read_form(request)
    svc = services(request)
    with svc.db.connect() as conn:
        watch = _load(conn, watch_id)
        watches_repo.set_enabled(conn, watch.id, not watch.enabled, iso(svc.clock()))
    flash(request, f"“{watch.name}” is {'paused' if watch.enabled else 'active again'}.")
    return redirect(f"/watches/{watch.id}")


@router.post("/watches/{watch_id}/check")
async def check_watch(request: Request, watch_id: int):
    current_user(request)
    await read_form(request)
    svc = services(request)
    with svc.db.connect() as conn:
        watch = _load(conn, watch_id)
    svc.scheduler.request_run((watch.id,))
    flash(request, "Check started. This page updates by itself while it runs.", "info")
    return redirect(f"/watches/{watch.id}")


@router.post("/watches/{watch_id}/delete")
async def delete_watch(request: Request, watch_id: int):
    current_user(request)
    await read_form(request)
    svc = services(request)
    with svc.db.connect() as conn:
        watch = _load(conn, watch_id)
        watches_repo.delete(conn, watch.id)
    flash(request, f"“{watch.name}” and its price history were deleted.")
    return redirect("/")

