"""Parsing helpers and FetchContext (no network: `responses` and a fake curl_cffi)."""
from __future__ import annotations

import socket

import pytest
import requests
import responses
from bs4 import BeautifulSoup

from pricewatch import netsafe
from pricewatch.scrapers import base
from pricewatch.scrapers.base import FetchContext, Listing, ScrapeError, parse_price, stock_from_text, text_of

URL = "https://shop.example/search?q=tv"
PUBLIC_IP = "93.184.216.34"


# --------------------------------------------------------------------------- parse_price

@pytest.mark.parametrize(
    "text, expected",
    [
        ("1 079€95", 1079.95),
        ("1\N{NO-BREAK SPACE}079€95", 1079.95),
        ("1.079€95", 1079.95),
        ("€ 1.029,00", 1029.0),
        ("1 249,99 €", 1249.99),
        ("1\N{NARROW NO-BREAK SPACE}249,99\N{NO-BREAK SPACE}€", 1249.99),
        ("1\N{THIN SPACE}249,99 €", 1249.99),
        ("549.99", 549.99),
        ("549€", 549.0),
        ("263,43€", 263.43),
        ("1 349,99€", 1349.99),
        ("1.299.000", 1299000.0),
        ("1299.", 1299.0),
        ("Prix : 12 € TTC", 12.0),
        ("à partir de 899,90 €", 899.9),
    ],
)
def test_parse_price(text, expected):
    assert parse_price(text) == pytest.approx(expected)


@pytest.mark.parametrize("text", ["", None, "prix sur demande", "€", "..."])
def test_parse_price_without_a_number(text):
    assert parse_price(text) is None


# --------------------------------------------------------------------------- stock_from_text

@pytest.mark.parametrize(
    "text, expected",
    [
        ("En stock", True),
        ("1 seul(e) en stock !", True),
        ("Stock limité", True),
        ("Sous 7 jours", True),
        ("Commandé, prêt à être expédié", True),
        ("Disponible", True),
        ("Rupture", False),
        ("Momentanément indisponible", False),  # contains "disponible": the negative must win
        ("Épuisé", False),
        ("Sur commande", False),
        ("Précommande", False),
        ("Bientôt", None),
        ("", None),
        (None, None),
    ],
)
def test_stock_from_text(text, expected):
    assert stock_from_text(text) is expected


def test_text_of():
    soup = BeautifulSoup("<p> Hello <b>big</b>\n world </p>", "lxml")
    assert text_of(soup.p) == "Hello big world"
    assert text_of(None) == ""


def test_listing_is_immutable_with_an_optional_source():
    listing = Listing("LDLC", "A card", 10.0, "https://x", None)
    assert listing.source == ""
    with pytest.raises(AttributeError):
        listing.price = 5.0


# --------------------------------------------------------------------------- FetchContext.get

@pytest.fixture
def ctx():
    sleeps: list[float] = []
    context = FetchContext(sleep=sleeps.append)
    context.sleeps = sleeps
    return context


@responses.activate
def test_get_returns_the_body_and_sends_browser_headers(ctx):
    responses.get(URL, body="<html>ok</html>", content_type="text/html; charset=UTF-8")
    assert ctx.get(URL) == "<html>ok</html>"
    sent = responses.calls[0].request.headers
    assert "Chrome" in sent["User-Agent"]
    assert sent["Accept-Language"].startswith("fr-FR")
    assert ctx.sleeps == []


@responses.activate
def test_get_retries_after_a_server_error(ctx):
    responses.get(URL, status=500)
    responses.get(URL, body="second time lucky")
    assert ctx.get(URL) == "second time lucky"
    assert len(responses.calls) == 2
    assert ctx.sleeps == [2]


@responses.activate
def test_get_gives_up_after_three_attempts(ctx):
    responses.get(URL, status=503)
    with pytest.raises(ScrapeError, match="HTTP 503"):
        ctx.get(URL)
    assert len(responses.calls) == 3
    assert ctx.sleeps == [2, 5]  # no pointless sleep after the last attempt


@responses.activate
def test_get_reports_network_errors_without_leaking_the_url(ctx):
    responses.get(URL, body=requests.ConnectionError(f"Max retries exceeded with url: {URL}"))
    with pytest.raises(ScrapeError) as excinfo:
        ctx.get(URL)
    assert str(excinfo.value) == "ConnectionError"


@responses.activate
def test_get_reads_utf8_when_no_charset_is_declared(ctx):
    # TopAchat serves "text/html" with no charset; requests would decode it as ISO-8859-1.
    responses.get(URL, body="Edition limitée".encode(), content_type="text/html")
    assert ctx.get(URL) == "Edition limitée"


@responses.activate
def test_get_falls_back_to_requests_decoding_for_non_utf8_bodies(ctx):
    responses.get(URL, body="Edition limitée".encode("latin-1"), content_type="text/html")
    assert ctx.get(URL) == "Edition limitée"


@responses.activate
def test_get_honours_a_declared_charset(ctx):
    responses.get(URL, body="Prix réduit".encode("latin-1"), content_type="text/html; charset=ISO-8859-1")
    assert ctx.get(URL) == "Prix réduit"


def test_get_refuses_private_addresses(ctx):
    # Nothing is mocked here: the guard must stop the request before any connection is attempted.
    with pytest.raises(ScrapeError, match="private or reserved"):
        ctx.get("http://127.0.0.1:9/internal")


@responses.activate
def test_get_cached_fetches_once_per_run(ctx):
    responses.get(URL, body="feed")
    assert ctx.get_cached(URL) == "feed"
    assert ctx.get_cached(URL) == "feed"
    assert len(responses.calls) == 1
    assert ctx.cache == {URL: "feed"}


@responses.activate
def test_get_cached_does_not_cache_failures(ctx):
    responses.get(URL, status=500)
    with pytest.raises(ScrapeError):
        ctx.get_cached(URL)
    assert ctx.cache == {}


def test_contexts_do_not_share_state():
    assert FetchContext().cache is not FetchContext().cache
    assert FetchContext().session is not FetchContext().session


# --------------------------------------------------------------------------- FetchContext.get_as_browser

class FakeResponse:
    def __init__(self, status_code=200, text="", headers=None, content=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.body = content if content is not None else text.encode()
        self.closed = False

    def iter_content(self):
        for start in range(0, len(self.body), 65536):
            yield self.body[start:start + 65536]

    def close(self):
        self.closed = True


class FakeCurl:
    """Stands in for curl_cffi.requests.get: replays queued responses and records the calls."""

    def __init__(self, *queued):
        self.queued = list(queued)
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        item = self.queued.pop(0) if len(self.queued) > 1 else self.queued[0]
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def urls(self):
        return [url for url, _ in self.calls]


@pytest.fixture
def public_dns(monkeypatch):
    def resolve(host, *args, **kwargs):
        try:
            socket.inet_aton(host)
            address = host
        except OSError:
            address = PUBLIC_IP
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))]

    monkeypatch.setattr(netsafe, "_original_getaddrinfo", resolve)


@pytest.fixture
def curl(monkeypatch, public_dns):
    def install(*queued):
        fake = FakeCurl(*queued)
        monkeypatch.setattr("curl_cffi.requests.get", fake)
        return fake

    return install


def redirect(location, status=302):
    return FakeResponse(status, headers={"location": location})


def test_browser_get_impersonates_chrome_and_never_auto_redirects(ctx, curl):
    response = FakeResponse(text="<html>idealo</html>")
    fake = curl(response)
    assert ctx.get_as_browser(URL) == "<html>idealo</html>"
    url, kwargs = fake.calls[0]
    assert url == URL
    assert kwargs["impersonate"] == "chrome"
    assert kwargs["allow_redirects"] is False and kwargs["stream"] is True
    assert kwargs["timeout"] == (base.CONNECT_TIMEOUT_SECONDS, base.TIMEOUT_SECONDS)
    assert kwargs["headers"]["Accept-Language"].startswith("fr-FR")
    assert response.closed


def test_browser_get_connects_to_the_address_it_validated(ctx, curl):
    """No second DNS lookup by libcurl: the checked address is pinned, and only http(s) is allowed."""
    from curl_cffi import CurlOpt

    fake = curl(redirect("http://cdn.shop.example:8080/p"), FakeResponse(text="ok"))
    assert ctx.get_as_browser(URL) == "ok"
    first, second = (kwargs["curl_options"] for _, kwargs in fake.calls)
    assert first[CurlOpt.RESOLVE] == [f"shop.example:443:{PUBLIC_IP}"]
    assert second[CurlOpt.RESOLVE] == [f"cdn.shop.example:8080:{PUBLIC_IP}"]
    assert first[CurlOpt.PROTOCOLS_STR] == "http,https" and first[CurlOpt.REDIR_PROTOCOLS_STR] == "http,https"


def test_browser_get_does_not_pin_a_literal_address(ctx, curl):
    from curl_cffi import CurlOpt

    fake = curl(FakeResponse(text="ok"))
    assert ctx.get_as_browser(f"http://{PUBLIC_IP}/p") == "ok"
    assert CurlOpt.RESOLVE not in fake.calls[0][1]["curl_options"]


def test_browser_get_follows_relative_and_absolute_redirects(ctx, curl):
    fake = curl(redirect("/fr/search?q=tv", 301), redirect("https://www.shop.example/final", 307), FakeResponse(text="done"))
    assert ctx.get_as_browser(URL) == "done"
    assert fake.urls == [URL, "https://shop.example/fr/search?q=tv", "https://www.shop.example/final"]


def test_browser_get_checks_the_first_url(ctx, curl):
    fake = curl(FakeResponse(text="never served"))
    with pytest.raises(ScrapeError, match="private or reserved"):
        ctx.get_as_browser("http://10.0.0.8/product")
    assert fake.calls == []
    assert ctx.sleeps == []  # an unsafe URL is not retried


def test_browser_get_checks_every_redirect_hop(ctx, curl):
    fake = curl(redirect("http://127.0.0.1/"), FakeResponse(text="internal secrets"))
    with pytest.raises(ScrapeError, match="private or reserved"):
        ctx.get_as_browser(URL)
    assert fake.urls == [URL]  # the internal hop was never requested


def test_browser_get_rejects_non_http_redirects(ctx, curl):
    curl(redirect("file:///etc/passwd"), FakeResponse(text="x"))
    with pytest.raises(ScrapeError, match="must start with http"):
        ctx.get_as_browser(URL)


def test_browser_get_accepts_public_hops(ctx, curl):
    fake = curl(redirect("https://cdn.shop.example/p/1"), FakeResponse(text="product"))
    assert ctx.get_as_browser(URL) == "product"
    assert len(fake.calls) == 2


def test_browser_get_decodes_with_the_declared_charset_or_utf8(ctx, curl):
    curl(FakeResponse(content="Prix réduit".encode("latin-1"), headers={"content-type": "text/html; charset=ISO-8859-1"}))
    assert ctx.get_as_browser(URL) == "Prix réduit"
    curl(FakeResponse(content="Prix réduit".encode(), headers={"content-type": "text/html"}))
    assert ctx.get_as_browser(URL) == "Prix réduit"
    curl(FakeResponse(content=b"caf\xe9", headers={"content-type": "text/html; charset=no-such-codec"}))
    assert ctx.get_as_browser(URL) == "caf\ufffd"


def test_browser_get_retries_once_when_blocked(ctx, curl):
    fake = curl(FakeResponse(403))
    with pytest.raises(ScrapeError, match=r"HTTP 403 \(blocked\?\)"):
        ctx.get_as_browser(URL)
    assert len(fake.calls) == 2
    assert ctx.sleeps == [3]


def test_browser_get_recovers_on_the_second_attempt(ctx, curl):
    curl(FakeResponse(429), FakeResponse(text="ok now"))
    assert ctx.get_as_browser(URL) == "ok now"


def test_browser_get_stops_on_redirect_loops(ctx, curl):
    fake = curl(redirect(URL))
    with pytest.raises(ScrapeError, match="too many redirects"):
        ctx.get_as_browser(URL)
    assert len(fake.calls) == 2 * (base.MAX_REDIRECTS + 1)


def test_browser_get_treats_a_redirect_without_location_as_a_failure(ctx, curl):
    curl(FakeResponse(302))
    with pytest.raises(ScrapeError, match="HTTP 302"):
        ctx.get_as_browser(URL)


def test_browser_get_rejects_oversized_pages(ctx, curl, monkeypatch):
    monkeypatch.setattr(base, "MAX_BODY_BYTES", 100_000)
    fake = curl(FakeResponse(content=b"x" * 100_001))
    with pytest.raises(ScrapeError, match="page too large"):
        ctx.get_as_browser(URL)
    assert len(fake.calls) == 1 and ctx.sleeps == []  # not worth a retry


def test_browser_get_gives_up_on_a_page_that_never_ends(ctx, curl, monkeypatch):
    monkeypatch.setattr(base, "BODY_DEADLINE_SECONDS", -1)
    curl(FakeResponse(text="slow"))
    with pytest.raises(ScrapeError, match="page took too long"):
        ctx.get_as_browser(URL)


@responses.activate
def test_get_rejects_oversized_pages(ctx, monkeypatch):
    monkeypatch.setattr(base, "MAX_BODY_BYTES", 1000)
    responses.get(URL, body="x" * 1001)
    with pytest.raises(ScrapeError, match="page too large"):
        ctx.get(URL)
    assert len(responses.calls) == 1


@responses.activate
def test_get_gives_up_on_a_page_that_never_ends(ctx, monkeypatch):
    monkeypatch.setattr(base, "BODY_DEADLINE_SECONDS", -1)
    responses.get(URL, body="slow")
    with pytest.raises(ScrapeError, match="page took too long"):
        ctx.get(URL)


def test_parse_price_is_bounded_on_hostile_input():
    import time

    started = time.monotonic()
    assert base.parse_price("1" * 400_000) is not None
    assert base.parse_price("9" * 200_000 + "€99") is not None
    assert time.monotonic() - started < 1.0
    assert base.parse_price("Prix : 1 079€95 TTC") == 1079.95  # normal prices still parse


def test_browser_get_reports_curl_errors_without_leaking_the_url(ctx, curl):
    curl(RuntimeError(f"curl: (28) Connection timed out for {URL}"))
    with pytest.raises(ScrapeError) as excinfo:
        ctx.get_as_browser(URL)
    assert str(excinfo.value) == "RuntimeError"


# --------------------------------------------------------------------------- sites that refuse or ignore us

@responses.activate
def test_a_site_that_drops_the_connection_fails_at_once_and_for_the_whole_site(ctx):
    responses.get(URL, body=requests.ConnectTimeout("no route"))
    with pytest.raises(ScrapeError) as caught:
        ctx.get(URL)
    assert str(caught.value) == "ConnectTimeout" and caught.value.site_wide
    assert len(responses.calls) == 1 and ctx.sleeps == []  # waiting three times would change nothing


@responses.activate
@pytest.mark.parametrize("status,site_wide", [(403, True), (429, True), (500, False), (404, False)])
def test_refusals_concern_the_whole_site_but_server_errors_do_not(ctx, status, site_wide):
    responses.get(URL, status=status)
    with pytest.raises(ScrapeError) as caught:
        ctx.get(URL)
    assert caught.value.site_wide is site_wide


@responses.activate
def test_connection_errors_are_retried_then_reported_as_site_wide(ctx):
    responses.get(URL, body=requests.ConnectionError("reset"))
    with pytest.raises(ScrapeError) as caught:
        ctx.get(URL)
    assert caught.value.site_wide and len(responses.calls) == 3


@responses.activate
def test_get_uses_a_short_connect_timeout(ctx):
    responses.get(URL, body="ok")
    ctx.get(URL)
    assert responses.calls[0].request.req_kwargs["timeout"] == (base.CONNECT_TIMEOUT_SECONDS, base.TIMEOUT_SECONDS)


def test_browser_get_flags_blocks_and_gives_up_fast_on_connect_timeouts(ctx, curl):
    curl(FakeResponse(403))
    with pytest.raises(ScrapeError) as caught:
        ctx.get_as_browser(URL)
    assert caught.value.site_wide

    class ConnectTimeout(Exception):
        pass

    fake = curl(ConnectTimeout("curl: (28)"))
    ctx.sleeps.clear()
    with pytest.raises(ScrapeError) as caught:
        ctx.get_as_browser(URL)
    assert str(caught.value) == "ConnectTimeout" and caught.value.site_wide
    assert len(fake.calls) == 1 and ctx.sleeps == []

    curl(FakeResponse(500))
    with pytest.raises(ScrapeError) as caught:
        ctx.get_as_browser(URL)
    assert not caught.value.site_wide
