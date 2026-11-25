"""Validation of submitted forms. Pure functions: (form values) -> (clean object | None, errors)."""
from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping

from pricewatch import deals, netsafe
from pricewatch.models import Channel, ProductURL, Settings, Watch
from pricewatch.notify.base import EVENT_KINDS, ChannelType, ConfigError, clean_config
from pricewatch.security import is_valid_email, normalize_email, password_problem

MAX_NAME_LENGTH = 100
MAX_QUERY_LENGTH = 200
MAX_PRODUCT_URLS = 20
MAX_URL_LENGTH = 2000
MAX_SELECTOR_LENGTH = 200
MAX_PRICE = 1_000_000
MIN_DROP_PCT = 0.5
MAX_DROP_PCT = 90

STOCK_DEFAULT = "default"
STOCK_REQUIRED = "in_stock"
STOCK_ANY = "any"
CHANNELS_ALL = "all"
CHANNELS_SELECTED = "selected"

Errors = dict[str, str]


def _text(form: Mapping[str, object], name: str) -> str:
    return str(form.get(name) or "").strip()


def _checked(form: Mapping[str, object], name: str) -> bool:
    return _text(form, name).lower() in ("1", "true", "on", "yes")


def parse_number(raw: str) -> float | None:
    """'1 079,95' or '1079.95' -> 1079.95. Raises ValueError when it is not a number."""
    cleaned = "".join(raw.split()).replace("€", "").replace(",", ".")  # split() also drops no-break spaces
    if not cleaned:
        return None
    value = float(cleaned)
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError("not a finite number")
    return value


def _optional_number(form: Mapping[str, object], name: str, errors: Errors, *, low: float, high: float) -> float | None:
    try:
        value = parse_number(_text(form, name))
    except ValueError:
        errors[name] = "Enter a number."
        return None
    if value is not None and not low <= value <= high:
        errors[name] = f"Must be between {low:g} and {high:g}."
        return None
    return value


def parse_product_urls(text: str) -> tuple[tuple[ProductURL, ...], str | None]:
    """One page per line, optionally followed by ' | css selector'. Returns (pages, error)."""
    pages: list[ProductURL] = []
    for number, line in enumerate((line.strip() for line in text.splitlines()), start=1):
        if not line:
            continue
        url, _, selector = (part.strip() for part in line.partition(" | "))
        if len(url) > MAX_URL_LENGTH or len(selector) > MAX_SELECTOR_LENGTH:
            return (), f"Line {number} is too long."
        try:
            url = netsafe.check_url(url)
        except netsafe.UnsafeURLError as exc:
            return (), f"Line {number}: {exc}."
        if all(page.url != url for page in pages):
            pages.append(ProductURL(url=url, selector=selector))
    if len(pages) > MAX_PRODUCT_URLS:
        return (), f"At most {MAX_PRODUCT_URLS} pages per watch."
    return tuple(pages), None


def format_product_urls(pages: Iterable[ProductURL]) -> str:
    return "\n".join(f"{p.url} | {p.selector}" if p.selector else p.url for p in pages)


def parse_watch(
    form: Mapping[str, object],
    *,
    site_keys: Iterable[str],
    channel_ids: Iterable[int],
    validate_site_url,
    existing: Watch | None = None,
) -> tuple[Watch | None, Errors]:
    errors: Errors = {}
    name = _text(form, "name")
    if not name:
        errors["name"] = "Give this watch a name."
    elif len(name) > MAX_NAME_LENGTH:
        errors["name"] = f"At most {MAX_NAME_LENGTH} characters."
    query = _text(form, "query") or name
    if len(query) > MAX_QUERY_LENGTH:
        errors["query"] = f"At most {MAX_QUERY_LENGTH} characters."

    match = _text(form, "match") or deals.auto_match(name)
    exclude = _text(form, "exclude")
    for field_name, pattern in (("match", match), ("exclude", exclude)):
        problem = deals.regex_problem(pattern) if pattern else None
        if problem:
            errors[field_name] = problem.capitalize() + "."

    min_price = _optional_number(form, "min_price", errors, low=0, high=MAX_PRICE)
    max_price = _optional_number(form, "max_price", errors, low=0, high=MAX_PRICE)
    if min_price is not None and max_price is not None and max_price < min_price:
        errors["max_price"] = "The target price is below the minimum price, so nothing could ever match."
    deal_drop_pct = _optional_number(form, "deal_drop_pct", errors, low=MIN_DROP_PCT, high=MAX_DROP_PCT)

    stock_rule = _text(form, "stock_rule") or STOCK_DEFAULT
    if stock_rule not in (STOCK_DEFAULT, STOCK_REQUIRED, STOCK_ANY):
        errors["stock_rule"] = "Pick one of the options."
    in_stock_only = {STOCK_DEFAULT: None, STOCK_REQUIRED: True, STOCK_ANY: False}.get(stock_rule)

    sites: list[str] = []
    site_urls: dict[str, str] = {}
    for key in site_keys:
        if not _checked(form, f"site_{key}"):
            continue
        sites.append(key)
        override = _text(form, f"url_{key}")
        if not override:
            continue
        try:
            site_urls[key] = validate_site_url(key, override)
        except netsafe.UnsafeURLError as exc:
            errors[f"url_{key}"] = str(exc).capitalize() + "."

    product_urls, problem = parse_product_urls(_text(form, "product_urls"))
    if problem:
        errors["product_urls"] = problem
    elif not sites and not product_urls:
        errors["sites"] = "Pick at least one shop to search, or add a product page below."

    selected: tuple[int, ...] | None = None
    if (_text(form, "channels_mode") or CHANNELS_ALL) == CHANNELS_SELECTED:
        selected = tuple(cid for cid in channel_ids if _checked(form, f"channel_{cid}"))

    if errors:
        return None, errors
    base = existing or Watch(name=name, query=query, match=match)
    return dataclasses.replace(
        base,
        name=name,
        query=query,
        match=match,
        exclude=exclude,
        min_price=min_price,
        max_price=max_price,
        deal_drop_pct=deal_drop_pct,
        in_stock_only=in_stock_only,
        notify_restock=_checked(form, "notify_restock"),
        sites=tuple(sites),
        site_urls=site_urls,
        product_urls=product_urls,
        channel_ids=selected,
        enabled=_checked(form, "enabled"),
    ), {}


_SETTING_RANGES: Mapping[str, tuple[float, float]] = {
    "deal_drop_pct": (MIN_DROP_PCT, MAX_DROP_PCT),
    "check_interval_minutes": (10, 1440),
    "baseline_days": (2, 120),
    "baseline_min_days": (1, 120),
    "renotify_drop_pct": (0, 50),
    "dealabs_max_age_days": (1, 30),
    "health_alert_after": (0, 100),
    "history_days": (7, 3650),
}


def parse_settings(form: Mapping[str, object]) -> tuple[Settings | None, Errors]:
    errors: Errors = {}
    values: dict[str, object] = {"in_stock_only": _checked(form, "in_stock_only")}
    for f in dataclasses.fields(Settings):
        if f.name not in _SETTING_RANGES:
            continue
        low, high = _SETTING_RANGES[f.name]
        try:
            value = parse_number(_text(form, f.name))
        except ValueError:
            value = None
        if value is None:
            errors[f.name] = "Enter a number."
        elif isinstance(f.default, int) and value != int(value):
            errors[f.name] = "Enter a whole number."
        elif not low <= value <= high:
            errors[f.name] = f"Must be between {low:g} and {high:g}."
        else:
            values[f.name] = int(value) if isinstance(f.default, int) else float(value)
    if not errors and values["baseline_min_days"] > values["baseline_days"]:
        errors["baseline_min_days"] = "Cannot be more than the number of days the usual price is computed over."
    if errors:
        return None, errors
    return Settings(**values), {}


def parse_channel(
    form: Mapping[str, object],
    channel_type: ChannelType,
    *,
    existing: Channel | None = None,
    allow_private: bool = False,
) -> tuple[Channel | None, Errors]:
    errors: Errors = {}
    name = _text(form, "name") or channel_type.label
    if len(name) > MAX_NAME_LENGTH:
        errors["name"] = f"At most {MAX_NAME_LENGTH} characters."
    events = tuple(kind for kind in EVENT_KINDS if _checked(form, f"event_{kind}"))
    if not events:
        errors["events"] = "Pick at least one kind of alert, or disable the channel instead."
    config: dict = {}
    try:
        config = clean_config(
            channel_type,
            {f.name: form.get(f"cfg_{f.name}") for f in channel_type.fields},
            existing=existing.config if existing and existing.readable else None,
            allow_private=allow_private,
        )
    except ConfigError as exc:
        errors.update({f"cfg_{name}": message[:1].upper() + message[1:] + "." for name, message in exc.errors.items()})
    if errors:
        return None, errors
    base = existing or Channel(name=name, type=channel_type.key, config=config)
    return dataclasses.replace(
        base, name=name, config=config, events=events, enabled=_checked(form, "enabled"), readable=True
    ), {}


def parse_new_user(form: Mapping[str, object]) -> tuple[tuple[str, str, bool] | None, Errors]:
    """Returns (email, password, is_admin)."""
    errors: Errors = {}
    email = normalize_email(_text(form, "email"))
    password = str(form.get("password") or "")
    if not is_valid_email(email):
        errors["email"] = "Enter a valid email address."
    problem = password_problem(password)
    if problem:
        errors["password"] = problem
    if errors:
        return None, errors
    return (email, password, _checked(form, "is_admin")), {}


def parse_password_change(form: Mapping[str, object]) -> tuple[tuple[str, str] | None, Errors]:
    """Returns (current password, new password)."""
    errors: Errors = {}
    current = str(form.get("current_password") or "")
    new = str(form.get("new_password") or "")
    if not current:
        errors["current_password"] = "Enter your current password."
    problem = password_problem(new)
    if problem:
        errors["new_password"] = problem
    elif new != str(form.get("confirm_password") or ""):
        errors["confirm_password"] = "The two passwords do not match."
    elif new == current:
        errors["new_password"] = "Choose a password different from the current one."
    if errors:
        return None, errors
    return (current, new), {}
