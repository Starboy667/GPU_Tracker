"""Sign in and out."""
from __future__ import annotations

import hashlib
import math

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from pricewatch import security
from pricewatch.repos import users as users_repo
from pricewatch.web.deps import (
    LoginRequired,
    client_ip,
    current_user,
    read_form,
    redirect,
    render,
    safe_next,
    services,
    sign_in,
)

router = APIRouter()

GENERIC_FAILURE = "Wrong email or password."


@router.get("/login")
def login_page(request: Request, next: str = "/"):
    try:
        current_user(request)
    except LoginRequired:
        return render(request, "login.html", next=safe_next(next), email="", error=None)
    return redirect(safe_next(next))


@router.post("/login")
async def login(request: Request):
    form = await read_form(request)
    svc = services(request)
    email = security.normalize_email(str(form.get("email") or ""))
    password = str(form.get("password") or "")
    target = safe_next(str(form.get("next") or "/"))
    # Per address: strict. Per account, across all addresses: loose, so that guessing at an
    # account from many places is slowed down without letting anyone lock its owner out.
    ip_key = "ip:" + client_ip(request)[:64]
    account_key = "account:" + hashlib.sha256(email.encode()).hexdigest()[:32]

    wait = max(svc.limiter.retry_after(ip_key), svc.account_limiter.retry_after(account_key))
    if wait > 0:
        minutes = max(math.ceil(wait / 60), 1)
        message = f"Too many failed attempts. Try again in {minutes} minute{'s' if minutes > 1 else ''}."
        return render(request, "login.html", status_code=429, next=target, email=email, error=message)

    well_formed = security.is_valid_email(email) and len(password) <= security.MAX_PASSWORD_LENGTH
    with svc.db.connect() as conn:
        user = users_repo.get_by_email(conn, email) if well_formed else None
    valid = await run_in_threadpool(security.verify_password, user.password_hash if user else None, password)
    if not valid:
        svc.limiter.record_failure(ip_key)
        svc.account_limiter.record_failure(account_key)
        shown = email if well_formed else ""
        return render(request, "login.html", status_code=401, next=target, email=shown, error=GENERIC_FAILURE)

    svc.limiter.reset(ip_key)
    if security.needs_rehash(user.password_hash):
        new_hash = await run_in_threadpool(security.hash_password, password)
        with svc.db.connect() as conn:
            users_repo.replace_hash(conn, user.id, new_hash)
    sign_in(request, user)
    return redirect(target)


@router.post("/logout")
async def logout(request: Request):
    await read_form(request)
    request.session.clear()
    return redirect("/login")
