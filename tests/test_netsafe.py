"""SSRF guard: address classification, up-front URL checks, connection-time guard."""
from __future__ import annotations

import socket
import threading

import pytest

from pricewatch import netsafe
from pricewatch.netsafe import UnsafeURLError, check_url, guard, host_matches, is_public_ip

PUBLIC_IP = "93.184.216.34"


def fake_dns(mapping: dict[str, list[str]]):
    """A getaddrinfo stand-in resolving only the given names (IP literals resolve to themselves)."""

    def resolve(host, *args, **kwargs):
        host = host.decode() if isinstance(host, bytes) else host
        if host not in mapping:
            try:
                socket.inet_pton(socket.AF_INET6 if ":" in host else socket.AF_INET, host)
            except OSError:
                raise socket.gaierror(socket.EAI_NONAME, "Name or service not known") from None
            addresses = [host]
        else:
            addresses = mapping[host]
        return [
            (socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0))
            for a in addresses
        ]

    return resolve


@pytest.fixture
def dns(monkeypatch):
    def install(mapping):
        monkeypatch.setattr(netsafe, "_original_getaddrinfo", fake_dns(mapping))

    return install


# --------------------------------------------------------------------------- is_public_ip

@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.1",
        "172.16.5.4",
        "192.168.1.10",
        "127.0.0.1",
        "169.254.169.254",  # cloud metadata
        "100.64.0.1",  # CGNAT
        "0.0.0.0",
        "224.0.0.1",  # multicast
        "::1",
        "fe80::1",
        "fe80::1%eth0",
        "fc00::1",
        "::ffff:10.0.0.1",  # IPv4-mapped private
        "::ffff:127.0.0.1",
        "not-an-ip",
        "",
    ],
)
def test_non_public_addresses(address):
    assert is_public_ip(address) is False


@pytest.mark.parametrize("address", [PUBLIC_IP, "8.8.8.8", "2606:4700:4700::1111", f"::ffff:{PUBLIC_IP}"])
def test_public_addresses(address):
    assert is_public_ip(address) is True


# --------------------------------------------------------------------------- host_matches

@pytest.mark.parametrize(
    "host, expected",
    [
        ("ldlc.com", True),
        ("www.ldlc.com", True),
        ("WWW.LDLC.COM.", True),
        ("evil-ldlc.com", False),
        ("ldlc.com.evil.net", False),
        ("ldlc.co", False),
    ],
)
def test_host_matches(host, expected):
    assert host_matches(host, ("ldlc.com", "ldlc.pro")) is expected


# --------------------------------------------------------------------------- check_url

def test_check_url_returns_the_stripped_url(dns):
    dns({"www.ldlc.com": [PUBLIC_IP]})
    assert check_url("  https://www.ldlc.com/recherche/tv/  ") == "https://www.ldlc.com/recherche/tv/"


@pytest.mark.parametrize("url", ["ftp://example.com/x", "file:///etc/passwd", "javascript:alert(1)", "example.com/x", ""])
def test_check_url_rejects_other_schemes(url):
    with pytest.raises(UnsafeURLError, match="must start with http"):
        check_url(url)


def test_check_url_rejects_none():
    with pytest.raises(UnsafeURLError):
        check_url(None)


def test_check_url_rejects_a_missing_host():
    with pytest.raises(UnsafeURLError, match="no host name"):
        check_url("https:///path")


@pytest.mark.parametrize("url", ["http://example.com:99999/", "http://example.com:port/", "http://[::1/"])
def test_check_url_rejects_malformed_urls(url):
    with pytest.raises(UnsafeURLError, match="not a valid URL"):
        check_url(url)


def test_check_url_credentials(dns):
    dns({"example.com": [PUBLIC_IP]})
    with pytest.raises(UnsafeURLError, match="user name or password"):
        check_url("https://user:secret@example.com/hook")
    with pytest.raises(UnsafeURLError, match="user name or password"):
        check_url("https://user@example.com/hook")
    assert check_url("https://user:secret@example.com/hook", allow_credentials=True)


def test_check_url_allowed_domains(dns):
    dns({"ldlc.com": [PUBLIC_IP], "www.ldlc.com": [PUBLIC_IP], "evil-ldlc.com": [PUBLIC_IP]})
    assert check_url("https://ldlc.com/x", allowed_domains=("ldlc.com",))
    assert check_url("https://www.ldlc.com/x", allowed_domains=("ldlc.com",))
    with pytest.raises(UnsafeURLError, match="must be on ldlc.com"):
        check_url("https://evil-ldlc.com/x", allowed_domains=("ldlc.com",))
    with pytest.raises(UnsafeURLError, match="must be on a.fr or b.fr"):
        check_url("https://www.ldlc.com/x", allowed_domains=("a.fr", "b.fr"))


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://10.1.2.3:8080/admin",
        "http://192.168.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://[::ffff:10.0.0.1]/",
        "http://0.0.0.0/",
    ],
)
def test_check_url_rejects_private_ip_literals(url):
    with pytest.raises(UnsafeURLError, match="private or reserved"):
        check_url(url)


def test_check_url_accepts_a_public_ip_literal():
    assert check_url(f"http://{PUBLIC_IP}/") == f"http://{PUBLIC_IP}/"


def test_check_url_rejects_names_resolving_to_private_addresses(dns):
    dns({"internal.example": ["10.0.0.5"], "mixed.example": [PUBLIC_IP, "192.168.1.1"]})
    with pytest.raises(UnsafeURLError, match="internal.example is unknown or resolves to a private or reserved address"):
        check_url("https://internal.example/")
    with pytest.raises(UnsafeURLError, match="private or reserved"):
        check_url("https://mixed.example/")


def test_check_url_allow_private_skips_resolution(dns):
    dns({})  # nothing resolves: allow_private must not even try
    assert check_url("http://ntfy.ntfy.svc.cluster.local/topic", allow_private=True)
    assert check_url("http://127.0.0.1:8080/", allow_private=True)


def test_check_url_unresolvable_host(dns, monkeypatch):
    dns({})
    with pytest.raises(UnsafeURLError, match="nowhere.example is unknown or resolves to a private"):
        check_url("https://nowhere.example/")
    monkeypatch.setattr(netsafe, "_original_getaddrinfo", lambda *a, **k: [])
    with pytest.raises(UnsafeURLError, match="is unknown or resolves to a private"):
        check_url("https://empty.example/")


def test_unsafe_url_error_is_a_value_error():
    assert issubclass(UnsafeURLError, ValueError)


# --------------------------------------------------------------------------- guard

def test_guard_blocks_private_addresses_only_while_active():
    assert socket.getaddrinfo("127.0.0.1", 80)  # before: untouched
    with guard():
        with pytest.raises(UnsafeURLError, match="127.0.0.1 is unknown or resolves to a private or reserved address"):
            socket.getaddrinfo("127.0.0.1", 80)
        assert socket.getaddrinfo(PUBLIC_IP, 443)
    assert socket.getaddrinfo("127.0.0.1", 80)  # after: untouched again


def test_guard_allow_private():
    with guard(allow_private=True):
        assert socket.getaddrinfo("127.0.0.1", 80)


def test_guard_checks_what_a_name_resolves_to(dns):
    dns({"rebind.example": ["10.0.0.7"], "ok.example": [PUBLIC_IP], "mixed.example": [PUBLIC_IP, "127.0.0.1"]})
    with guard():
        with pytest.raises(UnsafeURLError, match="rebind.example"):
            socket.getaddrinfo("rebind.example", 443)
        with pytest.raises(UnsafeURLError):
            socket.getaddrinfo("mixed.example", 443)
        assert socket.getaddrinfo("ok.example", 443)[0][4][0] == PUBLIC_IP


def test_guard_handles_a_bytes_host(dns):
    dns({"rebind.example": ["10.0.0.7"]})
    with guard(), pytest.raises(UnsafeURLError, match="rebind.example"):
        socket.getaddrinfo(b"rebind.example", 443)


def test_guard_nests_and_restores():
    with guard():
        with guard(allow_private=True):
            assert socket.getaddrinfo("127.0.0.1", 80)
        with pytest.raises(UnsafeURLError):
            socket.getaddrinfo("127.0.0.1", 80)
    assert socket.getaddrinfo("127.0.0.1", 80)


def test_guard_is_released_after_an_exception():
    with pytest.raises(RuntimeError), guard():
        raise RuntimeError("boom")
    assert socket.getaddrinfo("127.0.0.1", 80)


def test_guard_does_not_leak_into_other_threads():
    results = []

    def other_thread():
        try:
            results.append(bool(socket.getaddrinfo("127.0.0.1", 80)))
        except UnsafeURLError:
            results.append(False)

    with guard():
        thread = threading.Thread(target=other_thread)
        thread.start()
        thread.join()
    assert results == [True]


def test_guard_covers_socket_create_connection():
    # smtplib and urllib3 both end up in getaddrinfo; nothing is ever connected here.
    with guard(), pytest.raises(UnsafeURLError):
        socket.create_connection(("127.0.0.1", 9), timeout=0.2)


def test_install_is_idempotent():
    netsafe.install()
    netsafe.install()
    assert socket.getaddrinfo is netsafe._guarded_getaddrinfo


# --------------------------------------------------------------------------- later hardening

@pytest.mark.parametrize("address", [
    "::127.0.0.1",          # IPv4-compatible
    "64:ff9b::7f00:1",      # NAT64 of 127.0.0.1
    "64:ff9b::a00:5",       # NAT64 of 10.0.0.5
    "2002:7f00:1::",        # 6to4 of 127.0.0.1
    "2002:c0a8:101::1",     # 6to4 of 192.168.1.1
    "::ffff:169.254.169.254",
    "::1", "::",
])
def test_ipv6_forms_that_carry_a_private_ipv4_are_not_public(address):
    assert not netsafe.is_public_ip(address)
    with pytest.raises(UnsafeURLError):
        check_url(f"http://[{address}]/")


def test_ipv6_forms_that_carry_a_public_ipv4_stay_public():
    assert netsafe.is_public_ip("64:ff9b::5db8:d822")  # NAT64 of 93.184.216.34
    assert netsafe.is_public_ip("2606:4700:4700::1111")


def test_unknown_and_private_names_get_the_same_answer(dns):
    """Otherwise the error text says which internal names exist."""
    dns({"postgres.internal.example": ["10.43.0.12"]})
    messages = []
    for host in ("postgres.internal.example", "nothing.internal.example"):
        with pytest.raises(UnsafeURLError) as caught:
            check_url(f"http://{host}/")
        messages.append(str(caught.value).replace(host, "HOST"))
    assert messages[0] == messages[1]


def test_resolve_public_returns_the_validated_addresses(dns):
    dns({"shop.example": [PUBLIC_IP, PUBLIC_IP]})
    assert netsafe.resolve_public("shop.example") == (PUBLIC_IP,)


def test_read_capped():
    chunks = [b"a" * 10] * 5
    assert netsafe.read_capped(iter(chunks), limit=50, seconds=5) == b"a" * 50
    with pytest.raises(netsafe.ResponseTooLarge):
        netsafe.read_capped(iter(chunks), limit=49, seconds=5)
    assert netsafe.read_capped(iter(chunks), limit=25, seconds=5, truncate=True) == b"a" * 25

    def endless():
        while True:
            yield b"x"

    with pytest.raises(netsafe.ResponseTooLarge):
        netsafe.read_capped(endless(), limit=1000, seconds=5)
    with pytest.raises(TimeoutError):
        netsafe.read_capped(endless(), limit=10**9, seconds=-1)
