"""Generic product page: read the price of one product on any shop.

Works on pages exposing schema.org data (JSON-LD or microdata) or Open Graph product tags, which
covers most shops. For the others the user can give a CSS selector pointing at the price.
"""
from __future__ import annotations

import html as html_lib
import json
import math
import re
from collections.abc import Iterator
from dataclasses import dataclass
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from pricewatch.scrapers.base import (
    SOURCE_PRODUCT_PAGE,
    FetchContext,
    Listing,
    ScrapeError,
    parse_price,
    stock_from_text,
    text_of,
)

MAX_TITLE_LENGTH = 300
MAX_JSON_LD_DEPTH = 6
MAX_SELECTOR_MATCHES = 20
NO_PRICE_MESSAGE = "no price found on this page (try adding a CSS selector)"

PRODUCT_TYPES = {"product", "productgroup"}
# schema.org ItemAvailability values plus the Open Graph / Facebook catalogue spellings.
AVAILABLE = {"instock", "limitedavailability", "onlineonly", "instoreonly", "availablefororder"}
UNAVAILABLE = {"outofstock", "soldout", "discontinued", "preorder", "backorder", "oos"}
PRICE_META = ("product:price:amount", "og:price:amount")
AVAILABILITY_META = ("product:availability", "og:availability")
NESTED_NODE_KEYS = ("@graph", "mainEntity")  # not isRelatedTo & co: those are other products

_CDATA_OR_COMMENT = re.compile(r"^\s*(?://\s*)?(?:<!\[CDATA\[|<!--)|(?://\s*)?(?:\]\]>|-->)\s*$")


@dataclass(frozen=True)
class _Offer:
    price: float
    in_stock: bool | None


@dataclass(frozen=True)
class _Structured:
    name: str
    offer: _Offer | None


def site_label(url: str) -> str:
    """'https://www.boulanger.com/ref/1' -> 'boulanger.com'; '' if the URL has no host."""
    try:
        host = (urlsplit(url or "").hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def scrape_product_page(ctx: FetchContext, url: str, selector: str | None = None) -> Listing:
    """Fetch a product page typed by a user and read its price. Raises only ``ScrapeError``."""
    try:
        html = ctx.get_as_browser(url)
    except ScrapeError:
        raise
    except Exception as exc:
        raise ScrapeError("could not fetch this page") from exc
    return parse_product_page(html, url, selector)


def parse_product_page(html: str, url: str, selector: str | None = None) -> Listing:
    """Read title, price and availability from a product page. Raises only ``ScrapeError``."""
    try:
        return _parse(html, url, (selector or "").strip())
    except ScrapeError:
        raise
    except Exception as exc:
        raise ScrapeError("could not read this page") from exc


def _parse(html: str, url: str, selector: str) -> Listing:
    soup = BeautifulSoup(html, "lxml")
    structured = _from_json_ld(soup, url)
    stock = structured.offer.in_stock if structured.offer else None
    if selector:
        price = _price_from_selector(soup, selector)
    elif structured.offer:
        price = structured.offer.price
    else:
        price = _price_from_meta(soup)
    if price is None:
        raise ScrapeError(NO_PRICE_MESSAGE)
    if stock is None:
        stock = _stock_from_meta(soup)
    title = structured.name or _meta(soup, ("og:title",)) or text_of(soup.title) or site_label(url)
    return Listing(site_label(url), _clean_title(title), price, url, stock, SOURCE_PRODUCT_PAGE)


def _clean_title(title: str) -> str:
    return " ".join(html_lib.unescape(title).split())[:MAX_TITLE_LENGTH].rstrip()


# --------------------------------------------------------------------------- CSS selector

def _price_from_selector(soup: BeautifulSoup, selector: str) -> float:
    """Price of the first matching element that holds one (shops often emit empty twins first)."""
    try:
        elements = soup.select(selector, limit=MAX_SELECTOR_MATCHES)
    except Exception as exc:  # soupsieve.SelectorSyntaxError, and ValueError on odd input
        raise ScrapeError("not a valid CSS selector") from exc
    if not elements:
        raise ScrapeError("the CSS selector matches nothing on this page")
    for element in elements:
        price = _to_price(_attribute(element, "content")) or _to_price(text_of(element))
        if price is not None:
            return price
    raise ScrapeError("no price in the element matched by the CSS selector")


# --------------------------------------------------------------------------- JSON-LD

def _from_json_ld(soup: BeautifulSoup, url: str) -> _Structured:
    """Name and best offer of the first product that has a price (else the first product's name)."""
    first_name = ""
    for node in _json_ld_nodes(soup):
        if not PRODUCT_TYPES & _types(node):
            continue
        name = node.get("name") if isinstance(node.get("name"), str) else ""
        first_name = first_name or name
        offer = _best_offer(_offers_of(node, url))
        if offer:
            return _Structured(name or first_name, offer)
    return _Structured(first_name, None)


def _json_ld_nodes(soup: BeautifulSoup) -> Iterator[dict]:
    for script in soup.find_all("script"):
        if "ld+json" not in str(script.get("type") or "").lower():
            continue
        data = _load_json(script.string or script.get_text() or "")
        if data is not None:
            yield from _walk(data, 0)


def _load_json(raw: str):
    raw = _CDATA_OR_COMMENT.sub("", raw.strip()).strip().rstrip(";")
    try:
        return json.loads(raw, strict=False)
    except ValueError:
        return None  # a malformed block must not hide the valid ones


def _walk(data, depth: int) -> Iterator[dict]:
    if depth > MAX_JSON_LD_DEPTH:
        return
    if isinstance(data, list):
        for entry in data:
            yield from _walk(entry, depth + 1)
    elif isinstance(data, dict):
        yield data
        for key in NESTED_NODE_KEYS:
            if key in data:
                yield from _walk(data[key], depth + 1)


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _last_segment(value: str) -> str:
    """'https://schema.org/InStock', 'schema:InStock', 'in stock' -> 'instock'."""
    return re.sub(r"[\s_\-]", "", value.rsplit("/", 1)[-1].rsplit(":", 1)[-1]).lower()


def _types(node: dict) -> set[str]:
    return {_last_segment(t) for t in _as_list(node.get("@type")) if isinstance(t, str)}


def _offers_of(node: dict, url: str) -> list[_Offer]:
    variants = [v for v in _as_list(node.get("hasVariant")) if isinstance(v, dict)]
    if "productgroup" in _types(node) and variants:
        on_this_page = [v for v in variants if _same_page(v.get("url"), url)]
        offers = [o for v in (on_this_page or variants) for o in _iter_offers(v.get("offers"), 0)]
        if offers:
            return offers
    return list(_iter_offers(node.get("offers"), 0))


def _iter_offers(raw, depth: int) -> Iterator[_Offer]:
    if depth > MAX_JSON_LD_DEPTH:
        return
    for offer in _as_list(raw):
        if not isinstance(offer, dict):
            continue
        candidates = [offer.get("price"), offer.get("lowPrice")]
        candidates += [s.get("price") for s in _as_list(offer.get("priceSpecification")) if isinstance(s, dict)]
        prices = [p for p in map(_to_price, candidates) if p is not None]
        if prices:
            yield _Offer(min(prices), _availability(offer.get("availability")))
        if offer.get("offers"):  # AggregateOffer listing its offers
            yield from _iter_offers(offer["offers"], depth + 1)


def _best_offer(offers: list[_Offer]) -> _Offer | None:
    """The cheapest offer that can be bought; the cheapest of all if none can."""
    if not offers:
        return None
    buyable = [o for o in offers if o.in_stock is not False]
    return min(buyable or offers, key=lambda o: o.price)


def _same_page(candidate, url: str) -> bool:
    if not isinstance(candidate, str):
        return False
    try:
        a, b = urlsplit(candidate), urlsplit(url)
    except ValueError:
        return False
    return (site_label(candidate), a.path.rstrip("/"), a.query) == (site_label(url), b.path.rstrip("/"), b.query)


def _to_price(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        price = float(value)
    elif isinstance(value, str):
        price = parse_price(value)
    else:
        return None
    return price if price is not None and math.isfinite(price) and price > 0 else None


def _availability(value) -> bool | None:
    if isinstance(value, dict):
        value = value.get("@id") or value.get("name")
    if not isinstance(value, str):
        return None
    key = _last_segment(value)
    if key in AVAILABLE:
        return True
    if key in UNAVAILABLE:
        return False
    return None


# --------------------------------------------------------------------------- meta tags and microdata

def _attribute(element, name: str) -> str:
    value = element.get(name)
    return value.strip() if isinstance(value, str) else ""


def _meta(soup: BeautifulSoup, names: tuple[str, ...]) -> str:
    for name in names:
        for attr in ("property", "name"):
            tag = soup.find("meta", attrs={attr: name})
            if tag and _attribute(tag, "content"):
                return _attribute(tag, "content")
    return ""


def _price_from_meta(soup: BeautifulSoup) -> float | None:
    price = _to_price(_meta(soup, PRICE_META))
    if price is not None:
        return price
    element = soup.select_one("[itemprop=price]")
    if element is None:
        return None
    return _to_price(_attribute(element, "content")) or _to_price(text_of(element))


def _stock_from_meta(soup: BeautifulSoup) -> bool | None:
    value = _meta(soup, AVAILABILITY_META)
    if not value:
        element = soup.select_one("[itemprop=availability]")
        if element is None:
            return None
        value = _attribute(element, "content") or _attribute(element, "href") or text_of(element)
    stock = _availability(value)
    return stock if stock is not None else stock_from_text(value)
