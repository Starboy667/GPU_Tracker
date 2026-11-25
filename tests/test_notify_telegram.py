from __future__ import annotations

import json

import pytest
import requests
import responses

from pricewatch.notify import telegram
from pricewatch.notify.base import ConfigError, NotifyError, clean_config

try:
    from tests.notify_helpers import APP_URL, deal, message
except ImportError:  # tests/ is not a package
    from notify_helpers import APP_URL, deal, message

TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
CONFIG = {"bot_token": TOKEN, "chat_id": "-1001234567890", "topic_id": ""}


def _payloads() -> list[dict]:
    return [json.loads(call.request.body) for call in responses.calls]


@responses.activate
def test_sends_html_with_previews_disabled():
    responses.add(responses.POST, URL, json={"ok": True, "result": {}})
    telegram.send(CONFIG, message())
    (payload,) = _payloads()
    assert payload["chat_id"] == "-1001234567890"
    assert payload["parse_mode"] == "HTML"
    assert payload["link_preview_options"] == {"is_disabled": True}
    assert "message_thread_id" not in payload
    text = payload["text"]
    assert text.startswith("<b>🔥 2 deals</b>\n\n<b><a href=")
    assert '<a href="https://www.ldlc.com/fiche/PB00660000.html">RTX 5070 — LDLC</a>' in text
    assert text.endswith(f'<a href="{APP_URL}">Open Price Watch</a>')
    assert "<br" not in text and "<p>" not in text


@responses.activate
def test_topic_id_becomes_message_thread_id():
    responses.add(responses.POST, URL, json={"ok": True})
    telegram.send({**CONFIG, "topic_id": "42"}, message())
    assert _payloads()[0]["message_thread_id"] == 42


@responses.activate
def test_scraped_text_is_escaped():
    responses.add(responses.POST, URL, json={"ok": True})
    telegram.send(CONFIG, message(deal(title="<i>5 < 6 & 7</i>")))
    assert "&lt;i&gt;5 &lt; 6 &amp; 7&lt;/i&gt;" in _payloads()[0]["text"]


@responses.activate
def test_long_messages_are_split_on_item_boundaries():
    responses.add(responses.POST, URL, json={"ok": True})
    items = [deal(watch=f"Watch {i:02d}", title="T" * 200) for i in range(60)]
    telegram.send(CONFIG, message(*items))
    texts = [p["text"] for p in _payloads()]
    assert len(texts) > 1
    assert all(len(text) <= telegram.MAX_MESSAGE for text in texts)
    joined = "\n\n".join(texts)
    positions = [joined.index(f"Watch {i:02d} — LDLC") for i in range(60)]
    assert positions == sorted(positions)
    assert all(text.count("<b>") == text.count("</b>") for text in texts)


@responses.activate
def test_api_error_description_is_surfaced():
    responses.add(
        responses.POST, URL, status=400, json={"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
    )
    with pytest.raises(NotifyError) as err:
        telegram.send(CONFIG, message())
    assert str(err.value) == "Telegram: Bad Request: chat not found"


@responses.activate
def test_error_stops_before_sending_further_chunks():
    responses.add(responses.POST, URL, status=429, json={"ok": False, "description": "Too Many Requests: retry after 5"})
    with pytest.raises(NotifyError, match="Too Many Requests"):
        telegram.send(CONFIG, message(*[deal(watch=f"W{i}", title="T" * 250) for i in range(60)]))
    assert len(responses.calls) == 1


@responses.activate
def test_ok_false_with_http_200_is_an_error():
    responses.add(responses.POST, URL, status=200, json={"ok": False, "description": "nope"})
    with pytest.raises(NotifyError, match="Telegram: nope"):
        telegram.send(CONFIG, message())


@pytest.mark.parametrize("body", ["<html>Please sign in to the Wi-Fi</html>", "[1, 2]", ""])
@responses.activate
def test_a_200_that_is_not_telegrams_json_is_not_a_delivery(body):
    responses.add(responses.POST, URL, status=200, body=body)
    with pytest.raises(NotifyError) as err:
        telegram.send(CONFIG, message())
    assert str(err.value).startswith(("Telegram: ", "HTTP 200"))


@pytest.mark.parametrize(
    "response",
    [
        {"status": 401, "json": {"ok": False, "description": f"Unauthorized for bot{TOKEN}"}},
        {"status": 404, "body": f"<html>Not Found /bot{TOKEN}/sendMessage</html>"},
        {"status": 502, "body": ""},
        {"body": requests.ConnectionError(f"Max retries exceeded with url: /bot{TOKEN}/sendMessage")},
        {"body": requests.Timeout(f"Read timed out {URL}")},
    ],
)
@responses.activate
def test_the_bot_token_never_reaches_an_error_message(response):
    responses.add(responses.POST, URL, **response)
    with pytest.raises(NotifyError) as err:
        telegram.send(CONFIG, message())
    assert TOKEN not in str(err.value)
    assert TOKEN.split(":")[1] not in str(err.value)
    assert str(err.value)


def test_validate_accepts_good_values():
    assert telegram.validate(CONFIG) == {}
    assert telegram.validate({**CONFIG, "chat_id": "@my_channel", "topic_id": "7"}) == {}
    assert clean_config(telegram.TYPE, {"bot_token": TOKEN, "chat_id": " 4242 "}) == {
        "bot_token": TOKEN,
        "chat_id": "4242",
        "topic_id": "",
    }


def test_validate_reports_format_errors_without_echoing_values():
    bad = {"bot_token": "not-a-token-value", "chat_id": "my chat", "topic_id": "abc"}
    errors = telegram.validate(bad)
    assert set(errors) == {"bot_token", "chat_id", "topic_id"}
    assert not any(value in message for value in bad.values() for message in errors.values())
    with pytest.raises(ConfigError):
        clean_config(telegram.TYPE, bad)


def test_token_field_is_secret():
    fields = {f.name: f for f in telegram.TYPE.fields}
    assert fields["bot_token"].secret and fields["bot_token"].required
    assert not fields["chat_id"].secret
