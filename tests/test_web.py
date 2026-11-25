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

ADMIN = ("admin@example.org", "correct horse battery")
TOKEN = re.compile(r'name="csrf_token" value="([^"]+)"')


class Site:
    """A signed-out browser pointed at the app, plus handles on the fakes behind it."""

    def __init__(self, client, services, shop, sent):
        self.client, self.svc, self.shop, self.sent = client, services, shop, sent
        self.send_error = None

    def get(self, path, **kw):
        return self.client.get(path, follow_redirects=False, **kw)

    def token(self):
        page = self.get("/account")
        if page.status_code == 303:
            page = self.get("/login")
        return TOKEN.search(page.text).group(1)

    def post(self, path, data=None, *, token=None, follow=False):
        payload = {"csrf_token": self.token() if token is None else token, **(data or {})}
        return self.client.post(path, data=payload, follow_redirects=follow)

    def login(self, email=ADMIN[0], password=ADMIN[1], **extra):
        return self.post("/login", {"email": email, "password": password, **extra})

    def run_checks(self):
        while self.svc.scheduler.run_pending():
            pass

    def db(self):
        return self.svc.db.connect()


@pytest.fixture
def site(config, clock, public_dns):
    shop = {"results": []}
    sent = []

    def search(ctx, query, url=None):
        if isinstance(shop["results"], Exception):
            raise shop["results"]
        return list(shop["results"])

    def product(ctx, url, selector=None):
        return Listing(site="shop.example", title="Product page", price=499.0, url=url, in_stock=True, source="product")

    holder = {}

    def send(type_key, channel_config, message, *, allow_private=False):
        if holder["site"].send_error:
            raise holder["site"].send_error
        sent.append((type_key, channel_config, message))

    scrapers = {"ldlc": SiteScraper(key="ldlc", label="LDLC", kind=KIND_RETAILER, domains=("ldlc.com",),
                                    description="", search=search)}
    services = build_services(config, scrapers=scrapers, product_scraper=product, send=send,
                              pause=lambda: None, clock=clock)
    services.scheduler.start = lambda **kwargs: None  # tests drain the queue themselves: run_checks()
    with TestClient(create_app(config, services=services)) as client:
        holder["site"] = Site(client, services, shop, sent)
        yield holder["site"]


@pytest.fixture
def admin(site):
    assert site.login().status_code == 303
    return site


def offer(price=900.0, url="https://www.ldlc.com/fiche/1.html", title="MSI RTX 5070 12G", in_stock=True):
    return Listing(site="LDLC", title=title, price=price, url=url, in_stock=in_stock, source="ldlc")


WATCH = {"name": "RTX 5070", "site_ldlc": "on", "enabled": "on", "max_price": "950"}
NTFY = {"name": "Phone", "cfg_topic": "deals", "event_deal": "on", "event_health": "on", "enabled": "on"}


# ------------------------------------------------------------------ authentication

def test_pages_require_a_session(site):
    for path in ("/", "/alerts", "/channels", "/status", "/settings", "/users", "/account", "/watches/new",
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
    assert site.get("/").status_code == 303


def test_login_ignores_offsite_next(site):
    for target in ("https://evil.example/", "//evil.example/", "/\\evil.example"):
        site.client.cookies.clear()
        assert site.login(next=target).headers["location"] == "/"


def test_login_is_rate_limited(site):
    for _ in range(5):
        assert site.login(password="wrong wrong wrong").status_code == 401
    blocked = site.login()  # even the right password has to wait now
    assert blocked.status_code == 429 and "Too many failed attempts" in blocked.text
    assert site.get("/").status_code == 303


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
    assert admin.get("/").status_code == 303


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
    assert detail.text.count('class="chip chip-good">Deal') >= 1 and "your target price" in detail.text
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
    assert again.status_code == 422 and "Another watch already has this name." in again.text


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
    assert "Imported 0 watches. Skipped 2 that already exist" in again.text

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

    hijack = admin.post("/channels/1", {**NTFY, "cfg_server_url": "https://attacker.example", "cfg_token": ""})
    assert hijack.status_code == 422 and "Enter it again: the destination of this channel changed." in hijack.text
    admin.post("/channels/1/test")
    assert all(config["server_url"] == "https://ntfy.example.org" for _, config, _ in admin.sent)
    with admin.db() as conn:
        assert channels_repo.get(conn, admin.svc.box, 1).config["server_url"] == "https://ntfy.example.org"
    assert "tk_admin" not in admin.get("/channels/1/edit").text


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
    assert "45 alerts" in first.text and "Page 1 of 2" in first.text and "Not sent anywhere" in first.text
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
    assert admin.get("/").status_code == 303
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
