"""Background jobs, every minute: one gentle reminder for unpaid orders (inside WhatsApp's 24h
window), auto-cancel of orders still unpaid after a day, escalating seller alerts, RIRAONE plan
reminders / auto-pause (billing.py) and the 6-hourly WhatsApp connection check."""
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
CANCEL_UNPAID_AFTER = timedelta(hours=float(os.getenv("CANCEL_UNPAID_AFTER_HOURS", "24")))
CHECK_EVERY_SECONDS = 60


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


async def cancel_stale_unpaid(now: datetime | None = None) -> int:
    """Online orders nobody paid for within a day are cancelled, so they don't clutter the
    seller's list. Their stock was never taken (online orders reserve stock when paid)."""
    from notifications import notify_order_update
    from routers.orders import release_stock

    now = now or datetime.now(timezone.utc)
    cancelled = 0
    with SessionLocal() as db:
        orders = (
            db.query(models.Order)
            .options(joinedload(models.Order.customer), joinedload(models.Order.business))
            .filter(models.Order.is_paid.is_(False), models.Order.payment_method == "online",
                    models.Order.status == "Pending")
            .all()
        )
        for order in orders:
            if not order.created_at or now - _utc(order.created_at) < CANCEL_UNPAID_AFTER:
                continue
            order.status = "Cancelled"
            release_stock(db, order)
            db.commit()
            await notify_order_update(db, order.business, order,
                                      "It was cancelled because the payment wasn't completed. You can order again any time")
            cancelled += 1
    return cancelled


async def reminder_loop():
    from seller_alerts import escalate_waiting

    while True:
        try:
            count = await send_due_reminders()
            if count:
                print(f"Sent {count} payment reminder(s)")
            count = await cancel_stale_unpaid()
            if count:
                print(f"Cancelled {count} unpaid order(s)")
            with SessionLocal() as db:
                await escalate_waiting(db)
            from billing import billing_tick, check_whatsapp_health

            with SessionLocal() as db:
                count = await billing_tick(db)
                if count:
                    print(f"Sent {count} billing notice(s)")
                await check_whatsapp_health(db)
            from catalog_i18n import catch_up

            with SessionLocal() as db:
                await catch_up(db)
        except Exception as e:  # never let the loop die
            print("REMINDER LOOP ERROR:", repr(e))
        await asyncio.sleep(CHECK_EVERY_SECONDS)
