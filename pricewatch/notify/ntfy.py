"""ntfy push notifications (ntfy.sh or a self-hosted server), using JSON publishing."""
from __future__ import annotations

import re

from pricewatch.notify import http, render
from pricewatch.notify.base import (
    FIELD_SELECT,
    FIELD_URL,
    KIND_DEAL,
    KIND_HEALTH,
    KIND_RESTOCK,
    KIND_TEST,
    ChannelType,
    Field,
    Message,
)

DEFAULT_SERVER = "https://ntfy.sh"
MAX_MESSAGE_BYTES = 3800  # ntfy turns anything above 4096 bytes into an attachment
MAX_TITLE = 250
_TOPIC_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_TAGS = {KIND_DEAL: "fire", KIND_RESTOCK: "package", KIND_HEALTH: "warning", KIND_TEST: "white_check_mark"}


def validate(config: dict) -> dict[str, str]:
    if not _TOPIC_RE.fullmatch(config.get("topic", "")):
        return {"topic": "may only contain letters, digits, - and _ (64 characters at most)"}
    return {}


def fit_bytes(text: str, limit: int) -> str:
    """Truncate on line boundaries until the UTF-8 encoding fits in ``limit`` bytes."""
    size = limit
    while len(text.encode("utf-8")) > limit and size > 0:
        text = render.truncate(text, size)
        size -= 200
    return text


def build_payload(config: dict, message: Message) -> dict:
    payload: dict = {
        "topic": config["topic"],
        "title": render.clip(message.title, MAX_TITLE),
        "message": fit_bytes(render.as_markdown(message, with_title=False) or message.title, MAX_MESSAGE_BYTES),
        "markdown": True,
        "priority": int(config.get("priority") or 3),
        "tags": list(dict.fromkeys(_TAGS[item.kind] for item in message.items if item.kind in _TAGS)),
    }
    click = render.first_url(message)
    if click:
        payload["click"] = click
    return payload


def send(config: dict, message: Message) -> None:
    server = (config.get("server_url") or DEFAULT_SERVER).rstrip("/") + "/"
    token = config.get("token", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = http.request("POST", server, json=build_payload(config, message), headers=headers)
    http.ensure_ok(response, secrets=(token,))


TYPE = ChannelType(
    key="ntfy",
    label="ntfy",
    description="Push notifications to your phone or desktop with ntfy (ntfy.sh or self-hosted).",
    fields=(
        Field(
            "server_url",
            "Server",
            kind=FIELD_URL,
            required=True,
            default=DEFAULT_SERVER,
            help="Keep https://ntfy.sh unless you run your own server.",
        ),
        Field(
            "topic",
            "Topic",
            required=True,
            placeholder="pricewatch-7f3k2",
            help="On ntfy.sh topics are public: pick a name nobody can guess.",
        ),
        Field("token", "Access token", secret=True, help="Only for protected topics (starts with tk_)."),
        Field(
            "priority",
            "Priority",
            kind=FIELD_SELECT,
            required=True,
            default="3",
            choices=(("1", "1 — min"), ("2", "2 — low"), ("3", "3 — default"), ("4", "4 — high"), ("5", "5 — urgent")),
        ),
    ),
    send=send,
    validate=validate,
    setup_help="Install the ntfy app, subscribe to a topic of your choice, and enter the same topic here.",
    docs_url="https://docs.ntfy.sh/publish/",
)
