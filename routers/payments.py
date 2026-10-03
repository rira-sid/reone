import os

import razorpay
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session, joinedload

import models
import schemas
from database import get_db
from business import DEFAULT_BUSINESS_ID

RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET")

router = APIRouter(prefix="/payments", tags=["payments"])


def _client() -> razorpay.Client:
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        raise HTTPException(status_code=503, detail="Razorpay is not configured on this server")
    return razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))


@router.post("/{order_id}/link", response_model=schemas.PaymentLinkOut)
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

    # Idempotent: reuse the existing link instead of generating a new one on every call.
    if order.payment_link_url:
        return {"order_id": order.id, "payment_link_url": order.payment_link_url}

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

    return {"order_id": order.id, "payment_link_url": order.payment_link_url}


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

    order = (
        db.query(models.Order)
        .options(joinedload(models.Order.items))
        .filter(models.Order.id == order_id, models.Order.business_id == DEFAULT_BUSINESS_ID)
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
    return {"status": "ok"}
