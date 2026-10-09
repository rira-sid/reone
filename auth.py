"""Seller login.

Each business logs in with its own email + password and gets a signed token (stdlib HMAC) naming
its business id; every dashboard route reads the business from that token.

The original business (id 1) can also log in with just DASHBOARD_PASSWORD. If DASHBOARD_PASSWORD
isn't set, requests without a token are treated as business 1, so the existing single-seller
setup keeps working until a password is added.

New sellers sign up with an invite code (SIGNUP_CODE). Sign-up is closed unless both SIGNUP_CODE
and SECRET_KEY are set.
"""
import asyncio
import base64
import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

import models
from business import DEFAULT_BUSINESS_ID
from database import get_db

DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD")
SIGNUP_CODE = os.getenv("SIGNUP_CODE")
TOKEN_TTL_SECONDS = 30 * 24 * 3600

# Never sign with a guessable value: with no configured secret, use a random per-process one
# (tokens then just expire on restart).
_SECRET = (
    os.getenv("SECRET_KEY") or os.getenv("AUTH_SECRET")
    or (f"reone:{DASHBOARD_PASSWORD}" if DASHBOARD_PASSWORD else secrets.token_hex(32))
).encode()

if not DASHBOARD_PASSWORD:
    print("WARNING: DASHBOARD_PASSWORD is not set - business 1's dashboard is open to anyone with the URL.")

router = APIRouter(prefix="/auth", tags=["auth"])


# ---- passwords -----------------------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    try:
        scheme, salt_hex, digest_hex = (stored or "").split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
    return hmac.compare_digest(digest.hex(), digest_hex)


# ---- tokens --------------------------------------------------------------------------------

def _sign(payload: str) -> str:
    return hmac.new(_SECRET, payload.encode(), hashlib.sha256).hexdigest()


def sign_value(value: str) -> str:
    """Signature for public links (e.g. invoices) so their URLs can't be guessed."""
    return _sign(f"link:{value}")[:32]


def issue_token(business_id: int, staff_id: int = 0) -> tuple[str, int]:
    """staff_id 0 means the business owner."""
    expires_at = int(time.time()) + TOKEN_TTL_SECONDS
    payload = f"{business_id}:{staff_id}:{expires_at}"
    token = base64.urlsafe_b64encode(f"{payload}.{_sign(payload)}".encode()).decode()
    return token, expires_at


def _read_token(token: str) -> tuple[int, int] | None:
    """Returns (business_id, staff_id) for a valid, unexpired token."""
    try:
        payload, signature = base64.urlsafe_b64decode(token.encode()).decode().split(".", 1)
        business_id, staff_id, expires_at = (int(part) for part in payload.split(":"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not hmac.compare_digest(signature, _sign(payload)) or expires_at <= time.time():
        return None
    return business_id, staff_id


@dataclass
class CurrentUser:
    business: models.Business
    staff: models.StaffUser | None  # None = the owner

    @property
    def is_owner(self) -> bool:
        return self.staff is None


def current_user(authorization: str | None = Header(default=None), db: Session = Depends(get_db)) -> CurrentUser:
    scheme, _, token = (authorization or "").partition(" ")
    if not token and not DASHBOARD_PASSWORD:
        ids = (DEFAULT_BUSINESS_ID, 0)  # legacy open mode, see module docstring
    else:
        ids = _read_token(token) if scheme.lower() == "bearer" else None
    business = db.get(models.Business, ids[0]) if ids else None
    if not business:
        raise HTTPException(status_code=401, detail="Please log in")
    staff = None
    if ids[1]:
        staff = db.get(models.StaffUser, ids[1])
        if not staff or staff.business_id != business.id:  # removed staff lose access immediately
            raise HTTPException(status_code=401, detail="Please log in")
    return CurrentUser(business, staff)


def current_business(user: CurrentUser = Depends(current_user)) -> models.Business:
    return user.business


def owner_business(user: CurrentUser = Depends(current_user)) -> models.Business:
    """For settings, payment keys and staff management - owner only."""
    if not user.is_owner:
        raise HTTPException(status_code=403, detail="Only the shop owner can do this")
    return user.business


def email_taken(db: Session, email: str, exclude_business_id: int | None = None) -> bool:
    """Emails are unique across owners and staff, since one login form serves both."""
    owner = db.query(models.Business).filter(func.lower(models.Business.owner_email) == email)
    if exclude_business_id:
        owner = owner.filter(models.Business.id != exclude_business_id)
    staff = db.query(models.StaffUser).filter(func.lower(models.StaffUser.email) == email)
    return owner.first() is not None or staff.first() is not None


# ---- endpoints -----------------------------------------------------------------------------

class LoginIn(BaseModel):
    email: str | None = None
    password: str


class SignupIn(BaseModel):
    business_name: str
    email: str
    password: str
    signup_code: str


class TokenOut(BaseModel):
    token: str
    expires_at: int
    business_id: int


def _token_response(business_id: int, staff_id: int = 0) -> dict:
    token, expires_at = issue_token(business_id, staff_id)
    return {"token": token, "expires_at": expires_at, "business_id": business_id}


def _normalise_email(email: str) -> str:
    return email.strip().lower()


@router.post("/login", response_model=TokenOut)
async def login(payload: LoginIn, db: Session = Depends(get_db)):
    if payload.email:
        business = db.query(models.Business).filter(
            func.lower(models.Business.owner_email) == _normalise_email(payload.email)
        ).first()
        if business and verify_password(payload.password, business.password_hash):
            return _token_response(business.id)
        staff = db.query(models.StaffUser).filter(
            func.lower(models.StaffUser.email) == _normalise_email(payload.email)
        ).first()
        if not business and staff and verify_password(payload.password, staff.password_hash):
            return _token_response(staff.business_id, staff.id)
    elif not DASHBOARD_PASSWORD:
        return _token_response(DEFAULT_BUSINESS_ID)
    elif hmac.compare_digest(payload.password.encode(), DASHBOARD_PASSWORD.encode()):
        return _token_response(DEFAULT_BUSINESS_ID)
    await asyncio.sleep(1)  # slow down password guessing
    raise HTTPException(status_code=401, detail="Wrong email or password")


@router.post("/signup", response_model=TokenOut)
async def signup(payload: SignupIn, db: Session = Depends(get_db)):
    if not SIGNUP_CODE or not os.getenv("SECRET_KEY"):
        raise HTTPException(status_code=403, detail="Sign-up is not open yet")
    if not hmac.compare_digest(payload.signup_code.strip().encode(), SIGNUP_CODE.encode()):
        await asyncio.sleep(1)
        raise HTTPException(status_code=403, detail="Invalid invite code")

    email = _normalise_email(payload.email)
    if "@" not in email or not payload.business_name.strip():
        raise HTTPException(status_code=400, detail="Business name and a valid email are required")
    if len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    if email_taken(db, email):
        raise HTTPException(status_code=409, detail="An account with this email already exists")

    business = models.Business(
        name=payload.business_name.strip(), owner_email=email, password_hash=hash_password(payload.password)
    )
    db.add(business)
    db.commit()
    return _token_response(business.id)


class MeOut(BaseModel):
    business_id: int
    business_name: str
    role: str
    name: str | None


@router.get("/me", response_model=MeOut)
def me(user: CurrentUser = Depends(current_user)):
    return {
        "business_id": user.business.id,
        "business_name": user.business.name,
        "role": "owner" if user.is_owner else "staff",
        "name": user.staff.name if user.staff else None,
    }
