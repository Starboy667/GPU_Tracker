"""Watches: what to look for, where, and when to alert."""
from __future__ import annotations

import json
import sqlite3
from types import MappingProxyType

from pricewatch.models import ProductURL, Watch

_COLUMNS = (
    "name, query, match, exclude, min_price, max_price, deal_drop_pct, in_stock_only, "
    "notify_restock, sites, site_urls, product_urls, channel_ids, enabled"
)


def _nullable_bool(value) -> bool | None:
    return None if value is None else bool(value)


def _row(row: sqlite3.Row) -> Watch:
    channel_ids = row["channel_ids"]
    return Watch(
        id=row["id"],
        name=row["name"],
        query=row["query"],
        match=row["match"],
        exclude=row["exclude"],
        min_price=row["min_price"],
        max_price=row["max_price"],
        deal_drop_pct=row["deal_drop_pct"],
        in_stock_only=_nullable_bool(row["in_stock_only"]),
        notify_restock=bool(row["notify_restock"]),
        sites=tuple(json.loads(row["sites"])),
        site_urls=MappingProxyType(dict(json.loads(row["site_urls"]))),
        product_urls=tuple(ProductURL(p["url"], p.get("selector", "")) for p in json.loads(row["product_urls"])),
        channel_ids=None if channel_ids is None else tuple(json.loads(channel_ids)),
        enabled=bool(row["enabled"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_checked_at=row["last_checked_at"],
        last_errors=tuple((e[0], e[1]) for e in json.loads(row["last_errors"])),
    )


def _values(watch: Watch) -> tuple:
    return (
        watch.name,
        watch.query,
        watch.match,
        watch.exclude,
        watch.min_price,
        watch.max_price,
        watch.deal_drop_pct,
        None if watch.in_stock_only is None else int(watch.in_stock_only),
        int(watch.notify_restock),
        json.dumps(list(watch.sites)),
        json.dumps(dict(watch.site_urls)),
        json.dumps([{"url": p.url, "selector": p.selector} for p in watch.product_urls]),
        None if watch.channel_ids is None else json.dumps(list(watch.channel_ids)),
        int(watch.enabled),
    )


def list_all(conn: sqlite3.Connection, *, enabled_only: bool = False) -> list[Watch]:
    where = "WHERE enabled = 1" if enabled_only else ""
    return [_row(r) for r in conn.execute(f"SELECT * FROM watches {where} ORDER BY name COLLATE NOCASE")]


def get(conn: sqlite3.Connection, watch_id: int) -> Watch | None:
    row = conn.execute("SELECT * FROM watches WHERE id = ?", (watch_id,)).fetchone()
    return _row(row) if row else None


def get_by_name(conn: sqlite3.Connection, name: str) -> Watch | None:
    row = conn.execute("SELECT * FROM watches WHERE name = ?", (name,)).fetchone()
    return _row(row) if row else None


def create(conn: sqlite3.Connection, watch: Watch, now: str) -> Watch:
    placeholders = ", ".join("?" * 14)
    cur = conn.execute(
        f"INSERT INTO watches ({_COLUMNS}, created_at, updated_at) VALUES ({placeholders}, ?, ?)",
        (*_values(watch), now, now),
    )
    return get(conn, cur.lastrowid)


def update(conn: sqlite3.Connection, watch: Watch, now: str) -> Watch:
    assignments = ", ".join(f"{column.strip()} = ?" for column in _COLUMNS.split(","))
    conn.execute(
        f"UPDATE watches SET {assignments}, updated_at = ? WHERE id = ?",
        (*_values(watch), now, watch.id),
    )
    return get(conn, watch.id)


def set_enabled(conn: sqlite3.Connection, watch_id: int, enabled: bool, now: str) -> None:
    conn.execute("UPDATE watches SET enabled = ?, updated_at = ? WHERE id = ?", (int(enabled), now, watch_id))


def mark_checked(conn: sqlite3.Connection, watch_id: int, when: str, errors: tuple[tuple[str, str], ...]) -> None:
    conn.execute(
        "UPDATE watches SET last_checked_at = ?, last_errors = ? WHERE id = ?",
        (when, json.dumps([list(e) for e in errors]), watch_id),
    )


def delete(conn: sqlite3.Connection, watch_id: int) -> None:
    conn.execute("DELETE FROM watches WHERE id = ?", (watch_id,))
