"""Scraping one watch: ask every configured shop, filter the results. No database access."""
from __future__ import annotations

import dataclasses
import logging
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from pricewatch import deals
from pricewatch.models import Watch
from pricewatch.scrapers.base import SOURCE_PRODUCT_PAGE, FetchContext, Listing, ScrapeError, SiteScraper

log = logging.getLogger(__name__)

PRODUCT_HEALTH_PREFIX = "product:"
MAX_ERROR_LENGTH = 300
SLOW_PATTERN_MESSAGE = "the title pattern takes too long on these titles: simplify the match or ignore expression"

ProductScraper = Callable[[FetchContext, str, str | None], Listing]


@dataclass(frozen=True)
class WatchScan:
    """What the shops returned for one watch, before anything is stored."""

    accepted: tuple[Listing, ...]  # part of the watch, cheapest first, one per URL
    rejected: tuple[tuple[Listing, str], ...]  # (listing, why it was filtered out)
    errors: tuple[tuple[str, str], ...]  # (site label, error)
    sources_ok: tuple[str, ...]  # search scrapers that answered
    outcomes: tuple[tuple[str, str | None], ...]  # (health key, error or None) per request


def polite_pause() -> None:
    """Wait a little between two requests so retailers are not hammered."""
    time.sleep(random.uniform(1.5, 3.5))


def product_health_key(url: str) -> str:
    from pricewatch.scrapers import site_label

    return PRODUCT_HEALTH_PREFIX + site_label(url)


def scan_watch(
    watch: Watch,
    ctx: FetchContext,
    *,
    scrapers: Mapping[str, SiteScraper],
    product_scraper: ProductScraper,
    pause: Callable[[], None] = polite_pause,
) -> WatchScan:
    found: dict[str, Listing] = {}
    rejected: list[tuple[Listing, str]] = []
    errors: list[tuple[str, str]] = []
    sources_ok: list[str] = []
    outcomes: list[tuple[str, str | None]] = []
    first = True

    def wait() -> None:
        nonlocal first
        if not first:
            pause()
        first = False

    for key in watch.sites:
        scraper = scrapers.get(key)
        if scraper is None:
            errors.append((key, "unknown site"))
            continue
        wait()
        try:
            results = scraper.search(ctx, watch.query, watch.site_urls.get(key))
        except ScrapeError as exc:
            message = short(str(exc))
        except Exception as exc:  # a parser tripping on new markup must not stop the run
            log.exception("scraper %s crashed for watch %s", key, watch.name)
            message = f"parser error ({type(exc).__name__})"
        else:
            outcomes.append((key, None))  # the shop answered, whatever our filters make of it
            try:
                kept, dropped = _filter(results, watch)
            except deals.SlowPatternError:
                errors.append((scraper.label, SLOW_PATTERN_MESSAGE))
                continue
            sources_ok.append(key)
            rejected += dropped
            for listing in kept:
                found.setdefault(listing.url, listing)
            continue
        errors.append((scraper.label, message))
        outcomes.append((key, message))

    for product in watch.product_urls:
        wait()
        health_key = product_health_key(product.url)
        try:
            listing = product_scraper(ctx, product.url, product.selector or None)
        except ScrapeError as exc:
            message = short(str(exc))
        except Exception as exc:
            log.exception("product page scraper crashed for watch %s", watch.name)
            message = f"parser error ({type(exc).__name__})"
        else:
            outcomes.append((health_key, None))
            # Keep the configured URL as identity, whatever the page redirects or canonicalises to.
            found[product.url] = dataclasses.replace(listing, url=product.url, source=SOURCE_PRODUCT_PAGE)
            continue
        errors.append((health_key.removeprefix(PRODUCT_HEALTH_PREFIX), message))
        outcomes.append((health_key, message))

    return WatchScan(
        accepted=tuple(sorted(found.values(), key=lambda item: item.price)),
        rejected=tuple(rejected),
        errors=tuple(errors),
        sources_ok=tuple(sources_ok),
        outcomes=tuple(outcomes),
    )


def _filter(results: list[Listing], watch: Watch) -> tuple[list[Listing], list[tuple[Listing, str]]]:
    kept: list[Listing] = []
    dropped: list[tuple[Listing, str]] = []
    for listing in results:
        reason = deals.rejection_reason(listing, watch)
        if reason is None:
            kept.append(listing)
        else:
            dropped.append((listing, reason))
    return kept, dropped


def short(text: str) -> str:
    return " ".join(text.split())[:MAX_ERROR_LENGTH]
