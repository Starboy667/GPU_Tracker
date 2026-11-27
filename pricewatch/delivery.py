"""Delivering alerts: an outbox drained one message per channel, with retries."""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from pricewatch.config import Config
from pricewatch.db import Database, iso, utcnow
from pricewatch.models import DELIVERY_FAILED, DELIVERY_PENDING, DELIVERY_SENT, Alert, Channel
from pricewatch.notify.base import KIND_DEAL, KIND_HEALTH, KIND_RESTOCK, KIND_TEST, AlertItem, Message, NotifyError
from pricewatch.repos import alerts as alerts_repo
from pricewatch.repos import channels as channels_repo
from pricewatch.security import SecretBox

log = logging.getLogger(__name__)

MAX_DELIVERY_ATTEMPTS = 5
MAX_ITEMS_PER_MESSAGE = 20
PENDING_MAX_AGE = timedelta(hours=24)
MAX_ERROR_LENGTH = 300

Sender = Callable[..., None]


def alert_to_item(alert: Alert) -> AlertItem:
    return AlertItem(
        kind=alert.kind,
        watch=alert.watch_name,
        title=alert.title,
        site="" if alert.kind == KIND_HEALTH else alert.site,
        url=alert.url,
        price=alert.price,
        previous_price=alert.previous_price,
        baseline=alert.baseline,
        baseline_days=alert.baseline_days,
        target_price=alert.target_price,
        in_stock=alert.in_stock,
        detail=alert.detail,
    )


def message_title(kinds: list[str]) -> str:
    unique = set(kinds)
    count = len(kinds)
    if unique == {KIND_DEAL}:
        return f"🔥 {count} deal{'s' if count > 1 else ''}"
    if unique == {KIND_RESTOCK}:
        return "📦 Back in stock"
    if unique == {KIND_HEALTH}:
        return "⚠️ Price Watch: scraper status"
    return f"Price Watch: {count} alerts"


def sample_message(app_url: str) -> Message:
    """What the "Send test" button delivers: a greeting and a realistic deal alert."""
    return Message(
        title="Price Watch test",
        items=(
            AlertItem(
                kind=KIND_TEST,
                watch="",
                title="Test notification",
                detail="If you can read this, the channel works. Below is what a deal alert looks like.",
            ),
            AlertItem(
                kind=KIND_DEAL,
                watch="Example watch",
                title="Example product (sample alert, not a real offer)",
                site="Example shop",
                url=app_url or "https://example.org/",
                price=919.99,
                baseline=1049.0,
                baseline_days=14,
                in_stock=True,
            ),
        ),
        app_url=app_url,
    )


class Dispatcher:
    def __init__(
        self,
        db: Database,
        box: SecretBox,
        config: Config,
        *,
        send: Sender | None = None,
        clock: Callable[[], datetime] = utcnow,
    ):
        if send is None:
            from pricewatch.notify import send_message as send
        self._db = db
        self._box = box
        self._config = config
        self._send = send
        self._clock = clock

    def dispatch(self) -> int:
        """Send every pending delivery, one message per channel. Returns how many were delivered."""
        moment = self._clock()
        now = iso(moment)
        with self._db.connect() as conn:
            alerts_repo.expire_pending(conn, iso(moment - PENDING_MAX_AGE), now)
            pending = alerts_repo.pending(conn)
            channels = {c.id: c for c in channels_repo.list_all(conn, self._box)}
        by_channel: dict[int, list] = {}
        for delivery, alert in pending:
            by_channel.setdefault(delivery.channel_id, []).append((delivery, alert))

        delivered = 0
        for channel_id, entries in by_channel.items():
            channel = channels.get(channel_id)
            if channel is None:
                continue  # deleted a moment ago: its deliveries went with it
            for start in range(0, len(entries), MAX_ITEMS_PER_MESSAGE):
                batch = entries[start:start + MAX_ITEMS_PER_MESSAGE]
                error = self._deliver(channel, [alert for _, alert in batch])
                with self._db.connect() as conn:
                    for delivery, _ in batch:
                        attempts = delivery.attempts + 1
                        alerts_repo.mark_delivery(
                            conn, delivery.id, _status_after(error, attempts), attempts, error, now
                        )
                    channels_repo.record_result(conn, channel_id, error, now)
                if error is None:
                    delivered += len(batch)
        return delivered

    def send_test(self, channel: Channel) -> str | None:
        """Send a sample message through a channel. Returns None on success, else the error."""
        if not channel.readable:
            return "stored settings cannot be decrypted: enter them again"
        return self._attempt(channel, sample_message(self._config.base_url))

    def _deliver(self, channel: Channel, alerts: list[Alert]) -> str | None:
        """Send one message. Returns None on success, else a message safe to show in the UI."""
        if not channel.readable:
            return "stored settings cannot be decrypted: open the channel and enter them again"
        message = Message(
            title=message_title([a.kind for a in alerts]),
            items=tuple(alert_to_item(a) for a in alerts),
            app_url=self._config.base_url,
        )
        return self._attempt(channel, message)

    def _attempt(self, channel: Channel, message: Message) -> str | None:
        try:
            self._send(
                channel.type,
                dict(channel.config),
                message,
                allow_private=self._config.allow_private_notify_urls,
            )
        except NotifyError as exc:
            return " ".join(str(exc).split())[:MAX_ERROR_LENGTH] or "delivery failed"
        except Exception as exc:  # never let one channel break the others
            log.exception("channel %s (%s) crashed", channel.id, channel.type)
            return f"unexpected error ({type(exc).__name__})"
        return None


def _status_after(error: str | None, attempts: int) -> str:
    if error is None:
        return DELIVERY_SENT
    return DELIVERY_FAILED if attempts >= MAX_DELIVERY_ATTEMPTS else DELIVERY_PENDING
