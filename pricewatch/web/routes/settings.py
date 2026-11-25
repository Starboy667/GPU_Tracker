"""Global settings and user management (administrators only)."""
from __future__ import annotations

import dataclasses
import sqlite3

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from pricewatch import forms, security
from pricewatch.db import iso
from pricewatch.repos import settings as settings_repo
from pricewatch.repos import users as users_repo
from pricewatch.web.deps import NotFound, flash, read_form, redirect, render, require_admin, services
from pricewatch.web.views import number_input

router = APIRouter()


def _settings_values(settings) -> dict[str, str]:
    values = {}
    for name, value in dataclasses.asdict(settings).items():
        values[name] = ("on" if value else "") if isinstance(value, bool) else number_input(float(value))
    return values


@router.get("/settings")
def settings_page(request: Request):
    user = require_admin(request)
    with services(request).db.connect() as conn:
        settings = settings_repo.load(conn)
    return render(request, "settings.html", user=user, nav="settings", values=_settings_values(settings), errors={})


@router.post("/settings")
async def save_settings(request: Request):
    user = require_admin(request)
    form = await read_form(request)
    settings, errors = forms.parse_settings(form)
    if settings is None:
        return render(request, "settings.html", status_code=422, user=user, nav="settings", values=dict(form),
                      errors=errors)
    with services(request).db.connect() as conn:
        settings_repo.save(conn, settings)
    flash(request, "Settings saved. They apply from the next check.")
    return redirect("/settings")


# ------------------------------------------------------------------ users

def _users_page(request, user, *, values=None, errors=None, status_code=200):
    with services(request).db.connect() as conn:
        everyone = users_repo.list_all(conn)
    return render(
        request, "users.html", status_code=status_code, user=user, nav="settings", users=everyone,
        values=values or {}, errors=errors or {},
        min_password=security.MIN_PASSWORD_LENGTH,
    )


@router.get("/users")
def users_page(request: Request):
    return _users_page(request, require_admin(request))


@router.post("/users")
async def create_user(request: Request):
    user = require_admin(request)
    form = await read_form(request)
    parsed, errors = forms.parse_new_user(form)
    if parsed is None:
        return _users_page(request, user, values={"email": form.get("email", "")}, errors=errors, status_code=422)
    email, password, is_admin = parsed
    svc = services(request)
    password_hash = await run_in_threadpool(security.hash_password, password)
    try:
        with svc.db.connect() as conn:
            users_repo.create(conn, email, password_hash, is_admin, iso(svc.clock()))
    except sqlite3.IntegrityError:
        return _users_page(request, user, values={"email": email},
                           errors={"email": "An account with this email already exists."}, status_code=422)
    flash(request, f"Account created for {email}. Give them the password yourself: nothing is emailed.")
    return redirect("/users")


@router.post("/users/{user_id}/password")
async def reset_password(request: Request, user_id: int):
    user = require_admin(request)
    form = await read_form(request)
    password = str(form.get("password") or "")
    svc = services(request)
    with svc.db.connect() as conn:
        target = users_repo.get(conn, user_id)
    if target is None:
        raise NotFound
    problem = security.password_problem(password)
    if problem:
        flash(request, problem, "error")
        return redirect("/users")
    password_hash = await run_in_threadpool(security.hash_password, password)
    with svc.db.connect() as conn:
        users_repo.set_password(conn, target.id, password_hash)
    if target.id == user.id:
        request.session.clear()
        return redirect("/login")
    flash(request, f"Password changed for {target.email}. Their open sessions were signed out.")
    return redirect("/users")


@router.post("/users/{user_id}/delete")
async def delete_user(request: Request, user_id: int):
    user = require_admin(request)
    await read_form(request)
    svc = services(request)
    with svc.db.connect() as conn:
        target = users_repo.get(conn, user_id)
        if target is None:
            raise NotFound
        if target.id == user.id:
            # Also what keeps at least one administrator: only administrators get here.
            flash(request, "You cannot delete the account you are signed in with.", "error")
        else:
            users_repo.delete(conn, target.id)
            flash(request, f"Account {target.email} deleted.")
    return redirect("/users")
