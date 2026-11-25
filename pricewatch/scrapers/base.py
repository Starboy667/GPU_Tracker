"""Shared types and HTTP helpers for scrapers.

A scraper is a ``SiteScraper`` whose ``search(ctx, query, url)`` returns the listings of one search
or category page. ``url`` is an optional override already validated against ``SiteScraper.domains``.
Parsing lives in pure ``parse_*(html)`` functions so it can be tested against saved pages.
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import requests

from pricewatch import netsafe

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.5",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
TIMEOUT_SECONDS = 25
# A site that does not even accept the connection is dropping our packets (typical of an address
# block): give up quickly instead of waiting out the full read timeout three times.
CONNECT_TIMEOUT_SECONDS = 8
BLOCKED_STATUSES = (403, 429)
TRANSPORT_FAILURES = frozenset({"ConnectTimeout", "ConnectionError", "DNSError", "SSLError", "CertificateVerifyError"})
MAX_REDIRECTS = 5
MAX_BODY_BYTES = 6 * 1024 * 1024
BODY_DEADLINE_SECONDS = 60
MAX_PRICE_TEXT = 400

KIND_RETAILER = "retailer"
KIND_COMPARATOR = "comparator"
KIND_DEALS = "deals"
SOURCE_PRODUCT_PAGE = "product"


class ScrapeError(RuntimeError):
    """A page could not be fetched or understood. The message is shown in the UI.

    ``site_wide`` means the failure is about the site, not about this page: it does not answer
    at all, or it refuses us (403, 429). Asking it again in the same check is pointless.
    """

    def __init__(self, message: str, *, site_wide: bool = False):
        super().__init__(message)
        self.site_wide = site_wide


@dataclass(frozen=True)
class Listing:
    site: str  # display label: "LDLC", "Dealabs/Amazon", "Idealo (12 offres)", "boulanger.com"
    title: str
    price: float
    url: str
    in_stock: bool | None  # None = unknown
    source: str = ""  # key of the scraper that produced it ("ldlc", ...) or SOURCE_PRODUCT_PAGE


@dataclass
class FetchContext:
    """Per-run HTTP state: one session, one cache, injectable sleep for tests."""

    session: requests.Session = field(default_factory=requests.Session)
    dealabs_max_age_days: int = 3
    sleep: Callable[[float], None] = time.sleep
    cache: dict[str, str] = field(default_factory=dict)
    # Sites (scraper key, or "product:<host>") that proved unreachable or closed to us during
    # this run, with the error: the rest of the run skips them instead of timing out again.
    unreachable: dict[str, str] = field(default_factory=dict)

    def get(self, url: str) -> str:
        """Plain HTTP GET with retries, for the retailers that tolerate it."""
        err, site_wide = "no attempt made", False
        for attempt in range(3):
            try:
                with netsafe.guard():
                    r = self.session.get(
                        url, headers=HEADERS, timeout=(CONNECT_TIMEOUT_SECONDS, TIMEOUT_SECONDS), stream=True
                    )
                    try:
                        if r.status_code == 200:
                            r._content = netsafe.read_capped(
                                r.iter_content(65536), limit=MAX_BODY_BYTES, seconds=BODY_DEADLINE_SECONDS
                            )
                            return _body_text(r)
                    finally:
                        r.close()
                err, site_wide = f"HTTP {r.status_code}", r.status_code in BLOCKED_STATUSES
            except netsafe.ResponseTooLarge as exc:
                raise ScrapeError("page too large") from exc
            except TimeoutError:
                err, site_wide = "page took too long", False
            except requests.ConnectTimeout as exc:
                raise ScrapeError(type(exc).__name__, site_wide=True) from None  # retrying only waits longer
            except (requests.RequestException, netsafe.UnsafeURLError) as exc:
                err = type(exc).__name__ if isinstance(exc, requests.RequestException) else str(exc)
                site_wide = isinstance(exc, requests.ConnectionError)
            if attempt < 2:
                self.sleep(2 + attempt * 3)
        raise ScrapeError(err, site_wide=site_wide)

    def get_cached(self, url: str) -> str:
        """Like ``get`` but fetched once per run (feeds shared by several watches)."""
        if url not in self.cache:
            self.cache[url] = self.get(url)
        return self.cache[url]

    def get_as_browser(self, url: str) -> str:
        """GET impersonating Chrome's TLS/HTTP2 fingerprint, for sites that reject plain clients.

        libcurl resolves names itself, so ``netsafe.guard`` cannot see these requests. Instead
        redirects are followed by hand and every hop is validated, limited to http(s), and
        connected to the exact public address that was validated (no second DNS lookup).
        """
        try:
            from curl_cffi import requests as cr
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise ScrapeError("curl_cffi is not installed") from exc
        err, site_wide = "no attempt made", False
        for attempt in range(2):
            try:
                return self._browser_get(cr, url)
            except netsafe.UnsafeURLError as exc:
                raise ScrapeError(str(exc)) from exc
            except netsafe.ResponseTooLarge as exc:
                raise ScrapeError("page too large") from exc
            except TimeoutError:
                err, site_wide = "page took too long", False
            except ScrapeError as exc:
                err, site_wide = str(exc), exc.site_wide
            except Exception as exc:  # curl_cffi raises its own hierarchy
                err = type(exc).__name__
                site_wide = err in TRANSPORT_FAILURES
                if err == "ConnectTimeout":
                    break  # retrying only waits longer
            if attempt < 1:
                self.sleep(3 + attempt * 5)
        raise ScrapeError(err, site_wide=site_wide)

    def _browser_get(self, cr, url: str) -> str:
        for _ in range(MAX_REDIRECTS + 1):
            r = cr.get(
                url,
                impersonate="chrome",
                timeout=(CONNECT_TIMEOUT_SECONDS, TIMEOUT_SECONDS),
                allow_redirects=False,
                stream=True,
                headers={"Accept-Language": HEADERS["Accept-Language"]},
                curl_options=_pinned_curl_options(url),
            )
            try:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                    url = requests.compat.urljoin(url, r.headers["location"])
                    continue
                if r.status_code != 200:
                    raise ScrapeError(f"HTTP {r.status_code} (blocked?)", site_wide=r.status_code in BLOCKED_STATUSES)
                body = netsafe.read_capped(r.iter_content(), limit=MAX_BODY_BYTES, seconds=BODY_DEADLINE_SECONDS)
                return _decode(body, r.headers.get("content-type", ""))
            finally:
                r.close()
        raise ScrapeError("too many redirects")


def _pinned_curl_options(url: str) -> dict:
    """libcurl options for one validated hop. Raises ``netsafe.UnsafeURLError`` for a bad URL.

    The name is resolved here, checked, and handed to libcurl with CURLOPT_RESOLVE, so the
    address that was validated is the address that gets connected to (no DNS rebinding window).
    """
    from curl_cffi import CurlOpt

    netsafe.check_url(url, allow_private=True)  # shape and scheme; addresses are checked just below
    parts = urlsplit(url)
    host = parts.hostname or ""
    addresses = netsafe.resolve_public(host)
    options: dict = {CurlOpt.PROTOCOLS_STR: "http,https", CurlOpt.REDIR_PROTOCOLS_STR: "http,https"}
    if host not in addresses:  # a literal address needs no pinning
        address = next((a for a in addresses if ":" not in a), addresses[0])
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
        options[CurlOpt.RESOLVE] = [f"{host}:{port}:{address if ':' not in address else f'[{address}]'}"]
    return options


def _decode(body: bytes, content_type: str) -> str:
    """Bytes to text: the declared charset when it works, else UTF-8, never an exception."""
    declared = re.search(r"charset=[\"']?([\w.:-]+)", content_type, re.I)
    for encoding in ([declared.group(1)] if declared else []) + ["utf-8"]:
        try:
            return body.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", errors="replace")


def _body_text(r: requests.Response) -> str:
    """``r.text``, except that a response declaring no charset is read as UTF-8 when it is valid UTF-8.

    requests falls back to ISO-8859-1 for ``text/*`` without a charset, which garbles accents
    (TopAchat serves plain ``text/html``).
    """
    if "charset" not in r.headers.get("content-type", "").lower():
        try:
            return r.content.decode("utf-8")
        except UnicodeDecodeError:
            pass
    return r.text


@dataclass(frozen=True)
class SiteScraper:
    key: str  # "ldlc"
    label: str  # "LDLC"
    kind: str  # KIND_RETAILER | KIND_COMPARATOR | KIND_DEALS
    domains: tuple[str, ...]  # hosts a URL override may point at: ("ldlc.com",)
    description: str  # one line shown next to the checkbox in the UI
    search: Callable[[FetchContext, str, str | None], list[Listing]]
    url_help: str = ""  # what a URL override should be for this site
    group: str = "Shops and comparators"  # heading the site is listed under in the watch form


# --------------------------------------------------------------------------- parsing helpers

def parse_price(text: str | None) -> float | None:
    """Parse French-style prices: '1 079€95', '€ 1.029,00', '1 249,99 €', '549.99'."""
    if not text:
        return None
    # Bounded on purpose: this runs on text taken from pages we do not control.
    t = re.sub(r"\s", "", text[:MAX_PRICE_TEXT])  # \s covers no-break and thin spaces too
    m = re.search(r"(\d[\d.]{0,15})€(\d{2})(?!\d)", t)  # LDLC / Materiel.net: 1079€95 (also "1079€95TTC")
    if m:
        return float(m.group(1).replace(".", "") + "." + m.group(2))
    t = t.replace("€", "")
    m = re.search(r"\d[\d.,]{0,20}", t)
    if not m:
        return None
    num = m.group(0).rstrip(".,")
    if "," in num:  # 1.029,00 -> 1029.00
        num = num.replace(".", "").replace(",", ".")
    elif num.count(".") > 1:
        num = num.replace(".", "")
    try:
        return float(num)
    except ValueError:
        return None


_OUT_OF_STOCK = ("rupture", "épuisé", "indisponible", "non dispo", "sur commande", "précommande")
_IN_STOCK = ("en stock", "stock limité", "disponible", "expédié")


def stock_from_text(text: str | None) -> bool | None:
    t = (text or "").lower()
    if any(w in t for w in _OUT_OF_STOCK):
        return False
    if any(w in t for w in _IN_STOCK) or re.search(r"sous \d+ jours", t):
        return True
    return None


def text_of(el) -> str:
    return el.get_text(" ", strip=True) if el else ""
