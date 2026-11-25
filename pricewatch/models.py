"""Domain objects. All frozen: change them with ``dataclasses.replace``."""
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

EMPTY_MAP: Mapping[str, str] = MappingProxyType({})

DELIVERY_PENDING = "pending"
DELIVERY_SENT = "sent"
DELIVERY_FAILED = "failed"

RUN_RUNNING = "running"
RUN_OK = "ok"
RUN_PARTIAL = "partial"  # finished, but some sites failed
RUN_FAILED = "failed"

TRIGGER_SCHEDULE = "schedule"
TRIGGER_MANUAL = "manual"


@dataclass(frozen=True)
class User:
    id: int
    email: str
    password_hash: str
    is_admin: bool
    session_version: int
    created_at: str


@dataclass(frozen=True)
class Settings:
    """Global tunables, editable in the UI. Watches can override the first three."""

    deal_drop_pct: float = 8.0  # a deal is at least this % under the usual price
    in_stock_only: bool = True  # only alert on listings that are (or may be) in stock
    check_interval_minutes: int = 30
    baseline_days: int = 14  # usual price = median of the daily lows over this many days
    baseline_min_days: int = 3  # days of history needed before a baseline exists
    renotify_drop_pct: float = 2.0  # alert again only if it drops this % further
    dealabs_max_age_days: int = 3  # ignore Dealabs posts older than this
    health_alert_after: int = 6  # consecutive failed runs before a scraper alert (0 = never)
    history_days: int = 180  # how long individual price points are kept


@dataclass(frozen=True)
class ProductURL:
    url: str
    selector: str = ""  # optional CSS selector for the price


@dataclass(frozen=True)
class Watch:
    name: str
    query: str
    match: str
    id: int | None = None
    exclude: str = ""
    min_price: float | None = None
    max_price: float | None = None  # fixed target: always alert at or below it
    deal_drop_pct: float | None = None  # None = use the global setting
    in_stock_only: bool | None = None  # None = use the global setting
    notify_restock: bool = False
    sites: tuple[str, ...] = ()
    site_urls: Mapping[str, str] = field(default_factory=lambda: EMPTY_MAP)
    product_urls: tuple[ProductURL, ...] = ()
    channel_ids: tuple[int, ...] | None = None  # None = every enabled channel
    enabled: bool = True
    created_at: str = ""
    updated_at: str = ""
    last_checked_at: str | None = None
    last_errors: tuple[tuple[str, str], ...] = ()  # (site, error) from the latest check


@dataclass(frozen=True)
class StoredListing:
    id: int
    watch_id: int
    url: str
    source: str
    site: str
    title: str
    price: float
    in_stock: bool | None
    active: bool
    first_seen: str
    last_seen: str
    notified_price: float | None
    notified_at: str | None


@dataclass(frozen=True)
class Channel:
    name: str
    type: str
    config: Mapping[str, object]
    id: int | None = None
    events: tuple[str, ...] = ("deal", "restock", "health")
    enabled: bool = True
    created_at: str = ""
    last_ok_at: str | None = None
    last_error: str | None = None
    last_error_at: str | None = None
    readable: bool = True  # False when the stored config could not be decrypted


@dataclass(frozen=True)
class Alert:
    kind: str
    watch_name: str = ""
    title: str = ""
    id: int | None = None
    watch_id: int | None = None
    site: str = ""
    url: str = ""
    price: float | None = None
    previous_price: float | None = None
    baseline: float | None = None
    baseline_days: int | None = None
    target_price: float | None = None
    in_stock: bool | None = None
    detail: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class Delivery:
    id: int
    alert_id: int
    channel_id: int
    status: str
    attempts: int
    last_error: str | None
    updated_at: str


@dataclass(frozen=True)
class Run:
    id: int
    started_at: str
    finished_at: str | None
    trigger: str
    status: str
    watches: int
    listings: int
    alerts: int
    errors: tuple[tuple[str, str, str], ...]  # (watch, site, error)


@dataclass(frozen=True)
class SiteHealth:
    site: str
    consecutive_failures: int
    last_ok_at: str | None
    last_error: str | None
    last_error_at: str | None
    alerted: bool
