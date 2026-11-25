"""Gotify (self-hosted push server)."""
from __future__ import annotations

from pricewatch.notify import http, render
from pricewatch.notify.base import FIELD_NUMBER, FIELD_URL, ChannelType, Field, Message

DEFAULT_PRIORITY = 5
MAX_TITLE = 250
MAX_MESSAGE = 8000


def validate(config: dict) -> dict[str, str]:
    try:
        priority = int(config.get("priority") or DEFAULT_PRIORITY)
    except ValueError:
        priority = -1
    if not 0 <= priority <= 10:
        return {"priority": "must be a whole number between 0 and 10"}
    return {}


def build_payload(config: dict, message: Message) -> dict:
    extras: dict = {"client::display": {"contentType": "text/markdown"}}
    click = render.first_url(message)
    if click:
        extras["client::notification"] = {"click": {"url": click}}
    return {
        "title": render.clip(message.title, MAX_TITLE),
        "message": render.truncate(render.as_markdown(message, with_title=False) or message.title, MAX_MESSAGE),
        "priority": int(config.get("priority") or DEFAULT_PRIORITY),
        "extras": extras,
    }


def send(config: dict, message: Message) -> None:
    token = config["token"]
    url = config["server_url"].rstrip("/") + "/message"
    response = http.request("POST", url, json=build_payload(config, message), headers={"X-Gotify-Key": token})
    http.ensure_ok(response, secrets=(token,))


TYPE = ChannelType(
    key="gotify",
    label="Gotify",
    description="Push notifications through your own Gotify server.",
    fields=(
        Field("server_url", "Server URL", kind=FIELD_URL, required=True, placeholder="https://gotify.example.com"),
        Field("token", "App token", required=True, secret=True, help="From Apps → Create application in Gotify."),
        Field(
            "priority",
            "Priority",
            kind=FIELD_NUMBER,
            required=True,
            default=str(DEFAULT_PRIORITY),
            help="0 to 10. The Android app makes a sound from 4 up.",
        ),
    ),
    send=send,
    validate=validate,
    setup_help="In the Gotify web UI, open Apps, create an application and copy its token.",
    docs_url="https://gotify.net/docs/pushmsg",
)
