from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from pricewatch.models import ProductURL, Settings, StoredListing, Watch
from pricewatch.presets import PRESETS_BY_KEY
from pricewatch.repos import listings, watches
from pricewatch.scrapers.base import Listing
from pricewatch.web import charts, views

TODAY = date(2026, 11, 25)
NOW = datetime(2026, 11, 25, 9, 0, tzinfo=timezone.utc)
STAMP = "2026-11-25T09:00:00Z"


def lows(count, start=1000.0, step=-5.0, skip=()):
    return {
        (TODAY - timedelta(days=back)).isoformat(): start + step * (count - back)
        for back in range(count, 0, -1) if back not in skip
    }


# ------------------------------------------------------------------ charts

def test_money_and_day_formats():
    assert charts.fmt_money(1079.95) == "1" + chr(0x202F) + "079,95" + chr(0xA0) + "€"
    assert charts.fmt_money(1000, decimals=False) == "1" + chr(0x202F) + "000" + chr(0xA0) + "€"
    assert charts.fmt_day(date(2026, 3, 7)) == "7 Mar"


@pytest.mark.parametrize("low,high", [(812, 1296), (0.4, 0.9), (95, 105), (500, 500), (0, 0)])
def test_nice_ticks_cover_the_range_with_round_steps(low, high):
    ticks = charts.nice_ticks(low, high)
    assert ticks[0] <= low and ticks[-1] >= high and 2 <= len(ticks) <= 8
    steps = {round(b - a, 6) for a, b in zip(ticks, ticks[1:])}
    assert len(steps) == 1


def test_line_chart_geometry():
    chart = charts.line_chart(lows(40), today=TODAY, width=720, height=260, usual=950.0, alert=874.0)
    assert len(chart.points) == 40 and chart.lone_points == ()
    assert chart.path.startswith("M") and chart.path.count("M") == 1 and chart.path.count("L") == 39
    for p in chart.points:
        assert chart.left <= p.x <= chart.right and chart.top <= p.y <= chart.bottom
    assert chart.points[0].x < chart.last.x and chart.points[0].y < chart.last.y  # falling prices run down the screen
    assert chart.last.day == (TODAY - timedelta(days=1)).isoformat()
    assert [r.key for r in chart.ref_lines] == ["usual", "alert"]  # upper line first
    usual, alert = chart.ref_lines
    assert usual.y < alert.y and alert.label_y > alert.y and usual.label_y != alert.label_y
    for ref in chart.ref_lines:  # no label sits on the data line
        x0, x1 = ref.label_x, ref.label_x + len(ref.label) * charts.LABEL_CHAR_WIDTH
        assert not any(x0 <= p.x <= x1 and abs(p.y - (ref.label_y - 4)) < charts.LABEL_CLEARANCE for p in chart.points)
    assert usual.label.startswith("Usual 950") and "or less" in alert.label
    assert chart.y_ticks[0].pos == chart.bottom and chart.y_ticks[-1].pos == chart.top
    assert chart.x_ticks[-1].anchor == "end" and chart.x_ticks[-1].label == "25 Nov"
    assert "Latest" in chart.summary and "lowest" in chart.summary


def test_line_chart_puts_the_end_label_away_from_the_line():
    falling = charts.line_chart({"2026-11-23": 1000, "2026-11-24": 1000, "2026-11-25": 700}, today=TODAY)
    rising = charts.line_chart({"2026-11-23": 700, "2026-11-24": 700, "2026-11-25": 1000}, today=TODAY)
    assert falling.end_label_y > falling.last.y  # line arrives from above: label below the dot
    assert rising.end_label_y < rising.last.y
    assert falling.end_label_anchor == "end"


def test_line_chart_breaks_on_long_gaps_and_marks_lonely_days():
    data = lows(40, skip=set(range(10, 25)) | {3, 4, 5, 6, 7, 8, 9})  # a long hole, then one isolated day
    chart = charts.line_chart(data, today=TODAY)
    assert chart.path.count("M") == 2
    assert chart.lone_points == ()  # the isolated last day is drawn as the end dot
    middle = charts.line_chart({"2026-10-01": 500, "2026-11-01": 510, "2026-11-24": 505, "2026-11-25": 500},
                               today=TODAY)
    assert [p.day for p in middle.lone_points] == ["2026-10-01", "2026-11-01"]


def test_line_chart_single_point_and_empty():
    assert charts.line_chart({}, today=TODAY) is None
    assert charts.line_chart({"2025-01-01": 100}, today=TODAY) is None  # outside the window
    one = charts.line_chart({TODAY.isoformat(): 499.0}, today=TODAY, width=360, height=230)
    assert one.path == "" and len(one.points) == 1 and one.last.text.startswith("499,00")
    assert len(one.x_ticks) <= 4 and one.end_label_anchor == "end"


def test_line_chart_with_cents_uses_decimal_ticks():
    chart = charts.line_chart({"2026-11-24": 0.5, "2026-11-25": 0.9}, today=TODAY)
    assert "," in chart.y_ticks[1].label


def test_sparkline():
    assert charts.sparkline({}, today=TODAY) is None
    assert charts.sparkline({"2026-11-25": 10}, today=TODAY) is None
    spark = charts.sparkline(lows(60), today=TODAY, days=30)
    assert spark.path.count("L") == 28 and 0 <= spark.end_y <= spark.height
    assert spark.end_x == pytest.approx(spark.width - 4)
    flat = charts.sparkline({"2026-11-24": 10, "2026-11-25": 10}, today=TODAY)
    assert flat.end_y == flat.height / 2


# ------------------------------------------------------------------ formatting helpers

@pytest.mark.parametrize("delta,expected", [
    (timedelta(seconds=30), "just now"), (timedelta(minutes=12), "12 min ago"), (timedelta(hours=3), "3 h ago"),
    (timedelta(days=4), "4 days ago"),
])
def test_ago(delta, expected):
    stamp = (NOW - delta).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert views.ago(stamp, NOW) == expected
    assert views.ago(None, NOW) == "never"


def test_until():
    assert views.until(None, NOW) == ""
    assert views.until(NOW + timedelta(seconds=20), NOW) == "in under a minute"
    assert views.until(NOW + timedelta(minutes=18), NOW) == "in 18 min"
    assert views.until(NOW + timedelta(hours=5), NOW) == "in 5 h"


def test_local_time_uses_the_configured_zone():
    paris = ZoneInfo("Europe/Paris")
    assert views.local_time(STAMP, paris) == "25 Nov 2026, 10:00"
    assert views.local_day("2026-12-31T23:30:00Z", paris) == "1 Jan 2027"
    assert views.local_time(None, paris) == "" and views.local_day("", paris) == ""


def test_calendar_day_is_not_shifted_by_time_zones():
    assert views.calendar_day("2026-11-25") == "25 Nov 2026"
    assert views.calendar_day("") == "" and views.calendar_day(None) == "" and views.calendar_day("soon") == ""


def test_http_url_only_lets_web_links_through():
    assert views.http_url("https://shop.example/a?b=1") == "https://shop.example/a?b=1"
    for bad in ("javascript:alert(1)", "data:text/html,x", "/relative", "", None, "http://[broken"):
        assert views.http_url(bad) == ""


def test_percent_and_number_input():
    assert views.percent(-12.4) == chr(0x2212) + "12" + chr(0xA0) + "%"
    assert views.percent(8.0) == "+8" + chr(0xA0) + "%"
    assert views.percent(None) == ""
    assert views.number_input(150.0) == "150" and views.number_input(599.9) == "599.9" and views.number_input(None) == ""


# ------------------------------------------------------------------ pricing and rows

def stored(price, in_stock=True, **kw):
    base = dict(id=1, watch_id=1, url="u", source="ldlc", site="LDLC", title="t", price=price, in_stock=in_stock,
                active=True, first_seen=STAMP, last_seen=STAMP, notified_price=None, notified_at=None)
    return StoredListing(**{**base, **kw})


def test_pricing_for():
    watch = Watch(name="A", query="a", match="a")
    learning = views.pricing_for(watch, lows(1), Settings(), TODAY)
    assert learning.baseline is None and learning.threshold is None and learning.days_until_baseline == 2
    ready = views.pricing_for(watch, {d: 1000.0 for d in lows(5)}, Settings(), TODAY)
    assert ready.baseline.price == 1000 and ready.threshold == pytest.approx(920) and ready.days_until_baseline == 0
    assert ready.in_stock_only and ready.drop_pct == 8
    custom = views.pricing_for(Watch(name="A", query="a", match="a", max_price=990, deal_drop_pct=20,
                                     in_stock_only=False), {d: 1000.0 for d in lows(5)}, Settings(), TODAY)
    assert custom.threshold == 990 and custom.drop_pct == 20 and not custom.in_stock_only
    assert views.is_deal(stored(900), ready) and not views.is_deal(stored(950), ready)
    assert not views.is_deal(stored(900, in_stock=False), ready) and views.is_deal(stored(900, in_stock=False), custom)
    assert not views.is_deal(stored(1), learning)


def test_watch_page_prices_with_the_full_baseline_window(config, clock, public_dns):
    """A baseline longer than the 90-day chart must not be computed from a cut-off history."""
    import dataclasses

    from fastapi.testclient import TestClient

    from pricewatch.repos import settings as settings_repo
    from pricewatch.web.app import build_services, create_app

    services = build_services(config, scrapers={}, product_scraper=lambda *a: None, send=lambda *a, **k: None,
                              clock=clock)
    services.scheduler.start = lambda **kwargs: None
    with TestClient(create_app(config, services=services)) as client:
        today = clock().date()
        with services.db.connect() as conn:
            settings_repo.save(conn, dataclasses.replace(Settings(), baseline_days=120, baseline_min_days=100))
            watch = watches.create(conn, Watch(name="Old timer", query="o", match="o", sites=("ldlc",)), STAMP)
            for back in range(1, 119):
                listings.record_daily_low(conn, watch.id, (today - timedelta(days=back)).isoformat(), 1000.0)
        page = client.get("/login")
        import re

        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        client.post("/login", data={"csrf_token": token, "email": config.admin_email,
                                    "password": config.admin_password})
        detail = client.get(f"/watches/{watch.id}").text
    assert "median over 118 days" in detail and "Still learning" not in detail


def test_watch_rows_states_and_order(db):
    def offer(price, in_stock=True):
        return Listing(site="LDLC", title="t", price=price, url="u", in_stock=in_stock, source="ldlc")

    with db.connect() as conn:
        deal = watches.create(conn, Watch(name="Deal", query="d", match="d", max_price=500), STAMP)
        tracking = watches.create(conn, Watch(name="Tracking", query="t", match="t"), STAMP)
        empty = watches.create(conn, Watch(name="Empty", query="e", match="e"), STAMP)
        watches.create(conn, Watch(name="Waiting", query="w", match="w"), STAMP)
        paused = watches.create(conn, Watch(name="Paused", query="p", match="p", enabled=False), STAMP)
        for w in (deal, tracking, empty, paused):
            watches.mark_checked(conn, w.id, STAMP, ())
        listings.save(conn, deal.id, offer(450.0), None, None, STAMP)
        listings.save(conn, tracking.id, offer(950.0), None, None, STAMP)
        for day, price in {d: 1000.0 for d in lows(5)}.items():
            listings.record_daily_low(conn, tracking.id, day, price)
        rows = views.watch_rows(conn, Settings(), TODAY)
    assert [(r.watch.name, r.state) for r in rows] == [
        ("Deal", "deal"), ("Tracking", "tracking"), ("Empty", "empty"), ("Waiting", "waiting"), ("Paused", "paused")]
    by_name = {r.watch.name: r for r in rows}
    assert by_name["Tracking"].delta_pct == pytest.approx(-5) and by_name["Tracking"].spark is not None
    assert by_name["Deal"].delta_pct is None and by_name["Deal"].listing_count == 1 and by_name["Deal"].spark is None


def test_form_values_round_trip_through_the_parser(public_dns):
    from pricewatch import forms
    from pricewatch.scrapers import SCRAPERS, validate_site_url

    watch = Watch(
        id=3, name="LG OLED55C5", query="lg oled 55 c5", match="OLED55C5", exclude="pied", min_price=100.0,
        max_price=1099.5, deal_drop_pct=6.0, in_stock_only=False, notify_restock=True, sites=("ldlc", "idealo"),
        site_urls={"ldlc": "https://www.ldlc.com/tv/"}, product_urls=(ProductURL("https://shop.example/tv", ".p"),),
        channel_ids=(2,), enabled=False,
    )
    parsed, errors = forms.parse_watch(views.watch_form_values(watch), site_keys=SCRAPERS, channel_ids=[1, 2],
                                       validate_site_url=validate_site_url, existing=watch)
    assert errors == {}
    assert parsed == watch.__class__(**{**watch.__dict__, "site_urls": dict(watch.site_urls)})

    plain = Watch(name="RTX 5070", query="RTX 5070", match=forms.deals.auto_match("RTX 5070"), sites=("ldlc",))
    values = views.watch_form_values(plain)
    assert values["query"] == "" and values["match"] == "" and values["channels_mode"] == "all"


def test_preset_form_values():
    gpu = views.preset_form_values(PRESETS_BY_KEY["gpu"])
    assert gpu["site_topachat"] == "on" and gpu["min_price"] == "150" and "cartes-graphiques" in gpu["url_dealabs"]
    page = views.preset_form_values(PRESETS_BY_KEY["page"])
    assert page["notify_restock"] == "on" and not any(k.startswith("site_") for k in page)


def test_reference_label_moves_off_the_data_line():
    flat = {(TODAY - timedelta(days=back)).isoformat(): 1000.0 + (3 if back % 2 else -3) for back in range(60, 0, -1)}
    hugging = charts.line_chart(flat, today=TODAY, width=1040, height=300, usual=1000.0)
    [usual] = hugging.ref_lines
    x0 = usual.label_x - (len(usual.label) * charts.LABEL_CHAR_WIDTH if usual.label_anchor == "end" else 0)
    x1 = x0 + len(usual.label) * charts.LABEL_CHAR_WIDTH
    near = [p for p in hugging.points if x0 <= p.x <= x1 and abs(p.y - (usual.label_y - 4)) < charts.LABEL_CLEARANCE]
    assert near == [] or usual.label_y > usual.y  # either clear of the line, or at least pushed under it
    lonely = charts.line_chart({TODAY.isoformat(): 500.0}, today=TODAY, usual=520.0, alert=480.0)
    assert [r.label_anchor for r in lonely.ref_lines] == ["start", "start"]
