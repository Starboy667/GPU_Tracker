"""Check runs and per-site scraper health."""
from __future__ import annotations

import json
import sqlite3

from pricewatch.models import RUN_FAILED, RUN_RUNNING, Run, SiteHealth

MAX_ERRORS_STORED = 200


def _run(row: sqlite3.Row) -> Run:
    stored = json.loads(row["errors"])  # [watch name, site, error, watch id]; older rows have no id
    return Run(
        id=row["id"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        trigger=row["trigger"],
        status=row["status"],
        watches=row["watches"],
        listings=row["listings"],
        alerts=row["alerts"],
        errors=tuple((e[0], e[1], e[2]) for e in stored),
        error_watch_ids=tuple(e[3] if len(e) > 3 else None for e in stored),
    )


def start(conn: sqlite3.Connection, trigger: str, now: str) -> int:
    cur = conn.execute(
        "INSERT INTO runs (started_at, trigger, status) VALUES (?, ?, ?)", (now, trigger, RUN_RUNNING)
    )
    return cur.lastrowid


def finish(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: str,
    watches: int,
    listings: int,
    alerts: int,
    errors: tuple[tuple[str, str, str], ...],
    now: str,
    error_watch_ids: tuple[int | None, ...] = (),
) -> None:
    """``error_watch_ids`` runs parallel to ``errors``: the watch each one is about, if any."""
    ids = (*error_watch_ids, *([None] * len(errors)))
    stored = [[*error, ids[index]] for index, error in enumerate(errors[:MAX_ERRORS_STORED])]
    conn.execute(
        "UPDATE runs SET finished_at = ?, status = ?, watches = ?, listings = ?, alerts = ?, errors = ? WHERE id = ?",
        (now, status, watches, listings, alerts, json.dumps(stored), run_id),
    )


def abandon_running(conn: sqlite3.Connection, now: str) -> int:
    """Close runs left 'running' by a process that was stopped mid-check."""
    return conn.execute(
        "UPDATE runs SET status = ?, finished_at = ?, errors = ? WHERE status = ?",
        (RUN_FAILED, now, json.dumps([["", "", "interrupted by a restart"]]), RUN_RUNNING),
    ).rowcount


def latest(conn: sqlite3.Connection, limit: int = 10) -> list[Run]:
    return [_run(r) for r in conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))]


def last_started(conn: sqlite3.Connection, trigger: str | None = None) -> str | None:
    where, params = ("WHERE trigger = ?", (trigger,)) if trigger else ("", ())
    row = conn.execute(f"SELECT MAX(started_at) FROM runs {where}", params).fetchone()
    return row[0]


def prune(conn: sqlite3.Connection, keep: int = 300) -> None:
    conn.execute("DELETE FROM runs WHERE id NOT IN (SELECT id FROM runs ORDER BY id DESC LIMIT ?)", (keep,))


def _health(row: sqlite3.Row) -> SiteHealth:
    return SiteHealth(
        site=row["site"],
        consecutive_failures=row["consecutive_failures"],
        last_ok_at=row["last_ok_at"],
        last_error=row["last_error"],
        last_error_at=row["last_error_at"],
        alerted=bool(row["alerted"]),
    )


def health_all(conn: sqlite3.Connection) -> list[SiteHealth]:
    return [_health(r) for r in conn.execute("SELECT * FROM site_health ORDER BY site")]


def health_get(conn: sqlite3.Connection, site: str) -> SiteHealth:
    row = conn.execute("SELECT * FROM site_health WHERE site = ?", (site,)).fetchone()
    return _health(row) if row else SiteHealth(site, 0, None, None, None, False)


def health_save(conn: sqlite3.Connection, health: SiteHealth) -> None:
    conn.execute(
        "INSERT INTO site_health (site, consecutive_failures, last_ok_at, last_error, last_error_at, alerted) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (site) DO UPDATE SET "
        "consecutive_failures = excluded.consecutive_failures, last_ok_at = excluded.last_ok_at, "
        "last_error = excluded.last_error, last_error_at = excluded.last_error_at, alerted = excluded.alerted",
        (health.site, health.consecutive_failures, health.last_ok_at, health.last_error, health.last_error_at,
         int(health.alerted)),
    )
