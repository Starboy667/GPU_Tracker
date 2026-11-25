"""Listings seen for each watch, their price history and the daily lows used as baseline."""
from __future__ import annotations

import sqlite3

from pricewatch.models import StoredListing
from pricewatch.scrapers.base import Listing


def _bool(value) -> bool | None:
    return None if value is None else bool(value)


def _int(value: bool | None) -> int | None:
    return None if value is None else int(value)


def _row(row: sqlite3.Row) -> StoredListing:
    return StoredListing(
        id=row["id"],
        watch_id=row["watch_id"],
        url=row["url"],
        source=row["source"],
        site=row["site"],
        title=row["title"],
        price=row["price"],
        in_stock=_bool(row["in_stock"]),
        active=bool(row["active"]),
        first_seen=row["first_seen"],
        last_seen=row["last_seen"],
        notified_price=row["notified_price"],
        notified_at=row["notified_at"],
    )


def get(conn: sqlite3.Connection, watch_id: int, url: str) -> StoredListing | None:
    row = conn.execute("SELECT * FROM listings WHERE watch_id = ? AND url = ?", (watch_id, url)).fetchone()
    return _row(row) if row else None


def for_watch(conn: sqlite3.Connection, watch_id: int, *, active_only: bool = True) -> list[StoredListing]:
    where = "AND active = 1" if active_only else ""
    rows = conn.execute(f"SELECT * FROM listings WHERE watch_id = ? {where} ORDER BY price, site", (watch_id,))
    return [_row(r) for r in rows]


def save(
    conn: sqlite3.Connection,
    watch_id: int,
    listing: Listing,
    previous: StoredListing | None,
    notified_price: float | None,
    now: str,
) -> int:
    """Insert or refresh a listing and record a price point when its price or stock changed."""
    if previous is None:
        cur = conn.execute(
            "INSERT INTO listings (watch_id, url, source, site, title, price, in_stock, active, "
            "first_seen, last_seen, notified_price, notified_at) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
            (watch_id, listing.url, listing.source, listing.site, listing.title, listing.price,
             _int(listing.in_stock), now, now, notified_price, now if notified_price is not None else None),
        )
        listing_id = cur.lastrowid
        changed = True
    else:
        listing_id = previous.id
        notified_at = previous.notified_at if notified_price == previous.notified_price else (
            now if notified_price is not None else None
        )
        conn.execute(
            "UPDATE listings SET source = ?, site = ?, title = ?, price = ?, in_stock = ?, active = 1, "
            "last_seen = ?, notified_price = ?, notified_at = ? WHERE id = ?",
            (listing.source, listing.site, listing.title, listing.price, _int(listing.in_stock),
             now, notified_price, notified_at, listing_id),
        )
        changed = listing.price != previous.price or listing.in_stock != previous.in_stock
    if changed:
        conn.execute(
            "INSERT INTO price_points (listing_id, ts, price, in_stock) VALUES (?, ?, ?, ?)",
            (listing_id, now, listing.price, _int(listing.in_stock)),
        )
    return listing_id


def deactivate_missing(
    conn: sqlite3.Connection, watch_id: int, sources_ok: tuple[str, ...], seen_urls: tuple[str, ...]
) -> None:
    """Hide listings that a successfully scraped source no longer returns. Sources that failed
    this time are left alone: their listings are stale, not gone."""
    if not sources_ok:
        return
    source_marks = ", ".join("?" * len(sources_ok))
    url_clause = f"AND url NOT IN ({', '.join('?' * len(seen_urls))})" if seen_urls else ""
    conn.execute(
        f"UPDATE listings SET active = 0 WHERE watch_id = ? AND source IN ({source_marks}) {url_clause}",
        (watch_id, *sources_ok, *seen_urls),
    )


def deactivate_urls(conn: sqlite3.Connection, watch_id: int, source: str, keep_urls: tuple[str, ...]) -> None:
    """Hide listings of ``source`` whose URL is no longer configured on the watch."""
    url_clause = f"AND url NOT IN ({', '.join('?' * len(keep_urls))})" if keep_urls else ""
    conn.execute(
        f"UPDATE listings SET active = 0 WHERE watch_id = ? AND source = ? {url_clause}",
        (watch_id, source, *keep_urls),
    )


def deactivate_other_sources(conn: sqlite3.Connection, watch_id: int, keep_sources: tuple[str, ...]) -> None:
    """Hide listings that came from a site the watch no longer uses."""
    marks = ", ".join("?" * len(keep_sources))
    conn.execute(
        f"UPDATE listings SET active = 0 WHERE watch_id = ? AND source NOT IN ({marks})",
        (watch_id, *keep_sources),
    )


def price_points(conn: sqlite3.Connection, listing_id: int) -> list[tuple[str, float, bool | None]]:
    rows = conn.execute(
        "SELECT ts, price, in_stock FROM price_points WHERE listing_id = ? ORDER BY ts", (listing_id,)
    )
    return [(r["ts"], r["price"], _bool(r["in_stock"])) for r in rows]


def prune_price_points(conn: sqlite3.Connection, before: str) -> int:
    return conn.execute("DELETE FROM price_points WHERE ts < ?", (before,)).rowcount


def daily_lows(conn: sqlite3.Connection, watch_id: int, *, since: str = "") -> dict[str, float]:
    rows = conn.execute(
        "SELECT day, price FROM daily_lows WHERE watch_id = ? AND day >= ? ORDER BY day", (watch_id, since)
    )
    return {r["day"]: r["price"] for r in rows}


def daily_lows_by_watch(conn: sqlite3.Connection, *, since: str = "") -> dict[int, dict[str, float]]:
    out: dict[int, dict[str, float]] = {}
    for r in conn.execute("SELECT watch_id, day, price FROM daily_lows WHERE day >= ? ORDER BY day", (since,)):
        out.setdefault(r["watch_id"], {})[r["day"]] = r["price"]
    return out


def record_daily_low(conn: sqlite3.Connection, watch_id: int, day: str, price: float) -> None:
    conn.execute(
        "INSERT INTO daily_lows (watch_id, day, price) VALUES (?, ?, ?) "
        "ON CONFLICT (watch_id, day) DO UPDATE SET price = MIN(price, excluded.price)",
        (watch_id, day, price),
    )


def cheapest_by_watch(conn: sqlite3.Connection) -> dict[int, StoredListing]:
    """Cheapest active listing that is not known to be out of stock, per watch."""
    out: dict[int, StoredListing] = {}
    rows = conn.execute(
        "SELECT * FROM listings WHERE active = 1 AND (in_stock IS NULL OR in_stock = 1) ORDER BY price"
    )
    for r in rows:
        out.setdefault(r["watch_id"], _row(r))
    return out


def counts_by_watch(conn: sqlite3.Connection) -> dict[int, int]:
    rows = conn.execute("SELECT watch_id, COUNT(*) AS n FROM listings WHERE active = 1 GROUP BY watch_id")
    return {r["watch_id"]: r["n"] for r in rows}
