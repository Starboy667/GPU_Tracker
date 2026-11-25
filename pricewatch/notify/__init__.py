"""Notification channels: registry and the single entry point for sending."""
from __future__ import annotations

from pricewatch import netsafe
from pricewatch.notify import (
    apprise_url,
    discord,
    freemobile,
    gotify,
    ntfy,
    pushover,
    slack,
    smtp,
    telegram,
    webhook,
)
from pricewatch.notify.base import ChannelType, Message, NotifyError

CHANNEL_TYPES: dict[str, ChannelType] = {
    channel_type.key: channel_type
    for channel_type in (
        discord.TYPE,
        telegram.TYPE,
        smtp.TYPE,
        webhook.TYPE,
        slack.TYPE,
        ntfy.TYPE,
        gotify.TYPE,
        pushover.TYPE,
        freemobile.TYPE,
        apprise_url.TYPE,
    )
}


def send_message(type_key: str, config: dict, message: Message, *, allow_private: bool = False) -> None:
    """Deliver ``message`` through one configured channel.

    Raises ``NotifyError`` with a message that is safe to store and display. Connections to
    private or reserved addresses are refused unless ``allow_private`` is set.
    """
    channel_type = CHANNEL_TYPES.get(type_key)
    if channel_type is None:
        raise NotifyError("unknown channel type")
    try:
        with netsafe.guard(allow_private=allow_private):
            channel_type.send(config, message)
    except NotifyError:
        raise
    except netsafe.UnsafeURLError as exc:
        raise NotifyError(str(exc)) from None
    except Exception as exc:  # never let a library message (URLs, tokens) reach the UI
        raise NotifyError(f"unexpected error ({type(exc).__name__})") from None


__all__ = ["CHANNEL_TYPES", "send_message"]
