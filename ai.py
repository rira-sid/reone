"""AI chat assistant: sorts each customer message - wants to shop (gets the shop link, cart pre-filled),
asks about their order (answered from order data), or anything else (goes to the seller) - in one call.

AI_PROVIDER picks the model: "gemini" (default, Google's free tier, needs GEMINI_API_KEY) or
"anthropic" (Claude, paid, needs ANTHROPIC_API_KEY). Switching is just an env var change."""
import asyncio
import base64
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
# Voice notes never go to the lighter fallback model - it mishears speech and answers something
# random. Wait longer for the main model instead; if it still can't, the customer is asked to resend.
GEMINI_AUDIO_RETRY_DELAYS = (3, 6, 10, 15)
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

SYSTEM_PROMPT = """You are the WhatsApp assistant of a small shop. Customers order by tapping a shop link (they browse the catalog, add to cart and pay on that page), so you never take orders in the chat. Your job is to understand each customer message and sort it, so it gets handled automatically or goes to the seller.

Set intent to one of:
- "shop": they want to buy, order, browse, see the menu / products / prices, or repeat an earlier order. Put the products they mentioned (with quantities, default 1) in cart, mapping what they said to catalog product ids by meaning - even with spelling mistakes or local-language names. If a product has sizes, pick the variant_id they asked for, else leave variant_id null. For "same as last time", fill cart from their latest order in <customer_orders>. Only use ids that exist in the catalog. Also use "shop" when they ask whether a product is available, its price, its sizes, or what it is - anything the catalog itself answers. reply MUST directly answer what they said, using only the catalog: e.g. "Yes, Mutton Masala is available - ₹120 a packet. I've added 2 packets to your cart, tap the button below to order." If something is out of stock or not in the catalog, say so plainly and suggest the closest product that is available. If they just want to browse, one friendly line inviting them to tap the button. Never say "here is our shop" without answering their question first.
- "order_status": they ask about one of THEIR existing orders - where it is, delivery status, tracking, payment status, payment link, invoice. Answer in reply using ONLY <customer_orders>: order number, status, paid or not, tracking if any, and the payment link again if it's unpaid. If they report a problem with an order (late, damaged, wrong, missing, refund) use "question" instead.
- "greeting": just hello / thanks / ok / bye with nothing else to handle. reply with one short friendly line that fits what they said (thanks -> "you're welcome", bye -> a warm goodbye, hello -> greet them and say you can help them order).
- "change_language": they ask to change the language.
- "unclear": ONLY for a voice note you genuinely can't make out (noise, silence, too faint, cut off). \
Never guess what an unclear voice note says - use "unclear" and leave reply empty.
- "question": everything else - questions about products, delivery areas or times, discounts, custom or bulk requests, complaints, refunds, problems, talking to a person, or anything you're not sure about. The seller answers these personally; leave reply empty.

Rules:
- Language: reply in the language of the customer's LATEST message, in that language's own script (Tamil in Tamil script, Hindi in Devanagari...), even if it differs from <language>. Only if the latest message has no clear language (an emoji, "ok", a number) use <language>. Set language_code to the language of the customer's latest message: one of en, hi, ta, te, ml, kn, bn, mr, gu, or, pa, ur, ar, ms, id, es, fr, pt, zh - or null if it has no clear language. Tanglish / Hinglish (Tamil or Hindi typed in English letters) count as ta / hi.
- Behave the same in every language: the same kind of message must get the same intent whether it is written in English, Tamil, Hindi or any other language, typed or spoken.
- Customers may send a voice note. If audio is attached, listen to the WHOLE recording carefully, treat what they said as their message and set heard_text to exactly what they said, word for word, in their language and script. Answer what they actually said - never a generic reply. Otherwise leave heard_text null.
- Customers may send a photo (a product picture or a handwritten list). Treat what it shows as part of their message.
- Never confirm or imply that a payment was received unless <customer_orders> says the order is paid. A customer saying "paid", or sending a screenshot or transaction ID, does not make it paid.
- Never make up prices, discounts, delivery dates or policies. Keep replies short, like a friendly shop assistant on WhatsApp. No markdown headings."""


class CartItem(BaseModel):
    product_id: int
    quantity: int
    variant_id: int | None = None


class AssistantTurn(BaseModel):
    intent: Literal["shop", "order_status", "greeting", "change_language", "question", "unclear"]
    reply: str
    cart: list[CartItem]
    heard_text: str | None = None
    language_code: str | None = None


class VoiceNotHeard(Exception):
    """A voice note couldn't be transcribed - ask the customer to resend or type it."""


class AssistantUnavailable(Exception):
    """The model declined or returned nothing usable - hand the chat to the seller."""


def _catalog_text(products: list[models.Product]) -> str:
    if not products:
        return "(The catalog is empty.)"
    lines = []
    for p in sorted(products, key=lambda p: ((p.category or "").lower(), p.name.lower())):
        category = f"[{p.category}] " if p.category else ""
        description = f" - {p.description}" if p.description else ""
        variants = p.active_variants
        if variants:
            sizes = "; ".join(f"variant_id {v.id}: {v.label} ₹{v.price:g} (in stock: {v.stock})" for v in variants)
            lines.append(f"- id {p.id}: {category}{p.name} - sizes: {sizes}{description}")
        else:
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
        items = ", ".join(f"{i.quantity} x {i.display_name} (product_id {i.product_id}"
                          + (f", variant_id {i.variant_id})" if i.variant_id else ")") for i in o.items)
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
    language: str = "English",
) -> AssistantTurn:
    """`image` / `audio` are an optional (bytes, mime_type) photo or voice note attached to the
    customer's latest message. Only Gemini can listen to audio; Claude is told to ask for text."""
    prompt = (
        f"<shop>{shop_name}</shop>\n\n"
        f"<language>{language}</language>\n\n"
        f"<shop_policies>\n{policies or '(none)'}\n</shop_policies>\n\n"
        f"<catalog>\n{_catalog_text(products)}\n</catalog>\n\n"
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
    if audio:
        delays = GEMINI_AUDIO_RETRY_DELAYS
        attempts = [GEMINI_MODEL] * (len(delays) + 1)
    else:
        delays = GEMINI_RETRY_DELAYS
        attempts = [GEMINI_MODEL] * (len(delays) + 1) + [GEMINI_FALLBACK_MODEL]
    for i, model in enumerate(attempts):
        try:
            response = await _gemini_client().aio.models.generate_content(model=model, contents=contents, config=config)
            break
        except errors.APIError as e:
            busy = e.code in (429, 500, 503, 504)
            if not busy or i == len(attempts) - 1:
                if audio and busy:
                    raise VoiceNotHeard(f"Gemini busy ({e.code}) - voice note not transcribed") from e
                raise
            print(f"GEMINI BUSY ({e.code}) on {model}, retrying")
            if i < len(delays):
                await asyncio.sleep(delays[i])
    if isinstance(response.parsed, AssistantTurn):
        return response.parsed
    if not response.text:
        raise AssistantUnavailable(f"Gemini returned no text (prompt_feedback={response.prompt_feedback})")
    try:
        return AssistantTurn.model_validate_json(response.text)
    except ValueError as exc:
        raise AssistantUnavailable(f"Gemini returned unparseable output: {exc}") from exc
