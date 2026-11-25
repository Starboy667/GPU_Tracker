"""Generic product-page extraction: JSON-LD shapes, meta fallbacks, CSS selector."""
from __future__ import annotations

import json

import pytest

from pricewatch.scrapers import parse_product_page, scrape_product_page, site_label
from pricewatch.scrapers.base import SOURCE_PRODUCT_PAGE, FetchContext, Listing, ScrapeError

URL = "https://www.shop.example/tv/lg-oled55c5.html"


def page(*json_ld, head: str = "", body: str = "", title: str = "Page title") -> str:
    scripts = "".join(
        f'<script type="application/ld+json">{block if isinstance(block, str) else json.dumps(block)}</script>'
        for block in json_ld
    )
    return f"<html><head><title>{title}</title>{head}{scripts}</head><body>{body}</body></html>"


def product(**extra) -> dict:
    return {"@context": "https://schema.org", "@type": "Product", "name": "LG OLED55C5", **extra}


def offer(price, availability: str | None = "https://schema.org/InStock", **extra) -> dict:
    out = {"@type": "Offer", "price": price, "priceCurrency": "EUR", **extra}
    if availability:
        out["availability"] = availability
    return out


# --------------------------------------------------------------------------- JSON-LD shapes

def test_single_product_with_offer_dict():
    listing = parse_product_page(page(product(offers=offer(1299.0))), URL)
    assert listing == Listing("shop.example", "LG OLED55C5", 1299.0, URL, True, SOURCE_PRODUCT_PAGE)


def test_top_level_list_skips_other_types():
    blocks = [{"@type": "BreadcrumbList", "itemListElement": []}, product(offers=offer("1 199,90"))]
    listing = parse_product_page(page(blocks), URL)
    assert listing.price == 1199.90
    assert listing.title == "LG OLED55C5"


def test_graph_and_type_as_list():
    graph = {
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "WebSite", "name": "Shop"},
            {"@type": ["Product", "Thing"], "name": "LG OLED55C5", "offers": [offer("999.50")]},
        ],
    }
    listing = parse_product_page(page(graph), URL)
    assert (listing.price, listing.in_stock) == (999.50, True)


def test_product_nested_under_main_entity():
    block = {"@type": "ItemPage", "mainEntity": product(offers=offer(450))}
    assert parse_product_page(page(block), URL).price == 450.0


def test_type_with_schema_url_prefix():
    block = product(offers=offer(450))
    block["@type"] = "http://schema.org/Product"
    assert parse_product_page(page(block), URL).price == 450.0


def test_offer_list_takes_lowest_price():
    listing = parse_product_page(page(product(offers=[offer(1300), offer("1250.00"), offer(1400)])), URL)
    assert listing.price == 1250.0


def test_offer_list_prefers_an_offer_that_can_be_bought():
    offers = [offer(900, "https://schema.org/OutOfStock"), offer(1000, "https://schema.org/InStock")]
    listing = parse_product_page(page(product(offers=offers)), URL)
    assert (listing.price, listing.in_stock) == (1000.0, True)


def test_all_offers_out_of_stock_still_reports_the_lowest():
    offers = [offer(900, "https://schema.org/OutOfStock"), offer(800, "https://schema.org/SoldOut")]
    listing = parse_product_page(page(product(offers=offers)), URL)
    assert (listing.price, listing.in_stock) == (800.0, False)


def test_aggregate_offer_low_price():
    aggregate = {"@type": "AggregateOffer", "lowPrice": "1089,00", "highPrice": "1499", "offerCount": 7}
    listing = parse_product_page(page(product(offers=aggregate)), URL)
    assert (listing.price, listing.in_stock) == (1089.0, None)


def test_aggregate_offer_with_nested_offers():
    aggregate = {"@type": "AggregateOffer", "offers": [offer(700), offer(650, "http://schema.org/LimitedAvailability")]}
    listing = parse_product_page(page(product(offers=aggregate)), URL)
    assert (listing.price, listing.in_stock) == (650.0, True)


def test_price_specification_dict_and_list():
    spec = {"@type": "Offer", "priceSpecification": {"@type": "UnitPriceSpecification", "price": "329.99"}}
    assert parse_product_page(page(product(offers=spec)), URL).price == 329.99
    spec_list = {"@type": "Offer", "priceSpecification": [{"price": 349.0}, {"price": 339.0}]}
    assert parse_product_page(page(product(offers=spec_list)), URL).price == 339.0


def test_product_group_variants_take_lowest():
    group = {
        "@type": "ProductGroup",
        "name": "LG OLED C5",
        "hasVariant": [
            {"@type": "Product", "name": "LG OLED65C5", "offers": offer(1899)},
            {"@type": "Product", "name": "LG OLED55C5", "offers": offer(1299)},
        ],
    }
    listing = parse_product_page(page(group), URL)
    assert (listing.title, listing.price) == ("LG OLED C5", 1299.0)


def test_product_group_prefers_the_variant_of_this_url():
    group = {
        "@type": "ProductGroup",
        "name": "LG OLED C5",
        "hasVariant": [
            {"@type": "Product", "name": "LG OLED48C5", "url": "https://www.shop.example/tv/lg-oled48c5.html",
             "offers": offer(999)},
            {"@type": "Product", "name": "LG OLED55C5", "url": URL + "#avis", "offers": offer(1299)},
        ],
    }
    assert parse_product_page(page(group), URL).price == 1299.0


@pytest.mark.parametrize(
    "availability, expected",
    [
        ("https://schema.org/InStock", True),
        ("http://schema.org/LimitedAvailability", True),
        ("OnlineOnly", True),
        ("schema:InStoreOnly", True),
        ("https://schema.org/OutOfStock", False),
        ("SoldOut", False),
        ("https://schema.org/Discontinued", False),
        ("https://schema.org/PreOrder", False),
        ("https://schema.org/BackOrder", False),
        ("https://schema.org/SomethingElse", None),
        (None, None),
    ],
)
def test_availability_mapping(availability, expected):
    assert parse_product_page(page(product(offers=offer(10, availability))), URL).in_stock is expected


def test_malformed_json_block_is_skipped():
    listing = parse_product_page(page('{"@type": "Product", "name": oops', product(offers=offer(42))), URL)
    assert listing.price == 42.0


def test_other_scripts_are_ignored_and_the_type_attribute_is_read_loosely():
    head = (
        '<script>var product = {"@type": "Product", "offers": {"price": 1}};</script>'
        '<script type="text/javascript">window.price = 2;</script>'
        '<script type="Application/LD+JSON; charset=utf-8">%s</script>' % json.dumps(product(offers=offer(88)))
    )
    assert parse_product_page(page(head=head), URL).price == 88.0


def test_first_product_with_a_price_wins():
    blocks = [product(name="No offer here"), product(name="Second", offers=offer(77))]
    listing = parse_product_page(page(*blocks), URL)
    assert (listing.title, listing.price) == ("Second", 77.0)


@pytest.mark.parametrize("bad", [0, -5, "gratuit", None, True, "", []])
def test_unusable_prices_are_ignored(bad):
    with pytest.raises(ScrapeError, match="no price found"):
        parse_product_page(page(product(offers={"@type": "Offer", "price": bad})), URL)


def test_related_products_are_not_mistaken_for_the_page_product():
    block = product(isRelatedTo=[product(name="Cheap cable", offers=offer(5))], offers=offer(1299))
    assert parse_product_page(page(block), URL).price == 1299.0


def test_availability_given_as_an_object():
    listed = offer(10, None)
    listed["availability"] = {"@id": "https://schema.org/OutOfStock"}
    assert parse_product_page(page(product(offers=listed)), URL).in_stock is False


def test_non_dict_entries_are_ignored():
    blocks = ["just a string", 42, None, product(offers=["nope", None, offer(12)])]
    assert parse_product_page(page(blocks), URL).price == 12.0


@pytest.mark.parametrize(
    "wrapper",
    ["//<![CDATA[\n%s\n//]]>", "<![CDATA[%s]]>", "<!--\n%s\n-->", "\n  %s;\n"],
)
def test_json_ld_wrapped_in_cdata_comments_or_ending_with_a_semicolon(wrapper):
    block = wrapper % json.dumps(product(offers=offer(64)))
    assert parse_product_page(page(block), URL).price == 64.0


def test_json_ld_with_raw_line_breaks_inside_strings():
    block = '{"@type": "Product", "name": "LG\nOLED55C5", "offers": {"price": "1299.00"}}'
    listing = parse_product_page(page(block), URL)
    assert (listing.title, listing.price) == ("LG OLED55C5", 1299.0)


def test_html_entities_in_names_are_decoded():
    listing = parse_product_page(page(product(name="Barre de son Bang &amp; Olufsen", offers=offer(1))), URL)
    assert listing.title == "Barre de son Bang & Olufsen"


def test_absurdly_nested_json_ld_is_cut_off():
    nested_graph = product(offers=offer(5))
    nested_offer = offer(5)
    for _ in range(20):
        nested_graph = {"@graph": [nested_graph]}
        nested_offer = {"@type": "AggregateOffer", "offers": [nested_offer]}
    for block in (nested_graph, product(offers=nested_offer)):
        with pytest.raises(ScrapeError, match="no price found"):
            parse_product_page(page(block), URL)


def test_product_group_with_unusable_variant_urls_uses_every_variant():
    group = {
        "@type": "ProductGroup",
        "name": "LG OLED C5",
        "hasVariant": [
            {"@type": "Product", "url": "http://[::1", "offers": offer(1899)},
            {"@type": "Product", "url": 12, "offers": offer(1299)},
        ],
    }
    assert parse_product_page(page(group), URL).price == 1299.0


def test_product_group_without_variant_offers_uses_its_own_offers():
    group = {"@type": "ProductGroup", "name": "LG OLED C5", "hasVariant": [{"@type": "Product"}], "offers": offer(500)}
    assert parse_product_page(page(group), URL).price == 500.0


# --------------------------------------------------------------------------- meta fallbacks

def test_meta_product_price_amount():
    head = (
        '<meta property="og:title" content="LG OLED55C5 - Shop">'
        '<meta property="product:price:amount" content="1 249,99">'
        '<meta property="product:availability" content="in stock">'
    )
    listing = parse_product_page(page(head=head), URL)
    assert listing == Listing("shop.example", "LG OLED55C5 - Shop", 1249.99, URL, True, SOURCE_PRODUCT_PAGE)


def test_meta_og_price_amount_and_oos():
    head = '<meta property="og:price:amount" content="89.90"><meta property="og:availability" content="oos">'
    listing = parse_product_page(page(head=head), URL)
    assert (listing.price, listing.in_stock, listing.title) == (89.90, False, "Page title")


def test_microdata_itemprop_content_and_link_availability():
    body = (
        '<div itemscope itemtype="https://schema.org/Product">'
        '<span itemprop="price" content="549.00">549 €</span>'
        '<link itemprop="availability" href="http://schema.org/OutOfStock"></div>'
    )
    listing = parse_product_page(page(body=body), URL)
    assert (listing.price, listing.in_stock) == (549.0, False)


def test_microdata_itemprop_text_and_french_availability_text():
    body = '<span itemprop="price">1 079€95</span><span itemprop="availability">En stock</span>'
    listing = parse_product_page(page(body=body), URL)
    assert (listing.price, listing.in_stock) == (1079.95, True)


def test_json_ld_without_price_falls_back_to_meta_but_keeps_its_name():
    head = '<meta property="product:price:amount" content="15.5">'
    listing = parse_product_page(page(product(name="From JSON-LD"), head=head), URL)
    assert (listing.title, listing.price) == ("From JSON-LD", 15.5)


def test_no_price_anywhere():
    with pytest.raises(ScrapeError, match=r"no price found on this page \(try adding a CSS selector\)"):
        parse_product_page(page(body="<p>Nothing to see</p>"), URL)


# --------------------------------------------------------------------------- CSS selector

def test_selector_reads_text():
    body = '<div class="buybox"><span class="now">1 499,00 €</span></div><span class="was">1 999,00 €</span>'
    listing = parse_product_page(page(body=body), URL, ".buybox .now")
    assert (listing.price, listing.in_stock, listing.title) == (1499.0, None, "Page title")


def test_selector_skips_empty_matches():
    # Amazon renders blank ".a-offscreen" spans before the one holding the price.
    body = '<span class="a-offscreen"></span><span class="a-offscreen"> </span><span class="a-offscreen">1 024,99 €</span>'
    assert parse_product_page(page(body=body), URL, ".a-offscreen").price == 1024.99


def test_selector_prefers_content_attribute():
    body = '<data class="p" content="12.34">douze euros</data>'
    assert parse_product_page(page(body=body), URL, ".p").price == 12.34


def test_selector_wins_over_json_ld_but_keeps_its_title_and_stock():
    body = '<b id="member-price">1 100 €</b>'
    listing = parse_product_page(page(product(offers=offer(1299)), body=body), URL, "#member-price")
    assert listing == Listing("shop.example", "LG OLED55C5", 1100.0, URL, True, SOURCE_PRODUCT_PAGE)


def test_blank_selector_is_ignored():
    assert parse_product_page(page(product(offers=offer(5))), URL, "   ").price == 5.0


def test_selector_matching_nothing():
    with pytest.raises(ScrapeError, match="matches nothing"):
        parse_product_page(page(product(offers=offer(5))), URL, ".missing")


def test_selector_invalid_syntax():
    with pytest.raises(ScrapeError, match="not a valid CSS selector"):
        parse_product_page(page(body="<p>x</p>"), URL, "div[[")


def test_selector_element_without_a_price():
    with pytest.raises(ScrapeError, match="no price in the element"):
        parse_product_page(page(body='<p class="p">Prix sur demande</p>'), URL, ".p")


# --------------------------------------------------------------------------- titles, labels, errors

def test_title_is_collapsed_and_trimmed():
    listing = parse_product_page(page(product(name="  LG \n OLED  " + "x" * 400, offers=offer(1))), URL)
    assert listing.title.startswith("LG OLED x")
    assert len(listing.title) == 300


def test_title_falls_back_to_the_site_label():
    html = '<html><head><meta property="og:price:amount" content="3"></head></html>'
    assert parse_product_page(html, URL).title == "shop.example"


@pytest.mark.parametrize(
    "url, label",
    [
        ("https://www.boulanger.com/ref/1234", "boulanger.com"),
        ("https://WWW.Fnac.COM/a1", "fnac.com"),
        ("http://shop.son-video.com/x", "shop.son-video.com"),
        ("https://www.amazon.fr:443/dp/B0", "amazon.fr"),
        ("not a url", ""),
        ("http://[::1", ""),
        (None, ""),
        ("", ""),
    ],
)
def test_site_label(url, label):
    assert site_label(url) == label


def test_unexpected_parser_failure_becomes_a_scrape_error(monkeypatch):
    import pricewatch.scrapers.product_page as product_page

    def boom(*args, **kwargs):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(product_page, "BeautifulSoup", boom)
    with pytest.raises(ScrapeError, match="could not read this page"):
        parse_product_page("<html></html>", URL)


# --------------------------------------------------------------------------- fetch wrapper

class FakeContext(FetchContext):
    def __init__(self, html: str | None = None, error: Exception | None = None):
        super().__init__(sleep=lambda seconds: None)
        self.html, self.error, self.calls = html, error, []

    def get_as_browser(self, url: str) -> str:
        self.calls.append(url)
        if self.error:
            raise self.error
        return self.html


def test_scrape_product_page_fetches_as_browser():
    ctx = FakeContext(page(product(offers=offer(1299))))
    listing = scrape_product_page(ctx, URL)
    assert listing.price == 1299.0
    assert ctx.calls == [URL]


def test_scrape_product_page_passes_the_selector():
    ctx = FakeContext(page(body='<i class="p">20 €</i>'))
    assert scrape_product_page(ctx, URL, ".p").price == 20.0


def test_scrape_product_page_propagates_scrape_errors():
    with pytest.raises(ScrapeError, match="HTTP 403"):
        scrape_product_page(FakeContext(error=ScrapeError("HTTP 403 (blocked?)")), URL)


def test_scrape_product_page_wraps_unexpected_errors():
    with pytest.raises(ScrapeError, match="could not fetch this page"):
        scrape_product_page(FakeContext(error=OSError("boom")), URL)
