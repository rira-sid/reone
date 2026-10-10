"""AI order assistant: reads the customer's WhatsApp message, keeps the cart up to date, collects
name + address, and replies in the customer's own language - all in one call.

AI_PROVIDER picks the model: "gemini" (default, Google's free tier, needs GEMINI_API_KEY) or
"anthropic" (Claude, paid, needs ANTHROPIC_API_KEY). Switching is just an env var change."""
import asyncio
import base64
import json
import os
from typing import Literal

from pydantic import BaseModel

import models

AI_PROVIDER = os.getenv("AI_PROVIDER", "gemini").lower()
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
# Free-tier Gemini often answers 503 "high demand" or 429 "rate limit" for a few seconds. Retry,
# then try the lighter model, before giving up and handing the chat to the seller.
GEMINI_FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-flash-lite-latest")
GEMINI_RETRY_DELAYS = (2, 5)
HISTORY_LIMIT = 20

_clients: dict = {}  # created on first use, so a missing key for the unused provider doesn't matter


def _anthropic_client():
    if "anthropic" not in _clients:
        import anthropic
        _clients["anthropic"] = anthropic.AsyncAnthropic()  # reads ANTHROPIC_API_KEY
    return _clients["anthropic"]


def _gemini_client():
    if "gemini" not in _clients:
        from google import genai
        _clients["gemini"] = genai.Client()  # reads GEMINI_API_KEY
    return _clients["gemini"]

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
- Customers may send a photo (e.g. a product picture or a handwritten list). If one is attached, read it and treat what it shows as part of their message.
- Customers may send a voice note instead of typing. If audio is attached, listen to it and treat what they said as their message, and set heard_text to exactly what they said, in their language and script. Otherwise leave heard_text null. If a message is just "[voice note]" and no audio is attached, politely ask them to type it instead.
- Always also write spoken_reply: your reply as it would sound spoken aloud in a short WhatsApp voice note, in the customer's language - natural speech, no links, emojis, lists or symbols, under 40 words. When you set ready_to_place_order to true, spoken_reply should thank them, say their order is confirmed with the total amount, and that the payment details are in the chat.
- If the customer asks about an earlier order (status, delivery, tracking, payment), answer from <customer_orders>. If an order is unpaid and has a payment link, you may share that link again. If you can't find the order they mean, or they report a problem with it, set needs_human to true. If they want to repeat an earlier order ("same as last time"), fill the cart from that order using products that are still in the catalog at today's prices, mention anything no longer available, and confirm the summary as usual - their saved name and address can be reused if they confirm them.
- Follow <shop_policies>. If the shop is not accepting orders right now, don't take an order: \
politely pass on the shop's message (in the customer's language) and still answer questions. Include the \
delivery fee in the order summary when it applies, and don't confirm an order below the minimum order value.
- If cash on delivery is available, ask whether they'd like to pay online or cash on delivery before \
confirming, and set payment_method. If it isn't available, payment is online - set payment_method to "online".
- Put any special instructions for this order (e.g. "less spicy", "deliver after 6pm") in order_note.
- If the customer asks for the menu or price list, you can share the catalog link from <shop_policies> \
as well as answering directly.
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
    payment_method: Literal["online", "cod"] | None
    order_note: str | None
    heard_text: str | None = None
    spoken_reply: str | None = None


class AssistantUnavailable(Exception):
    """The model declined or returned nothing usable - hand the chat to the seller."""


def _catalog_text(products: list[models.Product]) -> str:
    if not products:
        return "(The catalog is empty.)"
    lines = []
    for p in sorted(products, key=lambda p: ((p.category or "").lower(), p.name.lower())):
        category = f"[{p.category}] " if p.category else ""
        description = f" - {p.description}" if p.description else ""
        lines.append(f"- id {p.id}: {category}{p.name} - ₹{p.price:g} per {p.unit} (in stock: {p.stock}){description}")
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


# Image types both providers accept.
SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
# Audio Gemini can listen to; WhatsApp voice notes arrive as "audio/ogg; codecs=opus".
SUPPORTED_AUDIO_TYPES = {"audio/ogg", "audio/mpeg", "audio/mp3", "audio/wav", "audio/aac", "audio/flac", "audio/mp4"}


async def run_turn(
    shop_name: str,
    conversation: models.Conversation,
    products: list[models.Product],
    image: tuple[bytes, str] | None = None,
    recent_orders: list[models.Order] | None = None,
    policies: str = "",
    audio: tuple[bytes, str] | None = None,
) -> AssistantTurn:
    """`image` / `audio` are an optional (bytes, mime_type) photo or voice note attached to the
    customer's latest message. Only Gemini can listen to audio; Claude is told to ask for text."""
    state = {
        "cart": json.loads(conversation.cart_json or "[]"),
        "customer_name": conversation.customer_name,
        "delivery_address": conversation.delivery_address,
    }
    prompt = (
        f"<shop>{shop_name}</shop>\n\n"
        f"<shop_policies>\n{policies or '(none)'}\n</shop_policies>\n\n"
        f"<catalog>\n{_catalog_text(products)}\n</catalog>\n\n"
        f"<collected_so_far>\n{json.dumps(state, ensure_ascii=False)}\n</collected_so_far>\n\n"
        f"<customer_orders>\n{_orders_text(recent_orders or [])}\n</customer_orders>\n\n"
        f"<conversation>\n{_transcript(conversation)}\n</conversation>\n\n"
        "Respond to the customer's latest message."
    )
    if image and image[1] not in SUPPORTED_IMAGE_TYPES:
        image = None
    if audio:
        audio = (audio[0], audio[1].split(";")[0].strip())
        if audio[1] not in SUPPORTED_AUDIO_TYPES:
            audio = None
    if AI_PROVIDER == "anthropic":
        return await _run_claude(prompt, image)
    return await _run_gemini(prompt, image, audio)


async def _run_claude(prompt: str, image: tuple[bytes, str] | None) -> AssistantTurn:
    user_content: list[dict] = []
    if image:
        user_content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": image[1], "data": base64.standard_b64encode(image[0]).decode()},
        })
    user_content.append({"type": "text", "text": prompt})

    response = await _anthropic_client().messages.parse(
        model=CLAUDE_MODEL,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": user_content}],
        output_format=AssistantTurn,
    )

    if response.stop_reason == "refusal" or response.parsed_output is None:
        raise AssistantUnavailable(f"stop_reason={response.stop_reason} request_id={response._request_id}")
    return response.parsed_output


async def _run_gemini(prompt: str, image: tuple[bytes, str] | None, audio: tuple[bytes, str] | None = None) -> AssistantTurn:
    from google.genai import types

    contents: list = []
    if image:
        contents.append(types.Part.from_bytes(data=image[0], mime_type=image[1]))
    if audio:
        contents.append(types.Part.from_bytes(data=audio[0], mime_type=audio[1]))
    contents.append(prompt)

    from google.genai import errors

    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        response_mime_type="application/json",
        response_schema=AssistantTurn,
    )
    attempts = [GEMINI_MODEL] * (len(GEMINI_RETRY_DELAYS) + 1) + [GEMINI_FALLBACK_MODEL]
    for i, model in enumerate(attempts):
        try:
            response = await _gemini_client().aio.models.generate_content(model=model, contents=contents, config=config)
            break
        except errors.APIError as e:
            busy = e.code in (429, 500, 503, 504)
            if not busy or i == len(attempts) - 1:
                raise
            print(f"GEMINI BUSY ({e.code}) on {model}, retrying")
            if i < len(GEMINI_RETRY_DELAYS):
                await asyncio.sleep(GEMINI_RETRY_DELAYS[i])
    if isinstance(response.parsed, AssistantTurn):
        return response.parsed
    if not response.text:
        raise AssistantUnavailable(f"Gemini returned no text (prompt_feedback={response.prompt_feedback})")
    try:
        return AssistantTurn.model_validate_json(response.text)
    except ValueError as exc:
        raise AssistantUnavailable(f"Gemini returned unparseable output: {exc}") from exc
