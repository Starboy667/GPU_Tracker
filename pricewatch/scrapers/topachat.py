"""TopAchat search and category pages.

TopAchat is a Nuxt app: product data lives in an inline JS object, not in the HTML, so the page is
read with regular expressions rather than a DOM parser.
"""
from __future__ import annotations

import re
from urllib.parse import quote_plus, urljoin

from pricewatch.scrapers.base import KIND_RETAILER, FetchContext, Listing, SiteScraper

KEY = "topachat"
LABEL = "TopAchat"
BASE_URL = "https://www.topachat.com"
IN_STOCK_CODES = ("available", "limited-stock")
PRODUCT_WINDOW = 2500  # a product's price and link sit within this many characters of its ref

_PRODUCT = re.compile(r'\{ref:"(in\d+)",label:"((?:[^"\\]|\\.)*)"')
_PRICE = re.compile(r"price_final:(\d+(?:\.\d+)?)")
_LINK = re.compile(r'url_product:"((?:[^"\\]|\\.)+)"')
_AVAILABILITY = re.compile(r'availability:\{code:"([^"]+)"')
_JS_ESCAPE = re.compile(r"\\(u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|.)", re.S)
_JS_SIMPLE_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}


def js_unescape(text: str) -> str:
    """Decode the escapes of a JS string literal (\\u00e9, \\/, \\") and leave real UTF-8 alone."""

    def replace(m: re.Match) -> str:
        escape = m.group(1)
        if len(escape) > 1:  # uXXXX or xXX
            return chr(int(escape[1:], 16))
        return _JS_SIMPLE_ESCAPES.get(escape, escape)

    out = _JS_ESCAPE.sub(replace, text)
    if any(0xD800 <= ord(c) <= 0xDFFF for c in out):  # 😀 pairs -> one code point
        out = out.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    return out


def search_url(query: str) -> str:
    return f"{BASE_URL}/pages/recherche.php?q={quote_plus(query)}"


def parse(html: str, base_url: str | None = None) -> list[Listing]:
    matches = list(_PRODUCT.finditer(html))
    out = []
    for i, m in enumerate(matches):
        # Stop at the next product so a product without a price can't borrow its neighbour's.
        next_start = matches[i + 1].start() if i + 1 < len(matches) else len(html)
        segment = html[m.start(): min(m.start() + PRODUCT_WINDOW, next_start)]
        price = _PRICE.search(segment)
        if not price:
            continue
        link = _LINK.search(segment)
        availability = _AVAILABILITY.search(segment)
        stock = availability.group(1) in IN_STOCK_CODES if availability else None
        href = urljoin(BASE_URL, js_unescape(link.group(1))) if link else (base_url or BASE_URL)
        title = " ".join(js_unescape(m.group(2)).split())
        out.append(Listing(LABEL, title, float(price.group(1)), href, stock, KEY))
    return out


def search(ctx: FetchContext, query: str, url: str | None = None) -> list[Listing]:
    url = url or search_url(query)
    return parse(ctx.get(url), url)


SCRAPER = SiteScraper(
    key=KEY,
    label=LABEL,
    kind=KIND_RETAILER,
    domains=("topachat.com",),
    description="PC hardware retailer. Its search page returns few products; a category URL works better.",
    search=search,
    url_help="A TopAchat category page with the right filter applied (open the category, filter, copy the URL).",
)
