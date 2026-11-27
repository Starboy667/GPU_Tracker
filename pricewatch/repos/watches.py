"""Watches: what to look for, where, when to alert, and who may see them."""
from __future__ import annotations

import json
import sqlite3
from types import MappingProxyType

from pricewatch.models import ProductURL, Watch

_COLUMNS = (
    "owner_id, visibility, share_token, name, query, match, exclude, min_price, max_price, deal_drop_pct, "
    "in_stock_only, notify_restock, sites, site_urls, product_urls, channel_ids, enabled"
)
_COLUMN_COUNT = len(_COLUMNS.split(","))


def _nullable_bool(value) -> bool | None:
    return None if value is None else bool(value)


def _row(row: sqlite3.Row, shared_with: tuple[int, ...] = ()) -> Watch:
    channel_ids = row["channel_ids"]
    return Watch(
        id=row["id"],
        owner_id=row["owner_id"],
        visibility=row["visibility"],
        shared_with=shared_with,
        share_token=row["share_token"],
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
        watch.owner_id,
        watch.visibility,
        watch.share_token,
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


def _shares(conn: sqlite3.Connection) -> dict[int, tuple[int, ...]]:
    shares: dict[int, tuple[int, ...]] = {}
    for row in conn.execute("SELECT watch_id, user_id FROM watch_shares ORDER BY user_id"):
        shares[row["watch_id"]] = (*shares.get(row["watch_id"], ()), row["user_id"])
    return shares


def _one(conn: sqlite3.Connection, row: sqlite3.Row | None) -> Watch | None:
    if row is None:
        return None
    users = conn.execute("SELECT user_id FROM watch_shares WHERE watch_id = ? ORDER BY user_id", (row["id"],))
    return _row(row, tuple(r["user_id"] for r in users))


def list_all(conn: sqlite3.Connection, *, enabled_only: bool = False) -> list[Watch]:
    """Every watch of every owner. Callers that show them to someone filter with ``access.visible``."""
    where = "WHERE enabled = 1" if enabled_only else ""
    shares = _shares(conn)
    rows = conn.execute(f"SELECT * FROM watches {where} ORDER BY name COLLATE NOCASE, id")
    return [_row(r, shares.get(r["id"], ())) for r in rows]


def get(conn: sqlite3.Connection, watch_id: int) -> Watch | None:
    return _one(conn, conn.execute("SELECT * FROM watches WHERE id = ?", (watch_id,)).fetchone())


def get_by_name(conn: sqlite3.Connection, owner_id: int | None, name: str) -> Watch | None:
    """Names are unique per owner, not across the instance."""
    row = conn.execute("SELECT * FROM watches WHERE owner_id IS ? AND name = ?", (owner_id, name)).fetchone()
    return _one(conn, row)


def get_by_share_token(conn: sqlite3.Connection, token: str) -> Watch | None:
    if not token:
        return None
    return _one(conn, conn.execute("SELECT * FROM watches WHERE share_token = ?", (token,)).fetchone())


def _write_shares(conn: sqlite3.Connection, watch_id: int, user_ids: tuple[int, ...]) -> None:
    conn.execute("DELETE FROM watch_shares WHERE watch_id = ?", (watch_id,))
    conn.executemany(
        "INSERT OR IGNORE INTO watch_shares (watch_id, user_id) VALUES (?, ?)",
        [(watch_id, user_id) for user_id in user_ids],
    )


def create(conn: sqlite3.Connection, watch: Watch, now: str) -> Watch:
    placeholders = ", ".join("?" * _COLUMN_COUNT)
    cur = conn.execute(
        f"INSERT INTO watches ({_COLUMNS}, created_at, updated_at) VALUES ({placeholders}, ?, ?)",
        (*_values(watch), now, now),
    )
    _write_shares(conn, cur.lastrowid, watch.shared_with)
    return get(conn, cur.lastrowid)


def update(conn: sqlite3.Connection, watch: Watch, now: str) -> Watch:
    assignments = ", ".join(f"{column.strip()} = ?" for column in _COLUMNS.split(","))
    conn.execute(
        f"UPDATE watches SET {assignments}, updated_at = ? WHERE id = ?",
        (*_values(watch), now, watch.id),
    )
    _write_shares(conn, watch.id, watch.shared_with)
    return get(conn, watch.id)


def set_enabled(conn: sqlite3.Connection, watch_id: int, enabled: bool, now: str) -> None:
    conn.execute("UPDATE watches SET enabled = ?, updated_at = ? WHERE id = ?", (int(enabled), now, watch_id))


def set_share_token(conn: sqlite3.Connection, watch_id: int, token: str | None, now: str) -> None:
    conn.execute("UPDATE watches SET share_token = ?, updated_at = ? WHERE id = ?", (token, now, watch_id))


def mark_checked(conn: sqlite3.Connection, watch_id: int, when: str, errors: tuple[tuple[str, str], ...]) -> None:
    conn.execute(
        "UPDATE watches SET last_checked_at = ?, last_errors = ? WHERE id = ?",
        (when, json.dumps([list(e) for e in errors]), watch_id),
    )


def delete(conn: sqlite3.Connection, watch_id: int) -> None:
    """Delete a watch with everything that only made sense with it, its alert history included."""
    conn.execute("DELETE FROM alerts WHERE watch_id = ?", (watch_id,))
    conn.execute("DELETE FROM watches WHERE id = ?", (watch_id,))


# --------------------------------------------------------------------------- followers
#
# A follower is an account that asked to receive the alerts of a watch it does not own.

def followers(conn: sqlite3.Connection, watch_id: int) -> tuple[int, ...]:
    rows = conn.execute("SELECT user_id FROM watch_followers WHERE watch_id = ? ORDER BY user_id", (watch_id,))
    return tuple(r["user_id"] for r in rows)


def followed_ids(conn: sqlite3.Connection, user_id: int) -> set[int]:
    rows = conn.execute("SELECT watch_id FROM watch_followers WHERE user_id = ?", (user_id,))
    return {r["watch_id"] for r in rows}


def follow(conn: sqlite3.Connection, watch_id: int, user_id: int) -> None:
    conn.execute("INSERT OR IGNORE INTO watch_followers (watch_id, user_id) VALUES (?, ?)", (watch_id, user_id))


def unfollow(conn: sqlite3.Connection, watch_id: int, user_id: int) -> None:
    conn.execute("DELETE FROM watch_followers WHERE watch_id = ? AND user_id = ?", (watch_id, user_id))
