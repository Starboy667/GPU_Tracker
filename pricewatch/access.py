"""Who may see and change what. Every route and query that hands out a watch goes through here."""
from __future__ import annotations

from collections.abc import Iterable

from pricewatch.models import VISIBILITY_PRIVATE, VISIBILITY_PUBLIC, VISIBILITY_SHARED, Channel, User, Watch


def owns(user: User, watch: Watch) -> bool:
    """Owners change, pause, delete and share a watch. Nobody else does, administrators included.

    A watch without an owner is rare: the app gives such rows to the first administrator when
    it starts (``web.app.adopt_ownerless``). Until then, or if that could not be done, any
    administrator stands in.
    """
    if watch.owner_id is None:
        return user.is_admin
    return watch.owner_id == user.id


def can_view(user: User, watch: Watch) -> bool:
    if owns(user, watch) or watch.visibility == VISIBILITY_PUBLIC:
        return True
    return watch.visibility == VISIBILITY_SHARED and user.id in watch.shared_with


def visible(user: User, watches: Iterable[Watch]) -> list[Watch]:
    return [watch for watch in watches if can_view(user, watch)]


def link_is_active(watch: Watch) -> bool:
    """Whether the read-only link (no account needed) currently opens this watch."""
    return watch.visibility != VISIBILITY_PRIVATE and bool(watch.share_token)


def may_follow(user_id: int, watch: Watch) -> bool:
    """Whether a non-owner who asked for this watch's alerts still has the right to get them."""
    if watch.owner_id == user_id:
        return False  # owners are notified through their own channel settings
    if watch.visibility == VISIBILITY_PUBLIC:
        return True
    return watch.visibility == VISIBILITY_SHARED and user_id in watch.shared_with


def owns_channel(user: User, channel: Channel) -> bool:
    """Channels hold credentials: only their owner sees or uses them."""
    if channel.owner_id is None:
        return user.is_admin
    return channel.owner_id == user.id
