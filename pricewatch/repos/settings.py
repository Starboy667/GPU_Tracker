"""Global settings, stored as one JSON value per key."""
from __future__ import annotations

import dataclasses
import json
import sqlite3

from pricewatch.models import Settings


def load(conn: sqlite3.Connection) -> Settings:
    stored = {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM settings")}
    values = {}
    for f in dataclasses.fields(Settings):
        if f.name not in stored:
            continue
        try:
            value = json.loads(stored[f.name])
        except ValueError:
            continue  # unreadable value: fall back to the default
        default = f.default
        if isinstance(default, bool):
            ok = isinstance(value, bool)
        elif isinstance(default, int):
            ok = isinstance(value, int) and not isinstance(value, bool)
        else:
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
            value = float(value) if ok else value
        if ok:
            values[f.name] = value
    return Settings(**values)


def save(conn: sqlite3.Connection, settings: Settings) -> None:
    conn.executemany(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        [(k, json.dumps(v)) for k, v in dataclasses.asdict(settings).items()],
    )
