import pytest

from pricewatch import security
from pricewatch.config import ConfigError, load_config


def test_password_hash_and_verify():
    hashed = security.hash_password("correct horse battery")
    assert hashed.startswith("$argon2") and "correct horse" not in hashed
    assert security.verify_password(hashed, "correct horse battery")
    assert not security.verify_password(hashed, "wrong")
    assert not security.verify_password(None, "anything")  # unknown user
    assert not security.verify_password("not-a-hash", "anything")
    assert not security.needs_rehash(hashed)


def test_password_rules():
    assert security.password_problem("123456789") is not None
    assert security.password_problem("1234567890") is None
    assert security.password_problem("x" * 201) is not None


def test_emails():
    assert security.normalize_email("  Bob@Example.ORG ") == "bob@example.org"
    assert security.is_valid_email("bob@example.org")
    for bad in ("", "bob", "bob@", "bob@host", "a b@example.org", "x" * 250 + "@example.org"):
        assert not security.is_valid_email(bad), bad


def test_tokens():
    token = security.new_token()
    assert len(token) >= 40 and token != security.new_token()
    assert security.tokens_match(token, token)
    assert not security.tokens_match(token, token + "x")
    assert not security.tokens_match(None, token) and not security.tokens_match(token, "")


def test_rate_limiter_blocks_then_recovers():
    now = [0.0]
    limiter = security.RateLimiter(max_failures=3, window_seconds=60, clock=lambda: now[0])
    for _ in range(2):
        limiter.record_failure("ip:1")
    assert limiter.retry_after("ip:1") == 0
    limiter.record_failure("ip:1")
    assert limiter.retry_after("ip:1") == 60 and limiter.retry_after("ip:2") == 0
    now[0] = 45
    assert limiter.retry_after("ip:1") == 15
    now[0] = 61
    assert limiter.retry_after("ip:1") == 0
    for _ in range(3):
        limiter.record_failure("ip:1")
    limiter.reset("ip:1")
    assert limiter.retry_after("ip:1") == 0


def test_rate_limiter_memory_is_bounded():
    now = [0.0]
    limiter = security.RateLimiter(max_failures=3, window_seconds=60, clock=lambda: now[0], max_keys=5)
    for i in range(5):
        limiter.record_failure(f"k{i}")
    now[0] = 100  # everything above has expired: it is pruned
    limiter.record_failure("fresh")
    assert set(limiter._failures) == {"fresh"}
    for i in range(10):  # a flood of live keys never grows past the cap
        limiter.record_failure(f"flood{i}")
    assert len(limiter._failures) <= 5


def test_secret_box():
    box = security.SecretBox("k" * 40)
    sealed = box.seal({"token": "s3cret", "n": 1})
    assert "s3cret" not in sealed and box.open(sealed) == {"token": "s3cret", "n": 1}
    assert box.seal({"a": 1}) != box.seal({"a": 1})  # random IV
    with pytest.raises(security.SecretBoxError):
        security.SecretBox("another key " * 4).open(sealed)
    with pytest.raises(security.SecretBoxError):
        box.open("garbage")
    with pytest.raises(security.SecretBoxError):
        box.open(box._fernet.encrypt(b"[1, 2]").decode())


# ------------------------------------------------------------------ config

KEY = "k" * 40


def test_config_defaults():
    config = load_config({"SECRET_KEY": KEY})
    assert str(config.data_dir) == "data" and config.db_path.name == "pricewatch.db"
    assert config.cookie_secure and config.scheduler_enabled and not config.allow_private_notify_urls
    assert (config.host, config.port, config.timezone, config.base_url) == ("0.0.0.0", 8000, "Europe/Paris", "")
    assert config.tz.key == "Europe/Paris"


def test_config_from_environment():
    config = load_config({
        "SECRET_KEY": KEY, "DATA_DIR": "/data", "BASE_URL": "https://pw.example.org/", "PORT": "9000",
        "HOST": "127.0.0.1", "APP_TIMEZONE": "UTC", "SCHEDULER_ENABLED": "false", "ALLOW_PRIVATE_NOTIFY_URLS": "1",
        "ADMIN_EMAIL": " me@example.org ", "ADMIN_PASSWORD": " keep spaces ",
    })
    assert config.base_url == "https://pw.example.org" and config.cookie_secure
    assert (config.port, config.host, config.timezone) == (9000, "127.0.0.1", "UTC")
    assert not config.scheduler_enabled and config.allow_private_notify_urls
    assert config.admin_email == "me@example.org" and config.admin_password == " keep spaces "


def test_config_plain_http_turns_off_secure_cookies_unless_forced():
    assert not load_config({"SECRET_KEY": KEY, "BASE_URL": "http://localhost:8000"}).cookie_secure
    assert load_config({"SECRET_KEY": KEY, "BASE_URL": "http://localhost:8000", "COOKIE_SECURE": "true"}).cookie_secure


@pytest.mark.parametrize("env", [
    {}, {"SECRET_KEY": "short"}, {"SECRET_KEY": KEY, "PORT": "http"}, {"SECRET_KEY": KEY, "APP_TIMEZONE": "Mars/Base"},
])
def test_config_fails_fast(env):
    with pytest.raises(ConfigError):
        load_config(env)
