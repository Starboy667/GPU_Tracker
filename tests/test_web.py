"""The web app driven like a browser: real routes, forms, sessions and CSRF tokens; fake shops."""
import dataclasses
import re

import pytest
from fastapi.testclient import TestClient

from pricewatch.models import ProductURL
from pricewatch.notify.base import NotifyError
from pricewatch.repos import alerts as alerts_repo
from pricewatch.repos import channels as channels_repo
from pricewatch.repos import listings as listings_repo
from pricewatch.repos import runs as runs_repo
from pricewatch.repos import users as users_repo
from pricewatch.repos import watches as watches_repo
from pricewatch.scrapers.base import KIND_RETAILER, Listing, ScrapeError, SiteScraper
from pricewatch.web.app import bootstrap_admin, build_services, create_app
from tests.conftest import ADMIN, TOKEN, Site

def offer(price=900.0, url="https://www.ldlc.com/fiche/1.html", title="MSI RTX 5070 12G", in_stock=True):
    return Listing(site="LDLC", title=title, price=price, url=url, in_stock=in_stock, source="ldlc")


WATCH = {"name": "RTX 5070", "site_ldlc": "on", "enabled": "on", "max_price": "950"}
NTFY = {"name": "Phone", "cfg_topic": "deals", "event_deal": "on", "event_health": "on", "enabled": "on"}


# ------------------------------------------------------------------ authentication

def test_pages_require_a_session(site):
    for path in ("/alerts", "/channels", "/status", "/settings", "/users", "/account", "/watches/new",
                 "/watches/1", "/watches/export.yaml"):
        response = site.get(path)
        assert response.status_code == 303 and response.headers["location"].startswith("/login"), path
    assert site.get("/watches/1").headers["location"] == "/login?next=%2Fwatches%2F1"
    assert site.client.post("/check", data={}, follow_redirects=False).headers["location"] == "/login"
    assert site.get("/healthz").json() == {"status": "ok"}
    assert site.get("/static/app.css").status_code == 200


def test_login_logout_flow(site):
    page = site.get("/login")
    assert page.status_code == 200 and "Sign in" in page.text
    wrong = site.login(password="nope nope nope")
    assert wrong.status_code == 401 and "Wrong email or password." in wrong.text
    unknown = site.login(email="ghost@example.org")
    assert unknown.status_code == 401 and "Wrong email or password." in unknown.text  # same answer: no user enumeration

    ok = site.login(next="/alerts")
    assert ok.status_code == 303 and ok.headers["location"] == "/alerts"
    assert "pricewatch_session" in ok.headers["set-cookie"] and "httponly" in ok.headers["set-cookie"].lower()
    assert site.get("/").status_code == 200
    assert site.get("/login").status_code == 303  # already signed in

    assert site.post("/logout").headers["location"] == "/login"
    assert site.get("/account").status_code == 303


def test_login_ignores_offsite_next(site):
    for target in ("https://evil.example/", "//evil.example/", "/\\evil.example"):
        site.client.cookies.clear()
        assert site.login(next=target).headers["location"] == "/"


def test_login_is_rate_limited(site):
    for _ in range(5):
        assert site.login(password="wrong wrong wrong").status_code == 401
    blocked = site.login()  # even the right password has to wait now
    assert blocked.status_code == 429 and "Too many failed attempts" in blocked.text
    assert site.get("/account").status_code == 303


def test_one_address_cannot_lock_an_account_for_everyone(site):
    """Five bad guesses block the guessing address, not the account."""
    for _ in range(5):
        site.login(password="wrong wrong wrong")
    assert site.login().status_code == 429
    assert site.svc.account_limiter.retry_after(next(iter(site.svc.account_limiter._failures))) == 0
    site.svc.limiter.reset("ip:testclient")  # the owner, from another address
    assert site.login().status_code == 303


def test_an_account_is_throttled_across_addresses_after_many_failures(site):
    for attempt in range(50):
        site.svc.limiter.reset("ip:testclient")  # as if each guess came from a new address
        assert site.login(password="wrong wrong wrong").status_code == 401, attempt
    site.svc.limiter.reset("ip:testclient")
    assert site.login().status_code == 429


def test_login_throttling_keys_stay_small(site):
    huge = "a" * 300_000 + "@example.org"
    assert site.login(email=huge, password="x" * 500_000).status_code == 401
    keys = [*site.svc.limiter._failures, *site.svc.account_limiter._failures]
    assert keys and max(len(key) for key in keys) < 80


def test_post_without_csrf_token_is_refused(admin):
    for token in ("", "forged"):
        response = admin.post("/watches", WATCH, token=token)
        assert response.status_code == 403 and "expired" in response.text
    with admin.db() as conn:
        assert watches_repo.list_all(conn) == []
    assert admin.post("/login", {"email": ADMIN[0], "password": ADMIN[1]}, token="forged").status_code == 403


def test_security_headers(site):
    headers = site.get("/login").headers
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert "script-src 'self'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff" and headers["x-frame-options"] == "DENY"
    assert headers["cache-control"] == "no-store" and headers["referrer-policy"] == "same-origin"
    assert "strict-transport-security" not in headers  # plain http in tests
    assert "no-store" not in site.get("/static/app.css").headers.get("cache-control", "")


def test_hsts_and_secure_cookie_when_served_over_https(config, clock):
    secure = dataclasses.replace(config, cookie_secure=True, data_dir=config.data_dir / "secure")
    services = build_services(secure, scrapers={}, product_scraper=lambda *a: None, send=lambda *a, **k: None,
                              clock=clock)
    with TestClient(create_app(secure, services=services), base_url="https://testserver") as client:
        page = client.get("/login")
        assert "max-age=" in page.headers["strict-transport-security"]
        token = TOKEN.search(page.text).group(1)
        done = client.post("/login", data={"csrf_token": token, "email": ADMIN[0], "password": ADMIN[1]},
                           follow_redirects=False)
        assert "secure" in done.headers["set-cookie"].lower()


def test_session_ends_when_the_password_changes_elsewhere(admin):
    with admin.db() as conn:
        users_repo.set_password(conn, users_repo.get_by_email(conn, ADMIN[0]).id, "x")
    assert admin.get("/account").status_code == 303


def test_bootstrap_admin_rules(config, clock, tmp_path):
    from pricewatch.config import ConfigError

    def fresh(**changes):
        cfg = dataclasses.replace(config, data_dir=tmp_path / str(len(changes)) / "x", **changes)
        services = build_services(cfg, scrapers={}, product_scraper=lambda *a: None, send=lambda *a, **k: None)
        services.db.migrate()
        return services

    none = fresh(admin_email="", admin_password="")
    bootstrap_admin(none)  # allowed: the operator can use the CLI instead
    with none.db.connect() as conn:
        assert users_repo.count(conn) == 0
    with pytest.raises(ConfigError):
        bootstrap_admin(fresh(admin_email="not-an-email"))
    with pytest.raises(ConfigError):
        bootstrap_admin(fresh(admin_password="short", admin_email="a@example.org"))
    once = fresh(admin_email="A@Example.org", admin_password="long enough password", timezone="UTC")
    bootstrap_admin(once)
    bootstrap_admin(once)
    with once.db.connect() as conn:
        assert [(u.email, u.is_admin) for u in users_repo.list_all(conn)] == [("a@example.org", True)]


# ------------------------------------------------------------------ watches

def test_empty_dashboard_teaches_the_first_step(admin):
    page = admin.get("/")
    assert "Nothing is being watched yet" in page.text and "/watches/new" in page.text


def test_new_watch_starts_with_a_preset(admin):
    picker = admin.get("/watches/new")
    assert "Graphics card" in picker.text and "/watches/new?preset=tv" in picker.text
    assert "Video game (PC)" in picker.text
    game = admin.get("/watches/new?preset=game").text
    assert 'name="site_steam" checked' in game and 'name="site_dlcompare" checked' in game
    assert 'name="site_ldlc" checked' not in game and "Vente de compte" in game
    assert game.index("Shops and comparators") < game.index("Video games") < game.index('name="site_steam"')
    form = admin.get("/watches/new?preset=gpu")
    assert 'name="site_ldlc" checked' in form.text and "watercooling" in form.text
    assert 'name="preset" value="gpu"' in form.text


def test_create_watch_then_first_check_finds_a_deal(admin):
    admin.post("/channels?type=ntfy", NTFY)
    admin.shop["results"] = [offer(900), offer(990, "https://www.ldlc.com/fiche/2.html"),
                             offer(100, "https://www.ldlc.com/fiche/3.html", title="RTX 5080")]
    created = admin.post("/watches", WATCH)
    assert created.status_code == 303 and created.headers["location"] == "/watches/1"
    assert admin.svc.scheduler.is_busy
    admin.run_checks()

    detail = admin.get("/watches/1")
    assert detail.status_code == 200
    assert "MSI RTX 5070 12G" in detail.text and "RTX 5080" not in detail.text
    assert detail.text.count('class="chip chip-deal">Deal') >= 1 and "target price" in detail.text
    assert 'data-chart' in detail.text and "Show these prices as a table" in detail.text
    assert [m.title for _, _, m in admin.sent] == ["🔥 1 deal"]

    dashboard = admin.get("/")
    assert "RTX 5070" in dashboard.text and "Deal" in dashboard.text and "Latest alerts" in dashboard.text
    alerts = admin.get("/alerts")
    assert "Phone: delivered" in alerts.text and "Target" in alerts.text
    assert "after the first scheduled check" in admin.get("/status").text
    admin.svc.checker.run()
    status = admin.get("/status")
    assert "Working" in status.text and "on request" in status.text and "scheduled" in status.text


def test_watch_form_errors_keep_what_was_typed(admin):
    response = admin.post("/watches", {"name": "", "match": "(", "exclude": "typed-exclude", "site_ldlc": "on"})
    assert response.status_code == 422
    assert "2 fields need a correction" in response.text and "Give this watch a name." in response.text
    assert 'value="typed-exclude"' in response.text
    with admin.db() as conn:
        assert watches_repo.list_all(conn) == []


def test_duplicate_watch_name_is_a_form_error(admin):
    admin.post("/watches", WATCH)
    again = admin.post("/watches", {**WATCH, "name": "rtx 5070"})
    assert again.status_code == 422 and "You already have a watch with this name." in again.text


def test_edit_pause_check_delete(admin):
    admin.post("/watches", WATCH)
    admin.run_checks()
    form = admin.get("/watches/1/edit")
    assert 'value="RTX 5070"' in form.text and 'value="950"' in form.text and "Save changes" in form.text

    bad = admin.post("/watches/1", {**WATCH, "min_price": "abc"})
    assert bad.status_code == 422 and "Enter a number." in bad.text
    saved = admin.post("/watches/1", {**WATCH, "name": "RTX 5070 non-Ti", "max_price": "800",
                                      "product_urls": "https://shop.example/card"})
    assert saved.headers["location"] == "/watches/1"
    admin.run_checks()
    with admin.db() as conn:
        watch = watches_repo.get(conn, 1)
        assert (watch.name, watch.max_price) == ("RTX 5070 non-Ti", 800)
        assert watch.product_urls == (ProductURL("https://shop.example/card"),)
        assert [l.site for l in listings_repo.for_watch(conn, 1)] == ["shop.example"]

    assert admin.post("/watches/1/toggle").status_code == 303
    assert "Paused" in admin.get("/watches/1").text and "Resume" in admin.get("/watches/1").text
    admin.post("/watches/1/toggle")
    assert admin.post("/watches/1/check").headers["location"] == "/watches/1"
    assert "updates by itself" in admin.get("/watches/1").text
    admin.run_checks()

    assert admin.post("/watches/1/delete").headers["location"] == "/"
    assert admin.get("/watches/1").status_code == 404
    for path in ("/watches/1/edit",):
        assert admin.get(path).status_code == 404
    for path in ("/watches/1", "/watches/1/toggle", "/watches/1/check", "/watches/1/delete"):
        assert admin.post(path, WATCH).status_code == 404


def test_paused_watch_is_created_without_a_check(admin):
    admin.post("/watches", {**WATCH, "enabled": ""})
    assert not admin.svc.scheduler.is_busy
    assert "created, paused" in admin.get("/").text


def test_preview_shows_matches_and_rejections_without_saving(admin):
    admin.shop["results"] = [offer(900), offer(100, "https://www.ldlc.com/fiche/3.html", title="RTX 5080 <b>bold</b>")]
    response = admin.post("/watches/preview", WATCH)
    assert response.status_code == 200 and "Test result" in response.text
    assert "1 offer would be part of this watch" in response.text and "1 was filtered out" in response.text
    assert "Title does not match" in response.text and "Nothing was saved." in response.text
    assert "&lt;b&gt;bold&lt;/b&gt;" in response.text and "<b>bold</b>" not in response.text  # scraped text is escaped
    with admin.db() as conn:
        assert watches_repo.list_all(conn) == []

    admin.shop["results"] = ScrapeError("HTTP 403 (blocked?)")
    failed = admin.post("/watches/preview", WATCH)
    assert "<strong>LDLC</strong> failed: HTTP 403 (blocked?)" in failed.text
    assert admin.post("/watches/preview", {"name": ""}).status_code == 422

    admin.post("/watches", WATCH)
    existing = admin.post("/watches/preview", {**WATCH, "watch_id": "1", "max_price": ""})
    assert "No alert threshold yet" in existing.text and 'action="/watches/1"' in existing.text


def test_scraped_links_are_only_rendered_when_http(admin):
    admin.shop["results"] = [offer(900, url="javascript:alert(1)")]
    admin.post("/watches", WATCH)
    admin.run_checks()
    page = admin.get("/watches/1").text
    assert "MSI RTX 5070 12G" in page and "javascript:" not in page


def test_import_and_export(admin):
    assert "Import watches" in admin.get("/watches/import").text
    bad = admin.post("/watches/import", {"yaml": "watches:\n  - {name: A, sites: [nope]}"})
    assert bad.status_code == 422 and "unknown site" in bad.text and "sites: [nope]" in bad.text

    text = "watches:\n  - {name: LG OLED55C5, sites: [ldlc], max_price: 1100}\n  - {name: RTX 5070, sites: [ldlc]}"
    assert admin.post("/watches/import", {"yaml": text}).headers["location"] == "/"
    assert "Imported 2 watches." in admin.get("/").text
    again = admin.post("/watches/import", {"yaml": text}, follow=True)
    assert "Imported 0 watches. Skipped 2 you already have" in again.text

    exported = admin.get("/watches/export.yaml")
    assert exported.headers["content-type"].startswith("application/yaml")
    assert "attachment" in exported.headers["content-disposition"]
    assert "name: LG OLED55C5" in exported.text and "version: 2" in exported.text


def test_dashboard_points_out_broken_shops_and_missing_channels(admin):
    admin.post("/watches", WATCH)
    admin.shop["results"] = ScrapeError("HTTP 503")
    admin.run_checks()  # on request: shown on the watch, but not counted against the shop
    with admin.db() as conn:
        assert runs_repo.health_all(conn) == []
    admin.svc.checker.run()
    admin.svc.checker.run()
    page = admin.get("/")
    assert "LDLC" in page.text and "failed several checks in a row" in page.text
    assert "Add a notification channel" in page.text and "1 shop failed" in page.text
    assert "some shops failed" in page.text
    detail = admin.get("/watches/1")
    assert "could not read 1 source" in detail.text and "HTTP 503" in detail.text
    assert "Some shops failed" in admin.get("/status").text


# ------------------------------------------------------------------ channels

def test_channel_lifecycle(admin):
    listing = admin.get("/channels")
    assert "No channel yet" in listing.text and "/channels/new?type=telegram" in listing.text
    assert admin.get("/channels/new").headers["location"] == "/channels#add"
    assert admin.get("/channels/new?type=fax").headers["location"] == "/channels#add"
    form = admin.get("/channels/new?type=telegram")
    assert "BotFather" in form.text and 'name="cfg_bot_token" type="password"' in form.text

    bad = admin.post("/channels?type=ntfy", {"name": "Phone", "event_deal": "on"})
    assert bad.status_code == 422 and "Required." in bad.text
    assert admin.post("/channels?type=fax", NTFY).status_code == 404
    assert admin.post("/channels?type=ntfy", {**NTFY, "cfg_token": "tk_secret"}).headers["location"] == "/channels"

    with admin.db() as conn:
        raw = conn.execute("SELECT config FROM channels").fetchone()[0]
        channel = channels_repo.list_all(conn, admin.svc.box)[0]
    assert "tk_secret" not in raw and channel.config["token"] == "tk_secret"

    edit = admin.get("/channels/1/edit")
    assert "tk_secret" not in edit.text and "Saved. Leave empty to keep it." in edit.text
    assert 'value="deals"' in edit.text
    admin.post("/channels/1", {**NTFY, "name": "Phone 2", "cfg_topic": "other"})
    with admin.db() as conn:
        channel = channels_repo.get(conn, admin.svc.box, 1)
    assert (channel.name, channel.config["topic"], channel.config["token"]) == ("Phone 2", "other", "tk_secret")
    assert admin.post("/channels/1", {"name": "x"}).status_code == 422

    sent = admin.post("/channels/1/test", follow=True)
    assert "Test sent through “Phone 2”" in sent.text and "Delivered" in sent.text
    assert admin.sent[-1][2].title == "Price Watch test"
    admin.send_error = NotifyError("HTTP 403: forbidden")
    failed = admin.post("/channels/1/test", follow=True)
    assert "could not deliver the test: HTTP 403: forbidden" in failed.text and "Failed" in failed.text
    admin.send_error = None

    assert "Turn on" in admin.post("/channels/1/toggle", follow=True).text
    assert "Turn off" in admin.post("/channels/1/toggle", follow=True).text
    assert "deleted" in admin.post("/channels/1/delete", follow=True).text
    for path in ("/channels/1", "/channels/1/test", "/channels/1/toggle", "/channels/1/delete"):
        assert admin.post(path, NTFY).status_code == 404
    assert admin.get("/channels/1/edit").status_code == 404


def test_a_member_cannot_redirect_a_channel_to_collect_its_secret(admin):
    admin.post("/channels?type=ntfy", {**NTFY, "cfg_server_url": "https://ntfy.example.org", "cfg_token": "tk_admin"})
    admin.post("/users", {"email": "friend@example.org", "password": "friend long password"})
    admin.post("/logout")
    admin.login("friend@example.org", "friend long password")

    # Someone else's channel does not exist for them: nothing to read, edit, test, switch or delete.
    assert "No channel yet" in admin.get("/channels").text
    assert admin.get("/channels/1/edit").status_code == 404
    hijack = admin.post("/channels/1", {**NTFY, "cfg_server_url": "https://attacker.example", "cfg_token": ""})
    assert hijack.status_code == 404
    for action in ("test", "toggle", "delete"):
        assert admin.post(f"/channels/1/{action}").status_code == 404
    assert admin.sent == []
    with admin.db() as conn:
        untouched = channels_repo.get(conn, admin.svc.box, 1)
    assert untouched.config["server_url"] == "https://ntfy.example.org" and untouched.enabled

    # And on their own channel, moving it elsewhere still means typing its secret again.
    admin.post("/channels?type=ntfy", {**NTFY, "cfg_server_url": "https://ntfy.example.org", "cfg_token": "tk_friend"})
    moved = admin.post("/channels/2", {**NTFY, "cfg_server_url": "https://elsewhere.example", "cfg_token": ""})
    assert moved.status_code == 422 and "Enter it again: the destination of this channel changed." in moved.text


def test_slow_actions_are_limited_in_number(admin):
    admin.post("/channels?type=ntfy", NTFY)
    for slots, path, data in ((admin.svc.preview_slots, "/watches/preview", WATCH),
                              (admin.svc.test_slots, "/channels/1/test", {})):
        held = [slots.acquire(blocking=False), slots.acquire(blocking=False)]
        assert all(held)
        refused = admin.post(path, data, follow=True)
        assert "still" in refused.text and ("Try again" in refused.text)
        for _ in held:
            slots.release()
    assert admin.sent == []
    assert admin.post("/watches/preview", WATCH).status_code == 200  # slots are returned afterwards
    assert admin.post("/watches/preview", WATCH).status_code == 200
    assert admin.post("/watches/preview", WATCH).status_code == 200


def test_channel_refuses_private_addresses_by_default(admin):
    response = admin.post("/channels?type=ntfy", {**NTFY, "cfg_server_url": "http://10.0.0.8"})
    assert response.status_code == 422 and "private or reserved" in response.text


def test_watch_limited_to_selected_channels(admin):
    admin.post("/channels?type=ntfy", NTFY)
    admin.post("/channels?type=ntfy", {**NTFY, "name": "Other"})
    form = admin.get("/watches/new?preset=generic").text
    assert 'name="channel_1"' in form and 'name="channel_2"' in form
    admin.shop["results"] = [offer(900)]
    admin.post("/watches", {**WATCH, "channels_mode": "selected", "channel_2": "on"})
    admin.run_checks()
    with admin.db() as conn:
        [alert] = alerts_repo.recent(conn)
        assert alerts_repo.delivery_summary(conn, [alert.id]) == {alert.id: [("Other", "sent", None)]}
    assert "Sent to Other" in admin.get("/watches/1").text


def test_alerts_page_paginates_and_explains_missing_channels(admin):
    assert "No alert yet" in admin.get("/alerts").text
    admin.shop["results"] = [offer(900 - i, f"https://www.ldlc.com/fiche/{i}.html") for i in range(45)]
    admin.post("/watches", WATCH)
    admin.run_checks()
    first = admin.get("/alerts")
    assert "45 alerts" in first.text and "Page 1 of 2" in first.text and "Not sent to you: you had no channel for it." in first.text
    assert "Page 2 of 2" in admin.get("/alerts?page=2").text
    assert admin.get("/alerts?page=0").status_code == 200


# ------------------------------------------------------------------ settings, accounts

SETTINGS = {
    "deal_drop_pct": "6", "check_interval_minutes": "60", "baseline_days": "10", "baseline_min_days": "2",
    "renotify_drop_pct": "1", "dealabs_max_age_days": "2", "health_alert_after": "4", "history_days": "30",
    "in_stock_only": "on",
}


def test_settings(admin):
    page = admin.get("/settings")
    assert 'name="check_interval_minutes" type="text" value="30"' in page.text
    bad = admin.post("/settings", {**SETTINGS, "check_interval_minutes": "1"})
    assert bad.status_code == 422 and "Must be between 10 and 1440." in bad.text
    assert "Settings saved" in admin.post("/settings", SETTINGS, follow=True).text
    assert 'name="check_interval_minutes" type="text" value="60"' in admin.get("/settings").text


def test_accounts_and_roles(admin):
    assert "Administrator" in admin.get("/users").text
    bad = admin.post("/users", {"email": "nope", "password": "short"})
    assert bad.status_code == 422 and "Enter a valid email address." in bad.text
    assert admin.post("/users", {"email": "Bob@Example.org", "password": "bob's long password"}).status_code == 303
    assert "already exists" in admin.post("/users", {"email": "bob@example.org", "password": "another long one"}).text

    assert "cannot delete the account you are signed in with" in admin.post("/users/1/delete", follow=True).text
    assert "at least 10" in admin.post("/users/2/password", {"password": "short"}, follow=True).text
    assert "Password changed for bob@example.org" in admin.post(
        "/users/2/password", {"password": "bob's new password"}, follow=True).text
    assert admin.post("/users/99/password", {"password": "long enough password"}).status_code == 404
    assert admin.post("/users/99/delete").status_code == 404

    admin.post("/logout")
    assert admin.login("bob@example.org", "bob's new password").status_code == 303
    assert admin.get("/").status_code == 200 and "Settings" not in admin.get("/").text
    for path in ("/settings", "/users"):
        assert admin.get(path).status_code == 403
    assert admin.post("/settings", SETTINGS).status_code == 403
    assert admin.post("/users", {"email": "eve@example.org", "password": "long enough password"}).status_code == 403
    assert admin.post("/watches", WATCH).status_code == 303  # members manage watches

    admin.post("/logout")
    admin.login()
    assert "deleted" in admin.post("/users/2/delete", follow=True).text
    with admin.db() as conn:
        assert [u.email for u in users_repo.list_all(conn)] == [ADMIN[0]]


def test_an_administrator_can_remove_another_but_not_themselves(admin):
    admin.post("/users", {"email": "second@example.org", "password": "second long password", "is_admin": "on"})
    admin.post("/logout")
    admin.login("second@example.org", "second long password")
    assert "deleted" in admin.post("/users/1/delete", follow=True).text
    assert "cannot delete the account you are signed in with" in admin.post("/users/2/delete", follow=True).text
    with admin.db() as conn:
        assert [u.email for u in users_repo.list_all(conn)] == ["second@example.org"]


def test_admin_resetting_own_password_signs_out(admin):
    assert admin.post("/users/1/password", {"password": "a brand new password"}).headers["location"] == "/login"
    assert admin.get("/account").status_code == 303
    assert admin.login(password="a brand new password").status_code == 303


def test_change_own_password(admin):
    assert "Change password" in admin.get("/account").text
    form = {"current_password": ADMIN[1], "new_password": "my next password", "confirm_password": "my next password"}
    wrong = admin.post("/account/password", {**form, "current_password": "not it at all"})
    assert wrong.status_code == 422 and "This is not your current password." in wrong.text
    mismatch = admin.post("/account/password", {**form, "confirm_password": "something else"})
    assert mismatch.status_code == 422 and "do not match" in mismatch.text
    assert "Password changed" in admin.post("/account/password", form, follow=True).text
    assert admin.get("/").status_code == 200  # this browser stays signed in
    admin.post("/logout")
    assert admin.login().status_code == 401
    assert admin.login(password="my next password").status_code == 303


def test_check_now_works_when_automatic_checks_are_off(config, clock, public_dns):
    """SCHEDULER_ENABLED=false must not leave "Check now" queued forever."""
    import time

    calls = []
    scrapers = {"ldlc": SiteScraper(key="ldlc", label="LDLC", kind=KIND_RETAILER, domains=("ldlc.com",),
                                    description="", search=lambda ctx, query, url=None: calls.append(query) or [])}
    services = build_services(config, scrapers=scrapers, product_scraper=lambda *a: None,
                              send=lambda *a, **k: None, pause=lambda: None)
    services.scheduler._tick = 0.01
    with TestClient(create_app(config, services=services)) as client:
        browser = Site(client, services, {}, [])
        browser.login()
        browser.post("/watches", WATCH)
        for _ in range(300):
            if calls and not services.scheduler.is_busy:
                break
            time.sleep(0.01)
        assert calls == ["RTX 5070"] and not services.scheduler.is_busy
        assert services.scheduler.next_due is None  # nothing is scheduled
        assert "Automatic checks are turned off" in browser.get("/status").text


def test_preview_ignores_a_strange_watch_id(admin):
    assert admin.post("/watches/preview", {**WATCH, "watch_id": "²"}).status_code == 200


def test_long_flash_messages_are_trimmed(admin):
    names = "\n".join(f"  - {{name: {'w%03d' % i + 'x' * 60}, sites: [ldlc]}}" for i in range(30))
    admin.post("/watches/import", {"yaml": "watches:\n" + names})
    again = admin.post("/watches/import", {"yaml": "watches:\n" + names})
    assert len(again.headers["set-cookie"]) < 1500


def test_deleting_a_channel_takes_it_off_watches(admin):
    admin.post("/channels?type=ntfy", NTFY)
    admin.post("/channels?type=ntfy", {**NTFY, "name": "Other"})
    admin.post("/watches", {**WATCH, "channels_mode": "selected", "channel_1": "on", "channel_2": "on"})
    admin.post("/channels/2/delete")
    admin.post("/channels?type=ntfy", {**NTFY, "name": "Newcomer"})
    with admin.db() as conn:
        assert watches_repo.get(conn, 1).channel_ids == (1,)
        assert [c.id for c in channels_repo.list_all(conn, admin.svc.box)] == [3, 1]  # id 2 is not reused


def test_status_page_shows_the_vpn_state(admin):
    from pricewatch.outbound import OutboundStatus

    assert "Outbound connection" not in admin.get("/status").text  # no check URL configured

    class Monitor:
        enabled = True
        status = None

        def current(self):
            return self.status

    monitor = Monitor()
    admin.svc.outbound = monitor
    stamp = "2026-11-25T08:58:00Z"

    monitor.status = OutboundStatus(stamp, ok=True, ip="193.32.126.172", location="Paris, France",
                                    organization="31173 Services AB", vpn=True, vpn_server="fr-par-wg-004")
    page = admin.get("/status").text
    assert "Through the VPN" in page and "Server fr-par-wg-004" in page and "193.32.126.172" in page
    assert "Paris, France" in page and "Checked 2 min ago" in page

    monitor.status = OutboundStatus(stamp, ok=True, ip="54.37.231.202", location="Roubaix, FR", vpn=False)
    page = admin.get("/status").text
    assert "Not through the VPN" in page and "leaving from the server's own address" in page

    monitor.status = OutboundStatus(stamp, ok=True, ip="54.37.231.202", vpn=None)
    page = admin.get("/status").text
    assert "Not reported" in page and "Unknown" in page

    monitor.status = OutboundStatus(stamp, ok=False, error="cannot connect")
    page = admin.get("/status").text
    assert "No connection" in page and "cannot reach the internet: cannot connect" in page
    assert "the tunnel is down" in page


def test_unknown_pages(admin):
    missing = admin.get("/definitely-not-here")
    assert missing.status_code == 404 and "Page not found" in missing.text
    assert admin.get("/watches/abc").status_code == 404
    assert admin.get("/alerts?page=x").status_code == 404


# ------------------------------------------------------------------ whose watch is it, and who may see it

BOB = ("bob@example.org", "bob's long password")
CAT = ("cat@example.org", "cat's long password")


def add_account(site, email, password, **extra):
    assert site.post("/users", {"email": email, "password": password, **extra}).status_code == 303


def switch(site, email, password):
    site.post("/logout")
    assert site.login(email, password).status_code == 303


@pytest.fixture
def people(admin):
    """The administrator signed in, with two more accounts (ids 2 and 3) to share with."""
    add_account(admin, *BOB)
    add_account(admin, *CAT)
    return admin


OWNER_ACTIONS = ("", "/toggle", "/check", "/delete", "/share-link")


def test_a_new_watch_is_private_and_invisible_to_everyone_else(people):
    people.shop["results"] = [offer(900)]
    people.post("/watches", WATCH)
    people.run_checks()
    assert "Private" in people.get("/watches/1").text and "Only you. Nobody else knows" in people.get("/watches/1").text

    switch(people, *BOB)
    home = people.get("/").text
    assert "RTX 5070" not in home and "Nothing is being watched yet" in home
    assert people.get("/watches/1").status_code == 404 and people.get("/watches/1/edit").status_code == 404
    for action in (*OWNER_ACTIONS, "/follow", "/owner"):
        assert people.post("/watches/1" + action, {**WATCH, "owner": "2"}).status_code == 404, action
    assert "RTX 5070" not in people.get("/alerts").text and "No alert yet" in people.get("/alerts").text
    assert "RTX 5070" not in people.get("/status").text
    assert "RTX 5070" not in people.get("/watches/export.yaml").text
    with people.db() as conn:
        watch = watches_repo.get(conn, 1)
    assert (watch.owner_id, watch.visibility, watch.enabled, watch.name) == (1, "private", True, "RTX 5070")


def test_two_accounts_can_use_the_same_name(people):
    people.post("/watches", WATCH)
    switch(people, *BOB)
    assert people.post("/watches", WATCH).headers["location"] == "/watches/2"
    assert people.post("/watches", WATCH).status_code == 422  # but not twice for one account
    assert people.get("/").text.count('class="row-link"') == 1


def test_a_shared_watch_is_read_only_for_the_people_it_is_shared_with(people):
    people.shop["results"] = [offer(900)]
    form = people.get("/watches/new?preset=generic").text
    assert 'name="share_user_2"' in form and "bob@example.org" in form and 'name="share_user_1"' not in form
    people.post("/watches", {**WATCH, "visibility": "shared", "share_user_2": "on", "share_user_99": "on"})
    people.run_checks()
    owner_view = people.get("/watches/1").text
    assert "Shared" in owner_view and "You and bob@example.org." in owner_view

    switch(people, *BOB)
    home = people.get("/").text
    assert "From other people" in home and "From admin@example.org" in home and "RTX 5070" in home
    assert "You have no watch of your own yet" in home
    page = people.get("/watches/1")
    assert page.status_code == 200 and "Shared with you by admin@example.org" in page.text
    assert "MSI RTX 5070 12G" in page.text and "Receive its alerts" in page.text
    assert "/watches/1/edit" not in page.text and "/watches/1/delete" not in page.text and "/watches/1/owner" not in page.text
    assert people.get("/watches/1/edit").status_code == 403
    for action in OWNER_ACTIONS:
        refused = people.post("/watches/1" + action, WATCH)
        assert refused.status_code == 403 and "Only the owner" in refused.text, action
    assert people.post("/watches/1/owner", {"owner": "2"}).status_code == 403
    assert "RTX 5070" in people.get("/alerts").text  # they see what it found

    switch(people, *CAT)  # not on the list
    assert people.get("/watches/1").status_code == 404 and "RTX 5070" not in people.get("/").text
    with people.db() as conn:
        assert watches_repo.get(conn, 1).enabled and watches_repo.get(conn, 1).shared_with == (2,)


def test_shared_needs_someone_or_a_link(people):
    refused = people.post("/watches", {**WATCH, "visibility": "shared"})
    assert refused.status_code == 422 and "Pick at least one account to share with, or turn the link on" in refused.text
    assert people.post("/watches", {**WATCH, "visibility": "everyone"}).status_code == 422
    assert people.post("/watches", {**WATCH, "visibility": "shared", "share_link": "on"}).status_code == 303


def test_a_public_watch_is_seen_by_every_account(people):
    people.post("/watches", {**WATCH, "visibility": "public"})
    for account in (BOB, CAT):
        switch(people, *account)
        assert "A public watch by admin@example.org" in people.get("/watches/1").text
        assert "public" in people.get("/").text
    switch(people, *ADMIN)  # an account created afterwards sees it too
    add_account(people, "dan@example.org", "dan's long password")
    switch(people, "dan@example.org", "dan's long password")
    assert people.get("/watches/1").status_code == 200


def test_following_sends_the_alerts_to_my_own_channels(people):
    people.post("/channels?type=ntfy", {**NTFY, "name": "Owner phone"})
    people.post("/watches", {**WATCH, "visibility": "public"})
    switch(people, *BOB)
    assert "no channel yet" in people.post("/watches/1/follow", follow=True).text
    people.post("/channels?type=ntfy", {**NTFY, "name": "Bob phone", "cfg_topic": "bob"})
    assert people.post("/watches/1/follow", {"following": "off"}).status_code == 303
    followed = people.post("/watches/1/follow", follow=True)
    assert "You will receive the alerts" in followed.text and "Stop receiving its alerts" in followed.text
    assert "Following" in people.get("/").text

    people.shop["results"] = [offer(900)]
    people.svc.checker.run()
    assert sorted(config["topic"] for _, config, _ in people.sent) == ["bob", "deals"]
    alerts = people.get("/alerts").text
    assert "Bob phone: delivered" in alerts and "Owner phone" not in alerts  # where it went for others is theirs
    switch(people, *CAT)  # sees the watch, did not ask for its alerts
    assert "Bob phone" not in people.get("/alerts").text and "RTX 5070" in people.get("/alerts").text

    switch(people, *ADMIN)
    assert "Owner phone: delivered" in people.get("/alerts").text
    assert people.post("/watches/1/follow").status_code == 403  # owners use their own channel settings


def test_followers_stop_receiving_when_they_lose_access(people):
    people.post("/watches", {**WATCH, "visibility": "public"})
    switch(people, *BOB)
    people.post("/channels?type=ntfy", {**NTFY, "cfg_topic": "bob"})
    people.post("/watches/1/follow")
    switch(people, *ADMIN)
    people.post("/watches/1", {**WATCH, "visibility": "private"})
    with people.db() as conn:
        assert watches_repo.followers(conn, 1) == ()
    people.shop["results"] = [offer(900)]
    people.run_checks()
    assert people.sent == []
    with people.db() as conn:
        assert len(alerts_repo.recent(conn)) == 1  # the deal was found, just not sent to the former follower


def test_share_link_opens_a_read_only_page_without_an_account(people):
    people.shop["results"] = [offer(900)]
    people.post("/watches", {**WATCH, "visibility": "shared", "share_link": "on"})
    people.run_checks()
    owner_page = people.get("/watches/1").text
    link = re.search(r'value="http://testserver(/s/[A-Za-z0-9_-]{40,})"', owner_page).group(1)
    assert "Link on" in owner_page and "anyone who has the link" in owner_page

    people.post("/logout")
    people.client.cookies.clear()
    page = people.get(link)
    assert page.status_code == 200 and "set-cookie" not in page.headers
    assert "RTX 5070" in page.text and "MSI RTX 5070 12G" in page.text and "read-only" in page.text
    for private in ("admin@example.org", "/watches/1", "csrf_token", "Sign out", "Alerts for this watch", "/edit"):
        assert private not in page.text, private
    assert page.headers["cache-control"] == "no-store" and "noindex" in page.text
    for wrong in ("/s/" + "x" * 43, "/s/x", "/s/" + "y" * 500):
        missing = people.get(wrong)
        assert missing.status_code == 404 and "This link does not work" in missing.text and "set-cookie" not in missing.headers
    assert people.get("/watches/1").status_code == 303  # the link opens that page only

    people.login()
    assert people.post("/watches/1/share-link").status_code == 303
    new_link = re.search(r'value="http://testserver(/s/[A-Za-z0-9_-]{40,})"', people.get("/watches/1").text).group(1)
    assert new_link != link
    people.post("/logout")
    assert people.get(link).status_code == 404 and people.get(new_link).status_code == 200

    people.login()
    people.post("/watches/1", {**WATCH, "visibility": "private", "share_link": "on"})  # no link on a private watch
    assert people.post("/watches/1/share-link").status_code == 404
    people.post("/logout")
    assert people.get(new_link).status_code == 404
    with people.db() as conn:
        assert watches_repo.get(conn, 1).share_token is None


def test_share_link_survives_other_edits_and_can_be_turned_off(people):
    people.post("/watches", {**WATCH, "visibility": "public", "share_link": "on"})
    with people.db() as conn:
        token = watches_repo.get(conn, 1).share_token
    people.post("/watches/1", {**WATCH, "name": "Renamed", "visibility": "public", "share_link": "on"})
    with people.db() as conn:
        assert watches_repo.get(conn, 1).share_token == token  # same address after an unrelated change
    people.post("/watches/1", {**WATCH, "name": "Renamed", "visibility": "public"})
    page = people.get("/watches/1").text
    assert "No link for people without an account" in page and "/s/" not in page
    people.post("/logout")
    assert people.get("/s/" + token).status_code == 404


def test_giving_a_watch_to_someone_else(people):
    people.post("/channels?type=ntfy", NTFY)
    people.post("/watches", {**WATCH, "visibility": "shared", "share_user_2": "on", "channels_mode": "selected", "channel_1": "on"})
    switch(people, *BOB)
    people.post("/watches/1/follow")
    switch(people, *ADMIN)
    assert "Pick the account" in people.post("/watches/1/owner", {"owner": ""}, follow=True).text
    assert "Pick the account" in people.post("/watches/1/owner", {"owner": "999"}, follow=True).text
    moved = people.post("/watches/1/owner", {"owner": "2"})
    assert moved.headers["location"] == "/"  # no longer theirs to see
    with people.db() as conn:
        watch = watches_repo.get(conn, 1)
        assert (watch.owner_id, watch.channel_ids, watch.shared_with) == (2, None, ())
        assert watches_repo.followers(conn, 1) == ()  # an owner is not a follower
    assert people.get("/watches/1").status_code == 404
    switch(people, *BOB)
    assert "Edit" in people.get("/watches/1").text and people.get("/watches/1/edit").status_code == 200


def test_an_administrator_can_rehome_a_public_watch_but_not_a_private_one(people):
    switch(people, *BOB)
    people.post("/watches", {**WATCH, "visibility": "public"})
    people.post("/watches", {**WATCH, "name": "Secret"})
    switch(people, *CAT)
    assert people.post("/watches/1/owner", {"owner": "3"}).status_code == 403  # a member is not an administrator
    switch(people, *ADMIN)
    page = people.get("/watches/1").text
    assert "As an administrator you can move a public watch" in page and "/watches/1/edit" not in page
    assert people.post("/watches/2/owner", {"owner": "1"}).status_code == 404  # private: not even visible
    assert people.post("/watches/1/owner", {"owner": "3"}).status_code == 303
    with people.db() as conn:
        assert watches_repo.get(conn, 1).owner_id == 3 and watches_repo.get(conn, 2).owner_id == 2


def test_a_member_cannot_hand_a_watch_to_someone_else(people):
    """Otherwise its alerts, with whatever title and link, would land on that person's channels."""
    switch(people, *BOB)
    people.post("/watches", WATCH)
    page = people.get("/watches/1").text
    assert "/watches/1/owner" not in page and "Change owner" not in page
    refused = people.post("/watches/1/owner", {"owner": "3"})
    assert refused.status_code == 403
    with people.db() as conn:
        assert watches_repo.get(conn, 1).owner_id == 2


def test_moving_a_watch_never_tells_what_the_new_owner_privately_has(people):
    switch(people, *BOB)
    people.post("/watches", {**WATCH, "name": "Secret plan"})  # private
    switch(people, *ADMIN)
    people.post("/watches", {**WATCH, "name": "Secret plan", "visibility": "public"})
    moved = people.post("/watches/2/owner", {"owner": "2"}, follow=True)
    assert "now belongs to bob@example.org" in moved.text and "already has" not in moved.text
    with people.db() as conn:
        assert sorted(w.name for w in watches_repo.list_all(conn) if w.owner_id == 2) == ["Secret plan", "Secret plan (2)"]


def test_check_now_is_limited_to_my_own_watches_unless_i_run_the_server(people):
    people.post("/watches", {**WATCH, "name": "Admin watch", "match": "RTX"})
    switch(people, *BOB)
    assert "no active watch to check" in people.post("/check", follow=True).text
    people.post("/watches", {**WATCH, "name": "Bob watch", "match": "RTX"})
    people.post("/watches", {**WATCH, "name": "Bob paused", "match": "RTX", "enabled": ""})
    people.run_checks()
    people.shop["results"] = [offer(900)]
    people.post("/check")
    people.run_checks()
    with people.db() as conn:
        checked = {w.name: w.last_checked_at for w in watches_repo.list_all(conn)}
        assert listings_repo.for_watch(conn, 2) and not listings_repo.for_watch(conn, 1)
        assert not listings_repo.for_watch(conn, 3)  # paused stays paused
    assert checked["Bob watch"] is not None
    switch(people, *ADMIN)
    people.post("/check")
    people.run_checks()
    with people.db() as conn:
        assert listings_repo.for_watch(conn, 1) and not listings_repo.for_watch(conn, 3)


def test_share_link_page_does_not_show_how_the_owner_set_the_alert(people):
    people.shop["results"] = [offer(900)]
    people.post("/watches", {**WATCH, "visibility": "public", "share_link": "on", "max_price": "777"})
    people.run_checks()
    with people.db() as conn:
        token = watches_repo.get(conn, 1).share_token
    owner_page = people.get("/watches/1").text
    assert "Alert at or under" in owner_page and "777" in owner_page
    switch(people, *BOB)  # an account that can see the watch sees when it alerts: it may follow it
    assert "Alert at or under" in people.get("/watches/1").text
    people.post("/logout")
    public = people.get("/s/" + token).text
    assert "RTX 5070" in public and "900" in public
    assert "Alert at" not in public and "777" not in public and "target price" not in public


def test_a_deleted_account_cannot_come_back_through_a_reused_id(people):
    """SQLite hands the id of the last account deleted to the next one created."""
    add_account(people, "old@example.org", "old long password")
    switch(people, "old@example.org", "old long password")
    stale = dict(people.client.cookies)
    assert people.get("/account").status_code == 200
    switch(people, *ADMIN)
    people.post("/users/4/delete")
    add_account(people, "new@example.org", "new long password", is_admin="on")
    with people.db() as conn:
        assert users_repo.get_by_email(conn, "new@example.org").id == 4  # the id was reused
    people.client.cookies.clear()
    for name, value in stale.items():
        people.client.cookies.set(name, value)
    assert people.get("/account").status_code == 303 and people.get("/users").status_code == 303


def test_absurd_ids_are_not_found_rather_than_a_crash(admin):
    huge = "9" * 30
    assert admin.get(f"/watches/{huge}").status_code == 404
    assert admin.get(f"/channels/{huge}/edit").status_code == 404
    assert admin.post(f"/watches/{huge}/owner", {"owner": huge}).status_code == 404
    admin.post("/watches", {**WATCH, "visibility": "public"})
    assert "Pick the account" in admin.post("/watches/1/owner", {"owner": huge}, follow=True).text
    assert admin.post("/watches/preview", {**WATCH, "watch_id": huge}).status_code == 200


def test_testing_with_someone_elses_watch_id_reads_nothing_of_theirs(people):
    people.post("/watches", {**WATCH, "visibility": "public"})
    with people.db() as conn:
        for back in range(1, 6):
            listings_repo.record_daily_low(conn, 1, f"2026-11-{25 - back:02d}", 1000.0)
    switch(people, *BOB)
    people.shop["results"] = [offer(900)]
    response = people.post("/watches/preview", {**WATCH, "max_price": "", "watch_id": "1"})
    assert response.status_code == 200 and "No alert threshold yet" in response.text  # no baseline borrowed
    assert 'action="/watches"' in response.text and 'action="/watches/1"' not in response.text


def test_alerts_and_status_only_show_what_the_viewer_may_see(people):
    people.post("/watches", {**WATCH, "name": "Admin private", "product_urls": "https://hidden-shop.example/p"})
    switch(people, *BOB)
    people.post("/watches", {**WATCH, "name": "Bob private"})
    people.shop["results"] = ScrapeError("HTTP 503")
    with people.db() as conn:
        from pricewatch.repos import settings as settings_repo

        settings_repo.save(conn, dataclasses.replace(settings_repo.load(conn), health_alert_after=1))
    original = people.svc.checker._product_scraper
    people.svc.checker._product_scraper = lambda ctx, url, selector=None: (_ for _ in ()).throw(ScrapeError("HTTP 403"))
    people.svc.checker.run()
    people.svc.checker._product_scraper = original

    status = people.get("/status").text
    assert "Bob private" in status and "Admin private" not in status and "hidden-shop.example" not in status
    assert "LDLC" in status
    alerts = people.get("/alerts").text
    assert "LDLC has failed 1 checks in a row" in alerts and "hidden-shop.example" not in alerts
    assert "hidden-shop.example" not in people.get("/").text

    switch(people, *ADMIN)
    status = people.get("/status").text
    assert "Admin private" in status and "Bob private" not in status and "hidden-shop.example product pages" in status
    assert "hidden-shop.example product pages has failed" in people.get("/alerts").text


def test_scraper_alerts_about_private_hosts_only_go_to_administrators(people):
    people.post("/channels?type=ntfy", {**NTFY, "cfg_topic": "admin"})
    people.post("/watches", {**WATCH, "name": "Page", "site_ldlc": "", "product_urls": "https://hidden-shop.example/p"})
    switch(people, *BOB)
    people.post("/channels?type=ntfy", {**NTFY, "cfg_topic": "bob"})
    people.post("/watches", WATCH)
    with people.db() as conn:
        from pricewatch.repos import settings as settings_repo

        settings_repo.save(conn, dataclasses.replace(settings_repo.load(conn), health_alert_after=1))
    people.shop["results"] = ScrapeError("HTTP 503")
    people.svc.checker._product_scraper = lambda ctx, url, selector=None: (_ for _ in ()).throw(ScrapeError("HTTP 403"))
    people.svc.checker.run()
    by_topic = {config["topic"]: [item.title for item in message.items] for _, config, message in people.sent}
    assert by_topic["bob"] == ["LDLC has failed 1 checks in a row"]
    assert sorted(by_topic["admin"]) == ["LDLC has failed 1 checks in a row",
                                         "hidden-shop.example product pages has failed 1 checks in a row"]
    assert all(item.site == "" for _, _, message in people.sent for item in message.items)


def test_deleting_an_account_deletes_its_watches_and_channels(people):
    switch(people, *BOB)
    people.post("/channels?type=ntfy", NTFY)
    people.post("/watches", {**WATCH, "visibility": "public"})
    switch(people, *ADMIN)
    assert "with its watches, their history and its channels" in people.get("/users").text
    people.post("/users/2/delete")
    assert people.get("/watches/1").status_code == 404
    with people.db() as conn:
        assert watches_repo.list_all(conn) == [] and channels_repo.list_all(conn, people.svc.box) == []


def test_watch_form_only_offers_my_own_channels(people):
    people.post("/channels?type=ntfy", {**NTFY, "name": "Admin phone"})
    switch(people, *BOB)
    form = people.get("/watches/new?preset=generic").text
    assert "Admin phone" not in form and "You have no channel yet" in form
    people.post("/channels?type=ntfy", {**NTFY, "name": "Bob phone"})
    assert "Bob phone" in people.get("/watches/new?preset=generic").text
    people.post("/watches", {**WATCH, "channels_mode": "selected", "channel_1": "on", "channel_2": "on"})
    with people.db() as conn:
        assert watches_repo.get(conn, 1).channel_ids == (2,)  # the administrator's channel cannot be picked


def test_import_and_export_are_per_account(people):
    people.post("/watches/import", {"yaml": "version: 2\nwatches:\n  - {name: Mine, sites: [ldlc], visibility: public}\n  - {name: Quiet, sites: [ldlc], visibility: shared}"})
    with people.db() as conn:
        assert sorted((w.name, w.owner_id, w.visibility) for w in watches_repo.list_all(conn)) == [
            ("Mine", 1, "public"), ("Quiet", 1, "private")]
    exported = people.get("/watches/export.yaml").text
    assert "visibility: public" in exported and exported.count("visibility") == 1
    switch(people, *BOB)
    assert "Imported 1 watch." in people.post("/watches/import", {"yaml": "watches:\n  - {name: Mine, sites: [ldlc]}"}, follow=True).text
    assert people.get("/watches/export.yaml").text.count("name:") == 1
