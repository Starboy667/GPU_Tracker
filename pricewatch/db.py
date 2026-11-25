"""SQLite access: one short-lived connection per unit of work, schema migrations by user_version."""
from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

MIGRATIONS: tuple[str, ...] = (
    # 1 — initial schema
    """
    CREATE TABLE users (
        id INTEGER PRIMARY KEY,
        email TEXT NOT NULL UNIQUE COLLATE NOCASE,
        password_hash TEXT NOT NULL,
        is_admin INTEGER NOT NULL DEFAULT 0,
        session_version INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );
    CREATE TABLE settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE watches (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE COLLATE NOCASE,
        query TEXT NOT NULL,
        match TEXT NOT NULL,
        exclude TEXT NOT NULL DEFAULT '',
        min_price REAL,
        max_price REAL,
        deal_drop_pct REAL,
        in_stock_only INTEGER,
        notify_restock INTEGER NOT NULL DEFAULT 0,
        sites TEXT NOT NULL DEFAULT '[]',
        site_urls TEXT NOT NULL DEFAULT '{}',
        product_urls TEXT NOT NULL DEFAULT '[]',
        channel_ids TEXT,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        last_checked_at TEXT,
        last_errors TEXT NOT NULL DEFAULT '[]'
    );
    CREATE TABLE listings (
        id INTEGER PRIMARY KEY,
        watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
        url TEXT NOT NULL,
        source TEXT NOT NULL,
        site TEXT NOT NULL,
        title TEXT NOT NULL,
        price REAL NOT NULL,
        in_stock INTEGER,
        active INTEGER NOT NULL DEFAULT 1,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        notified_price REAL,
        notified_at TEXT,
        UNIQUE (watch_id, url)
    );
    CREATE TABLE price_points (
        id INTEGER PRIMARY KEY,
        listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
        ts TEXT NOT NULL,
        price REAL NOT NULL,
        in_stock INTEGER
    );
    CREATE INDEX price_points_listing ON price_points (listing_id, ts);
    CREATE INDEX price_points_ts ON price_points (ts);
    CREATE TABLE daily_lows (
        watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
        day TEXT NOT NULL,
        price REAL NOT NULL,
        PRIMARY KEY (watch_id, day)
    );
    CREATE TABLE channels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,  -- watches refer to channels by id: never reuse one
        name TEXT NOT NULL,
        type TEXT NOT NULL,
        config TEXT NOT NULL,
        events TEXT NOT NULL DEFAULT '["deal","restock","health"]',
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        last_ok_at TEXT,
        last_error TEXT,
        last_error_at TEXT
    );
    CREATE TABLE alerts (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,
        watch_id INTEGER REFERENCES watches(id) ON DELETE SET NULL,
        watch_name TEXT NOT NULL DEFAULT '',
        title TEXT NOT NULL DEFAULT '',
        site TEXT NOT NULL DEFAULT '',
        url TEXT NOT NULL DEFAULT '',
        price REAL,
        previous_price REAL,
        baseline REAL,
        baseline_days INTEGER,
        target_price REAL,
        in_stock INTEGER,
        detail TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    );
    CREATE INDEX alerts_created ON alerts (created_at);
    CREATE TABLE deliveries (
        id INTEGER PRIMARY KEY,
        alert_id INTEGER NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
        channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
        status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        updated_at TEXT NOT NULL,
        UNIQUE (alert_id, channel_id)
    );
    CREATE INDEX deliveries_status ON deliveries (status);
    CREATE TABLE runs (
        id INTEGER PRIMARY KEY,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        trigger TEXT NOT NULL,
        status TEXT NOT NULL,
        watches INTEGER NOT NULL DEFAULT 0,
        listings INTEGER NOT NULL DEFAULT 0,
        alerts INTEGER NOT NULL DEFAULT 0,
        errors TEXT NOT NULL DEFAULT '[]'
    );
    CREATE TABLE site_health (
        site TEXT PRIMARY KEY,
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        last_ok_at TEXT,
        last_error TEXT,
        last_error_at TEXT,
        alerted INTEGER NOT NULL DEFAULT 0
    );
    """,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    """Canonical timestamp stored in the database: UTC, second precision, sortable as text."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    @contextlib.contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Open a connection; commit on success, roll back on error, always close."""
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def migrate(self) -> int:
        """Apply pending migrations. Returns the schema version afterwards."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        for number, script in enumerate(MIGRATIONS, start=1):
            if number <= version:
                continue
            conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
            try:
                # executescript runs outside implicit transactions; wrap it so a failing
                # migration leaves the schema at the previous version.
                conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;")
            except sqlite3.Error:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()
            version = number
        return version
