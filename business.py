"""Per-business account lookups.

Business 1 is the original single-tenant business: when its own settings are empty it falls
back to the server's env vars, so the existing deployment keeps working unchanged.
"""
import os
from dataclasses import dataclass

import models
from secrets_box import decrypt

DEFAULT_BUSINESS_ID = 1


@dataclass
class WhatsAppCreds:
    phone_number_id: str
    token: str


@dataclass
class RazorpayCreds:
    key_id: str | None
    key_secret: str | None
    webhook_secret: str | None


def whatsapp_creds(business: models.Business) -> WhatsAppCreds | None:
    phone_number_id, token = business.wa_phone_number_id, decrypt(business.wa_token_enc)
    if business.id == DEFAULT_BUSINESS_ID:
        phone_number_id = phone_number_id or os.getenv("WHATSAPP_PHONE_NUMBER_ID")
        token = token or os.getenv("WHATSAPP_TOKEN")
    if not phone_number_id or not token:
        return None
    return WhatsAppCreds(phone_number_id, token)


def razorpay_creds(business: models.Business) -> RazorpayCreds:
    creds = RazorpayCreds(
        business.razorpay_key_id,
        decrypt(business.razorpay_key_secret_enc),
        decrypt(business.razorpay_webhook_secret_enc),
    )
    if business.id == DEFAULT_BUSINESS_ID:
        creds.key_id = creds.key_id or os.getenv("RAZORPAY_KEY_ID")
        creds.key_secret = creds.key_secret or os.getenv("RAZORPAY_KEY_SECRET")
        creds.webhook_secret = creds.webhook_secret or os.getenv("RAZORPAY_WEBHOOK_SECRET")
    return creds


def business_for_phone_number_id(db, phone_number_id: str | None) -> models.Business | None:
    """Which seller an incoming WhatsApp message was sent to."""
    if not phone_number_id:
        return None
    business = db.query(models.Business).filter(models.Business.wa_phone_number_id == phone_number_id).first()
    if business:
        return business
    if phone_number_id == os.getenv("WHATSAPP_PHONE_NUMBER_ID"):
        return db.get(models.Business, DEFAULT_BUSINESS_ID)
    return None
