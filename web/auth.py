"""
Sign-in with shared role passwords, and the guards routes depend on.

Two roles, one sign-in screen: worker credentials (APP_USERNAME /
APP_PASSWORD) open only the availability form; the manager's
(ADMIN_APP_USERNAME / ADMIN_APP_PASSWORD) open the scheduler. These are
shared passwords rather than user accounts, which fits a single desk. The
signed-in role is kept in a signed session cookie (see web/main.py).
"""

import hmac
import os

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from web.views import render

ADMIN, WORKER = "admin", "worker"

# Which env vars hold each role's shared username/password. Set locally in
# .env, and as Fly secrets in production.
ROLE_CREDENTIALS = {
    ADMIN: ("ADMIN_APP_USERNAME", "ADMIN_APP_PASSWORD"),
    WORKER: ("APP_USERNAME", "APP_PASSWORD"),
}

# Where each role lands after signing in.
HOME = {ADMIN: "/manager", WORKER: "/submit"}


class NotAllowed(Exception):
    """Raised by a guard when the session's role can't open a page. The
    handler in web/main.py redirects to `location`."""

    def __init__(self, location: str):
        self.location = location


def credentials_match(role: str, username: str, password: str) -> bool:
    """True only if this role's credentials are configured (non-empty) and
    match. An unset or blank env var must never let a blank login through."""
    user_var, pass_var = ROLE_CREDENTIALS[role]
    expected_user = (os.getenv(user_var) or "").strip()
    expected_pass = (os.getenv(pass_var) or "").strip()
    if not expected_user or not expected_pass:
        return False
    user_ok = hmac.compare_digest(username.strip().encode(), expected_user.encode())
    pass_ok = hmac.compare_digest(password.encode(), expected_pass.encode())
    return user_ok and pass_ok


def _home_for(role: str | None) -> str:
    return HOME.get(role, "/login")


def require_role(role: str):
    """A route dependency that lets only `role` through. Anyone else goes to
    their own home page, or to sign-in if they aren't signed in."""

    def guard(request: Request) -> None:
        current = request.session.get("role")
        if current != role:
            raise NotAllowed(_home_for(current))

    return guard


require_admin = require_role(ADMIN)
require_worker = require_role(WORKER)

router = APIRouter()


@router.get("/")
def home(request: Request):
    return RedirectResponse(_home_for(request.session.get("role")), status_code=303)


@router.get("/login")
def login_page(request: Request):
    if request.session.get("role") in HOME:
        return RedirectResponse(_home_for(request.session["role"]), status_code=303)
    return render(request, "login.html")


@router.post("/login")
def login(request: Request, username: str = Form(""), password: str = Form("")):
    role = next((r for r in (ADMIN, WORKER) if credentials_match(r, username, password)), None)
    if role is None:
        return render(
            request, "login.html", status_code=401, error="Invalid username or password.", username=username
        )
    request.session.clear()
    request.session["role"] = role
    return RedirectResponse(HOME[role], status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
