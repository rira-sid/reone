"""Messages the system sends to a customer on its own (payment received, order shipped...).

WhatsApp only allows free-form messages within 24 hours of the customer's last message. Outside
that window we must use a Meta-approved template - see WHATSAPP_TEMPLATES.md for the one to submit.
"""
import os
from datetime import datetime, timedelta, timezone

import models
from business import whatsapp_creds
from whatsapp import WhatsAppSendError, send_message, send_template

ORDER_UPDATE_TEMPLATE = os.getenv("WA_TEMPLATE_ORDER_UPDATE", "order_update")
ORDER_UPDATE_TEMPLATE_LANG = os.getenv("WA_TEMPLATE_ORDER_UPDATE_LANG", "en")
# Stay a little inside Meta's 24h limit to allow for clock skew and slow deliveries.
SERVICE_WINDOW = timedelta(hours=23, minutes=30)


def within_service_window(conversation: models.Conversation | None) -> bool:
    if not conversation or not conversation.last_customer_message_at:
        return False
    last = conversation.last_customer_message_at
    if last.tzinfo is None:  # SQLite drops the timezone; values are stored in UTC
        last = last.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last < SERVICE_WINDOW


async def notify_order_update(db, business: models.Business, order: models.Order, update: str):
    """Tell the customer about their order. `update` is one plain sentence, e.g.
    "It has been shipped. Tracking: DTDC 12345"."""
    customer = order.customer
    conversation = db.query(models.Conversation).filter(
        models.Conversation.business_id == business.id, models.Conversation.phone == customer.phone
    ).first()
    creds = whatsapp_creds(business)
    text = f"Order #{order.id} update: {update}"

    try:
        if within_service_window(conversation):
            await send_message(creds, customer.phone, text)
        else:
            await send_template(
                creds, customer.phone, ORDER_UPDATE_TEMPLATE, ORDER_UPDATE_TEMPLATE_LANG,
                [customer.name or "there", str(order.id), update],
            )
    except WhatsAppSendError as e:
        # The order change itself is saved; the seller can see in the inbox that no update went out.
        print(f"ORDER UPDATE NOT DELIVERED (order {order.id}):", e)
        return False

    if conversation:
        db.add(models.Message(conversation_id=conversation.id, sender="system", text=text))
        db.commit()
    return True
