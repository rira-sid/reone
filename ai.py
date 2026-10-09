"""Claude-powered order assistant: reads the customer's WhatsApp message, keeps the cart up to
date, collects name + address, and replies in the customer's own language - all in one call."""
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
    labels = {"customer": "Customer", "ai": "You", "seller": "Seller (human)"}
    recent = conversation.messages[-HISTORY_LIMIT:]
    return "\n".join(f"{labels.get(m.sender, m.sender)}: {m.text}" for m in recent)


async def run_turn(conversation: models.Conversation, products: list[models.Product]) -> AssistantTurn:
    state = {
        "cart": json.loads(conversation.cart_json or "[]"),
        "customer_name": conversation.customer_name,
        "delivery_address": conversation.delivery_address,
    }
    user_content = (
        f"<catalog>\n{_catalog_text(products)}\n</catalog>\n\n"
        f"<collected_so_far>\n{json.dumps(state, ensure_ascii=False)}\n</collected_so_far>\n\n"
        f"<conversation>\n{_transcript(conversation)}\n</conversation>\n\n"
        "Respond to the customer's latest message."
    )

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
