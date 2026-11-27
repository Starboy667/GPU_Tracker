"""Notification channels: where alerts are sent."""
from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from pricewatch import access, forms
from pricewatch.db import iso
from pricewatch.models import Channel, User
from pricewatch.notify import CHANNEL_TYPES
from pricewatch.notify.base import EVENT_KINDS, ChannelType, masked_config
from pricewatch.repos import channels as channels_repo
from pricewatch.web.deps import NotFound, current_user, flash, read_form, redirect, render, services

router = APIRouter()

EVENT_LABELS = {
    "deal": ("Deals", "A price at or under the alert threshold."),
    "restock": ("Back in stock", "For watches that ask for restock alerts."),
    "health": ("Scraper problems", "A shop keeps failing, or works again."),
}


def _load(request: Request, channel_id: int, user: User) -> Channel:
    """A channel of ``user``. Someone else's does not exist as far as they are concerned."""
    svc = services(request)
    with svc.db.connect() as conn:
        channel = channels_repo.get(conn, svc.box, channel_id)
    if channel is None or not access.owns_channel(user, channel):
        raise NotFound
    return channel


def _type_of(key: str) -> ChannelType:
    channel_type = CHANNEL_TYPES.get(key)
    if channel_type is None:
        raise NotFound
    return channel_type


def _form_page(request, user, channel_type, *, values, errors=None, channel=None, status_code=200):
    return render(
        request,
        "channel_form.html",
        status_code=status_code,
        user=user,
        nav="channels",
        channel=channel,
        channel_type=channel_type,
        values=values,
        errors=errors or {},
        event_labels=EVENT_LABELS,
        allow_private=services(request).config.allow_private_notify_urls,
    )


def _values(channel_type: ChannelType, channel: Channel | None) -> dict[str, object]:
    """Form values for a stored channel (secrets blanked) or for a new one (defaults)."""
    config = masked_config(channel_type, channel.config if channel and channel.readable else {})
    values: dict[str, object] = {f"cfg_{name}": value for name, value in config.items()}
    events = channel.events if channel else EVENT_KINDS
    values.update({f"event_{kind}": "on" for kind in events})
    values["name"] = channel.name if channel else ""
    values["enabled"] = "on" if channel is None or channel.enabled else ""
    return values


@router.get("/channels")
def list_channels(request: Request):
    user = current_user(request)
    svc = services(request)
    with svc.db.connect() as conn:
        channels = channels_repo.list_owned(conn, svc.box, user.id)
    return render(
        request, "channels.html", user=user, nav="channels", channels=channels,
        types=CHANNEL_TYPES, event_labels=EVENT_LABELS,
    )


@router.get("/channels/new")
def new_channel(request: Request, type: str = ""):
    user = current_user(request)
    if type not in CHANNEL_TYPES:
        return redirect("/channels#add")
    channel_type = CHANNEL_TYPES[type]
    return _form_page(request, user, channel_type, values=_values(channel_type, None))


@router.post("/channels")
async def create_channel(request: Request, type: str = ""):
    user = current_user(request)
    form = await read_form(request)
    channel_type = _type_of(type)
    svc = services(request)
    channel, errors = forms.parse_channel(
        form, channel_type, allow_private=svc.config.allow_private_notify_urls, owner_id=user.id
    )
    if channel is None:
        return _form_page(request, user, channel_type, values=dict(form), errors=errors, status_code=422)
    with svc.db.connect() as conn:
        saved = channels_repo.create(conn, svc.box, channel, iso(svc.clock()))
    flash(request, f"Channel “{saved.name}” added. Send a test to make sure it arrives.")
    return redirect("/channels")


@router.get("/channels/{channel_id}/edit")
def edit_channel(request: Request, channel_id: int):
    user = current_user(request)
    channel = _load(request, channel_id, user)
    channel_type = _type_of(channel.type)
    return _form_page(request, user, channel_type, values=_values(channel_type, channel), channel=channel)


@router.post("/channels/{channel_id}")
async def update_channel(request: Request, channel_id: int):
    user = current_user(request)
    form = await read_form(request)
    existing = _load(request, channel_id, user)
    channel_type = _type_of(existing.type)
    svc = services(request)
    channel, errors = forms.parse_channel(
        form, channel_type, existing=existing, allow_private=svc.config.allow_private_notify_urls
    )
    if channel is None:
        return _form_page(request, user, channel_type, values=dict(form), errors=errors, channel=existing,
                          status_code=422)
    with svc.db.connect() as conn:
        channels_repo.update(conn, svc.box, channel)
    flash(request, "Saved.")
    return redirect("/channels")


@router.post("/channels/{channel_id}/toggle")
async def toggle_channel(request: Request, channel_id: int):
    user = current_user(request)
    await read_form(request)
    channel = _load(request, channel_id, user)
    svc = services(request)
    with svc.db.connect() as conn:
        channels_repo.set_enabled(conn, channel.id, not channel.enabled)
    flash(request, f"“{channel.name}” is {'off' if channel.enabled else 'on'}.")
    return redirect("/channels")


@router.post("/channels/{channel_id}/test")
async def test_channel(request: Request, channel_id: int):
    user = current_user(request)
    await read_form(request)
    channel = _load(request, channel_id, user)
    svc = services(request)
    if not svc.test_slots.acquire(blocking=False):
        flash(request, "Other tests are still being sent. Try again in a moment.", "error")
        return redirect("/channels")
    try:
        error = await run_in_threadpool(svc.dispatcher.send_test, channel)
    finally:
        svc.test_slots.release()
    with svc.db.connect() as conn:
        channels_repo.record_result(conn, channel.id, error, iso(svc.clock()))
    if error is None:
        flash(request, f"Test sent through “{channel.name}”. Check that it arrived.")
    else:
        flash(request, f"“{channel.name}” could not deliver the test: {error}", "error")
    return redirect("/channels")


@router.post("/channels/{channel_id}/delete")
async def delete_channel(request: Request, channel_id: int):
    user = current_user(request)
    await read_form(request)
    channel = _load(request, channel_id, user)
    svc = services(request)
    with svc.db.connect() as conn:
        channels_repo.delete(conn, channel.id)
    flash(request, f"Channel “{channel.name}” deleted.")
    return redirect("/channels")
