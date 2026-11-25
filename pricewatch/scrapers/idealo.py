"""Idealo.fr, a price comparator: one listing per product, at its lowest price across all shops.

Idealo rejects plain HTTP clients, hence ``get_as_browser`` (Chrome impersonation). Keep the check
interval long; this goes against Idealo's terms of use.
"""
from __future__ import annotations

import re
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup

from pricewatch.scrapers.base import (
    KIND_COMPARATOR,
    FetchContext,
    Listing,
    SiteScraper,
    parse_price,
    text_of,
)

KEY = "idealo"
LABEL = "Idealo"
BASE_URL = "https://www.idealo.fr"
ITEM_SELECTOR = "[data-testid=result-item]"

_OFFER_COUNT = re.compile(r"(\d+)\s*offres?")


def search_url(query: str) -> str:
    return f"{BASE_URL}/prechcat.html?q={quote_plus(query)}"


def parse(html: str, base_url: str | None = None) -> list[Listing]:
    soup = BeautifulSoup(html, "lxml")
    out = []
    for item in soup.select(ITEM_SELECTOR):
        a = item.select_one("[data-testid=resultItemLink] a[href]")
        title = text_of(item.select_one("[data-testid=product-summary-title]"))
        price = parse_price(text_of(item.select_one("[data-testid=detailed-price-info-price]")))
        if not a or not title or price is None:
            continue
        offers = _OFFER_COUNT.search(text_of(item.select_one("[data-testid=detailed-price-info]")))
        count = int(offers.group(1)) if offers else 0
        site = f"{LABEL} ({count} offer{'s' if count != 1 else ''})" if offers else LABEL
        # Idealo only lists products with live offers; the price is the cheapest shop's.
        out.append(Listing(site, title, price, urljoin(base_url or BASE_URL, a["href"]), None, KEY))
    return out


def search(ctx: FetchContext, query: str, url: str | None = None) -> list[Listing]:
    url = url or search_url(query)
    return parse(ctx.get_as_browser(url), url)


SCRAPER = SiteScraper(
    key=KEY,
    label=LABEL,
    kind=KIND_COMPARATOR,
    domains=("idealo.fr",),
    description="Price comparator covering most French shops: the lowest price per product, stock unknown.",
    search=search,
    url_help="An Idealo search or category page listing the products to follow.",
)
