"""Alternate.fr search and category pages."""
from __future__ import annotations

from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup

from pricewatch.scrapers.base import (
    KIND_RETAILER,
    FetchContext,
    Listing,
    SiteScraper,
    parse_price,
    stock_from_text,
    text_of,
)

KEY = "alternate"
LABEL = "Alternate"
BASE_URL = "https://www.alternate.fr"
ITEM_SELECTOR = "a.productBox"


def search_url(query: str) -> str:
    return f"{BASE_URL}/listing.xhtml?q={quote_plus(query)}"


def parse(html: str, base_url: str | None = None) -> list[Listing]:
    soup = BeautifulSoup(html, "lxml")
    out = []
    for a in soup.select(ITEM_SELECTOR):
        price = parse_price(text_of(a.select_one("span.price")))
        title = text_of(a.select_one(".product-name"))
        if not title or not a.get("href") or price is None:
            continue
        stock = stock_from_text(text_of(a.select_one(".delivery-info")))
        out.append(Listing(LABEL, title, price, urljoin(base_url or BASE_URL, a["href"]), stock, KEY))
    return out


def search(ctx: FetchContext, query: str, url: str | None = None) -> list[Listing]:
    url = url or search_url(query)
    return parse(ctx.get(url), url)


SCRAPER = SiteScraper(
    key=KEY,
    label=LABEL,
    kind=KIND_RETAILER,
    domains=("alternate.fr",),
    description="PC hardware and electronics retailer; searched with the watch's query.",
    search=search,
    url_help="An Alternate.fr search or category page listing the products to follow.",
)
