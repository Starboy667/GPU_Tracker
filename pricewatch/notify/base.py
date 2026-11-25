"""Shared types for notification channels.

A channel type declares the fields it needs (rendered as a form in the UI) and a ``send`` function.
Stored channel configs are plain ``dict[str, str | bool]`` validated by ``clean_config``.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from pricewatch import netsafe

KIND_DEAL = "deal"
KIND_RESTOCK = "restock"
KIND_HEALTH = "health"
KIND_TEST = "test"
EVENT_KINDS = (KIND_DEAL, KIND_RESTOCK, KIND_HEALTH)

FIELD_TEXT = "text"
FIELD_URL = "url"
FIELD_NUMBER = "number"
FIELD_SELECT = "select"
FIELD_TEXTAREA = "textarea"
FIELD_BOOL = "bool"

MAX_FIELD_LENGTH = 4000


class NotifyError(RuntimeError):
    """Delivery failed. The message is stored and shown in the UI: it must never contain secrets
    (webhook URLs, bot tokens, passwords). Build it from status codes and short response excerpts,
    not from ``str(exception)`` of an HTTP library, which usually embeds the full URL."""


class ConfigError(ValueError):
    """Invalid channel configuration. ``errors`` maps a field name to a message for the user."""

    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


@dataclass(frozen=True)
class AlertItem:
    kind: str  # KIND_DEAL | KIND_RESTOCK | KIND_HEALTH | KIND_TEST
    watch: str  # watch name ("" for health/test)
    title: str  # product title, or a short headline for health/test
    site: str = ""
    url: str = ""
    price: float | None = None
    previous_price: float | None = None  # price we last alerted at for this product
    baseline: float | None = None  # usual price (median of recent daily lows)
    baseline_days: int | None = None  # how many days the baseline covers
    target_price: float | None = None  # the watch's fixed target, if any
    in_stock: bool | None = None
    detail: str = ""  # free text, used by health/test items


@dataclass(frozen=True)
class Message:
    title: str  # "🔥 3 deals", "Price Watch test"
    items: tuple[AlertItem, ...]
    app_url: str = ""  # public URL of this app, "" if unknown


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    kind: str = FIELD_TEXT
    required: bool = False
    secret: bool = False  # masked in the UI; leaving it blank on edit keeps the stored value
    help: str = ""
    placeholder: str = ""
    default: str = ""
    choices: tuple[tuple[str, str], ...] = ()  # (value, label) for FIELD_SELECT
    url_domains: tuple[str, ...] | None = None  # FIELD_URL: restrict to these domains
    # Where the message (and the credentials with it) is sent. Every FIELD_URL is one already;
    # set this on other fields that decide the destination, such as an SMTP host.
    destination: bool = False


@dataclass(frozen=True)
class ChannelType:
    key: str  # "telegram"
    label: str  # "Telegram"
    description: str  # one line for the "add a channel" picker
    fields: tuple[Field, ...]
    send: Callable[[dict, Message], None]  # raises NotifyError
    setup_help: str = ""  # short how-to shown above the form (plain text)
    docs_url: str = ""
    # Optional cross-field / format checks run after the per-field ones. Receives the cleaned
    # config and returns {field name: message}. Messages must not echo secret values.
    validate: Callable[[dict], dict[str, str]] | None = None


def clean_config(
    channel_type: ChannelType,
    raw: Mapping[str, object],
    *,
    existing: Mapping[str, object] | None = None,
    allow_private: bool = False,
) -> dict:
    """Validate submitted form values against the type's fields and return the config to store.

    A blank secret keeps the value from ``existing``. Raises ``ConfigError`` with per-field messages.
    """
    existing = existing or {}
    out: dict = {}
    errors: dict[str, str] = {}
    # Anyone who may edit a channel could otherwise point it at their own server, leave the
    # secret fields blank ("keep"), and receive the stored credentials with the next message.
    moved = bool(existing) and _destination_changed(channel_type, raw, existing)
    for f in channel_type.fields:
        if f.kind == FIELD_BOOL:
            out[f.name] = _as_bool(raw.get(f.name))
            continue
        value = str(raw.get(f.name) or "").strip()
        if not value and f.secret and existing.get(f.name):
            if moved:
                errors[f.name] = "enter it again: the destination of this channel changed"
                continue
            value = str(existing[f.name])
        if not value:
            value = f.default
        if not value:
            if f.required:
                errors[f.name] = "required"
            else:
                out[f.name] = ""
            continue
        if len(value) > MAX_FIELD_LENGTH:
            errors[f.name] = "too long"
            continue
        error = _check_value(f, value, allow_private)
        if error:
            errors[f.name] = error
        else:
            out[f.name] = value
    if not errors and channel_type.validate is not None:
        errors = dict(channel_type.validate(out))
    if errors:
        raise ConfigError(errors)
    return out


def _destination_changed(channel_type: ChannelType, raw: Mapping[str, object], existing: Mapping[str, object]) -> bool:
    for f in channel_type.fields:
        if f.kind != FIELD_URL and not f.destination:
            continue
        submitted = str(raw.get(f.name) or "").strip()
        stored = str(existing.get(f.name) or "")
        if f.secret and not submitted:
            continue  # blank secret = unchanged
        if (submitted or f.default) != (stored or f.default):
            return True
    return False


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "on", "yes")


def _check_value(f: Field, value: str, allow_private: bool) -> str | None:
    if f.kind == FIELD_URL:
        try:
            netsafe.check_url(
                value, allow_private=allow_private, allowed_domains=f.url_domains, allow_credentials=True
            )
        except netsafe.UnsafeURLError as exc:
            return str(exc)
    elif f.kind == FIELD_NUMBER:
        try:
            float(value)
        except ValueError:
            return "must be a number"
    elif f.kind == FIELD_SELECT:
        if value not in {choice for choice, _ in f.choices}:
            return "not one of the allowed values"
    return None


def masked_config(channel_type: ChannelType, config: Mapping[str, object]) -> dict:
    """Config safe to render back into a form: secrets are replaced by an empty string."""
    return {f.name: ("" if f.secret else config.get(f.name, f.default)) for f in channel_type.fields}
