"""LDLC search and category pages."""
from __future__ import annotations

from urllib.parse import quote, urljoin

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

KEY = "ldlc"
LABEL = "LDLC"
BASE_URL = "https://www.ldlc.com"
ITEM_SELECTOR = "li.pdt-item"


def search_url(query: str) -> str:
    return f"{BASE_URL}/recherche/{quote(query, safe='')}/"


def parse(html: str, base_url: str | None = None) -> list[Listing]:
    soup = BeautifulSoup(html, "lxml")
    out = []
    for li in soup.select(ITEM_SELECTOR):
        a = li.select_one("h3 a")
        price_el = li.select_one(".price")
        price = parse_price(price_el.get_text("", strip=True)) if price_el else None
        if not a or not a.get("href") or price is None:
            continue
        stock = stock_from_text(text_of(li.select_one(".stock")))
        out.append(Listing(LABEL, text_of(a), price, urljoin(base_url or BASE_URL, a["href"]), stock, KEY))
    return out


def search(ctx: FetchContext, query: str, url: str | None = None) -> list[Listing]:
    url = url or search_url(query)
    return parse(ctx.get(url), url)


SCRAPER = SiteScraper(
    key=KEY,
    label=LABEL,
    kind=KIND_RETAILER,
    domains=("ldlc.com",),
    description="PC hardware and electronics retailer; searched with the watch's query.",
    search=search,
    url_help="An LDLC search or category page listing the products to follow.",
)
