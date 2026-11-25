"""Slack, ntfy and Gotify."""
from __future__ import annotations

import json

import pytest
import requests
import responses

from pricewatch.notify import gotify, ntfy, slack
from pricewatch.notify.base import ConfigError, NotifyError, clean_config

try:
    from tests.notify_helpers import APP_URL, deal, fake_dns, health, message
except ImportError:  # tests/ is not a package
    from notify_helpers import APP_URL, deal, fake_dns, health, message

SLACK_URL = "https://hooks.slack.com/services/T000/B000/slack-s3cr3t-token"
NTFY_TOKEN = "tk_ntfy-s3cr3t-token"
GOTIFY_TOKEN = "gotify-s3cr3t-token"


def _json(index: int = 0) -> dict:
    return json.loads(responses.calls[index].request.body)


# --------------------------------------------------------------------------- Slack

@responses.activate
def test_slack_posts_mrkdwn_text_with_links():
    responses.add(responses.POST, SLACK_URL, body="ok")
    slack.send({"webhook_url": SLACK_URL}, message())
    payload = _json()
    assert set(payload) == {"text"}
    text = payload["text"]
    assert text.startswith("*🔥 2 deals*\n\n*<https://www.ldlc.com/fiche/PB00660000.html|RTX 5070 — LDLC>*\n")
    assert "919,99 € (-12 % vs usual 1 049,00 €, median over 14 days)" in text
    assert text.endswith(f"<{APP_URL}|Open Price Watch>")


def test_slack_escapes_control_characters_and_link_separators():
    item = deal(title="5 < 6 & <!channel>", watch="A|B", url="https://shop.example/a|b>c")
    text = slack.item_mrkdwn(item)
    assert "5 &lt; 6 &amp; &lt;!channel&gt;" in text
    assert "<https://shop.example/a%7Cb%3Ec|A|B — LDLC>" in text
    assert slack.link("x", "javascript:alert(1)") == "x"


@responses.activate
def test_slack_error_does_not_leak_the_webhook():
    responses.add(responses.POST, SLACK_URL, status=404, body="no_service")
    with pytest.raises(NotifyError) as err:
        slack.send({"webhook_url": SLACK_URL}, message())
    assert str(err.value) == "HTTP 404: no_service"
    responses.replace(responses.POST, SLACK_URL, body=requests.ConnectionError(f"url: {SLACK_URL}"))
    with pytest.raises(NotifyError) as err:
        slack.send({"webhook_url": SLACK_URL}, message())
    assert "s3cr3t" not in str(err.value)


def test_slack_url_is_secret_and_accepts_self_hosted_hosts(monkeypatch):
    fake_dns(monkeypatch)
    (field,) = slack.TYPE.fields
    assert field.secret and field.url_domains is None
    assert "Mattermost" in field.help
    assert clean_config(slack.TYPE, {"webhook_url": "https://chat.example.org/hooks/abc"})


# --------------------------------------------------------------------------- ntfy

NTFY_CONFIG = {"server_url": "https://ntfy.sh", "topic": "pricewatch-7f3k2", "token": "", "priority": "4"}


@responses.activate
def test_ntfy_publishes_json_to_the_server_root():
    responses.add(responses.POST, "https://ntfy.sh/", json={"id": "x"})
    ntfy.send(NTFY_CONFIG, message())
    request = responses.calls[0].request
    assert "Authorization" not in request.headers
    payload = _json()
    assert payload["topic"] == "pricewatch-7f3k2"
    assert payload["title"] == "🔥 2 deals"
    assert payload["markdown"] is True
    assert payload["priority"] == 4
    assert payload["click"] == "https://www.ldlc.com/fiche/PB00660000.html"
    assert payload["tags"] == ["fire", "package"]
    assert payload["message"].startswith("**[RTX 5070 — LDLC](https://www.ldlc.com/fiche/PB00660000.html)**")
    assert "🔥 2 deals" not in payload["message"]


@responses.activate
def test_ntfy_self_hosted_server_with_token():
    responses.add(responses.POST, "https://push.example.org/ntfy/", json={})
    config = {**NTFY_CONFIG, "server_url": "https://push.example.org/ntfy/", "token": NTFY_TOKEN, "priority": ""}
    ntfy.send(config, message(health(), app_url=""))
    request = responses.calls[0].request
    assert request.headers["Authorization"] == f"Bearer {NTFY_TOKEN}"
    payload = _json()
    assert payload["priority"] == 3 and payload["tags"] == ["warning"]
    assert "click" not in payload


def test_ntfy_message_fits_the_byte_limit():
    items = [deal(watch=f"W{i}", title="€" * 280) for i in range(20)]
    payload = ntfy.build_payload(NTFY_CONFIG, message(*items))
    assert len(payload["message"].encode("utf-8")) <= ntfy.MAX_MESSAGE_BYTES
    assert payload["message"].endswith("…")


@responses.activate
def test_ntfy_error_does_not_leak_the_token():
    responses.add(responses.POST, "https://ntfy.sh/", status=403, body=f'{{"error":"forbidden {NTFY_TOKEN}"}}')
    with pytest.raises(NotifyError) as err:
        ntfy.send({**NTFY_CONFIG, "token": NTFY_TOKEN}, message())
    assert "HTTP 403" in str(err.value) and "s3cr3t" not in str(err.value)


def test_ntfy_validation_and_defaults(monkeypatch):
    fake_dns(monkeypatch)
    cleaned = clean_config(ntfy.TYPE, {"topic": "deals_1"})
    assert cleaned == {"server_url": "https://ntfy.sh", "topic": "deals_1", "token": "", "priority": "3"}
    for topic in ("has space", "a/b", "x" * 65):
        with pytest.raises(ConfigError) as err:
            clean_config(ntfy.TYPE, {"topic": topic})
        assert set(err.value.errors) == {"topic"}
    assert {f.name for f in ntfy.TYPE.fields if f.secret} == {"token"}


# --------------------------------------------------------------------------- Gotify

GOTIFY_CONFIG = {"server_url": "https://gotify.example.org/", "token": GOTIFY_TOKEN, "priority": "7"}


@responses.activate
def test_gotify_posts_markdown_with_click_url():
    responses.add(responses.POST, "https://gotify.example.org/message", json={"id": 1})
    gotify.send(GOTIFY_CONFIG, message())
    request = responses.calls[0].request
    assert request.headers["X-Gotify-Key"] == GOTIFY_TOKEN
    assert GOTIFY_TOKEN not in request.url
    payload = _json()
    assert payload["title"] == "🔥 2 deals"
    assert payload["priority"] == 7
    assert payload["extras"]["client::display"] == {"contentType": "text/markdown"}
    assert payload["extras"]["client::notification"] == {
        "click": {"url": "https://www.ldlc.com/fiche/PB00660000.html"}
    }
    assert "**[RTX 5070 — LDLC]" in payload["message"]


def test_gotify_without_any_link_has_no_click_extra():
    payload = gotify.build_payload({**GOTIFY_CONFIG, "priority": ""}, message(health(), app_url=""))
    assert "client::notification" not in payload["extras"]
    assert payload["priority"] == gotify.DEFAULT_PRIORITY


@responses.activate
def test_gotify_error_does_not_leak_the_token():
    body = f'{{"error":"Unauthorized","errorCode":401,"errorDescription":"bad token {GOTIFY_TOKEN}"}}'
    responses.add(responses.POST, "https://gotify.example.org/message", status=401, body=body)
    with pytest.raises(NotifyError) as err:
        gotify.send(GOTIFY_CONFIG, message())
    assert "HTTP 401" in str(err.value) and "Unauthorized" in str(err.value)
    assert "s3cr3t" not in str(err.value)


def test_gotify_validation(monkeypatch):
    fake_dns(monkeypatch)
    cleaned = clean_config(gotify.TYPE, {"server_url": "https://gotify.example.org", "token": GOTIFY_TOKEN})
    assert cleaned["priority"] == "5"
    for priority in ("11", "-1", "2.5"):
        assert set(gotify.validate({**GOTIFY_CONFIG, "priority": priority})) == {"priority"}
    assert {f.name for f in gotify.TYPE.fields if f.secret} == {"token"}


def test_slack_text_cannot_ping_a_channel():
    from pricewatch.notify import slack

    escaped = slack.escape("Great deal @channel @all <b>")
    assert "@channel" not in escaped and "@all" not in escaped and "&lt;b&gt;" in escaped
    assert escaped.replace(slack.ZERO_WIDTH_SPACE, "") == "Great deal @channel @all &lt;b&gt;"
