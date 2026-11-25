"""The signed-in user's own account."""
from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from pricewatch import forms, security
from pricewatch.repos import users as users_repo
from pricewatch.web.deps import current_user, flash, read_form, redirect, render, services, sign_in

router = APIRouter()


@router.get("/account")
def account_page(request: Request):
    user = current_user(request)
    return render(request, "account.html", user=user, nav="account", errors={},
                  min_password=security.MIN_PASSWORD_LENGTH)


@router.post("/account/password")
async def change_password(request: Request):
    user = current_user(request)
    form = await read_form(request)
    parsed, errors = forms.parse_password_change(form)
    if parsed is not None:
        current, new = parsed
        if not await run_in_threadpool(security.verify_password, user.password_hash, current):
            parsed, errors = None, {"current_password": "This is not your current password."}
    if parsed is None:
        return render(request, "account.html", status_code=422, user=user, nav="account", errors=errors,
                      min_password=security.MIN_PASSWORD_LENGTH)
    svc = services(request)
    new_hash = await run_in_threadpool(security.hash_password, parsed[1])
    with svc.db.connect() as conn:
        users_repo.set_password(conn, user.id, new_hash)
        refreshed = users_repo.get(conn, user.id)
    sign_in(request, refreshed)  # keep this browser signed in; every other session is now invalid
    flash(request, "Password changed. Other devices were signed out.")
    return redirect("/account")
