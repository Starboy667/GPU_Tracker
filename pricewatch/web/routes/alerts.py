"""Alert history."""
from __future__ import annotations

from fastapi import APIRouter, Request

from pricewatch import access
from pricewatch.repos import alerts as alerts_repo
from pricewatch.repos import watches as watches_repo
from pricewatch.web import views
from pricewatch.web.deps import current_user, render, services

router = APIRouter()

PAGE_SIZE = 40


@router.get("/alerts")
def list_alerts(request: Request, page: int = 1):
    user = current_user(request)
    svc = services(request)
    page = max(page, 1)
    with svc.db.connect() as conn:
        watches = access.visible(user, watches_repo.list_all(conn))
        watch_ids = [w.id for w in watches]
        health = alerts_repo.HEALTH_ALL if user.is_admin else alerts_repo.HEALTH_SHOPS
        total = alerts_repo.count_visible(conn, watch_ids, health=health)
        alerts = alerts_repo.recent_visible(
            conn, watch_ids, health=health, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE
        )
        # Only through the viewer's own channels: where it went for other people is theirs to know.
        deliveries = alerts_repo.delivery_summary(conn, [a.id for a in alerts], owner_id=user.id)
    days: list[tuple[str, list]] = []
    for alert in alerts:
        day = views.local_day(alert.created_at, svc.config.tz)  # the day as lived here, not in UTC
        if not days or days[-1][0] != day:
            days.append((day, []))
        days[-1][1].append(alert)
    return render(
        request,
        "alerts.html",
        user=user,
        nav="alerts",
        days=days,
        deliveries=deliveries,
        own_watch_ids={w.id for w in watches if access.owns(user, w)},
        total=total,
        page=page,
        pages=max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1),
    )
