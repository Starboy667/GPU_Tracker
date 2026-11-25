from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime

import pytest
import requests
import responses

from pricewatch.notify import webhook
from pricewatch.notify.base import ConfigError, NotifyError, clean_config

try:
    from tests.notify_helpers import APP_URL, fake_dns, message
except ImportError:  # tests/ is not a package
    from notify_helpers import APP_URL, fake_dns, message

URL = "https://hooks.example/in/path-t0ken-s3cr3t?auth=query-s3cr3t"
SIGNING_SECRET = "signing-s3cr3t-value"
BEARER = "Bearer header-s3cr3t-value"
CONFIG = {"url": URL, "method": "POST", "headers": "", "secret": ""}


@responses.activate
def test_posts_the_documented_json_body():
    responses.add(responses.POST, URL, status=200)
    webhook.send(CONFIG, message())
    request = responses.calls[0].request
    assert request.headers["Content-Type"] == "application/json; charset=utf-8"
    assert webhook.SIGNATURE_HEADER not in request.headers
    body = json.loads(request.body)
    assert body["event"] == "alerts"
    assert body["title"] == "🔥 2 deals"
    assert body["app_url"] == APP_URL
    assert datetime.fromisoformat(body["sent_at"]).utcoffset().total_seconds() == 0
    assert body["items"][0] == {
        "kind": "deal",
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
        "detail": "",
    }
    assert body["items"][1]["kind"] == "restock"
    assert "🔥".encode() in request.body  # sent as UTF-8, not \u escapes


@responses.activate
def test_signature_is_the_hmac_of_the_exact_body():
    responses.add(responses.POST, URL, status=204)
    webhook.send({**CONFIG, "secret": SIGNING_SECRET}, message())
    request = responses.calls[0].request
    expected = hmac.new(SIGNING_SECRET.encode(), request.body, hashlib.sha256).hexdigest()
    assert request.headers[webhook.SIGNATURE_HEADER] == f"sha256={expected}"


@responses.activate
def test_put_method_and_custom_headers():
    responses.add(responses.PUT, URL, status=201)
    headers = f"Authorization: {BEARER}\r\nX-Source : pricewatch\r\n\r\nHost: evil.example\r\ncontent-length: 1\r\n"
    webhook.send({**CONFIG, "method": "PUT", "headers": headers}, message())
    sent = responses.calls[0].request.headers
    assert sent["Authorization"] == BEARER
    assert sent["X-Source"] == "pricewatch"
    assert sent.get("Host") != "evil.example"
    assert sent["Content-Length"] != "1"


def test_parse_headers_drops_framing_headers():
    assert webhook.parse_headers("Host: a\nContent-Length: 3\nTransfer-Encoding: chunked\nConnection: close\nX-A: b") == {
        "X-A": "b"
    }
    assert webhook.parse_headers("") == {}


@pytest.mark.parametrize(
    "text",
    [
        "no colon here s3cr3t",
        "Bad Name: s3cr3t",
        ": s3cr3t",
        "X-A: fine\nX-B: evil\rX-Injected: s3cr3t",
        "X-A: café-s3cr3t",
        "\n".join(f"X-{i}: s3cr3t" for i in range(21)),
    ],
)
def test_malformed_headers_are_rejected_without_echoing_them(text):
    errors = webhook.validate({**CONFIG, "headers": text})
    assert set(errors) == {"headers"}
    assert "s3cr3t" not in errors["headers"]
    with pytest.raises(NotifyError) as err:
        webhook.send({**CONFIG, "headers": text}, message())
    assert "s3cr3t" not in str(err.value)


@responses.activate
def test_non_2xx_is_an_error_with_secrets_redacted():
    echo = f"denied for {URL} with {BEARER} and {SIGNING_SECRET}"
    responses.add(responses.POST, URL, status=500, body=echo)
    config = {**CONFIG, "headers": f"Authorization: {BEARER}", "secret": SIGNING_SECRET}
    with pytest.raises(NotifyError) as err:
        webhook.send(config, message())
    text = str(err.value)
    assert text.startswith("HTTP 500: denied for")
    assert "s3cr3t" not in text


@responses.activate
def test_redirects_are_not_followed():
    responses.add(responses.POST, URL, status=302, headers={"Location": "http://10.0.0.1/internal"})
    with pytest.raises(NotifyError, match="HTTP 302"):
        webhook.send(CONFIG, message())
    assert len(responses.calls) == 1


@responses.activate
def test_connection_error_does_not_leak_the_url():
    responses.add(responses.POST, URL, body=requests.ConnectionError(f"Max retries exceeded with url: {URL}"))
    with pytest.raises(NotifyError) as err:
        webhook.send(CONFIG, message())
    assert str(err.value) == "connection failed"


def test_clean_config_defaults_and_secret_fields(monkeypatch):
    fake_dns(monkeypatch)
    cleaned = clean_config(webhook.TYPE, {"url": URL})
    assert cleaned == {"url": URL, "method": "POST", "headers": "", "secret": ""}
    fields = {f.name: f for f in webhook.TYPE.fields}
    assert fields["url"].secret and fields["headers"].secret and fields["secret"].secret
    with pytest.raises(ConfigError):
        clean_config(webhook.TYPE, {"url": URL, "method": "DELETE"})
    kept = clean_config(webhook.TYPE, {"url": "", "headers": ""}, existing={"url": URL, "headers": "X-A: b"})
    assert kept["url"] == URL and kept["headers"] == "X-A: b"


def test_clean_config_refuses_private_targets_unless_allowed():
    with pytest.raises(ConfigError) as err:
        clean_config(webhook.TYPE, {"url": "http://10.0.0.5/hook"})
    assert set(err.value.errors) == {"url"}
    assert clean_config(webhook.TYPE, {"url": "http://10.0.0.5/hook"}, allow_private=True)["url"] == "http://10.0.0.5/hook"
