import dataclasses
import sqlite3

import pytest

from pricewatch.db import Database, iso, parse_iso
from pricewatch.models import Alert, Channel, ProductURL, Settings, SiteHealth, Watch
from pricewatch.repos import alerts, channels, listings, runs, settings, users, watches
from pricewatch.scrapers.base import Listing
from pricewatch.security import SecretBox

NOW = "2026-11-25T09:00:00Z"
LATER = "2026-11-25T09:30:00Z"


def make_watch(**kw):
    return Watch(**{**dict(name="RTX 5070", query="rtx 5070", match="RTX"), **kw})


def make_listing(**kw):
    return Listing(**{**dict(site="LDLC", title="RTX 5070", price=600.0, url="https://x/1", in_stock=True,
                             source="ldlc"), **kw})


def test_migrate_is_idempotent(db: Database):
    assert db.migrate() == 1
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


def test_connect_rolls_back_on_error(db: Database):
    with pytest.raises(RuntimeError):
        with db.connect() as conn:
            users.create(conn, "a@example.org", "hash", True, NOW)
            raise RuntimeError("boom")
    with db.connect() as conn:
        assert users.count(conn) == 0


def test_iso_round_trip(clock):
    assert parse_iso(iso(clock())) == clock()
    assert parse_iso(None) is None and parse_iso("") is None


def test_users(db: Database):
    with db.connect() as conn:
        admin = users.create(conn, "Admin@Example.org", "h1", True, NOW)
        users.create(conn, "bob@example.org", "h2", False, NOW)
        assert users.count(conn) == 2 and users.count_admins(conn) == 1
        assert users.get_by_email(conn, "admin@example.org").id == admin.id  # case-insensitive
        assert [u.email for u in users.list_all(conn)] == ["Admin@Example.org", "bob@example.org"]
        with pytest.raises(sqlite3.IntegrityError):
            users.create(conn, "ADMIN@example.org", "h3", False, NOW)
        users.set_password(conn, admin.id, "h9")
        changed = users.get(conn, admin.id)
        assert changed.password_hash == "h9" and changed.session_version == admin.session_version + 1
        users.replace_hash(conn, admin.id, "h10")
        assert users.get(conn, admin.id).session_version == changed.session_version
        users.set_admin(conn, admin.id, False)
        assert users.count_admins(conn) == 0
        users.delete(conn, admin.id)
        assert users.get(conn, admin.id) is None


def test_settings_defaults_and_round_trip(db: Database):
    with db.connect() as conn:
        assert settings.load(conn) == Settings()
        custom = dataclasses.replace(Settings(), deal_drop_pct=5.5, in_stock_only=False, check_interval_minutes=60)
        settings.save(conn, custom)
        assert settings.load(conn) == custom


def test_settings_ignore_corrupt_values(db: Database):
    with db.connect() as conn:
        conn.execute("INSERT INTO settings (key, value) VALUES ('baseline_days', '\"many\"')")
        conn.execute("INSERT INTO settings (key, value) VALUES ('in_stock_only', 'not json')")
        conn.execute("INSERT INTO settings (key, value) VALUES ('deal_drop_pct', '12')")
        loaded = settings.load(conn)
    assert loaded.baseline_days == Settings().baseline_days
    assert loaded.in_stock_only is True
    assert loaded.deal_drop_pct == 12.0 and isinstance(loaded.deal_drop_pct, float)


def test_watches_round_trip(db: Database):
    full = make_watch(
        exclude="PC", min_price=150, max_price=900, deal_drop_pct=5, in_stock_only=False, notify_restock=True,
        sites=("ldlc", "idealo"), site_urls={"ldlc": "https://www.ldlc.com/x"},
        product_urls=(ProductURL("https://shop.example/p", ".price"),), channel_ids=(1, 2), enabled=False,
    )
    with db.connect() as conn:
        saved = watches.create(conn, full, NOW)
        assert saved.id is not None and saved.created_at == NOW
        for name in ("exclude", "min_price", "max_price", "deal_drop_pct", "in_stock_only", "notify_restock",
                     "sites", "product_urls", "channel_ids", "enabled"):
            assert getattr(saved, name) == getattr(full, name), name
        assert dict(saved.site_urls) == {"ldlc": "https://www.ldlc.com/x"}
        assert watches.get_by_name(conn, "rtx 5070").id == saved.id

        updated = watches.update(conn, dataclasses.replace(saved, name="RTX 5070 Ti", channel_ids=None), LATER)
        assert updated.name == "RTX 5070 Ti" and updated.channel_ids is None and updated.updated_at == LATER
        assert updated.in_stock_only is False

        watches.set_enabled(conn, saved.id, True, LATER)
        watches.mark_checked(conn, saved.id, LATER, (("LDLC", "HTTP 503"),))
        again = watches.get(conn, saved.id)
        assert again.enabled and again.last_checked_at == LATER and again.last_errors == (("LDLC", "HTTP 503"),)
        assert [w.id for w in watches.list_all(conn, enabled_only=True)] == [saved.id]

        watches.delete(conn, saved.id)
        assert watches.get(conn, saved.id) is None and watches.list_all(conn) == []


def test_watch_names_are_unique(db: Database):
    with db.connect() as conn:
        watches.create(conn, make_watch(), NOW)
        with pytest.raises(sqlite3.IntegrityError):
            watches.create(conn, make_watch(name="rtx 5070"), NOW)


def test_listings_price_points_only_on_change(db: Database):
    with db.connect() as conn:
        w = watches.create(conn, make_watch(), NOW)
        first = make_listing()
        lid = listings.save(conn, w.id, first, None, None, NOW)
        stored = listings.get(conn, w.id, first.url)
        assert stored.id == lid and stored.active and stored.first_seen == NOW and stored.notified_price is None

        listings.save(conn, w.id, first, stored, None, LATER)  # nothing changed
        assert len(listings.price_points(conn, lid)) == 1

        cheaper = dataclasses.replace(first, price=550.0)
        listings.save(conn, w.id, cheaper, listings.get(conn, w.id, first.url), 550.0, LATER)
        stored = listings.get(conn, w.id, first.url)
        assert stored.price == 550.0 and stored.notified_price == 550.0 and stored.notified_at == LATER
        assert stored.first_seen == NOW and stored.last_seen == LATER

        gone = dataclasses.replace(cheaper, in_stock=False)
        listings.save(conn, w.id, gone, stored, 550.0, "2026-11-25T10:00:00Z")
        points = listings.price_points(conn, lid)
        assert [(p[1], p[2]) for p in points] == [(600.0, True), (550.0, True), (550.0, False)]
        assert listings.get(conn, w.id, first.url).notified_at == LATER  # unchanged notification keeps its date

        assert listings.prune_price_points(conn, LATER) == 1
        assert len(listings.price_points(conn, lid)) == 2


def test_listings_deactivation(db: Database):
    with db.connect() as conn:
        w = watches.create(conn, make_watch(), NOW)
        for url, source in (("u1", "ldlc"), ("u2", "ldlc"), ("u3", "idealo"), ("u4", "product"), ("u5", "product")):
            listings.save(conn, w.id, make_listing(url=url, source=source), None, None, NOW)

        listings.deactivate_missing(conn, w.id, (), ("u1",))  # nothing scraped: nothing hidden
        assert len(listings.for_watch(conn, w.id)) == 5

        listings.deactivate_missing(conn, w.id, ("ldlc",), ("u1",))  # ldlc answered without u2
        assert {x.url for x in listings.for_watch(conn, w.id)} == {"u1", "u3", "u4", "u5"}

        listings.deactivate_urls(conn, w.id, "product", ("u4",))
        assert {x.url for x in listings.for_watch(conn, w.id)} == {"u1", "u3", "u4"}

        listings.deactivate_other_sources(conn, w.id, ("ldlc", "product"))
        assert {x.url for x in listings.for_watch(conn, w.id)} == {"u1", "u4"}
        assert len(listings.for_watch(conn, w.id, active_only=False)) == 5

        listings.deactivate_missing(conn, w.id, ("ldlc",), ())
        listings.deactivate_urls(conn, w.id, "product", ())
        assert listings.for_watch(conn, w.id) == []


def test_daily_lows_keep_the_minimum(db: Database):
    with db.connect() as conn:
        w = watches.create(conn, make_watch(), NOW)
        listings.record_daily_low(conn, w.id, "2026-11-24", 700)
        listings.record_daily_low(conn, w.id, "2026-11-25", 650)
        listings.record_daily_low(conn, w.id, "2026-11-25", 690)
        listings.record_daily_low(conn, w.id, "2026-11-25", 640)
        assert listings.daily_lows(conn, w.id) == {"2026-11-24": 700, "2026-11-25": 640}
        assert listings.daily_lows(conn, w.id, since="2026-11-25") == {"2026-11-25": 640}
        assert listings.daily_lows_by_watch(conn) == {w.id: {"2026-11-24": 700, "2026-11-25": 640}}


def test_cheapest_and_counts_by_watch(db: Database):
    with db.connect() as conn:
        w = watches.create(conn, make_watch(), NOW)
        listings.save(conn, w.id, make_listing(url="a", price=500, in_stock=False), None, None, NOW)
        listings.save(conn, w.id, make_listing(url="b", price=620, in_stock=None), None, None, NOW)
        listings.save(conn, w.id, make_listing(url="c", price=700), None, None, NOW)
        assert listings.cheapest_by_watch(conn)[w.id].url == "b"  # the out-of-stock one is skipped
        assert listings.counts_by_watch(conn) == {w.id: 3}


def test_deleting_a_watch_cascades(db: Database):
    with db.connect() as conn:
        w = watches.create(conn, make_watch(), NOW)
        lid = listings.save(conn, w.id, make_listing(), None, None, NOW)
        listings.record_daily_low(conn, w.id, "2026-11-25", 600)
        alert_id = alerts.create(conn, Alert(kind="deal", watch_id=w.id, watch_name=w.name), NOW)
        watches.delete(conn, w.id)
        assert listings.price_points(conn, lid) == [] and listings.daily_lows(conn, w.id) == {}
        kept = alerts.recent(conn)[0]
        assert kept.id == alert_id and kept.watch_id is None and kept.watch_name == "RTX 5070"


def test_channels_are_encrypted_at_rest(db: Database, box: SecretBox):
    with db.connect() as conn:
        created = channels.create(conn, box, Channel(name="Phone", type="ntfy", config={"token": "s3cret"}), NOW)
        raw = conn.execute("SELECT config FROM channels").fetchone()[0]
        assert "s3cret" not in raw
        assert dict(created.config) == {"token": "s3cret"} and created.readable and created.enabled
        assert created.events == ("deal", "restock", "health")

        changed = channels.update(conn, box, dataclasses.replace(created, name="Tel", events=("deal",),
                                                                 config={"token": "new"}))
        assert changed.name == "Tel" and changed.events == ("deal",) and dict(changed.config) == {"token": "new"}

        channels.set_enabled(conn, created.id, False)
        channels.record_result(conn, created.id, "HTTP 500", NOW)
        failed = channels.get(conn, box, created.id)
        assert not failed.enabled and failed.last_error == "HTTP 500" and failed.last_error_at == NOW
        channels.record_result(conn, created.id, None, LATER)
        ok = channels.get(conn, box, created.id)
        assert ok.last_ok_at == LATER and ok.last_error is None

        other = channels.get(conn, SecretBox("another-secret-key-another-secret-key-0000"), created.id)
        assert not other.readable and dict(other.config) == {}

        channels.delete(conn, created.id)
        assert channels.list_all(conn, box) == []


def test_alerts_and_deliveries(db: Database, box: SecretBox):
    with db.connect() as conn:
        w = watches.create(conn, make_watch(), NOW)
        c1 = channels.create(conn, box, Channel(name="A", type="ntfy", config={}), NOW)
        c2 = channels.create(conn, box, Channel(name="B", type="ntfy", config={}, enabled=False), NOW)
        a1 = alerts.create(conn, Alert(kind="deal", watch_id=w.id, watch_name=w.name, title="t", site="LDLC",
                                       url="u", price=1.0, previous_price=2.0, baseline=3.0, baseline_days=4,
                                       target_price=5.0, in_stock=True, detail="d"), NOW)
        a2 = alerts.create(conn, Alert(kind="health", title="LDLC broken"), LATER)
        for alert_id in (a1, a2):
            alerts.add_delivery(conn, alert_id, c1.id, NOW)
            alerts.add_delivery(conn, alert_id, c1.id, NOW)  # idempotent
            alerts.add_delivery(conn, alert_id, c2.id, NOW)

        newest_first = alerts.recent(conn)
        assert [a.id for a in newest_first] == [a2, a1]
        stored = newest_first[1]
        assert (stored.price, stored.previous_price, stored.baseline, stored.baseline_days, stored.target_price,
                stored.in_stock, stored.detail) == (1.0, 2.0, 3.0, 4, 5.0, True, "d")
        assert alerts.count(conn) == 2 and alerts.count(conn, watch_id=w.id) == 1
        assert [a.id for a in alerts.recent(conn, watch_id=w.id)] == [a1]
        assert [a.id for a in alerts.recent(conn, limit=1, offset=1)] == [a1]

        todo = alerts.pending(conn)
        assert [(d.channel_id, a.id) for d, a in todo] == [(c1.id, a1), (c1.id, a2)]  # disabled channel skipped

        alerts.mark_delivery(conn, todo[0][0].id, "sent", 1, None, LATER)
        alerts.mark_delivery(conn, todo[1][0].id, "pending", 1, "HTTP 500", LATER)
        summary = alerts.delivery_summary(conn, [a1, a2])
        assert summary[a1] == [("A", "sent", None), ("B", "pending", None)]
        assert summary[a2] == [("A", "pending", "HTTP 500"), ("B", "pending", None)]
        assert alerts.delivery_summary(conn, []) == {}

        assert alerts.expire_pending(conn, LATER, LATER) == 1  # only a1's remaining delivery is older
        assert alerts.prune(conn, LATER) == 1
        assert [a.id for a in alerts.recent(conn)] == [a2]


def test_runs_and_health(db: Database):
    with db.connect() as conn:
        assert runs.last_started(conn) is None
        first = runs.start(conn, "schedule", NOW)
        runs.finish(conn, first, status="partial", watches=2, listings=10, alerts=1,
                    errors=(("RTX 5070", "LDLC", "HTTP 503"),), now=LATER)
        second = runs.start(conn, "manual", LATER)
        assert runs.last_started(conn) == LATER and runs.last_started(conn, "schedule") == NOW
        assert runs.abandon_running(conn, LATER) == 1
        latest = runs.latest(conn)
        assert [r.id for r in latest] == [second, first]
        assert latest[0].status == "failed" and "restart" in latest[0].errors[0][2]
        assert latest[1].errors == (("RTX 5070", "LDLC", "HTTP 503"),) and latest[1].listings == 10
        runs.prune(conn, keep=1)
        assert [r.id for r in runs.latest(conn)] == [second]

        assert runs.health_get(conn, "ldlc") == SiteHealth("ldlc", 0, None, None, None, False)
        runs.health_save(conn, SiteHealth("ldlc", 3, NOW, "HTTP 503", LATER, True))
        runs.health_save(conn, SiteHealth("ldlc", 4, NOW, "HTTP 503", LATER, True))
        assert runs.health_all(conn) == [SiteHealth("ldlc", 4, NOW, "HTTP 503", LATER, True)]
