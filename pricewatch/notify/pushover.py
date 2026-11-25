"""Pushover push notifications."""
from __future__ import annotations

import re

import requests

from pricewatch.notify import http, render
from pricewatch.notify.base import FIELD_SELECT, ChannelType, Field, Message, NotifyError

API_URL = "https://api.pushover.net/1/messages.json"
MAX_TITLE = 250
MAX_MESSAGE = 1024
MAX_URL = 512
MAX_URL_TITLE = 100
_KEY_RE = re.compile(r"[A-Za-z0-9]{30}")
_DEVICE_RE = re.compile(r"[A-Za-z0-9_,-]{1,200}")


def validate(config: dict) -> dict[str, str]:
    errors: dict[str, str] = {}
    for name in ("app_token", "user_key"):
        if not _KEY_RE.fullmatch(config.get(name, "")):
            errors[name] = "must be 30 letters and digits"
    if config.get("device") and not _DEVICE_RE.fullmatch(config["device"]):
        errors["device"] = "may only contain letters, digits, - and _ (several devices: separate with commas)"
    return errors


def build_data(config: dict, message: Message) -> dict[str, str]:
    data = {
        "token": config["app_token"],
        "user": config["user_key"],
        "title": render.clip(message.title, MAX_TITLE),
        "priority": str(config.get("priority") or "0"),
    }
    body = render.join_fitting([render.item_html(item) for item in message.items], MAX_MESSAGE)
    if body:
        data = {**data, "message": body, "html": "1"}
    else:  # nothing fits as HTML without cutting a tag in half: fall back to plain text
        text = "\n\n".join(render.item_text(item) for item in message.items) or message.title
        data = {**data, "message": render.truncate(text, MAX_MESSAGE)}
    link = _link(message)
    if link:
        data = {**data, "url": link[0], "url_title": link[1]}
    if config.get("device"):
        data = {**data, "device": config["device"]}
    return data


def _link(message: Message) -> tuple[str, str] | None:
    for item in message.items:
        url = render.safe_url(item.url)
        if url and len(url) <= MAX_URL:
            return url, render.clip(render.item_headline(item), MAX_URL_TITLE)
    app_url = render.safe_url(message.app_url)
    if app_url and len(app_url) <= MAX_URL:
        return app_url, render.APP_LINK_LABEL
    return None


def _error(response: requests.Response, secrets: tuple[str, ...]) -> NotifyError:
    try:
        errors = response.json().get("errors") or []
    except (ValueError, AttributeError):
        errors = []
    if errors:
        detail = http.redact("; ".join(str(e) for e in errors), secrets)[: http.EXCERPT_LENGTH]
        return NotifyError(f"Pushover: {detail}")
    detail = http.excerpt(response, secrets)
    return NotifyError(f"HTTP {response.status_code}: {detail}" if detail else f"HTTP {response.status_code}")


def send(config: dict, message: Message) -> None:
    response = http.request("POST", API_URL, data=build_data(config, message))
    if not http.is_ok(response):
        raise _error(response, (config["app_token"], config["user_key"]))


TYPE = ChannelType(
    key="pushover",
    label="Pushover",
    description="Push notifications to iOS, Android and desktop through Pushover.",
    fields=(
        Field("app_token", "Application token", required=True, secret=True, help="Create an application on pushover.net."),
        Field("user_key", "User key", required=True, secret=True, help="Shown on your Pushover dashboard (or a group key)."),
        Field("device", "Device", help="Optional: only notify this device name."),
        Field(
            "priority",
            "Priority",
            kind=FIELD_SELECT,
            required=True,
            default="0",
            choices=(("-1", "Low (no sound)"), ("0", "Normal"), ("1", "High (bypasses quiet hours)")),
        ),
    ),
    send=send,
    validate=validate,
    setup_help=(
        "Sign in at pushover.net: your user key is on the dashboard. Then create an application "
        "(Your Applications → Create an Application) to get an API token."
    ),
    docs_url="https://pushover.net/api",
)
