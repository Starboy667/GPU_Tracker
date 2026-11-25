from __future__ import annotations

import pytest
import requests
import responses

from pricewatch import netsafe
from pricewatch.notify import http
from pricewatch.notify.base import NotifyError

URL = "https://hooks.example/services/T0KEN-s3cr3t-part/another?key=querysecret"


@responses.activate
def test_request_returns_the_response_and_sets_a_user_agent():
    responses.add(responses.POST, URL, json={"ok": True}, status=200)
    response = http.request("POST", URL, json={"a": 1}, headers={"X-Extra": "1"})
    assert response.status_code == 200
    sent = responses.calls[0].request
    assert sent.headers["User-Agent"] == http.USER_AGENT
    assert sent.headers["X-Extra"] == "1"


@responses.activate
def test_redirects_are_not_followed():
    responses.add(responses.POST, URL, status=302, headers={"Location": "http://169.254.169.254/latest"})
    response = http.request("POST", URL, json={})
    assert response.status_code == 302
    assert len(responses.calls) == 1
    with pytest.raises(NotifyError, match="HTTP 302"):
        http.ensure_ok(response)


@pytest.mark.parametrize(
    ("exception", "expected"),
    [
        (requests.Timeout(f"timeout for {URL}"), "timed out"),
        (requests.ConnectTimeout(f"connect timeout for {URL}"), "timed out"),
        (requests.exceptions.SSLError(f"bad cert for {URL}"), "TLS handshake failed"),
        (requests.ConnectionError(f"HTTPSConnectionPool: Max retries exceeded with url: {URL}"), "connection failed"),
        (requests.exceptions.TooManyRedirects(URL), "request failed (TooManyRedirects)"),
        (UnicodeEncodeError("latin-1", "é", 0, 1, URL), "invalid request (check the URL and headers)"),
    ],
)
@responses.activate
def test_transport_errors_are_mapped_without_leaking_the_url(exception, expected):
    responses.add(responses.POST, URL, body=exception)
    with pytest.raises(NotifyError) as err:
        http.request("POST", URL, json={})
    assert str(err.value) == expected
    assert "s3cr3t" not in str(err.value)
    assert err.value.__cause__ is None and err.value.__suppress_context__


def test_guard_refusal_is_passed_through_as_is():
    with netsafe.guard(), pytest.raises(NotifyError, match="private or reserved address"):
        http.request("POST", "http://127.0.0.1:1/hook", json={})


@responses.activate
def test_ensure_ok_builds_a_short_redacted_excerpt():
    body = "Unknown   webhook\n\n" + URL + " " + "x" * 500
    responses.add(responses.POST, URL, body=body, status=404)
    response = http.request("POST", URL, json={})
    with pytest.raises(NotifyError) as err:
        http.ensure_ok(response, secrets=http.url_secrets(URL))
    message = str(err.value)
    assert message.startswith("HTTP 404: Unknown webhook ***")
    assert "s3cr3t" not in message and "querysecret" not in message
    assert len(message) <= len("HTTP 404: ") + http.EXCERPT_LENGTH


@responses.activate
def test_ensure_ok_without_body_and_on_success():
    responses.add(responses.POST, URL, status=500)
    with pytest.raises(NotifyError) as err:
        http.ensure_ok(http.request("POST", URL))
    assert str(err.value) == "HTTP 500"
    responses.replace(responses.POST, URL, status=204)
    http.ensure_ok(http.request("POST", URL))


def test_redact_ignores_short_values_and_replaces_longest_first():
    assert http.redact("abc 123456 1234567890", ("abc", "123456", "1234567890", "")) == "abc *** ***"


def test_url_secrets_lists_url_path_segments_query_values_and_password():
    secrets = http.url_secrets("https://user:hunter22@host.example/a/tokentoken?key=value123")
    assert secrets[0].startswith("https://user:hunter22@")
    assert {"a", "tokentoken", "value123", "hunter22"} <= set(secrets)
    assert http.url_secrets("http://[bad") == ("http://[bad",)


# --------------------------------------------------------------------------- hostile endpoints

URL_BIG = "https://hooks.example/big"


@responses.activate
def test_a_huge_response_is_cut_not_held_in_memory():
    responses.post(URL_BIG, body="x" * (http.MAX_RESPONSE_BYTES * 4), status=500)
    response = http.request("POST", URL_BIG, json={})
    assert len(response.content) == http.MAX_RESPONSE_BYTES
    with pytest.raises(NotifyError, match="HTTP 500: xxx"):
        http.ensure_ok(response)


@responses.activate
def test_a_response_that_never_ends_is_a_timeout(monkeypatch):
    responses.post(URL_BIG, body="drip")
    with pytest.raises(NotifyError, match="timed out"):
        http.request("POST", URL_BIG, json={}, timeout=-1)
