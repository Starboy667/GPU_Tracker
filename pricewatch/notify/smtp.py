"""E-mail over SMTP (the "email" channel type)."""
from __future__ import annotations

import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr

from pricewatch import netsafe
from pricewatch.notify import render
from pricewatch.notify.base import FIELD_NUMBER, FIELD_SELECT, ChannelType, Field, Message, NotifyError

TIMEOUT_SECONDS = 15
SECURITY_STARTTLS = "starttls"
SECURITY_SSL = "ssl"
SECURITY_NONE = "none"
MAX_RECIPIENTS = 20
_ADDRESS_RE = re.compile(r"[^@\s<>,;\"']+@[^@\s<>,;\"']+\.[^@\s<>,;\"']+")
_HOST_RE = re.compile(r"[A-Za-z0-9._\-:\[\]]+")


def split_recipients(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in re.split(r"[,;\n]", value or "") if part.strip())


def _valid_address(value: str) -> bool:
    return bool(_ADDRESS_RE.fullmatch(parseaddr(value)[1])) and "\n" not in value and "\r" not in value


def validate(config: dict) -> dict[str, str]:
    errors: dict[str, str] = {}
    if not _HOST_RE.fullmatch(config.get("host", "")):
        errors["host"] = "must be a host name such as smtp.example.com (no http://)"
    try:
        port = int(config.get("port") or 0)
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        errors["port"] = "must be a port number between 1 and 65535"
    if not _valid_address(config.get("from_addr", "")):
        errors["from_addr"] = "is not a valid e-mail address"
    recipients = split_recipients(config.get("recipients", ""))
    if not recipients or not all(_valid_address(r) for r in recipients):
        errors["recipients"] = "must be one or more e-mail addresses separated by commas"
    elif len(recipients) > MAX_RECIPIENTS:
        errors["recipients"] = f"at most {MAX_RECIPIENTS} recipients"
    if config.get("password") and not config.get("username"):
        errors["username"] = "required when a password is set"
    return errors


def build_email(config: dict, message: Message) -> EmailMessage:
    sender = config["from_addr"]
    email = EmailMessage()
    email["Subject"] = render.clip(message.title, 200)
    email["From"] = sender
    email["To"] = ", ".join(split_recipients(config["recipients"]))
    email["Date"] = formatdate(localtime=False)
    email["Message-ID"] = make_msgid(domain=parseaddr(sender)[1].rpartition("@")[2] or "pricewatch.local")
    email.set_content(render.as_text(message))
    email.add_alternative(
        '<!doctype html><html><body style="font-family:sans-serif;line-height:1.5">'
        f"{render.as_html(message)}</body></html>",
        subtype="html",
    )
    return email


def _connect(config: dict) -> smtplib.SMTP:
    host, port = config["host"], int(config["port"])
    if config.get("security") == SECURITY_SSL:
        return smtplib.SMTP_SSL(host, port, timeout=TIMEOUT_SECONDS, context=ssl.create_default_context())
    return smtplib.SMTP(host, port, timeout=TIMEOUT_SECONDS)


def _deliver(config: dict, email: EmailMessage) -> None:
    with _connect(config) as client:
        if config.get("security", SECURITY_STARTTLS) == SECURITY_STARTTLS:
            client.starttls(context=ssl.create_default_context())
        if config.get("username"):
            client.login(config["username"], config.get("password", ""))
        client.send_message(
            email,
            from_addr=parseaddr(config["from_addr"])[1],
            to_addrs=[parseaddr(r)[1] for r in split_recipients(config["recipients"])],
        )


def send(config: dict, message: Message) -> None:
    try:
        _deliver(config, build_email(config, message))
    except netsafe.UnsafeURLError as exc:
        raise NotifyError(str(exc)) from None
    except smtplib.SMTPAuthenticationError:
        raise NotifyError("SMTP authentication failed (check the user name and password)") from None
    except smtplib.SMTPNotSupportedError:
        raise NotifyError("the server does not support this (STARTTLS or login); check the security mode") from None
    except smtplib.SMTPRecipientsRefused:
        raise NotifyError("the server refused the recipient address") from None
    except smtplib.SMTPSenderRefused:
        raise NotifyError("the server refused the sender address") from None
    except smtplib.SMTPResponseException as exc:
        raise NotifyError(f"SMTP error {exc.smtp_code}") from None
    except smtplib.SMTPException as exc:
        raise NotifyError(f"SMTP conversation failed ({type(exc).__name__})") from None
    except ssl.SSLError:
        raise NotifyError("TLS handshake failed (wrong security mode for this port?)") from None
    except TimeoutError:
        raise NotifyError("timed out") from None
    except OSError:
        raise NotifyError("could not connect to the SMTP server (check the host and port)") from None
    except ValueError:  # header injection attempts, unencodable addresses
        raise NotifyError("invalid e-mail address or subject") from None


TYPE = ChannelType(
    key="email",
    label="E-mail",
    description="Send alerts by e-mail through any SMTP server (Gmail, OVH, Proton Bridge, your own).",
    fields=(
        Field("host", "SMTP server", required=True, placeholder="smtp.example.com", destination=True),
        Field(
            "port", "Port", kind=FIELD_NUMBER, required=True, default="587", destination=True,
            help="587 for STARTTLS, 465 for SSL/TLS.",
        ),
        Field(
            "security",
            "Security",
            kind=FIELD_SELECT,
            required=True,
            default=SECURITY_STARTTLS,
            choices=(
                (SECURITY_STARTTLS, "STARTTLS (port 587)"),
                (SECURITY_SSL, "SSL/TLS (port 465)"),
                (SECURITY_NONE, "None (unencrypted)"),
            ),
            help="With None the password travels in clear text: only use it on a trusted network.",
        ),
        Field("username", "User name", help="Leave blank if the server needs no login."),
        Field("password", "Password", secret=True, help="For Gmail and similar, use an app password."),
        Field("from_addr", "From address", required=True, placeholder="pricewatch@example.com"),
        Field(
            "recipients",
            "Recipients",
            required=True,
            placeholder="me@example.com, you@example.com",
            help="One or more addresses separated by commas.",
        ),
    ),
    send=send,
    validate=validate,
    setup_help=(
        "Use the SMTP settings of your mail provider. Most providers require an app-specific "
        "password rather than your normal one."
    ),
)
