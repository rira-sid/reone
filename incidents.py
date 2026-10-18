"""Customer issues (incidents): damaged or wrong item, payment problem, refund, late delivery...

The customer gets one personal note with the number (#INC-<id>); the owner gets an URGENT alert on
WhatsApp (swipe-reply answers the customer) and the issue on the dashboard's Issues page. Until the
owner resolves it, only the owner replies in that chat - later messages and photos are added to the
same incident. Resolutions favour the customer: refund, free replacement, or a discount on the next
order. Not handled in 10 minutes -> second alert; after 1 hour it shows red on the founder site."""
from datetime import datetime, timedelta, timezone

import models
from business import whatsapp_creds
from seller_alerts import _send_alert, _who
from whatsapp import WhatsAppSendError, send_message

REALERT_AFTER = timedelta(minutes=10)
FOUNDER_OVERDUE_AFTER = timedelta(hours=1)
CATEGORY_LABEL = {
    "damaged": "Damaged item", "wrong_item": "Wrong item", "missing": "Missing item", "late_delivery": "Late / not delivered",
    "payment": "Payment problem", "refund": "Refund / return", "other": "Complaint",
}


def _utc(value):
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _note(reply: str | None, incident: models.Incident, conversation: models.Conversation) -> str:
    """The AI's personal note in the customer's language, with the incident number filled in."""
    number = f"#{incident.number}"
    if reply and "{INC}" in reply:
        return reply.replace("{INC}", number)
    name = f", {conversation.customer_name}" if conversation.customer_name else ""
    return f"So sorry about this{name} 🙏 I've noted it as {number} - you'll hear back on this very soon."


async def _alert(db, business: models.Business, incident: models.Incident, text: str, prefix: str = "🔴 URGENT"):
    conversation = incident.conversation
    order = f", Order #{incident.order_id}" if incident.order_id else ""
    body = (f"{prefix} #{incident.number} · {CATEGORY_LABEL.get(incident.category, 'Issue')}\n"
            f"{_who(conversation)} (+{conversation.phone}){order}:\n\"{text}\"\n\n"
            "↩️ Swipe-reply to answer them. Resolve it on the dashboard → Issues.")
    await _send_alert(db, business, conversation, body, text)


async def open_incident(db, business: models.Business, conversation: models.Conversation, text: str, category: str,
                        summary: str, order: models.Order | None, lang: str | None, reply: str | None = None) -> models.Incident:
    incident = models.Incident(business_id=business.id, conversation_id=conversation.id,
                               order_id=order.id if order else None,
                               category=category if category in CATEGORY_LABEL else "other", summary=summary[:500])
    db.add(incident)
    db.commit()
    db.refresh(incident)
    note = _note(reply, incident, conversation)
    db.add(models.Message(conversation_id=conversation.id, sender="ai", text=note))
    db.commit()
    try:
        await send_message(whatsapp_creds(business), conversation.phone, note)
    except WhatsAppSendError as e:
        print("INCIDENT NOTE NOT DELIVERED:", e)
    await _alert(db, business, incident, text)
    return incident


async def add_to_incident(db, business: models.Business, incident: models.Incident, text: str):
    """More messages / photos about an open issue: tell the owner, no reply to the customer."""
    incident.updated_at = datetime.now(timezone.utc)
    db.commit()
    await _alert(db, business, incident, text, prefix="➕ Update on")


async def escalate(db, now: datetime | None = None) -> int:
    """Second alert after 10 minutes if the owner hasn't started on it."""
    now = now or datetime.now(timezone.utc)
    done = 0
    for incident in db.query(models.Incident).filter(models.Incident.status == "open",
                                                     models.Incident.alert_stage < 2).all():
        if now - _utc(incident.created_at) < REALERT_AFTER:
            continue
        incident.alert_stage = 2
        db.commit()
        business = db.get(models.Business, incident.business_id)
        await _alert(db, business, incident, incident.summary or "", prefix="⏰ STILL OPEN (10 min)")
        done += 1
    return done


def overdue(incident: models.Incident, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    return incident.status != "resolved" and now - _utc(incident.created_at) >= FOUNDER_OVERDUE_AFTER


def incident_dict(incident: models.Incident) -> dict:
    c = incident.conversation
    order = incident.order
    return {
        "id": incident.id, "number": incident.number, "category": incident.category,
        "category_label": CATEGORY_LABEL.get(incident.category, "Issue"), "summary": incident.summary,
        "status": incident.status, "resolution": incident.resolution, "resolution_note": incident.resolution_note,
        "refund_amount": incident.refund_amount, "replacement_order_id": incident.replacement_order_id,
        "discount_amount": incident.discount_amount, "order_id": incident.order_id,
        "conversation_id": incident.conversation_id, "customer_name": c.customer_name if c else None,
        "phone": c.phone if c else None, "created_at": _utc(incident.created_at),
        "updated_at": _utc(incident.updated_at), "resolved_at": _utc(incident.resolved_at),
        "overdue": overdue(incident),
        # What the Issues page needs to offer the right fixes (a refund only on a paid order).
        "order_total": order.total_amount if order else None,
        "order_paid": bool(order and order.is_paid),
        "order_paid_online": bool(order and order.razorpay_payment_id),
    }
