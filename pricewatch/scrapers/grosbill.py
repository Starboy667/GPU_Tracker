"""Grosbill search and category pages."""
from __future__ import annotations

from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup

from pricewatch.scrapers.base import (
    KIND_RETAILER,
    FetchContext,
    Listing,
    SiteScraper,
    parse_price,
    text_of,
)

KEY = "grosbill"
LABEL = "Grosbill"
BASE_URL = "https://www.grosbill.com"
ITEM_SELECTOR = "article.product-row"
OUT_OF_STOCK_CLASS = "dw-out"


def search_url(query: str) -> str:
    return f"{BASE_URL}/catv2.cgi?mode=recherche&recherche={quote_plus(query)}"


def parse(html: str, base_url: str | None = None) -> list[Listing]:
    soup = BeautifulSoup(html, "lxml")
    out = []
    for art in soup.select(ITEM_SELECTOR):
        a = art.select_one("a.p-name")
        price = parse_price(art.get("data-price", "")) or parse_price(text_of(art.select_one(".price-main")))
        if not a or not a.get("href") or price is None:
            continue
        status = art.select_one(".dw-status")
        stock = OUT_OF_STOCK_CLASS not in (status.get("class") or []) if status else None
        # data-price sometimes carries a third decimal (999.991).
        out.append(Listing(LABEL, text_of(a), round(price, 2), urljoin(base_url or BASE_URL, a["href"]), stock, KEY))
    return out


def search(ctx: FetchContext, query: str, url: str | None = None) -> list[Listing]:
    url = url or search_url(query)
    return parse(ctx.get(url), url)


SCRAPER = SiteScraper(
    key=KEY,
    label=LABEL,
    kind=KIND_RETAILER,
    domains=("grosbill.com",),
    description="PC hardware and electronics retailer; searched with the watch's query.",
    search=search,
    url_help="A Grosbill search or category page listing the products to follow.",
)
