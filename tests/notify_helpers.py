"""Sample messages and small fakes shared by the notification tests."""
from __future__ import annotations

from pricewatch import netsafe
from pricewatch.notify.base import KIND_DEAL, KIND_HEALTH, KIND_RESTOCK, KIND_TEST, AlertItem, Message

PUBLIC_IP = "93.184.216.34"
APP_URL = "https://pw.example"


def deal(**overrides) -> AlertItem:
    values = {
        "kind": KIND_DEAL,
        "watch": "RTX 5070",
        "title": "Gigabyte GeForce RTX 5070 WINDFORCE OC SFF 12G",
        "site": "LDLC",
        "url": "https://www.ldlc.com/fiche/PB00660000.html",
        "price": 919.99,
        "previous_price": 950.0,
        "baseline": 1049.0,
        "baseline_days": 14,
        "target_price": 900.0,
        "in_stock": True,
    }
    return AlertItem(**{**values, **overrides})


def restock(**overrides) -> AlertItem:
    values = {
        "kind": KIND_RESTOCK,
        "watch": "LG OLED55C5",
        "title": "LG OLED55C54LA (55 pouces)",
        "site": "boulanger.com",
        "url": "https://www.boulanger.com/ref/1200000",
        "price": 1299.0,
        "in_stock": True,
    }
    return AlertItem(**{**values, **overrides})


def health(**overrides) -> AlertItem:
    values = {
        "kind": KIND_HEALTH,
        "watch": "",
        "title": "Idealo has been failing for 6 checks",
        "detail": "Last error: HTTP 403 (blocked?)",
    }
    return AlertItem(**{**values, **overrides})


def test_item(**overrides) -> AlertItem:
    values = {
        "kind": KIND_TEST,
        "watch": "",
        "title": "Price Watch is connected",
        "detail": "This is a test notification.",
    }
    return AlertItem(**{**values, **overrides})


test_item.__test__ = False  # not a pytest test, despite the name


def message(*items: AlertItem, title: str = "🔥 2 deals", app_url: str = APP_URL) -> Message:
    return Message(title=title, items=tuple(items) or (deal(), restock()), app_url=app_url)


def fake_dns(monkeypatch, ip: str = PUBLIC_IP) -> None:
    """Make every host name resolve to ``ip`` for ``netsafe.check_url`` (no network in tests)."""

    def resolve(host, *args, **kwargs):
        return [(2, 1, 6, "", (ip, 0))]

    monkeypatch.setattr(netsafe, "_original_getaddrinfo", resolve)
