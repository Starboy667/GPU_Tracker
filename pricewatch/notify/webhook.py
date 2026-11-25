"""Generic JSON webhook, optionally signed (n8n, Home Assistant, Zapier, your own endpoint)."""
from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import re
from datetime import datetime, timezone

from pricewatch.notify import http
from pricewatch.notify.base import (
    FIELD_SELECT,
    FIELD_TEXTAREA,
    FIELD_URL,
    ChannelType,
    Field,
    Message,
    NotifyError,
)

EVENT_NAME = "alerts"
SIGNATURE_HEADER = "X-PriceWatch-Signature"
MAX_HEADERS = 20
_HEADER_NAME_RE = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_IGNORED_HEADERS = frozenset({"host", "content-length", "transfer-encoding", "connection"})


class HeaderSyntaxError(ValueError):
    """A custom header line is malformed. The message names the line number, never its content."""


def parse_headers(text: str) -> dict[str, str]:
    """Parse ``Name: value`` lines. Hop-by-hop and framing headers are silently dropped."""
    headers: dict[str, str] = {}
    lines = [line for line in (text or "").split("\n") if line.strip()]
    if len(lines) > MAX_HEADERS:
        raise HeaderSyntaxError(f"at most {MAX_HEADERS} headers")
    for number, line in enumerate(lines, 1):
        name, separator, value = line.partition(":")
        name, value = name.strip(), value.strip()
        if not separator or not _HEADER_NAME_RE.fullmatch(name):
            raise HeaderSyntaxError(f"line {number} is not a 'Name: value' header")
        if _CONTROL_RE.search(value) or not value.isascii():
            raise HeaderSyntaxError(f"line {number}: the value may only contain printable ASCII characters")
        if name.lower() not in _IGNORED_HEADERS:
            headers[name] = value
    return headers


def validate(config: dict) -> dict[str, str]:
    try:
        parse_headers(config.get("headers", ""))
    except HeaderSyntaxError as exc:
        return {"headers": str(exc)}
    return {}


def build_body(message: Message) -> bytes:
    payload = {
        "event": EVENT_NAME,
        "title": message.title,
        "app_url": message.app_url,
        "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "items": [dataclasses.asdict(item) for item in message.items],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def send(config: dict, message: Message) -> None:
    try:
        custom = parse_headers(config.get("headers", ""))
    except HeaderSyntaxError:
        raise NotifyError("the custom headers are not valid") from None
    body = build_body(message)
    headers = {**custom, "Content-Type": "application/json; charset=utf-8"}
    secret = config.get("secret", "")
    if secret:
        headers[SIGNATURE_HEADER] = sign(body, secret)
    method = config.get("method") or "POST"
    response = http.request(method, config["url"], data=body, headers=headers)
    http.ensure_ok(response, secrets=(*http.url_secrets(config["url"]), secret, *custom.values()))


TYPE = ChannelType(
    key="webhook",
    label="Webhook",
    description="Send alerts as JSON to any URL: n8n, Home Assistant, Zapier, Make or your own service.",
    fields=(
        Field("url", "URL", kind=FIELD_URL, required=True, secret=True, placeholder="https://example.com/hooks/pricewatch"),
        Field(
            "method",
            "Method",
            kind=FIELD_SELECT,
            required=True,
            default="POST",
            choices=(("POST", "POST"), ("PUT", "PUT")),
        ),
        Field(
            "headers",
            "Extra headers",
            kind=FIELD_TEXTAREA,
            secret=True,
            placeholder="Authorization: Bearer …",
            help="Optional, one 'Name: value' per line. Stored encrypted and never shown again.",
        ),
        Field(
            "secret",
            "Signing secret",
            secret=True,
            help=(
                "Optional. When set, each request carries X-PriceWatch-Signature: sha256=<HMAC-SHA256 "
                "of the raw body, hex> so the receiver can check it really comes from here."
            ),
        ),
    ),
    send=send,
    validate=validate,
    setup_help=(
        'The body is JSON: {"event": "alerts", "title": …, "app_url": …, "sent_at": …, "items": [{"kind", '
        '"watch", "title", "site", "url", "price", "previous_price", "baseline", "baseline_days", '
        '"target_price", "in_stock", "detail"}]}. Any 2xx answer counts as delivered.'
    ),
)
