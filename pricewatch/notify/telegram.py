"""Telegram bot: HTML messages through ``sendMessage``."""
from __future__ import annotations

import html
import re

import requests

from pricewatch.notify import http, render
from pricewatch.notify.base import ChannelType, Field, Message, NotifyError

API_BASE = "https://api.telegram.org"
MAX_MESSAGE = 4096
_TOKEN_RE = re.compile(r"\d{5,}:[A-Za-z0-9_-]{20,}")
_CHAT_RE = re.compile(r"-?\d+|@[A-Za-z][A-Za-z0-9_]{3,}")


def validate(config: dict) -> dict[str, str]:
    errors: dict[str, str] = {}
    if not _TOKEN_RE.fullmatch(config.get("bot_token", "")):
        errors["bot_token"] = "does not look like a bot token (digits, a colon, then letters and digits)"
    if not _CHAT_RE.fullmatch(config.get("chat_id", "")):
        errors["chat_id"] = "must be a numeric chat id (groups start with -) or @channelname"
    if config.get("topic_id") and not config["topic_id"].isdigit():
        errors["topic_id"] = "must be a number"
    return errors


def build_texts(message: Message) -> list[str]:
    """The message as one or more HTML texts, each within Telegram's size limit."""
    blocks = [f"<b>{html.escape(message.title)}</b>"]
    blocks += [render.item_html(item) for item in message.items]
    if render.safe_url(message.app_url):
        blocks.append(render.html_link(render.APP_LINK_LABEL, message.app_url))
    return render.chunk_blocks(blocks, MAX_MESSAGE)


def _error(response: requests.Response, token: str) -> NotifyError:
    description = ""
    try:
        description = str(response.json().get("description") or "")
    except (ValueError, AttributeError):
        pass
    detail = http.redact(render.clip(description, http.EXCERPT_LENGTH), (token,)) or http.excerpt(response, (token,))
    return NotifyError(f"Telegram: {detail}" if detail else f"HTTP {response.status_code}")


def _accepted(response: requests.Response) -> bool:
    """Only Telegram's own ``{"ok": true}`` counts: a 200 from a proxy or captive portal does not."""
    if not http.is_ok(response):
        return False
    try:
        return response.json().get("ok") is True
    except (ValueError, AttributeError):
        return False


def send(config: dict, message: Message) -> None:
    token = config["bot_token"]
    for text in build_texts(message):
        payload: dict = {
            "chat_id": config["chat_id"],
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if config.get("topic_id"):
            payload["message_thread_id"] = int(config["topic_id"])
        response = http.request("POST", f"{API_BASE}/bot{token}/sendMessage", json=payload)
        if not _accepted(response):
            raise _error(response, token)


TYPE = ChannelType(
    key="telegram",
    label="Telegram",
    description="Message a chat, group or channel through your own Telegram bot.",
    fields=(
        Field(
            "bot_token",
            "Bot token",
            required=True,
            secret=True,
            placeholder="123456789:AA…",
            help="Given by @BotFather when you create the bot.",
        ),
        Field(
            "chat_id",
            "Chat ID",
            required=True,
            placeholder="123456789, -1001234567890 or @mychannel",
            help="Your user id, a group id (negative number) or a public channel name.",
        ),
        Field(
            "topic_id",
            "Topic ID",
            help="Only for groups with topics: the message_thread_id of the topic to post in.",
        ),
    ),
    send=send,
    validate=validate,
    setup_help=(
        "Create a bot with @BotFather and copy its token, then send the bot a message (or add it to "
        "your group) so it is allowed to write to you. Your chat id is shown by @userinfobot, or in "
        "https://api.telegram.org/bot<token>/getUpdates after you have written to the bot."
    ),
    docs_url="https://core.telegram.org/bots/tutorial#obtain-your-bot-token",
)
