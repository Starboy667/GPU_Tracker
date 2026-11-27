"""The public home page, and how static files are served to it."""
import dataclasses
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pricewatch.config import ConfigError, load_config
from pricewatch.web.app import HERE, build_services, create_app
from tests.conftest import SECRET_KEY

STATIC = HERE / "static"
TEMPLATES = HERE / "templates"
ASSET = re.compile(r'(?:src|href)="(/static/[^"]+)"')
IMPORT = re.compile(r"""(?:import|export)\s[^'"]*?from\s+['"]([^'"]+)['"]|import\s+['"]([^'"]+)['"]""")


# ------------------------------------------------------------------ the page

def test_visitors_without_a_session_get_the_landing_page(site):
    page = site.get("/")
    assert page.status_code == 200
    assert 'href="/login"' in page.text and "Sign in" in page.text
    assert "Request access" in page.text
    assert "LDLC" in page.text  # the shops this server reads, not a hard-coded list
    assert "set-cookie" not in page.headers  # looking at the home page starts no session
    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert 'name="robots" content="noindex' in page.text  # a private server stays out of search engines
    from pricewatch import __version__

    assert __version__ not in page.text  # nothing that helps match the server to a known flaw


def test_the_landing_page_says_how_often_prices_are_checked(site):
    from pricewatch.repos import settings as settings_repo

    with site.db() as conn:
        settings_repo.save(conn, dataclasses.replace(settings_repo.load(conn), check_interval_minutes=45))
    assert "every 45 minutes" in site.get("/").text


def test_signed_in_people_get_their_watches_instead(admin):
    page = admin.get("/")
    assert page.status_code == 200
    assert "<h1>Watches</h1>" in page.text and "Request access" not in page.text


def test_without_a_contact_the_access_button_explains_whom_to_ask(site):
    page = site.get("/").text
    assert 'href="#access"' in page
    assert "Accounts are created by the person who runs this server" in page


def test_with_a_contact_the_access_button_leads_to_it(config, clock):
    contact = "mailto:host@example.org?subject=Price%20Watch"
    with_contact = dataclasses.replace(config, access_request_url=contact)
    services = build_services(with_contact, scrapers={}, product_scraper=lambda *a: None,
                              send=lambda *a, **k: None, clock=clock)
    services.scheduler.start = lambda **kwargs: None
    with TestClient(create_app(with_contact, services=services)) as client:
        page = client.get("/").text
    assert 'href="mailto:host@example.org?subject=Price%20Watch"' in page


@pytest.mark.parametrize("value", ["https://chat.example.org/invite", "mailto:host@example.org", ""])
def test_access_request_url_accepts_links_people_can_follow(value):
    assert load_config({"SECRET_KEY": SECRET_KEY, "ACCESS_REQUEST_URL": value}).access_request_url == value


@pytest.mark.parametrize("value", ["javascript:alert(1)", "http://plain.example.org", "host@example.org",
                                   "data:text/html,x", 'https://x.example/"onmouseover="x', "https://a b",
                                   "https://[not-an-address", "mailto:nobody", "https://"])
def test_access_request_url_refuses_anything_else(value):
    with pytest.raises(ConfigError, match="ACCESS_REQUEST_URL"):
        load_config({"SECRET_KEY": SECRET_KEY, "ACCESS_REQUEST_URL": value})


# ------------------------------------------------------------------ static files

def test_every_file_the_pages_load_exists(site, admin):
    seen = set()
    for client, path in ((admin, "/"), (admin, "/watches/new")):
        seen.update(ASSET.findall(client.get(path).text))
    admin.post("/logout")
    for path in ("/", "/login"):
        seen.update(ASSET.findall(site.get(path).text))
    assert any("landing" in url for url in seen) and any(url.endswith("app.css") for url in seen)
    for url in sorted(seen):
        assert site.get(url).status_code == 200, url


def test_versioned_static_addresses_can_be_cached_for_good(site):
    page = site.get("/login").text
    versioned = next(url for url in ASSET.findall(page) if url.endswith("app.css"))
    assert re.fullmatch(r"/static/v-[0-9a-f]{10}/app\.css", versioned)
    for headers in ({"accept-encoding": "gzip"}, {"accept-encoding": "identity"}):
        response = site.get(versioned, headers=headers)
        assert response.status_code == 200 and "immutable" in response.headers["cache-control"], headers
    # A page cached before an upgrade still finds its files, but under another release's name
    # the current file is not pinned for a year. Nor is the bare address.
    stale = site.get("/static/v-0000000000/app.css")
    assert stale.status_code == 200 and stale.text == site.get(versioned).text
    assert "immutable" not in stale.headers.get("cache-control", "")
    assert "immutable" not in site.get("/static/app.css").headers.get("cache-control", "")
    assert site.get("/static/v-0000000000/../../etc/passwd").status_code == 404
    assert site.get("/static/v-0000000000/nothing.css").status_code == 404


def test_large_static_files_are_compressed_once(site, monkeypatch):
    from pricewatch.web import static_files

    library = next(STATIC.glob("vendor/three-*.min.js"))
    address = f"/static/vendor/{library.name}"

    def again(*args, **kwargs):  # compressing 600 kB per request would hand visitors the processor
        raise AssertionError("compressed again")

    monkeypatch.setattr(static_files.gzip, "compress", again)
    response = site.get(address, headers={"accept-encoding": "br, gzip"})
    assert response.status_code == 200 and response.headers["content-encoding"] == "gzip"
    assert response.headers["content-type"].startswith("text/javascript")
    assert "accept-encoding" in response.headers["vary"].lower()
    assert response.content == library.read_bytes()  # the client library undid the compression
    assert int(response.headers["content-length"]) < library.stat().st_size / 3

    known = site.get(address, headers={"accept-encoding": "gzip", "if-none-match": response.headers["etag"]})
    assert known.status_code == 304 and known.content == b""
    head = site.client.head(address, headers={"accept-encoding": "gzip"})
    assert head.status_code == 200 and head.headers["content-encoding"] == "gzip" and head.content == b""


@pytest.mark.parametrize("headers", [
    {"accept-encoding": "identity"},
    {"accept-encoding": "gzip;q=0, identity"},
    {"accept-encoding": "gzip", "range": "bytes=0-99"},  # a slice of the file itself, not of its compressed form
])
def test_static_files_are_sent_as_they_are_when_gzip_does_not_fit(site, headers):
    library = next(STATIC.glob("vendor/three-*.min.js"))
    response = site.get(f"/static/vendor/{library.name}", headers=headers)
    assert response.status_code in (200, 206) and "content-encoding" not in response.headers
    assert library.read_bytes().startswith(response.content[:100])


@pytest.mark.parametrize("header, expected", [
    ("gzip", True), ("br, GZIP ; q=0.5", True), ("deflate, gzip;q=1.0", True),
    ("", False), ("identity", False), ("gzip;q=0", False), ("gzip;q=0.000", False), ("gzip;q=soon", False),
    ("x-gzip-ish", False),
])
def test_reading_accept_encoding(header, expected):
    from pricewatch.web.static_files import accepts_gzip

    assert accepts_gzip(header) is expected


def test_small_and_binary_static_files_are_not_compressed(site):
    assert "content-encoding" not in site.get("/static/favicon.svg", headers={"accept-encoding": "gzip"}).headers
    font = site.get("/static/fonts/archivo-latin.woff2", headers={"accept-encoding": "gzip"})
    assert font.status_code == 200 and "content-encoding" not in font.headers  # already compressed by design


def test_the_bundled_library_is_the_one_that_was_reviewed():
    """tools/vendor-three.sh records the fingerprint of what it built. A bundle that changed
    without going through the script (or the other way round) fails here."""
    import hashlib

    library = next(STATIC.glob("vendor/three-*.min.js"))
    recorded = (HERE.parents[1] / "tools" / "vendor-three.sha256").read_text().split()
    assert recorded == [hashlib.sha256(library.read_bytes()).hexdigest(), library.name]


def test_pages_with_secrets_are_never_compressed(admin):
    """Compressing a page that mixes a CSRF token with reflected input would leak the token."""
    response = admin.get("/account", headers={"accept-encoding": "gzip"})
    assert "content-encoding" not in response.headers


def test_scripts_only_import_files_that_ship(site):
    scripts = sorted(STATIC.rglob("*.js"))
    assert scripts
    for script in scripts:
        if "vendor" in script.parts:
            continue  # a single bundled file
        for match in IMPORT.finditer(script.read_text()):
            target = match.group(1) or match.group(2)
            assert target.startswith("."), f"{script.name}: {target} is not a relative import"
            assert (script.parent / target).resolve().is_file(), f"{script.name}: {target} is missing"


def test_the_bundled_library_has_everything_the_scene_uses():
    """The three.js file only carries what tools/vendor-three-entry.js lists."""
    library = next(STATIC.glob("vendor/three-*.min.js")).read_text()
    exported = set(re.findall(r"(?:\bas |[{,])([A-Za-z_][A-Za-z0-9_]*)(?=[,}])", library[library.rindex("export{"):]))
    used = set()
    for script in (STATIC / "landing").glob("*.js"):
        used.update(re.findall(r"\bTHREE\.([A-Za-z_][A-Za-z0-9_]*)", script.read_text()))
    assert len(used) > 20 and used <= exported, f"missing from the bundle: {sorted(used - exported)}"
    wrapper = (STATIC / "landing" / "three.js").read_text()
    assert f"../vendor/{next(STATIC.glob('vendor/three-*.min.js')).name}" in wrapper


def test_templates_keep_the_content_security_policy_intact():
    """script-src 'self' and style-src 'self': no inline script, style or event handler anywhere."""
    for template in sorted(TEMPLATES.glob("*.html")):
        text = template.read_text()
        assert not re.search(r"\sstyle\s*=", text), f"{template.name}: inline style"
        assert not re.search(r"<style[\s>]", text), f"{template.name}: <style> block"
        assert not re.search(r"\son[a-z]+\s*=\s*[\"']", text), f"{template.name}: inline event handler"
        for tag in re.findall(r"<script\b[^>]*>", text):
            assert " src=" in tag, f"{template.name}: inline script {tag}"


def test_stylesheets_only_reference_files_that_ship():
    for sheet in sorted(STATIC.rglob("*.css")):
        for target in re.findall(r"url\(\s*[\"']?([^\"')]+)[\"']?\s*\)", sheet.read_text()):
            if target.startswith("data:"):
                continue
            assert (sheet.parent / target).resolve().is_file(), f"{sheet.name}: {target} is missing"


def test_the_static_version_follows_files_in_subfolders(tmp_path: Path):
    from pricewatch.web.app import static_version

    (tmp_path / "landing").mkdir()
    (tmp_path / "app.css").write_text("a")
    (tmp_path / "landing" / "scene.js").write_text("b")
    before = static_version(tmp_path)
    (tmp_path / "landing" / "scene.js").write_text("c")
    assert re.fullmatch(r"[0-9a-f]{10}", before) and static_version(tmp_path) != before


# ------------------------------------------------------------------ the app around it

def test_the_sign_in_page_leads_back_to_the_home_page(site):
    page = site.get("/login").text
    assert 'href="/"' in page and "Every price drops." in page


def test_the_dashboard_puts_current_deals_on_tags(admin):
    from tests.test_web import WATCH, offer

    admin.shop["results"] = [offer(price=900.0)]
    assert admin.post("/watches", WATCH).status_code == 303  # alerts at or under 950
    admin.run_checks()
    page = admin.get("/").text
    deals = page[page.index('id="deals"'):page.index("</section>", page.index('id="deals"'))]
    assert "RTX 5070" in deals and 'href="/watches/1"' in deals and "https://www.ldlc.com/fiche/1.html" in deals
    assert "900,00" in deals and "target" in deals.lower()
    admin.post("/watches/1/toggle")  # paused: nothing to act on
    assert 'id="deals"' not in admin.get("/").text


def test_pages_without_a_deal_show_no_tag(admin):
    assert 'id="deals"' not in admin.get("/").text


# ------------------------------------------------------------------ one art direction on every screen

SIGNED_IN_PAGES = ("/", "/alerts", "/channels", "/status", "/settings", "/users", "/account", "/watches/new",
                   "/watches/new?preset=generic", "/watches/import", "/channels/new?type=ntfy", "/watches/1",
                   "/watches/1/edit")


def test_every_screen_opens_on_a_masthead_with_its_one_title(admin):
    from tests.test_web import WATCH, offer

    admin.shop["results"] = [offer(price=900.0)]
    assert admin.post("/watches", WATCH).status_code == 303
    admin.run_checks()
    for path in SIGNED_IN_PAGES:
        page = admin.get(path)
        assert page.status_code == 200, path
        assert page.text.count("<h1") == 1, path
        masthead = page.text[page.text.index('<header class="masthead"'):page.text.index("</header>", page.text.index('<header class="masthead"'))]
        assert "<h1" in masthead, path  # the title sits on the coloured band, not under it
        assert page.text.index('id="main"') < page.text.index('<header class="masthead"'), path  # inside the main landmark


def test_signed_in_screens_say_which_version_runs_and_public_ones_do_not(admin):
    from pricewatch import __version__

    assert f"Price Watch {__version__}" in admin.get("/status").text
    admin.post("/users", {"email": "member@example.org", "password": "a member's password"})
    admin.post("/logout")
    assert admin.login("member@example.org", "a member's password").status_code == 303
    assert __version__ not in admin.get("/status").text  # the version is the administrator's business
    admin.post("/logout")
    for path in ("/", "/login", "/s/not-a-real-link"):
        assert __version__ not in admin.get(path).text, path


def test_choosing_what_to_watch_is_picking_a_tag(admin):
    from pricewatch.presets import PRESETS

    page = admin.get("/watches/new").text
    for preset in PRESETS:
        assert f'href="/watches/new?preset={preset.key}"' in page and preset.label in page and preset.example in page
    assert page.count('class="tag tag-choice"') == len(PRESETS)


def test_the_alert_history_prints_prices_like_the_tags(admin):
    from tests.test_web import NTFY, WATCH, offer

    admin.post("/channels?type=ntfy", NTFY)
    admin.shop["results"] = [offer(price=900.0)]
    admin.post("/watches", WATCH)
    admin.run_checks()
    page = admin.get("/alerts").text
    assert 'class="tp"' in page and "900,00" in page and "Phone: delivered" in page and "Target" in page


def test_a_shared_link_page_carries_the_brand_and_a_way_in(admin):
    from tests.test_web import WATCH

    assert admin.post("/watches", {**WATCH, "visibility": "shared", "share_link": "on"}).status_code == 303
    with admin.db() as conn:
        from pricewatch.repos import watches as watches_repo

        token = watches_repo.get(conn, 1).share_token
    admin.post("/logout")
    admin.client.cookies.clear()
    page = admin.get(f"/s/{token}")
    assert page.status_code == 200 and "set-cookie" not in page.headers
    assert 'class="guestbar"' in page.text and 'href="/"' in page.text and 'href="/login"' in page.text
    assert "read-only" in page.text and '<header class="masthead"' in page.text


def test_error_pages_show_their_number_on_a_tag(admin):
    missing = admin.get("/watches/999")
    assert missing.status_code == 404 and ">404<" in missing.text and "Back to the watches" in missing.text
    admin.post("/logout")
    admin.client.cookies.clear()
    dead_link = admin.get("/s/not-a-real-link")
    assert dead_link.status_code == 404 and ">404<" in dead_link.text and "This link does not work" in dead_link.text
    assert "Back to the watches" not in dead_link.text and 'href="/"' in dead_link.text
