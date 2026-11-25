"""Free Mobile (France): SMS to your own line through the free notification API."""
from __future__ import annotations

import re

from pricewatch.notify import http, render
from pricewatch.notify.base import ChannelType, Field, Message, NotifyError

API_URL = "https://smsapi.free-mobile.fr/sendmsg"
MAX_SMS = 480  # three SMS segments
_USER_RE = re.compile(r"\d{6,12}")
_ERRORS = {
    400: "Free Mobile: a required parameter is missing",
    402: "Free Mobile: too many SMS sent in too short a time, try again later",
    403: "Free Mobile: the SMS notification option is not enabled on this line, or the user id / key is wrong",
    500: "Free Mobile: server error, try again later",
}


def validate(config: dict) -> dict[str, str]:
    if not _USER_RE.fullmatch(config.get("user", "")):
        return {"user": "must be your Free Mobile identifier (digits only)"}
    return {}


def send(config: dict, message: Message) -> None:
    payload = {
        "user": config["user"],
        "pass": config["api_key"],
        "msg": render.truncate(render.as_text(message), MAX_SMS),
    }
    response = http.request("POST", API_URL, json=payload)
    if http.is_ok(response):
        return
    raise NotifyError(_ERRORS.get(response.status_code, f"Free Mobile: HTTP {response.status_code}"))


TYPE = ChannelType(
    key="freemobile",
    label="Free Mobile SMS",
    description="Text alerts to your own Free Mobile line (France), free of charge.",
    fields=(
        Field("user", "Identifier", required=True, placeholder="12345678", help="The number you log in to the Free Mobile account with."),
        Field("api_key", "API key", required=True, secret=True, help="Shown when you enable the option."),
    ),
    send=send,
    validate=validate,
    setup_help=(
        "In your Free Mobile account (mobile.free.fr), go to Gérer mon compte → Mes options and "
        "enable « Notifications par SMS ». The page then shows your API key. SMS only reach the "
        "line that owns the account."
    ),
    docs_url="https://mobile.free.fr/account/mes-options",
)
