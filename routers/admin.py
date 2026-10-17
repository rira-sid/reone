"""Founder console: every business on RIRAONE, how each is doing, and onboarding new ones.
Only a founder login (founder_auth.py) can call these - shop logins never can."""
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

import billing
import models
from auth import email_taken, hash_password, issue_token
from founder_auth import current_founder
from business import DEFAULT_BUSINESS_ID, whatsapp_creds
from database import get_db
from routers.insights import funnel
from routers.invoices import PUBLIC_BASE_URL
from routers.pay import effective_payment_method
from slugs import check_wanted, ensure_slug, shop_path
from tracking import audit, client_ip

router = APIRouter(prefix="/admin", tags=["founder"], dependencies=[Depends(current_founder)])


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def setup_steps(db: Session, business: models.Business) -> dict:
    """What a business still has to do before customers can order end to end."""
    products = db.query(models.Product).filter(
        models.Product.business_id == business.id, models.Product.is_active.is_(True)).count()
    return {
        "details": bool(business.name and business.name != "My Business" and (business.phone or business.address)),
        "products": products > 0,
        "whatsapp": whatsapp_creds(business) is not None,
        "payments": effective_payment_method(business) is not None,
        "alerts": bool(business.alert_phone),
    }


def _row(db: Session, business: models.Business, now: datetime) -> dict:
    since = now - timedelta(days=30)
    orders = db.query(models.Order).filter(
        models.Order.business_id == business.id, models.Order.status != "Cancelled").all()
    recent = [o for o in orders if o.created_at and _utc(o.created_at) >= since]
    last_order = max((_utc(o.created_at) for o in orders if o.created_at), default=None)
    last_chat = db.query(func.max(models.Conversation.last_customer_message_at)).filter(
        models.Conversation.business_id == business.id).scalar()
    last_activity = max([d for d in (last_order, _utc(last_chat)) if d], default=None)
    steps = setup_steps(db, business)
    messages = db.query(models.Message.sender, func.count(models.Message.id)).join(models.Conversation).filter(
        models.Conversation.business_id == business.id, models.Message.created_at >= since
    ).group_by(models.Message.sender).all()
    messages = dict(messages)
    last_login = db.query(func.max(models.LoginEvent.created_at)).filter(
        models.LoginEvent.site == "seller", models.LoginEvent.business_id == business.id,
        models.LoginEvent.success.is_(True)).scalar()
    return {
        "id": business.id,
        "name": business.name,
        "owner_email": business.owner_email,
        "phone": business.phone,
        "created_at": business.created_at,
        "is_active": business.is_active,
        "paused_reason": business.paused_reason,
        "approval_status": business.approval_status,
        "signup_source": business.signup_source,
        "billing": billing.status(db, business, now),
        "usage_30d": {
            "customer_messages": messages.get("customer", 0),
            "bot_replies": messages.get("ai", 0),
            "seller_replies": messages.get("seller", 0),
            "chats": db.query(models.Conversation).filter(
                models.Conversation.business_id == business.id,
                models.Conversation.last_customer_message_at >= since).count(),
        },
        "last_login_at": _utc(last_login),
        "wa_health": business.wa_health,
        "wa_health_detail": business.wa_health_detail,
        "is_founder_shop": business.id == DEFAULT_BUSINESS_ID,
        "setup": steps,
        "setup_done": sum(steps.values()),
        "setup_total": len(steps),
        "products": db.query(models.Product).filter(
            models.Product.business_id == business.id, models.Product.is_active.is_(True)).count(),
        "orders_total": len(orders),
        "orders_30d": len(recent),
        "revenue_30d": round(sum(o.total_amount for o in recent if o.is_paid), 2),
        "revenue_total": round(sum(o.total_amount for o in orders if o.is_paid), 2),
        "customers": db.query(models.Customer.phone).filter(
            models.Customer.business_id == business.id).distinct().count(),
        "needs_you": db.query(models.Conversation).filter(
            models.Conversation.business_id == business.id, models.Conversation.needs_seller.is_(True)).count(),
        "funnel_30d": funnel(db, business.id),
        "last_activity_at": last_activity,
        "active_7d": bool(last_activity and now - last_activity < timedelta(days=7)),
        "shop_url": f"{PUBLIC_BASE_URL}{shop_path(business)}",
        "slug": business.slug,
    }


@router.get("/overview")
def overview(db: Session = Depends(get_db)):
    now = datetime.now(timezone.utc)
    businesses = db.query(models.Business).order_by(models.Business.id).all()
    rows = [_row(db, b, now) for b in businesses]
    totals = {
        "businesses": len(rows),
        "active_7d": sum(1 for r in rows if r["active_7d"]),
        "live": sum(1 for r in rows if r["setup_done"] == r["setup_total"] and r["is_active"]),
        "orders_30d": sum(r["orders_30d"] for r in rows),
        "revenue_30d": round(sum(r["revenue_30d"] for r in rows), 2),
        "views_30d": sum(r["funnel_30d"]["views"] for r in rows),
        "new_30d": sum(1 for b in businesses if b.created_at and now - _utc(b.created_at) < timedelta(days=30)),
        "pending_approval": sum(1 for r in rows if r["approval_status"] == "pending"),
        "overdue": sum(1 for r in rows if r["billing"]["state"] in ("overdue", "paused")),
        "trials": sum(1 for r in rows if r["billing"]["plan"] == "trial"),
        "paying": sum(1 for r in rows if r["billing"]["plan"] in billing.PAID_PLANS and r["billing"]["state"] in ("active", "due_soon")),
    }
    return {"totals": totals, "businesses": rows}


class NewBusiness(BaseModel):
    name: str
    email: str
    phone: str | None = None
    password: str | None = None  # blank = generate one
    slug: str | None = None  # web address; blank = made from the name


@router.post("/businesses")
def create_business(payload: NewBusiness, request: Request, db: Session = Depends(get_db),
                    founder: models.FounderUser = Depends(current_founder)):
    """Founder onboards a seller. The password is returned once, to pass on to the seller."""
    email = payload.email.strip().lower()
    name = payload.name.strip()
    if not name or "@" not in email:
        raise HTTPException(status_code=400, detail="Business name and a valid email are required")
    if email_taken(db, email):
        raise HTTPException(status_code=409, detail="An account with this email already exists")
    slug = None
    if (payload.slug or "").strip():
        try:
            slug = check_wanted(db, payload.slug)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    password = (payload.password or "").strip() or secrets.token_urlsafe(9)
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    business = models.Business(name=name, owner_email=email, password_hash=hash_password(password),
                               phone=(payload.phone or "").strip() or None, slug=slug)
    billing.start_trial(business, "founder")
    db.add(business)
    db.commit()
    db.refresh(business)
    ensure_slug(db, business)
    audit(db, founder, "add business", business.id, f"{name} <{email}>", client_ip(request))
    return {"business": _row(db, business, datetime.now(timezone.utc)), "login_email": email, "password": password}


class BusinessStatus(BaseModel):
    is_active: bool


@router.patch("/businesses/{business_id}")
def set_status(business_id: int, payload: BusinessStatus, request: Request, db: Session = Depends(get_db),
               founder: models.FounderUser = Depends(current_founder)):
    business = db.get(models.Business, business_id)
    if not business:
        raise HTTPException(status_code=404, detail="Business not found")
    if business.id == DEFAULT_BUSINESS_ID and not payload.is_active:
        raise HTTPException(status_code=400, detail="The founder's own shop can't be paused")
    business.is_active = payload.is_active
    business.paused_reason = None if payload.is_active else "founder"
    db.commit()
    audit(db, founder, "resume" if payload.is_active else "pause", business.id, None, client_ip(request))
    return _row(db, business, datetime.now(timezone.utc))


class AddressChange(BaseModel):
    slug: str


@router.patch("/businesses/{business_id}/address")
def change_address(business_id: int, payload: AddressChange, request: Request, db: Session = Depends(get_db),
                   founder: models.FounderUser = Depends(current_founder)):
    business = db.get(models.Business, business_id)
    if not business:
        raise HTTPException(status_code=404, detail="Business not found")
    try:
        slug = check_wanted(db, payload.slug, business.id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    old, business.slug = business.slug, slug
    db.commit()
    audit(db, founder, "web address", business.id, f"{old} -> {slug}", client_ip(request))
    return _row(db, business, datetime.now(timezone.utc))


@router.post("/businesses/{business_id}/open")
def open_dashboard(business_id: int, request: Request, db: Session = Depends(get_db),
                   founder: models.FounderUser = Depends(current_founder)):
    """A short-lived owner login for that business, so the founder can see and fix its setup."""
    business = db.get(models.Business, business_id)
    if not business:
        raise HTTPException(status_code=404, detail="Business not found")
    if not business.is_active and business.paused_reason != "billing":
        raise HTTPException(status_code=400, detail="Resume the business first")
    token, expires_at = issue_token(business.id, ttl=8 * 3600)
    audit(db, founder, "support login", business.id, "opened their dashboard (8h)", client_ip(request))
    return {"token": token, "expires_at": expires_at, "business_id": business.id, "business_name": business.name}
