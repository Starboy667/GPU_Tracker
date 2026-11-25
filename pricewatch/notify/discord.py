"""Discord incoming webhook: one embed per alert."""
from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timezone

import requests

from pricewatch.notify import http, render
from pricewatch.notify.base import (
    FIELD_URL,
    KIND_DEAL,
    KIND_HEALTH,
    KIND_RESTOCK,
    AlertItem,
    ChannelType,
    Field,
    Message,
)

USERNAME = "Price Watch"
MAX_EMBEDS = 10  # per message
MAX_MESSAGE_CHARS = 5500  # Discord allows 6000 across all embeds of a message
MAX_TITLE = 256
MAX_DESCRIPTION = 1500
MAX_CONTENT = 2000
MAX_RETRY_WAIT = 10.0
DEFAULT_RETRY_WAIT = 2.0

COLOR_IN_STOCK = 0x2ECC71
COLOR_OTHER = 0xF1C40F
COLOR_HEALTH = 0xE74C3C
COLOR_INFO = 0x3498DB
_STOCK_VALUES = {True: "✅ In stock", False: "❌ Out of stock", None: "❔ Unknown"}


def _description(item: AlertItem) -> str:
    if item.kind not in (KIND_DEAL, KIND_RESTOCK):
        return "\n".join(render.md_line(line) for line in render.item_lines(item))
    lines = [render.md_line(render.clip(item.title, render.MAX_TITLE))]
    if item.price is not None:
        context = render.price_context(item) if item.kind == KIND_DEAL else ""
        lines.append(f"**{render.fmt_eur(item.price)}** {context}".strip())
    if item.kind == KIND_DEAL and item.target_price is not None:
        lines.append(f"Target: {render.fmt_eur(item.target_price)}")
    if item.kind == KIND_DEAL and item.previous_price is not None:
        lines.append(f"Last alert: ~~{render.fmt_eur(item.previous_price)}~~")
    return "\n".join(lines)


def _color(item: AlertItem) -> int:
    if item.kind == KIND_HEALTH:
        return COLOR_HEALTH
    if item.kind in (KIND_DEAL, KIND_RESTOCK):
        return COLOR_IN_STOCK if item.in_stock else COLOR_OTHER
    return COLOR_INFO


def make_embed(item: AlertItem, timestamp: str) -> dict:
    embed: dict = {
        "title": render.clip(render.item_headline(item), MAX_TITLE),
        "description": render.truncate(_description(item), MAX_DESCRIPTION),
        "color": _color(item),
        "timestamp": timestamp,
    }
    if render.safe_url(item.url):
        embed["url"] = render.safe_url(item.url)
    if item.kind in (KIND_DEAL, KIND_RESTOCK):
        embed["fields"] = [{"name": "Stock", "value": _STOCK_VALUES[item.in_stock], "inline": True}]
    return embed


def _size(embed: dict) -> int:
    fields = sum(len(f["name"]) + len(f["value"]) for f in embed.get("fields", ()))
    return len(embed["title"]) + len(embed["description"]) + fields


def batches(embeds: list[dict]) -> list[list[dict]]:
    """Split embeds so each message respects Discord's count and total-size limits."""
    out: list[list[dict]] = []
    current: list[dict] = []
    used = 0
    for embed in embeds:
        size = _size(embed)
        if current and (len(current) >= MAX_EMBEDS or used + size > MAX_MESSAGE_CHARS):
            out.append(current)
            current, used = [], 0
        current = [*current, embed]
        used += size
    if current:
        out.append(current)
    return out


def _retry_after(response: requests.Response) -> float:
    try:
        wait = float(response.json().get("retry_after", DEFAULT_RETRY_WAIT))
    except (ValueError, TypeError, AttributeError):
        wait = DEFAULT_RETRY_WAIT
    return min(max(wait, 0.0), MAX_RETRY_WAIT)


def _post(url: str, payload: dict, sleep: Callable[[float], None]) -> None:
    response = http.request("POST", url, json=payload)
    if response.status_code == 429:  # rate limited: wait as told, once
        sleep(_retry_after(response))
        response = http.request("POST", url, json=payload)
    http.ensure_ok(response, secrets=http.url_secrets(url))


def send(config: dict, message: Message, *, sleep: Callable[[float], None] = time.sleep) -> None:
    url = config["webhook_url"]
    timestamp = datetime.now(timezone.utc).isoformat()
    groups = batches([make_embed(item, timestamp) for item in message.items]) or [[]]
    for index, group in enumerate(groups):
        payload: dict = {"username": USERNAME, "allowed_mentions": {"parse": []}}
        if index == 0:
            payload["content"] = render.truncate(message.title, MAX_CONTENT)
        if group:
            payload["embeds"] = group
        _post(url, payload, sleep)


TYPE = ChannelType(
    key="discord",
    label="Discord",
    description="Post alerts to a Discord channel through a webhook, one card per deal.",
    fields=(
        Field(
            "webhook_url",
            "Webhook URL",
            kind=FIELD_URL,
            required=True,
            secret=True,
            placeholder="https://discord.com/api/webhooks/…",
            help="Anyone holding this URL can post to the channel.",
            url_domains=("discord.com", "discordapp.com"),
        ),
    ),
    send=send,
    setup_help=(
        "In Discord, open the channel settings, then Integrations → Webhooks → New Webhook, "
        "and copy the webhook URL."
    ),
    docs_url="https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks",
)
