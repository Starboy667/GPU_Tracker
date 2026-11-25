from pathlib import Path

import pytest

from pricewatch import portability
from pricewatch.presets import DEALABS_GPU_FEED, PRESETS
from pricewatch.scrapers import SCRAPERS, validate_site_url

pytestmark = pytest.mark.usefixtures("public_dns")
ROOT = Path(__file__).resolve().parent.parent


def load(text):
    return portability.import_yaml(text, site_keys=SCRAPERS, validate_site_url=validate_site_url)


def problems(text):
    with pytest.raises(portability.ImportError_) as caught:
        load(text)
    return caught.value.problems


def test_original_gpu_config_imports():
    watches = load((ROOT / "examples" / "gpu-watches.yaml").read_text(encoding="utf-8"))
    by_name = {w.name: w for w in watches}
    assert len(watches) == 10
    card = by_name["RTX 5070"]
    assert card.match == r"RTX\s*5070(?!\s*Ti)" and card.query == "rtx 5070" and card.min_price == 150
    assert card.sites == ("ldlc", "materiel", "topachat", "grosbill", "alternate", "dealabs", "idealo")
    assert card.site_urls["topachat"].startswith("https://www.topachat.com/")
    assert card.site_urls["dealabs"] == DEALABS_GPU_FEED  # the script only ever read the GPU feed
    assert "occasion" in card.exclude  # the global exclude moves onto each watch
    assert by_name["RX 9070"].match.startswith(r"RX\s*9070(?!")  # auto match refuses the XT


def test_export_round_trips():
    watches = load("""
version: 2
watches:
  - name: LG OLED55C5
    sites: [ldlc, idealo]
    exclude: 'support|pied'
    min_price: 100
    max_price: 1100
    deal_drop_pct: 5
    in_stock_only: false
    notify_restock: true
    enabled: false
    urls: {ldlc: 'https://www.ldlc.com/tv/'}
    product_urls:
      - https://shop.example/tv
      - {url: 'https://www.amazon.fr/dp/B0', selector: '.a-price'}
""")
    [tv] = watches
    assert (tv.max_price, tv.deal_drop_pct, tv.in_stock_only, tv.notify_restock, tv.enabled) == (
        1100, 5, False, True, False)
    assert [p.selector for p in tv.product_urls] == ["", ".a-price"]
    assert "dealabs" not in tv.site_urls  # version 2: no legacy GPU feed added
    assert load(portability.export_yaml(watches)) == watches


def test_a_document_without_sites_means_the_original_hardware_shops():
    [w] = load("watches:\n  - {name: RTX 5070}")
    assert w.sites == ("ldlc", "materiel", "topachat", "grosbill", "alternate", "idealo", "dealabs")
    [game] = load("version: 2\nwatches:\n  - {name: Elden Ring, sites: [steam, dlcompare]}")
    assert game.sites == ("steam", "dlcompare")


def test_legacy_and_watch_excludes_are_combined():
    [w] = load("defaults: {exclude: 'PC'}\nwatches:\n  - {name: A, sites: [ldlc], exclude: 'kit'}")
    assert w.exclude == "(?:PC)|(?:kit)"
    [w] = load("defaults: {exclude: 'PC'}\nwatches:\n  - {name: A, sites: [ldlc]}")
    assert w.exclude == "PC"


@pytest.mark.parametrize("text,expected", [
    ("watches: [", "not valid YAML"),
    ("hello: world", "expected a `watches:` list"),
    ("watches: []", "expected a `watches:` list"),
    ("watches:\n  - {query: x}", "watch #1: missing `name`"),
    ("watches:\n  - {name: A, sites: [nope]}", "unknown site 'nope'"),
    ("watches:\n  - {name: A, sites: []}", "no site and no product page"),
    ("watches:\n  - {name: A, sites: [ldlc], match: '('}", "`match` invalid regular expression"),
    ("watches:\n  - {name: A, sites: [ldlc], max_price: cheap}", "`max_price` must be a number"),
    ("watches:\n  - {name: A, sites: [ldlc], max_price: .nan}", "`max_price` must be a number"),
    ("watches:\n  - {name: A, sites: [ldlc], min_price: -5}", "`min_price` must be between"),
    ("watches:\n  - {name: A, sites: [ldlc], deal_drop_pct: 100}", "`deal_drop_pct` must be between 0.5 and 90"),
    ("watches:\n  - {name: A, sites: [ldlc], exclude: [a, b]}", "`exclude` must be text"),
    ("watches:\n  - {name: A, sites: [ldlc], match: {a: 1}}", "`match` must be text"),
    ("watches:\n  - {name: A, sites: [ldlc], query: " + "q" * 201 + "}", "`query` is too long"),
    ("watches:\n  - {name: A, sites: [ldlc], urls: {ldlc: 'https://example.org/'}}", "URL for ldlc"),
    ("watches:\n  - {name: A, product_urls: ['http://127.0.0.1/']}", "product page"),
    ("watches:\n  - {name: A, product_urls: [{url: 'https://shop.example/a', selector: " + "s" * 201 + "}]}",
     "`selector` must be text of at most 200"),
    ("watches:\n  - {name: A, product_urls: [{url: 'https://shop.example/a', selector: [x]}]}",
     "`selector` must be text"),
    ("watches:\n  - {name: A, sites: [ldlc]}\n  - {name: a, sites: [ldlc]}", "listed twice"),
    ("watches:\n  - {name: " + "x" * 101 + ", sites: [ldlc]}", "name too long"),
])
def test_import_problems(text, expected):
    assert any(expected in problem for problem in problems(text))


def test_import_reports_every_problem_at_once():
    found = problems("watches:\n  - {name: A, sites: [nope]}\n  - {name: B, sites: [ldlc], match: '('}")
    assert len(found) == 2


def test_import_size_limits():
    assert problems("x" * 200_001) == ["the document is too large"]
    many = "watches:\n" + "".join(f"  - {{name: w{i}, sites: [ldlc]}}\n" for i in range(201))
    assert "at most 200" in problems(many)[0]
    pages = ", ".join(f"'https://shop.example/{i}'" for i in range(21))
    assert any("at most 20" in p for p in problems(f"watches:\n  - {{name: A, product_urls: [{pages}]}}"))


def test_presets_only_use_known_sites():
    for preset in PRESETS:
        assert set(preset.sites) <= set(SCRAPERS), preset.key
        assert set(preset.site_urls) <= set(SCRAPERS), preset.key
        for site, url in preset.site_urls.items():
            assert validate_site_url(site, url) == url


def test_numbers_are_accepted_where_text_is_expected():
    [w] = load("watches:\n  - {name: 5070, sites: [ldlc], query: 5070, exclude: 4060}")
    assert (w.name, w.query, w.exclude) == ("5070", "5070", "4060")
