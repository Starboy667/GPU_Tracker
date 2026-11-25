"""DLCompare.fr, a comparator of game keys: every offer of one game, shop by shop.

A game page lists hundreds of offers that differ by platform (Steam key, PS5, Xbox...), edition,
region, and kind (key, gift, account, boxed copy). Each becomes one listing whose title spells
those out, so a watch's filters can keep, say, Steam keys for Europe only:

    Elden Ring - Steam (PC) - Standard Edition - Europe - code DLCOMPARE
    Elden Ring Shadow of the Erdtree [DLC] - Steam (PC) - Standard Edition - Global

The offers come from the JSON the game page itself loads. With a query, the first search result
whose name fits is used; give the game's page address to be exact. Like Idealo, the site rejects
plain HTTP clients, and automated access is against its terms: keep the check interval long.
"""
from __future__ import annotations

import json
import re
from urllib.parse import quote_plus

from bs4 import BeautifulSoup

from pricewatch.scrapers.base import KIND_COMPARATOR, FetchContext, Listing, ScrapeError, SiteScraper, text_of

KEY = "dlcompare"
LABEL = "DLCompare"
GROUP = "Video games"
BASE_URL = "https://www.dlcompare.fr"
MAX_OFFERS = 600
MAX_TITLE_LENGTH = 300

_GAME_PAGE = re.compile(r"/jeux/(\d+)/")
_WORDS = re.compile(r"[a-z0-9]+")


def search_url(query: str) -> str:
    return f"{BASE_URL}/search?q={quote_plus(query)}"


def prices_url(game_id: str) -> str:
    return f"{BASE_URL}/price/data/game/{game_id}/eur"


def offer_url(offer_id: int) -> str:
    """DLCompare's own redirect to the shop: the link its price list uses."""
    return f"{BASE_URL}/price/{offer_id}/serve"


def parse_search(html: str) -> list[tuple[str, str]]:
    """(game id, name) of each game in a search page, in the site's order."""
    soup = BeautifulSoup(html, "lxml")
    games = []
    for item in soup.select("a.game-list-item[href]"):
        page = _GAME_PAGE.search(item["href"])
        name = text_of(item.select_one(".name"))
        if page and name:
            games.append((page.group(1), name))
    return games


def pick_game(games: list[tuple[str, str]], query: str) -> str | None:
    """The game a query most plausibly means: same name, else all its words, else the first."""
    wanted = _WORDS.findall(query.lower())
    for game_id, name in games:
        if _WORDS.findall(name.lower()) == wanted:
            return game_id
    for game_id, name in games:
        if set(wanted) <= set(_WORDS.findall(name.lower())):
            return game_id
    return games[0][0] if games else None


def _amount(value: object) -> float | None:
    try:
        amount = float(value)  # the site sends prices as strings: "12.14"
    except (TypeError, ValueError):
        return None
    return amount if amount > 0 else None


def _price(offer: dict) -> float | None:
    """What the buyer pays: with payment fees when the shop adds some (cheapest payment method)."""
    fees = offer.get("feePrices")
    with_fees = [a for a in map(_amount, fees.values()) if a is not None] if isinstance(fees, dict) else []
    return min(with_fees) if with_fees else _amount(offer.get("finalPrice"))


def _named(table: object, key: object, field: str) -> str:
    entry = table.get(str(key)) if isinstance(table, dict) else None
    value = entry.get(field) if isinstance(entry, dict) else None
    return " ".join(value.split()) if isinstance(value, str) else ""


def _title(offer: dict, data: dict) -> str:
    platform = _named(data.get("platforms"), offer.get("platformId"), "title")
    support = _named(data.get("platforms"), offer.get("platformId"), "support")
    if platform and support and support.lower() not in platform.lower():
        platform = f"{platform} ({support})"
    special_ids = offer.get("specialIds") if isinstance(offer.get("specialIds"), list) else []
    name = str(offer.get("objectName") or "").strip()
    extra = str(offer.get("extraId") or "")
    if name and extra:
        name += " [DLC]" if extra.startswith("dlc") else " [extra]"
    parts = [
        name,
        platform,
        *(_named(data.get("specials"), special_id, "title") for special_id in special_ids),
        str(offer.get("displayRegion") or "").strip(),
        "boîte" if offer.get("box") else "",
        str(offer.get("conditionLabel") or "").strip(),
        f"code {offer['discountCode']}" if offer.get("discountCode") else "",
    ]
    return " - ".join(part for part in parts if part)[:MAX_TITLE_LENGTH]


def parse_prices(text: str) -> list[Listing]:
    try:
        data = json.loads(text).get("priceListData")
    except (ValueError, AttributeError) as exc:
        raise ScrapeError("DLCompare did not answer with its price list") from exc
    if not isinstance(data, dict) or not isinstance(data.get("prices"), list):
        raise ScrapeError("DLCompare did not answer with its price list")
    out = []
    for offer in data["prices"][:MAX_OFFERS]:
        if not isinstance(offer, dict) or not isinstance(offer.get("id"), int) or offer.get("isSuspicious"):
            continue
        price = _price(offer)
        title = _title(offer, data)
        if price is None or not title:
            continue
        shop = _named(data.get("shops"), offer.get("shopId"), "name")
        out.append(Listing(
            site=f"{LABEL}/{shop}" if shop else LABEL,
            title=title,
            price=price,
            url=offer_url(offer["id"]),
            in_stock=not offer.get("unavailableLabel"),
            source=KEY,
        ))
    return out


def search(ctx: FetchContext, query: str, url: str | None = None) -> list[Listing]:
    page = _GAME_PAGE.search(url) if url else None
    if page:
        game_id = page.group(1)
    else:
        game_id = pick_game(parse_search(ctx.get_as_browser(url or search_url(query))), query)
        if game_id is None:
            return []
    return parse_prices(ctx.get_as_browser(prices_url(game_id)))


SCRAPER = SiteScraper(
    key=KEY,
    label=LABEL,
    kind=KIND_COMPARATOR,
    domains=("dlcompare.fr",),
    description=(
        "Key shops compared (Instant Gaming, Eneba, Kinguin, G2A...): one line per offer, with platform, "
        "edition, region and whether it is a key, an account or a boxed copy. Use the filters to keep what you want."
    ),
    search=search,
    url_help="The game's page, e.g. https://www.dlcompare.fr/jeux/100007044/acheter-elden-ring (else the first search result is used).",
    group=GROUP,
)
