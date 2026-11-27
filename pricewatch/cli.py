"""Command line: run the server, or do the few things that must work without the web UI."""
from __future__ import annotations

import argparse
import dataclasses
import getpass
import logging
import sys
from pathlib import Path

from pricewatch import security
from pricewatch.config import Config, ConfigError, load_config
from pricewatch.db import Database, iso, utcnow
from pricewatch.repos import users as users_repo
from pricewatch.repos import watches as watches_repo


def _database(config: Config) -> Database:
    db = Database(config.db_path)
    db.migrate()
    return db


def _read_password(args: argparse.Namespace) -> str:
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Again: "):
        raise SystemExit("The two passwords do not match.")
    return first


def cmd_serve(args: argparse.Namespace, config: Config) -> int:
    import uvicorn

    from pricewatch.web.app import create_app

    uvicorn.run(
        create_app(config),
        host=config.host,
        port=config.port,
        proxy_headers=True,
        forwarded_allow_ips="*",  # the pod is only reachable through the ingress controller
        log_level="info",
        access_log=False,
    )
    return 0


def cmd_check(args: argparse.Namespace, config: Config) -> int:
    """Run one check now, in the foreground (cron-style use, or debugging a scraper)."""
    from pricewatch.models import TRIGGER_MANUAL
    from pricewatch.web.app import build_services

    _database(config)
    run = build_services(config).checker.run(TRIGGER_MANUAL)
    print(f"{run.status}: {run.watches} watches, {run.listings} offers, {run.alerts} alerts")
    for watch, site, error in run.errors:
        print(f"  ! {watch or '-'} / {site or '-'}: {error}", file=sys.stderr)
    return 0 if run.status != "failed" else 1


def cmd_create_user(args: argparse.Namespace, config: Config) -> int:
    email = security.normalize_email(args.email)
    if not security.is_valid_email(email):
        raise SystemExit("Not a valid email address.")
    password = _read_password(args)
    problem = security.password_problem(password)
    if problem:
        raise SystemExit(problem)
    with _database(config).connect() as conn:
        if users_repo.get_by_email(conn, email):
            raise SystemExit(f"An account already exists for {email}.")
        users_repo.create(conn, email, security.hash_password(password), args.admin, iso(utcnow()))
    print(f"Created {'administrator' if args.admin else 'member'} {email}.")
    return 0


def cmd_reset_password(args: argparse.Namespace, config: Config) -> int:
    email = security.normalize_email(args.email)
    password = _read_password(args)
    problem = security.password_problem(password)
    if problem:
        raise SystemExit(problem)
    with _database(config).connect() as conn:
        user = users_repo.get_by_email(conn, email)
        if user is None:
            raise SystemExit(f"No account for {email}.")
        users_repo.set_password(conn, user.id, security.hash_password(password))
    print(f"Password changed for {email}. Their sessions were signed out.")
    return 0


def cmd_import(args: argparse.Namespace, config: Config) -> int:
    from pricewatch import portability
    from pricewatch.scrapers import SCRAPERS, validate_site_url

    try:
        watches = portability.import_yaml(
            Path(args.file).read_text(encoding="utf-8"), site_keys=SCRAPERS, validate_site_url=validate_site_url
        )
    except OSError as exc:
        raise SystemExit(f"Cannot read {args.file}: {exc.strerror}") from exc
    except portability.ImportError_ as exc:
        raise SystemExit("Nothing imported:\n  - " + "\n  - ".join(exc.problems)) from exc
    created = skipped = 0
    with _database(config).connect() as conn:
        if args.owner:
            owner = users_repo.get_by_email(conn, security.normalize_email(args.owner))
            if owner is None:
                raise SystemExit(f"No account for {args.owner}.")
        else:
            owner = users_repo.first_admin(conn)
            if owner is None:
                raise SystemExit("Create an account first (pricewatch create-user), or name the owner with --owner.")
        for watch in watches:
            if watches_repo.get_by_name(conn, owner.id, watch.name):
                skipped += 1
            else:
                watches_repo.create(conn, dataclasses.replace(watch, owner_id=owner.id), iso(utcnow()))
                created += 1
    print(f"Imported {created} watches for {owner.email}, skipped {skipped} they already have.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pricewatch", description=__doc__)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the web app and the scheduler (default)").set_defaults(func=cmd_serve)
    sub.add_parser("check", help="run one check now and exit").set_defaults(func=cmd_check)

    create = sub.add_parser("create-user", help="create an account")
    create.add_argument("email")
    create.add_argument("--admin", action="store_true", help="make it an administrator")
    create.add_argument("--password-stdin", action="store_true", help="read the password from standard input")
    create.set_defaults(func=cmd_create_user)

    reset = sub.add_parser("reset-password", help="set a new password for an account")
    reset.add_argument("email")
    reset.add_argument("--password-stdin", action="store_true", help="read the password from standard input")
    reset.set_defaults(func=cmd_reset_password)

    importer = sub.add_parser("import", help="import watches from a YAML file")
    importer.add_argument("file")
    importer.add_argument("--owner", metavar="EMAIL", help="account the watches will belong to (default: the first administrator)")
    importer.set_defaults(func=cmd_import)
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    try:
        config = load_config()
        return (getattr(args, "func", None) or cmd_serve)(args, config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
