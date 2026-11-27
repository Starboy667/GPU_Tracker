"""Watches: create, edit, test, inspect, share, import and export.

Every route that touches an existing watch goes through ``_load``, which answers "not found" for a
watch the user may not see and "not allowed" for one they may see but not change.
"""
from __future__ import annotations

import dataclasses
import sqlite3

import anyio
from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from pricewatch import access, deals, forms, portability, security
from pricewatch.db import iso
from pricewatch.models import VISIBILITY_PUBLIC, User, Watch
from pricewatch.presets import DEFAULT_PRESET, PRESETS, PRESETS_BY_KEY
from pricewatch.repos import alerts as alerts_repo
from pricewatch.repos import channels as channels_repo
from pricewatch.repos import listings as listings_repo
from pricewatch.repos import settings as settings_repo
from pricewatch.repos import users as users_repo
from pricewatch.repos import watches as watches_repo
from pricewatch.scrapers import SCRAPERS, validate_site_url
from pricewatch.web import views
from pricewatch.web.deps import Forbidden, NotFound, current_user, flash, read_form, redirect, render, services

router = APIRouter()

PREVIEW_DEADLINE_SECONDS = 75
NAME_TAKEN = "You already have a watch with this name."
OWNER_ONLY = "Only the owner of this watch can change it."


def _load(conn, watch_id: int, user: User, *, to_change: bool = False) -> Watch:
    watch = watches_repo.get(conn, watch_id)
    if watch is None or not access.can_view(user, watch):
        raise NotFound  # same answer whether it does not exist or is not theirs to see
    if to_change and not access.owns(user, watch):
        raise Forbidden(OWNER_ONLY)
    return watch


def _may_reassign(user: User, watch: Watch) -> bool:
    """Only an administrator moves a watch to another account, and only one they could already
    manage or everyone can see: their own, an ownerless one, or a public one.

    Members cannot hand a watch to someone: its alerts would reach that person's channels
    without their say.
    """
    return user.is_admin and (access.owns(user, watch) or watch.visibility == VISIBILITY_PUBLIC)


def _free_name(conn, owner_id: int, name: str) -> str:
    """``name``, or ``name (2)``, ``name (3)``... if that account already uses it. Renaming
    quietly, rather than refusing, keeps the answer from telling what the account privately has."""
    candidate, number = name, 1
    while watches_repo.get_by_name(conn, owner_id, candidate) is not None:
        number += 1
        candidate = f"{name[:forms.MAX_NAME_LENGTH - 6]} ({number})"
    return candidate


def _form_page(request, user, *, values, errors=None, watch=None, preset=None, preview=None, status_code=200):
    svc = services(request)
    with svc.db.connect() as conn:
        channels = channels_repo.list_owned(conn, svc.box, user.id)
        settings = settings_repo.load(conn)
        others = [u for u in users_repo.list_all(conn) if u.id != user.id]
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
        others=others,
        settings=settings,
        preview=preview,
        auto_match=deals.auto_match(str(values.get("name") or "")) if values.get("name") else "",
    )


def _parse(request, form, existing: Watch | None, user: User):
    svc = services(request)
    with svc.db.connect() as conn:
        channel_ids = [c.id for c in channels_repo.list_owned(conn, svc.box, user.id)]
        other_ids = [u.id for u in users_repo.list_all(conn) if u.id != user.id]
    return forms.parse_watch(
        form,
        site_keys=SCRAPERS,
        channel_ids=channel_ids,
        validate_site_url=validate_site_url,
        existing=existing,
        owner_id=user.id,
        shareable_user_ids=other_ids,
    )


def _drop_followers_without_access(conn, watch: Watch) -> None:
    for user_id in watches_repo.followers(conn, watch.id):
        if not access.may_follow(user_id, watch):
            watches_repo.unfollow(conn, watch.id, user_id)


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
    watch, errors = _parse(request, form, None, user)
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
    if watch_id.isascii() and watch_id.isdigit() and len(watch_id) <= 12:
        with svc.db.connect() as conn:
            candidate = watches_repo.get(conn, int(watch_id))
        if candidate is not None and access.owns(user, candidate):
            existing = candidate  # anything else is tested as a new watch: no peek at someone's history
    preset = PRESETS_BY_KEY.get(str(form.get("preset") or ""), DEFAULT_PRESET)
    watch, errors = _parse(request, form, existing, user)
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
            if watches_repo.get_by_name(conn, user.id, watch.name):
                skipped.append(watch.name)
            else:
                created.append(watches_repo.create(conn, dataclasses.replace(watch, owner_id=user.id), now))
    if created:
        svc.scheduler.request_run(tuple(w.id for w in created))
    message = f"Imported {len(created)} watch{'es' if len(created) != 1 else ''}."
    if skipped:
        message += f" Skipped {len(skipped)} you already have: {', '.join(skipped[:8])}."
    flash(request, message, "ok" if created else "info")
    return redirect("/")


@router.get("/watches/export.yaml")
def export_watches(request: Request):
    user = current_user(request)
    svc = services(request)
    with svc.db.connect() as conn:
        mine = [w for w in watches_repo.list_all(conn) if w.owner_id == user.id]
    return PlainTextResponse(
        portability.export_yaml(mine), media_type="application/yaml",
        headers={"content-disposition": 'attachment; filename="pricewatch-watches.yaml"'},
    )


# ------------------------------------------------------------------ one watch

@router.get("/watches/{watch_id}")
def watch_detail(request: Request, watch_id: int):
    user = current_user(request)
    svc = services(request)
    today = svc.clock().astimezone(svc.config.tz).date()
    with svc.db.connect() as conn:
        watch = _load(conn, watch_id, user)
        mine = access.owns(user, watch)
        page = views.watch_page(conn, watch, settings_repo.load(conn), today)
        watch_alerts = alerts_repo.recent(conn, limit=10, watch_id=watch.id)
        my_channels = channels_repo.list_owned(conn, svc.box, user.id)
        everyone = users_repo.list_all(conn)
        following = watch.id in watches_repo.followed_ids(conn, user.id)
    emails = {u.id: u.email for u in everyone}
    if mine:
        receiving = [c.name for c in my_channels if c.enabled and (watch.channel_ids is None or c.id in watch.channel_ids)]
    else:
        receiving = [c.name for c in my_channels if c.enabled] if following else []
    return render(
        request,
        "watch_detail.html",
        user=user,
        nav="watches",
        **page,
        mine=mine,
        owner_email=emails.get(watch.owner_id, ""),
        shared_emails=[emails[uid] for uid in watch.shared_with if uid in emails],
        following=following,
        may_reassign=_may_reassign(user, watch),
        accounts=[u for u in everyone if u.id != watch.owner_id],
        share_url=_share_url(request, watch) if mine and access.link_is_active(watch) else "",
        alerts=watch_alerts,
        site_labels={key: scraper.label for key, scraper in SCRAPERS.items()},
        channel_names=receiving,
        refresh=svc.scheduler.is_busy,
    )


def _share_url(request: Request, watch: Watch) -> str:
    base = services(request).config.base_url or str(request.base_url).rstrip("/")
    return f"{base}/s/{watch.share_token}"


@router.get("/watches/{watch_id}/edit")
def edit_watch(request: Request, watch_id: int):
    user = current_user(request)
    with services(request).db.connect() as conn:
        watch = _load(conn, watch_id, user, to_change=True)
    return _form_page(request, user, values=views.watch_form_values(watch), watch=watch)


@router.post("/watches/{watch_id}")
async def update_watch(request: Request, watch_id: int):
    user = current_user(request)
    form = await read_form(request)
    svc = services(request)
    with svc.db.connect() as conn:
        existing = _load(conn, watch_id, user, to_change=True)
    watch, errors = _parse(request, form, existing, user)
    if watch is None:
        return _form_page(request, user, values=dict(form), errors=errors, watch=existing, status_code=422)
    try:
        with svc.db.connect() as conn:
            saved = watches_repo.update(conn, watch, iso(svc.clock()))
            _drop_followers_without_access(conn, saved)
    except sqlite3.IntegrityError:
        return _form_page(request, user, values=dict(form), errors={"name": NAME_TAKEN}, watch=existing, status_code=422)
    if watch.enabled:
        svc.scheduler.request_run((watch.id,))
    flash(request, "Saved. Checking it again with the new settings." if watch.enabled else "Saved.")
    return redirect(f"/watches/{watch.id}")


@router.post("/watches/{watch_id}/toggle")
async def toggle_watch(request: Request, watch_id: int):
    user = current_user(request)
    await read_form(request)
    svc = services(request)
    with svc.db.connect() as conn:
        watch = _load(conn, watch_id, user, to_change=True)
        watches_repo.set_enabled(conn, watch.id, not watch.enabled, iso(svc.clock()))
    flash(request, f"“{watch.name}” is {'paused' if watch.enabled else 'active again'}.")
    return redirect(f"/watches/{watch.id}")


@router.post("/watches/{watch_id}/check")
async def check_watch(request: Request, watch_id: int):
    user = current_user(request)
    await read_form(request)
    svc = services(request)
    with svc.db.connect() as conn:
        watch = _load(conn, watch_id, user, to_change=True)
    svc.scheduler.request_run((watch.id,))
    flash(request, "Check started. This page updates by itself while it runs.", "info")
    return redirect(f"/watches/{watch.id}")


@router.post("/watches/{watch_id}/delete")
async def delete_watch(request: Request, watch_id: int):
    user = current_user(request)
    await read_form(request)
    svc = services(request)
    with svc.db.connect() as conn:
        watch = _load(conn, watch_id, user, to_change=True)
        watches_repo.delete(conn, watch.id)
    flash(request, f"“{watch.name}” and its price history were deleted.")
    return redirect("/")


# ------------------------------------------------------------------ sharing

@router.post("/watches/{watch_id}/share-link")
async def renew_share_link(request: Request, watch_id: int):
    """Replace the link by a new one: the old address stops working at once."""
    user = current_user(request)
    await read_form(request)
    svc = services(request)
    with svc.db.connect() as conn:
        watch = _load(conn, watch_id, user, to_change=True)
        if not access.link_is_active(watch):
            raise NotFound
        watches_repo.set_share_token(conn, watch.id, security.new_token(), iso(svc.clock()))
    flash(request, "New link created. The previous one no longer works.")
    return redirect(f"/watches/{watch.id}")


@router.post("/watches/{watch_id}/follow")
async def follow_watch(request: Request, watch_id: int):
    """Receive this watch's alerts on your own channels, or stop (``following=off``)."""
    user = current_user(request)
    form = await read_form(request)
    svc = services(request)
    with svc.db.connect() as conn:
        watch = _load(conn, watch_id, user)
        if access.owns(user, watch):
            raise Forbidden("This is your own watch: choose its channels in its settings.")
        if str(form.get("following") or "") == "off":
            watches_repo.unfollow(conn, watch.id, user.id)
            flash(request, f"You no longer receive the alerts of “{watch.name}”.")
        else:
            watches_repo.follow(conn, watch.id, user.id)
            has_channel = any(c.enabled for c in channels_repo.list_owned(conn, svc.box, user.id))
            if has_channel:
                flash(request, f"You will receive the alerts of “{watch.name}” on your channels.")
            else:
                flash(request, f"You follow “{watch.name}”, but you have no channel yet: add one to receive its alerts.", "info")
    return redirect(f"/watches/{watch.id}")


@router.post("/watches/{watch_id}/owner")
async def change_owner(request: Request, watch_id: int):
    user = current_user(request)
    form = await read_form(request)
    svc = services(request)
    target_id = str(form.get("owner") or "")
    with svc.db.connect() as conn:
        watch = _load(conn, watch_id, user)
        if not _may_reassign(user, watch):
            raise Forbidden(OWNER_ONLY)
        known = target_id.isascii() and target_id.isdigit() and len(target_id) <= 12
        target = users_repo.get(conn, int(target_id)) if known else None
        if target is None or target.id == watch.owner_id:
            flash(request, "Pick the account that should own this watch.", "error")
            return redirect(f"/watches/{watch.id}")
        # The channels picked on the watch were the previous owner's: the new owner starts with all of theirs.
        moved = dataclasses.replace(
            watch,
            name=_free_name(conn, target.id, watch.name),
            owner_id=target.id,
            channel_ids=None,
            shared_with=tuple(uid for uid in watch.shared_with if uid != target.id),
        )
        saved = watches_repo.update(conn, moved, iso(svc.clock()))
        _drop_followers_without_access(conn, saved)  # the new owner included
        still_visible = access.can_view(user, saved)
    flash(request, f"“{saved.name}” now belongs to {target.email}.")
    return redirect(f"/watches/{watch.id}" if still_visible else "/")
