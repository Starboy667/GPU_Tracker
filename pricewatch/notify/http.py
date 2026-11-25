"""The one place notification channels make HTTP calls from.

Requests never follow redirects, always time out, and failures surface as ``NotifyError`` with text
that is safe to store and show: a status code and a short, redacted excerpt of the response body.
Messages of ``requests`` exceptions are never used, because they embed the full URL (and with it
webhook tokens and bot tokens).
"""
from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import parse_qsl, urlsplit

import requests

from pricewatch import netsafe
from pricewatch.notify.base import NotifyError

TIMEOUT_SECONDS = 15
EXCERPT_LENGTH = 200
MAX_RESPONSE_BYTES = 64 * 1024
MIN_SECRET_LENGTH = 6
USER_AGENT = "PriceWatch/1"
REDACTED = "***"


def request(
    method: str,
    url: str,
    *,
    json: object = None,
    data: object = None,
    headers: dict[str, str] | None = None,
    timeout: float = TIMEOUT_SECONDS,
) -> requests.Response:
    """Send one request. Raises ``NotifyError`` on transport failure; the status is not checked."""
    try:
        response = requests.request(
            method,
            url,
            json=json,
            data=data,
            headers={"User-Agent": USER_AGENT, **(headers or {})},
            timeout=timeout,
            allow_redirects=False,
            stream=True,
        )
        try:
            # Only the status and the start of the body are ever used: never hold more than that.
            response._content = netsafe.read_capped(
                response.iter_content(8192), limit=MAX_RESPONSE_BYTES, seconds=timeout, truncate=True
            )
        finally:
            response.close()
        return response
    except netsafe.UnsafeURLError as exc:
        raise NotifyError(str(exc)) from None
    except (requests.Timeout, TimeoutError):
        raise NotifyError("timed out") from None
    except requests.exceptions.SSLError:
        raise NotifyError("TLS handshake failed") from None
    except requests.ConnectionError:
        raise NotifyError("connection failed") from None
    except requests.RequestException as exc:
        raise NotifyError(f"request failed ({type(exc).__name__})") from None
    except ValueError:  # e.g. a header value http.client refuses to encode
        raise NotifyError("invalid request (check the URL and headers)") from None


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Replace every known secret found in ``text``."""
    known = {s for s in secrets if s and len(s) >= MIN_SECRET_LENGTH}
    for secret in sorted(known, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    return text


def excerpt(response: requests.Response, secrets: Iterable[str] = ()) -> str:
    """First characters of the response body, whitespace collapsed, known secrets removed."""
    body = " ".join((response.text or "").split())
    return redact(body, secrets)[:EXCERPT_LENGTH]


def is_ok(response: requests.Response) -> bool:
    return 200 <= response.status_code < 300


def ensure_ok(response: requests.Response, *, secrets: Iterable[str] = ()) -> None:
    """Raise ``NotifyError('HTTP 404: <excerpt>')`` unless the status is 2xx."""
    if is_ok(response):
        return
    detail = excerpt(response, secrets)
    raise NotifyError(f"HTTP {response.status_code}: {detail}" if detail else f"HTTP {response.status_code}")


def url_secrets(url: str) -> tuple[str, ...]:
    """The URL plus the parts of it that are likely tokens (path segments, query values)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return (url,)
    pieces = [segment for segment in parts.path.split("/") if segment]
    pieces += [value for _, value in parse_qsl(parts.query)]
    if parts.password:
        pieces.append(parts.password)
    return (url, *pieces)
