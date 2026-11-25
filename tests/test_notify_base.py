from __future__ import annotations

import dataclasses

import pytest

from pricewatch.notify.base import (
    FIELD_BOOL,
    FIELD_NUMBER,
    FIELD_SELECT,
    FIELD_URL,
    ChannelType,
    ConfigError,
    Field,
    clean_config,
    masked_config,
)

try:
    from tests.notify_helpers import fake_dns
except ImportError:  # tests/ is not a package
    from notify_helpers import fake_dns


def _type(*fields: Field, validate=None) -> ChannelType:
    return ChannelType(
        key="fake", label="Fake", description="", fields=fields, send=lambda config, message: None, validate=validate
    )


def test_required_field_missing():
    with pytest.raises(ConfigError) as err:
        clean_config(_type(Field("token", "Token", required=True)), {"token": "   "})
    assert err.value.errors == {"token": "required"}
    assert "token: required" in str(err.value)


def test_optional_field_defaults_to_empty_and_values_are_stripped():
    kind = _type(Field("name", "Name"), Field("topic", "Topic", required=True))
    assert clean_config(kind, {"topic": "  deals  "}) == {"name": "", "topic": "deals"}


def test_default_fills_a_blank_required_field():
    kind = _type(Field("server", "Server", required=True, default="https://ntfy.sh"))
    assert clean_config(kind, {}, allow_private=True) == {"server": "https://ntfy.sh"}


def test_blank_secret_keeps_existing_value_but_new_value_wins():
    kind = _type(Field("token", "Token", required=True, secret=True))
    assert clean_config(kind, {"token": ""}, existing={"token": "old"}) == {"token": "old"}
    assert clean_config(kind, {"token": "new"}, existing={"token": "old"}) == {"token": "new"}


def test_blank_non_secret_does_not_inherit_existing():
    kind = _type(Field("device", "Device"))
    assert clean_config(kind, {"device": ""}, existing={"device": "phone"}) == {"device": ""}


def test_too_long_value():
    with pytest.raises(ConfigError) as err:
        clean_config(_type(Field("name", "Name")), {"name": "x" * 4001})
    assert err.value.errors == {"name": "too long"}


def test_number_and_select_validation():
    kind = _type(
        Field("port", "Port", kind=FIELD_NUMBER),
        Field("mode", "Mode", kind=FIELD_SELECT, choices=(("a", "A"), ("b", "B"))),
    )
    assert clean_config(kind, {"port": "587", "mode": "b"}) == {"port": "587", "mode": "b"}
    with pytest.raises(ConfigError) as err:
        clean_config(kind, {"port": "abc", "mode": "z"})
    assert err.value.errors == {"port": "must be a number", "mode": "not one of the allowed values"}


@pytest.mark.parametrize(("raw", "expected"), [("on", True), ("1", True), (True, True), ("", False), (None, False)])
def test_bool_field(raw, expected):
    assert clean_config(_type(Field("silent", "Silent", kind=FIELD_BOOL)), {"silent": raw}) == {"silent": expected}


def test_url_field_rejects_private_targets_unless_allowed():
    kind = _type(Field("url", "URL", kind=FIELD_URL, required=True))
    with pytest.raises(ConfigError) as err:
        clean_config(kind, {"url": "http://127.0.0.1:8080/hook"})
    assert "private" in err.value.errors["url"]
    assert clean_config(kind, {"url": "http://127.0.0.1:8080/hook"}, allow_private=True) == {
        "url": "http://127.0.0.1:8080/hook"
    }


def test_url_field_scheme_and_domain_restriction(monkeypatch):
    fake_dns(monkeypatch)
    kind = _type(Field("url", "URL", kind=FIELD_URL, url_domains=("discord.com",)))
    assert clean_config(kind, {"url": "https://discord.com/api/webhooks/1/x"})["url"].startswith("https://discord.com")
    for bad in ("https://evil.example/api/webhooks/1/x", "file:///etc/passwd", "https://discord.com.evil.example/x"):
        with pytest.raises(ConfigError):
            clean_config(kind, {"url": bad})


def test_validate_hook_runs_after_field_checks_and_reports_errors():
    calls = []

    def validate(config):
        calls.append(dict(config))
        return {"token": "wrong format"} if config["token"] != "ok" else {}

    kind = _type(Field("token", "Token", required=True), validate=validate)
    assert clean_config(kind, {"token": "ok"}) == {"token": "ok"}
    with pytest.raises(ConfigError) as err:
        clean_config(kind, {"token": "nope"})
    assert err.value.errors == {"token": "wrong format"}
    with pytest.raises(ConfigError):
        clean_config(kind, {"token": ""})
    assert calls == [{"token": "ok"}, {"token": "nope"}]  # not called when a field already failed


def test_masked_config_blanks_secrets_and_applies_defaults():
    kind = _type(
        Field("token", "Token", secret=True),
        Field("chat", "Chat"),
        Field("priority", "Priority", default="3"),
    )
    assert masked_config(kind, {"token": "s3cret", "chat": "42"}) == {"token": "", "chat": "42", "priority": "3"}


def test_types_are_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        Field("a", "A").name = "b"  # type: ignore[misc]


# --------------------------------------------------------------------------- secrets and destinations

def test_stored_secrets_do_not_follow_a_channel_to_a_new_destination():
    """Otherwise anyone allowed to edit a channel could redirect it and collect its credentials."""
    from pricewatch.notify import CHANNEL_TYPES

    ntfy = CHANNEL_TYPES["ntfy"]
    stored = {"server_url": "https://ntfy.example.org", "topic": "deals", "token": "tk_stored", "priority": "3"}

    kept = clean_config(ntfy, {"server_url": "https://ntfy.example.org", "topic": "other", "priority": "5"},
                        existing=stored, allow_private=True)
    assert kept["token"] == "tk_stored"  # same server: blank still means "keep"

    with pytest.raises(ConfigError) as caught:
        clean_config(ntfy, {"server_url": "https://attacker.example", "topic": "deals"}, existing=stored,
                     allow_private=True)
    assert caught.value.errors == {"token": "enter it again: the destination of this channel changed"}

    moved = clean_config(ntfy, {"server_url": "https://new.example", "topic": "deals", "token": "tk_new"},
                         existing=stored, allow_private=True)
    assert moved["token"] == "tk_new"


def test_smtp_password_does_not_follow_a_new_host_or_port():
    from pricewatch.notify import CHANNEL_TYPES

    email = CHANNEL_TYPES["email"]
    stored = {"host": "smtp.example.org", "port": "587", "security": "starttls", "username": "me",
              "password": "app-password", "from_addr": "me@example.org", "recipients": "me@example.org"}
    same = {k: v for k, v in stored.items() if k != "password"}
    assert clean_config(email, {**same, "recipients": "you@example.org"}, existing=stored)["password"] == "app-password"
    for change in ({"host": "smtp.attacker.example"}, {"port": "2525"}):
        with pytest.raises(ConfigError) as caught:
            clean_config(email, {**same, **change}, existing=stored)
        assert "password" in caught.value.errors


def test_webhook_headers_and_signing_secret_do_not_follow_a_new_url():
    from pricewatch.notify import CHANNEL_TYPES

    webhook = CHANNEL_TYPES["webhook"]
    stored = {"url": "https://hooks.example.org/in", "method": "POST", "headers": "Authorization: Bearer abc",
              "secret": "signing-secret"}
    assert clean_config(webhook, {"method": "PUT"}, existing=stored, allow_private=True)["secret"] == "signing-secret"
    with pytest.raises(ConfigError) as caught:
        clean_config(webhook, {"url": "https://attacker.example/in", "method": "POST"}, existing=stored,
                     allow_private=True)
    assert set(caught.value.errors) == {"headers", "secret"}
