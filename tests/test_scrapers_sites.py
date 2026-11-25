"""Site parsers against trimmed copies of real pages (tests/fixtures, see tools/make_fixtures.py).

The expected values come from the committed fixtures: update them after regenerating.
"""
from __future__ import annotations

import socket
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pricewatch import netsafe
from pricewatch.scrapers import (
    DEALABS_GPU_FEED,
    DEFAULT_SITES,
    SCRAPERS,
    alternate,
    dealabs,
    grosbill,
    idealo,
    ldlc,
    materiel,
    topachat,
    validate_site_url,
)
from pricewatch.scrapers.base import (
    KIND_COMPARATOR,
    KIND_DEALS,
    KIND_RETAILER,
    FetchContext,
    Listing,
)

FIXTURES = Path(__file__).parent / "fixtures"
NOON = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)  # a few hours after the Dealabs fixture's newest item


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def summary(listings: list[Listing]) -> list[tuple[str, float, bool | None]]:
    return [(l.title, l.price, l.in_stock) for l in listings]


# --------------------------------------------------------------------------- fixtures of real pages

def test_ldlc_fixture():
    listings = ldlc.parse(fixture("ldlc.html"))
    assert listings[0] == Listing(
        "LDLC", "Gigabyte GeForce RTX 5070 WINDFORCE OC SFF 12G", 1119.95,
        "https://www.ldlc.com/fiche/PB00671172.html", True, "ldlc",
    )
    assert summary(listings) == [
        ("Gigabyte GeForce RTX 5070 WINDFORCE OC SFF 12G", 1119.95, True),
        ("Gigabyte GeForce RTX 5070 GAMING OC 12G", 1189.95, True),
        ("MSI GeForce RTX 5070 12G VENTUS 2X OC", 1149.95, True),
        ("Gigabyte GeForce RTX 5070 Ti WINDFORCE OC SFF 16G", 1699.95, False),
    ]


def test_materiel_fixture():
    listings = materiel.parse(fixture("materiel.html"))
    assert listings[0] == Listing(
        "Materiel.net", "MSI GeForce RTX 5070 12G VENTUS 2X OC", 1149.95,
        "https://www.materiel.net/produit/202502180051.html", True, "materiel",
    )
    assert summary(listings) == [
        ("MSI GeForce RTX 5070 12G VENTUS 2X OC", 1149.95, True),
        ("MSI GeForce RTX 5070 Ti 16G GAMING TRIO OC", 1829.95, True),
        ("MSI GeForce RTX 5070 Ti 16G VENTUS 3X OC", 1749.95, True),
        ("Gigabyte GeForce RTX 5070 Ti WINDFORCE OC SFF 16G", 1699.95, False),
    ]


def test_topachat_fixture_keeps_accents():
    listings = topachat.parse(fixture("topachat.html"))
    assert listings[0] == Listing(
        "TopAchat", "Gainward GeForce RTX 5070 Python III", 1059.99,
        "https://www.topachat.com/pages/detail2_cat_est_micro_puis_rubrique_est_wgfx_pcie_puis_ref_est_in20027484.html",
        True, "topachat",
    )
    assert summary(listings) == [
        ("Gainward GeForce RTX 5070 Python III", 1059.99, True),
        ("Asus GeForce RTX 5070 DUAL O12G", 1149.99, True),
        ("Gainward GeForce RTX 5070 PYTHON III OC", 1099.99, True),
        ("PC Gamer STYGIAN (Edition limitée)", 2399.99, True),
    ]


def test_grosbill_fixture():
    listings = grosbill.parse(fixture("grosbill.html"))
    assert listings[0] == Listing(
        "Grosbill", "PNY RTX 5070 12GB Overclocked Triple Fan", 1249.99,
        "https://www.grosbill.com/pny-rtx-5070-12gb-overclocked-triple-fan-150251", False, "grosbill",
    )
    assert summary(listings) == [
        ("PNY RTX 5070 12GB Overclocked Triple Fan", 1249.99, False),
        ("PNY RTX 5070Ti 16GB ARGB Overclocked Triple Fan", 1499.99, False),
        ("PNY RTX 5070Ti 16GB Overclocked Triple Fan V1", 1599.99, False),  # data-price="1599.991"
        ("Gigabyte GeForce RTX 5070 WINDFORCE OC SFF 12G", 979.99, True),
    ]


def test_alternate_fixture():
    listings = alternate.parse(fixture("alternate.html"))
    assert listings[0] == Listing(
        "Alternate", "ZOTAC Geforce RTX 5070 SOLID OC, Carte graphique", 1029.0,
        "https://www.alternate.fr/ZOTAC/Geforce-RTX-5070-SOLID-OC-Carte-graphique/html/product/100117395",
        True, "alternate",
    )
    assert summary(listings) == [
        ("ZOTAC Geforce RTX 5070 SOLID OC, Carte graphique", 1029.0, True),
        ("ASUS PRIME GeForce RTX 5070 OC, Carte graphique", 1036.0, True),
        ("ASUS DUAL GeForce RTX 5070 OC, Carte graphique", 933.0, True),
        ("PNY GeForce RTX 5070, Carte graphique", 1118.0, False),
    ]


def test_idealo_fixture():
    listings = idealo.parse(fixture("idealo.html"))
    assert listings[0] == Listing(
        "Idealo (73 offers)", "GigaByte GeForce RTX 5070", 919.99,
        "https://www.idealo.fr/prix/206026491/gigabyte-geforce-rtx-5070.html", None, "idealo",
    )
    assert [(l.site, l.title, l.price) for l in listings] == [
        ("Idealo (73 offers)", "GigaByte GeForce RTX 5070", 919.99),
        ("Idealo (34 offers)", "ASUS GeForce RTX 5070", 899.99),
        ("Idealo (32 offers)", "MSI GeForce RTX 5070", 949.99),
        ("Idealo (56 offers)", "Zotac GeForce RTX 5070", 924.9),
    ]
    assert all(l.in_stock is None for l in listings)


def test_dealabs_fixture():
    listings = dealabs.parse(fixture("dealabs.xml"), now=NOON)
    assert listings[0] == Listing(
        "Dealabs/AliExpress", "Carte graphique SOYO GeForce RTX 3070 8 Go GDDR6 (243,43€ via PayPal)", 263.43,
        "https://www.dealabs.com/bons-plans/carte-graphique-soyo-geforce-rtx-3070-8-go-gddr6-expediee-depuis-la-"
        "france-a-26343eur-24343eur-via-paypal-sur-aliexpress-3468655",
        None, "dealabs",
    )
    assert [(l.site, l.price) for l in listings] == [
        ("Dealabs/AliExpress", 263.43),
        ("Dealabs/Cdiscount", 634.99),
        ("Dealabs/Cdiscount", 689.99),
        ("Dealabs/AliExpress", 296.76),
    ]


def test_dealabs_fixture_drops_deals_older_than_the_limit():
    # Items were posted on 25 Nov at 09:28, 00:47, 00:32 UTC and on 24 Nov at 22:24 UTC.
    three_days_later = datetime(2026, 11, 28, 2, 0, tzinfo=timezone.utc)
    assert [l.price for l in dealabs.parse(fixture("dealabs.xml"), now=three_days_later)] == [263.43]
    assert len(dealabs.parse(fixture("dealabs.xml"), now=three_days_later, max_age_days=7)) == 4
    assert dealabs.parse(fixture("dealabs.xml"), now=three_days_later, max_age_days=1) == []


@pytest.mark.parametrize("module", [ldlc, materiel, topachat, grosbill, alternate, idealo, dealabs])
def test_parsers_return_nothing_for_an_unrelated_page(module):
    assert module.parse("<html><body><p>Maintenance en cours</p></body></html>") == []
    assert module.parse("") == []


# --------------------------------------------------------------------------- edge cases, hand-written markup

def test_ldlc_skips_incomplete_items_and_handles_unknown_stock():
    html = """
    <ul>
      <li class="pdt-item"><h3><a href="/fiche/A.html">No price</a></h3></li>
      <li class="pdt-item"><h3>No link</h3><div class="price">10€00</div></li>
      <li class="pdt-item"><h3><a>No href</a></h3><div class="price">10€00</div></li>
      <li class="pdt-item"><h3><a href="/fiche/B.html">Kept</a></h3><div class="price">1 079€<sup>95</sup></div></li>
    </ul>"""
    assert ldlc.parse(html) == [Listing("LDLC", "Kept", 1079.95, "https://www.ldlc.com/fiche/B.html", None, "ldlc")]


def test_materiel_title_falls_back_to_the_title_element():
    html = """
    <ul>
      <li class="c-products-list__item"><a class="c-product__link" href="/produit/1.html"></a>
        <span class="c-product__title">From the title node</span>
        <span class="o-product__price">549€<sup>95</sup></span>
        <span class="o-availability__value">Rupture</span></li>
      <li class="c-products-list__item"><a class="c-product__link" href="/produit/2.html" title="x"></a></li>
    </ul>"""
    assert materiel.parse(html) == [
        Listing("Materiel.net", "From the title node", 549.95, "https://www.materiel.net/produit/1.html", False, "materiel")
    ]


def test_grosbill_price_text_fallback_and_stock_states():
    html = """
    <article class="product-row"><a class="p-name" href="/a-1">Text price</a>
      <span class="price-main">1 249,99 €</span><span class="dw-status dw-low">x</span></article>
    <article class="product-row" data-price="99.5"><a class="p-name" href="/b-2">No status</a></article>
    <article class="product-row" data-price="12"><span>no name</span></article>
    <article class="product-row"><a class="p-name" href="/c-3">No price</a></article>
    """
    assert grosbill.parse(html) == [
        Listing("Grosbill", "Text price", 1249.99, "https://www.grosbill.com/a-1", True, "grosbill"),
        Listing("Grosbill", "No status", 99.5, "https://www.grosbill.com/b-2", None, "grosbill"),
    ]


def test_alternate_skips_boxes_without_a_name_or_price():
    html = """
    <a class="productBox" href="/p/1"><div class="product-name">Kept</div><span class="price">€ 1.029,00</span></a>
    <a class="productBox" href="/p/2"><span class="price">€ 10,00</span></a>
    <a class="productBox" href="/p/3"><div class="product-name">No price</div></a>
    """
    assert alternate.parse(html) == [Listing("Alternate", "Kept", 1029.0, "https://www.alternate.fr/p/1", None, "alternate")]


def test_idealo_without_an_offer_count_and_with_one_offer():
    def item(href, title, price, info):
        return (
            f'<div data-testid="result-item"><div data-testid="resultItemLink"><a href="{href}">x</a></div>'
            f'<div data-testid="product-summary-title">{title}</div>'
            f'<div data-testid="detailed-price-info">{info}'
            f'<span data-testid="detailed-price-info-price">{price}</span></div></div>'
        )

    html = (
        item("/prix/1/a.html", "Plain", "1 018,00 €", "dès")
        + item("https://www.idealo.fr/prix/2/b.html", "Single", "99,90 €", "1 offre dès")
        + item("/prix/3/c.html", "", "5,00 €", "")
        + item("/prix/4/d.html", "No price", "", "")
    )
    assert idealo.parse(html) == [
        Listing("Idealo", "Plain", 1018.0, "https://www.idealo.fr/prix/1/a.html", None, "idealo"),
        Listing("Idealo (1 offer)", "Single", 99.9, "https://www.idealo.fr/prix/2/b.html", None, "idealo"),
    ]


# --------------------------------------------------------------------------- TopAchat's inline JS

BACKSLASH = chr(92)
JS_E_ACUTE = BACKSLASH + "u00e9"  # the six characters of a JS unicode escape for "é"
JS_GRINNING_FACE = BACKSLASH + "ud83d" + BACKSLASH + "ude00"  # a surrogate pair, as JS writes emoji

@pytest.mark.parametrize(
    "raw, expected",
    [
        (f"Edition limit{JS_E_ACUTE}e", "Edition limitée"),
        ("Edition limitée", "Edition limitée"),  # real UTF-8 must survive untouched
        (r"\/pages\/detail.html", "/pages/detail.html"),
        (r'TV 55\" OLED', 'TV 55" OLED'),
        (r"caf\xe9", "café"),
        (r"a\\b", r"a\b"),
        (r"line\nbreak", "line\nbreak"),
        (f"{JS_GRINNING_FACE} promo", "\U0001f600 promo"),
        ("plain", "plain"),
    ],
)
def test_js_unescape(raw, expected):
    assert topachat.js_unescape(raw) == expected


def js_product(ref, label, *, price=None, url=None, availability=None):
    parts = [f'ref:"{ref}"', f'label:"{label}"']
    if availability:
        parts.append(f'offer:{{availability:{{code:"{availability}",label:"x"}}')
    if price is not None:
        parts.append(f"price:{{price_final:{price}}}")
    if url:
        parts.append(f'url_product:"{url}"')
    return "{" + ",".join(parts) + "}"


def test_topachat_decodes_escapes_and_reads_stock_codes():
    html = "<script>window.__NUXT__={products:[" + ",".join([
        js_product("in1", f'T{JS_E_ACUTE}l{JS_E_ACUTE}viseur 55{BACKSLASH}" OLED', price=999.9, url=r"\/pages\/tv_in1.html", availability="available"),
        js_product("in2", "Carte  avec   espaces", price=10, url="/pages/in2.html", availability="out-of-stock"),
        js_product("in3", "Stock limité", price=20.5, url="/pages/in3.html", availability="limited-stock"),
        js_product("in4", "Sans dispo", price=30, url="/pages/in4.html"),
    ]) + "]}</script>"
    assert topachat.parse(html) == [
        Listing("TopAchat", 'Téléviseur 55" OLED', 999.9, "https://www.topachat.com/pages/tv_in1.html", True, "topachat"),
        Listing("TopAchat", "Carte avec espaces", 10.0, "https://www.topachat.com/pages/in2.html", False, "topachat"),
        Listing("TopAchat", "Stock limité", 20.5, "https://www.topachat.com/pages/in3.html", True, "topachat"),
        Listing("TopAchat", "Sans dispo", 30.0, "https://www.topachat.com/pages/in4.html", None, "topachat"),
    ]


def test_topachat_product_without_price_does_not_borrow_its_neighbour():
    html = js_product("in1", "No price") + "," + js_product("in2", "Priced", price=50, url="/pages/in2.html")
    assert [l.title for l in topachat.parse(html)] == ["Priced"]


def test_topachat_missing_link_falls_back_to_the_page_url():
    html = js_product("in1", "No link", price=50)
    page = "https://www.topachat.com/pages/category.html"
    assert topachat.parse(html, page)[0].url == page
    assert topachat.parse(html)[0].url == "https://www.topachat.com"


# --------------------------------------------------------------------------- Dealabs feed details

def rss(*items: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?><rss xmlns:pepper="http://www.pepper.com/rss" version="2.0">'
        "<channel><title>Dealabs</title>" + "".join(items) + "</channel></rss>"
    )


def rss_item(title="TV LG OLED55C5", price="999€", shop="Boulanger", date="Wed, 25 Nov 2026 10:00:00 +0100", link="https://www.dealabs.com/bons-plans/x-1"):
    merchant = "" if price is None else '<pepper:merchant %s price="%s"/>' % (f'name="{shop}"' if shop else "", price)
    pub = f"<pubDate>{date}</pubDate>" if date else ""
    link_tag = f"<link>{link}</link>" if link else ""
    return f"<item>{merchant}<title>{title}</title>{link_tag}{pub}</item>"


def test_dealabs_item_variants():
    xml = rss(
        rss_item(),
        rss_item(title="No merchant", price=None),
        rss_item(title="Free", price="GRATUIT"),
        rss_item(title="No shop name", shop=None, price="1 349,99€", link="https://www.dealabs.com/bons-plans/x-2"),
        rss_item(title="Unreadable date", date="yesterday-ish", link="https://www.dealabs.com/bons-plans/x-3"),
        rss_item(title="No date", date=None, link="https://www.dealabs.com/bons-plans/x-4"),
        rss_item(title="Naive date", date="Wed, 25 Nov 2026 10:00:00", link="https://www.dealabs.com/bons-plans/x-5"),
        rss_item(title="Old", date="Mon, 02 Nov 2026 10:00:00 +0100"),
        rss_item(title="No link", link=None),
    )
    assert [(l.site, l.title, l.price) for l in dealabs.parse(xml, now=NOON)] == [
        ("Dealabs/Boulanger", "TV LG OLED55C5", 999.0),
        ("Dealabs", "No shop name", 1349.99),
        ("Dealabs/Boulanger", "Unreadable date", 999.0),
        ("Dealabs/Boulanger", "No date", 999.0),
        ("Dealabs/Boulanger", "Naive date", 999.0),
    ]


def test_dealabs_parse_defaults_to_the_current_time():
    assert dealabs.parse(rss(rss_item(date="Mon, 02 Nov 2020 10:00:00 +0100"))) == []


# --------------------------------------------------------------------------- search(): URLs and fetchers

class RecordingContext(FetchContext):
    def __init__(self, body: str = "", **kwargs):
        super().__init__(sleep=lambda seconds: None, **kwargs)
        self.body = body
        self.calls: list[tuple[str, str]] = []

    def get(self, url: str) -> str:
        self.calls.append(("get", url))
        return self.body

    def get_as_browser(self, url: str, *, public_only: bool = False) -> str:
        self.calls.append(("browser", url))
        return self.body


@pytest.mark.parametrize(
    "key, fetcher, expected_url",
    [
        ("ldlc", "get", "https://www.ldlc.com/recherche/lg%20oled55c5%20%2B%20barre/"),
        ("materiel", "get", "https://www.materiel.net/recherche/lg%20oled55c5%20%2B%20barre/"),
        ("topachat", "get", "https://www.topachat.com/pages/recherche.php?q=lg+oled55c5+%2B+barre"),
        ("grosbill", "get", "https://www.grosbill.com/catv2.cgi?mode=recherche&recherche=lg+oled55c5+%2B+barre"),
        ("alternate", "get", "https://www.alternate.fr/listing.xhtml?q=lg+oled55c5+%2B+barre"),
        ("idealo", "browser", "https://www.idealo.fr/prechcat.html?q=lg+oled55c5+%2B+barre"),
        ("dealabs", "get", "https://www.dealabs.com/rss/nouveaux"),
    ],
)
def test_search_builds_the_default_url(key, fetcher, expected_url):
    ctx = RecordingContext()
    assert SCRAPERS[key].search(ctx, "lg oled55c5 + barre", None) == []
    assert ctx.calls == [(fetcher, expected_url)]


# The game sources turn an override into an API call instead (see test_scrapers_games.py).
@pytest.mark.parametrize("key", list(DEFAULT_SITES))
def test_search_uses_a_url_override_as_is(key):
    override = f"https://www.{SCRAPERS[key].domains[0]}/some/category?page=2"
    ctx = RecordingContext()
    SCRAPERS[key].search(ctx, "ignored", override)
    assert [url for _, url in ctx.calls] == [override]


@pytest.mark.parametrize(
    "module, name",
    [(ldlc, "ldlc.html"), (materiel, "materiel.html"), (topachat, "topachat.html"), (grosbill, "grosbill.html"),
     (alternate, "alternate.html"), (idealo, "idealo.html")],
)
def test_search_returns_the_parsed_page(module, name):
    listings = module.search(RecordingContext(fixture(name)), "rtx 5070")
    assert len(listings) == 4
    assert {l.source for l in listings} == {module.KEY}


def test_dealabs_search_fetches_the_feed_once_per_run_and_applies_the_age_limit():
    xml = rss(rss_item(date="Mon, 02 Nov 2020 10:00:00 +0100"))
    ctx = RecordingContext(xml, dealabs_max_age_days=100_000)
    assert len(dealabs.search(ctx, "rtx 5070")) == 1
    assert len(dealabs.search(ctx, "rx 9070")) == 1
    assert ctx.calls == [("get", dealabs.DEFAULT_FEED)]  # second watch served from the run cache
    assert dealabs.search(RecordingContext(xml, dealabs_max_age_days=3), "rtx 5070") == []


def test_dealabs_group_feeds_are_cached_separately():
    ctx = RecordingContext(rss(rss_item()))
    dealabs.search(ctx, "tv", "https://www.dealabs.com/rss/groupe/televiseurs")
    dealabs.search(ctx, "gpu", DEALABS_GPU_FEED)
    dealabs.search(ctx, "tv again", "https://www.dealabs.com/rss/groupe/televiseurs")
    assert [url for _, url in ctx.calls] == ["https://www.dealabs.com/rss/groupe/televiseurs", DEALABS_GPU_FEED]


# --------------------------------------------------------------------------- registry

def test_registry_order_and_defaults():
    hardware = ["ldlc", "materiel", "topachat", "grosbill", "alternate", "idealo", "dealabs"]
    assert list(SCRAPERS) == [*hardware, "steam", "dlcompare"]
    assert DEFAULT_SITES == tuple(hardware)  # game sources are opt-in
    assert DEALABS_GPU_FEED == "https://www.dealabs.com/rss/groupe/cartes-graphiques"


def test_registry_kinds():
    kinds = {key: scraper.kind for key, scraper in SCRAPERS.items()}
    assert kinds == {
        "ldlc": KIND_RETAILER, "materiel": KIND_RETAILER, "topachat": KIND_RETAILER, "grosbill": KIND_RETAILER,
        "alternate": KIND_RETAILER, "idealo": KIND_COMPARATOR, "dealabs": KIND_DEALS,
        "steam": KIND_RETAILER, "dlcompare": KIND_COMPARATOR,
    }


@pytest.mark.parametrize("key", list(SCRAPERS))
def test_registry_entries_are_complete(key):
    scraper = SCRAPERS[key]
    assert scraper.key == key
    assert scraper.label and scraper.description and scraper.url_help
    assert scraper.domains and all("." in d and not d.startswith("www.") for d in scraper.domains)
    assert callable(scraper.search)


@pytest.fixture
def public_dns(monkeypatch):
    def resolve(host, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(netsafe, "_original_getaddrinfo", resolve)


def test_validate_site_url_accepts_the_sites_own_pages(public_dns):
    url = "https://www.topachat.com/pages/produits_cat_est_micro.html"
    assert validate_site_url("topachat", f"  {url} ") == url
    assert validate_site_url("dealabs", "https://www.dealabs.com/rss/groupe/televiseurs")
    assert validate_site_url("ldlc", "https://ldlc.com/recherche/tv/")


@pytest.mark.parametrize(
    "key, url, message",
    [
        ("ldlc", "https://www.materiel.net/recherche/tv/", "must be on ldlc.com"),
        ("ldlc", "https://evil-ldlc.com/recherche/tv/", "must be on ldlc.com"),
        ("ldlc", "https://ldlc.com.evil.net/", "must be on ldlc.com"),
        ("idealo", "ftp://www.idealo.fr/x", "must start with http"),
        ("idealo", "https://user:pw@www.idealo.fr/x", "user name or password"),
        ("nope", "https://www.ldlc.com/", "unknown site 'nope'"),
    ],
)
def test_validate_site_url_rejects(public_dns, key, url, message):
    with pytest.raises(netsafe.UnsafeURLError, match=message):
        validate_site_url(key, url)


def test_validate_site_url_rejects_a_site_domain_pointing_inside(monkeypatch):
    def resolve(host, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.3", 0))]

    monkeypatch.setattr(netsafe, "_original_getaddrinfo", resolve)
    with pytest.raises(netsafe.UnsafeURLError, match="private or reserved"):
        validate_site_url("ldlc", "https://www.ldlc.com/recherche/tv/")
