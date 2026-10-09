"""Claude-powered order assistant: reads the customer's WhatsApp message, keeps the cart up to
date, collects name + address, and replies in the customer's own language - all in one call."""
import base64
import json
from typing import Literal

import anthropic
from pydantic import BaseModel

import models

MODEL = "claude-opus-5-5"
HISTORY_LIMIT = 20

_client = anthropic.AsyncAnthropic()  # reads ANTHROPIC_API_KEY from the environment

SYSTEM_PROMPT = """You are the WhatsApp order assistant for a small Indian business. Customers message \
you to buy products from the catalog below. Your job is to take the order end to end so the seller only \
has to pack and ship.

How to handle each message:
- Detect the language the customer is writing in (English, Tamil, Hindi, Telugu, Arabic, Malay, Tanglish, \
Hinglish, etc.) and write your reply in that same language and script. Product names may stay as they \
appear in the catalog.
- Keep the cart up to date. Map what the customer asks for to catalog products by meaning, even with \
spelling mistakes or local-language names. Return the FULL cart after this message (not just the change). \
Remove items the customer cancels. Only use product_ids that exist in the catalog.
- If a requested product isn't in the catalog, say so politely and suggest the closest match.
- Do not promise more than the stock shown.
- Before an order can be placed you need: at least one cart item, the customer's name, and a full \
delivery address (house/street, area, city, pincode). Ask for whatever is missing, one short question at a time.
- When everything is collected, show a short summary (items, quantities, total in ₹) and ask the customer \
to confirm. Set ready_to_place_order to true ONLY after the customer has clearly confirmed that summary. \
When you set it to true, your reply should just thank them; the system will append the order number, \
total and payment link itself - do not invent a payment link.
- Set needs_human to true when the customer asks for a person, complains, asks about refunds, a damaged \
or missing delivery, custom requests, bulk/wholesale pricing, or anything you can't answer from the \
catalog. In that case, tell them the seller will reply shortly.
- Customers may send a photo (e.g. a product picture or a handwritten list). If one is attached, read it and treat what it shows as part of their message. Voice notes can't be listened to yet - if the customer sent one, politely ask them to type their order instead.
- If the customer asks about an earlier order (status, delivery, tracking, payment), answer from <customer_orders>. If an order is unpaid and has a payment link, you may share that link again. If you can't find the order they mean, or they report a problem with it, set needs_human to true. If they want to repeat an earlier order ("same as last time"), fill the cart from that order using products that are still in the catalog at today's prices, mention anything no longer available, and confirm the summary as usual - their saved name and address can be reused if they confirm them.
- Keep replies short and friendly, like a helpful shop assistant on WhatsApp. No markdown headings.
- Never make up prices, discounts, delivery dates or policies."""


class CartItem(BaseModel):
    product_id: int
    quantity: int


class AssistantTurn(BaseModel):
    language: str
    intent: Literal["order", "catalog_question", "order_status", "greeting", "other"]
    reply: str
    cart: list[CartItem]
    customer_name: str | None
    delivery_address: str | None
    ready_to_place_order: bool
    needs_human: bool


class AssistantUnavailable(Exception):
    """Claude declined or returned nothing usable - hand the chat to the seller."""


def _catalog_text(products: list[models.Product]) -> str:
    if not products:
        return "(The catalog is empty.)"
    lines = [f"- id {p.id}: {p.name} - ₹{p.price:g} per {p.unit} (in stock: {p.stock})" for p in products]
    return "\n".join(lines)


def _transcript(conversation: models.Conversation) -> str:
    labels = {"customer": "Customer", "ai": "You", "seller": "Seller (human)", "system": "Automatic update"}
    recent = conversation.messages[-HISTORY_LIMIT:]
    return "\n".join(f"{labels.get(m.sender, m.sender)}: {m.text}" for m in recent)


def _orders_text(orders: list[models.Order]) -> str:
    if not orders:
        return "(No earlier orders.)"
    lines = []
    for o in orders:
        items = ", ".join(f"{i.quantity} x {i.product.name} (product_id {i.product_id})" for i in o.items)
        payment = "paid" if o.is_paid else (f"unpaid - payment link {o.payment_link_url}" if o.payment_link_url else "unpaid")
        tracking = f", tracking: {o.tracking_info}" if o.tracking_info else ""
        placed = o.created_at.strftime("%d %b %Y") if o.created_at else "?"
        lines.append(f"- Order #{o.id} placed {placed}: {items}; total ₹{o.total_amount:g}; {payment}; status {o.status}{tracking}")
    return "\n".join(lines)


# Image types the Claude API accepts.
SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


async def run_turn(
    shop_name: str,
    conversation: models.Conversation,
    products: list[models.Product],
    image: tuple[bytes, str] | None = None,
    recent_orders: list[models.Order] | None = None,
) -> AssistantTurn:
    """`image` is an optional (bytes, mime_type) photo attached to the customer's latest message."""
    state = {
        "cart": json.loads(conversation.cart_json or "[]"),
        "customer_name": conversation.customer_name,
        "delivery_address": conversation.delivery_address,
    }
    prompt = (
        f"<shop>{shop_name}</shop>\n\n"
        f"<catalog>\n{_catalog_text(products)}\n</catalog>\n\n"
        f"<collected_so_far>\n{json.dumps(state, ensure_ascii=False)}\n</collected_so_far>\n\n"
        f"<customer_orders>\n{_orders_text(recent_orders or [])}\n</customer_orders>\n\n"
        f"<conversation>\n{_transcript(conversation)}\n</conversation>\n\n"
        "Respond to the customer's latest message."
    )
    user_content: list[dict] = []
    if image and image[1] in SUPPORTED_IMAGE_TYPES:
        user_content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": image[1], "data": base64.standard_b64encode(image[0]).decode()},
        })
    user_content.append({"type": "text", "text": prompt})

    response = await _client.messages.parse(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": user_content}],
        output_format=AssistantTurn,
    )

    if response.stop_reason == "refusal" or response.parsed_output is None:
        raise AssistantUnavailable(f"stop_reason={response.stop_reason} request_id={response._request_id}")
    return response.parsed_output
