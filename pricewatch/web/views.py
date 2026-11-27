"""View models and display helpers shared by the routes and templates."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pricewatch import access, deals
from pricewatch.db import parse_iso
from pricewatch.forms import (
    CHANNELS_ALL,
    CHANNELS_SELECTED,
    STOCK_ANY,
    STOCK_DEFAULT,
    STOCK_REQUIRED,
    format_product_urls,
)
from pricewatch.models import VISIBILITY_PRIVATE, Settings, StoredListing, User, Watch
from pricewatch.presets import Preset
from pricewatch.repos import listings as listings_repo
from pricewatch.repos import users as users_repo
from pricewatch.repos import watches as watches_repo
from pricewatch.web.charts import MONTHS, NBSP, Sparkline, line_chart, sparkline

STATE_DEAL = "deal"
STATE_PAUSED = "paused"
STATE_WAITING = "waiting"  # never checked yet
STATE_EMPTY = "empty"  # checked, nothing found
STATE_TRACKING = "tracking"

SPARK_DAYS = 30
CHART_DAYS = 90


# --------------------------------------------------------------------------- formatting

def ago(value: str | None, now: datetime) -> str:
    moment = parse_iso(value)
    if moment is None:
        return "never"
    seconds = max((now - moment).total_seconds(), 0)
    if seconds < 90:
        return "just now"
    if seconds < 3600 * 1.5:
        return f"{round(seconds / 60)} min ago"
    if seconds < 86400 * 1.5:
        return f"{round(seconds / 3600)} h ago"
    return f"{round(seconds / 86400)} days ago"


def until(moment: datetime | None, now: datetime) -> str:
    if moment is None:
        return ""
    seconds = (moment - now).total_seconds()
    if seconds < 60:
        return "in under a minute"
    if seconds < 3600 * 1.5:
        return f"in {round(seconds / 60)} min"
    return f"in {round(seconds / 3600)} h"


def local_time(value: str | None, tz: ZoneInfo) -> str:
    moment = parse_iso(value)
    if moment is None:
        return ""
    here = moment.astimezone(tz)
    return f"{here.day} {MONTHS[here.month - 1]} {here.year}, {here:%H:%M}"


def local_day(value: str | None, tz: ZoneInfo) -> str:
    moment = parse_iso(value)
    if moment is None:
        return ""
    here = moment.astimezone(tz)
    return f"{here.day} {MONTHS[here.month - 1]} {here.year}"


def calendar_day(day: str | None) -> str:
    """'2026-11-25' -> '25 Nov 2026'. For values that already are local calendar days."""
    try:
        parsed = date.fromisoformat(day or "")
    except ValueError:
        return ""
    return f"{parsed.day} {MONTHS[parsed.month - 1]} {parsed.year}"


def http_url(url: str | None) -> str:
    """The URL if it is plain http(s), else ''. Scraped links are never trusted in an href."""
    try:
        return url if url and urlsplit(url).scheme in ("http", "https") else ""
    except ValueError:
        return ""


def percent(delta: float | None) -> str:
    if delta is None:
        return ""
    sign = "+" if delta > 0 else ("−" if delta < 0 else "")
    return f"{sign}{abs(delta):.0f}{NBSP}%"


def number_input(value: float | None) -> str:
    """A number for an <input>: no trailing zeros, dot decimal."""
    if value is None:
        return ""
    return f"{value:g}"


# --------------------------------------------------------------------------- pricing of a watch

@dataclass(frozen=True)
class Pricing:
    baseline: deals.Baseline | None
    threshold: float | None
    drop_pct: float
    in_stock_only: bool
    days_until_baseline: int  # 0 once a usual price exists


def pricing_for(watch: Watch, lows: Mapping[str, float], settings: Settings, today: date) -> Pricing:
    baseline = deals.baseline(lows, today, settings.baseline_days, settings.baseline_min_days)
    drop_pct = settings.deal_drop_pct if watch.deal_drop_pct is None else watch.deal_drop_pct
    start = (today - timedelta(days=settings.baseline_days)).isoformat()
    known = sum(1 for day in lows if start <= day < today.isoformat())
    return Pricing(
        baseline=baseline,
        threshold=deals.deal_threshold(watch.max_price, baseline, drop_pct),
        drop_pct=drop_pct,
        in_stock_only=settings.in_stock_only if watch.in_stock_only is None else watch.in_stock_only,
        days_until_baseline=0 if baseline else max(settings.baseline_min_days - known, 1),
    )


def is_deal(listing: StoredListing, pricing: Pricing) -> bool:
    if pricing.threshold is None or listing.price > pricing.threshold:
        return False
    return listing.in_stock is not False or not pricing.in_stock_only


@dataclass(frozen=True)
class WatchRow:
    watch: Watch
    state: str
    best: StoredListing | None
    pricing: Pricing
    delta_pct: float | None  # best price vs usual price
    listing_count: int
    spark: Sparkline | None
    mine: bool = True
    owner: str = ""  # who shares it, for watches that are not the viewer's
    following: bool = False


def watch_rows(conn, settings: Settings, today: date, viewer: User) -> list[WatchRow]:
    """One row per watch ``viewer`` may see, deals first."""
    since = (today - timedelta(days=max(settings.baseline_days, SPARK_DAYS) + 1)).isoformat()
    lows_by_watch = listings_repo.daily_lows_by_watch(conn, since=since)
    cheapest = listings_repo.cheapest_by_watch(conn)
    counts = listings_repo.counts_by_watch(conn)
    emails = {u.id: u.email for u in users_repo.list_all(conn)}
    followed = watches_repo.followed_ids(conn, viewer.id)
    rows = []
    for watch in access.visible(viewer, watches_repo.list_all(conn)):
        lows = lows_by_watch.get(watch.id, {})
        pricing = pricing_for(watch, lows, settings, today)
        best = cheapest.get(watch.id)
        if not watch.enabled:
            state = STATE_PAUSED
        elif watch.last_checked_at is None:
            state = STATE_WAITING
        elif best is None:
            state = STATE_EMPTY
        elif is_deal(best, pricing):
            state = STATE_DEAL
        else:
            state = STATE_TRACKING
        delta = None
        if best is not None and pricing.baseline is not None:
            delta = (best.price / pricing.baseline.price - 1) * 100
        rows.append(WatchRow(
            watch=watch,
            state=state,
            best=best,
            pricing=pricing,
            delta_pct=delta,
            listing_count=counts.get(watch.id, 0),
            spark=sparkline(lows, today=today, days=SPARK_DAYS),
            mine=access.owns(viewer, watch),
            owner=emails.get(watch.owner_id, ""),
            following=watch.id in followed,
        ))
    order = {STATE_DEAL: 0, STATE_TRACKING: 1, STATE_EMPTY: 2, STATE_WAITING: 3, STATE_PAUSED: 4}
    return sorted(rows, key=lambda row: (order[row.state], row.watch.name.lower()))


# --------------------------------------------------------------------------- watch form values

def watch_page(conn, watch: Watch, settings: Settings, today: date, *, show_alert_rule: bool = True) -> dict:
    """What a page about one watch shows: prices, chart, offers. ``show_alert_rule`` adds when it
    alerts (the owner's target or drop setting), which people with an account see and visitors
    with only the link do not."""
    window = max(CHART_DAYS, settings.baseline_days + 1)
    lows = listings_repo.daily_lows(conn, watch.id, since=(today - timedelta(days=window)).isoformat())
    listings = listings_repo.for_watch(conn, watch.id)
    pricing = pricing_for(watch, lows, settings, today)
    chart_args = dict(
        today=today, days=CHART_DAYS,
        usual=pricing.baseline.price if pricing.baseline else None,
        alert=pricing.threshold if show_alert_rule else None,
    )
    available = [item for item in listings if item.in_stock is not False]
    return {
        "show_alert_rule": show_alert_rule,
        "watch": watch,
        "settings": settings,
        "pricing": pricing,
        "listings": listings,
        "deal_ids": {item.id for item in listings if is_deal(item, pricing)} if show_alert_rule else set(),
        "best": available[0] if available else None,
        "lowest": min(lows.items(), key=lambda item: item[1]) if lows else None,
        "lows": sorted(lows.items(), reverse=True),
        # One drawing per layout width, so text keeps a readable size at every screen size.
        "charts": [
            (name, line_chart(lows, width=width, height=height, **chart_args))
            for name, width, height in (("chart-wide", 1040, 300), ("chart-medium", 700, 270), ("chart-narrow", 360, 230))
        ],
        "chart_days": CHART_DAYS,
    }


def watch_form_values(watch: Watch) -> dict[str, str]:
    """Flatten a watch into the string values its edit form shows."""
    values = {
        "visibility": watch.visibility,
        "share_link": "on" if watch.share_token else "",
        "name": watch.name,
        "query": "" if watch.query == watch.name else watch.query,
        "match": "" if watch.match == deals.auto_match(watch.name) else watch.match,
        "exclude": watch.exclude,
        "min_price": number_input(watch.min_price),
        "max_price": number_input(watch.max_price),
        "deal_drop_pct": number_input(watch.deal_drop_pct),
        "stock_rule": {None: STOCK_DEFAULT, True: STOCK_REQUIRED, False: STOCK_ANY}[watch.in_stock_only],
        "notify_restock": "on" if watch.notify_restock else "",
        "product_urls": format_product_urls(watch.product_urls),
        "channels_mode": CHANNELS_ALL if watch.channel_ids is None else CHANNELS_SELECTED,
        "enabled": "on" if watch.enabled else "",
    }
    values.update({f"site_{key}": "on" for key in watch.sites})
    values.update({f"url_{key}": url for key, url in watch.site_urls.items()})
    values.update({f"channel_{cid}": "on" for cid in (watch.channel_ids or ())})
    values.update({f"share_user_{uid}": "on" for uid in watch.shared_with})
    return values


def preset_form_values(preset: Preset) -> dict[str, str]:
    values = {
        "exclude": preset.exclude,
        "min_price": number_input(preset.min_price),
        "stock_rule": STOCK_DEFAULT,
        "notify_restock": "on" if preset.notify_restock else "",
        "visibility": VISIBILITY_PRIVATE,
        "channels_mode": CHANNELS_ALL,
        "enabled": "on",
    }
    values.update({f"site_{key}": "on" for key in preset.sites})
    values.update({f"url_{key}": url for key, url in preset.site_urls.items()})
    return values
