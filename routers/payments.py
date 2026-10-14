import razorpay
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session, joinedload

import models
import schemas
from auth import current_business
from business import DEFAULT_BUSINESS_ID, razorpay_creds, whatsapp_creds
from database import get_db
from routers.invoices import invoice_url
from routers.orders import mark_order_paid
from whatsapp import WhatsAppSendError, send_message

router = APIRouter(prefix="/payments", tags=["payments"])


def _client(business: models.Business) -> razorpay.Client:
    creds = razorpay_creds(business)
    if not creds.key_id or not creds.key_secret:
        raise HTTPException(status_code=503, detail="Razorpay is not connected for this business")
    return razorpay.Client(auth=(creds.key_id, creds.key_secret))


@router.post("/{order_id}/link", response_model=schemas.PaymentLinkOut)
def create_payment_link(order_id: int, db: Session = Depends(get_db), business: models.Business = Depends(current_business)):
    order = (
        db.query(models.Order)
        .options(joinedload(models.Order.customer))
        .filter(models.Order.id == order_id, models.Order.business_id == business.id)
        .first()
    )
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.is_paid:
        raise HTTPException(status_code=400, detail="Order is already paid")

    ensure_payment_link(db, business, order)
    return {"order_id": order.id, "payment_link_url": order.payment_link_url}


def ensure_payment_link(db: Session, business: models.Business, order: models.Order) -> str:
    """Return the order's Razorpay hosted payment link, creating it on first call. Raises
    HTTPException(503) if Razorpay isn't connected for this business."""
    # Idempotent: reuse the existing link instead of generating a new one on every call.
    if order.razorpay_payment_link_id and order.razorpay_link_url:
        return order.razorpay_link_url

    client = _client(business)
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
    order.razorpay_link_url = link["short_url"]
    # Keep the pay page if the customer was already sent there; it links to this as "More options".
    if not order.payment_link_url:
        order.payment_link_url = link["short_url"]
    db.commit()
    return order.razorpay_link_url


@router.post("/webhook/razorpay")
async def razorpay_webhook_legacy(request: Request, db: Session = Depends(get_db)):
    """Original single-business webhook URL - kept so an existing Razorpay setup keeps working."""
    return await razorpay_webhook(DEFAULT_BUSINESS_ID, request, db)


@router.post("/webhook/razorpay/{business_id}")
async def razorpay_webhook(business_id: int, request: Request, db: Session = Depends(get_db)):
    business = db.get(models.Business, business_id)
    if not business:
        raise HTTPException(status_code=404, detail="Unknown business")
    webhook_secret = razorpay_creds(business).webhook_secret
    if not webhook_secret:
        raise HTTPException(status_code=503, detail="Razorpay webhook secret is not configured")

    raw_body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")

    try:
        razorpay.Utility().verify_webhook_signature(raw_body.decode(), signature, webhook_secret)
    except razorpay.errors.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    event = await request.json()
    kind = event.get("event")
    # Row lock (Postgres) so two near-simultaneous deliveries of the same event can't both
    # pass the is_paid check below. SQLite ignores FOR UPDATE, which is fine for local dev.
    orders = db.query(models.Order).filter(models.Order.business_id == business.id).with_for_update()
    if kind == "payment_link.paid":  # paid through a Razorpay payment link
        try:
            order_id = int(event["payload"]["payment_link"]["entity"]["reference_id"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Missing or invalid reference_id")
        order = orders.filter(models.Order.id == order_id).first()
    elif kind == "order.paid":  # paid from a UPI app button on our pay page
        rp_order_id = (event.get("payload", {}).get("order", {}).get("entity") or {}).get("id")
        if not rp_order_id:
            raise HTTPException(status_code=400, detail="Missing order id")
        order = orders.filter(models.Order.razorpay_order_id == rp_order_id).first()
        if not order:
            # A payment link makes its own Razorpay order, so link payments also fire order.paid.
            # payment_link.paid handles those - answer 200 so Razorpay doesn't retry or disable the hook.
            return {"status": "ignored"}
    else:
        return {"status": "ignored"}
    payment_entity = event["payload"]["payment"]["entity"]
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    # Idempotent: Razorpay can retry the same event; never double-charge stock.
    if order.is_paid:
        return {"status": "already processed"}

    paid_amount = payment_entity["amount"] / 100
    if abs(paid_amount - order.total_amount) > 0.01:
        raise HTTPException(status_code=400, detail="Paid amount does not match order total")

    mark_order_paid(db, order, payment_entity["id"])
    db.commit()
    await notify_payment_received(business, order)
    return {"status": "ok"}


async def notify_payment_received(business: models.Business, order: models.Order):
    """Thank the customer once a payment is confirmed automatically, with the invoice, and give the
    seller a heads-up to pack it. Outside the 24h window (e.g. ordered from a shared shop link) the
    approved order_update template is used instead."""
    from database import SessionLocal
    from notifications import notify_order_update, within_service_window
    from seller_alerts import notify_seller

    with SessionLocal() as db:
        order = db.get(models.Order, order.id)
        business = db.get(models.Business, business.id)
        conversation = db.query(models.Conversation).filter(
            models.Conversation.business_id == business.id, models.Conversation.phone == order.customer.phone
        ).first()
        text = (f"✅ Payment received for Order #{order.id} (₹{order.total_amount:g}). Thank you! "
                f"We'll let you know when it ships.\n🧾 Invoice: {invoice_url(order.id)}")
        if within_service_window(conversation):
            try:
                await send_message(whatsapp_creds(business), order.customer.phone, text)
                db.add(models.Message(conversation_id=conversation.id, sender="system", text=text))
                db.commit()
            except WhatsAppSendError as e:
                # Payment is already recorded - a failed confirmation message shouldn't fail the webhook.
                print("PAYMENT CONFIRMATION NOT DELIVERED:", e)
        else:
            await notify_order_update(db, business, order,
                                      f"Payment received, thank you! Invoice: {invoice_url(order.id)}")
        items = ", ".join(f"{i.quantity} × {i.display_name}" for i in order.items)
        await notify_seller(db, business, f"💰 Order #{order.id} PAID ₹{order.total_amount:g} - {order.customer.name}: "
                                          f"{items}. Ready to pack 📦")
