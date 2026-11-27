"""SQLite access: one short-lived connection per unit of work, schema migrations by user_version."""
from __future__ import annotations

import contextlib
import logging
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

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
    # 2 — watches and channels belong to a user; watches get a visibility and an optional
    # share link. Until now everything was visible to every account, so existing watches become
    # "public" and go, with the channels, to the first administrator.
    #
    # The UNIQUE(name) of watches becomes UNIQUE(owner, name), which SQLite can only do by
    # rebuilding the table. The runner turns foreign keys off for migrations, so dropping the old
    # table does not cascade into listings, daily lows and alerts: they keep pointing at the
    # same ids in the table that takes its name.
    """
    CREATE TABLE watches_new (
        id INTEGER PRIMARY KEY AUTOINCREMENT,  -- never reused: past checks remember watches by id
        owner_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        visibility TEXT NOT NULL DEFAULT 'private',
        share_token TEXT UNIQUE,
        name TEXT NOT NULL COLLATE NOCASE,
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
        last_errors TEXT NOT NULL DEFAULT '[]',
        UNIQUE (owner_id, name)
    );
    INSERT INTO watches_new (
        id, owner_id, visibility, name, query, match, exclude, min_price, max_price, deal_drop_pct,
        in_stock_only, notify_restock, sites, site_urls, product_urls, channel_ids, enabled,
        created_at, updated_at, last_checked_at, last_errors
    )
    SELECT
        id, (SELECT id FROM users ORDER BY is_admin DESC, id LIMIT 1), 'public', name, query, match, exclude,
        min_price, max_price, deal_drop_pct, in_stock_only, notify_restock, sites, site_urls, product_urls,
        channel_ids, enabled, created_at, updated_at, last_checked_at, last_errors
    FROM watches;
    DROP TABLE watches;
    ALTER TABLE watches_new RENAME TO watches;
    CREATE INDEX watches_owner ON watches (owner_id);
    CREATE TABLE watch_shares (
        watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        PRIMARY KEY (watch_id, user_id)
    );
    CREATE TABLE watch_followers (
        watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        PRIMARY KEY (watch_id, user_id)
    );
    ALTER TABLE channels ADD COLUMN owner_id INTEGER REFERENCES users(id) ON DELETE CASCADE;
    UPDATE channels SET owner_id = (SELECT id FROM users ORDER BY is_admin DESC, id LIMIT 1);
    CREATE INDEX channels_owner ON channels (owner_id);
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
            if number > version:
                self._apply(number, script)
                version = number
        return version

    def _apply(self, number: int, script: str) -> bool:
        """Run one migration in one transaction. Returns False when it was already applied."""
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            # Foreign keys off (it cannot be changed inside a transaction): a migration that
            # rebuilds a table must not cascade-delete the rows that reference it.
            conn.execute("PRAGMA foreign_keys = OFF")
            # The write lock is taken first and the version read again under it: two processes
            # starting together (the server and a shell command) must not both apply it.
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("PRAGMA user_version").fetchone()[0] >= number:
                conn.execute("ROLLBACK")
                return False
            for statement in _statements(script):
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {number}")
            conn.execute("COMMIT")
            dangling = conn.execute("PRAGMA foreign_key_check").fetchall()
            if dangling:
                log.warning("migration %d left %d dangling references: %s", number, len(dangling), dangling[:5])
            return True
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")  # a failed migration leaves the schema at the previous version
            raise
        finally:
            conn.close()


def _statements(script: str) -> Iterator[str]:
    """Split a migration script into statements, so they can run inside our own transaction
    (``executescript`` would commit it first)."""
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            yield buffer.strip()
            buffer = ""
    if buffer.strip():
        raise sqlite3.ProgrammingError("migration script ends with an incomplete statement")
