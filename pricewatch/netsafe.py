"""Outbound-request safety (SSRF guard).

Every URL this app requests is ultimately typed by a logged-in user: product pages, retailer
overrides, webhook endpoints, self-hosted push servers. Left unchecked, that lets an account reach
whatever the server can reach, including cluster-internal services.

Two layers:

* ``check_url`` validates a URL up front (scheme, host, optional domain allow-list, public address),
  so forms can reject bad input with a clear message.
* ``guard`` is enforced at connection time. While it is active, any hostname resolved through
  Python's ``socket`` module in the current thread must resolve to public addresses only. This covers
  ``requests``, ``smtplib`` and third-party libraries such as Apprise, and it closes the gap between
  "validated" and "connected" (DNS rebinding, redirects to internal hosts).

libcurl (``curl_cffi``) resolves names itself and is not covered by ``guard``; callers using it must
run ``check_url`` on every hop instead.
"""
from __future__ import annotations

import contextlib
import contextvars
import ipaddress
import socket
import threading
import time
from collections.abc import Iterable, Iterator
from urllib.parse import urlsplit

ALLOWED_SCHEMES = ("http", "https")

_public_only: contextvars.ContextVar[bool] = contextvars.ContextVar("pricewatch_public_only", default=False)
_original_getaddrinfo = socket.getaddrinfo
_install_lock = threading.Lock()
_installed = False


class UnsafeURLError(ValueError):
    """The URL is malformed, uses a forbidden scheme or points at a non-public address."""


class ResponseTooLarge(ValueError):
    """The other side sent more than we are willing to hold in memory."""


def read_capped(chunks: Iterable[bytes], *, limit: int, seconds: float, truncate: bool = False) -> bytes:
    """Collect a streamed body, refusing to hold more than ``limit`` bytes or wait past ``seconds``.

    Addresses are chosen by users, so the far end may be hostile: an endless or very slow body
    must cost a bounded amount of memory and time. With ``truncate`` an oversized body is cut
    instead of refused (enough to quote the start of an error page).
    """
    deadline = time.monotonic() + seconds
    parts: list[bytes] = []
    size = 0
    for chunk in chunks:
        size += len(chunk)
        if size > limit:
            if not truncate:
                raise ResponseTooLarge("response too large")
            parts.append(chunk[: len(chunk) - (size - limit)])
            break
        parts.append(chunk)
        if time.monotonic() > deadline:
            raise TimeoutError("response took too long")
    return b"".join(parts)


_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_IPV4_COMPATIBLE = ipaddress.ip_network("::/96")


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """The IPv4 address an IPv6 one stands for (mapped, compatible, NAT64, 6to4, Teredo), if any."""
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip in _NAT64 or (ip in _IPV4_COMPATIBLE and int(ip) > 1):
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip.teredo is not None:
        return ip.teredo[1]
    return None


def is_public_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value.split("%", 1)[0])  # drop an IPv6 zone id
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = _embedded_ipv4(ip)
        if embedded is not None:
            ip = embedded
    return ip.is_global and not ip.is_multicast


def _guarded_getaddrinfo(host, *args, **kwargs):
    results = _original_getaddrinfo(host, *args, **kwargs)
    if _public_only.get():
        for result in results:
            address = result[4][0]
            if not is_public_ip(address):
                raise UnsafeURLError(f"{_display_host(host)} is unknown or resolves to a private or reserved address")
    return results


def _display_host(host) -> str:
    return host.decode("ascii", "replace") if isinstance(host, bytes) else str(host)


def install() -> None:
    """Route ``socket.getaddrinfo`` through the guard. Idempotent; a no-op unless ``guard`` is active."""
    global _installed
    with _install_lock:
        if not _installed:
            socket.getaddrinfo = _guarded_getaddrinfo
            _installed = True


@contextlib.contextmanager
def guard(*, allow_private: bool = False) -> Iterator[None]:
    """Within this block, refuse connections to non-public addresses (unless ``allow_private``)."""
    install()
    token = _public_only.set(not allow_private)
    try:
        yield
    finally:
        _public_only.reset(token)


def host_matches(host: str, domains: tuple[str, ...]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in domains)


def check_url(
    url: str,
    *,
    allow_private: bool = False,
    allowed_domains: tuple[str, ...] | None = None,
    allow_credentials: bool = False,
) -> str:
    """Return the stripped URL if it is safe to request, else raise ``UnsafeURLError``.

    The message is meant to be shown to the user as-is.
    """
    url = (url or "").strip()
    try:
        parts = urlsplit(url)
        port = parts.port  # raises ValueError on a malformed port
    except ValueError as exc:
        raise UnsafeURLError("not a valid URL") from exc
    del port
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise UnsafeURLError("the URL must start with http:// or https://")
    host = parts.hostname
    if not host:
        raise UnsafeURLError("the URL has no host name")
    if (parts.username or parts.password) and not allow_credentials:
        raise UnsafeURLError("the URL must not contain a user name or password")
    if allowed_domains is not None and not host_matches(host, allowed_domains):
        raise UnsafeURLError("the URL must be on " + " or ".join(allowed_domains))
    if not allow_private:
        _require_public(host)
    return url


def resolve_public(host: str) -> tuple[str, ...]:
    """The addresses ``host`` resolves to, all of them public. Raises ``UnsafeURLError`` otherwise.

    One message for "does not exist" and "is private", so the answer cannot be used to find out
    which internal names exist.
    """
    refusal = UnsafeURLError(f"{host} is unknown or resolves to a private or reserved address")
    try:
        infos = _original_getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as exc:
        raise refusal from exc
    addresses = tuple(dict.fromkeys(info[4][0] for info in infos))
    if not addresses or not all(is_public_ip(address) for address in addresses):
        raise refusal
    return addresses


def _require_public(host: str) -> None:
    resolve_public(host)
