"""Passwords, CSRF tokens, login throttling and encryption of stored channel credentials."""
from __future__ import annotations

import base64
import hmac
import json
import re
import secrets
import threading
import time
from collections.abc import Callable, Mapping

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 200
MAX_EMAIL_LENGTH = 254
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_hasher = PasswordHasher()
# Verified against when the email is unknown, so both paths take the same time.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


# --------------------------------------------------------------------------- passwords

# Argon2 takes about 64 MiB per operation by design. Several sign-ins at once must queue rather
# than add up past what a small container has.
_argon_slots = threading.BoundedSemaphore(2)


def hash_password(password: str) -> str:
    with _argon_slots:
        return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    """Constant-shape check: an unknown user (``None``) costs as much as a wrong password.

    Only a real mismatch (or an unreadable stored hash) answers False. Anything else, such as
    the system refusing the memory Argon2 needs, is raised: telling someone their password is
    wrong when the server is short of memory would be a lie.
    """
    target = password_hash or _DUMMY_HASH
    try:
        with _argon_slots:
            ok = _hasher.verify(target, password)
    except (VerifyMismatchError, InvalidHashError):
        return False
    return bool(ok) and password_hash is not None


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def password_problem(password: str) -> str | None:
    """Why this password is not acceptable, or None."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"The password must be at least {MIN_PASSWORD_LENGTH} characters long."
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"The password must be at most {MAX_PASSWORD_LENGTH} characters long."
    return None


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def is_valid_email(email: str) -> bool:
    return len(email) <= MAX_EMAIL_LENGTH and bool(_EMAIL_RE.match(email))


# --------------------------------------------------------------------------- CSRF

def new_token() -> str:
    return secrets.token_urlsafe(32)


def tokens_match(expected: str | None, submitted: str | None) -> bool:
    if not expected or not submitted:
        return False
    return hmac.compare_digest(expected.encode(), submitted.encode())


# --------------------------------------------------------------------------- login throttling

class RateLimiter:
    """Counts failures per key in a sliding window. In-memory: the app runs as a single replica."""

    def __init__(
        self,
        max_failures: int = 5,
        window_seconds: float = 900,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 10_000,
    ):
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._clock = clock
        self._max_keys = max_keys
        self._failures: dict[str, tuple[float, ...]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> tuple[float, ...]:
        return tuple(t for t in self._failures.get(key, ()) if now - t < self.window_seconds)

    def retry_after(self, key: str) -> float:
        """Seconds until ``key`` may try again; 0 when it is not blocked."""
        now = self._clock()
        with self._lock:
            recent = self._recent(key, now)
        if len(recent) < self.max_failures:
            return 0.0
        return max(0.0, self.window_seconds - (now - recent[0]))

    def record_failure(self, key: str) -> None:
        now = self._clock()
        with self._lock:
            if len(self._failures) >= self._max_keys:
                self._failures = {k: v for k in self._failures if (v := self._recent(k, now))}
            if len(self._failures) >= self._max_keys:
                self._failures = {}  # under flood: forget rather than grow without bound
            self._failures[key] = (*self._recent(key, now), now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


# --------------------------------------------------------------------------- secrets at rest

class SecretBoxError(ValueError):
    """Stored data could not be decrypted (wrong SECRET_KEY or corrupted)."""


class SecretBox:
    """Encrypts channel configurations (tokens, SMTP passwords) before they hit the database.

    The key is derived from SECRET_KEY, so a copy of the database file alone does not leak them.
    Changing SECRET_KEY makes existing channels unreadable: they must be entered again.
    """

    def __init__(self, secret_key: str):
        derived = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=None, info=b"pricewatch/channel-config/v1"
        ).derive(secret_key.encode())
        self._fernet = Fernet(base64.urlsafe_b64encode(derived))

    def seal(self, data: Mapping[str, object]) -> str:
        return self._fernet.encrypt(json.dumps(dict(data), separators=(",", ":")).encode()).decode()

    def open(self, token: str) -> dict:
        try:
            raw = self._fernet.decrypt(token.encode())
            data = json.loads(raw)
        except (InvalidToken, ValueError) as exc:
            raise SecretBoxError("cannot decrypt stored configuration") from exc
        if not isinstance(data, dict):
            raise SecretBoxError("unexpected stored configuration")
        return data
