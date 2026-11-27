"""Joybuy.fr (JD.com's European shop), a general marketplace: electronics, appliances, groceries.

The search page is a Next.js app: the products are not in the visible markup in a stable form
(class names are hashed) but in the data the page ships for its own rendering, as a stream of
``self.__next_f.push([1, "..."])`` calls. Each product is an ``itemCompleteView`` object in it,
with its id, title, price and stock.
"""
from __future__ import annotations

import json
import math
import re
from urllib.parse import quote

from pricewatch.scrapers.base import KIND_RETAILER, FetchContext, Listing, SiteScraper

KEY = "joybuy"
LABEL = "Joybuy"
BASE_URL = "https://www.joybuy.fr"
MAX_TITLE_LENGTH = 300

_PUSH = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)')
_ITEM = re.compile(r'"itemCompleteView":')
_decoder = json.JSONDecoder()


def search_url(query: str) -> str:
    return f"{BASE_URL}/s?k={quote(query)}"


def _page_data(html: str) -> str:
    """The text of every data chunk the page pushes, unescaped and joined."""
    parts = []
    for chunk in _PUSH.findall(html):
        try:
            parts.append(json.loads(f'"{chunk}"'))
        except ValueError:
            continue  # a chunk cut mid-escape: skip it, the others still parse
    return "".join(parts)


def _price(view: object) -> float | None:
    info = view.get("priceInfo") if isinstance(view, dict) else None
    raw = info.get("mainPrice") if isinstance(info, dict) else None
    try:
        price = float(raw)  # sent as a string: "949.99"
    except (TypeError, ValueError):
        return None
    return price if math.isfinite(price) and price > 0 else None


def _in_stock(common: dict) -> bool | None:
    flags = [common.get("inStock"), common.get("isAvailable")]
    if any(flag is False for flag in flags):
        return False
    return True if any(flag is True for flag in flags) else None


def product_url(product_id: str) -> str:
    """The address of a product from its id alone. The site ignores the descriptive part of its
    own links (/dp/<words>/<id>), and those words change when a seller renames the product: a
    listing is identified by its address, so it has to stay the same from one check to the next."""
    return f"{BASE_URL}/dp/p/{product_id}"


def parse(html: str, base_url: str | None = None) -> list[Listing]:
    data = _page_data(html)
    out, seen = [], set()
    for match in _ITEM.finditer(data):
        try:
            item, _ = _decoder.raw_decode(data, match.end())
        except ValueError:
            continue
        if not isinstance(item, dict) or not isinstance(item.get("itemCommonView"), dict):
            continue
        common = item["itemCommonView"]
        product_id = str(common.get("skuId") or "")
        title_view = item.get("itemTitleView")
        title = title_view.get("title") if isinstance(title_view, dict) else None
        price = _price(item.get("itemPriceView"))
        if not product_id.isdigit() or not isinstance(title, str) or not title.strip() or price is None:
            continue
        if product_id in seen:
            continue
        seen.add(product_id)
        out.append(Listing(
            site=LABEL,
            title=" ".join(title.split())[:MAX_TITLE_LENGTH],
            price=price,
            url=product_url(product_id),
            in_stock=_in_stock(common),
            source=KEY,
        ))
    return out


def search(ctx: FetchContext, query: str, url: str | None = None) -> list[Listing]:
    url = url or search_url(query)
    return parse(ctx.get(url), url)


SCRAPER = SiteScraper(
    key=KEY,
    label=LABEL,
    kind=KIND_RETAILER,
    domains=("joybuy.fr",),
    description="General marketplace (JD.com in Europe): electronics, appliances, everyday goods; searched with the watch's query.",
    search=search,
    url_help="A Joybuy search page, e.g. https://www.joybuy.fr/s?k=rtx%205070",
)
