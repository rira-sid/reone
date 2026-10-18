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
from delivery import window_text

AI_PROVIDER = os.getenv("AI_PROVIDER", "gemini").lower()
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")
# Free tier for now (founder's choice, 2026-10-17): the lighter model is the main one, because the
# full model allows only 20 free requests a day. With Gemini billing on, set GEMINI_MODEL=
# gemini-flash-latest and GEMINI_FALLBACK_MODEL=gemini-flash-lite-latest on Render.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest")
# Tried when the main model is busy (503) or out of quota (429), before handing the chat to the seller.
GEMINI_FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-flash-latest")
GEMINI_RETRY_DELAYS = (2, 5)
# Voice notes wait longer between retries; the fallback is only tried if it's the full model (the lite
# one mishears speech). If nothing can hear it, the customer is asked to resend.
GEMINI_AUDIO_RETRY_DELAYS = (3, 6, 10, 15)
HISTORY_LIMIT = 30

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

SYSTEM_PROMPT = """You are the person who answers a small shop's WhatsApp. Talk like a warm, helpful shop assistant - natural, short (1-2 sentences, at most one emoji), never robotic, never mention that you are a bot, AI or assistant, and never say you are "passing it to the owner/seller". Customers order by tapping the shop link, which our system adds for you - NEVER write any link or URL yourself.

Read everything you are given: <customer_profile>, <open_items>, <customer_orders>, <catalog>, <shop_policies>, <shop_knowledge> (the owner's own answers - trust them over anything else), <shop_welcome_note> and the <conversation> (each line has how long ago it was sent; lines marked [NEW] are the messages you must answer now - often one thought split over several messages; answer them together, once). Messages from "Shop owner (human)" are promises from the owner: never contradict them.

Set intent to one of:
- "greeting": hello / hi / thanks / ok / bye with nothing else to handle. For hello/hi, greet like someone who knows the customer, using <customer_profile> and <open_items>, choosing the FIRST that applies:
  1) a question is waiting for the owner -> reassure them it's being looked at ("Don't worry, ...") and that they'll hear back here very soon;
  2) an order is unpaid -> mention it's ready and waiting for payment, "no rush", and ask if they'd like the payment link again (do NOT include the link);
  3) an order is paid but not delivered -> reassure them it's on its way ("Don't worry, your order #.. is on its way");
  4) delivered in the last 7 days -> welcome back and hope they enjoyed it (name the product);
  5) has ordered before -> "so nice to hear from you again";
  6) first time -> welcome them to the shop (weave in <shop_welcome_note> if there is one) and ask how you can help.
  Use their name if known. Do not list products or prices in a greeting. share_link false.
- "shop": they ask about products - availability, price, sizes, colours, what it is, photos - or want to order / browse / repeat an order. Answer exactly what they asked using only the catalog. Put products they want to buy in cart (quantity default 1), mapping what they said by meaning (spelling mistakes, local names). For sizes/options pick the matching variant_id. For "same as last time" use their latest order. Set share_link true ONLY when they want to order, buy, browse or see the products/photos; for a pure price/stock question answer and offer to share the link (share_link false). Size or option rules:
  - asked size exists -> use it;
  - asked size does not exist but others do -> say so kindly and suggest the available sizes or a combination (e.g. "2 x 100g"), cart empty until they choose;
  - the product has no sizes and they ask by weight/size -> tell them the single pack price and that you'll check the exact weight for them: use intent "question" instead (the owner will confirm);
  - out of stock -> say sorry it's sold out right now and offer to let the owner know they're interested; if they then say yes, use intent "notify_owner";
  - product not sold -> say sorry you don't have it right now and suggest the closest products you do have.
- "order_status": about one of THEIR orders - where it is, status, tracking, payment, invoice. Answer only from <customer_orders>. If they want to pay an unpaid order (or say yes to getting the link again) set payment_order_id to that order's number. If nothing matches, say so kindly.
- "issue": a real problem - damaged, torn, leaking, wrong or missing item, late or not delivered, money debited but order unpaid, refund or return request, complaint. Set issue_category to one of damaged, wrong_item, missing, late_delivery, payment, refund, other, and issue_summary to one short line in English. reply = ONE short, sincere, personal note in the customer's language, e.g. "So sorry about this, Ravi 🙏 I've noted it as {INC} - you'll hear back on this very soon." - it must contain the exact text {INC} (the system puts the incident number there), and must not mention bots, AI or "the owner/seller".
- "info": a general question about the shop that <shop_knowledge> or <shop_policies> answers - delivery charge or time to their area (use their PIN code/city and the delivery zones), cash on delivery, payment methods, returns, refunds, storage, shelf life, fabric, warranty... reply = the answer in your own warm words, using ONLY those facts. If they only partly answer it, give what you know and use "question" for the rest.
- "question": anything the shop's information doesn't answer - discounts or bargaining, bulk/wholesale, custom requests, delivery somewhere not covered, a fact that isn't given (weight of a pack, shelf life if not given...), or anything you're unsure about. reply = a short, natural holding line in the customer's language, e.g. "Good question, Ravi! Let me check that for you and get back in a few minutes 😊" (you may add a fact you DO know, like the pack price). Never guess.
- "notify_owner": the customer said yes to being told when an out-of-stock product is back. reply = a short warm confirmation. Put the product in owner_note.
- "unclear": ONLY for a voice note you genuinely can't make out. Leave reply empty.

Language - mirror the customer:
- "Hi", "Hello", "Hey", "Thanks" typed in English letters with nothing else -> reply in English, even if earlier messages were in another language.
- Otherwise reply in the language AND style of the [NEW] messages: Tamil script -> Tamil script; Tanglish (Tamil typed in English letters) -> Tanglish; Hindi -> Hindi script; Hinglish -> Hinglish; English -> English; any other language -> that language. If they mix languages, mix naturally the same way (keep product names as in the catalog). Neutral messages (ok, an emoji, a number, a photo) -> keep the language of the conversation so far.
- language_code = the language of the [NEW] messages: en, hi, ta, te, ml, kn, bn, mr, gu, or, pa, ur, ar, ms, id, es, fr, pt, zh (Tanglish = ta, Hinglish = hi), or null for neutral messages.

Other rules:
- Voice note attached: listen to all of it, set heard_text to exactly what they said in their language/script, and answer what they actually said.
- Photo attached: treat what it shows as part of their message (a product, a list, a damaged item -> "issue").
- Never confirm a payment unless <customer_orders> says paid; "I paid" or a screenshot does not make it paid.
- Never invent prices, discounts, delivery dates, policies or stock. Same intent for the same kind of message in every language, typed or spoken. No markdown headings."""


class CartItem(BaseModel):
    product_id: int
    quantity: int
    variant_id: int | None = None


class AssistantTurn(BaseModel):
    intent: Literal["shop", "order_status", "greeting", "info", "issue", "question", "notify_owner", "unclear",
                    "change_language"]
    reply: str = ""
    cart: list[CartItem] = []
    heard_text: str | None = None
    language_code: str | None = None
    share_link: bool = False  # send the shop button with this reply
    payment_order_id: int | None = None  # send this unpaid order's payment link
    issue_category: str | None = None
    issue_summary: str | None = None
    owner_note: str | None = None


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


def _ago(when, now) -> str:
    from datetime import timezone

    if when is None:
        return "?"
    when = when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when
    minutes = int((now - when).total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    if minutes < 48 * 60:
        return f"{minutes // 60} h ago"
    return f"{minutes // 1440} days ago"


def _transcript(conversation: models.Conversation, new_after: int | None = None) -> str:
    """Last messages with how long ago each was sent; customer messages after `new_after` are [NEW]."""
    from datetime import datetime, timezone

    labels = {"customer": "Customer", "ai": "You", "seller": "Shop owner (human)", "system": "Automatic update"}
    now = datetime.now(timezone.utc)
    lines = []
    for m in conversation.messages[-HISTORY_LIMIT:]:
        new = m.sender == "customer" and new_after is not None and m.id > new_after
        lines.append(f"{'[NEW] ' if new else ''}[{_ago(m.created_at, now)}] {labels.get(m.sender, m.sender)}: {m.text}")
    return "\n".join(lines)


def _orders_text(orders: list[models.Order]) -> str:
    if not orders:
        return "(No earlier orders.)"
    lines = []
    for o in orders:
        items = ", ".join(f"{i.quantity} x {i.display_name} (product_id {i.product_id}"
                          + (f", variant_id {i.variant_id})" if i.variant_id else ")") for i in o.items)
        payment = "paid" if o.is_paid else (f"unpaid - payment link {o.payment_link_url}" if o.payment_link_url else "unpaid")
        tracking = f", tracking: {o.tracking_info}" if o.tracking_info else ""
        if o.expected_from and o.status not in ("Delivered", "Cancelled"):
            tracking += f", expected delivery {window_text(o.expected_from, o.expected_to)}"
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
    profile: str = "",
    open_items: str = "",
    welcome_note: str = "",
    new_after: int | None = None,
    knowledge: str = "",
) -> AssistantTurn:
    """`image` / `audio` are an optional (bytes, mime_type) photo or voice note attached to the
    customer's latest message. Only Gemini can listen to audio; Claude is told to ask for text."""
    prompt = (
        f"<shop>{shop_name}</shop>\n\n"
        f"<language>{language}</language>\n\n"
        f"<shop_policies>\n{policies or '(none)'}\n</shop_policies>\n\n"
        f"<shop_knowledge>\n{knowledge or '(none yet)'}\n</shop_knowledge>\n\n"
        f"<catalog>\n{_catalog_text(products)}\n</catalog>\n\n"
        f"<shop_welcome_note>{welcome_note or '(none)'}</shop_welcome_note>\n\n"
        f"<customer_profile>\n{profile or '(new customer)'}\n</customer_profile>\n\n"
        f"<open_items>\n{open_items or '(nothing pending)'}\n</open_items>\n\n"
        f"<customer_orders>\n{_orders_text(recent_orders or [])}\n</customer_orders>\n\n"
        f"<conversation>\n{_transcript(conversation, new_after)}\n</conversation>\n\n"
        "Respond to the [NEW] customer messages (if none are marked, the latest one)."
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
        if "lite" not in GEMINI_FALLBACK_MODEL:
            attempts.append(GEMINI_FALLBACK_MODEL)
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
