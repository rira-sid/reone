"""Password login for the seller dashboard.

Set DASHBOARD_PASSWORD on the server to turn protection on. The dashboard exchanges the password
for a signed token (stdlib HMAC - no extra dependency) and sends it as a Bearer header. If
DASHBOARD_PASSWORD isn't set the API stays open, so existing setups keep working until it's added.
"""
import asyncio
import base64
import hashlib
import hmac
import os
import time

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD")
TOKEN_TTL_SECONDS = 30 * 24 * 3600

# Changing the password (or AUTH_SECRET) invalidates every token issued before.
_SECRET = (os.getenv("AUTH_SECRET") or f"reone:{DASHBOARD_PASSWORD}").encode()

if not DASHBOARD_PASSWORD:
    print("WARNING: DASHBOARD_PASSWORD is not set - the dashboard API is open to anyone with the URL.")

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginIn(BaseModel):
    password: str


class LoginOut(BaseModel):
    token: str
    expires_at: int


def _sign(payload: str) -> str:
    return hmac.new(_SECRET, payload.encode(), hashlib.sha256).hexdigest()


def _issue_token() -> tuple[str, int]:
    expires_at = int(time.time()) + TOKEN_TTL_SECONDS
    payload = str(expires_at)
    token = base64.urlsafe_b64encode(f"{payload}.{_sign(payload)}".encode()).decode()
    return token, expires_at


def _token_valid(token: str) -> bool:
    try:
        payload, signature = base64.urlsafe_b64decode(token.encode()).decode().split(".", 1)
        expires_at = int(payload)
    except (ValueError, UnicodeDecodeError):
        return False
    return hmac.compare_digest(signature, _sign(payload)) and expires_at > time.time()


def require_auth(authorization: str | None = Header(default=None)):
    if not DASHBOARD_PASSWORD:
        return
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not _token_valid(token):
        raise HTTPException(status_code=401, detail="Please log in")


@router.post("/login", response_model=LoginOut)
async def login(payload: LoginIn):
    if not DASHBOARD_PASSWORD:
        token, expires_at = _issue_token()
        return {"token": token, "expires_at": expires_at}
    if not hmac.compare_digest(payload.password.encode(), DASHBOARD_PASSWORD.encode()):
        await asyncio.sleep(1)  # slow down password guessing
        raise HTTPException(status_code=401, detail="Wrong password")
    token, expires_at = _issue_token()
    return {"token": token, "expires_at": expires_at}
