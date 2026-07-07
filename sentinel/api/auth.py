"""Single-user session auth (spec §7).

Local network is the default deployment, so auth is enforced only when
APP_ENV != "dev" — for any remote exposure set APP_ENV=prod and a strong
APP_PASSWORD, and put HTTPS in front (docs/SETUP.md). Sessions are an
HMAC-signed cookie derived from APP_SECRET_KEY; there are no user accounts.
"""

import hashlib
import hmac
import secrets
import time

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from sentinel.config import get_settings

COOKIE_NAME = "bq_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600


def _sign(value: str) -> str:
    key = get_settings().app_secret_key.encode()
    return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()


def make_session_token() -> str:
    issued = str(int(time.time()))
    return f"{issued}.{_sign(issued)}"


def session_valid(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    issued, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(issued)):
        return False
    try:
        age = time.time() - int(issued)
    except ValueError:
        return False
    return 0 <= age <= SESSION_TTL_SECONDS


def auth_enabled() -> bool:
    return not get_settings().is_dev


def require_auth(request: Request) -> None:
    """Router dependency: reject unauthenticated API calls in prod."""
    if not auth_enabled():
        return
    if not session_valid(request.cookies.get(COOKIE_NAME)):
        raise HTTPException(status_code=401, detail="login required")


router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    password: str


@router.post("/login")
def login(body: LoginIn, response: Response) -> dict:
    expected = get_settings().app_password
    if not secrets.compare_digest(body.password, expected):
        raise HTTPException(status_code=401, detail="invalid password")
    response.set_cookie(
        COOKIE_NAME,
        make_session_token(),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
    )
    return {"ok": True}


@router.post("/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


@router.get("/me")
def me(request: Request) -> dict:
    if auth_enabled() and not session_valid(request.cookies.get(COOKIE_NAME)):
        return {"authenticated": False, "auth_required": True}
    return {"authenticated": True, "auth_required": auth_enabled()}
