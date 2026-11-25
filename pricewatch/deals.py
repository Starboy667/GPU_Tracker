"""Deal detection. Pure functions: no I/O, no clock, no database.

For each watch we keep one number per day: the lowest price at which the product was available at a
regular shop. The *usual price* (baseline) is the median of those daily lows over the last N days,
excluding today so an ongoing deal cannot lower its own reference. A listing is a deal when it is at
or below ``max(target price, usual price - X %)``.
"""
from __future__ import annotations

import re
import statistics
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta

import regex

from pricewatch.models import Watch
from pricewatch.scrapers.base import SOURCE_PRODUCT_PAGE, Listing

ALERT_DEAL = "deal"
ALERT_RESTOCK = "restock"

MAX_REGEX_LENGTH = 500
# Patterns are typed by users and run on every scraped title. The `regex` module is used instead
# of `re` because it can give up: a pattern with runaway backtracking must not freeze the checks.
MATCH_TIMEOUT_SECONDS = 0.25
GPU_VARIANT_SUFFIXES = ("ti", "xt", "xtx", "super", "gre")
_GPU_NAME = re.compile(r"\b(?:rtx|gtx|rx|arc)\s*[a-z]?\d{3,4}", re.I)
# Sources whose posts are already deals: they would drag the "usual price" down.
DEAL_SOURCES = frozenset({"dealabs"})


class SlowPatternError(ValueError):
    """A match or exclude pattern took too long on a real title."""


@dataclass(frozen=True)
class Baseline:
    price: float  # median daily low
    days: int  # number of days it is computed from


@dataclass(frozen=True)
class Decision:
    alert: str | None  # ALERT_DEAL, ALERT_RESTOCK or None
    notified_price: float | None  # value to store for de-duplication (None = re-armed)


def auto_match(name: str) -> str:
    """Title regex derived from a product name: tokens in order, spaces optional.

    'RX 9070 XT' -> 'RX\\s*9070\\s*XT'. For graphics cards, 'RTX 5070' also refuses the
    Ti / Super / XT variants, which are different products.
    """
    tokens = name.split()
    pattern = r"\s*".join(re.escape(t) for t in tokens)
    if tokens and _GPU_NAME.search(name) and tokens[-1].lower() not in GPU_VARIANT_SUFFIXES:
        pattern += r"(?![\s-]*(?:" + "|".join(GPU_VARIANT_SUFFIXES) + r")\b)"
    return pattern


def regex_problem(pattern: str) -> str | None:
    """Why this regex cannot be used, or None."""
    if len(pattern) > MAX_REGEX_LENGTH:
        return f"too long (max {MAX_REGEX_LENGTH} characters)"
    try:
        regex.compile(pattern)
    except regex.error as exc:
        return f"invalid regular expression: {exc}"
    return None


def pattern_matches(pattern: str, text: str) -> bool:
    """Case-insensitive search with a time limit. Raises ``SlowPatternError`` when it runs out."""
    try:
        return regex.search(pattern, text, regex.IGNORECASE, timeout=MATCH_TIMEOUT_SECONDS) is not None
    except TimeoutError as exc:
        raise SlowPatternError("pattern too slow") from exc


def rejection_reason(listing: Listing, watch: Watch) -> str | None:
    """Why a scraped listing is not part of this watch, or None when it is."""
    if listing.source == SOURCE_PRODUCT_PAGE:
        return None  # the user pointed at this exact page: nothing to filter
    if not pattern_matches(watch.match, listing.title):
        return "title does not match"
    if watch.exclude and pattern_matches(watch.exclude, listing.title):
        return "excluded by filter"
    if watch.min_price is not None and listing.price < watch.min_price:
        return "below the minimum price"
    return None


def counts_for_baseline(listing: Listing) -> bool:
    return listing.in_stock is not False and listing.source not in DEAL_SOURCES


def baseline(lows: Mapping[str, float], today: date, span_days: int, min_days: int) -> Baseline | None:
    """Median of the daily lows in [today - span_days, today), or None without enough history."""
    start = (today - timedelta(days=span_days)).isoformat()
    end = today.isoformat()
    recent = [price for day, price in lows.items() if start <= day < end]
    if len(recent) < max(min_days, 1):
        return None
    return Baseline(price=statistics.median(recent), days=len(recent))


def deal_threshold(max_price: float | None, base: Baseline | None, drop_pct: float) -> float | None:
    """Alert at or below this price: the larger of the fixed target and baseline - drop_pct %."""
    limits = []
    if max_price is not None:
        limits.append(float(max_price))
    if base is not None:
        limits.append(base.price * (1 - drop_pct / 100))
    return max(limits) if limits else None


def decide(
    *,
    price: float,
    in_stock: bool | None,
    threshold: float | None,
    in_stock_only: bool,
    renotify_drop_pct: float,
    notify_restock: bool,
    seen_before: bool,
    previous_in_stock: bool | None,
    notified_price: float | None,
) -> Decision:
    """What to do with one listing given what we knew about it before this check."""
    is_deal = threshold is not None and price <= threshold and (in_stock is not False or not in_stock_only)
    restocked = notify_restock and seen_before and previous_in_stock is False and in_stock is True
    if not is_deal:
        # Back above the threshold (or out of stock): forget it so the next dip alerts again.
        return Decision(alert=ALERT_RESTOCK if restocked else None, notified_price=None)
    dropped_further = notified_price is not None and price < notified_price and (
        price <= notified_price * (1 - renotify_drop_pct / 100)
    )
    if notified_price is None or dropped_further:
        return Decision(alert=ALERT_DEAL, notified_price=price)
    return Decision(alert=ALERT_RESTOCK if restocked else None, notified_price=notified_price)
