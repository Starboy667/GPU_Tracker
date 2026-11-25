"""Slack incoming webhook (the same payload works for Mattermost and Rocket.Chat)."""
from __future__ import annotations

from pricewatch.notify import http, render
from pricewatch.notify.base import FIELD_URL, AlertItem, ChannelType, Field, Message

MAX_TEXT = 35000  # Slack truncates around 40 000 characters


ZERO_WIDTH_SPACE = chr(0x200B)


def escape(text: str) -> str:
    """Neutralise mrkdwn control characters, and mentions such as @channel (Mattermost honours them)."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return text.replace("@", "@" + ZERO_WIDTH_SPACE)


def link(label: str, url: str) -> str:
    target = render.safe_url(url)
    if not target:
        return escape(label)
    for char, code in (("|", "%7C"), ("<", "%3C"), (">", "%3E")):
        target = target.replace(char, code)
    return f"<{target}|{escape(label)}>"


def item_mrkdwn(item: AlertItem) -> str:
    lines = [f"*{link(render.item_headline(item), item.url)}*", *(escape(line) for line in render.item_lines(item))]
    return "\n".join(lines)


def build_text(message: Message) -> str:
    blocks = [f"*{escape(message.title)}*", *(item_mrkdwn(item) for item in message.items)]
    if render.safe_url(message.app_url):
        blocks.append(link(render.APP_LINK_LABEL, message.app_url))
    return render.truncate("\n\n".join(blocks), MAX_TEXT)


def send(config: dict, message: Message) -> None:
    url = config["webhook_url"]
    response = http.request("POST", url, json={"text": build_text(message)})
    http.ensure_ok(response, secrets=http.url_secrets(url))


TYPE = ChannelType(
    key="slack",
    label="Slack",
    description="Post alerts to a Slack channel. Also works with Mattermost and Rocket.Chat incoming webhooks.",
    fields=(
        Field(
            "webhook_url",
            "Webhook URL",
            kind=FIELD_URL,
            required=True,
            secret=True,
            placeholder="https://hooks.slack.com/services/…",
            help=(
                "Any host is accepted, because Mattermost and Rocket.Chat incoming webhooks take the "
                "same payload. Anyone holding this URL can post to the channel."
            ),
        ),
    ),
    send=send,
    setup_help=(
        "In Slack, create an app at api.slack.com/apps, enable Incoming Webhooks, add a webhook to "
        "the channel you want and copy its URL. In Mattermost or Rocket.Chat, create an incoming "
        "webhook in the integrations settings."
    ),
    docs_url="https://api.slack.com/messaging/webhooks",
)
