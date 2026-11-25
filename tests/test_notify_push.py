"""Pushover and Free Mobile SMS."""
from __future__ import annotations

import json
from urllib.parse import parse_qs

import pytest
import requests
import responses

from pricewatch.notify import freemobile, pushover
from pricewatch.notify.base import ConfigError, NotifyError, clean_config

try:
    from tests.notify_helpers import APP_URL, deal, health, message
except ImportError:  # tests/ is not a package
    from notify_helpers import APP_URL, deal, health, message

APP_TOKEN = "a" * 10 + "S3cr3tAppT0ken" + "b" * 6
USER_KEY = "u" * 10 + "S3cr3tUserKey0" + "v" * 6
PUSHOVER_CONFIG = {"app_token": APP_TOKEN, "user_key": USER_KEY, "device": "", "priority": "1"}

FREE_KEY = "FreeApiKeyS3cr3t"
FREE_CONFIG = {"user": "12345678", "api_key": FREE_KEY}


def _form() -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(responses.calls[0].request.body).items()}


# --------------------------------------------------------------------------- Pushover

@responses.activate
def test_pushover_sends_html_form_with_first_link():
    responses.add(responses.POST, pushover.API_URL, json={"status": 1})
    pushover.send({**PUSHOVER_CONFIG, "device": "phone"}, message())
    form = _form()
    assert form["token"] == APP_TOKEN and form["user"] == USER_KEY
    assert form["title"] == "🔥 2 deals"
    assert form["html"] == "1"
    assert form["priority"] == "1"
    assert form["device"] == "phone"
    assert form["url"] == "https://www.ldlc.com/fiche/PB00660000.html"
    assert form["url_title"] == "RTX 5070 — LDLC"
    assert form["message"].startswith('<b><a href="https://www.ldlc.com/fiche/PB00660000.html">RTX 5070 — LDLC</a></b>\n')


def test_pushover_drops_whole_items_to_fit_the_limit():
    data = pushover.build_data(PUSHOVER_CONFIG, message(*[deal(watch=f"W{i}") for i in range(12)]))
    assert len(data["message"]) <= pushover.MAX_MESSAGE
    assert data["html"] == "1"
    assert data["message"].count("<b>") == data["message"].count("</b>")
    assert "… and " in data["message"] and data["message"].endswith(" more")
    assert "device" not in data


def test_pushover_falls_back_to_plain_text_when_one_item_is_too_big():
    long_url = "https://shop.example/" + "p" * 1200
    data = pushover.build_data(PUSHOVER_CONFIG, message(deal(url=long_url)))
    assert "html" not in data
    assert len(data["message"]) <= pushover.MAX_MESSAGE
    assert "<b>" not in data["message"]
    assert (data["url"], data["url_title"]) == (APP_URL, "Open Price Watch")


def test_pushover_without_any_link_or_item():
    data = pushover.build_data(PUSHOVER_CONFIG, message(health(), app_url=""))
    assert "url" not in data and "url_title" not in data
    assert len(pushover.build_data(PUSHOVER_CONFIG, message(deal(watch="w" * 600), title="t" * 600))["title"]) == 250


@responses.activate
def test_pushover_surfaces_the_api_errors():
    responses.add(
        responses.POST,
        pushover.API_URL,
        status=400,
        json={"status": 0, "errors": ["user identifier is invalid", f"token {APP_TOKEN} is bad"]},
    )
    with pytest.raises(NotifyError) as err:
        pushover.send(PUSHOVER_CONFIG, message())
    assert str(err.value).startswith("Pushover: user identifier is invalid; token ***")
    assert "S3cr3t" not in str(err.value)


@pytest.mark.parametrize(
    "response",
    [
        {"status": 500, "body": f"<html>upstream error for {USER_KEY}</html>"},
        {"status": 429, "body": ""},
        {"body": requests.ConnectionError(f"failed token={APP_TOKEN}")},
    ],
)
@responses.activate
def test_pushover_never_leaks_keys(response):
    responses.add(responses.POST, pushover.API_URL, **response)
    with pytest.raises(NotifyError) as err:
        pushover.send(PUSHOVER_CONFIG, message())
    assert "S3cr3t" not in str(err.value) and str(err.value)


def test_pushover_validation():
    assert pushover.validate(PUSHOVER_CONFIG) == {}
    errors = pushover.validate({"app_token": "short-S3cr3t", "user_key": USER_KEY + "!", "device": "my phone"})
    assert set(errors) == {"app_token", "user_key", "device"}
    assert not any("S3cr3t" in text for text in errors.values())
    cleaned = clean_config(pushover.TYPE, {"app_token": APP_TOKEN, "user_key": USER_KEY})
    assert cleaned["priority"] == "0"
    with pytest.raises(ConfigError):
        clean_config(pushover.TYPE, {"app_token": APP_TOKEN, "user_key": USER_KEY, "priority": "2"})
    assert {f.name for f in pushover.TYPE.fields if f.secret} == {"app_token", "user_key"}


# --------------------------------------------------------------------------- Free Mobile

@responses.activate
def test_freemobile_posts_plain_text_sms():
    responses.add(responses.POST, freemobile.API_URL, status=200)
    freemobile.send(FREE_CONFIG, message(*[deal(watch=f"W{i}") for i in range(10)]))
    payload = json.loads(responses.calls[0].request.body)
    assert payload["user"] == "12345678" and payload["pass"] == FREE_KEY
    assert payload["msg"].startswith("🔥 2 deals\n\nW0 — LDLC\n")
    assert len(payload["msg"]) <= freemobile.MAX_SMS
    assert "<" not in payload["msg"]
    assert FREE_KEY not in responses.calls[0].request.url


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, "a required parameter is missing"),
        (402, "too many SMS"),
        (403, "not enabled on this line, or the user id / key is wrong"),
        (500, "server error"),
        (418, "Free Mobile: HTTP 418"),
    ],
)
@responses.activate
def test_freemobile_error_mapping(status, expected):
    responses.add(responses.POST, freemobile.API_URL, status=status, body=f"echo {FREE_KEY}")
    with pytest.raises(NotifyError) as err:
        freemobile.send(FREE_CONFIG, message())
    assert expected in str(err.value)
    assert FREE_KEY not in str(err.value)


@responses.activate
def test_freemobile_connection_error_does_not_leak_the_key():
    responses.add(responses.POST, freemobile.API_URL, body=requests.ConnectionError(f"pass={FREE_KEY}"))
    with pytest.raises(NotifyError) as err:
        freemobile.send(FREE_CONFIG, message())
    assert str(err.value) == "connection failed"


def test_freemobile_validation():
    assert freemobile.validate(FREE_CONFIG) == {}
    assert set(freemobile.validate({**FREE_CONFIG, "user": "0612-345"})) == {"user"}
    assert {f.name for f in freemobile.TYPE.fields if f.secret} == {"api_key"}
    assert clean_config(freemobile.TYPE, {"user": " 12345678 ", "api_key": FREE_KEY}) == FREE_CONFIG
