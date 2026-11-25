"""How this server reaches the internet: the address the shops see, and whether that is a VPN exit.

Asks a "what is my address" JSON endpoint (set with OUTBOUND_CHECK_URL) and keeps the last answer.
With a VPN sidecar whose kill switch is on, "no answer" means the tunnel is down.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

import requests

from pricewatch import netsafe
from pricewatch.db import iso, utcnow

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 8
MAX_RESPONSE_BYTES = 64 * 1024
MAX_FIELD_LENGTH = 80
FRESH_FOR = timedelta(minutes=5)  # a good answer is reused this long
RETRY_AFTER = timedelta(minutes=1)  # a failure is not retried on every page view


@dataclass(frozen=True)
class OutboundStatus:
    checked_at: str
    ok: bool
    error: str = ""
    ip: str = ""
    location: str = ""  # "Paris, France"
    organization: str = ""
    vpn: bool | None = None  # None: the endpoint does not say whether this is a VPN exit
    vpn_server: str = ""


def _text(payload: dict, *names: str) -> str:
    for name in names:
        value = payload.get(name)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:MAX_FIELD_LENGTH]
    return ""


def parse(payload: object, checked_at: str) -> OutboundStatus:
    """Read the answer of a what-is-my-address service. Understands Mullvad's and ipinfo-like ones."""
    if not isinstance(payload, dict):
        return OutboundStatus(checked_at, ok=False, error="unexpected answer")
    address = _text(payload, "ip", "query", "ip_addr")
    try:
        address = str(ipaddress.ip_address(address))
    except ValueError:
        return OutboundStatus(checked_at, ok=False, error="unexpected answer")
    is_vpn_exit = payload.get("mullvad_exit_ip")
    return OutboundStatus(
        checked_at=checked_at,
        ok=True,
        ip=address,
        location=", ".join(part for part in (_text(payload, "city"), _text(payload, "country")) if part),
        organization=_text(payload, "organization", "org", "asn_org"),
        vpn=is_vpn_exit if isinstance(is_vpn_exit, bool) else None,
        vpn_server=_text(payload, "mullvad_exit_ip_hostname"),
    )


class OutboundMonitor:
    def __init__(self, url: str, *, clock: Callable[[], datetime] = utcnow, fetch: Callable[[str], object] | None = None):
        self.url = url
        self._clock = clock
        self._fetch = fetch or _fetch_json
        self._lock = threading.Lock()
        self._latest: OutboundStatus | None = None
        self._latest_at: datetime | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def current(self) -> OutboundStatus | None:
        """The latest status, checked again when it is stale. None when no check URL is set."""
        if not self.enabled:
            return None
        with self._lock:
            now = self._clock()
            if self._latest is not None and self._latest_at is not None:
                keep = FRESH_FOR if self._latest.ok else RETRY_AFTER
                if now - self._latest_at < keep:
                    return self._latest
            self._latest, self._latest_at = self._check(now), now
            return self._latest

    def _check(self, now: datetime) -> OutboundStatus:
        stamp = iso(now)
        try:
            return parse(self._fetch(self.url), stamp)
        except OutboundError as exc:
            return OutboundStatus(stamp, ok=False, error=str(exc))
        except Exception as exc:  # never let a status widget break the page
            log.exception("outbound check failed unexpectedly")
            return OutboundStatus(stamp, ok=False, error=f"check failed ({type(exc).__name__})")


class OutboundError(RuntimeError):
    """The check could not be completed. The message is shown on the Status page."""


def _fetch_json(url: str) -> object:
    try:
        with netsafe.guard():
            response = requests.get(
                url, timeout=TIMEOUT_SECONDS, stream=True, allow_redirects=False,
                headers={"Accept": "application/json", "User-Agent": "PriceWatch/1"},
            )
            try:
                body = netsafe.read_capped(
                    response.iter_content(8192), limit=MAX_RESPONSE_BYTES, seconds=TIMEOUT_SECONDS
                )
            finally:
                response.close()
    except (requests.Timeout, TimeoutError):
        raise OutboundError("no answer within a few seconds") from None
    except requests.ConnectionError:
        raise OutboundError("cannot connect") from None
    except (requests.RequestException, netsafe.UnsafeURLError, netsafe.ResponseTooLarge) as exc:
        raise OutboundError(f"check failed ({type(exc).__name__})") from None
    if response.status_code != 200:
        raise OutboundError(f"the check service answered HTTP {response.status_code}")
    try:
        return json.loads(body)
    except ValueError:
        raise OutboundError("unexpected answer") from None
