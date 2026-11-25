"""User accounts."""
from __future__ import annotations

import sqlite3

from pricewatch.models import User


def _row(row: sqlite3.Row) -> User:
    return User(
        id=row["id"],
        email=row["email"],
        password_hash=row["password_hash"],
        is_admin=bool(row["is_admin"]),
        session_version=row["session_version"],
        created_at=row["created_at"],
    )


def count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def count_admins(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1").fetchone()[0]


def get(conn: sqlite3.Connection, user_id: int) -> User | None:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _row(row) if row else None


def get_by_email(conn: sqlite3.Connection, email: str) -> User | None:
    row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    return _row(row) if row else None


def list_all(conn: sqlite3.Connection) -> list[User]:
    return [_row(r) for r in conn.execute("SELECT * FROM users ORDER BY email")]


def create(conn: sqlite3.Connection, email: str, password_hash: str, is_admin: bool, now: str) -> User:
    cur = conn.execute(
        "INSERT INTO users (email, password_hash, is_admin, created_at) VALUES (?, ?, ?, ?)",
        (email, password_hash, int(is_admin), now),
    )
    return get(conn, cur.lastrowid)


def set_password(conn: sqlite3.Connection, user_id: int, password_hash: str) -> None:
    """Store a new hash and bump session_version, which signs out every existing session."""
    conn.execute(
        "UPDATE users SET password_hash = ?, session_version = session_version + 1 WHERE id = ?",
        (password_hash, user_id),
    )


def replace_hash(conn: sqlite3.Connection, user_id: int, password_hash: str) -> None:
    """Upgrade the hash parameters of the same password: sessions stay valid."""
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))


def set_admin(conn: sqlite3.Connection, user_id: int, is_admin: bool) -> None:
    conn.execute("UPDATE users SET is_admin = ? WHERE id = ?", (int(is_admin), user_id))


def delete(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
