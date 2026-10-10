"""Handles one incoming WhatsApp message: store it, let Claude respond (unless the seller has
taken over the chat), and turn a confirmed cart into a real order + payment link."""
import json
from datetime import datetime, timezone

import ai
import models
import schemas
import voice
from business import whatsapp_creds
from database import SessionLocal
from fastapi import HTTPException

from routers.invoices import PUBLIC_BASE_URL
from routers.orders import place_order
from routers.pay import checkout_instruction, send_payment_request
from routers.payments import ensure_payment_link
from whatsapp import WhatsAppSendError, download_media, send_audio, send_message

HANDOFF_REPLY = "Thanks for your message! The seller will reply to you here shortly."


def _get_or_create_conversation(db, business_id: int, phone: str, profile_name: str | None) -> models.Conversation:
    conversation = db.query(models.Conversation).filter(
        models.Conversation.business_id == business_id, models.Conversation.phone == phone
    ).first()
    if not conversation:
        conversation = models.Conversation(business_id=business_id, phone=phone, customer_name=profile_name)
        db.add(conversation)
        db.flush()
    return conversation


def _get_or_create_customer(db, conversation: models.Conversation) -> models.Customer:
    customer = db.query(models.Customer).filter(
        models.Customer.business_id == conversation.business_id, models.Customer.phone == conversation.phone
    ).first()
    if not customer:
        customer = models.Customer(business_id=conversation.business_id, name="", phone=conversation.phone)
        db.add(customer)
    customer.name = conversation.customer_name or customer.name or conversation.phone
    customer.address = conversation.delivery_address or customer.address
    db.flush()
    return customer


def _stock_problems(db, business_id: int, cart: list[ai.CartItem]) -> list[str]:
    problems = []
    for item in cart:
        product = db.query(models.Product).filter(
            models.Product.id == item.product_id, models.Product.business_id == business_id
        ).first()
        if not product or not product.is_active:
            problems.append(f"product {item.product_id} no longer exists")
        elif product.stock < item.quantity:
            problems.append(f"only {product.stock} {product.unit} of {product.name} left")
    return problems


def shop_policies(business: models.Business) -> str:
    lines = [f"Catalog link: {PUBLIC_BASE_URL}/shop/{business.id}"]
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


def _checkout(db, business: models.Business, conversation: models.Conversation, cart: list[ai.CartItem],
              payment_method: str, note: str | None) -> tuple[str, models.Order | None]:
    """Create the order. Returns the text to append to the AI's thank-you reply, and the order
    if a separate payment message (UPI button / WhatsApp Pay card) should follow it."""
    customer = _get_or_create_customer(db, conversation)
    items = [schemas.OrderItemCreate(product_id=i.product_id, quantity=i.quantity) for i in cart if i.quantity > 0]
    order = place_order(db, business, customer, items, payment_method, note)
    conversation.cart_json = "[]"
    db.commit()
    db.refresh(order)

    summary = f"Order #{order.id} - Total ₹{order.total_amount:g}"
    if order.delivery_fee:
        summary += f" (incl. ₹{order.delivery_fee:g} delivery)"
    if order.payment_method == "cod":
        return f"{summary}\nPlease pay ₹{order.total_amount:g} in cash on delivery.", None
    # UPI button / WhatsApp Pay card: a separate interactive message follows the summary.
    instruction = checkout_instruction(business, order)
    if instruction:
        db.commit()
        return f"{summary}\n{instruction}", order
    try:
        link = ensure_payment_link(db, business, order)
    except Exception as e:
        # No payment method set up yet, or the Razorpay call failed - the order is still saved
        # on the dashboard, and the seller can share payment details by hand.
        print("PAYMENT LINK ERROR:", repr(e))
        return f"{summary}\nThe seller will send you the payment details shortly.", None
    return f"{summary}\nPay here: {link}", None


async def handle_incoming(
    business_id: int,
    phone: str,
    text: str,
    profile_name: str | None = None,
    image_media_id: str | None = None,
    wa_message_id: str | None = None,
    audio_media_id: str | None = None,
):
    with SessionLocal() as db:
        if wa_message_id and db.query(models.Message).filter(models.Message.wa_message_id == wa_message_id).first():
            print(f"Already handled {wa_message_id}, skipping")
            return
        business = db.get(models.Business, business_id)
        creds = whatsapp_creds(business)
        conversation = _get_or_create_conversation(db, business_id, phone, profile_name)
        conversation.last_customer_message_at = datetime.now(timezone.utc)
        incoming = models.Message(conversation_id=conversation.id, sender="customer", text=text, wa_message_id=wa_message_id)
        db.add(incoming)
        db.commit()
        db.refresh(conversation)

        if conversation.ai_paused:
            return  # seller has taken over this chat from the dashboard

        image = None
        if image_media_id:
            try:
                image = await download_media(creds, image_media_id)
            except WhatsAppSendError as e:
                print("IMAGE DOWNLOAD FAILED:", e)
        audio = None
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
                                     shop_policies(business), audio=audio)
        except Exception as e:
            # Refusal, API outage, missing key, bad output... never leave the customer unanswered.
            print("AI ERROR:", repr(e))
            conversation.ai_paused = True
            await _reply(db, creds, conversation, HANDOFF_REPLY)
            return

        print("AI TURN:", turn.model_dump_json())
        if audio and turn.heard_text:
            incoming.text = f"🎤 {turn.heard_text}"  # what the seller sees in the inbox, and the AI next turn
        conversation.language = turn.language
        conversation.cart_json = json.dumps([i.model_dump() for i in turn.cart])
        if turn.customer_name:
            conversation.customer_name = turn.customer_name
        if turn.delivery_address:
            conversation.delivery_address = turn.delivery_address
        db.commit()  # keep what the AI collected even if checkout below has to roll back

        reply = turn.reply
        spoken = turn.spoken_reply  # only kept when the AI's own reply goes out unchanged
        order_placed = False
        pay_order = None
        if turn.needs_human:
            conversation.ai_paused = True
        elif turn.ready_to_place_order and turn.cart and conversation.customer_name and conversation.delivery_address:
            problems = _stock_problems(db, business_id, turn.cart)
            payment_method = "cod" if turn.payment_method == "cod" and business.cod_enabled else "online"
            if not business.accepting_orders:
                # Safety net - the AI is told the shop is closed, but never place an order anyway.
                reply = business.closed_message or "Sorry, we're not taking orders right now. Please check back soon!"
                spoken = None
            elif problems:
                # Stock changed since Claude saw it - let the seller sort it out rather than guess.
                conversation.ai_paused = True
                print("STOCK PROBLEM:", problems)
                reply = HANDOFF_REPLY
                spoken = None
            else:
                try:
                    checkout_text, pay_order = _checkout(db, business, conversation, turn.cart, payment_method,
                                                         turn.order_note)
                    reply = f"{reply}\n\n{checkout_text}"
                    order_placed = True
                except HTTPException as e:
                    # Broke a shop rule the AI missed (e.g. minimum order) - say so, keep the cart.
                    db.rollback()
                    reply = f"Sorry, we couldn't place this order: {e.detail}. Would you like to add something?"
                    spoken = None

        await _reply(db, creds, conversation, reply)
        if pay_order:
            await send_payment_request(business, pay_order, conversation.phone)
        # Answer a voice note with a voice note, and always confirm a new order out loud.
        if spoken and (audio or order_placed):
            await _send_voice(db, creds, conversation, spoken)


async def _send_voice(db, creds, conversation: models.Conversation, text: str):
    """Best effort: the text reply has already gone out, so a voice failure only gets logged."""
    if not voice.enabled():
        return
    try:
        audio = await voice.synthesize(text)
        await send_audio(creds, conversation.phone, audio)
    except Exception as e:
        print("VOICE REPLY NOT SENT:", repr(e))
        return
    db.add(models.Message(conversation_id=conversation.id, sender="ai", text=f"🔊 {text}"))
    db.commit()


async def _reply(db, creds, conversation: models.Conversation, text: str):
    db.add(models.Message(conversation_id=conversation.id, sender="ai", text=text))
    db.commit()
    try:
        await send_message(creds, conversation.phone, text)
    except WhatsAppSendError as e:
        print("AI REPLY NOT DELIVERED:", e)
