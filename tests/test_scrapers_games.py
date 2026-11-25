"""Game sources: Steam's store API and DLCompare's price list, against saved (trimmed) answers."""
import json
import re
from pathlib import Path

import pytest

from pricewatch import deals, netsafe
from pricewatch.models import Watch
from pricewatch.presets import PC_GAME_EXCLUDE, PRESETS_BY_KEY
from pricewatch.scrapers import DEFAULT_SITES, SCRAPERS, dlcompare, steam, validate_site_url
from pricewatch.scrapers.base import KIND_COMPARATOR, FetchContext, Listing, ScrapeError

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class Pages(FetchContext):
    """A context that serves canned bodies and records what was asked."""

    def __init__(self, pages: dict[str, str]):
        super().__init__(sleep=lambda seconds: None)
        self.pages, self.asked = pages, []

    def _serve(self, url: str) -> str:
        self.asked.append(url)
        if url not in self.pages:
            raise ScrapeError("HTTP 404")
        return self.pages[url]

    get = _serve
    get_as_browser = _serve


# ------------------------------------------------------------------ registry

def test_game_sources_are_registered_but_not_defaults():
    assert list(SCRAPERS)[-2:] == ["steam", "dlcompare"]
    assert "steam" not in DEFAULT_SITES and "dlcompare" not in DEFAULT_SITES and len(DEFAULT_SITES) == 7
    assert SCRAPERS["steam"].group == SCRAPERS["dlcompare"].group == "Video games"
    assert SCRAPERS["ldlc"].group == "Shops and comparators"
    assert SCRAPERS["dlcompare"].kind == KIND_COMPARATOR
    for key in ("steam", "dlcompare"):
        assert SCRAPERS[key].description and SCRAPERS[key].url_help


def test_url_overrides_must_stay_on_the_site(public_dns):
    assert validate_site_url("steam", "https://store.steampowered.com/app/1245620/ELDEN_RING/")
    assert validate_site_url("dlcompare", "https://www.dlcompare.fr/jeux/100007044/acheter-elden-ring")
    for key, url in (("steam", "https://steampowered.com.evil.example/app/1/"), ("dlcompare", "https://www.dlcompare.com.evil.example/")):
        with pytest.raises(netsafe.UnsafeURLError):
            validate_site_url(key, url)


# ------------------------------------------------------------------ Steam

def test_steam_search_results():
    listings = steam.parse_search(fixture("steam-search.json"))
    assert listings[0] == Listing("Steam", "ELDEN RING", 59.99, "https://store.steampowered.com/app/1245620/", True, "steam")
    assert [l.title for l in listings[:3]] == ["ELDEN RING", "ELDEN RING Shadow of the Erdtree", "ELDEN RING NIGHTREIGN"]
    assert all(l.source == "steam" and l.in_stock for l in listings)


def test_steam_search_skips_what_has_no_euro_price():
    body = json.dumps({"items": [
        {"type": "app", "id": 1, "name": "Free game"},
        {"type": "app", "id": 2, "name": "Dollar game", "price": {"currency": "USD", "initial": 999, "final": 999}},
        {"type": "app", "id": 3, "name": "Zero", "price": {"currency": "EUR", "initial": 0, "final": 0}},
        {"type": "sub", "id": 4, "name": "A package", "price": {"currency": "EUR", "initial": 2999, "final": 1499}},
        {"type": "app", "id": "5", "name": "Bad id", "price": {"currency": "EUR", "final": 100}},
        "junk",
    ]})
    assert steam.parse_search(body) == [Listing("Steam", "A package", 14.99, "https://store.steampowered.com/sub/4/", True, "steam")]
    assert steam.parse_search("{}") == [] and steam.parse_search("[]") == []
    with pytest.raises(ScrapeError, match="JSON"):
        steam.parse_search("<html>maintenance</html>")


def test_steam_exact_name_wins_over_dlc_and_sequels():
    listings = steam.parse_search(fixture("steam-search.json"))
    assert [l.title for l in steam.prefer_exact_name(listings, "Elden Ring")] == ["ELDEN RING"]
    assert steam.prefer_exact_name(listings, "elden") == listings  # no exact name: keep everything


def test_steam_search_by_name_and_by_page():
    ctx = Pages({
        steam.search_url("elden ring"): fixture("steam-search.json"),
        steam.details_url("app", "1245620"): fixture("steam-app.json"),
    })
    assert [l.title for l in steam.search(ctx, "elden ring")] == ["ELDEN RING"]
    by_page = steam.search(ctx, "ignored", "https://store.steampowered.com/app/1245620/ELDEN_RING/")
    assert by_page == [Listing("Steam", "ELDEN RING", 59.99, "https://store.steampowered.com/app/1245620/", True, "steam")]
    assert "cc=FR" in ctx.asked[0] and "appids=1245620" in ctx.asked[1] and "cc=fr" in ctx.asked[1]
    with pytest.raises(ScrapeError, match="game page"):
        steam.search(ctx, "x", "https://store.steampowered.com/bundle/123/")


def test_steam_details_edge_cases():
    assert steam.parse_details(json.dumps({"7": {"success": True, "data": {"name": "Free", "is_free": True}}}), "app", "7") == []
    package = json.dumps({"9": {"success": True, "data": {"name": "Pack", "price": {"currency": "EUR", "final": 4999}}}})
    assert steam.parse_details(package, "sub", "9")[0].price == 49.99
    assert "packagedetails?packageids=9" in steam.details_url("sub", "9")
    for body in ('{"7": {"success": false}}', "{}", "null"):
        with pytest.raises(ScrapeError, match="does not know this page"):
            steam.parse_details(body, "app", "7")


# ------------------------------------------------------------------ DLCompare

def test_dlcompare_search_lists_games_in_order():
    assert dlcompare.parse_search(fixture("dlcompare-search.html")) == [
        ("100007044", "Elden Ring"), ("100034176", "Elden Ring Nightreign"),
        ("100037252", "ELDEN RING NIGHTREIGN The Forsaken Hollows"), ("100017044", "Elden Ring Shadow of the Erdtree"),
    ]
    assert dlcompare.parse_search("<html></html>") == []


def test_dlcompare_picks_the_game_the_query_means():
    games = dlcompare.parse_search(fixture("dlcompare-search.html"))
    assert dlcompare.pick_game(games, "elden ring") == "100007044"
    assert dlcompare.pick_game(games, "Elden Ring: Nightreign") == "100034176"
    assert dlcompare.pick_game(games, "erdtree") == "100017044"  # all its words appear in one name
    assert dlcompare.pick_game(games, "zelda") == "100007044"  # nothing fits: first result
    assert dlcompare.pick_game([], "zelda") is None


def test_dlcompare_offers_spell_out_what_is_sold():
    by_title = {l.title: l for l in dlcompare.parse_prices(fixture("dlcompare-prices.json"))}
    key = by_title["Elden Ring - Steam (PC) - Standard Edition - Europe - code SEAL17DLC"]
    assert (key.site, key.source, key.in_stock) == ("DLCompare/GAMESEAL", "dlcompare", True)
    assert key.url == "https://www.dlcompare.fr/price/15505563/serve"
    titles = list(by_title)
    assert "Elden Ring - Steam (PC) - Vente de compte - Global - code dlcompare" in titles
    assert "Elden Ring - Gift (PC) - Standard Edition - Global - code RAB12DLC" in titles
    assert "Elden Ring - PlayStation 5 (PS5) - Standard Edition - France - boîte - Occasion" in titles
    assert any(t.startswith("Elden Ring Shadow of the Erdtree [DLC] - Steam (PC)") for t in titles)
    assert any(t.startswith("Elden Ring - Pre-order Bonus [extra] - ") for t in titles)
    assert "Elden Ring - Xbox Series X|S - Vente de compte - Rest of World - code RAB18DLC" in titles


def test_dlcompare_price_includes_payment_fees_when_there_are_some():
    raw = {o["id"]: o for o in json.loads(fixture("dlcompare-prices.json"))["priceListData"]["prices"]}
    listings = {int(l.url.split("/")[-2]): l for l in dlcompare.parse_prices(fixture("dlcompare-prices.json"))}
    with_fees = raw[15505563]
    assert float(with_fees["finalPrice"]) < listings[15505563].price == min(map(float, with_fees["feePrices"].values()))
    without = next(o for o in raw.values() if not o["feePrices"])
    assert listings[without["id"]].price == float(without["finalPrice"])


def test_dlcompare_skips_unusable_offers():
    data = {"priceListData": {"platforms": {"2": {"title": "Steam", "support": "PC"}}, "shops": {}, "specials": {}, "prices": [
        {"id": 1, "shopId": 9, "platformId": 2, "finalPrice": "10.00", "objectName": "Game", "isSuspicious": True},
        {"id": 2, "shopId": 9, "platformId": 2, "finalPrice": "free", "objectName": "Game"},
        {"id": "3", "shopId": 9, "platformId": 2, "finalPrice": "10.00", "objectName": "Game"},
        {"id": 4, "shopId": 9, "platformId": 2, "finalPrice": "10.00", "objectName": "Game", "unavailableLabel": "Rupture"},
        "junk",
    ]}}
    [only] = dlcompare.parse_prices(json.dumps(data))
    assert (only.site, only.title, only.price, only.in_stock) == ("DLCompare", "Game - Steam (PC)", 10.0, False)
    for body in ("<html>", "{}", '{"priceListData": {"prices": "none"}}', "[]"):
        with pytest.raises(ScrapeError, match="price list"):
            dlcompare.parse_prices(body)


def test_dlcompare_search_by_name_then_prices():
    ctx = Pages({
        dlcompare.search_url("elden ring"): fixture("dlcompare-search.html"),
        dlcompare.prices_url("100007044"): fixture("dlcompare-prices.json"),
    })
    assert len(dlcompare.search(ctx, "elden ring")) == 10
    assert ctx.asked == [dlcompare.search_url("elden ring"), dlcompare.prices_url("100007044")]


def test_dlcompare_game_page_address_skips_the_search():
    ctx = Pages({dlcompare.prices_url("100007044"): fixture("dlcompare-prices.json")})
    listings = dlcompare.search(ctx, "anything", "https://www.dlcompare.fr/jeux/100007044/acheter-elden-ring")
    assert len(listings) == 10 and ctx.asked == [dlcompare.prices_url("100007044")]
    empty = Pages({dlcompare.search_url("nothing here"): "<html></html>"})
    assert dlcompare.search(empty, "nothing here") == []


# ------------------------------------------------------------------ the PC game preset

def test_pc_game_preset_keeps_only_pc_keys_of_the_game():
    preset = PRESETS_BY_KEY["game"]
    assert preset.sites == ("steam", "dlcompare") and deals.regex_problem(preset.exclude) is None
    watch = Watch(name="Elden Ring", query="Elden Ring", match=deals.auto_match("Elden Ring"), exclude=PC_GAME_EXCLUDE)
    kept = [l.title for l in dlcompare.parse_prices(fixture("dlcompare-prices.json")) if deals.rejection_reason(l, watch) is None]
    assert kept == [
        "Elden Ring - Steam (PC) - Standard Edition - Europe - code SEAL17DLC",
        "Elden Ring - Steam (PC) - Standard Edition - EMEA",
    ]
    steam_results = steam.prefer_exact_name(steam.parse_search(fixture("steam-search.json")), watch.query)
    assert [l.title for l in steam_results if deals.rejection_reason(l, watch) is None] == ["ELDEN RING"]
    assert not re.search(PC_GAME_EXCLUDE, "Hades II - Steam (PC) - Standard Edition - Global", re.I)
