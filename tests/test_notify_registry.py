from __future__ import annotations

import pytest

from pricewatch import netsafe
from pricewatch.notify import CHANNEL_TYPES, send_message
from pricewatch.notify.base import FIELD_SELECT, FIELD_URL, ChannelType, NotifyError

try:
    from tests.notify_helpers import message
except ImportError:  # tests/ is not a package
    from notify_helpers import message

EXPECTED_SECRETS = {
    "discord": {"webhook_url"},
    "telegram": {"bot_token"},
    "email": {"password"},
    "webhook": {"url", "headers", "secret"},
    "slack": {"webhook_url"},
    "ntfy": {"token"},
    "gotify": {"token"},
    "pushover": {"app_token", "user_key"},
    "freemobile": {"api_key"},
    "apprise": {"urls"},
}


def _fake_type(send) -> ChannelType:
    return ChannelType(key="fake", label="Fake", description="", fields=(), send=send)


def test_registry_order_and_keys():
    assert list(CHANNEL_TYPES) == [
        "discord", "telegram", "email", "webhook", "slack", "ntfy", "gotify", "pushover", "freemobile", "apprise",
    ]
    assert all(key == channel_type.key for key, channel_type in CHANNEL_TYPES.items())


@pytest.mark.parametrize("key", list(EXPECTED_SECRETS))
def test_every_type_is_complete_and_flags_its_secrets(key):
    channel_type = CHANNEL_TYPES[key]
    assert channel_type.label and channel_type.description and channel_type.setup_help
    assert callable(channel_type.send)
    names = [f.name for f in channel_type.fields]
    assert len(names) == len(set(names))
    assert {f.name for f in channel_type.fields if f.secret} == EXPECTED_SECRETS[key]
    for field in channel_type.fields:
        assert field.label
        if field.kind == FIELD_SELECT:
            assert field.choices and field.default in {value for value, _ in field.choices}
        if field.name.endswith("url") and field.name != "urls":
            assert field.kind == FIELD_URL


def test_unknown_type():
    with pytest.raises(NotifyError, match="unknown channel type"):
        send_message("carrier-pigeon", {}, message())


def test_unexpected_exceptions_are_reduced_to_their_class_name(monkeypatch):
    def boom(config, msg):
        raise RuntimeError("POST https://hooks.example/s3cr3t-token failed")

    monkeypatch.setitem(CHANNEL_TYPES, "fake", _fake_type(boom))
    with pytest.raises(NotifyError) as err:
        send_message("fake", {}, message())
    assert str(err.value) == "unexpected error (RuntimeError)"
    assert err.value.__suppress_context__


def test_notify_errors_and_guard_refusals_pass_through(monkeypatch):
    def refuse(config, msg):
        raise NotifyError("HTTP 404: nope")

    def unsafe(config, msg):
        raise netsafe.UnsafeURLError("internal.example resolves to a private or reserved address")

    monkeypatch.setitem(CHANNEL_TYPES, "fake", _fake_type(refuse))
    with pytest.raises(NotifyError, match="HTTP 404: nope"):
        send_message("fake", {}, message())
    monkeypatch.setitem(CHANNEL_TYPES, "fake", _fake_type(unsafe))
    with pytest.raises(NotifyError, match="internal.example resolves to a private"):
        send_message("fake", {}, message())


def test_guard_is_active_during_send_and_released_after(monkeypatch):
    seen: list[bool] = []
    monkeypatch.setitem(CHANNEL_TYPES, "fake", _fake_type(lambda config, msg: seen.append(netsafe._public_only.get())))
    send_message("fake", {}, message())
    send_message("fake", {}, message(), allow_private=True)
    assert seen == [True, False]
    assert netsafe._public_only.get() is False


def test_private_targets_are_refused_unless_allowed():
    config = {"url": "http://127.0.0.1:1/hook?token=s3cr3t-value", "method": "POST", "headers": "", "secret": ""}
    with pytest.raises(NotifyError) as err:
        send_message("webhook", config, message())
    assert "private or reserved address" in str(err.value)
    assert "s3cr3t" not in str(err.value)
    with pytest.raises(NotifyError) as err:
        send_message("webhook", config, message(), allow_private=True)
    assert str(err.value) == "connection failed"  # allowed through the guard, nothing listens there


def test_smtp_connections_are_guarded_too():
    config = {
        "host": "127.0.0.1", "port": "1", "security": "none", "username": "", "password": "",
        "from_addr": "bot@example.com", "recipients": "me@example.com",
    }
    with pytest.raises(NotifyError, match="private or reserved address"):
        send_message("email", config, message())
    with pytest.raises(NotifyError, match="could not connect"):
        send_message("email", config, message(), allow_private=True)
