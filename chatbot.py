"""Handles one incoming WhatsApp message: store it, let Claude respond (unless the seller has
taken over the chat), and turn a confirmed cart into a real order + payment link."""
import json

import ai
import models
import schemas
from business import DEFAULT_BUSINESS_ID
from database import SessionLocal
from routers.orders import place_order
from routers.payments import ensure_payment_link
from whatsapp import WhatsAppSendError, send_message

HANDOFF_REPLY = "Thanks for your message! The seller will reply to you here shortly."


def _get_or_create_conversation(db, phone: str, profile_name: str | None) -> models.Conversation:
    conversation = db.query(models.Conversation).filter(
        models.Conversation.business_id == DEFAULT_BUSINESS_ID, models.Conversation.phone == phone
    ).first()
    if not conversation:
        conversation = models.Conversation(business_id=DEFAULT_BUSINESS_ID, phone=phone, customer_name=profile_name)
        db.add(conversation)
        db.flush()
    return conversation


def _get_or_create_customer(db, conversation: models.Conversation) -> models.Customer:
    customer = db.query(models.Customer).filter(
        models.Customer.business_id == DEFAULT_BUSINESS_ID, models.Customer.phone == conversation.phone
    ).first()
    if not customer:
        customer = models.Customer(business_id=DEFAULT_BUSINESS_ID, name="", phone=conversation.phone)
        db.add(customer)
    customer.name = conversation.customer_name or customer.name or conversation.phone
    customer.address = conversation.delivery_address or customer.address
    db.flush()
    return customer


def _stock_problems(db, cart: list[ai.CartItem]) -> list[str]:
    problems = []
    for item in cart:
        product = db.query(models.Product).filter(
            models.Product.id == item.product_id, models.Product.business_id == DEFAULT_BUSINESS_ID
        ).first()
        if not product:
            problems.append(f"product {item.product_id} no longer exists")
        elif product.stock < item.quantity:
            problems.append(f"only {product.stock} {product.unit} of {product.name} left")
    return problems


def _checkout(db, conversation: models.Conversation, cart: list[ai.CartItem]) -> str:
    """Create the order and return the text to append to the AI's thank-you reply."""
    customer = _get_or_create_customer(db, conversation)
    items = [schemas.OrderItemCreate(product_id=i.product_id, quantity=i.quantity) for i in cart if i.quantity > 0]
    order = place_order(db, customer, items)
    conversation.cart_json = "[]"
    db.commit()
    db.refresh(order)

    summary = f"Order #{order.id} - Total ₹{order.total_amount:g}"
    try:
        link = ensure_payment_link(db, order)
    except Exception as e:
        # Razorpay not configured yet, or the link call failed - the order is still saved
        # on the dashboard, and the seller can share payment details by hand.
        return f"{summary}\nThe seller will send you the payment details shortly."
    return f"{summary}\nPay here: {link}"


async def handle_incoming(phone: str, text: str, profile_name: str | None = None):
    with SessionLocal() as db:
        conversation = _get_or_create_conversation(db, phone, profile_name)
        db.add(models.Message(conversation_id=conversation.id, sender="customer", text=text))
        db.commit()
        db.refresh(conversation)

        if conversation.ai_paused:
            return  # seller has taken over this chat from the dashboard

        products = db.query(models.Product).filter(models.Product.business_id == DEFAULT_BUSINESS_ID).all()
        try:
            turn = await ai.run_turn(conversation, products)
        except Exception as e:
            # Refusal, API outage, missing key, bad output... never leave the customer unanswered.
            print("AI ERROR:", repr(e))
            conversation.ai_paused = True
            await _reply(db, conversation, HANDOFF_REPLY)
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
            problems = _stock_problems(db, turn.cart)
            if problems:
                # Stock changed since Claude saw it - let the seller sort it out rather than guess.
                conversation.ai_paused = True
                print("STOCK PROBLEM:", problems)
                reply = HANDOFF_REPLY
            else:
                reply = f"{reply}\n\n{_checkout(db, conversation, turn.cart)}"

        await _reply(db, conversation, reply)


async def _reply(db, conversation: models.Conversation, text: str):
    db.add(models.Message(conversation_id=conversation.id, sender="ai", text=text))
    db.commit()
    try:
        await send_message(conversation.phone, text)
    except WhatsAppSendError as e:
        print("AI REPLY NOT DELIVERED:", e)
