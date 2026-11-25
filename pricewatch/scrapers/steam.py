"""Steam store, through the JSON endpoints its own pages use (no key needed).

With a plain query it returns the store's search results (only the game itself when one result
bears exactly that name). With the address of a store page
(``https://store.steampowered.com/app/1245620/ELDEN_RING/``) it follows exactly that game, which is
the reliable way to leave its DLC and sequels out. Prices are those of the French store, in euros.
"""
from __future__ import annotations

import json
import re
from urllib.parse import quote_plus

from pricewatch.scrapers.base import KIND_RETAILER, FetchContext, Listing, ScrapeError, SiteScraper

KEY = "steam"
LABEL = "Steam"
GROUP = "Video games"
STORE = "https://store.steampowered.com"
COUNTRY = "FR"
MAX_TITLE_LENGTH = 300

_STORE_PAGE = re.compile(r"/(app|sub)/(\d+)")
_WORDS = re.compile(r"[a-z0-9]+")


def search_url(query: str) -> str:
    return f"{STORE}/api/storesearch/?term={quote_plus(query)}&l=french&cc={COUNTRY}"


def details_url(kind: str, item_id: str) -> str:
    if kind == "sub":
        return f"{STORE}/api/packagedetails?packageids={item_id}&cc={COUNTRY.lower()}&l=french"
    return f"{STORE}/api/appdetails?appids={item_id}&cc={COUNTRY.lower()}&l=french&filters=basic,price_overview"


def _load(text: str) -> object:
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ScrapeError("Steam did not answer with JSON") from exc


def _euros(price: object) -> float | None:
    """Steam prices are integers in cents, with their currency next to them."""
    if not isinstance(price, dict) or price.get("currency") != "EUR":
        return None
    cents = price.get("final")
    if isinstance(cents, bool) or not isinstance(cents, (int, float)) or cents <= 0:
        return None
    return round(cents / 100, 2)


def parse_search(text: str) -> list[Listing]:
    data = _load(text)
    items = data.get("items") if isinstance(data, dict) else None
    out = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        name, item_id, price = item.get("name"), item.get("id"), _euros(item.get("price"))
        if not isinstance(name, str) or not isinstance(item_id, int) or price is None:
            continue  # free, unreleased, or not sold in euros
        kind = "sub" if item.get("type") == "sub" else "app"
        out.append(Listing(LABEL, name[:MAX_TITLE_LENGTH], price, f"{STORE}/{kind}/{item_id}/", True, KEY))
    return out


def parse_details(text: str, kind: str, item_id: str) -> list[Listing]:
    data = _load(text)
    entry = data.get(item_id) if isinstance(data, dict) else None
    if not isinstance(entry, dict) or not entry.get("success") or not isinstance(entry.get("data"), dict):
        raise ScrapeError("Steam does not know this page (or does not sell it in France)")
    details = entry["data"]
    name = details.get("name")
    price = _euros(details.get("price_overview") if kind == "app" else details.get("price"))
    if not isinstance(name, str) or price is None:
        return []  # free to play, or not released yet: nothing to follow
    return [Listing(LABEL, name[:MAX_TITLE_LENGTH], price, f"{STORE}/{kind}/{item_id}/", True, KEY)]


def search(ctx: FetchContext, query: str, url: str | None = None) -> list[Listing]:
    if url:
        page = _STORE_PAGE.search(url)
        if page is None:
            raise ScrapeError("the Steam address must be a game page (…/app/<number>/…) or a package (…/sub/<number>/)")
        kind, item_id = page.groups()
        return parse_details(ctx.get(details_url(kind, item_id)), kind, item_id)
    return prefer_exact_name(parse_search(ctx.get(search_url(query))), query)


def prefer_exact_name(listings: list[Listing], query: str) -> list[Listing]:
    """When a result is named exactly like the query, keep only it: searching "Elden Ring" means
    the game, not its DLC, its packs and its sequel, which would pass for cheap copies of it."""
    wanted = _WORDS.findall(query.lower())
    exact = [listing for listing in listings if _WORDS.findall(listing.title.lower()) == wanted]
    return exact or listings


SCRAPER = SiteScraper(
    key=KEY,
    label=LABEL,
    kind=KIND_RETAILER,
    domains=("steampowered.com",),
    description="The official store, French prices. A search also returns DLC and sequels: give the game's page to follow it alone.",
    search=search,
    url_help="The game's store page, e.g. https://store.steampowered.com/app/1245620/ELDEN_RING/",
    group=GROUP,
)
