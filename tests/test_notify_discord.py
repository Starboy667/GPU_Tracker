from __future__ import annotations

import json

import pytest
import requests
import responses

from pricewatch.notify import discord
from pricewatch.notify.base import FIELD_URL, ConfigError, Message, NotifyError, clean_config

try:
    from tests.notify_helpers import deal, fake_dns, health, message, test_item
except ImportError:  # tests/ is not a package
    from notify_helpers import deal, fake_dns, health, message, test_item

TOKEN = "s3cr3tT0kenValue-abcdefghijklmnopqrstuvwxyz"
URL = f"https://discord.com/api/webhooks/123456789012345678/{TOKEN}"
CONFIG = {"webhook_url": URL}


def _payloads() -> list[dict]:
    return [json.loads(call.request.body) for call in responses.calls]


@responses.activate
def test_sends_one_embed_per_item():
    responses.add(responses.POST, URL, status=204)
    discord.send(CONFIG, message())
    (payload,) = _payloads()
    assert payload["username"] == "Price Watch"
    assert payload["content"] == "🔥 2 deals"
    assert payload["allowed_mentions"] == {"parse": []}
    first, second = payload["embeds"]
    assert first["title"] == "RTX 5070 — LDLC"
    assert first["url"] == "https://www.ldlc.com/fiche/PB00660000.html"
    assert first["color"] == discord.COLOR_IN_STOCK
    assert first["fields"] == [{"name": "Stock", "value": "✅ In stock", "inline": True}]
    assert "**919,99 €** (-12 % vs usual 1 049,00 €, median over 14 days)" in first["description"]
    assert "Target: 900,00 €" in first["description"]
    assert "Last alert: ~~950,00 €~~" in first["description"]
    assert first["timestamp"].endswith("+00:00")
    assert second["title"] == "LG OLED55C5 — back in stock at boulanger.com"
    assert second["description"] == "LG OLED55C54LA (55 pouces)\n**1 299,00 €**"


def test_embed_colors_and_fields_by_kind():
    assert discord.make_embed(deal(in_stock=None), "t")["color"] == discord.COLOR_OTHER
    assert discord.make_embed(deal(in_stock=False), "t")["fields"][0]["value"] == "❌ Out of stock"
    sick = discord.make_embed(health(), "t")
    assert sick["color"] == discord.COLOR_HEALTH
    assert "fields" not in sick and "url" not in sick
    assert sick["description"] == "Last error: HTTP 403 (blocked?)"
    assert discord.make_embed(test_item(), "t")["color"] == discord.COLOR_INFO


def test_embed_escapes_markdown_and_drops_unsafe_links():
    embed = discord.make_embed(deal(title="**big** [x](http://evil)", url="javascript:alert(1)"), "t")
    assert "url" not in embed
    assert embed["description"].startswith(r"\*\*big\*\* \[x\](http://evil)")
    assert len(discord.make_embed(deal(watch="w" * 400), "t")["title"]) <= discord.MAX_TITLE


@responses.activate
def test_more_than_ten_embeds_are_chunked_and_only_the_first_has_content():
    responses.add(responses.POST, URL, status=204)
    discord.send(CONFIG, message(*[deal(watch=f"W{i}") for i in range(25)]))
    payloads = _payloads()
    assert [len(p["embeds"]) for p in payloads] == [10, 10, 5]
    assert "content" in payloads[0] and "content" not in payloads[1] and "content" not in payloads[2]
    assert payloads[2]["embeds"][-1]["title"] == "W24 — LDLC"


def test_batches_respect_the_total_size_limit():
    embeds = [discord.make_embed(health(detail="x " * 700), "t") for _ in range(8)]
    groups = discord.batches(embeds)
    assert len(groups) > 1
    assert sum(len(g) for g in groups) == 8
    for group in groups:
        assert sum(len(e["title"]) + len(e["description"]) for e in group) <= discord.MAX_MESSAGE_CHARS
    assert discord.batches([]) == []


@responses.activate
def test_message_without_items_still_posts_the_title():
    responses.add(responses.POST, URL, status=204)
    discord.send(CONFIG, Message(title="hello", items=()))
    (payload,) = _payloads()
    assert payload["content"] == "hello" and "embeds" not in payload


@responses.activate
def test_rate_limit_waits_as_told_then_retries_once():
    responses.add(responses.POST, URL, status=429, json={"retry_after": 0.75})
    responses.add(responses.POST, URL, status=204)
    waits: list[float] = []
    discord.send(CONFIG, message(), sleep=waits.append)
    assert waits == [0.75]
    assert len(responses.calls) == 2


@responses.activate
def test_rate_limit_wait_is_capped_and_second_429_fails():
    responses.add(responses.POST, URL, status=429, json={"retry_after": 3600})
    responses.add(responses.POST, URL, status=429, body="slow down")
    waits: list[float] = []
    with pytest.raises(NotifyError, match="HTTP 429"):
        discord.send(CONFIG, message(), sleep=waits.append)
    assert waits == [discord.MAX_RETRY_WAIT]


@responses.activate
def test_rate_limit_without_json_uses_the_default_wait():
    responses.add(responses.POST, URL, status=429, body="<html>")
    responses.add(responses.POST, URL, status=204)
    waits: list[float] = []
    discord.send(CONFIG, message(), sleep=waits.append)
    assert waits == [discord.DEFAULT_RETRY_WAIT]


@responses.activate
def test_http_error_does_not_leak_the_webhook_token():
    responses.add(responses.POST, URL, status=404, body=f'{{"message": "Unknown Webhook", "url": "{URL}"}}')
    with pytest.raises(NotifyError) as err:
        discord.send(CONFIG, message())
    assert "HTTP 404" in str(err.value) and "Unknown Webhook" in str(err.value)
    assert TOKEN not in str(err.value)


@responses.activate
def test_connection_error_does_not_leak_the_webhook_token():
    responses.add(responses.POST, URL, body=requests.ConnectionError(f"Max retries exceeded with url: {URL}"))
    with pytest.raises(NotifyError) as err:
        discord.send(CONFIG, message())
    assert str(err.value) == "connection failed"
    assert TOKEN not in str(err.value)


def test_type_declares_a_secret_discord_only_url(monkeypatch):
    fake_dns(monkeypatch)
    (field,) = discord.TYPE.fields
    assert field.kind == FIELD_URL and field.secret and field.required
    assert clean_config(discord.TYPE, {"webhook_url": f" {URL} "}) == CONFIG
    assert clean_config(discord.TYPE, {"webhook_url": URL.replace("discord.com", "discordapp.com")})
    with pytest.raises(ConfigError):
        clean_config(discord.TYPE, {"webhook_url": "https://evil.example/api/webhooks/1/x"})
