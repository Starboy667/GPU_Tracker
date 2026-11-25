from __future__ import annotations

import smtplib
import ssl

import pytest

from pricewatch.notify import smtp
from pricewatch.notify.base import ConfigError, NotifyError, clean_config

try:
    from tests.notify_helpers import deal, message
except ImportError:  # tests/ is not a package
    from notify_helpers import deal, message

PASSWORD = "app-pass-s3cr3t-value"
CONFIG = {
    "host": "smtp.example.com",
    "port": "587",
    "security": "starttls",
    "username": "bot@example.com",
    "password": PASSWORD,
    "from_addr": "Price Watch <bot@example.com>",
    "recipients": "me@example.com, you@example.org",
}


class FakeSMTP:
    """Records what the channel does with smtplib; ``fail`` maps a step name to an exception."""

    instances: list[FakeSMTP] = []
    fail: dict[str, Exception] = {}

    def __init__(self, host, port, timeout=None, context=None):
        self.kind = type(self).__name__
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.calls: list[str] = []
        self.sent = None
        type(self).instances.append(self)
        self._step("connect")

    def _step(self, name):
        self.calls.append(name)
        error = FakeSMTP.fail.get(name)
        if error:
            raise error

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.calls.append("quit")
        return False

    def starttls(self, context=None):
        self.tls_context = context
        self._step("starttls")

    def login(self, user, password):
        self.credentials = (user, password)
        self._step("login")

    def send_message(self, msg, from_addr=None, to_addrs=None):
        self.sent = (msg, from_addr, to_addrs)
        self._step("send")


class FakeSMTPSSL(FakeSMTP):
    pass


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    FakeSMTP.fail = {}
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTPSSL)
    return FakeSMTP


def test_starttls_logs_in_and_sends_a_multipart_message(fake_smtp):
    smtp.send(CONFIG, message())
    (client,) = fake_smtp.instances
    assert client.kind == "FakeSMTP"
    assert (client.host, client.port, client.timeout) == ("smtp.example.com", 587, smtp.TIMEOUT_SECONDS)
    assert client.calls == ["connect", "starttls", "login", "send", "quit"]
    assert isinstance(client.tls_context, ssl.SSLContext)
    assert client.credentials == ("bot@example.com", PASSWORD)
    email, from_addr, to_addrs = client.sent
    assert from_addr == "bot@example.com"
    assert to_addrs == ["me@example.com", "you@example.org"]
    assert email["Subject"] == "🔥 2 deals"
    assert email["From"] == "Price Watch <bot@example.com>"
    assert email["To"] == "me@example.com, you@example.org"
    assert email["Message-ID"].endswith("@example.com>")
    assert email.get_content_type() == "multipart/alternative"
    plain, rich = email.iter_parts()
    assert plain.get_content_type() == "text/plain" and "RTX 5070 — LDLC" in plain.get_content()
    assert rich.get_content_type() == "text/html"
    assert '<a href="https://www.ldlc.com/fiche/PB00660000.html">' in rich.get_content()


def test_ssl_mode_uses_smtp_ssl_without_starttls(fake_smtp):
    smtp.send({**CONFIG, "security": "ssl", "port": "465"}, message())
    (client,) = fake_smtp.instances
    assert client.kind == "FakeSMTPSSL" and client.port == 465
    assert isinstance(client.context, ssl.SSLContext)
    assert "starttls" not in client.calls and "login" in client.calls


def test_plain_mode_without_credentials(fake_smtp):
    smtp.send({**CONFIG, "security": "none", "username": "", "password": ""}, message())
    (client,) = fake_smtp.instances
    assert client.kind == "FakeSMTP"
    assert client.calls == ["connect", "send", "quit"]


def test_hostile_title_cannot_inject_headers(fake_smtp):
    smtp.send(CONFIG, message(deal(), title="deal\r\nBcc: victim@example.com"))
    email = fake_smtp.instances[0].sent[0]
    assert "\n" not in email["Subject"] and email["Bcc"] is None


@pytest.mark.parametrize(
    ("step", "exception", "expected"),
    [
        ("login", smtplib.SMTPAuthenticationError(535, f"bad credentials {PASSWORD}".encode()), "authentication failed"),
        ("starttls", smtplib.SMTPNotSupportedError("STARTTLS extension not supported"), "does not support"),
        ("send", smtplib.SMTPRecipientsRefused({"me@example.com": (550, b"no")}), "refused the recipient"),
        ("send", smtplib.SMTPSenderRefused(550, b"no", "bot@example.com"), "refused the sender"),
        ("send", smtplib.SMTPDataError(554, b"rejected as spam"), "SMTP error 554"),
        ("connect", smtplib.SMTPConnectError(421, b"busy"), "SMTP error 421"),
        ("send", smtplib.SMTPServerDisconnected("Connection unexpectedly closed"), "SMTPServerDisconnected"),
        ("starttls", ssl.SSLError("WRONG_VERSION_NUMBER"), "TLS handshake failed"),
        ("connect", TimeoutError("timed out"), "timed out"),
        ("connect", ConnectionRefusedError(111, "Connection refused"), "could not connect"),
        ("connect", OSError(-2, "Name or service not known smtp.example.com"), "could not connect"),
    ],
)
def test_failures_are_mapped_to_short_safe_messages(fake_smtp, step, exception, expected):
    fake_smtp.fail = {step: exception}
    with pytest.raises(NotifyError) as err:
        smtp.send(CONFIG, message())
    assert expected in str(err.value)
    assert PASSWORD not in str(err.value)
    assert err.value.__suppress_context__


def test_header_injection_through_a_stored_address_is_refused(fake_smtp):
    with pytest.raises(NotifyError, match="invalid e-mail address or subject"):
        smtp.send({**CONFIG, "from_addr": "bot@example.com\nBcc: victim@example.com"}, message())
    assert fake_smtp.instances == []  # refused before any connection


def test_validate_accepts_the_reference_config():
    assert smtp.validate(CONFIG) == {}
    cleaned = clean_config(smtp.TYPE, {**CONFIG, "port": "", "security": ""})
    assert cleaned["port"] == "587" and cleaned["security"] == "starttls"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("host", "http://smtp.example.com"),
        ("host", "smtp example"),
        ("port", "0"),
        ("port", "70000"),
        ("port", "58.7"),
        ("from_addr", "not-an-address"),
        ("from_addr", "a@b.co\nBcc: x@y.zz"),
        ("recipients", "me@example.com, nope"),
        ("recipients", " , "),
        ("recipients", ",".join(f"u{i}@example.com" for i in range(21))),
    ],
)
def test_validate_rejects_bad_values_without_echoing_them(field, value):
    errors = smtp.validate({**CONFIG, field: value})
    assert set(errors) == {field}
    assert value not in errors[field]


def test_password_without_username_is_rejected():
    assert set(smtp.validate({**CONFIG, "username": ""})) == {"username"}
    with pytest.raises(ConfigError):
        clean_config(smtp.TYPE, {**CONFIG, "username": ""})


def test_split_recipients_accepts_commas_semicolons_and_newlines():
    assert smtp.split_recipients("a@b.co; c@d.co\r\n e@f.co,") == ("a@b.co", "c@d.co", "e@f.co")


def test_password_field_is_secret_and_type_key_is_email():
    fields = {f.name: f for f in smtp.TYPE.fields}
    assert smtp.TYPE.key == "email"
    assert fields["password"].secret and not fields["password"].required
    assert [c[0] for c in fields["security"].choices] == ["starttls", "ssl", "none"]
