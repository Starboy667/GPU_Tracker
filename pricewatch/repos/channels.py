"""Notification channels. Configurations are encrypted with the app's SecretBox."""
from __future__ import annotations

import json
import sqlite3
from types import MappingProxyType

from pricewatch.models import Channel
from pricewatch.security import SecretBox, SecretBoxError

MAX_ERROR_LENGTH = 500


def _row(row: sqlite3.Row, box: SecretBox) -> Channel:
    try:
        config, readable = box.open(row["config"]), True
    except SecretBoxError:
        config, readable = {}, False
    return Channel(
        id=row["id"],
        owner_id=row["owner_id"],
        name=row["name"],
        type=row["type"],
        config=MappingProxyType(config),
        events=tuple(json.loads(row["events"])),
        enabled=bool(row["enabled"]),
        created_at=row["created_at"],
        last_ok_at=row["last_ok_at"],
        last_error=row["last_error"],
        last_error_at=row["last_error_at"],
        readable=readable,
    )


def list_all(conn: sqlite3.Connection, box: SecretBox) -> list[Channel]:
    """Every channel of every owner: for delivering, never for showing."""
    return [_row(r, box) for r in conn.execute("SELECT * FROM channels ORDER BY name COLLATE NOCASE, id")]


def list_owned(conn: sqlite3.Connection, box: SecretBox, owner_id: int) -> list[Channel]:
    rows = conn.execute("SELECT * FROM channels WHERE owner_id = ? ORDER BY name COLLATE NOCASE, id", (owner_id,))
    return [_row(r, box) for r in rows]


def get(conn: sqlite3.Connection, box: SecretBox, channel_id: int) -> Channel | None:
    row = conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
    return _row(row, box) if row else None


def create(conn: sqlite3.Connection, box: SecretBox, channel: Channel, now: str) -> Channel:
    cur = conn.execute(
        "INSERT INTO channels (owner_id, name, type, config, events, enabled, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (channel.owner_id, channel.name, channel.type, box.seal(channel.config), json.dumps(list(channel.events)),
         int(channel.enabled), now),
    )
    return get(conn, box, cur.lastrowid)


def update(conn: sqlite3.Connection, box: SecretBox, channel: Channel) -> Channel:
    conn.execute(
        "UPDATE channels SET name = ?, config = ?, events = ?, enabled = ? WHERE id = ?",
        (channel.name, box.seal(channel.config), json.dumps(list(channel.events)), int(channel.enabled),
         channel.id),
    )
    return get(conn, box, channel.id)


def set_enabled(conn: sqlite3.Connection, channel_id: int, enabled: bool) -> None:
    conn.execute("UPDATE channels SET enabled = ? WHERE id = ?", (int(enabled), channel_id))


def record_result(conn: sqlite3.Connection, channel_id: int, error: str | None, now: str) -> None:
    """Remember the outcome of the latest delivery attempt (``error`` None = success)."""
    if error is None:
        conn.execute("UPDATE channels SET last_ok_at = ?, last_error = NULL, last_error_at = NULL WHERE id = ?",
                     (now, channel_id))
    else:
        conn.execute("UPDATE channels SET last_error = ?, last_error_at = ? WHERE id = ?",
                     (error[:MAX_ERROR_LENGTH], now, channel_id))


def delete(conn: sqlite3.Connection, channel_id: int) -> None:
    """Delete a channel and take it off the watches that were limited to it."""
    conn.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
    for row in conn.execute("SELECT id, channel_ids FROM watches WHERE channel_ids IS NOT NULL").fetchall():
        selected = json.loads(row["channel_ids"])
        if channel_id in selected:
            remaining = [cid for cid in selected if cid != channel_id]
            conn.execute("UPDATE watches SET channel_ids = ? WHERE id = ?", (json.dumps(remaining), row["id"]))
