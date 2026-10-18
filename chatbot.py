"""Handles incoming WhatsApp messages - like a warm shop assistant who knows the customer.

  messages are collected for a few seconds (customers split one thought over several messages),
  then answered once, with the customer's profile, open orders/issues and chat history in mind:
  greeting      -> a personal hello (order on its way, unpaid order, welcome back...) - no link
  products      -> answered from the catalog; the shop link only when they want to order/browse
  order status  -> answered from their orders (payment link again if they want it)
  issue         -> incident #INC-<id>, one personal note, urgent alert; then only the owner replies
  anything else -> a natural holding line + alert on the owner's WhatsApp (swipe-reply to answer)
"""
import asyncio
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import ai
import models
from auth import sign_value
from business import whatsapp_creds
from database import SessionLocal
import zones
from delivery import delivery_days
from languages import FIRST_PAGE, LANGUAGES, available_languages, language_name, t
from routers.invoices import PUBLIC_BASE_URL
import incidents
from seller_alerts import alert_seller, handle_seller_reply, is_seller_reply, notify_seller
from whatsapp import WhatsAppSendError, download_media, send_interactive, send_message

# Don't repeat "the seller will reply" to every message of a customer who sends several in a row.
ACK_EVERY = timedelta(minutes=10)
# While the seller is answering a customer, don't keep telling the customer "the seller will reply".
SELLER_CHAT_ACTIVE = timedelta(minutes=30)
MORE_LANGUAGES_ID = "lang:more"
# Wait for the customer to stop typing before answering; longer when the last message looks unfinished.
BATCH_WAIT = float(os.getenv("BATCH_WAIT_SECONDS", "10"))
BATCH_WAIT_LONG = float(os.getenv("BATCH_WAIT_LONG_SECONDS", "20"))
UNFINISHED_ENDINGS = {"i", "need", "want", "and", "also", "the", "a", "for", "with", "of", "to", "venum", "vendum", "chahiye", "aur"}
_pending: dict[int, asyncio.Task] = {}


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


def shop_link(business: models.Business, phone: str | None, lang: str | None,
              cart: list[ai.CartItem] | None = None) -> str:
    from slugs import shop_path

    params = {}
    if phone:
        params["c"] = customer_token(business.id, phone)
    if lang:
        params["lang"] = lang
    if cart:
        params["add"] = ",".join(
            f"{i.product_id}{'-' + str(i.variant_id) if i.variant_id else ''}:{max(1, i.quantity)}" for i in cart
        )
    query = f"?{urlencode(params)}" if params else ""
    return f"{PUBLIC_BASE_URL}{shop_path(business)}{query}"


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


def shop_policies(business: models.Business, db=None) -> str:
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
    days = delivery_days(business)
    if days:
        lines.append(f"Usual delivery time: {days[0]}-{days[1]} days after the order is confirmed" if days[0] != days[1]
                     else f"Usual delivery time: {days[0]} days after the order is confirmed")
    if business.min_order_amount:
        lines.append(f"Minimum order value: ₹{business.min_order_amount:g} (before delivery fee)")
    lines.append("Cash on delivery: " + ("available" if business.cod_enabled else "not available - online payment only"))
    zone_text = zones.describe(db, business) if db is not None else ""
    if zone_text:
        lines.append("The general rules above apply only where a zone leaves something blank.")
        lines.append(zone_text)
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
    url = shop_link(business, conversation.phone, lang, cart)
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


async def route_to_seller(db, business: models.Business, creds, conversation: models.Conversation, question: str,
                          hold: str | None = None):
    """Mark the chat "Needs reply", alert the owner's phone, and send the customer one natural holding
    line (written by the AI in their language) - not again for every follow-up message."""
    now = datetime.now(timezone.utc)
    if not conversation.needs_seller:
        conversation.needs_seller = True
        conversation.needs_seller_since = now
        conversation.seller_alert_stage = 1
    db.commit()
    await alert_seller(db, business, conversation, question)
    acked = _utc(conversation.seller_ack_at)
    if not acked or now - acked > ACK_EVERY:
        conversation.seller_ack_at = now
        await _send_text(db, creds, conversation, hold or t(conversation.preferred_language, "seller_will_reply"))


# ---- context for the AI --------------------------------------------------------------------

def _orders_for(db, business_id: int, phone: str, limit: int = 5) -> list[models.Order]:
    return (
        db.query(models.Order)
        .join(models.Customer)
        .filter(models.Order.business_id == business_id, models.Customer.phone == phone)
        .order_by(models.Order.id.desc())
        .limit(limit)
        .all()
    )


def customer_profile(db, conversation: models.Conversation, orders: list[models.Order]) -> str:
    """What a shopkeeper remembers about a regular: name, how long they've been buying, usual
    products, where they live, past issues."""
    lines = []
    if conversation.customer_name:
        lines.append(f"Name: {conversation.customer_name}")
    first = _utc(conversation.messages[0].created_at) if conversation.messages else None
    if first:
        lines.append(f"First messaged: {first:%d %b %Y}")
    real = [o for o in orders if o.status != "Cancelled"]
    if real:
        counts = {}
        for o in real:
            for i in o.items:
                counts[i.display_name] = counts.get(i.display_name, 0) + i.quantity
        usual = ", ".join(n for n, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:3])
        lines.append(f"Orders: {len(real)} recent; usually buys {usual}")
        address = real[0].customer.address if real[0].customer else ""
        if address:
            pincode = real[0].customer.pincode
            lines.append(f"Delivery address on file: {address[:120]}" + (f" (PIN {pincode})" if pincode else ""))
    if conversation.language:
        lines.append(f"Language used last time: {conversation.language}")
    issues = db.query(models.Incident).filter(models.Incident.conversation_id == conversation.id).count()
    if issues:
        lines.append(f"Past issues reported: {issues}")
    return "\n".join(lines)


def open_items(db, conversation: models.Conversation, orders: list[models.Order]) -> str:
    now = datetime.now(timezone.utc)
    lines = []
    if conversation.needs_seller and conversation.needs_seller_since:
        waited = int((now - _utc(conversation.needs_seller_since)).total_seconds() // 60)
        last_q = (db.query(models.Message).filter(models.Message.conversation_id == conversation.id,
                                                  models.Message.sender == "customer")
                  .filter(models.Message.created_at <= _utc(conversation.needs_seller_since) + timedelta(seconds=30))
                  .order_by(models.Message.id.desc()).first())
        lines.append(f"A question is waiting for the owner ({waited} min): \"{(last_q.text if last_q else '')[:200]}\"")
    for o in orders:
        if o.status == "Cancelled":
            continue
        if not o.is_paid and o.payment_method == "online" and o.status == "Pending":
            lines.append(f"Order #{o.id} is waiting for payment (₹{o.total_amount:g})")
        elif o.status not in ("Delivered",) and (o.is_paid or o.payment_method == "cod"):
            lines.append(f"Order #{o.id} is {o.status} - not delivered yet")
        elif o.status == "Delivered" and o.created_at and now - _utc(o.created_at) < timedelta(days=7):
            items = ", ".join(i.display_name for i in o.items)
            lines.append(f"Order #{o.id} ({items}) was delivered recently")
    open_issue = db.query(models.Incident).filter(models.Incident.conversation_id == conversation.id,
                                                  models.Incident.status != "resolved").first()
    if open_issue:
        lines.append(f"Issue #{open_issue.number} ({open_issue.category}) is {open_issue.status.replace('_', ' ')}")
    return "\n".join(lines)


# ---- receiving messages ------------------------------------------------------------------------

def looks_unfinished(text: str) -> bool:
    """A message that probably continues in the next one ("I need", "and", "chicken masala,")."""
    words = (text or "").strip().lower().split()
    if not words:
        return False
    if text.strip()[-1:] in (",", "-", "&", "+", ":"):
        return True
    return len(words) <= 3 and words[-1] in UNFINISHED_ENDINGS


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
    """Store the message, then answer once the customer stops typing (BATCH_WAIT, longer if the last
    message looks unfinished). Several quick messages get one combined reply."""
    with SessionLocal() as db:
        if wa_message_id and db.query(models.Message).filter(models.Message.wa_message_id == wa_message_id).first():
            print(f"Already handled {wa_message_id}, skipping")
            return
        business = db.get(models.Business, business_id)

        # The seller answering an alert from their own WhatsApp.
        if is_seller_reply(db, business, phone, context_id, text):
            await handle_seller_reply(db, business, phone, text, context_id, wa_message_id)
            return

        conversation = _get_or_create_conversation(db, business_id, phone, profile_name)
        if conversation.last_processed_message_id is None:
            # First message ever (or a chat from before batching): everything earlier counts as handled.
            last = db.query(models.Message.id).filter(models.Message.conversation_id == conversation.id) \
                .order_by(models.Message.id.desc()).first()
            conversation.last_processed_message_id = last[0] if last else 0
        conversation.last_customer_message_at = datetime.now(timezone.utc)
        media_id, media_type = (audio_media_id, "audio") if audio_media_id else (image_media_id, "image") if image_media_id else (None, None)
        db.add(models.Message(conversation_id=conversation.id, sender="customer", text=text, wa_message_id=wa_message_id,
                              media_id=media_id, media_type=media_type))
        db.commit()
        conversation_id = conversation.id

        # Old language-menu taps (menus sent before this version) still set the language.
        if interactive_id and interactive_id.startswith("lang:") and interactive_id != MORE_LANGUAGES_ID:
            code = interactive_id[5:]
            conversation.preferred_language = code if code in LANGUAGES else "en"
            conversation.language = language_name(conversation.preferred_language)
            db.commit()

    wait = BATCH_WAIT_LONG if (audio_media_id or looks_unfinished(text)) else BATCH_WAIT
    if wait <= 0:
        await respond(conversation_id)
        return
    previous = _pending.pop(conversation_id, None)
    if previous and not previous.done():
        previous.cancel()  # the customer is still typing - restart the wait
    _pending[conversation_id] = asyncio.create_task(_respond_later(conversation_id, wait))


async def _respond_later(conversation_id: int, wait: float):
    try:
        await asyncio.sleep(wait)
    except asyncio.CancelledError:
        return
    _pending.pop(conversation_id, None)
    try:
        await respond(conversation_id)
    except Exception as e:  # never lose a reply silently
        print(f"RESPOND FAILED (conversation {conversation_id}):", repr(e))
        from tracking import log_event

        log_event("ai", f"Reply failed: {e!r}"[:500])


# ---- answering ---------------------------------------------------------------------------

async def respond(conversation_id: int):
    """Answer all customer messages that arrived since the last answer, together."""
    with SessionLocal() as db:
        conversation = db.get(models.Conversation, conversation_id)
        if conversation is None:
            return
        business = db.get(models.Business, conversation.business_id)
        creds = whatsapp_creds(business)
        handled_upto = conversation.last_processed_message_id or 0
        new = [m for m in conversation.messages if m.sender == "customer" and m.id > handled_upto]
        if not new:
            return
        conversation.last_processed_message_id = new[-1].id
        db.commit()
        combined = "\n".join(m.text for m in new)
        now = datetime.now(timezone.utc)

        # An open issue: only the owner replies until it's resolved - add what they sent to it.
        incident = db.query(models.Incident).filter(models.Incident.conversation_id == conversation.id,
                                                    models.Incident.status != "resolved").first()
        if incident:
            await incidents.add_to_incident(db, business, incident, combined)
            return
        # The owner took the chat over in the dashboard, or is chatting with this customer right now.
        seller_chatting = _utc(conversation.last_seller_reply_at) and now - _utc(conversation.last_seller_reply_at) < SELLER_CHAT_ACTIVE
        if conversation.ai_paused or seller_chatting:
            await alert_seller(db, business, conversation, combined)
            return

        image = audio = None
        for m in reversed(new):  # the latest photo / voice note in the batch
            if m.media_id and m.media_type == "audio" and audio is None:
                try:
                    audio = await download_media(creds, m.media_id)
                except WhatsAppSendError as e:
                    print("VOICE NOTE DOWNLOAD FAILED:", e)
            if m.media_id and m.media_type == "image" and image is None:
                try:
                    image = await download_media(creds, m.media_id)
                except WhatsAppSendError as e:
                    print("IMAGE DOWNLOAD FAILED:", e)

        products = db.query(models.Product).filter(
            models.Product.business_id == business.id, models.Product.is_active.is_(True)).all()
        orders = _orders_for(db, business.id, conversation.phone)
        try:
            turn = await ai.run_turn(business.name, conversation, products, image, orders, shop_policies(business, db),
                                     audio=audio, language=language_name(conversation.preferred_language or "en"),
                                     profile=customer_profile(db, conversation, orders),
                                     open_items=open_items(db, conversation, orders),
                                     welcome_note=business.welcome_message or "", new_after=handled_upto)
        except ai.VoiceNotHeard as e:
            print("VOICE NOTE NOT HEARD:", repr(e))
            await _send_text(db, creds, conversation, t(conversation.preferred_language, "voice_unclear"))
            return
        except Exception as e:
            # API outage, missing key, bad output... the owner answers instead.
            print("AI ERROR:", repr(e))
            from tracking import log_event

            log_event("ai", f"AI failed, chat sent to the seller: {e!r}"[:500], business.id)
            await route_to_seller(db, business, creds, conversation, combined, hold=None)
            return

        print("AI TURN:", turn.model_dump_json())
        if audio and turn.heard_text:
            voice = next((m for m in reversed(new) if m.media_type == "audio"), None)
            if voice:
                voice.text = f"🎤 {turn.heard_text}"  # what the owner sees in the inbox and alerts
                db.commit()
                combined = "\n".join(m.text for m in new)
        if turn.language_code in LANGUAGES and turn.language_code in available_languages():
            if turn.language_code != conversation.preferred_language:
                conversation.preferred_language = turn.language_code
                conversation.language = language_name(turn.language_code)
                db.commit()
        elif not conversation.preferred_language:
            conversation.preferred_language, conversation.language = "en", "English"
            db.commit()
        lang = conversation.preferred_language
        reply = (turn.reply or "").strip()

        # A question is already with the owner: keep everything in that one thread (no second alert,
        # no bot reply) - except a plain greeting, which gets a reassuring hello.
        if conversation.needs_seller and turn.intent not in ("greeting", "order_status"):
            await alert_seller(db, business, conversation, combined, added=True)
            return

        if turn.intent == "issue":
            order = next((o for o in orders if o.status != "Cancelled"), None)
            await incidents.open_incident(db, business, conversation, combined, turn.issue_category or "other",
                                          turn.issue_summary or combined[:200], order, lang, reply=reply)
        elif turn.intent == "question":
            await route_to_seller(db, business, creds, conversation, combined, hold=reply or None)
        elif turn.intent == "notify_owner":
            await notify_seller(db, business, f"🔔 {conversation.customer_name or 'A customer'} (+{conversation.phone}) "
                                              f"wants to know when this is back in stock: {turn.owner_note or combined}")
            await _send_text(db, creds, conversation, reply or "👍")
        elif turn.intent == "unclear":
            await _send_text(db, creds, conversation, t(lang, "voice_unclear"))
        elif turn.intent == "shop":
            if not business.accepting_orders and turn.share_link:
                await _send_text(db, creds, conversation, business.closed_message or t(lang, "closed"))
            elif turn.share_link:
                valid = {p.id: p for p in products}
                cart = [i for i in turn.cart if i.product_id in valid and (
                    i.variant_id is None or any(v.id == i.variant_id for v in valid[i.product_id].active_variants))]
                await _send_shop_button(db, business, creds, conversation, reply or t(lang, "shop_again"), cart)
            else:
                await _send_text(db, creds, conversation, reply or t(lang, "shop_again"))
        elif turn.intent == "order_status":
            if not reply:
                reply = t(lang, "no_orders")
            pay_for = next((o for o in orders if o.id == turn.payment_order_id and not o.is_paid and o.payment_link_url), None)
            if pay_for:
                reply = f"{reply}\n\n💳 {pay_for.payment_link_url}"
            await _send_text(db, creds, conversation, reply)
        else:  # greeting (and old "change_language")
            await _send_text(db, creds, conversation, reply or "🙏")
