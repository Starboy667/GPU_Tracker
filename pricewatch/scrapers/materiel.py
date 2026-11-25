"""Materiel.net search and category pages."""
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

KEY = "materiel"
LABEL = "Materiel.net"
BASE_URL = "https://www.materiel.net"
ITEM_SELECTOR = "li.c-products-list__item"


def search_url(query: str) -> str:
    return f"{BASE_URL}/recherche/{quote(query, safe='')}/"


def parse(html: str, base_url: str | None = None) -> list[Listing]:
    soup = BeautifulSoup(html, "lxml")
    out = []
    for li in soup.select(ITEM_SELECTOR):
        a = li.select_one("a.c-product__link")
        price_el = li.select_one(".o-product__price")
        price = parse_price(price_el.get_text("", strip=True)) if price_el else None
        if not a or not a.get("href") or price is None:
            continue
        title = a.get("title") or text_of(li.select_one(".c-product__title"))
        stock = stock_from_text(text_of(li.select_one(".o-availability__value")))
        out.append(Listing(LABEL, title, price, urljoin(base_url or BASE_URL, a["href"]), stock, KEY))
    return out


def search(ctx: FetchContext, query: str, url: str | None = None) -> list[Listing]:
    url = url or search_url(query)
    return parse(ctx.get(url), url)


SCRAPER = SiteScraper(
    key=KEY,
    label=LABEL,
    kind=KIND_RETAILER,
    domains=("materiel.net",),
    description="PC hardware, TVs and electronics retailer; searched with the watch's query.",
    search=search,
    url_help="A Materiel.net search or category page listing the products to follow.",
)
