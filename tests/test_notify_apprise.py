from __future__ import annotations

import json
import threading

import pytest
import responses

from pricewatch import netsafe
from pricewatch.notify import apprise_url, send_message
from pricewatch.notify.base import ConfigError, NotifyError, clean_config

try:
    from tests.notify_helpers import health, message
except ImportError:  # tests/ is not a package
    from notify_helpers import health, message

HOOK = "http://example.com/hook"
SECRET_URL = "json://user:p4ssw0rd-s3cr3t@example.com/hook"


@responses.activate
def test_sends_title_and_markdown_body_through_apprise():
    responses.add(responses.POST, HOOK, body="ok")
    apprise_url.send({"urls": "json://example.com/hook"}, message())
    (call,) = responses.calls
    payload = json.loads(call.request.body)
    assert payload["title"] == "🔥 2 deals"
    assert payload["type"] == "info"
    assert payload["message"].startswith("**[RTX 5070 — LDLC](https://www.ldlc.com/fiche/PB00660000.html)**")
    assert "🔥 2 deals" not in payload["message"]


@responses.activate
def test_every_line_is_notified_in_the_calling_thread():
    responses.add(responses.POST, HOOK, body="ok")
    responses.add(responses.POST, "http://other.example.org/in", body="ok")
    before = threading.active_count()
    apprise_url.send({"urls": "json://example.com/hook\r\n\r\n  json://other.example.org/in  "}, message())
    assert [call.request.url for call in responses.calls] == [HOOK, "http://other.example.org/in"]
    assert threading.active_count() == before


@responses.activate
def test_health_only_messages_are_sent_as_warnings():
    responses.add(responses.POST, HOOK, body="ok")
    apprise_url.send({"urls": "json://example.com/hook"}, message(health()))
    assert json.loads(responses.calls[0].request.body)["type"] == "warning"


@responses.activate
def test_delivery_failure_is_reported_without_details():
    responses.add(responses.POST, HOOK, status=500, body="boom p4ssw0rd-s3cr3t")
    with pytest.raises(NotifyError) as err:
        apprise_url.send({"urls": SECRET_URL}, message())
    assert str(err.value) == "Apprise reported a delivery failure"


@pytest.mark.parametrize(
    ("urls", "expected"),
    [
        ("json://example.com/hook\nnot a url s3cr3t", "line 2 is not an Apprise URL"),
        ("zzzz://s3cr3t@whatever", "line 1 is not a valid Apprise URL"),
        ("json://example.com/hook\n\nsyslog://s3cr3t", "line 2 uses a service that cannot be used"),
        ("DBUS://s3cr3t", "line 1 uses a service that cannot be used"),
        ("rsyslog://10.0.0.1/s3cr3t", "line 1 uses a service that cannot be used"),
        ("\n".join(f"json://h{i}.example.com/s3cr3t" for i in range(11)), "at most 10 URLs"),
    ],
)
def test_validate_names_the_line_never_the_url(urls, expected):
    errors = apprise_url.validate({"urls": urls})
    assert set(errors) == {"urls"}
    assert expected in errors["urls"]
    assert "s3cr3t" not in errors["urls"]
    with pytest.raises(ConfigError):
        clean_config(apprise_url.TYPE, {"urls": urls})


def test_validate_accepts_common_services():
    urls = "\n".join(
        [
            "json://example.com/hook",
            "tgram://123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw/12345",
            "matrixs://user:pass@matrix.example.org/#room",
            "mailtos://user:pass@example.com?to=me@example.com",
        ]
    )
    assert apprise_url.validate({"urls": urls}) == {}
    assert clean_config(apprise_url.TYPE, {"urls": f"\n{urls}\n"}) == {"urls": urls}


def test_send_rechecks_stored_urls():
    with pytest.raises(NotifyError, match="line 1 uses a service that cannot be used"):
        apprise_url.send({"urls": "syslog://"}, message())
    with pytest.raises(NotifyError, match="line 2 is not a valid Apprise URL"):
        apprise_url.send({"urls": "json://example.com/hook\nzzzz://nope"}, message())
    with pytest.raises(NotifyError, match="no Apprise URL configured"):
        apprise_url.send({"urls": " \n "}, message())


def test_guard_covers_connections_made_by_apprise(monkeypatch):
    checked: list[str] = []
    real = netsafe.is_public_ip

    def spy(value: str) -> bool:
        checked.append(value)
        return real(value)

    monkeypatch.setattr(netsafe, "is_public_ip", spy)
    with pytest.raises(NotifyError) as err:
        send_message("apprise", {"urls": "json://user:p4ssw0rd-s3cr3t@127.0.0.1:1/hook"}, message())
    assert "127.0.0.1" in checked  # the guard saw the connection attempt and refused it
    assert "s3cr3t" not in str(err.value)


def test_field_is_secret_and_description_lists_what_it_unlocks():
    (field,) = apprise_url.TYPE.fields
    assert field.secret and field.required
    for service in ("Matrix", "Signal", "Teams", "WhatsApp", "Home Assistant"):
        assert service in apprise_url.TYPE.description
    assert apprise_url.TYPE.docs_url == "https://github.com/caronc/apprise/wiki"
