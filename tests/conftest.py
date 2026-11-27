import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pricewatch.config import Config
from pricewatch.db import Database
from pricewatch.scrapers.base import KIND_RETAILER, Listing, SiteScraper
from pricewatch.security import SecretBox
from pricewatch.web.app import build_services, create_app

SECRET_KEY = "test-secret-key-that-is-long-enough-0123456789"


class FakeClock:
    """A clock tests can move by hand."""

    def __init__(self, start: datetime | None = None):
        self.now = start or datetime(2026, 11, 25, 9, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        from datetime import timedelta

        self.now = self.now + timedelta(**kwargs)


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        data_dir=tmp_path,
        secret_key=SECRET_KEY,
        base_url="http://testserver",
        cookie_secure=False,
        admin_email="admin@example.org",
        admin_password="correct horse battery",
        scheduler_enabled=False,
    )


@pytest.fixture
def db(config: Config) -> Database:
    database = Database(config.db_path)
    database.migrate()
    return database


@pytest.fixture
def box() -> SecretBox:
    return SecretBox(SECRET_KEY)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def public_dns(monkeypatch):
    """Every host name resolves to a public address, without touching the network."""
    import ipaddress
    import socket

    from pricewatch import netsafe

    real = netsafe._original_getaddrinfo

    def fake(host, *args, **kwargs):
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
        return real(host, *args, **kwargs)

    monkeypatch.setattr(netsafe, "_original_getaddrinfo", fake)


# ------------------------------------------------------------------ the web app, driven like a browser

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
