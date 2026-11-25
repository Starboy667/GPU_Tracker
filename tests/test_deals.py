from datetime import date

import pytest

from pricewatch import deals
from pricewatch.models import ProductURL, Watch
from pricewatch.scrapers.base import SOURCE_PRODUCT_PAGE, Listing

TODAY = date(2026, 11, 25)


def listing(title="MSI GeForce RTX 5070 12G", price=600.0, in_stock=True, source="ldlc", url="https://x/1"):
    return Listing(site="LDLC", title=title, price=price, url=url, in_stock=in_stock, source=source)


def watch(**kw):
    base = dict(name="RTX 5070", query="rtx 5070", match=deals.auto_match("RTX 5070"))
    return Watch(**{**base, **kw})


# ------------------------------------------------------------------ auto_match

@pytest.mark.parametrize(
    "name,title,expected",
    [
        ("RTX 5070", "MSI GeForce RTX 5070 12G VENTUS", True),
        ("RTX 5070", "MSI GeForce RTX5070 12G", True),
        ("RTX 5070", "MSI GeForce RTX 5070 Ti 16G", False),
        ("RTX 5070", "MSI GeForce RTX 5070 SUPER", False),
        ("RX 9070", "Sapphire Radeon RX 9070 XT Pulse", False),
        ("RX 9070 XT", "Sapphire Radeon RX 9070 XT Pulse", True),
        ("RTX 4070 Ti Super", "ASUS RTX 4070 Ti SUPER 16G", True),
        ("LG OLED55C5", "TV LG OLED55C54LA 55 pouces", True),
        ("LG OLED55C5", "TV LG OLED65C5", False),
        ("Samsung The Frame", "Samsung The Frame Super Slim 55", True),
        ("C++ Primer (5th)", "C++ Primer (5th) edition", True),
    ],
)
def test_auto_match(name, title, expected):
    import re

    assert bool(re.search(deals.auto_match(name), title, re.I)) is expected


def test_regex_problem():
    assert deals.regex_problem(r"RTX\s*5070") is None
    assert deals.regex_problem("(") is not None
    assert "long" in deals.regex_problem("a" * 1000)


# ------------------------------------------------------------------ rejection_reason

def test_listing_accepted():
    assert deals.rejection_reason(listing(), watch()) is None


def test_listing_rejected_when_title_does_not_match():
    assert deals.rejection_reason(listing(title="RTX 5080"), watch()) == "title does not match"


def test_listing_rejected_by_exclude():
    w = watch(exclude=r"\bPC\b|portable")
    assert deals.rejection_reason(listing(title="PC Gamer RTX 5070"), w) == "excluded by filter"


def test_listing_rejected_below_min_price():
    assert deals.rejection_reason(listing(price=99), watch(min_price=150)) == "below the minimum price"
    assert deals.rejection_reason(listing(price=150), watch(min_price=150)) is None


def test_product_pages_skip_title_filters():
    w = watch(exclude="MSI", min_price=5000, product_urls=(ProductURL("https://x/1"),))
    assert deals.rejection_reason(listing(title="whatever", source=SOURCE_PRODUCT_PAGE), w) is None


# ------------------------------------------------------------------ baseline

def lows(*pairs):
    return dict(pairs)


def test_baseline_is_median_of_recent_daily_lows_excluding_today():
    history = lows(
        ("2026-11-20", 700.0), ("2026-11-21", 720.0), ("2026-11-22", 710.0),
        ("2026-11-25", 100.0),  # today: an ongoing deal must not lower its own reference
        ("2026-10-01", 5.0),    # outside the window
    )
    base = deals.baseline(history, TODAY, span_days=14, min_days=3)
    assert base == deals.Baseline(price=710.0, days=3)


def test_baseline_needs_enough_history():
    history = lows(("2026-11-23", 700.0), ("2026-11-24", 720.0))
    assert deals.baseline(history, TODAY, span_days=14, min_days=3) is None


def test_baseline_window_boundary_is_inclusive():
    history = lows(("2026-11-11", 1.0), ("2026-11-12", 2.0), ("2026-11-13", 3.0))
    assert deals.baseline(history, TODAY, span_days=14, min_days=3).price == 2.0
    assert deals.baseline(history, TODAY, span_days=13, min_days=2).price == 2.5


def test_counts_for_baseline():
    assert deals.counts_for_baseline(listing())
    assert deals.counts_for_baseline(listing(in_stock=None))
    assert not deals.counts_for_baseline(listing(in_stock=False))
    assert not deals.counts_for_baseline(listing(source="dealabs"))


# ------------------------------------------------------------------ deal_threshold

def test_threshold_from_baseline():
    assert deals.deal_threshold(None, deals.Baseline(1000.0, 5), 8) == pytest.approx(920.0)


def test_threshold_is_the_more_generous_of_target_and_baseline():
    base = deals.Baseline(1000.0, 5)
    assert deals.deal_threshold(950, base, 8) == 950
    assert deals.deal_threshold(800, base, 8) == pytest.approx(920.0)


def test_threshold_with_target_only_or_nothing():
    assert deals.deal_threshold(500, None, 8) == 500
    assert deals.deal_threshold(None, None, 8) is None


# ------------------------------------------------------------------ decide

def decide(**kw):
    args = dict(
        price=900.0, in_stock=True, threshold=920.0, in_stock_only=True, renotify_drop_pct=2.0,
        notify_restock=False, seen_before=True, previous_in_stock=True, notified_price=None,
    )
    return deals.decide(**{**args, **kw})


def test_first_time_under_threshold_alerts():
    assert decide() == deals.Decision(alert=deals.ALERT_DEAL, notified_price=900.0)


def test_above_threshold_is_quiet_and_rearms():
    assert decide(price=950.0, notified_price=900.0) == deals.Decision(alert=None, notified_price=None)


def test_no_threshold_means_no_deal():
    assert decide(threshold=None) == deals.Decision(alert=None, notified_price=None)


def test_same_deal_does_not_alert_twice():
    assert decide(price=900.0, notified_price=900.0) == deals.Decision(alert=None, notified_price=900.0)
    assert decide(price=890.0, notified_price=900.0) == deals.Decision(alert=None, notified_price=900.0)


def test_further_drop_alerts_again():
    assert decide(price=882.0, notified_price=900.0) == deals.Decision(alert=deals.ALERT_DEAL, notified_price=882.0)


def test_out_of_stock_deal_is_ignored_when_stock_is_required():
    assert decide(in_stock=False) == deals.Decision(alert=None, notified_price=None)
    assert decide(in_stock=False, in_stock_only=False).alert == deals.ALERT_DEAL


def test_unknown_stock_counts_as_available():
    assert decide(in_stock=None).alert == deals.ALERT_DEAL


def test_restock_alert_when_enabled():
    quiet = dict(threshold=None, previous_in_stock=False, in_stock=True)
    assert decide(**quiet).alert is None
    assert decide(**quiet, notify_restock=True) == deals.Decision(alert=deals.ALERT_RESTOCK, notified_price=None)


def test_restock_needs_a_known_out_of_stock_before():
    assert decide(threshold=None, notify_restock=True, previous_in_stock=None).alert is None
    assert decide(threshold=None, notify_restock=True, previous_in_stock=False, seen_before=False).alert is None


def test_deal_wins_over_restock():
    d = decide(notify_restock=True, previous_in_stock=False)
    assert d.alert == deals.ALERT_DEAL


def test_restock_of_an_already_announced_deal():
    d = decide(notify_restock=True, previous_in_stock=False, in_stock_only=False, notified_price=900.0)
    assert d == deals.Decision(alert=deals.ALERT_RESTOCK, notified_price=900.0)


def test_classic_regex_bombs_do_not_hang():
    import time

    started = time.monotonic()
    for pattern, title in ((r"(a+)+$", "a" * 60 + "!"), (r"^(\w+\s?)*$", "ASUS Dual GeForce RTX 5070 OC Edition 12 Go !")):
        try:
            deals.rejection_reason(listing(title=title), watch(match=pattern))
        except deals.SlowPatternError:
            pass  # giving up is fine; hanging is not
    assert time.monotonic() - started < 2.0


def test_pattern_time_limit_becomes_a_clear_error(monkeypatch):
    seen = {}

    def slow(pattern, text, flags, timeout):
        seen["timeout"] = timeout
        raise TimeoutError

    monkeypatch.setattr(deals.regex, "search", slow)
    with pytest.raises(deals.SlowPatternError):
        deals.rejection_reason(listing(), watch())
    assert seen["timeout"] == deals.MATCH_TIMEOUT_SECONDS
    monkeypatch.undo()
    assert deals.pattern_matches(r"rtx\s*5070", "MSI RTX 5070") and not deals.pattern_matches("^x", "MSI")
