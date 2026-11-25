"""Alerts (what was detected) and deliveries (one row per alert and channel: the outbox)."""
from __future__ import annotations

import sqlite3

from pricewatch.models import DELIVERY_FAILED, DELIVERY_PENDING, Alert, Delivery

MAX_ERROR_LENGTH = 500


def _bool(value) -> bool | None:
    return None if value is None else bool(value)


def _alert(row: sqlite3.Row, prefix: str = "") -> Alert:
    g = lambda name: row[prefix + name]  # noqa: E731
    return Alert(
        id=g("id"),
        kind=g("kind"),
        watch_id=g("watch_id"),
        watch_name=g("watch_name"),
        title=g("title"),
        site=g("site"),
        url=g("url"),
        price=g("price"),
        previous_price=g("previous_price"),
        baseline=g("baseline"),
        baseline_days=g("baseline_days"),
        target_price=g("target_price"),
        in_stock=_bool(g("in_stock")),
        detail=g("detail"),
        created_at=g("created_at"),
    )


def create(conn: sqlite3.Connection, alert: Alert, now: str) -> int:
    cur = conn.execute(
        "INSERT INTO alerts (kind, watch_id, watch_name, title, site, url, price, previous_price, baseline, "
        "baseline_days, target_price, in_stock, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (alert.kind, alert.watch_id, alert.watch_name, alert.title, alert.site, alert.url, alert.price,
         alert.previous_price, alert.baseline, alert.baseline_days, alert.target_price,
         None if alert.in_stock is None else int(alert.in_stock), alert.detail, now),
    )
    return cur.lastrowid


def add_delivery(conn: sqlite3.Connection, alert_id: int, channel_id: int, now: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO deliveries (alert_id, channel_id, status, updated_at) VALUES (?, ?, ?, ?)",
        (alert_id, channel_id, DELIVERY_PENDING, now),
    )


def recent(conn: sqlite3.Connection, *, limit: int = 50, offset: int = 0, watch_id: int | None = None) -> list[Alert]:
    where, params = ("WHERE watch_id = ?", [watch_id]) if watch_id is not None else ("", [])
    rows = conn.execute(
        f"SELECT * FROM alerts {where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?", (*params, limit, offset)
    )
    return [_alert(r) for r in rows]


def count(conn: sqlite3.Connection, *, watch_id: int | None = None) -> int:
    where, params = ("WHERE watch_id = ?", [watch_id]) if watch_id is not None else ("", [])
    return conn.execute(f"SELECT COUNT(*) FROM alerts {where}", params).fetchone()[0]


def delivery_summary(conn: sqlite3.Connection, alert_ids: list[int]) -> dict[int, list[tuple[str, str, str | None]]]:
    """For each alert: (channel name, status, last error) of every delivery."""
    if not alert_ids:
        return {}
    marks = ", ".join("?" * len(alert_ids))
    rows = conn.execute(
        f"SELECT d.alert_id, c.name, d.status, d.last_error FROM deliveries d "
        f"JOIN channels c ON c.id = d.channel_id WHERE d.alert_id IN ({marks}) ORDER BY c.name",
        alert_ids,
    )
    out: dict[int, list[tuple[str, str, str | None]]] = {}
    for r in rows:
        out.setdefault(r["alert_id"], []).append((r["name"], r["status"], r["last_error"]))
    return out


def pending(conn: sqlite3.Connection) -> list[tuple[Delivery, Alert]]:
    """Deliveries still to attempt, oldest alert first, for enabled channels only."""
    rows = conn.execute(
        "SELECT d.id AS d_id, d.alert_id AS d_alert_id, d.channel_id AS d_channel_id, d.status AS d_status, "
        "d.attempts AS d_attempts, d.last_error AS d_last_error, d.updated_at AS d_updated_at, a.* "
        "FROM deliveries d JOIN alerts a ON a.id = d.alert_id JOIN channels c ON c.id = d.channel_id "
        "WHERE d.status = ? AND c.enabled = 1 ORDER BY a.created_at, a.id",
        (DELIVERY_PENDING,),
    )
    out = []
    for r in rows:
        delivery = Delivery(
            id=r["d_id"], alert_id=r["d_alert_id"], channel_id=r["d_channel_id"], status=r["d_status"],
            attempts=r["d_attempts"], last_error=r["d_last_error"], updated_at=r["d_updated_at"],
        )
        out.append((delivery, _alert(r)))
    return out


def mark_delivery(
    conn: sqlite3.Connection, delivery_id: int, status: str, attempts: int, error: str | None, now: str
) -> None:
    conn.execute(
        "UPDATE deliveries SET status = ?, attempts = ?, last_error = ?, updated_at = ? WHERE id = ?",
        (status, attempts, error[:MAX_ERROR_LENGTH] if error else None, now, delivery_id),
    )


def expire_pending(conn: sqlite3.Connection, created_before: str, now: str) -> int:
    """Give up on deliveries of alerts that are too old to be worth sending."""
    return conn.execute(
        "UPDATE deliveries SET status = ?, last_error = 'expired before it could be delivered', updated_at = ? "
        "WHERE status = ? AND alert_id IN (SELECT id FROM alerts WHERE created_at < ?)",
        (DELIVERY_FAILED, now, DELIVERY_PENDING, created_before),
    ).rowcount


def prune(conn: sqlite3.Connection, created_before: str) -> int:
    return conn.execute("DELETE FROM alerts WHERE created_at < ?", (created_before,)).rowcount
