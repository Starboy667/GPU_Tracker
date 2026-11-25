import dataclasses
from datetime import timedelta

import pytest

from pricewatch.checker import Checker
from pricewatch.delivery import MAX_DELIVERY_ATTEMPTS, Dispatcher, message_title, sample_message
from pricewatch.models import Channel, ProductURL, Watch
from pricewatch.notify.base import NotifyError
from pricewatch.repos import alerts, channels, listings, runs, settings, watches
from pricewatch.scan import scan_watch
from pricewatch.scheduler import Scheduler
from pricewatch.scrapers.base import KIND_DEALS, KIND_RETAILER, FetchContext, Listing, ScrapeError, SiteScraper

NOW = "2026-11-25T09:00:00Z"


class Shop:
    """A fake retailer: tests set ``results`` (or an exception) and the scraper returns it."""

    def __init__(self, key, label=None, kind=KIND_RETAILER):
        self.key = key
        self.results = []
        self.calls = []
        self.scraper = SiteScraper(
            key=key, label=label or key.upper(), kind=kind, domains=(f"{key}.example",),
            description="", search=self.search,
        )

    def search(self, ctx, query, url=None):
        self.calls.append((query, url))
        if isinstance(self.results, Exception):
            raise self.results
        return list(self.results)


class Outbox:
    def __init__(self):
        self.sent = []
        self.error = None

    def __call__(self, type_key, config, message, *, allow_private=False):
        if self.error is not None:
            raise self.error
        self.sent.append((type_key, config, message))


class World:
    def __init__(self, db, box, config, clock):
        self.db, self.box, self.clock = db, box, clock
        self.ldlc = Shop("ldlc", "LDLC")
        self.dealabs = Shop("dealabs", "Dealabs", KIND_DEALS)
        self.pages = {}
        self.outbox = Outbox()
        self.pauses = 0
        self.dispatcher = Dispatcher(db, box, config, send=self.outbox, clock=clock)
        self.checker = Checker(
            db, box, config, self.dispatcher,
            scrapers={"ldlc": self.ldlc.scraper, "dealabs": self.dealabs.scraper},
            product_scraper=self.product, pause=self.pause, clock=clock,
        )

    def pause(self):
        self.pauses += 1

    def product(self, ctx, url, selector=None):
        page = self.pages[url]
        if isinstance(page, Exception):
            raise page
        return page

    def add_watch(self, **kw):
        base = dict(name="RTX 5070", query="rtx 5070", match="RTX\\s*5070", sites=("ldlc",))
        with self.db.connect() as conn:
            return watches.create(conn, Watch(**{**base, **kw}), NOW)

    def add_channel(self, **kw):
        base = dict(name="Phone", type="ntfy", config={"topic": "x"})
        with self.db.connect() as conn:
            return channels.create(conn, self.box, Channel(**{**base, **kw}), NOW)

    def set_settings(self, **kw):
        with self.db.connect() as conn:
            settings.save(conn, dataclasses.replace(settings.load(conn), **kw))

    def seed_lows(self, watch, price, days=5):
        today = self.clock().date()
        with self.db.connect() as conn:
            for back in range(1, days + 1):
                listings.record_daily_low(conn, watch.id, (today - timedelta(days=back)).isoformat(), price)

    def alerts(self):
        with self.db.connect() as conn:
            return alerts.recent(conn)

    def listings(self, watch, **kw):
        with self.db.connect() as conn:
            return listings.for_watch(conn, watch.id, **kw)


def offer(price=900.0, url="https://ldlc.example/a", title="MSI RTX 5070 12G", in_stock=True, source="ldlc"):
    return Listing(site=source.upper(), title=title, price=price, url=url, in_stock=in_stock, source=source)


@pytest.fixture
def world(db, box, config, clock):
    return World(db, box, config, clock)


# ------------------------------------------------------------------ scan_watch

def test_scan_filters_deduplicates_and_sorts(world):
    w = world.add_watch(exclude="PC", min_price=100)
    world.ldlc.results = [
        offer(950, "u1"), offer(900, "u2"), offer(920, "u2"),  # same URL twice: first wins
        offer(50, "u3"), offer(800, "u4", title="PC Gamer RTX 5070"), offer(700, "u5", title="RTX 5080"),
    ]
    scan = scan_watch(w, FetchContext(), scrapers=world.checker._scrapers, product_scraper=world.product,
                      pause=world.pause)
    assert [(l.url, l.price) for l in scan.accepted] == [("u2", 900), ("u1", 950)]
    assert {reason for _, reason in scan.rejected} == {
        "below the minimum price", "excluded by filter", "title does not match"}
    assert scan.sources_ok == ("ldlc",) and scan.errors == () and scan.outcomes == (("ldlc", None),)


def test_scan_reports_failures_and_keeps_going(world):
    w = world.add_watch(
        sites=("ldlc", "dealabs", "nope"), site_urls={"dealabs": "https://dealabs.example/feed"},
        product_urls=(ProductURL("https://shop.example/tv", ".price"), ProductURL("https://other.example/tv")),
    )
    world.ldlc.results = ScrapeError("HTTP 503")
    world.dealabs.results = ValueError("markup changed")
    world.pages["https://shop.example/tv"] = offer(1000, "https://shop.example/redirected", source="product")
    world.pages["https://other.example/tv"] = ScrapeError("no price found")
    scan = scan_watch(w, FetchContext(), scrapers=world.checker._scrapers, product_scraper=world.product,
                      pause=world.pause)
    assert world.dealabs.calls == [("rtx 5070", "https://dealabs.example/feed")]
    assert [l.url for l in scan.accepted] == ["https://shop.example/tv"]  # identity = configured URL
    assert scan.accepted[0].source == "product"
    assert scan.errors == (
        ("LDLC", "HTTP 503"), ("Dealabs", "parser error (ValueError)"), ("nope", "unknown site"),
        ("other.example", "no price found"),
    )
    assert dict(scan.outcomes) == {
        "ldlc": "HTTP 503", "dealabs": "parser error (ValueError)",
        "product:shop.example": None, "product:other.example": "no price found",
    }
    assert scan.sources_ok == ()
    assert world.pauses == 3  # between the four requests, not before the first


def test_a_shop_that_refuses_us_is_asked_once_per_check(world):
    """Otherwise every watch would wait for the same time-out again."""
    first = world.add_watch(name="A", sites=("ldlc", "dealabs"),
                            product_urls=(ProductURL("https://shop.example/1"), ProductURL("https://shop.example/2")))
    second = world.add_watch(name="B", sites=("ldlc", "dealabs"))
    world.ldlc.results = ScrapeError("ConnectTimeout", site_wide=True)
    world.dealabs.results = ScrapeError("HTTP 500")  # a plain failure: still worth asking again
    calls = []

    def product(ctx, url, selector=None):
        calls.append(url)
        raise ScrapeError("HTTP 403 (blocked?)", site_wide=True)

    world.checker._product_scraper = product
    run = world.checker.run()
    assert len(world.ldlc.calls) == 1 and len(world.dealabs.calls) == 2
    assert calls == ["https://shop.example/1"]
    assert ("A", "LDLC", "ConnectTimeout") in run.errors
    assert ("B", "LDLC", "ConnectTimeout (not asked again during this check)") in run.errors
    assert [error for watch, site, error in run.errors if site == "shop.example"] == [
        "HTTP 403 (blocked?)", "HTTP 403 (blocked?) (not asked again during this check)"]
    with world.db.connect() as conn:
        assert runs.health_get(conn, "ldlc").consecutive_failures == 1
        assert runs.health_get(conn, "product:shop.example").consecutive_failures == 1

    world.ldlc.results = [offer(900)]
    world.checker.run()  # a new check asks again
    assert len(world.ldlc.calls) == 3 and len(world.listings(first)) == 1 and len(world.listings(second)) == 1


# ------------------------------------------------------------------ run: detection

def test_target_price_alerts_once_then_on_a_further_drop(world):
    w = world.add_watch(max_price=920)
    world.add_channel()
    world.ldlc.results = [offer(900)]

    run = world.checker.run()
    assert (run.status, run.watches, run.listings, run.alerts) == ("ok", 1, 1, 1)
    [alert] = world.alerts()
    assert (alert.kind, alert.price, alert.target_price, alert.previous_price) == ("deal", 900, 920, None)
    assert len(world.outbox.sent) == 1 and world.outbox.sent[0][2].title == "🔥 1 deal"
    assert world.outbox.sent[0][2].app_url == "http://testserver"

    world.clock.advance(minutes=30)
    world.checker.run()  # same price: quiet
    world.ldlc.results = [offer(895)]
    world.checker.run()  # less than 2 % cheaper: quiet
    assert len(world.alerts()) == 1

    world.ldlc.results = [offer(870)]
    world.checker.run()
    newest = world.alerts()[0]
    assert (newest.price, newest.previous_price) == (870, 900)
    assert len(world.outbox.sent) == 2

    world.ldlc.results = [offer(990)]  # back above the target: re-armed
    world.checker.run()
    world.ldlc.results = [offer(900)]
    world.checker.run()
    assert len(world.alerts()) == 3
    assert world.listings(w)[0].notified_price == 900


def test_baseline_deal_and_no_alert_without_history(world):
    w = world.add_watch()
    world.add_channel()
    world.ldlc.results = [offer(900)]
    world.checker.run()
    assert world.alerts() == []  # no target and no baseline yet

    world.seed_lows(w, 1000.0)
    world.ldlc.results = [offer(930)]  # 7 % under: not enough
    world.checker.run()
    assert world.alerts() == []
    world.ldlc.results = [offer(915)]  # 8.5 % under the usual 1000
    world.checker.run()
    [alert] = world.alerts()
    assert (alert.baseline, alert.baseline_days, alert.price) == (1000.0, 5, 915)


def test_watch_overrides_global_drop_and_stock_rules(world):
    w = world.add_watch(deal_drop_pct=20, in_stock_only=False)
    world.seed_lows(w, 1000.0)
    world.ldlc.results = [offer(850)]
    world.checker.run()
    assert world.alerts() == []  # 15 % < the watch's 20 %
    world.ldlc.results = [offer(790, in_stock=False)]
    world.checker.run()
    assert [a.in_stock for a in world.alerts()] == [False]  # stock not required on this watch


def test_out_of_stock_is_ignored_by_default_and_restock_alerts(world):
    w = world.add_watch(notify_restock=True)
    world.add_channel()
    world.ldlc.results = [offer(900, in_stock=False)]
    world.checker.run()
    world.ldlc.results = [offer(900, in_stock=True)]
    world.checker.run()
    [alert] = world.alerts()
    assert alert.kind == "restock" and alert.previous_price is None
    assert world.outbox.sent[-1][2].title == "📦 Back in stock"
    assert world.listings(w)[0].notified_price is None


def test_daily_low_ignores_dealabs_and_out_of_stock(world):
    w = world.add_watch(sites=("ldlc", "dealabs"))
    world.ldlc.results = [offer(900), offer(700, "u2", in_stock=False)]
    world.dealabs.results = [offer(500, "u3", source="dealabs", in_stock=None)]
    world.checker.run()
    with world.db.connect() as conn:
        assert listings.daily_lows(conn, w.id) == {"2026-11-25": 900}
    world.ldlc.results = [offer(880)]
    world.checker.run()
    with world.db.connect() as conn:
        assert listings.daily_lows(conn, w.id) == {"2026-11-25": 880}


def test_listings_disappear_only_when_their_site_answered(world):
    w = world.add_watch(sites=("ldlc", "dealabs"))
    world.ldlc.results = [offer(900, "u1"), offer(950, "u2")]
    world.dealabs.results = [offer(800, "u3", source="dealabs")]
    world.checker.run()
    assert {l.url for l in world.listings(w)} == {"u1", "u2", "u3"}

    world.ldlc.results = [offer(900, "u1")]
    world.dealabs.results = ScrapeError("HTTP 500")
    run = world.checker.run()
    assert run.status == "partial" and run.errors == (("RTX 5070", "Dealabs", "HTTP 500"),)
    assert {l.url for l in world.listings(w)} == {"u1", "u3"}  # u2 gone, u3 kept (its site failed)
    with world.db.connect() as conn:
        assert watches.get(conn, w.id).last_errors == (("Dealabs", "HTTP 500"),)

    with world.db.connect() as conn:
        watches.update(conn, dataclasses.replace(watches.get(conn, w.id), sites=("ldlc",)), NOW)
    world.checker.run()
    assert {l.url for l in world.listings(w)} == {"u1"}  # dealabs no longer part of the watch


def test_a_broken_watch_does_not_stop_the_others(world, monkeypatch):
    world.add_watch(name="Broken")
    fine = world.add_watch(name="Fine", max_price=950)
    world.add_channel()
    world.ldlc.results = [offer(900)]
    original = world.checker._store

    def store(conn, watch, scan, settings):
        if watch.name == "Broken":
            raise RuntimeError("boom")
        return original(conn, watch, scan, settings)

    monkeypatch.setattr(world.checker, "_store", store)
    run = world.checker.run()
    assert (run.status, run.watches, run.alerts) == ("partial", 1, 1)
    assert run.errors == (("Broken", "", "internal error (RuntimeError)"),)
    assert len(world.listings(fine)) == 1 and len(world.outbox.sent) == 1


def test_same_price_never_alerts_twice_even_with_a_zero_step(world):
    world.set_settings(renotify_drop_pct=0.0)
    world.add_watch(max_price=950)
    world.ldlc.results = [offer(900)]
    for _ in range(3):
        world.checker.run()
    assert len(world.alerts()) == 1
    world.ldlc.results = [offer(899.99)]
    world.checker.run()
    assert len(world.alerts()) == 2  # any further drop counts when the step is 0


def test_manual_checks_do_not_count_against_a_shop(world):
    world.set_settings(health_alert_after=2)
    w = world.add_watch()
    world.ldlc.results = ScrapeError("HTTP 503")
    for _ in range(5):
        world.checker.run("manual", (w.id,))
    with world.db.connect() as conn:
        assert runs.health_all(conn) == []
    assert world.alerts() == []


def test_dispatch_skips_a_channel_deleted_meanwhile(world, monkeypatch):
    from pricewatch import delivery

    doomed = world.add_channel(name="Doomed")
    world.add_channel(name="Kept")
    world.add_watch(max_price=950)
    world.ldlc.results = [offer(900)]
    world.outbox.error = NotifyError("down")
    world.checker.run()
    world.outbox.error = None
    real_list = delivery.channels_repo.list_all
    monkeypatch.setattr(delivery.channels_repo, "list_all",
                        lambda conn, box: [c for c in real_list(conn, box) if c.id != doomed.id])
    assert world.dispatcher.dispatch() == 1
    assert [config["topic"] for _, config, _ in world.outbox.sent] == ["x"]


def test_only_enabled_watches_unless_asked_explicitly(world):
    on = world.add_watch()
    off = world.add_watch(name="Paused", enabled=False)
    world.ldlc.results = [offer(900)]
    assert world.checker.run().watches == 1
    assert world.listings(off) == []
    assert world.checker.run("manual", (off.id,)).watches == 1
    assert len(world.listings(off)) == 1 and len(world.listings(on)) == 1


def test_run_survives_a_watch_deleted_meanwhile(world):
    w = world.add_watch()

    def delete_then_answer(ctx, query, url=None):
        with world.db.connect() as conn:
            watches.delete(conn, w.id)
        return [offer(900)]

    world.checker._scrapers["ldlc"] = dataclasses.replace(world.ldlc.scraper, search=delete_then_answer)
    run = world.checker.run()
    assert (run.status, run.watches) == ("ok", 0)


def test_internal_error_is_recorded_not_raised(world, monkeypatch):
    world.add_watch()
    world.ldlc.results = [offer(900)]
    monkeypatch.setattr(world.checker, "_housekeeping", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    run = world.checker.run()
    assert run.status == "failed" and run.errors == (("", "", "internal error (RuntimeError)"),)
    assert run.finished_at is not None


def test_only_one_run_at_a_time(world):
    world.add_watch()
    seen = []
    world.ldlc.results = []
    original = world.ldlc.search

    def nested(ctx, query, url=None):
        seen.append(world.checker.is_running)
        seen.append(world.checker.run())
        return original(ctx, query, url)

    world.checker._scrapers["ldlc"] = dataclasses.replace(world.ldlc.scraper, search=nested)
    assert world.checker.run() is not None
    assert seen == [True, None] and not world.checker.is_running


def test_preview_stores_nothing(world):
    w = world.add_watch()
    world.ldlc.results = [offer(900)]
    scan = world.checker.preview(w)
    assert len(scan.accepted) == 1 and world.listings(w) == []
    with world.db.connect() as conn:
        assert runs.latest(conn) == []


# ------------------------------------------------------------------ channels and delivery

def test_alerts_go_to_subscribed_channels_of_the_watch(world):
    everything = world.add_channel(name="All")
    world.add_channel(name="Health only", events=("health",))
    world.add_channel(name="Off", enabled=False)
    other = world.add_channel(name="Other")
    w = world.add_watch(max_price=950, channel_ids=(everything.id,))
    world.ldlc.results = [offer(900)]
    world.checker.run()
    with world.db.connect() as conn:
        summary = alerts.delivery_summary(conn, [a.id for a in alerts.recent(conn)])
    assert list(summary.values()) == [[("All", "sent", None)]]
    assert other.id != everything.id and len(world.outbox.sent) == 1 and w.channel_ids == (everything.id,)


def test_failed_delivery_is_retried_then_abandoned(world):
    channel = world.add_channel()
    world.add_watch(max_price=950)
    world.ldlc.results = [offer(900)]
    world.outbox.error = NotifyError("HTTP 500:   server\n exploded")
    world.checker.run()
    with world.db.connect() as conn:
        [(delivery, _)] = alerts.pending(conn)
        stored = channels.get(conn, world.box, channel.id)
    assert (delivery.attempts, delivery.last_error) == (1, "HTTP 500: server exploded")
    assert stored.last_error == "HTTP 500: server exploded" and stored.last_ok_at is None

    world.outbox.error = RuntimeError("secret-token-in-traceback")
    for _ in range(MAX_DELIVERY_ATTEMPTS - 1):
        world.dispatcher.dispatch()
    with world.db.connect() as conn:
        assert alerts.pending(conn) == []
        [[(_, status, error)]] = alerts.delivery_summary(conn, [a.id for a in alerts.recent(conn)]).values()
    assert status == "failed" and error == "unexpected error (RuntimeError)"


def test_delivery_recovers_and_batches(world):
    channel = world.add_channel()
    world.add_watch(max_price=950)
    world.ldlc.results = [offer(900 - i, f"u{i}") for i in range(25)]
    world.outbox.error = NotifyError("down")
    world.checker.run()
    world.outbox.error = None
    assert world.dispatcher.dispatch() == 25
    assert [len(m.items) for _, _, m in world.outbox.sent] == [20, 5]
    assert world.outbox.sent[0][2].title == "🔥 20 deals"
    with world.db.connect() as conn:
        assert channels.get(conn, world.box, channel.id).last_error is None


def test_stale_pending_deliveries_expire(world):
    world.add_channel()
    world.add_watch(max_price=950)
    world.ldlc.results = [offer(900)]
    world.outbox.error = NotifyError("down")
    world.checker.run()
    world.outbox.error = None
    world.clock.advance(hours=25)
    assert world.dispatcher.dispatch() == 0 and world.outbox.sent == []


def test_unreadable_channel_fails_with_a_clear_message(world, config):
    from pricewatch.security import SecretBox

    channel = world.add_channel()
    other_key = Dispatcher(world.db, SecretBox("x" * 40), config, send=world.outbox, clock=world.clock)
    with world.db.connect() as conn:
        broken = channels.get(conn, SecretBox("x" * 40), channel.id)
    assert "cannot be decrypted" in other_key.send_test(broken)
    world.add_watch(max_price=950)
    world.ldlc.results = [offer(900)]
    world.checker.run()  # delivery created with the right key...
    assert len(world.outbox.sent) == 1
    assert world.dispatcher.send_test(channel) is None  # ...and the right key can send tests
    assert world.outbox.sent[-1][2] == sample_message("http://testserver")


def test_message_titles():
    assert message_title(["deal", "deal"]) == "🔥 2 deals"
    assert message_title(["restock"]) == "📦 Back in stock"
    assert message_title(["health"]) == "⚠️ Price Watch: scraper status"
    assert message_title(["deal", "health", "restock"]) == "Price Watch: 3 alerts"


# ------------------------------------------------------------------ scraper health

def test_health_alert_after_repeated_failures_and_on_recovery(world):
    world.set_settings(health_alert_after=3)
    world.add_channel()
    world.add_watch(product_urls=(ProductURL("https://shop.example/tv"),))
    world.pages["https://shop.example/tv"] = offer(1000, source="product")
    world.ldlc.results = ScrapeError("HTTP 403 (blocked?)")
    for _ in range(2):
        world.checker.run()
    assert world.alerts() == []
    world.checker.run()
    [alert] = world.alerts()
    assert alert.kind == "health" and alert.title == "LDLC has failed 3 checks in a row"
    assert alert.detail == "Latest error: HTTP 403 (blocked?)"
    world.checker.run()
    assert len(world.alerts()) == 1  # alerted once, not on every failed run
    with world.db.connect() as conn:
        health = {h.site: h for h in runs.health_all(conn)}
    assert health["ldlc"].consecutive_failures == 4 and health["ldlc"].alerted
    assert health["product:shop.example"].consecutive_failures == 0

    world.ldlc.results = []
    world.checker.run()
    assert world.alerts()[0].title == "LDLC is working again"
    with world.db.connect() as conn:
        assert runs.health_get(conn, "ldlc").consecutive_failures == 0


def test_health_alerts_can_be_turned_off(world):
    world.set_settings(health_alert_after=0)
    world.add_watch(sites=(), product_urls=(ProductURL("https://shop.example/tv"),))
    world.pages["https://shop.example/tv"] = ScrapeError("HTTP 403")
    for _ in range(8):
        world.checker.run()
    assert world.alerts() == []
    with world.db.connect() as conn:
        assert runs.health_get(conn, "product:shop.example").consecutive_failures == 8


def test_housekeeping_prunes_old_price_points(world):
    w = world.add_watch()
    world.set_settings(history_days=30)
    world.ldlc.results = [offer(900)]
    world.checker.run()
    world.clock.advance(days=40)
    world.ldlc.results = [offer(800)]
    world.checker.run()
    with world.db.connect() as conn:
        points = listings.price_points(conn, world.listings(w)[0].id)
    assert [p[1] for p in points] == [800]


# ------------------------------------------------------------------ scheduler

def make_scheduler(world, **kw):
    return Scheduler(world.checker, world.db, clock=world.clock, jitter=lambda: 1.0, **kw)


def test_scheduler_runs_when_due_and_keeps_the_cadence(world):
    world.add_watch()
    world.ldlc.results = [offer(900)]
    scheduler = make_scheduler(world)
    scheduler._next_due = scheduler._initial_due()
    assert scheduler.run_pending() is False  # first run is delayed a little after startup
    world.clock.advance(seconds=21)
    assert scheduler.run_pending() is True
    assert scheduler.next_due == world.clock() + timedelta(minutes=30)
    assert scheduler.run_pending() is False
    world.clock.advance(minutes=31)
    assert scheduler.run_pending() is True
    with world.db.connect() as conn:
        assert [r.trigger for r in runs.latest(conn)] == ["schedule", "schedule"]


def test_scheduler_never_retries_a_crashing_run_in_a_loop(world, monkeypatch):
    scheduler = make_scheduler(world)
    scheduler._next_due = world.clock()
    monkeypatch.setattr(world.checker, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))
    monkeypatch.setattr(scheduler, "_interval", lambda: (_ for _ in ()).throw(RuntimeError("db locked")))
    with pytest.raises(RuntimeError, match="disk full"):
        scheduler.run_pending()
    assert scheduler.next_due == world.clock() + timedelta(minutes=30)  # already pushed back
    assert scheduler.run_pending() is False


def test_scheduler_without_a_schedule_only_serves_requests(world):
    world.add_watch()
    scheduler = make_scheduler(world, tick_seconds=0.01)
    scheduler.start(scheduled=False)
    try:
        assert scheduler.next_due is None
        world.clock.advance(days=3)
        scheduler.request_run()
        import time

        for _ in range(300):
            if not scheduler.is_busy:
                break
            time.sleep(0.01)
        with world.db.connect() as conn:
            assert [r.trigger for r in runs.latest(conn)] == ["manual"]
    finally:
        scheduler.stop()


def test_scheduler_resumes_after_a_restart_without_rescraping(world):
    world.add_watch()
    world.checker.run()
    world.clock.advance(minutes=10)
    restarted = make_scheduler(world)
    assert restarted._initial_due() == world.clock() + timedelta(minutes=20)
    world.clock.advance(hours=5)
    assert restarted._initial_due() == world.clock() + timedelta(seconds=20)


def test_scheduler_manual_requests(world):
    a = world.add_watch()
    world.add_watch(name="Other")
    world.ldlc.results = [offer(900)]
    scheduler = make_scheduler(world)
    assert not scheduler.is_busy
    scheduler.request_run((a.id,))
    scheduler.request_run((a.id,))  # duplicate request is merged
    assert scheduler.is_busy
    assert scheduler.run_pending() is True and scheduler.run_pending() is False
    scheduler.request_run((a.id,))
    scheduler.request_run()  # "everything" replaces queued single-watch requests
    scheduler.request_run((a.id,))
    assert scheduler.run_pending() is True and scheduler.run_pending() is False
    with world.db.connect() as conn:
        assert [(r.trigger, r.watches) for r in runs.latest(conn)] == [("manual", 2), ("manual", 1)]


def test_scheduler_thread_starts_and_stops(world):
    world.add_watch()
    world.ldlc.results = [offer(900)]
    with world.db.connect() as conn:
        runs.start(conn, "schedule", NOW)  # left "running" by a previous process
    scheduler = make_scheduler(world, tick_seconds=0.01)
    scheduler.start()
    scheduler.start()  # idempotent
    scheduler.request_run()
    for _ in range(200):
        with world.db.connect() as conn:
            done = [r for r in runs.latest(conn) if r.trigger == "manual" and r.finished_at]
        if done:
            break
        import time

        time.sleep(0.01)
    scheduler.stop()
    assert done and done[0].status == "ok"
    with world.db.connect() as conn:
        assert runs.latest(conn)[-1].status == "failed"  # the abandoned one was closed


def test_slow_pattern_is_reported_and_keeps_existing_offers(world, monkeypatch):
    from pricewatch import deals

    w = world.add_watch()
    world.ldlc.results = [offer(900)]
    world.checker.run()
    monkeypatch.setattr(deals.regex, "search", lambda *a, **k: (_ for _ in ()).throw(TimeoutError()))
    run = world.checker.run()
    assert run.status == "partial" and "takes too long" in run.errors[0][2]
    assert len(world.listings(w)) == 1  # nothing was wiped: the shop answered, our filter did not
    with world.db.connect() as conn:
        assert runs.health_get(conn, "ldlc").consecutive_failures == 0  # not the shop's fault
