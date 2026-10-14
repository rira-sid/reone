"""Founder login - completely separate from shop logins.

The founder console lives at its own URL with its own account (FounderUser). Founder tokens are
signed like shop tokens but carry an "F:" prefix, so a founder token is never accepted as a shop
login and a shop token is never accepted here.

First-time setup: the founder account is created once, from the founder site, by proving you know
the server's DASHBOARD_PASSWORD (the original owner password). After that, setup is closed for good.
"""
import asyncio
import base64
import hmac
import time

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

import models
from auth import DASHBOARD_PASSWORD, _sign, hash_password, verify_password
from database import get_db

FOUNDER_TOKEN_TTL = 7 * 24 * 3600
MIN_PASSWORD = 12

router = APIRouter(prefix="/founder", tags=["founder"])


def _issue(founder_id: int) -> dict:
    expires_at = int(time.time()) + FOUNDER_TOKEN_TTL
    payload = f"F:{founder_id}:{expires_at}"
    token = base64.urlsafe_b64encode(f"{payload}.{_sign(payload)}".encode()).decode()
    return {"token": token, "expires_at": expires_at}


def _read(token: str) -> int | None:
    try:
        payload, signature = base64.urlsafe_b64decode(token.encode()).decode().split(".", 1)
        kind, founder_id, expires_at = payload.split(":")
        founder_id, expires_at = int(founder_id), int(expires_at)
    except (ValueError, UnicodeDecodeError):
        return None
    if kind != "F" or not hmac.compare_digest(signature, _sign(payload)) or expires_at <= time.time():
        return None
    return founder_id


def current_founder(authorization: str | None = Header(default=None), db: Session = Depends(get_db)) -> models.FounderUser:
    scheme, _, token = (authorization or "").partition(" ")
    founder_id = _read(token) if scheme.lower() == "bearer" else None
    founder = db.get(models.FounderUser, founder_id) if founder_id else None
    if not founder:
        raise HTTPException(status_code=401, detail="Please log in")
    return founder


class FounderLogin(BaseModel):
    email: str
    password: str


class FounderSetup(FounderLogin):
    setup_key: str


@router.get("/status")
def status(db: Session = Depends(get_db)):
    return {"setup_needed": db.query(models.FounderUser).count() == 0}


@router.post("/setup")
async def setup(payload: FounderSetup, db: Session = Depends(get_db)):
    if db.query(models.FounderUser).count():
        raise HTTPException(status_code=403, detail="The founder account already exists - please log in")
    if not DASHBOARD_PASSWORD or not hmac.compare_digest(payload.setup_key.encode(), DASHBOARD_PASSWORD.encode()):
        await asyncio.sleep(1)
        raise HTTPException(status_code=403, detail="Wrong setup key")
    email = payload.email.strip().lower()
    if "@" not in email:
        raise HTTPException(status_code=400, detail="Enter a valid email")
    if len(payload.password) < MIN_PASSWORD:
        raise HTTPException(status_code=400, detail=f"Use at least {MIN_PASSWORD} characters for the founder password")
    founder = models.FounderUser(email=email, password_hash=hash_password(payload.password))
    db.add(founder)
    db.commit()
    return _issue(founder.id)


@router.post("/login")
async def login(payload: FounderLogin, db: Session = Depends(get_db)):
    founder = db.query(models.FounderUser).filter(
        func.lower(models.FounderUser.email) == payload.email.strip().lower()).first()
    if founder and verify_password(payload.password, founder.password_hash):
        return _issue(founder.id)
    await asyncio.sleep(1.5)  # slow down guessing
    raise HTTPException(status_code=401, detail="Wrong email or password")


@router.get("/me")
def me(founder: models.FounderUser = Depends(current_founder)):
    return {"email": founder.email}


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


@router.post("/password")
def change_password(payload: PasswordChange, founder: models.FounderUser = Depends(current_founder),
                    db: Session = Depends(get_db)):
    if not verify_password(payload.current_password, founder.password_hash):
        raise HTTPException(status_code=400, detail="Current password is wrong")
    if len(payload.new_password) < MIN_PASSWORD:
        raise HTTPException(status_code=400, detail=f"Use at least {MIN_PASSWORD} characters")
    founder.password_hash = hash_password(payload.new_password)
    db.commit()
    return {"ok": True}
