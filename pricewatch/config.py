"""Process configuration, read once from environment variables at startup."""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MIN_SECRET_KEY_LENGTH = 32


class ConfigError(RuntimeError):
    """The environment is missing something required. Raised at startup so the pod fails fast."""


@dataclass(frozen=True)
class Config:
    data_dir: Path
    secret_key: str
    base_url: str = ""  # public URL, e.g. https://pricewatch.example.org ("" = unknown)
    cookie_secure: bool = True
    admin_email: str = ""  # first account, created when the user table is empty
    admin_password: str = ""
    timezone: str = "Europe/Paris"
    scheduler_enabled: bool = True
    allow_private_notify_urls: bool = False  # let channels target private/cluster-internal hosts
    host: str = "0.0.0.0"
    port: int = 8000
    session_days: int = 14

    @property
    def db_path(self) -> Path:
        return self.data_dir / "pricewatch.db"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def _flag(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def load_config(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    secret_key = env.get("SECRET_KEY", "").strip()
    if len(secret_key) < MIN_SECRET_KEY_LENGTH:
        raise ConfigError(
            f"SECRET_KEY must be set to at least {MIN_SECRET_KEY_LENGTH} random characters. "
            'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(48))"'
        )
    base_url = env.get("BASE_URL", "").strip().rstrip("/")
    timezone = env.get("APP_TIMEZONE", "").strip() or "Europe/Paris"
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"APP_TIMEZONE={timezone!r} is not a known time zone") from exc
    try:
        port = int(env.get("PORT", "").strip() or 8000)
    except ValueError as exc:
        raise ConfigError("PORT must be a number") from exc
    return Config(
        data_dir=Path(env.get("DATA_DIR", "").strip() or "data"),
        secret_key=secret_key,
        base_url=base_url,
        # Secure cookies unless the app is explicitly served over plain http (local development).
        cookie_secure=_flag(env, "COOKIE_SECURE", not base_url.startswith("http://")),
        admin_email=env.get("ADMIN_EMAIL", "").strip(),
        admin_password=env.get("ADMIN_PASSWORD", ""),
        timezone=timezone,
        scheduler_enabled=_flag(env, "SCHEDULER_ENABLED", True),
        allow_private_notify_urls=_flag(env, "ALLOW_PRIVATE_NOTIFY_URLS", False),
        host=env.get("HOST", "").strip() or "0.0.0.0",
        port=port,
    )
