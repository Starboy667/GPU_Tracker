"""Dealabs community deals, read from its public RSS feeds.

Dealabs has no search feed, so the default is the feed of all new deals and the watch's ``match``
regex does the filtering; the query is ignored. A URL override can point at a group feed instead
(``/rss/groupe/cartes-graphiques``, ``/rss/groupe/televiseurs``). One feed serves every watch, so it
is fetched once per run. Covers merchants that can't be scraped directly (Amazon, Cdiscount, ...).
Stock is unknown and deals may have expired.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from bs4 import BeautifulSoup

from pricewatch.scrapers.base import KIND_DEALS, FetchContext, Listing, SiteScraper, parse_price

KEY = "dealabs"
LABEL = "Dealabs"
DEFAULT_FEED = "https://www.dealabs.com/rss/nouveaux"
GPU_FEED = "https://www.dealabs.com/rss/groupe/cartes-graphiques"
DEFAULT_MAX_AGE_DAYS = 3


def _is_too_old(pub_date: str, now: datetime, max_age_days: int) -> bool:
    try:
        posted = parsedate_to_datetime(pub_date)
    except (TypeError, ValueError):
        return False  # unreadable date: keep the deal
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=timezone.utc)
    return now - posted > timedelta(days=max_age_days)


def parse(
    xml: str,
    base_url: str | None = None,
    *,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    now: datetime | None = None,
) -> list[Listing]:
    """Listings of an RSS feed, skipping deals posted more than ``max_age_days`` ago."""
    now = now or datetime.now(timezone.utc)
    soup = BeautifulSoup(xml, "xml")
    out = []
    for item in soup.find_all("item"):
        merchant = item.find("merchant")
        price = parse_price(merchant.get("price", "")) if merchant else None
        title = item.title.get_text(strip=True) if item.title else ""
        link = item.link.get_text(strip=True) if item.link else ""
        if price is None or not title or not link:
            continue
        if item.pubDate and _is_too_old(item.pubDate.get_text(strip=True), now, max_age_days):
            continue
        shop = merchant.get("name")
        out.append(Listing(f"{LABEL}/{shop}" if shop else LABEL, title, price, link, None, KEY))
    return out


def search(ctx: FetchContext, query: str, url: str | None = None) -> list[Listing]:
    url = url or DEFAULT_FEED
    return parse(ctx.get_cached(url), url, max_age_days=ctx.dealabs_max_age_days)


SCRAPER = SiteScraper(
    key=KEY,
    label=LABEL,
    kind=KIND_DEALS,
    domains=("dealabs.com",),
    description=(
        "Community deals from every merchant (Amazon, Cdiscount, ...). Best with a group feed: the default "
        "feed of all new deals only reaches back about half an hour."
    ),
    search=search,
    url_help="A Dealabs RSS feed, e.g. https://www.dealabs.com/rss/groupe/televiseurs (default: all new deals).",
)
