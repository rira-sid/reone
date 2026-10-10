from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

import models
from auth import current_business, email_taken, hash_password, owner_business, verify_password
import re

from business import razorpay_creds, whatsapp_creds
from routers.pay import PAYMENT_METHODS, effective_payment_method, method_ready
from database import get_db
from secrets_box import encrypt

router = APIRouter(prefix="/business", tags=["business"])


class BusinessOut(BaseModel):
    id: int
    name: str
    owner_email: str | None
    phone: str | None
    address: str | None
    gstin: str | None
    wa_phone_number_id: str | None
    razorpay_key_id: str | None
    accepting_orders: bool
    closed_message: str | None
    cod_enabled: bool
    delivery_fee: float
    free_delivery_above: float | None
    min_order_amount: float | None
    catalog_path: str
    # Secrets are write-only: the dashboard only learns whether they're set.
    whatsapp_connected: bool
    razorpay_connected: bool
    razorpay_webhook_ready: bool
    razorpay_webhook_url_path: str
    payment_method: str | None  # what the seller picked
    active_payment_method: str | None  # what customers actually get right now
    upi_id: str | None
    upi_name: str | None
    wa_payment_config: str | None


class BusinessUpdate(BaseModel):
    name: str | None = None
    phone: str | None = None
    address: str | None = None
    gstin: str | None = None
    wa_phone_number_id: str | None = None
    wa_token: str | None = None
    razorpay_key_id: str | None = None
    razorpay_key_secret: str | None = None
    razorpay_webhook_secret: str | None = None
    accepting_orders: bool | None = None
    closed_message: str | None = None
    cod_enabled: bool | None = None
    delivery_fee: float | None = None
    free_delivery_above: float | None = None
    min_order_amount: float | None = None
    payment_method: str | None = None
    upi_id: str | None = None
    upi_name: str | None = None
    wa_payment_config: str | None = None


# name@bank - letters, digits, dot, dash, underscore before the @, letters after it.
UPI_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,256}@[A-Za-z][A-Za-z0-9.-]{1,63}$")


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


def _out(business: models.Business) -> dict:
    rp = razorpay_creds(business)
    return {
        "id": business.id,
        "name": business.name,
        "owner_email": business.owner_email,
        "phone": business.phone,
        "address": business.address,
        "gstin": business.gstin,
        "wa_phone_number_id": business.wa_phone_number_id,
        "razorpay_key_id": business.razorpay_key_id,
        "accepting_orders": business.accepting_orders,
        "closed_message": business.closed_message,
        "cod_enabled": business.cod_enabled,
        "delivery_fee": business.delivery_fee or 0.0,
        "free_delivery_above": business.free_delivery_above,
        "min_order_amount": business.min_order_amount,
        "catalog_path": f"/shop/{business.id}",
        "whatsapp_connected": whatsapp_creds(business) is not None,
        "razorpay_connected": bool(rp.key_id and rp.key_secret),
        "razorpay_webhook_ready": bool(rp.webhook_secret),
        "razorpay_webhook_url_path": f"/payments/webhook/razorpay/{business.id}",
        "payment_method": business.payment_method,
        "active_payment_method": effective_payment_method(business),
        "upi_id": business.upi_id,
        "upi_name": business.upi_name,
        "wa_payment_config": business.wa_payment_config,
    }


@router.get("", response_model=BusinessOut)
def get_business(business: models.Business = Depends(current_business)):
    return _out(business)


@router.patch("", response_model=BusinessOut)
def update_business(
    payload: BusinessUpdate, business: models.Business = Depends(owner_business), db: Session = Depends(get_db)
):
    data = payload.model_dump(exclude_unset=True)
    for field in ("name", "phone", "address", "gstin", "razorpay_key_id"):
        if field in data:
            value = (data[field] or "").strip() or None
            if field == "name" and not value:
                raise HTTPException(status_code=400, detail="Business name can't be empty")
            setattr(business, field, value)

    for field in ("accepting_orders", "cod_enabled"):
        if data.get(field) is not None:
            setattr(business, field, data[field])
    if "closed_message" in data:
        business.closed_message = (data["closed_message"] or "").strip() or None
    if "delivery_fee" in data:
        business.delivery_fee = max(0.0, data["delivery_fee"] or 0.0)
    for field in ("free_delivery_above", "min_order_amount"):
        if field in data:
            value = data[field]
            setattr(business, field, value if value and value > 0 else None)

    if "wa_phone_number_id" in data:
        phone_number_id = (data["wa_phone_number_id"] or "").strip() or None
        if phone_number_id:
            taken = db.query(models.Business).filter(
                models.Business.wa_phone_number_id == phone_number_id, models.Business.id != business.id
            ).first()
            if taken:
                raise HTTPException(status_code=409, detail="This WhatsApp number is already connected to another business")
        business.wa_phone_number_id = phone_number_id

    # Blank secret fields mean "leave unchanged" - the dashboard never sees the stored value.
    for field, column in (
        ("wa_token", "wa_token_enc"),
        ("razorpay_key_secret", "razorpay_key_secret_enc"),
        ("razorpay_webhook_secret", "razorpay_webhook_secret_enc"),
    ):
        value = (data.get(field) or "").strip()
        if value:
            setattr(business, column, encrypt(value))

    for field in ("upi_name", "wa_payment_config"):
        if field in data:
            setattr(business, field, (data[field] or "").strip() or None)
    if "upi_id" in data:
        upi_id = (data["upi_id"] or "").strip() or None
        if upi_id and not UPI_ID_RE.match(upi_id):
            raise HTTPException(status_code=400, detail="That doesn't look like a UPI ID (it should look like name@bank)")
        business.upi_id = upi_id
    if "payment_method" in data:
        method = data["payment_method"] or None
        if method and method not in PAYMENT_METHODS:
            raise HTTPException(status_code=400, detail="Unknown payment method")
        if method and not method_ready(business, method):
            missing = {"upi": "your UPI ID", "razorpay_link": "your Razorpay keys",
                       "whatsapp_pay": "the WhatsApp payment configuration name"}[method]
            raise HTTPException(status_code=400, detail=f"Add {missing} before choosing this payment method")
        business.payment_method = method

    db.commit()
    db.refresh(business)
    return _out(business)


@router.post("/password")
def change_password(
    payload: PasswordChange, business: models.Business = Depends(owner_business), db: Session = Depends(get_db)
):
    if not business.owner_email:
        raise HTTPException(status_code=400, detail="Set an email for this business first")
    if business.password_hash and not verify_password(payload.current_password, business.password_hash):
        raise HTTPException(status_code=401, detail="Current password is wrong")
    if len(payload.new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    business.password_hash = hash_password(payload.new_password)
    db.commit()
    return {"status": "ok"}


class EmailUpdate(BaseModel):
    email: str


@router.post("/email")
def set_email(payload: EmailUpdate, business: models.Business = Depends(owner_business), db: Session = Depends(get_db)):
    """Lets the original business (which logs in with DASHBOARD_PASSWORD) add an email login."""
    email = payload.email.strip().lower()
    if "@" not in email:
        raise HTTPException(status_code=400, detail="Enter a valid email")
    if email_taken(db, email, exclude_business_id=business.id):
        raise HTTPException(status_code=409, detail="An account with this email already exists")
    business.owner_email = email
    db.commit()
    return {"status": "ok"}


# ---- staff logins (owner only) ---------------------------------------------------------

class StaffIn(BaseModel):
    name: str
    email: str
    password: str


class StaffOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    email: str


class StaffPassword(BaseModel):
    password: str


@router.get("/staff", response_model=list[StaffOut])
def list_staff(business: models.Business = Depends(owner_business), db: Session = Depends(get_db)):
    return db.query(models.StaffUser).filter(models.StaffUser.business_id == business.id).order_by(models.StaffUser.id).all()


@router.post("/staff", response_model=StaffOut)
def add_staff(payload: StaffIn, business: models.Business = Depends(owner_business), db: Session = Depends(get_db)):
    email = payload.email.strip().lower()
    if "@" not in email or not payload.name.strip():
        raise HTTPException(status_code=400, detail="Name and a valid email are required")
    if len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    if email_taken(db, email):
        raise HTTPException(status_code=409, detail="An account with this email already exists")
    staff = models.StaffUser(business_id=business.id, name=payload.name.strip(), email=email,
                             password_hash=hash_password(payload.password))
    db.add(staff)
    db.commit()
    db.refresh(staff)
    return staff


def _get_staff(db: Session, business: models.Business, staff_id: int) -> models.StaffUser:
    staff = db.query(models.StaffUser).filter(
        models.StaffUser.id == staff_id, models.StaffUser.business_id == business.id
    ).first()
    if not staff:
        raise HTTPException(status_code=404, detail="Staff member not found")
    return staff


@router.post("/staff/{staff_id}/password")
def reset_staff_password(staff_id: int, payload: StaffPassword, business: models.Business = Depends(owner_business),
                         db: Session = Depends(get_db)):
    if len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    _get_staff(db, business, staff_id).password_hash = hash_password(payload.password)
    db.commit()
    return {"status": "ok"}


@router.delete("/staff/{staff_id}")
def remove_staff(staff_id: int, business: models.Business = Depends(owner_business), db: Session = Depends(get_db)):
    db.delete(_get_staff(db, business, staff_id))
    db.commit()
    return {"status": "deleted"}
