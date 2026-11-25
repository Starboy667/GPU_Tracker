"""Background thread that runs checks on a schedule and on demand."""
from __future__ import annotations

import logging
import random
import threading
from collections.abc import Callable
from datetime import datetime, timedelta

from pricewatch.checker import Checker
from pricewatch.db import Database, iso, parse_iso, utcnow
from pricewatch.models import TRIGGER_MANUAL, TRIGGER_SCHEDULE
from pricewatch.repos import runs as runs_repo
from pricewatch.repos import settings as settings_repo

log = logging.getLogger(__name__)

FIRST_RUN_DELAY = timedelta(seconds=20)
FALLBACK_INTERVAL = timedelta(minutes=30)
ALL_WATCHES = None


class Scheduler:
    def __init__(
        self,
        checker: Checker,
        db: Database,
        *,
        tick_seconds: float = 2.0,
        clock: Callable[[], datetime] = utcnow,
        jitter: Callable[[], float] = lambda: random.uniform(0.9, 1.1),
    ):
        self._checker = checker
        self._db = db
        self._tick = tick_seconds
        self._clock = clock
        self._jitter = jitter
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._requested: list[tuple[int, ...] | None] = []
        self._thread: threading.Thread | None = None
        self._next_due: datetime | None = None

    # ------------------------------------------------------------------ public API

    def start(self, *, scheduled: bool = True) -> None:
        """Start the loop. With ``scheduled=False`` it only serves checks requested by hand."""
        if self._thread and self._thread.is_alive():
            return
        with self._db.connect() as conn:
            runs_repo.abandon_running(conn, iso(self._clock()))
        self._next_due = self._initial_due() if scheduled else None
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="pricewatch-scheduler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)

    def request_run(self, watch_ids: tuple[int, ...] | None = ALL_WATCHES) -> None:
        """Ask for a check as soon as possible: all enabled watches, or only ``watch_ids``."""
        with self._lock:
            if watch_ids is ALL_WATCHES:
                self._requested = [ALL_WATCHES]
            elif ALL_WATCHES not in self._requested and watch_ids not in self._requested:
                self._requested = [*self._requested, watch_ids]
        self._wake.set()

    @property
    def is_busy(self) -> bool:
        with self._lock:
            queued = bool(self._requested)
        return queued or self._checker.is_running

    @property
    def next_due(self) -> datetime | None:
        return self._next_due

    # ------------------------------------------------------------------ internals

    def _interval(self) -> timedelta:
        with self._db.connect() as conn:
            minutes = settings_repo.load(conn).check_interval_minutes
        return timedelta(minutes=minutes * self._jitter())

    def _initial_due(self) -> datetime:
        """Resume the cadence after a restart instead of scraping again right away."""
        now = self._clock()
        with self._db.connect() as conn:
            last = parse_iso(runs_repo.last_started(conn, TRIGGER_SCHEDULE))
        if last is None:
            return now + FIRST_RUN_DELAY
        return max(last + self._interval(), now + FIRST_RUN_DELAY)

    def _take_request(self) -> tuple[bool, tuple[int, ...] | None]:
        with self._lock:
            if not self._requested:
                return False, None
            head, *rest = self._requested
            self._requested = rest
            return True, head

    def run_pending(self) -> bool:
        """Run one requested or due check, if any. Returns True when something ran."""
        requested, watch_ids = self._take_request()
        if requested:
            self._checker.run(TRIGGER_MANUAL, watch_ids)
            return True
        if self._next_due is not None and self._clock() >= self._next_due:
            # Schedule the next one first: if this run blows up, the loop must not retry at once.
            self._next_due = self._clock() + self._safe_interval()
            self._checker.run(TRIGGER_SCHEDULE)
            return True
        return False

    def _safe_interval(self) -> timedelta:
        try:
            return self._interval()
        except Exception:
            log.exception("could not read the check interval, using %s", FALLBACK_INTERVAL)
            return FALLBACK_INTERVAL

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                ran = self.run_pending()
            except Exception:  # keep the loop alive whatever happens in a run
                log.exception("scheduler iteration failed")
                ran = False
            if not ran:
                self._wake.wait(self._tick)
                self._wake.clear()
