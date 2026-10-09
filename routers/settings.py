from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

import models
from auth import current_business, hash_password, verify_password
from business import razorpay_creds, whatsapp_creds
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
    # Secrets are write-only: the dashboard only learns whether they're set.
    whatsapp_connected: bool
    razorpay_connected: bool
    razorpay_webhook_ready: bool
    razorpay_webhook_url_path: str


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
        "whatsapp_connected": whatsapp_creds(business) is not None,
        "razorpay_connected": bool(rp.key_id and rp.key_secret),
        "razorpay_webhook_ready": bool(rp.webhook_secret),
        "razorpay_webhook_url_path": f"/payments/webhook/razorpay/{business.id}",
    }


@router.get("", response_model=BusinessOut)
def get_business(business: models.Business = Depends(current_business)):
    return _out(business)


@router.patch("", response_model=BusinessOut)
def update_business(
    payload: BusinessUpdate, business: models.Business = Depends(current_business), db: Session = Depends(get_db)
):
    data = payload.model_dump(exclude_unset=True)
    for field in ("name", "phone", "address", "gstin", "razorpay_key_id"):
        if field in data:
            value = (data[field] or "").strip() or None
            if field == "name" and not value:
                raise HTTPException(status_code=400, detail="Business name can't be empty")
            setattr(business, field, value)

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

    db.commit()
    db.refresh(business)
    return _out(business)


@router.post("/password")
def change_password(
    payload: PasswordChange, business: models.Business = Depends(current_business), db: Session = Depends(get_db)
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
def set_email(payload: EmailUpdate, business: models.Business = Depends(current_business), db: Session = Depends(get_db)):
    """Lets the original business (which logs in with DASHBOARD_PASSWORD) add an email login."""
    email = payload.email.strip().lower()
    if "@" not in email:
        raise HTTPException(status_code=400, detail="Enter a valid email")
    taken = db.query(models.Business).filter(
        func.lower(models.Business.owner_email) == email, models.Business.id != business.id
    ).first()
    if taken:
        raise HTTPException(status_code=409, detail="An account with this email already exists")
    business.owner_email = email
    db.commit()
    return {"status": "ok"}
