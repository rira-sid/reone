"""Issues page: customer problems (incidents) and how the shop makes them right.
Resolutions favour the customer - full/partial refund, free replacement, or a discount on the next order -
and each one can message the customer automatically."""
from datetime import datetime, timezone

import razorpay
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

import incidents
import models
from auth import current_business
from business import razorpay_creds, whatsapp_creds
from database import get_db
from notifications import within_service_window
from routers.orders import reserve_stock
from whatsapp import WhatsAppSendError, send_message

router = APIRouter(prefix="/issues", tags=["issues"])


def _get(db: Session, business: models.Business, incident_id: int) -> models.Incident:
    incident = db.query(models.Incident).filter(models.Incident.id == incident_id,
                                                models.Incident.business_id == business.id).first()
    if not incident:
        raise HTTPException(status_code=404, detail="Issue not found")
    return incident


@router.get("")
def list_issues(status: str | None = None, db: Session = Depends(get_db),
                business: models.Business = Depends(current_business)):
    q = db.query(models.Incident).filter(models.Incident.business_id == business.id)
    if status in models.INCIDENT_STATUSES:
        q = q.filter(models.Incident.status == status)
    rows = q.order_by(models.Incident.id.desc()).limit(200).all()
    return [incidents.incident_dict(i) for i in rows]


class StatusIn(BaseModel):
    status: str


@router.patch("/{incident_id}")
def set_status(incident_id: int, payload: StatusIn, db: Session = Depends(get_db),
               business: models.Business = Depends(current_business)):
    incident = _get(db, business, incident_id)
    if payload.status not in models.INCIDENT_STATUSES:
        raise HTTPException(status_code=400, detail="Status must be open, in_progress or resolved")
    incident.status = payload.status
    incident.resolved_at = datetime.now(timezone.utc) if payload.status == "resolved" else None
    db.commit()
    return incidents.incident_dict(incident)


class ResolveIn(BaseModel):
    action: str  # refund | partial_refund | replacement | discount | other
    amount: float | None = None
    note: str | None = None
    notify: bool = True


async def _tell_customer(db: Session, business: models.Business, incident: models.Incident, text: str) -> bool:
    conversation = incident.conversation
    if not within_service_window(conversation):
        return False
    try:
        await send_message(whatsapp_creds(business), conversation.phone, text)
    except WhatsAppSendError as e:
        print("ISSUE UPDATE NOT DELIVERED:", e)
        return False
    db.add(models.Message(conversation_id=conversation.id, sender="seller", text=text))
    db.commit()
    return True


@router.post("/{incident_id}/resolve")
async def resolve(incident_id: int, payload: ResolveIn, db: Session = Depends(get_db),
                  business: models.Business = Depends(current_business)):
    incident = _get(db, business, incident_id)
    order = db.get(models.Order, incident.order_id) if incident.order_id else None
    number = f"#{incident.number}"
    if payload.action in ("refund", "partial_refund"):
        if not order or not order.is_paid:
            raise HTTPException(status_code=400, detail="There's no paid order on this issue to refund")
        amount = order.total_amount if payload.action == "refund" else (payload.amount or 0)
        if not 0 < amount <= order.total_amount:
            raise HTTPException(status_code=400, detail="Refund amount must be more than ₹0 and at most the order total")
        if order.razorpay_payment_id:
            creds = razorpay_creds(business)
            try:
                razorpay.Client(auth=(creds.key_id, creds.key_secret)).payment.refund(
                    order.razorpay_payment_id, {"amount": round(amount * 100), "notes": {"issue": incident.number}})
            except Exception as e:  # noqa: BLE001 - Razorpay rejected it; tell the seller why
                raise HTTPException(status_code=502, detail=f"Razorpay couldn't refund: {e}") from e
            how = "It will reach your account in 5-7 working days."
        else:
            how = "We'll send it to you directly."  # COD / manual payment - the seller pays back by hand
        incident.refund_amount = amount
        text = f"Update on {number}: we've refunded ₹{amount:g} for order #{order.id}. {how} Sorry again for the trouble 🙏"
    elif payload.action == "replacement":
        if not order:
            raise HTTPException(status_code=400, detail="There's no order on this issue to replace")
        replacement = models.Order(business_id=business.id, customer_id=order.customer_id, status="Confirmed",
                                   payment_method="online", is_paid=True, paid_at=datetime.now(timezone.utc),
                                   total_amount=0.0, customer_note=f"Free replacement for {number} (order #{order.id})")
        for item in order.items:
            replacement.items.append(models.OrderItem(product_id=item.product_id, quantity=item.quantity,
                                                      price_at_order=0.0, variant_id=item.variant_id,
                                                      variant_label=item.variant_label))
        db.add(replacement)
        db.flush()
        reserve_stock(db, replacement)
        incident.replacement_order_id = replacement.id
        text = f"Update on {number}: we're sending you a free replacement (order #{replacement.id}). Sorry again for the trouble 🙏"
    elif payload.action == "discount":
        if not payload.amount or payload.amount <= 0:
            raise HTTPException(status_code=400, detail="Enter the discount amount")
        db.add(models.CustomerCredit(business_id=business.id, phone=incident.conversation.phone, amount=payload.amount,
                                     incident_id=incident.id))
        incident.discount_amount = payload.amount
        text = (f"Update on {number}: we've added a ₹{payload.amount:g} discount for you - it comes off your next "
                f"order automatically. Sorry again for the trouble 🙏")
    elif payload.action == "other":
        text = f"Update on {number}: {payload.note.strip()}" if payload.note and payload.note.strip() else ""
    else:
        raise HTTPException(status_code=400, detail="Unknown action")
    incident.resolution = payload.action
    incident.resolution_note = (payload.note or "").strip() or None
    incident.status = "resolved"
    incident.resolved_at = datetime.now(timezone.utc)
    db.commit()
    notified = await _tell_customer(db, business, incident, text) if payload.notify and text else False
    return {**incidents.incident_dict(incident), "customer_notified": notified}
