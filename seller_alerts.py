"""Alerts on the seller's own WhatsApp (Business.alert_phone) whenever a customer needs a person,
and the way back: the seller swipe-replies to an alert and the reply goes to that customer.

WhatsApp only allows free-form messages to the seller within 24h of the seller messaging the shop
number; outside that the approved `seller_alert` template is used (WHATSAPP_TEMPLATES.md)."""
import os
import re
from datetime import datetime, timedelta, timezone

import models
from business import whatsapp_creds
from notifications import within_service_window
from whatsapp import WhatsAppSendError, send_message, send_reaction, send_template

SELLER_ALERT_TEMPLATE = os.getenv("WA_TEMPLATE_SELLER_ALERT", "seller_alert")
SELLER_ALERT_TEMPLATE_LANG = os.getenv("WA_TEMPLATE_SELLER_ALERT_LANG", "en")
REALERT_AFTER = timedelta(minutes=5)
TELL_CUSTOMER_AFTER = timedelta(minutes=15)
# "Bot", "#bot", "bot.", "AI" ... all mean "let the assistant handle this chat" - never sent to the customer.
HAND_BACK_WORDS = {"bot", "ai", "assistant"}


def is_hand_back(text: str) -> bool:
    word = re.sub(r"[^a-z]", "", (text or "").lower())
    return word in HAND_BACK_WORDS or (text or "").strip().lower() == "#done"


def _digits(phone: str | None) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return "91" + digits if len(digits) == 10 else digits


def _seller_window_open(db, business: models.Business, seller_phone: str) -> bool:
    seller_chat = db.query(models.Conversation).filter(
        models.Conversation.business_id == business.id, models.Conversation.phone == seller_phone
    ).first()
    return within_service_window(seller_chat)


def _who(conversation: models.Conversation) -> str:
    return conversation.customer_name or "A customer"


async def _send_alert(db, business: models.Business, conversation: models.Conversation, text: str,
                      template_question: str) -> bool:
    seller = _digits(business.alert_phone)
    if not seller:
        return False
    creds = whatsapp_creds(business)
    try:
        if _seller_window_open(db, business, seller):
            message_id = await send_message(creds, seller, text)
        else:
            flat = " ".join(template_question.split())[:900] or "(no text)"
            message_id = await send_template(creds, seller, SELLER_ALERT_TEMPLATE, SELLER_ALERT_TEMPLATE_LANG,
                                             [_who(conversation), f"+{conversation.phone}", flat])
    except WhatsAppSendError as e:
        print(f"SELLER ALERT NOT DELIVERED (conversation {conversation.id}):", e)
        return False
    db.add(models.SellerAlert(business_id=business.id, conversation_id=conversation.id, wa_message_id=message_id))
    db.commit()
    return True


async def alert_seller(db, business: models.Business, conversation: models.Conversation, question: str) -> bool:
    text = (f"❓ {_who(conversation)} (+{conversation.phone}) asks:\n\"{question}\"\n\n"
            "↩️ Swipe-reply to this message to answer.\nReply *bot* to let the assistant handle it.")
    return await _send_alert(db, business, conversation, text, question)


async def notify_seller(db, business: models.Business, text: str) -> bool:
    """Plain heads-up to the seller (new order, payment received). Only inside the seller's 24h
    window - these aren't urgent enough to pay for a template; the dashboard shows them anyway."""
    seller = _digits(business.alert_phone)
    if not seller or not _seller_window_open(db, business, seller):
        return False
    try:
        await send_message(whatsapp_creds(business), seller, text)
        return True
    except WhatsAppSendError as e:
        print("SELLER NOTE NOT DELIVERED:", e)
        return False


def _alert_for(db, business: models.Business, context_id: str | None):
    alerts = db.query(models.SellerAlert).filter(models.SellerAlert.business_id == business.id)
    if context_id:
        return alerts.filter(models.SellerAlert.wa_message_id == context_id).first()
    return alerts.order_by(models.SellerAlert.id.desc()).first()


def is_seller_reply(db, business: models.Business, phone: str, context_id: str | None, text: str) -> bool:
    """A message from the seller's own number that swipe-replies to one of our alerts - or a bare
    "bot" command, which applies to the latest alerted chat. Anything else from the seller's number
    is treated as a normal customer message (sellers test their own shop)."""
    if not business.alert_phone or _digits(phone) != _digits(business.alert_phone):
        return False
    if not context_id and not is_hand_back(text):
        return False
    return _alert_for(db, business, context_id) is not None


async def _react(creds, to: str, message_id: str | None, emoji: str):
    if not message_id:
        return
    try:
        await send_reaction(creds, to, message_id, emoji)
    except WhatsAppSendError as e:
        print("REACTION NOT SENT:", e)


async def handle_seller_reply(db, business: models.Business, phone: str, text: str, context_id: str,
                              wa_message_id: str | None):
    creds = whatsapp_creds(business)
    alert = _alert_for(db, business, context_id)
    conversation = alert.conversation
    name = _who(conversation)

    if is_hand_back(text):
        conversation.needs_seller = False
        conversation.seller_alert_stage = 0
        conversation.ai_paused = False
        db.commit()
        await send_message(creds, phone, f"✅ The assistant is back on for {name}.")
        return

    if text.startswith("["):  # photo, voice note, sticker... only text can be passed on
        await send_message(creds, phone, "Please reply with text - only text answers can be passed to the customer.")
        return
    if not within_service_window(conversation):
        await send_message(creds, phone, f"⚠️ Couldn't send: {name} last wrote over 24 hours ago, so WhatsApp "
                                         "won't allow a free message. It will go through once they write again.")
        return
    try:
        await send_message(creds, conversation.phone, text)
    except WhatsAppSendError as e:
        await send_message(creds, phone, f"⚠️ Couldn't send to {name}: {e}")
        return
    db.add(models.Message(conversation_id=conversation.id, sender="seller", text=text, wa_message_id=wa_message_id))
    conversation.needs_seller = False
    conversation.seller_alert_stage = 0
    conversation.last_seller_reply_at = datetime.now(timezone.utc)
    db.commit()
    await _react(creds, phone, wa_message_id, "✅")


async def escalate_waiting(db, now: datetime | None = None) -> int:
    """Re-alert the seller after 5 min, and tell the customer after 15 min. Returns actions taken."""
    from languages import t

    now = now or datetime.now(timezone.utc)
    done = 0
    waiting = db.query(models.Conversation).filter(models.Conversation.needs_seller.is_(True)).all()
    for conversation in waiting:
        since = conversation.needs_seller_since
        if since is None:
            continue
        since = since.replace(tzinfo=timezone.utc) if since.tzinfo is None else since
        waited = now - since
        business = db.get(models.Business, conversation.business_id)
        if conversation.seller_alert_stage <= 1 and waited >= REALERT_AFTER:
            conversation.seller_alert_stage = 2
            db.commit()
            last = (db.query(models.Message)
                    .filter(models.Message.conversation_id == conversation.id, models.Message.sender == "customer")
                    .order_by(models.Message.id.desc()).first())
            question = last.text if last else ""
            minutes = int(waited.total_seconds() // 60)
            await _send_alert(db, business, conversation,
                              f"⏰ {_who(conversation)} (+{conversation.phone}) has been waiting {minutes} min:\n"
                              f"\"{question}\"\n\n↩️ Swipe-reply to this message to answer.", question)
            done += 1
        elif conversation.seller_alert_stage == 2 and waited >= TELL_CUSTOMER_AFTER:
            conversation.seller_alert_stage = 3
            db.commit()
            if within_service_window(conversation):
                text = t(conversation.preferred_language, "seller_busy")
                try:
                    await send_message(whatsapp_creds(business), conversation.phone, text)
                    db.add(models.Message(conversation_id=conversation.id, sender="ai", text=text))
                    db.commit()
                except WhatsAppSendError as e:
                    print("WAIT MESSAGE NOT DELIVERED:", e)
            done += 1
    return done
