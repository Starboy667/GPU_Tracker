from datetime import datetime, timezone
from pathlib import Path

import pytest

from pricewatch.config import Config
from pricewatch.db import Database
from pricewatch.security import SecretBox

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
