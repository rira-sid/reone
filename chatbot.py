"""Handles one incoming WhatsApp message.

Customer flow, fully automatic:
  first message -> language menu -> welcome + "Shop now" button (personal shop link)
  wants to buy  -> "Shop now" button, cart pre-filled with what they asked for
  order status  -> answered from their orders
  anything else -> passed to the seller (instant alert on the seller's own WhatsApp; the seller
                   swipe-replies to the alert and the answer reaches the customer)
"""
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import ai
import models
from auth import sign_value
from business import whatsapp_creds
from database import SessionLocal
from languages import FIRST_PAGE, LANGUAGES, available_languages, language_name, t
from routers.invoices import PUBLIC_BASE_URL
from seller_alerts import alert_seller, handle_seller_reply, is_seller_reply
from whatsapp import WhatsAppSendError, download_media, send_interactive, send_message

# Don't repeat "the seller will reply" to every message of a customer who sends several in a row.
ACK_EVERY = timedelta(minutes=10)
# While the seller is answering a customer, don't keep telling the customer "the seller will reply".
SELLER_CHAT_ACTIVE = timedelta(minutes=30)
MORE_LANGUAGES_ID = "lang:more"


def normalize_phone(phone: str | None) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 10:  # Indian mobile without country code
        digits = "91" + digits
    return digits


def customer_token(business_id: int, phone: str) -> str:
    """Personal shop-link token: lets the shop page fill in (and trust) the customer's number."""
    return f"{phone}.{sign_value(f'cust:{business_id}:{phone}')}"


def phone_from_token(business_id: int, token: str | None) -> str | None:
    if not token or "." not in token:
        return None
    phone, sig = token.rsplit(".", 1)
    return phone if sig == sign_value(f"cust:{business_id}:{phone}") else None


def shop_link(business_id: int, phone: str | None, lang: str | None, cart: list[ai.CartItem] | None = None) -> str:
    params = {}
    if phone:
        params["c"] = customer_token(business_id, phone)
    if lang:
        params["lang"] = lang
    if cart:
        params["add"] = ",".join(
            f"{i.product_id}{'-' + str(i.variant_id) if i.variant_id else ''}:{max(1, i.quantity)}" for i in cart
        )
    query = f"?{urlencode(params)}" if params else ""
    return f"{PUBLIC_BASE_URL}/shop/{business_id}{query}"


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _get_or_create_conversation(db, business_id: int, phone: str, profile_name: str | None) -> models.Conversation:
    conversation = db.query(models.Conversation).filter(
        models.Conversation.business_id == business_id, models.Conversation.phone == phone
    ).first()
    if not conversation:
        conversation = models.Conversation(business_id=business_id, phone=phone, customer_name=profile_name)
        db.add(conversation)
        db.flush()
    elif profile_name and not conversation.customer_name:
        conversation.customer_name = profile_name
    return conversation


def shop_policies(business: models.Business) -> str:
    lines = []
    if not business.accepting_orders:
        lines.append("NOT accepting orders right now. Shop's message: "
                     + (business.closed_message or "We're closed at the moment and will be back soon."))
    if business.delivery_fee:
        rule = f"Delivery fee: ₹{business.delivery_fee:g}"
        if business.free_delivery_above:
            rule += f" (free delivery for orders of ₹{business.free_delivery_above:g} or more)"
        lines.append(rule)
    else:
        lines.append("Delivery: free")
    if business.min_order_amount:
        lines.append(f"Minimum order value: ₹{business.min_order_amount:g} (before delivery fee)")
    lines.append("Cash on delivery: " + ("available" if business.cod_enabled else "not available - online payment only"))
    return "\n".join(lines)


# ---- outgoing messages ---------------------------------------------------------------------

async def _send_text(db, creds, conversation: models.Conversation, text: str, sender: str = "ai"):
    db.add(models.Message(conversation_id=conversation.id, sender=sender, text=text))
    db.commit()
    try:
        await send_message(creds, conversation.phone, text)
    except WhatsAppSendError as e:
        print("REPLY NOT DELIVERED:", e)


async def _send_shop_button(db, business: models.Business, creds, conversation: models.Conversation, text: str,
                            cart: list[ai.CartItem] | None = None):
    lang = conversation.preferred_language
    url = shop_link(business.id, conversation.phone, lang, cart)
    db.add(models.Message(conversation_id=conversation.id, sender="ai", text=f"{text}\n🛍️ {url}"))
    db.commit()
    try:
        await send_interactive(creds, conversation.phone, {
            "type": "cta_url",
            "body": {"text": text[:1024]},
            "footer": {"text": business.name[:60]},
            "action": {"name": "cta_url", "parameters": {"display_text": t(lang, "shop_button")[:20], "url": url}},
        })
    except WhatsAppSendError as e:
        print("SHOP BUTTON FAILED, sending plain link:", e)
        try:
            await send_message(creds, conversation.phone, f"{text}\n\n🛍️ {url}")
        except WhatsAppSendError as e2:
            print("SHOP LINK NOT DELIVERED:", e2)


async def send_language_menu(db, business: models.Business, creds, conversation: models.Conversation, page: int = 1):
    codes = available_languages()
    if page == 1 and len(codes) > FIRST_PAGE + 1:
        shown, more = codes[:FIRST_PAGE], True
    else:
        shown, more = (codes[FIRST_PAGE:] if page == 2 else codes)[:10], False
    rows = [{"id": f"lang:{c}", "title": LANGUAGES[c][0][:24],
             **({"description": LANGUAGES[c][1]} if LANGUAGES[c][0] != LANGUAGES[c][1] else {})} for c in shown]
    if more:
        rows.append({"id": MORE_LANGUAGES_ID, "title": "🌐 More languages", "description": "Arabic, Español, Français..."})
    body = t("en", "pick", shop=business.name)
    db.add(models.Message(conversation_id=conversation.id, sender="ai", text="[language menu]"))
    db.commit()
    try:
        await send_interactive(creds, conversation.phone, {
            "type": "list",
            "body": {"text": body},
            "action": {"button": "Choose language", "sections": [{"title": "Languages", "rows": rows}]},
        })
    except WhatsAppSendError as e:
        print("LANGUAGE MENU FAILED, continuing in English:", e)
        conversation.preferred_language = "en"
        db.commit()
        await _send_welcome(db, business, creds, conversation)


async def _send_welcome(db, business: models.Business, creds, conversation: models.Conversation):
    lang = conversation.preferred_language
    text = t(lang, "welcome", name=conversation.customer_name or "", shop=business.name).replace(" !", "!")
    if business.welcome_message:
        text = f"{text}\n\n{business.welcome_message}"
    if not business.accepting_orders:
        await _send_text(db, creds, conversation,
                         f"{text}\n\n{business.closed_message or t(lang, 'closed')}")
        return
    await _send_shop_button(db, business, creds, conversation, text)


async def route_to_seller(db, business: models.Business, creds, conversation: models.Conversation, question: str):
    """Mark the chat "Needs you", alert the seller's phone, and tell the customer (not on every message)."""
    now = datetime.now(timezone.utc)
    if not conversation.needs_seller:
        conversation.needs_seller = True
        conversation.needs_seller_since = now
        conversation.seller_alert_stage = 1
    db.commit()
    await alert_seller(db, business, conversation, question)
    acked = _utc(conversation.seller_ack_at)
    seller_chatting = _utc(conversation.last_seller_reply_at) and now - _utc(conversation.last_seller_reply_at) < SELLER_CHAT_ACTIVE
    if not seller_chatting and (not acked or now - acked > ACK_EVERY):
        conversation.seller_ack_at = now
        await _send_text(db, creds, conversation, t(conversation.preferred_language, "seller_will_reply"))


# ---- main entry ----------------------------------------------------------------------------

async def handle_incoming(
    business_id: int,
    phone: str,
    text: str,
    profile_name: str | None = None,
    image_media_id: str | None = None,
    wa_message_id: str | None = None,
    audio_media_id: str | None = None,
    interactive_id: str | None = None,
    context_id: str | None = None,
):
    with SessionLocal() as db:
        if wa_message_id and db.query(models.Message).filter(models.Message.wa_message_id == wa_message_id).first():
            print(f"Already handled {wa_message_id}, skipping")
            return
        business = db.get(models.Business, business_id)
        creds = whatsapp_creds(business)

        # The seller answering an alert from their own WhatsApp.
        if is_seller_reply(db, business, phone, context_id, text):
            await handle_seller_reply(db, business, phone, text, context_id, wa_message_id)
            return

        conversation = _get_or_create_conversation(db, business_id, phone, profile_name)
        conversation.last_customer_message_at = datetime.now(timezone.utc)
        incoming = models.Message(conversation_id=conversation.id, sender="customer", text=text, wa_message_id=wa_message_id)
        db.add(incoming)
        db.commit()
        db.refresh(conversation)

        # Language menu answers.
        if interactive_id == MORE_LANGUAGES_ID:
            await send_language_menu(db, business, creds, conversation, page=2)
            return
        if interactive_id and interactive_id.startswith("lang:"):
            code = interactive_id[5:]
            conversation.preferred_language = code if code in LANGUAGES else "en"
            conversation.language = language_name(conversation.preferred_language)
            db.commit()
            await _send_welcome(db, business, creds, conversation)
            return

        if conversation.ai_paused:
            # The seller took this chat over in the dashboard: everything goes to them.
            await alert_seller(db, business, conversation, text)
            return

        if not conversation.preferred_language:
            await send_language_menu(db, business, creds, conversation)
            return

        image = audio = None
        if image_media_id:
            try:
                image = await download_media(creds, image_media_id)
            except WhatsAppSendError as e:
                print("IMAGE DOWNLOAD FAILED:", e)
        if audio_media_id:
            try:
                audio = await download_media(creds, audio_media_id)
            except WhatsAppSendError as e:
                print("VOICE NOTE DOWNLOAD FAILED:", e)

        products = db.query(models.Product).filter(
            models.Product.business_id == business_id, models.Product.is_active.is_(True)
        ).all()
        recent_orders = (
            db.query(models.Order)
            .join(models.Customer)
            .filter(models.Order.business_id == business_id, models.Customer.phone == phone)
            .order_by(models.Order.id.desc())
            .limit(5)
            .all()
        )
        try:
            turn = await ai.run_turn(business.name, conversation, products, image, recent_orders,
                                     shop_policies(business), audio=audio,
                                     language=language_name(conversation.preferred_language))
        except ai.VoiceNotHeard as e:
            print("VOICE NOTE NOT HEARD:", repr(e))
            await _send_text(db, creds, conversation, t(conversation.preferred_language, "voice_unclear"))
            return
        except Exception as e:
            # API outage, missing key, bad output... the seller answers instead.
            print("AI ERROR:", repr(e))
            await route_to_seller(db, business, creds, conversation, text)
            return

        print("AI TURN:", turn.model_dump_json())
        if audio and turn.heard_text:
            incoming.text = f"🎤 {turn.heard_text}"  # what the seller sees in the inbox and alerts
            db.commit()
        # Follow the language the customer is actually using now (e.g. picked English, then writes Tamil),
        # so fixed texts, buttons and the shop page match the AI's reply.
        if turn.language_code in LANGUAGES and turn.language_code in available_languages()                 and turn.language_code != conversation.preferred_language:
            conversation.preferred_language = turn.language_code
            conversation.language = language_name(turn.language_code)
            db.commit()
        lang = conversation.preferred_language

        if turn.intent == "shop":
            if not business.accepting_orders:
                await _send_text(db, creds, conversation, business.closed_message or t(lang, "closed"))
            else:
                valid = {p.id: p for p in products}
                cart = [i for i in turn.cart if i.product_id in valid and (
                    i.variant_id is None or any(v.id == i.variant_id for v in valid[i.product_id].active_variants))]
                await _send_shop_button(db, business, creds, conversation, turn.reply.strip() or t(lang, "shop_again"), cart)
        elif turn.intent == "order_status":
            if recent_orders and turn.reply.strip():
                await _send_text(db, creds, conversation, turn.reply)
            else:
                await _send_shop_button(db, business, creds, conversation, t(lang, "no_orders"))
        elif turn.intent == "unclear":
            await _send_text(db, creds, conversation, t(lang, "voice_unclear"))
        elif turn.intent == "change_language":
            await send_language_menu(db, business, creds, conversation)
        elif turn.intent == "greeting":
            # Hello / thanks / ok are always answered by the assistant - never worth an alert.
            await _send_text(db, creds, conversation, turn.reply.strip() or "🙏")
        else:
            await route_to_seller(db, business, creds, conversation, incoming.text)
