"""Seller login.

Each business logs in with its own email + password and gets a signed token (stdlib HMAC) naming
its business id; every dashboard route reads the business from that token.

The original business (id 1) can also log in with just DASHBOARD_PASSWORD. If DASHBOARD_PASSWORD
isn't set, requests without a token are treated as business 1, so the existing single-seller
setup keeps working until a password is added.

New sellers sign up themselves. With the invite code (SIGNUP_CODE) they're approved at once;
without it (open sign-up, on unless OPEN_SIGNUP=0) they wait for the founder's approval - they can
set up their dashboard meanwhile, but customers can't use their shop yet. Sign-up needs SECRET_KEY.
Every sign-in attempt is recorded (tracking.py) for the founder console.
"""
import asyncio
import base64
import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass

from collections import defaultdict, deque

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

import models
from business import DEFAULT_BUSINESS_ID
from database import get_db

DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD")
SIGNUP_CODE = os.getenv("SIGNUP_CODE")
OPEN_SIGNUP = os.getenv("OPEN_SIGNUP", "1") != "0"
SIGNUPS_PER_IP_PER_HOUR = 5
_signups_by_ip: dict[str, deque] = defaultdict(deque)
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


def issue_token(business_id: int, staff_id: int = 0, ttl: int = TOKEN_TTL_SECONDS) -> tuple[str, int]:
    """staff_id 0 means the business owner."""
    expires_at = int(time.time()) + ttl
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
    _check_can_log_in(business)
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


def _check_can_log_in(business: models.Business):
    if business.approval_status == "rejected":
        raise HTTPException(status_code=403, detail="This sign-up wasn't approved. Please contact RIRAONE support.")
    # Paused for an unpaid plan: the owner can still log in to pay. Paused by the founder: no.
    if not business.is_active and business.paused_reason != "billing":
        raise HTTPException(status_code=403, detail="This account is paused. Please contact RIRAONE support.")


# ---- endpoints -----------------------------------------------------------------------------

class LoginIn(BaseModel):
    email: str | None = None
    password: str


class SignupIn(BaseModel):
    business_name: str
    email: str
    password: str
    signup_code: str | None = None
    phone: str | None = None


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
async def login(payload: LoginIn, request: Request, background: BackgroundTasks, db: Session = Depends(get_db)):
    from tracking import record_login, request_info

    info = request_info(request)
    email = _normalise_email(payload.email) if payload.email else "(owner password)"

    def ok(business_id: int, staff_id: int = 0):
        background.add_task(record_login, info, "seller", email, True, business_id, staff_id or None)
        return _token_response(business_id, staff_id)

    if payload.email:
        business = db.query(models.Business).filter(
            func.lower(models.Business.owner_email) == _normalise_email(payload.email)
        ).first()
        if business and verify_password(payload.password, business.password_hash):
            _check_can_log_in(business)
            return ok(business.id)
        staff = db.query(models.StaffUser).filter(
            func.lower(models.StaffUser.email) == _normalise_email(payload.email)
        ).first()
        if not business and staff and verify_password(payload.password, staff.password_hash):
            _check_can_log_in(db.get(models.Business, staff.business_id))
            return ok(staff.business_id, staff.id)
    elif not DASHBOARD_PASSWORD:
        return _token_response(DEFAULT_BUSINESS_ID)
    elif hmac.compare_digest(payload.password.encode(), DASHBOARD_PASSWORD.encode()):
        return ok(DEFAULT_BUSINESS_ID)
    # Inline, not a background task: those don't run when the response is an error.
    await record_login(info, "seller", email, False)
    await asyncio.sleep(1)  # slow down password guessing
    raise HTTPException(status_code=401, detail="Wrong email or password")


def _signup_allowed_from(ip: str | None) -> bool:
    if not ip:
        return True
    now = time.time()
    recent = _signups_by_ip[ip]
    while recent and now - recent[0] > 3600:
        recent.popleft()
    if len(recent) >= SIGNUPS_PER_IP_PER_HOUR:
        return False
    recent.append(now)
    return True


@router.post("/signup", response_model=TokenOut)
async def signup(payload: SignupIn, request: Request, background: BackgroundTasks, db: Session = Depends(get_db)):
    from billing import start_trial
    from tracking import client_ip, founder_alert, record_login, request_info

    if not os.getenv("SECRET_KEY"):
        raise HTTPException(status_code=403, detail="Sign-up is not open yet")
    code = (payload.signup_code or "").strip()
    if code:
        if not SIGNUP_CODE or not hmac.compare_digest(code.encode(), SIGNUP_CODE.encode()):
            await asyncio.sleep(1)
            raise HTTPException(status_code=403, detail="Invalid invite code")
    elif not OPEN_SIGNUP:
        raise HTTPException(status_code=403, detail="Sign-up needs an invite code")

    email = _normalise_email(payload.email)
    if "@" not in email or not payload.business_name.strip():
        raise HTTPException(status_code=400, detail="Business name and a valid email are required")
    if len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    if email_taken(db, email):
        raise HTTPException(status_code=409, detail="An account with this email already exists")

    if not code and not _signup_allowed_from(client_ip(request)):  # open sign-ups only
        raise HTTPException(status_code=429, detail="Too many sign-ups from this network - please try again later")

    business = models.Business(
        name=payload.business_name.strip(), owner_email=email, password_hash=hash_password(payload.password),
        phone=(payload.phone or "").strip() or None, approval_status="approved" if code else "pending",
    )
    start_trial(business, "invite" if code else "self")
    db.add(business)
    db.commit()
    from slugs import ensure_slug

    ensure_slug(db, business)
    background.add_task(record_login, request_info(request), "seller", email, True, business.id)
    await founder_alert(db, f"New sign-up: {business.name} ({email}{', ' + business.phone if business.phone else ''})"
                            + (" - waiting for your approval" if business.approval_status == "pending" else " (invite code)"))
    return _token_response(business.id)


class MeOut(BaseModel):
    business_id: int
    business_name: str
    slug: str | None = None
    logo_url: str | None = None
    brand_color: str | None = None
    email: str | None = None
    role: str
    name: str | None


def _logo_url(business: models.Business) -> str | None:
    from routers.settings import shop_image_url

    return shop_image_url(business.logo_image_id)


@router.get("/me", response_model=MeOut)
def me(user: CurrentUser = Depends(current_user)):
    return {
        "business_id": user.business.id,
        "business_name": user.business.name,
        "slug": user.business.slug,
        "logo_url": _logo_url(user.business),
        "brand_color": user.business.brand_color,
        "email": user.staff.email if user.staff else user.business.owner_email,
        "role": "owner" if user.is_owner else "staff",
        "name": user.staff.name if user.staff else None,
    }
