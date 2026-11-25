from __future__ import annotations

import pytest

from pricewatch.notify import render
from pricewatch.notify.render import as_html, as_markdown, as_text, fmt_eur, item_headline, item_lines, truncate

try:
    from tests.notify_helpers import APP_URL, deal, health, message, restock, test_item
except ImportError:  # tests/ is not a package
    from notify_helpers import APP_URL, deal, health, message, restock, test_item


@pytest.mark.parametrize(
    ("price", "expected"),
    [(1079.95, "1 079,95 €"), (549, "549,00 €"), (1234567.5, "1 234 567,50 €"), (0.5, "0,50 €")],
)
def test_fmt_eur(price, expected):
    assert fmt_eur(price) == expected


def test_deal_headline_and_lines():
    item = deal()
    assert item_headline(item) == "RTX 5070 — LDLC"
    assert item_lines(item) == (
        "Gigabyte GeForce RTX 5070 WINDFORCE OC SFF 12G",
        "919,99 € (-12 % vs usual 1 049,00 €, median over 14 days)",
        "Target: 900,00 €",
        "Last alert: 950,00 €",
        "In stock",
    )


def test_deal_without_baseline_target_or_previous():
    item = deal(baseline=None, baseline_days=None, target_price=None, previous_price=None, in_stock=None)
    assert item_lines(item) == ("Gigabyte GeForce RTX 5070 WINDFORCE OC SFF 12G", "919,99 €", "Stock unknown")


def test_deal_baseline_without_day_count_and_out_of_stock():
    lines = item_lines(deal(baseline_days=None, in_stock=False))
    assert lines[1] == "919,99 € (-12 % vs usual 1 049,00 €)"
    assert lines[-1] == "Out of stock"


def test_items_without_a_price_have_no_price_line():
    assert item_lines(deal(price=None)) == ("Gigabyte GeForce RTX 5070 WINDFORCE OC SFF 12G", "Target: 900,00 €", "Last alert: 950,00 €", "In stock")
    assert item_lines(restock(price=None)) == ("LG OLED55C54LA (55 pouces)",)
    assert render.price_context(deal(baseline=0)) == ""


def test_baseline_over_a_single_day_is_singular():
    assert "median over 1 day)" in item_lines(deal(baseline_days=1))[1]


def test_restock_headline_and_lines():
    item = restock()
    assert item_headline(item) == "LG OLED55C5 — back in stock at boulanger.com"
    assert item_lines(item) == ("LG OLED55C54LA (55 pouces)", "1 299,00 €")
    assert item_headline(restock(site="")) == "LG OLED55C5 — back in stock"


def test_health_and_test_items_use_title_and_detail():
    assert item_headline(health()) == "Idealo has been failing for 6 checks"
    assert item_lines(health()) == ("Last error: HTTP 403 (blocked?)",)
    assert item_lines(health(detail="one\n\n  two  ")) == ("one", "two")
    assert item_headline(test_item()) == "Price Watch is connected"
    assert item_lines(test_item(detail="")) == ()


def test_as_text_has_title_items_links_and_footer():
    text = as_text(message())
    assert text.startswith("🔥 2 deals\n\nRTX 5070 — LDLC\n")
    assert "https://www.ldlc.com/fiche/PB00660000.html" in text
    assert "LG OLED55C5 — back in stock at boulanger.com" in text
    assert text.endswith(f"Open Price Watch: {APP_URL}")


def test_as_text_without_app_url_has_no_footer():
    assert "Open Price Watch" not in as_text(message(app_url=""))


def test_as_markdown_links_headline_and_escapes():
    hostile = deal(title="Card [best] *deal* _now_", url="https://shop.example/p_(1)")
    md = as_markdown(message(hostile))
    assert md.startswith("**🔥 2 deals**")
    assert "**[RTX 5070 — LDLC](https://shop.example/p_%281%29)**" in md
    assert r"Card \[best\] \*deal\* \_now\_" in md
    assert f"[Open Price Watch]({APP_URL})" in md


def test_markdown_does_not_start_blocks_from_scraped_text():
    md = as_markdown(message(health(detail="# heading\n- item\n1. first")), with_title=False)
    assert r"\# heading" in md
    assert r"\- item" in md
    assert r"1\. first" in md
    assert "**🔥" not in md


def test_as_html_escapes_scraped_text():
    hostile = deal(title='<script>alert("x")</script>', watch="A&B", site="<b>shop</b>")
    out = as_html(message(hostile))
    assert "<script>" not in out
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in out
    assert "A&amp;B — &lt;b&gt;shop&lt;/b&gt;" in out
    assert '<a href="https://www.ldlc.com/fiche/PB00660000.html">' in out
    assert f'<a href="{APP_URL}">Open Price Watch</a>' in out
    assert "<h2>🔥 2 deals</h2>" in out


@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/html,x", "ftp://x/y", "https://a b", ""])
def test_unsafe_urls_are_not_linked(url):
    item = deal(url=url)
    assert "<a href" not in render.item_html(item)
    assert "](" not in render.item_markdown(item)
    assert render.safe_url(url) == ""


def test_html_attribute_cannot_be_broken_out_of():
    item = deal(url='https://shop.example/?q="><img/src=x>')
    out = render.item_html(item)
    assert "<img" not in out
    assert "&quot;&gt;&lt;img" in out


def test_item_html_is_chat_grade():
    out = render.item_html(deal())
    assert out.startswith('<b><a href="https://www.ldlc.com/fiche/PB00660000.html">RTX 5070 — LDLC</a></b>\n')
    assert "<br" not in out and "<p" not in out


def test_long_scraped_fields_are_clipped():
    item = deal(title="x" * 5000, watch="w" * 600)
    assert len(item_lines(item)[0]) == render.MAX_TITLE
    assert len(item_headline(item)) == render.MAX_HEADLINE
    assert item_lines(item)[0].endswith("…")


def test_truncate_cuts_on_a_line_boundary():
    text = "first line\nsecond line\nthird line"
    assert truncate(text, 100) == text
    assert truncate(text, 25) == "first line\nsecond line\n…"
    assert truncate(text, 14) == "first line\n…"
    assert len(truncate(text, 14)) <= 14


def test_truncate_single_long_line():
    assert truncate("abcdefghij", 5) == "abcd…"
    assert truncate("abc", 0) == ""
    assert truncate("abc", 1) == "…"


def test_first_url_prefers_items_then_app():
    assert render.first_url(message()) == "https://www.ldlc.com/fiche/PB00660000.html"
    assert render.first_url(message(health())) == APP_URL
    assert render.first_url(message(health(), app_url="")) == ""


def test_join_fitting_reports_dropped_blocks():
    blocks = ["a" * 10, "b" * 10, "c" * 10]
    assert render.join_fitting(blocks, 100) == "\n\n".join(blocks)
    fitted = render.join_fitting(blocks, 30)
    assert fitted.startswith("a" * 10) and fitted.endswith("… and 2 more")
    assert len(fitted) <= 30
    assert render.join_fitting(["z" * 50], 30) == ""


def test_chunk_blocks_respects_limit_and_order():
    blocks = ["a" * 40, "b" * 40, "c" * 40]
    chunks = render.chunk_blocks(blocks, 90)
    assert chunks == ["a" * 40 + "\n\n" + "b" * 40, "c" * 40]
    assert render.chunk_blocks([], 10) == []
    oversized = render.chunk_blocks(["x" * 50], 20)
    assert len(oversized) == 1 and len(oversized[0]) <= 20
