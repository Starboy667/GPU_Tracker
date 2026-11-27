"""One check: scrape every watch, store what was found, detect alerts, hand them to the dispatcher.

Scraping (slow, network) is kept apart from storing (fast, one transaction per watch) so the
database is never locked while a retailer is slow to answer.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta

from pricewatch import access, deals
from pricewatch.config import Config
from pricewatch.db import Database, iso, utcnow
from pricewatch.delivery import Dispatcher
from pricewatch.models import (
    RUN_FAILED,
    RUN_OK,
    RUN_PARTIAL,
    TRIGGER_SCHEDULE,
    Alert,
    Channel,
    Run,
    Settings,
    SiteHealth,
    Watch,
)
from pricewatch.notify.base import KIND_DEAL, KIND_HEALTH
from pricewatch.repos import alerts as alerts_repo
from pricewatch.repos import channels as channels_repo
from pricewatch.repos import listings as listings_repo
from pricewatch.repos import runs as runs_repo
from pricewatch.repos import settings as settings_repo
from pricewatch.repos import users as users_repo
from pricewatch.repos import watches as watches_repo
from pricewatch.scan import PRODUCT_HEALTH_PREFIX, ProductScraper, WatchScan, polite_pause, scan_watch
from pricewatch.scrapers.base import SOURCE_PRODUCT_PAGE, FetchContext, SiteScraper
from pricewatch.security import SecretBox

log = logging.getLogger(__name__)

ALERT_RETENTION = timedelta(days=365)


class Checker:
    def __init__(
        self,
        db: Database,
        box: SecretBox,
        config: Config,
        dispatcher: Dispatcher,
        *,
        scrapers: Mapping[str, SiteScraper] | None = None,
        product_scraper: ProductScraper | None = None,
        pause: Callable[[], None] = polite_pause,
        clock: Callable[[], datetime] = utcnow,
    ):
        if scrapers is None or product_scraper is None:
            from pricewatch import scrapers as scrapers_pkg

            scrapers = scrapers_pkg.SCRAPERS if scrapers is None else scrapers
            product_scraper = product_scraper or scrapers_pkg.scrape_product_page
        self._db = db
        self._box = box
        self._config = config
        self._dispatcher = dispatcher
        self._scrapers = scrapers
        self._product_scraper = product_scraper
        self._pause = pause
        self._clock = clock
        self._lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        return self._lock.locked()

    # ------------------------------------------------------------------ scraping

    def _scan(self, watch: Watch, ctx: FetchContext) -> WatchScan:
        return scan_watch(
            watch, ctx, scrapers=self._scrapers, product_scraper=self._product_scraper, pause=self._pause
        )

    def preview(self, watch: Watch) -> WatchScan:
        """Scrape a watch (possibly unsaved) without storing anything: the form's Test button."""
        with self._db.connect() as conn:
            settings = settings_repo.load(conn)
        return self._scan(watch, FetchContext(dealabs_max_age_days=settings.dealabs_max_age_days))

    def run(self, trigger: str = TRIGGER_SCHEDULE, watch_ids: tuple[int, ...] | None = None) -> Run | None:
        """Check the enabled watches, or exactly ``watch_ids``. Returns None if a check is already running."""
        if not self._lock.acquire(blocking=False):
            return None
        try:
            return self._run(trigger, watch_ids)
        finally:
            self._lock.release()

    def _run(self, trigger: str, watch_ids: tuple[int, ...] | None) -> Run:
        with self._db.connect() as conn:
            run_id = runs_repo.start(conn, trigger, iso(self._clock()))
            settings = settings_repo.load(conn)
            if watch_ids is None:
                todo = watches_repo.list_all(conn, enabled_only=True)
            else:
                todo = [w for w in watches_repo.list_all(conn) if w.id in watch_ids]
        ctx = FetchContext(dealabs_max_age_days=settings.dealabs_max_age_days)
        errors: list[tuple[str, str, str]] = []
        error_watch_ids: list[int | None] = []  # parallel to errors: who may read each one later
        outcomes: dict[str, list[str | None]] = {}
        listing_count = alert_count = watch_count = 0
        status = RUN_OK
        try:
            for index, watch in enumerate(todo):
                if index:
                    self._pause()
                try:
                    scan = self._scan(watch, ctx)
                    with self._db.connect() as conn:
                        if watches_repo.get(conn, watch.id) is None:
                            continue  # deleted while we were scraping
                        alert_count += self._store(conn, watch, scan, settings)
                except Exception as exc:  # one broken watch must not cost the others their check
                    log.exception("watch %s failed in run %s", watch.id, run_id)
                    errors.append((watch.name, "", f"internal error ({type(exc).__name__})"))
                    error_watch_ids.append(watch.id)
                    continue
                watch_count += 1
                listing_count += len(scan.accepted)
                errors += [(watch.name, site, error) for site, error in scan.errors]
                error_watch_ids += [watch.id] * len(scan.errors)
                for key, error in scan.outcomes:
                    outcomes.setdefault(key, []).append(error)
            with self._db.connect() as conn:
                if trigger == TRIGGER_SCHEDULE:
                    # Manual checks of a single watch would otherwise inflate "N checks in a row".
                    alert_count += self._update_health(conn, outcomes, settings)
                self._housekeeping(conn, settings)
            self._dispatcher.dispatch()
            if errors:
                status = RUN_PARTIAL
        except Exception as exc:
            log.exception("check run %s failed", run_id)
            status = RUN_FAILED
            errors.append(("", "", f"internal error ({type(exc).__name__})"))
            error_watch_ids.append(None)
        finally:
            with self._db.connect() as conn:
                runs_repo.finish(
                    conn,
                    run_id,
                    status=status,
                    watches=watch_count,
                    listings=listing_count,
                    alerts=alert_count,
                    errors=tuple(errors),
                    error_watch_ids=tuple(error_watch_ids),
                    now=iso(self._clock()),
                )
                run = runs_repo.latest(conn, 1)[0]
        log.info(
            "check %s: %s, %d watches, %d listings, %d alerts, %d errors",
            run.id, status, watch_count, listing_count, alert_count, len(errors),
        )
        return run

    # ------------------------------------------------------------------ storing and detecting

    def _store(self, conn, watch: Watch, scan: WatchScan, settings: Settings) -> int:
        """Persist one watch's scan and create the alerts it triggers. Returns the number of alerts."""
        moment = self._clock()
        now = iso(moment)
        today = moment.astimezone(self._config.tz).date()
        since = (today - timedelta(days=settings.baseline_days + 1)).isoformat()
        base = deals.baseline(
            listings_repo.daily_lows(conn, watch.id, since=since),
            today,
            settings.baseline_days,
            settings.baseline_min_days,
        )
        drop_pct = settings.deal_drop_pct if watch.deal_drop_pct is None else watch.deal_drop_pct
        threshold = deals.deal_threshold(watch.max_price, base, drop_pct)
        in_stock_only = settings.in_stock_only if watch.in_stock_only is None else watch.in_stock_only
        channels = self._recipients(conn, watch)

        created = 0
        for listing in scan.accepted:
            previous = listings_repo.get(conn, watch.id, listing.url)
            decision = deals.decide(
                price=listing.price,
                in_stock=listing.in_stock,
                threshold=threshold,
                in_stock_only=in_stock_only,
                renotify_drop_pct=settings.renotify_drop_pct,
                notify_restock=watch.notify_restock,
                seen_before=previous is not None,
                previous_in_stock=previous.in_stock if previous else None,
                notified_price=previous.notified_price if previous else None,
            )
            listings_repo.save(conn, watch.id, listing, previous, decision.notified_price, now)
            if decision.alert is None:
                continue
            alert = Alert(
                kind=decision.alert,
                watch_id=watch.id,
                watch_name=watch.name,
                title=listing.title,
                site=listing.site,
                url=listing.url,
                price=listing.price,
                previous_price=previous.notified_price if previous and decision.alert == KIND_DEAL else None,
                baseline=base.price if base else None,
                baseline_days=base.days if base else None,
                target_price=watch.max_price,
                in_stock=listing.in_stock,
            )
            _record_alert(conn, alert, channels, now)
            created += 1

        available = [item.price for item in scan.accepted if deals.counts_for_baseline(item)]
        if available:
            listings_repo.record_daily_low(conn, watch.id, today.isoformat(), min(available))
        seen = tuple(item.url for item in scan.accepted)
        listings_repo.deactivate_missing(conn, watch.id, scan.sources_ok, seen)
        listings_repo.deactivate_urls(
            conn, watch.id, SOURCE_PRODUCT_PAGE, tuple(p.url for p in watch.product_urls)
        )
        listings_repo.deactivate_other_sources(conn, watch.id, (*watch.sites, SOURCE_PRODUCT_PAGE))
        watches_repo.mark_checked(conn, watch.id, now, scan.errors)
        return created

    def _recipients(self, conn, watch: Watch) -> list[Channel]:
        """The channels an alert of this watch goes to: its owner's (those picked on the watch),
        and every channel of the accounts that follow it and may still see it."""
        followers = {
            user_id for user_id in watches_repo.followers(conn, watch.id) if access.may_follow(user_id, watch)
        }
        recipients = []
        for channel in channels_repo.list_all(conn, self._box):
            if not channel.enabled or channel.owner_id is None:
                continue
            if channel.owner_id == watch.owner_id:
                if watch.channel_ids is None or channel.id in watch.channel_ids:
                    recipients.append(channel)
            elif channel.owner_id in followers:
                recipients.append(channel)
        return recipients

    def site_label(self, health_key: str) -> str:
        if health_key.startswith(PRODUCT_HEALTH_PREFIX):
            return f"{health_key.removeprefix(PRODUCT_HEALTH_PREFIX)} product pages"
        scraper = self._scrapers.get(health_key)
        return scraper.label if scraper else health_key

    def _update_health(self, conn, outcomes: Mapping[str, list[str | None]], settings: Settings) -> int:
        """Track consecutive failed runs per site. Alert once when a site looks broken and once
        when it recovers. Returns the number of alerts created."""
        now = iso(self._clock())
        enabled = [c for c in channels_repo.list_all(conn, self._box) if c.enabled]
        admins = users_repo.admin_ids(conn)
        created = 0
        for key, results in outcomes.items():
            # A shop everyone can pick concerns everyone. The host of a product page says what
            # somebody is watching: that only goes to the people who run the instance.
            private_host = key.startswith(PRODUCT_HEALTH_PREFIX)
            channels = [c for c in enabled if not private_host or c.owner_id in admins]
            health = runs_repo.health_get(conn, key)
            failures = [error for error in results if error is not None]
            if len(failures) < len(results):
                if health.alerted:
                    alert = Alert(kind=KIND_HEALTH, site=key, title=f"{self.site_label(key)} is working again")
                    _record_alert(conn, alert, channels, now)
                    created += 1
                runs_repo.health_save(conn, SiteHealth(key, 0, now, health.last_error, health.last_error_at, False))
                continue
            streak = health.consecutive_failures + 1
            alerted = health.alerted
            if settings.health_alert_after > 0 and streak >= settings.health_alert_after and not alerted:
                alert = Alert(
                    kind=KIND_HEALTH,
                    site=key,
                    title=f"{self.site_label(key)} has failed {streak} checks in a row",
                    detail=f"Latest error: {failures[-1]}",
                )
                _record_alert(conn, alert, channels, now)
                created += 1
                alerted = True
            runs_repo.health_save(conn, SiteHealth(key, streak, health.last_ok_at, failures[-1], now, alerted))
        return created

    def _housekeeping(self, conn, settings: Settings) -> None:
        moment = self._clock()
        listings_repo.prune_price_points(conn, iso(moment - timedelta(days=settings.history_days)))
        alerts_repo.prune(conn, iso(moment - ALERT_RETENTION))
        runs_repo.prune(conn)


def _record_alert(conn, alert: Alert, channels: list[Channel], now: str) -> None:
    """Store an alert and queue one delivery per channel subscribed to that kind of event."""
    alert_id = alerts_repo.create(conn, alert, now)
    for channel in channels:
        if alert.kind in channel.events:
            alerts_repo.add_delivery(conn, alert_id, channel.id, now)


