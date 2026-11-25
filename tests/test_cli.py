import io

import pytest

from pricewatch import cli, security
from pricewatch.db import Database
from pricewatch.repos import users, watches
from tests.conftest import SECRET_KEY


@pytest.fixture
def env(monkeypatch, tmp_path, public_dns):
    monkeypatch.setenv("SECRET_KEY", SECRET_KEY)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    return Database(tmp_path / "pricewatch.db")


def stdin(monkeypatch, text):
    monkeypatch.setattr("sys.stdin", io.StringIO(text))


def test_create_user_and_reset_password(env, monkeypatch, capsys):
    stdin(monkeypatch, "a long enough password\n")
    assert cli.main(["create-user", "Me@Example.org", "--admin", "--password-stdin"]) == 0
    assert "Created administrator me@example.org." in capsys.readouterr().out
    with env.connect() as conn:
        user = users.get_by_email(conn, "me@example.org")
    assert user.is_admin and security.verify_password(user.password_hash, "a long enough password")

    stdin(monkeypatch, "another password\n")
    with pytest.raises(SystemExit, match="already exists"):
        cli.main(["create-user", "me@example.org", "--password-stdin"])

    stdin(monkeypatch, "the replacement password\n")
    assert cli.main(["reset-password", "me@example.org", "--password-stdin"]) == 0
    with env.connect() as conn:
        changed = users.get_by_email(conn, "me@example.org")
    assert security.verify_password(changed.password_hash, "the replacement password")
    assert changed.session_version == user.session_version + 1


def test_user_commands_validate_their_input(env, monkeypatch):
    stdin(monkeypatch, "short\n")
    with pytest.raises(SystemExit, match="at least"):
        cli.main(["create-user", "me@example.org", "--password-stdin"])
    with pytest.raises(SystemExit, match="valid email"):
        cli.main(["create-user", "nope", "--password-stdin"])
    stdin(monkeypatch, "short\n")
    with pytest.raises(SystemExit, match="at least"):
        cli.main(["reset-password", "me@example.org", "--password-stdin"])
    stdin(monkeypatch, "a long enough password\n")
    with pytest.raises(SystemExit, match="No account"):
        cli.main(["reset-password", "ghost@example.org", "--password-stdin"])


def test_password_prompt(env, monkeypatch):
    answers = iter(["typed twice ok!", "typed twice ok!", "one thing", "another thing"])
    monkeypatch.setattr("getpass.getpass", lambda prompt: next(answers))
    assert cli.main(["create-user", "me@example.org"]) == 0
    with pytest.raises(SystemExit, match="do not match"):
        cli.main(["create-user", "you@example.org"])


def test_import_command(env, tmp_path, capsys):
    source = tmp_path / "watches.yaml"
    source.write_text("watches:\n  - {name: LG OLED55C5, sites: [ldlc]}\n  - {name: RTX 5070, sites: [idealo]}\n")
    assert cli.main(["import", str(source)]) == 0
    assert cli.main(["import", str(source)]) == 0
    out = capsys.readouterr().out
    assert "Imported 2 watches, skipped 0" in out and "Imported 0 watches, skipped 2" in out
    with env.connect() as conn:
        assert [w.name for w in watches.list_all(conn)] == ["LG OLED55C5", "RTX 5070"]

    source.write_text("watches:\n  - {name: A, sites: [nope]}\n")
    with pytest.raises(SystemExit, match="unknown site"):
        cli.main(["import", str(source)])
    with pytest.raises(SystemExit, match="Cannot read"):
        cli.main(["import", str(tmp_path / "missing.yaml")])


def test_check_command_runs_once(env, capsys):
    assert cli.main(["check"]) == 0  # no watch yet: nothing is fetched
    assert "ok: 0 watches, 0 offers, 0 alerts" in capsys.readouterr().out


def test_missing_secret_key_is_a_clear_error(monkeypatch, capsys):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    assert cli.main(["check"]) == 2
    assert "SECRET_KEY must be set" in capsys.readouterr().err


def test_serve_is_the_default_command(env, monkeypatch):
    started = {}

    def fake_run(app, **kwargs):
        started.update(kwargs, title=app.title)

    monkeypatch.setattr("uvicorn.run", fake_run)
    assert cli.main([]) == 0
    assert started["title"] == "Price Watch" and started["port"] == 8000 and started["proxy_headers"] is True
