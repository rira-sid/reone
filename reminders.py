"""One gentle reminder for orders left unpaid, sent while WhatsApp's 24h window is still open
(the reminder is free-form text, so it can't go out after the window closes)."""
import asyncio
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import joinedload

import models
from business import whatsapp_creds
from database import SessionLocal
from notifications import within_service_window
from whatsapp import WhatsAppSendError, send_message

REMIND_AFTER = timedelta(hours=float(os.getenv("PAYMENT_REMINDER_AFTER_HOURS", "2")))
CHECK_EVERY_SECONDS = 600


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


async def send_due_reminders(now: datetime | None = None) -> int:
    """Send reminders that are due; returns how many went out."""
    now = now or datetime.now(timezone.utc)
    sent = 0
    with SessionLocal() as db:
        orders = (
            db.query(models.Order)
            .options(joinedload(models.Order.customer), joinedload(models.Order.business))
            .filter(
                models.Order.is_paid.is_(False),
                models.Order.payment_link_url.isnot(None),
                models.Order.payment_reminder_sent_at.is_(None),
                models.Order.status != "Cancelled",
            )
            .all()
        )
        for order in orders:
            if not order.created_at or now - _utc(order.created_at) < REMIND_AFTER:
                continue
            conversation = db.query(models.Conversation).filter(
                models.Conversation.business_id == order.business_id,
                models.Conversation.phone == order.customer.phone,
            ).first()
            # Mark it either way: one attempt per order, never a stream of nags.
            order.payment_reminder_sent_at = now
            db.commit()
            if not within_service_window(conversation):
                continue
            text = (f"Just a reminder - Order #{order.id} (₹{order.total_amount:g}) is waiting for payment. "
                    f"Pay here: {order.payment_link_url}")
            try:
                await send_message(whatsapp_creds(order.business), order.customer.phone, text)
            except WhatsAppSendError as e:
                print(f"PAYMENT REMINDER NOT DELIVERED (order {order.id}):", e)
                continue
            db.add(models.Message(conversation_id=conversation.id, sender="system", text=text))
            db.commit()
            sent += 1
    return sent


async def reminder_loop():
    while True:
        try:
            count = await send_due_reminders()
            if count:
                print(f"Sent {count} payment reminder(s)")
        except Exception as e:  # never let the loop die
            print("REMINDER LOOP ERROR:", repr(e))
        await asyncio.sleep(CHECK_EVERY_SECONDS)
