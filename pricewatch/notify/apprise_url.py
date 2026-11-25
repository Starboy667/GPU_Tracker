"""Catch-all channel backed by Apprise: Matrix, Signal, Teams, WhatsApp and ~100 other services."""
from __future__ import annotations

import apprise

from pricewatch.notify import render
from pricewatch.notify.base import FIELD_TEXTAREA, KIND_HEALTH, ChannelType, Field, Message, NotifyError

MAX_URLS = 10
MAX_BODY = 8000
# Services that talk to the local desktop / machine, or that do not go through Python's resolver
# (raw sockets, native clients) and would therefore escape ``netsafe.guard``.
BLOCKED_SCHEMAS = frozenset({
    "dbus", "kde", "gnome", "glib", "gio", "qt", "macosx", "windows", "blink1",
    "syslog", "rsyslog", "growl", "mqtt", "mqtts", "xmpp", "xmpps", "amqp", "amqps",
    "irc", "ircs", "smpp", "smpps",
})


def split_urls(text: str) -> tuple[str, ...]:
    return tuple(line.strip() for line in (text or "").splitlines() if line.strip())


def _asset() -> apprise.AppriseAsset:
    # Sequential and without Apprise's own deadline: every send then runs in the calling thread,
    # inside the caller's ``netsafe.guard``. Each plugin still has its own socket timeouts.
    return apprise.AppriseAsset(async_mode=False, service_timeout=0)


def _line_error(number: int, url: str) -> str | None:
    """Why line ``number`` is unusable, or None. Never quotes the URL: it holds credentials."""
    schema, separator, _ = url.partition("://")
    if not separator or not schema:
        return f"line {number} is not an Apprise URL (expected something like tgram://…)"
    if schema.lower() in BLOCKED_SCHEMAS:
        return f"line {number} uses a service that cannot be used from this server"
    if apprise.Apprise.instantiate(url, asset=_asset(), suppress_exceptions=True) is None:
        return f"line {number} is not a valid Apprise URL (check the service's wiki page)"
    return None


def validate(config: dict) -> dict[str, str]:
    urls = split_urls(config.get("urls", ""))
    if len(urls) > MAX_URLS:
        return {"urls": f"at most {MAX_URLS} URLs per channel"}
    for number, url in enumerate(urls, 1):
        error = _line_error(number, url)
        if error:
            return {"urls": error}
    return {}


def send(config: dict, message: Message) -> None:
    urls = split_urls(config.get("urls", ""))
    if not urls:
        raise NotifyError("no Apprise URL configured")
    client = apprise.Apprise(asset=_asset())
    for number, url in enumerate(urls, 1):
        error = _line_error(number, url)
        if error or not client.add(url):
            raise NotifyError(error or f"line {number} is not a valid Apprise URL")
    only_health = bool(message.items) and all(item.kind == KIND_HEALTH for item in message.items)
    delivered = client.notify(
        title=message.title,
        body=render.truncate(render.as_markdown(message, with_title=False) or message.title, MAX_BODY),
        body_format=apprise.NotifyFormat.MARKDOWN,
        notify_type=apprise.NotifyType.WARNING if only_health else apprise.NotifyType.INFO,
    )
    if not delivered:
        raise NotifyError("Apprise reported a delivery failure")


TYPE = ChannelType(
    key="apprise",
    label="Other (Apprise)",
    description=(
        "Everything else: Matrix, Signal, Microsoft Teams, WhatsApp, Google Chat, Mattermost, "
        "Home Assistant, SMS gateways and about 100 more services, through Apprise URLs."
    ),
    fields=(
        Field(
            "urls",
            "Apprise URLs",
            kind=FIELD_TEXTAREA,
            required=True,
            secret=True,
            placeholder="matrixs://user:password@matrix.org/#room\nworkflows://…",
            help="One URL per line. They contain credentials: stored encrypted and never shown again.",
        ),
    ),
    send=send,
    validate=validate,
    setup_help=(
        "Find your service in the Apprise wiki and build its URL as described there, for example "
        "matrixs://user:password@server/#room or signal://host:port/from/to."
    ),
    docs_url="https://github.com/caronc/apprise/wiki",
)
