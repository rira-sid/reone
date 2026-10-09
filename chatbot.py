"""Handles one incoming WhatsApp message: store it, let Claude respond (unless the seller has
taken over the chat), and turn a confirmed cart into a real order + payment link."""
import json
from datetime import datetime, timezone

import ai
import models
import schemas
from business import whatsapp_creds
from database import SessionLocal
from routers.orders import place_order
from routers.payments import ensure_payment_link
from whatsapp import WhatsAppSendError, download_media, send_message

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


def _checkout(db, business: models.Business, conversation: models.Conversation, cart: list[ai.CartItem]) -> str:
    """Create the order and return the text to append to the AI's thank-you reply."""
    customer = _get_or_create_customer(db, conversation)
    items = [schemas.OrderItemCreate(product_id=i.product_id, quantity=i.quantity) for i in cart if i.quantity > 0]
    order = place_order(db, business.id, customer, items)
    conversation.cart_json = "[]"
    db.commit()
    db.refresh(order)

    summary = f"Order #{order.id} - Total ₹{order.total_amount:g}"
    try:
        link = ensure_payment_link(db, business, order)
    except Exception as e:
        # Razorpay not connected yet, or the link call failed - the order is still saved
        # on the dashboard, and the seller can share payment details by hand.
        print("PAYMENT LINK ERROR:", repr(e))
        return f"{summary}\nThe seller will send you the payment details shortly."
    return f"{summary}\nPay here: {link}"


async def handle_incoming(
    business_id: int,
    phone: str,
    text: str,
    profile_name: str | None = None,
    image_media_id: str | None = None,
    wa_message_id: str | None = None,
):
    with SessionLocal() as db:
        if wa_message_id and db.query(models.Message).filter(models.Message.wa_message_id == wa_message_id).first():
            print(f"Already handled {wa_message_id}, skipping")
            return
        business = db.get(models.Business, business_id)
        creds = whatsapp_creds(business)
        conversation = _get_or_create_conversation(db, business_id, phone, profile_name)
        conversation.last_customer_message_at = datetime.now(timezone.utc)
        db.add(models.Message(conversation_id=conversation.id, sender="customer", text=text, wa_message_id=wa_message_id))
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
            turn = await ai.run_turn(business.name, conversation, products, image, recent_orders)
        except Exception as e:
            # Refusal, API outage, missing key, bad output... never leave the customer unanswered.
            print("AI ERROR:", repr(e))
            conversation.ai_paused = True
            await _reply(db, creds, conversation, HANDOFF_REPLY)
            return

        print("AI TURN:", turn.model_dump_json())
        conversation.language = turn.language
        conversation.cart_json = json.dumps([i.model_dump() for i in turn.cart])
        if turn.customer_name:
            conversation.customer_name = turn.customer_name
        if turn.delivery_address:
            conversation.delivery_address = turn.delivery_address

        reply = turn.reply
        if turn.needs_human:
            conversation.ai_paused = True
        elif turn.ready_to_place_order and turn.cart and conversation.customer_name and conversation.delivery_address:
            problems = _stock_problems(db, business_id, turn.cart)
            if problems:
                # Stock changed since Claude saw it - let the seller sort it out rather than guess.
                conversation.ai_paused = True
                print("STOCK PROBLEM:", problems)
                reply = HANDOFF_REPLY
            else:
                reply = f"{reply}\n\n{_checkout(db, business, conversation, turn.cart)}"

        await _reply(db, creds, conversation, reply)


async def _reply(db, creds, conversation: models.Conversation, text: str):
    db.add(models.Message(conversation_id=conversation.id, sender="ai", text=text))
    db.commit()
    try:
        await send_message(creds, conversation.phone, text)
    except WhatsAppSendError as e:
        print("AI REPLY NOT DELIVERED:", e)
