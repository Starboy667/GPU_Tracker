"""The rules of who sees and changes what, and the upgrade of a database that predates them."""
import sqlite3

import pytest

from pricewatch import access, forms
from pricewatch.db import MIGRATIONS, Database
from pricewatch.models import Channel, User, Watch
from pricewatch.repos import channels, listings, watches
from pricewatch.scrapers import SCRAPERS, validate_site_url
from pricewatch.security import SecretBox

NOW = "2026-11-25T09:00:00Z"


def user(user_id, admin=False):
    return User(id=user_id, email=f"u{user_id}@example.org", password_hash="h", is_admin=admin, session_version=1, created_at=NOW)


def watch(**kw):
    return Watch(**{**dict(name="W", query="w", match="w", id=1, owner_id=1), **kw})


OWNER, FRIEND, STRANGER, ADMIN = user(1), user(2), user(3), user(9, admin=True)


@pytest.mark.parametrize("visibility,shared_with,friend,stranger", [
    ("private", (), False, False),
    ("private", (2,), False, False),  # a stale list means nothing while the watch is private
    ("shared", (2,), True, False),
    ("shared", (), False, False),
    ("public", (), True, True),
])
def test_who_can_view(visibility, shared_with, friend, stranger):
    w = watch(visibility=visibility, shared_with=shared_with)
    assert access.can_view(OWNER, w)
    assert access.can_view(FRIEND, w) is friend
    assert access.can_view(STRANGER, w) is stranger
    assert access.can_view(ADMIN, w) is (visibility == "public")  # administrators get no special view
    assert [x.id for x in access.visible(FRIEND, [w])] == ([1] if friend else [])


def test_only_the_owner_changes_a_watch():
    w = watch(visibility="public")
    assert access.owns(OWNER, w) and not access.owns(FRIEND, w) and not access.owns(ADMIN, w)
    orphan = watch(owner_id=None)
    assert access.owns(ADMIN, orphan) and not access.owns(FRIEND, orphan)
    assert access.can_view(ADMIN, orphan) and not access.can_view(FRIEND, orphan)


def test_link_needs_a_token_and_a_watch_that_is_not_private():
    assert access.link_is_active(watch(visibility="shared", share_token="t"))
    assert access.link_is_active(watch(visibility="public", share_token="t"))
    assert not access.link_is_active(watch(visibility="private", share_token="t"))
    assert not access.link_is_active(watch(visibility="public", share_token=None))
    assert not access.link_is_active(watch(visibility="public", share_token=""))


def test_who_may_follow():
    assert access.may_follow(2, watch(visibility="public")) and access.may_follow(2, watch(visibility="shared", shared_with=(2,)))
    assert not access.may_follow(3, watch(visibility="shared", shared_with=(2,)))
    assert not access.may_follow(2, watch(visibility="private", shared_with=(2,)))
    assert not access.may_follow(1, watch(visibility="public"))  # the owner is not a follower


def test_channels_belong_to_one_account():
    mine = Channel(name="c", type="ntfy", config={}, owner_id=1)
    assert access.owns_channel(OWNER, mine) and not access.owns_channel(FRIEND, mine) and not access.owns_channel(ADMIN, mine)
    orphan = Channel(name="c", type="ntfy", config={})
    assert access.owns_channel(ADMIN, orphan) and not access.owns_channel(OWNER, orphan)


# ------------------------------------------------------------------ the form

def parse(form, **kw):
    args = dict(site_keys=SCRAPERS, channel_ids=[], validate_site_url=validate_site_url, owner_id=1,
                shareable_user_ids=[2, 3], new_token=lambda: "fresh-token")
    return forms.parse_watch({"name": "TV", "site_ldlc": "on", **form}, **{**args, **kw})


def test_form_defaults_to_private_and_sets_the_owner():
    w, errors = parse({})
    assert errors == {} and (w.owner_id, w.visibility, w.shared_with, w.share_token) == (1, "private", (), None)
    assert parse({"share_link": "on", "share_user_2": "on"})[0].share_token is None  # nothing leaks from a private watch


def test_form_shared_and_public():
    w, _ = parse({"visibility": "shared", "share_user_3": "on", "share_user_7": "on", "share_user_1": "on"})
    assert (w.visibility, w.shared_with, w.share_token) == ("shared", (3,), None)  # only real, other accounts
    w, _ = parse({"visibility": "shared", "share_link": "on"})
    assert (w.shared_with, w.share_token) == ((), "fresh-token")
    w, _ = parse({"visibility": "public", "share_user_2": "on", "share_link": "on"})
    assert (w.visibility, w.shared_with, w.share_token) == ("public", (), "fresh-token")
    assert "visibility" in parse({"visibility": "shared"})[1]
    assert "visibility" in parse({"visibility": "world"})[1]


def test_an_edit_that_says_nothing_about_sharing_leaves_it_alone():
    existing = watch(owner_id=5, visibility="shared", shared_with=(2,), share_token="kept-token", name="TV")
    w, errors = parse({}, existing=existing)
    assert errors == {} and (w.visibility, w.shared_with, w.share_token) == ("shared", (2,), "kept-token")
    assert parse({"share_link": "on"}, existing=existing)[0].visibility == "shared"
    assert parse({}, existing=None)[0].visibility == "private"  # a new watch is private unless told otherwise


def test_form_keeps_owner_and_link_of_an_existing_watch():
    existing = watch(owner_id=5, visibility="public", share_token="kept-token", name="TV")
    w, _ = parse({"visibility": "public", "share_link": "on"}, existing=existing, owner_id=1)
    assert (w.owner_id, w.share_token) == (5, "kept-token")
    assert parse({"visibility": "public"}, existing=existing)[0].share_token is None  # unticked: the link is gone
    assert parse({"visibility": "private", "share_link": "on"}, existing=existing)[0].share_token is None


# ------------------------------------------------------------------ upgrading a database from before owners existed

def old_database(path) -> Database:
    """A database as version 0.3 left it: schema 1, with data."""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(f"BEGIN;{MIGRATIONS[0]}PRAGMA user_version = 1;COMMIT;")
    conn.executescript("""
        INSERT INTO users (id, email, password_hash, is_admin, created_at) VALUES
            (1, 'member@example.org', 'h', 0, '2026-11-20T00:00:00Z'),
            (2, 'boss@example.org', 'h', 1, '2026-11-21T00:00:00Z'),
            (3, 'second-admin@example.org', 'h', 1, '2026-11-22T00:00:00Z');
        INSERT INTO watches (id, name, query, match, sites, channel_ids, created_at, updated_at, last_errors) VALUES
            (4, '9070 XT', '9070 xt', '9070', '["ldlc"]', '[1]', '2026-11-25T09:00:00Z', '2026-11-25T09:00:00Z', '[["LDLC","HTTP 503"]]'),
            (7, 'RTX 5070 ti', 'rtx 5070 ti', '5070', '["idealo"]', NULL, '2026-11-25T09:00:00Z', '2026-11-25T09:00:00Z', '[]');
        INSERT INTO listings (id, watch_id, url, source, site, title, price, first_seen, last_seen)
            VALUES (1, 4, 'u1', 'ldlc', 'LDLC', 't', 700, '2026-11-25T09:00:00Z', '2026-11-25T09:00:00Z');
        INSERT INTO price_points (listing_id, ts, price) VALUES (1, '2026-11-25T09:00:00Z', 700);
        INSERT INTO daily_lows (watch_id, day, price) VALUES (4, '2026-11-25', 700), (7, '2026-11-25', 800);
        INSERT INTO alerts (id, kind, watch_id, watch_name, created_at) VALUES (1, 'deal', 7, 'RTX 5070 ti', '2026-11-25T09:00:00Z');
        INSERT INTO channels (id, name, type, config, created_at) VALUES (1, 'Phone', 'ntfy', 'sealed', '2026-11-25T09:00:00Z');
        INSERT INTO deliveries (alert_id, channel_id, status, updated_at) VALUES (1, 1, 'sent', '2026-11-25T09:00:00Z');
    """)
    conn.close()
    return Database(path)


def test_upgrade_keeps_everything_and_everyone_keeps_seeing_it(tmp_path):
    db = old_database(tmp_path / "old.db")
    assert db.migrate() == 2
    with db.connect() as conn:
        migrated = {w.id: w for w in watches.list_all(conn)}
        assert sorted(migrated) == [4, 7]
        for w in migrated.values():  # what everybody saw stays visible to everybody, owned by the first administrator
            assert (w.owner_id, w.visibility, w.share_token, w.shared_with) == (2, "public", None, ())
        assert (migrated[4].name, migrated[4].sites, migrated[4].channel_ids, migrated[4].last_errors) == (
            "9070 XT", ("ldlc",), (1,), (("LDLC", "HTTP 503"),))
        assert len(listings.for_watch(conn, 4)) == 1 and listings.daily_lows(conn, 7) == {"2026-11-25": 800}
        assert conn.execute("SELECT COUNT(*) FROM price_points").fetchone()[0] == 1
        assert conn.execute("SELECT watch_id FROM alerts").fetchone()[0] == 7
        assert conn.execute("SELECT owner_id FROM channels").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        # References still work after the table was rebuilt: deleting a watch takes its rows with it.
        watches.delete(conn, 4)
        assert conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM price_points").fetchone()[0] == 0
        assert listings.daily_lows(conn, 4) == {} and listings.daily_lows(conn, 7) != {}
        # Names are now unique per owner, not across the instance.
        watches.create(conn, watch(id=None, name="rtx 5070 TI", owner_id=1), NOW)
        with pytest.raises(sqlite3.IntegrityError):
            watches.create(conn, watch(id=None, name="rtx 5070 TI", owner_id=2), NOW)
    assert db.migrate() == 2  # and running it again changes nothing


def test_watch_ids_are_never_reused(tmp_path):
    """Past checks remember watches by id: a new watch must not pass for a deleted one."""
    db = old_database(tmp_path / "old.db")
    db.migrate()
    with db.connect() as conn:
        watches.delete(conn, 7)  # the highest id
        fresh = watches.create(conn, watch(id=None, name="New", owner_id=1), NOW)
        assert fresh.id == 8


def test_a_migration_is_not_applied_twice_by_two_processes(tmp_path):
    db = old_database(tmp_path / "old.db")
    assert db._apply(2, MIGRATIONS[1]) is True
    with db.connect() as conn:
        conn.execute("UPDATE watches SET visibility = 'private' WHERE id = 4")  # life goes on after the upgrade
    assert db._apply(2, MIGRATIONS[1]) is False  # the latecomer sees it is done and touches nothing
    with db.connect() as conn:
        assert watches.get(conn, 4).visibility == "private" and len(watches.list_all(conn)) == 2


def test_a_failing_migration_changes_nothing(tmp_path):
    db = old_database(tmp_path / "old.db")
    broken = MIGRATIONS[1] + "\nINSERT INTO no_such_table VALUES (1);\n"
    with pytest.raises(sqlite3.Error):
        db._apply(2, broken)
    raw = sqlite3.connect(db.path)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 1
    assert "owner_id" not in [r[1] for r in raw.execute("PRAGMA table_info(watches)")]
    assert raw.execute("SELECT COUNT(*) FROM watches").fetchone()[0] == 2
    raw.close()
    assert db.migrate() == 2  # and the real one still applies afterwards


def test_migration_scripts_split_into_whole_statements():
    from pricewatch.db import _statements

    for script in MIGRATIONS:
        statements = list(_statements(script))
        assert statements and all(sqlite3.complete_statement(s) for s in statements)
    assert sum(s.startswith("CREATE TABLE") for s in _statements(MIGRATIONS[1])) == 3
    with pytest.raises(sqlite3.ProgrammingError):
        list(_statements("CREATE TABLE t (a);\nCREATE TABLE u (b"))


def test_ownerless_rows_go_to_the_first_administrator_at_start(tmp_path, config):
    import dataclasses

    from pricewatch.repos import users
    from pricewatch.web.app import adopt_ownerless, build_services

    svc = build_services(dataclasses.replace(config, data_dir=tmp_path), scrapers={}, product_scraper=lambda *a: None,
                         send=lambda *a, **k: None)
    svc.db.migrate()
    adopt_ownerless(svc)  # nobody to give them to yet: nothing happens
    with svc.db.connect() as conn:
        watches.create(conn, watch(id=None, name="Imported", owner_id=None), NOW)
        watches.create(conn, watch(id=None, name="Taken", owner_id=None), NOW)
        channels.create(conn, svc.box, Channel(name="Old", type="ntfy", config={}), NOW)
        users.create(conn, "member@example.org", "h", False, NOW)
        boss = users.create(conn, "boss@example.org", "h", True, NOW)
        watches.create(conn, watch(id=None, name="Taken", owner_id=boss.id), NOW)
    adopt_ownerless(svc)
    adopt_ownerless(svc)
    with svc.db.connect() as conn:
        owners = {(w.name, w.owner_id) for w in watches.list_all(conn)}
        # "Taken" clashes with a name the administrator already uses: it waits for a rename.
        assert owners == {("Imported", boss.id), ("Taken", boss.id), ("Taken", None)}
        assert [c.owner_id for c in channels.list_all(conn, svc.box)] == [boss.id]


def test_upgrade_without_any_account_leaves_watches_for_an_administrator_to_claim(tmp_path):
    db = old_database(tmp_path / "old.db")
    raw = sqlite3.connect(db.path)
    raw.execute("DELETE FROM users")
    raw.commit()
    raw.close()
    db.migrate()
    with db.connect() as conn:
        assert {w.owner_id for w in watches.list_all(conn)} == {None}
        assert [c.owner_id for c in channels.list_all(conn, SecretBox("k" * 40))] == [None]


# ------------------------------------------------------------------ what the Status page tells whom

def test_problems_of_a_check_are_filtered_by_what_the_viewer_may_see():
    from pricewatch.models import Run, SiteHealth
    from pricewatch.web.routes.dashboard import visible_health, visible_problems

    run = Run(
        id=1, started_at=NOW, finished_at=NOW, trigger="schedule", status="partial", watches=3, listings=0, alerts=0,
        errors=(("Mine", "LDLC", "HTTP 503"), ("Theirs", "LDLC", "HTTP 503"), ("", "", "internal error (X)"),
                ("Old row", "LDLC", "HTTP 500")),
        error_watch_ids=(10, 20, None),  # shorter than errors on purpose: rows written before ids were kept
    )
    assert visible_problems(FRIEND, run, {10}) == [("Mine", "LDLC", "HTTP 503"), ("", "", "internal error (X)")]
    assert visible_problems(ADMIN, run, {20}) == [
        ("Theirs", "LDLC", "HTTP 503"), ("", "", "internal error (X)"), ("Old row", "LDLC", "HTTP 500")]
    assert visible_problems(FRIEND, run, set()) == [("", "", "internal error (X)")]

    health = [SiteHealth("ldlc", 0, NOW, None, None, False), SiteHealth("product:secret.example", 3, None, "HTTP 403", NOW, True)]
    assert [h.site for h in visible_health(FRIEND, health)] == ["ldlc"]
    assert [h.site for h in visible_health(ADMIN, health)] == ["ldlc", "product:secret.example"]


def test_alert_feed_filter():
    from pricewatch.repos import alerts

    assert alerts._visible_clause([], alerts.HEALTH_NONE) == ("WHERE 0", [])
    assert alerts._visible_clause([3, 4], alerts.HEALTH_NONE) == ("WHERE watch_id IN (?, ?)", [3, 4])
    clause, params = alerts._visible_clause([3], alerts.HEALTH_SHOPS)
    assert clause == "WHERE watch_id IN (?) OR (kind = 'health' AND site != '' AND site NOT LIKE ?)"
    assert params == [3, "product:%"]
    assert alerts._visible_clause([], alerts.HEALTH_ALL) == ("WHERE watch_id IS NULL", [])


def test_alert_feed_returns_only_visible_alerts(tmp_path):
    from pricewatch.models import Alert
    from pricewatch.repos import alerts

    db = Database(tmp_path / "a.db")
    db.migrate()
    with db.connect() as conn:
        mine = watches.create(conn, watch(id=None, name="Mine", owner_id=None), NOW)
        theirs = watches.create(conn, watch(id=None, name="Theirs", owner_id=None), NOW)
        for kind, watch_id, site in (("deal", mine.id, "LDLC"), ("deal", theirs.id, "LDLC"), ("health", None, "ldlc"),
                                     ("health", None, "product:secret.example"), ("health", None, "")):
            alerts.create(conn, Alert(kind=kind, watch_id=watch_id, watch_name="w", site=site), NOW)
        assert alerts.count_visible(conn, [mine.id]) == 1
        # Members see scraper alerts about shops; not those about a product-page host, nor the
        # old ones that do not say what they are about.
        assert [a.site for a in alerts.recent_visible(conn, [mine.id], health=alerts.HEALTH_SHOPS)] == ["ldlc", "LDLC"]
        assert alerts.count_visible(conn, [mine.id, theirs.id], health=alerts.HEALTH_ALL) == 5
        # What earlier versions left behind when a watch was deleted: nobody's, so administrators only.
        alerts.create(conn, Alert(kind="deal", watch_id=None, watch_name="Gone"), NOW)
        assert alerts.count_visible(conn, [mine.id], health=alerts.HEALTH_SHOPS) == 2
        assert "Gone" in [a.watch_name for a in alerts.recent_visible(conn, [], health=alerts.HEALTH_ALL)]
        conn.execute("DELETE FROM alerts WHERE watch_name = 'Gone'")
        assert alerts.recent_visible(conn, [], health=alerts.HEALTH_NONE) == [] and alerts.count_visible(conn, []) == 0
        assert len(alerts.recent_visible(conn, [mine.id, theirs.id], health=alerts.HEALTH_ALL, limit=2, offset=4)) == 1
