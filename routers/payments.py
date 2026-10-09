import os

import razorpay
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session, joinedload

import models
import schemas
from auth import require_auth
from database import get_db
from business import DEFAULT_BUSINESS_ID
from whatsapp import WhatsAppSendError, send_message

RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET")

router = APIRouter(prefix="/payments", tags=["payments"])


def _client() -> razorpay.Client:
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        raise HTTPException(status_code=503, detail="Razorpay is not configured on this server")
    return razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))


@router.post("/{order_id}/link", response_model=schemas.PaymentLinkOut, dependencies=[Depends(require_auth)])
def create_payment_link(order_id: int, db: Session = Depends(get_db)):
    order = (
        db.query(models.Order)
        .options(joinedload(models.Order.customer))
        .filter(models.Order.id == order_id, models.Order.business_id == DEFAULT_BUSINESS_ID)
        .first()
    )
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.is_paid:
        raise HTTPException(status_code=400, detail="Order is already paid")

    return {"order_id": order.id, "payment_link_url": ensure_payment_link(db, order)}


def ensure_payment_link(db: Session, order: models.Order) -> str:
    """Return the order's Razorpay link, creating it on first call. Raises HTTPException(503)
    if Razorpay isn't configured on this server."""
    # Idempotent: reuse the existing link instead of generating a new one on every call.
    if order.payment_link_url:
        return order.payment_link_url

    client = _client()
    link = client.payment_link.create({
        "amount": round(order.total_amount * 100),
        "currency": "INR",
        "accept_partial": False,
        "description": f"Order #{order.id}",
        "customer": {
            "name": order.customer.name,
            "contact": order.customer.phone,
        },
        "notify": {"sms": False, "email": False},
        "reference_id": str(order.id),
    })

    order.razorpay_payment_link_id = link["id"]
    order.payment_link_url = link["short_url"]
    db.commit()
    return order.payment_link_url


@router.post("/webhook/razorpay")
async def razorpay_webhook(request: Request, db: Session = Depends(get_db)):
    if not RAZORPAY_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="Razorpay webhook secret is not configured")

    raw_body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")

    try:
        razorpay.Utility().verify_webhook_signature(raw_body.decode(), signature, RAZORPAY_WEBHOOK_SECRET)
    except razorpay.errors.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    event = await request.json()
    if event.get("event") != "payment_link.paid":
        return {"status": "ignored"}

    link_entity = event["payload"]["payment_link"]["entity"]
    payment_entity = event["payload"]["payment"]["entity"]

    try:
        order_id = int(link_entity["reference_id"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Missing or invalid reference_id")

    # Row lock (Postgres) so two near-simultaneous deliveries of the same event can't both
    # pass the is_paid check below. SQLite ignores FOR UPDATE, which is fine for local dev.
    order = (
        db.query(models.Order)
        .filter(models.Order.id == order_id, models.Order.business_id == DEFAULT_BUSINESS_ID)
        .with_for_update()
        .first()
    )
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    # Idempotent: Razorpay can retry the same event; never double-charge stock.
    if order.is_paid:
        return {"status": "already processed"}

    paid_amount = payment_entity["amount"] / 100
    if abs(paid_amount - order.total_amount) > 0.01:
        raise HTTPException(status_code=400, detail="Paid amount does not match order total")

    order.is_paid = True
    order.status = "Confirmed"
    order.razorpay_payment_id = payment_entity["id"]

    for item in order.items:
        product = db.query(models.Product).filter(models.Product.id == item.product_id).first()
        if product:
            product.stock = max(0, product.stock - item.quantity)

    db.commit()

    try:
        await send_message(
            order.customer.phone,
            f"Payment received for Order #{order.id} (₹{order.total_amount:g}). Thank you! "
            "We'll let you know when it ships.",
        )
    except WhatsAppSendError as e:
        # Payment is already recorded - a failed confirmation message shouldn't fail the webhook.
        print("PAYMENT CONFIRMATION NOT DELIVERED:", e)
    return {"status": "ok"}
