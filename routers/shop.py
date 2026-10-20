"""The customer shop: a colourful, easy catalog page (photos, sizes, nutrition facts, cart) with
checkout on the same page - name, address, pay. The order is placed automatically and the customer
goes straight to payment; confirmation, invoice and updates follow on WhatsApp.

Links sent in WhatsApp carry `c` (the customer's number, signed - so it can't be changed), `lang`
(their chosen language) and optionally `add` (cart pre-filled from what they asked for in chat)."""
import json
import re
from datetime import datetime, timedelta, timezone
from html import escape

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

import models
import schemas
from business import whatsapp_creds
import zones
from database import get_db
from delivery import IST, expected_line, window_text
from languages import LANGUAGES, RTL, TEXTS, available_languages, lang_code, language_name, t
from routers.orders import place_order
from routers.pay import effective_payment_method, pay_url, send_payment_request
from whatsapp import WhatsAppSendError, send_message

router = APIRouter(prefix="/shop", tags=["shop"])


def _wa_link(phone: str | None, text: str = "") -> str | None:
    from urllib.parse import quote

    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 10:  # Indian mobile without country code
        digits = "91" + digits
    if len(digits) < 11:
        return None
    return f"https://wa.me/{digits}" + (f"?text={quote(text)}" if text else "")


@router.get("/image/{image_id}")
def product_image(image_id: int, db: Session = Depends(get_db)):
    image = db.get(models.ProductImage, image_id)
    if not image:
        raise HTTPException(status_code=404, detail="Not found")
    # Photos never change in place (a new upload gets a new id), so browsers can keep them.
    return Response(image.data, media_type=image.mime_type,
                    headers={"Cache-Control": "public, max-age=604800, immutable"})


@router.get("/media/{image_id}")
def shop_media(image_id: int, db: Session = Depends(get_db)):
    """A business's logo / banner."""
    image = db.get(models.ShopImage, image_id)
    if not image:
        raise HTTPException(status_code=404, detail="Not found")
    return Response(image.data, media_type=image.mime_type,
                    headers={"Cache-Control": "public, max-age=604800, immutable"})


def _record(db: Session, business_id: int, kind: str):
    db.add(models.ShopEvent(business_id=business_id, kind=kind))
    db.commit()


@router.post("/{business_id}/event")
async def shop_event(business_id: int, request: Request, db: Session = Depends(get_db)):
    """Funnel beacons from the shop page (navigator.sendBeacon posts the kind as plain text)."""
    kind = (await request.body()).decode(errors="ignore").strip()
    if kind in ("add_to_cart", "checkout_open") and db.get(models.Business, business_id):
        _record(db, business_id, kind)
    return Response(status_code=204)


class ShopVisitIn(BaseModel):
    visitor: str | None = None
    source: str | None = None
    staff: bool = False


@router.post("/{business_id}/visit")
def shop_visit(business_id: int, payload: ShopVisitIn, request: Request, background: BackgroundTasks,
               db: Session = Depends(get_db)):
    """Sent by the shop page once it has opened in a browser - link previews and crawlers that only
    fetch the HTML never send it, so a visit means a person actually saw the shop."""
    from tracking import record_visit, request_info

    if db.get(models.Business, business_id):
        background.add_task(record_visit, request_info(request), "shop", business_id,
                            (payload.source or "Direct / WhatsApp")[:60], (payload.visitor or "")[:64] or None,
                            payload.staff)
    return Response(status_code=204)


def _own_number(db: Session, business: models.Business, phone: str | None) -> bool:
    """The shop's own numbers (or the founder's) testing their shop link - not a customer."""
    from tracking import founder_phone

    tail = lambda p: re.sub(r"\D", "", p or "")[-10:]  # noqa: E731
    return bool(phone) and tail(phone) in {tail(p) for p in (business.phone, business.alert_phone, founder_phone(db)) if p}


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def _shade(color: str, factor: float) -> str:
    """factor < 1 darkens, > 1 lightens (towards white)."""
    r, g, b = _hex_to_rgb(color)
    if factor <= 1:
        r, g, b = (int(v * factor) for v in (r, g, b))
    else:
        r, g, b = (int(v + (255 - v) * (factor - 1)) for v in (r, g, b))
    return f"#{r:02x}{g:02x}{b:02x}"


def _nutrition_rows(text: str | None) -> list[list[str]]:
    rows = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        label, _, value = line.partition(":")
        if label.strip() and value.strip():
            rows.append([label.strip(), value.strip()])
    return rows


def _catalog_json(business: models.Business, products: list[models.Product], tr: dict | None = None) -> list[dict]:
    """Products for the shop page, in the customer's language where a translation exists (`tr`)."""
    from routers.products import image_url

    tr = tr or {}
    # One label per category, so every product of a category groups under the same heading.
    category_label = {}
    for p in products:
        row = tr.get(p.id)
        if p.category and row and row.category and p.category not in category_label:
            category_label[p.category] = row.category
    out = []
    for p in sorted(products, key=lambda p: ((p.category or "~").lower(), p.name.lower())):
        row = tr.get(p.id)
        sizes = json.loads(row.sizes_json or "[]") if row else []
        active = p.active_variants
        if len(sizes) != len(active):
            sizes = [v.label for v in active]
        options = p.options
        names = json.loads(row.options_json or "[]") if row and row.options_json else []
        if len(names) != len(options):
            names = options
        images = {i.id: image_url(i) for i in p.images}
        variants = []
        for v, label in zip(active, sizes):
            # The translated label "M / சிவப்பு" splits back into the two option values.
            parts = [x.strip() for x in label.split(" / ")] if len(options) > 1 else [label]
            if len(parts) != max(len(options), 1):
                parts = [v.option1 or label, v.option2] if options else [label]
            variants.append({"id": v.id, "label": label, "price": v.price, "stock": v.stock,
                             "o1": parts[0] if options else None, "o2": parts[1] if len(options) > 1 else None,
                             "image": images.get(v.image_id)})
        out.append({
            "id": p.id, "name": (row.name if row else p.name),
            "alt": p.name if row else "",  # English name, so searching in English still works
            "category": category_label.get(p.category, p.category or ""),
            "description": (row.description if row and row.description else p.description or ""),
            "unit": p.unit or "", "price": p.price, "stock": p.stock, "variants": variants, "options": names,
            "images": [image_url(i) for i in p.images],
            "nutrition": _nutrition_rows(row.nutrition if row and row.nutrition else p.nutrition),
        })
    return out


@router.get("/{shop_key}", response_class=HTMLResponse)
def catalog(shop_key: str, request: Request, background: BackgroundTasks, c: str | None = None,
            lang: str | None = None, add: str | None = None, src: str | None = None, db: Session = Depends(get_db)):
    from catalog_i18n import translations_for
    from chatbot import phone_from_token
    from routers.settings import shop_image_url
    from slugs import find

    business = find(db, shop_key)
    if not business:
        raise HTTPException(status_code=404, detail="Shop not found")
    if not business.is_live:
        return HTMLResponse("<!doctype html><meta name=viewport content='width=device-width'><title>Unavailable</title>"
                            "<p style='font:18px system-ui;text-align:center;margin-top:30vh'>This shop is "
                            "temporarily unavailable.</p>", status_code=503)
    from tracking import traffic_source

    # The visit itself is counted by the page's own beacon (POST /shop/{id}/visit) once it opens.
    source = traffic_source(request.headers.get("referer"), src, request.headers.get("user-agent"))
    products = (
        db.query(models.Product)
        .filter(models.Product.business_id == business.id, models.Product.is_active.is_(True))
        .all()
    )
    phone = phone_from_token(business.id, c)
    customer = None
    if phone:
        customer = db.query(models.Customer).filter(
            models.Customer.business_id == business.id, models.Customer.phone == phone).first()
        conversation = db.query(models.Conversation).filter(
            models.Conversation.business_id == business.id, models.Conversation.phone == phone).first()
        if not lang and conversation:
            lang = conversation.preferred_language
    code = lang_code(lang)
    prefill = []
    for part in (add or "").split(","):
        m = re.fullmatch(r"(\d+)(?:-(\d+))?:(\d+)", part.strip())
        if m:
            prefill.append({"product_id": int(m[1]), "variant_id": int(m[2]) if m[2] else None,
                            "quantity": min(int(m[3]), 99)})
    rules = {
        "delivery_fee": business.delivery_fee or 0, "free_above": business.free_delivery_above or 0,
        "min_order": business.min_order_amount or 0, "cod": business.cod_enabled,
        "open": business.accepting_orders,
        "closed_message": business.closed_message or t(code, "closed"),
        "zones": bool(zones.business_zones(db, business)),  # checkout asks for the PIN code and prices by zone
    }
    data = {
        "shop": {"id": business.id, "name": business.name, "address": business.address or "",
                 "type": business.business_type or "",
                 "tagline": business.tagline or "", "about": business.about or "",
                 "logo": shop_image_url(business.logo_image_id), "banner": shop_image_url(business.banner_image_id)},
        "products": _catalog_json(business, products, translations_for(db, products, code)),
        "rules": rules, "lang": code, "texts": {**TEXTS["en"], **TEXTS[code]}, "token": c if phone else None,
        "customer": {"name": (customer.name if customer and customer.name != phone else ""),
                     "address": customer.address if customer else "", "phone": phone or "",
                     "pincode": (customer.pincode or "") if customer else ""},
        "prefill": prefill,
        "languages": [[k, LANGUAGES[k][0]] for k in available_languages()],
        "whatsapp": _wa_link(business.phone),
        "visit": {"source": source, "staff": _own_number(db, business, phone)},
    }
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return HTMLResponse(SHOP_HTML
                        .replace("__LANG__", code)
                        .replace("__DIR__", "rtl" if code in RTL else "ltr")
                        .replace("__TITLE__", escape(business.name))
                        .replace("__THEME__", "#fcf6f0")
                        .replace("__DESC__", escape(business.tagline or "Order and pay in a minute."))
                        .replace("__DATA__", payload))


# ---- checkout --------------------------------------------------------------------------------

class QuoteIn(BaseModel):
    pincode: str | None = None
    address: str | None = None
    subtotal: float = 0


@router.post("/{business_id}/quote")
def quote(business_id: int, payload: QuoteIn, db: Session = Depends(get_db)):
    """Delivery terms for an address, so the checkout shows the right fee, COD and date as the customer types."""
    business = db.get(models.Business, business_id)
    if not business or not business.is_live:
        raise HTTPException(status_code=404, detail="Shop not found")
    terms = zones.terms_for(db, business, payload.pincode, payload.address)
    eta = None
    if terms.days:
        today = datetime.now(IST).date()
        eta = window_text(today + timedelta(days=terms.days[0]), today + timedelta(days=terms.days[1]))
    return {"status": terms.status, "fee": terms.fee_for(payload.subtotal), "rate": terms.fee or 0,
            "free_above": terms.free_above or 0,
            "min_order": terms.min_order or 0, "cod": terms.cod, "eta": eta, "note": terms.note,
            "zone": terms.zone.name if terms.zone else None}


async def _ask_owner_about_area(db, business, conversation, name, address, pincode, items):
    from seller_alerts import alert_seller

    products = {p.id: p.name for p in db.query(models.Product).filter(models.Product.business_id == business.id)}
    cart = ", ".join(f"{i.quantity} × {products.get(i.product_id, '?')}" for i in items if i.quantity > 0)
    question = (f"Wants to order ({cart}) for delivery to: {address}" + (f" - PIN {pincode}" if pincode else "")
                + ". This area isn't in your delivery zones - do you deliver there, and what's the charge?")
    now = datetime.now(timezone.utc)
    conversation.needs_seller = True
    conversation.needs_seller_since = conversation.needs_seller_since or now
    db.add(models.Message(conversation_id=conversation.id, sender="system",
                          text=f"Checkout stopped: delivery area not in your zones ({address}"
                               + (f", PIN {pincode}" if pincode else "") + f"). Cart: {cart}"))
    db.commit()
    await alert_seller(db, business, conversation, question)

class CheckoutItem(BaseModel):
    product_id: int
    variant_id: int | None = None
    quantity: int


class CheckoutIn(BaseModel):
    items: list[CheckoutItem]
    name: str
    address: str
    pincode: str | None = None
    phone: str | None = None
    c: str | None = None  # signed customer token from the WhatsApp link
    note: str | None = None
    payment_method: str = "online"
    lang: str | None = None


def _summary(order: models.Order) -> str:
    lines = [f"🧾 Order #{order.id}"]
    lines += [f"• {i.quantity} × {i.display_name} - ₹{i.price_at_order * i.quantity:g}" for i in order.items]
    if order.delivery_fee:
        lines.append(f"🚚 Delivery ₹{order.delivery_fee:g}")
    lines.append(f"*Total ₹{order.total_amount:g}*")
    lines.append(f"📍 {order.customer.address}")
    if order.customer_note:
        lines.append(f"📝 {order.customer_note}")
    return "\n".join(lines)


@router.post("/{business_id}/checkout")
async def checkout(business_id: int, payload: CheckoutIn, db: Session = Depends(get_db)):
    from chatbot import normalize_phone, phone_from_token
    from notifications import within_service_window
    from seller_alerts import notify_seller

    business = db.get(models.Business, business_id)
    if not business:
        raise HTTPException(status_code=404, detail="Shop not found")
    code = lang_code(payload.lang)
    if not business.is_live:
        raise HTTPException(status_code=503, detail="This shop is temporarily unavailable")
    if not business.accepting_orders:
        raise HTTPException(status_code=400, detail=business.closed_message or t(code, "closed"))
    phone = phone_from_token(business.id, payload.c) or normalize_phone(payload.phone)
    if not 11 <= len(phone) <= 15:
        raise HTTPException(status_code=400, detail="Please enter your WhatsApp number with country code")
    name, address = payload.name.strip(), payload.address.strip()
    if not name or len(address) < 8:
        raise HTTPException(status_code=400, detail="Please enter your name and full delivery address")
    if not payload.items:
        raise HTTPException(status_code=400, detail=t(code, "empty_cart"))

    customer = db.query(models.Customer).filter(
        models.Customer.business_id == business.id, models.Customer.phone == phone).first()
    if not customer:
        customer = models.Customer(business_id=business.id, name=name, phone=phone)
        db.add(customer)
    customer.name, customer.address = name, address
    customer.pincode = zones.clean_pincode(payload.pincode) or zones.pincode_in(address) or customer.pincode
    conversation = db.query(models.Conversation).filter(
        models.Conversation.business_id == business.id, models.Conversation.phone == phone).first()
    if not conversation:
        conversation = models.Conversation(business_id=business.id, phone=phone)
        db.add(conversation)
    conversation.customer_name, conversation.delivery_address = name, address
    if not conversation.preferred_language:
        conversation.preferred_language = code
        conversation.language = language_name(code)
    db.flush()

    method = "cod" if payload.payment_method == "cod" else "online"
    items = [schemas.OrderItemCreate(product_id=i.product_id, variant_id=i.variant_id, quantity=i.quantity)
             for i in payload.items if i.quantity > 0]
    terms = zones.terms_for(db, business, customer.pincode, address)
    if terms.status == "no_delivery":
        db.commit()
        raise HTTPException(status_code=400, detail=t(code, "no_delivery_area") + (f" {terms.note}" if terms.note else ""))
    if terms.status == "unknown":
        # Not in any zone the seller set up: ask the owner instead of guessing the charge.
        await _ask_owner_about_area(db, business, conversation, name, address, customer.pincode, payload.items)
        raise HTTPException(status_code=409, detail=t(code, "area_check"))
    try:
        order = place_order(db, business, customer, items, method, payload.note, check_stock=True, terms=terms)
    except HTTPException:
        db.rollback()
        raise
    pay_method = effective_payment_method(business) if method == "online" else None
    if pay_method in ("upi", "razorpay_link"):
        order.payment_link_url = pay_url(order.id)
    db.add(models.ShopEvent(business_id=business.id, kind="order"))
    db.commit()
    db.refresh(order)

    creds = whatsapp_creds(business)
    summary = _summary(order)
    can_message = within_service_window(conversation)
    if can_message:
        text = summary + ("\n\n" + t(code, "order_placed_cod", amount=f"{order.total_amount:g}") if method == "cod" else "")
        if method == "cod" and expected_line(order):
            text += "\n" + expected_line(order)
        try:
            await send_message(creds, phone, text)
            db.add(models.Message(conversation_id=conversation.id, sender="system", text=text))
            db.commit()
        except WhatsAppSendError as e:
            print("ORDER SUMMARY NOT DELIVERED:", e)
        if method == "online" and pay_method:
            await send_payment_request(business, order, phone)
    await notify_seller(db, business, f"🛒 New order #{order.id} from {name} - ₹{order.total_amount:g}"
                                      f" ({'cash on delivery' if method == 'cod' else 'online, awaiting payment'})")

    response = {"order_id": order.id, "total": order.total_amount, "redirect": None, "message": ""}
    if method == "cod":
        response["message"] = t(code, "order_placed_cod", amount=f"{order.total_amount:g}")
    elif pay_method in ("upi", "razorpay_link"):
        response["redirect"] = pay_url(order.id)
    elif pay_method == "whatsapp_pay":
        if not can_message:
            await send_payment_request(business, order, phone)
        response["message"] = "Open WhatsApp and tap 'Review and pay' to complete your payment."
    else:
        response["message"] = "The seller will send you the payment details on WhatsApp shortly."
        await notify_seller(db, business, f"⚠️ Order #{order.id} has no online payment set up - "
                                          "send the customer payment details, or connect Razorpay in Settings.")
    return response


SHOP_HTML = r"""<!doctype html>
<html lang="__LANG__" dir="__DIR__"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="__THEME__">
<title>__TITLE__</title>
<meta property="og:title" content="__TITLE__">
<meta property="og:description" content="__DESC__">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600;700&family=Noto+Sans:wght@400;600;700&display=swap" rel="stylesheet">
<style>
:root{--hdr:linear-gradient(135deg,#7c3aed 0%,#db2777 55%,#f97316 100%);--soft:#f5f0ff;--violet:#7c3aed;--pink:#ec4899;--orange:#f97316;--yellow:#facc15;--green:#16a34a;--green-d:#15803d;
 --ink:#1e1b2e;--muted:#5b5670;--line:#ece8f5;--bg:#faf7ff;--card:#fff;--radius:20px}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html{font-size:17px}
body{margin:0;font-family:"Noto Sans",system-ui,sans-serif;color:var(--ink);background:var(--bg);padding-bottom:110px}
button,input,textarea,select{font:inherit;color:inherit}
:focus-visible{outline:3px solid var(--orange);outline-offset:2px;border-radius:10px}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
header{background:var(--hdr);background-size:cover;background-position:center;color:#fff;padding:18px 16px 54px;position:relative;overflow:hidden}
header:after{content:"";position:absolute;right:-40px;top:-40px;width:160px;height:160px;border-radius:50%;background:rgba(255,255,255,.12)}
.top{display:flex;justify-content:space-between;align-items:center;gap:10px;max-width:960px;margin:0 auto;position:relative;z-index:1}
.brand{display:flex;align-items:center;gap:12px;min-width:0}
.logo{width:48px;height:48px;border-radius:16px;background:#fff;color:var(--violet);display:grid;place-items:center;font:800 1.5rem "Baloo 2",sans-serif;flex-shrink:0;box-shadow:0 6px 16px rgba(0,0,0,.15)}
h1{font:800 1.45rem/1.1 "Baloo 2",sans-serif;margin:0;overflow-wrap:anywhere}
.addr{margin:2px 0 0;font-size:.82rem;opacity:.9}
.langsel{background:rgba(255,255,255,.2);border:1.5px solid rgba(255,255,255,.5);color:#fff;border-radius:999px;padding:8px 10px;min-height:44px;font-size:.85rem;max-width:118px;flex-shrink:0}
.langsel option{color:#111}
.badges{display:flex;flex-wrap:wrap;gap:8px;max-width:960px;margin:14px auto 0;position:relative;z-index:1}
.badges span{background:rgba(255,255,255,.22);border-radius:999px;padding:5px 12px;font-size:.8rem;font-weight:600}
.searchbox{max-width:960px;margin:-30px auto 0;padding:0 16px;position:relative;z-index:2}
.searchbox input{width:100%;border:0;border-radius:18px;padding:16px 18px 16px 50px;font-size:1rem;background:#fff url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='22' height='22' fill='none' stroke='%237c3aed' stroke-width='2.5' stroke-linecap='round'%3E%3Ccircle cx='10' cy='10' r='7'/%3E%3Cpath d='m20 20-4.5-4.5'/%3E%3C/svg%3E") no-repeat 16px center;box-shadow:0 10px 30px rgba(76,29,149,.18)}
[dir=rtl] .searchbox input{padding:16px 50px 16px 18px;background-position:calc(100% - 16px) center}
.chips{display:flex;gap:10px;overflow-x:auto;padding:16px;max-width:960px;margin:0 auto;scrollbar-width:none}
.chips::-webkit-scrollbar{display:none}
.chip{flex-shrink:0;border:0;border-radius:999px;padding:10px 18px;min-height:44px;font-weight:700;font-size:.92rem;cursor:pointer;background:#fff;color:var(--ink);box-shadow:0 2px 8px rgba(30,27,46,.08)}
.chip[aria-pressed=true]{color:#fff;background:var(--c,var(--violet));box-shadow:0 6px 16px rgba(124,58,237,.3)}
main{max-width:960px;margin:0 auto;padding:0 16px}
.notice{background:#fff1f2;border:2px solid #fecdd3;color:#9f1239;border-radius:16px;padding:14px 16px;margin:8px 0 14px;font-weight:600}
h2{font:800 1.25rem "Baloo 2",sans-serif;margin:18px 4px 10px;display:flex;align-items:center;gap:8px}
h2:before{content:"";width:10px;height:22px;border-radius:6px;background:var(--c,var(--violet))}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}
@media(min-width:640px){.grid{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(min-width:900px){.grid{grid-template-columns:repeat(4,minmax(0,1fr))}}
.card{background:var(--card);border-radius:var(--radius);overflow:hidden;display:flex;flex-direction:column;box-shadow:0 4px 14px rgba(30,27,46,.07);border:0;padding:0;text-align:start;cursor:pointer;position:relative}
.card .ph{aspect-ratio:1;background:var(--c,#ede9fe);display:grid;place-items:center;font:800 2.6rem "Baloo 2",sans-serif;color:#fff;position:relative}
.card .ph img{width:100%;height:100%;object-fit:cover;display:block}
.card .body{padding:10px 12px 12px;display:flex;flex-direction:column;gap:4px;flex:1}
.card .name{font-weight:700;font-size:.98rem;line-height:1.25}
.card .from{font-size:.78rem;color:var(--muted)}
.card .row{flex-wrap:wrap;display:flex;justify-content:space-between;align-items:center;gap:6px;margin-top:auto;padding-top:6px}
.price{font:800 1.15rem "Baloo 2",sans-serif;color:var(--violet)}
.unit{font:500 .78rem "Noto Sans",sans-serif;color:var(--muted)}
.add{white-space:nowrap;border:0;border-radius:14px;background:var(--green);color:#fff;font-weight:800;min-width:48px;min-height:44px;padding:0 14px;cursor:pointer;font-size:.95rem;box-shadow:0 4px 10px rgba(22,163,74,.3)}
.add:active{transform:scale(.96)}
.qty{display:flex;align-items:center;background:#f0fdf4;border-radius:14px;border:2px solid var(--green)}
.qty button{border:0;background:none;width:40px;height:40px;font-size:1.3rem;font-weight:800;color:var(--green-d);cursor:pointer}
.qty span{min-width:22px;text-align:center;font-weight:800}
.out{position:absolute;top:10px;inset-inline-start:10px;background:#111827cc;color:#fff;font-size:.75rem;font-weight:700;padding:4px 10px;border-radius:999px}
.incart{position:absolute;top:10px;inset-inline-end:10px;background:var(--green);color:#fff;font-size:.8rem;font-weight:800;min-width:28px;height:28px;border-radius:999px;display:grid;place-items:center;padding:0 8px}
.empty{text-align:center;color:var(--muted);padding:40px 10px}
.bar{position:fixed;left:0;right:0;bottom:0;padding:12px 16px calc(12px + env(safe-area-inset-bottom));background:linear-gradient(180deg,rgba(250,247,255,0),var(--bg) 35%);z-index:20;pointer-events:none}
.bar button{pointer-events:auto;width:100%;max-width:600px;margin:0 auto;display:flex;justify-content:space-between;align-items:center;border:0;border-radius:20px;padding:16px 20px;min-height:60px;background:linear-gradient(135deg,#16a34a,#059669);color:#fff;font-weight:800;font-size:1.05rem;cursor:pointer;box-shadow:0 10px 24px rgba(5,150,105,.35)}
.bar .count{background:#fff;color:var(--green-d);border-radius:999px;padding:2px 10px;margin-inline-end:8px}
.sheet{position:fixed;inset:0;z-index:30;display:none}
.sheet.open{display:block}
.sheet .shade{position:absolute;inset:0;background:rgba(17,12,34,.55)}
.sheet .panel{position:absolute;left:0;right:0;bottom:0;max-height:92vh;overflow-y:auto;background:#fff;border-radius:26px 26px 0 0;padding:0 0 calc(20px + env(safe-area-inset-bottom));max-width:640px;margin:0 auto;animation:up .22s ease-out}
@keyframes up{from{transform:translateY(40px);opacity:.4}to{transform:none;opacity:1}}
@media (prefers-reduced-motion:reduce){.sheet .panel{animation:none}}
.shead{position:sticky;top:0;z-index:3;display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px 12px 10px 20px;background:#fff;border-bottom:1px solid var(--line)}
.shead b{font:800 1.1rem "Baloo 2",sans-serif;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.close{width:44px;height:44px;border-radius:50%;border:0;background:#f3f0fa;font-size:1.2rem;cursor:pointer;flex-shrink:0}
.gallery{display:flex;overflow-x:auto;scroll-snap-type:x mandatory;scrollbar-width:none}
.gallery::-webkit-scrollbar{display:none}
.gallery img,.gallery .ph{flex:0 0 100%;width:100%;height:min(100vw,52vh,640px);object-fit:cover;scroll-snap-align:center}
.gallery .ph{display:grid;place-items:center;font:800 4rem "Baloo 2",sans-serif;color:#fff;background:var(--c,var(--violet))}
.dots{display:flex;justify-content:center;gap:6px;margin-top:-22px;position:relative}
.dots i{width:8px;height:8px;border-radius:50%;background:#fff;opacity:.6;box-shadow:0 0 3px rgba(0,0,0,.4)}
.dots i.on{opacity:1;width:20px;border-radius:6px}
.pad{padding:16px 20px}
.pad h3{font:800 1.5rem/1.15 "Baloo 2",sans-serif;margin:4px 0 6px}
.desc{color:var(--muted);line-height:1.55;margin:8px 0 0;white-space:pre-line}
.label{font-weight:700;margin:18px 0 8px;font-size:.95rem}
.sizes{display:flex;flex-wrap:wrap;gap:10px}
.size{border:2px solid var(--line);background:#fff;border-radius:14px;padding:10px 16px;min-height:48px;cursor:pointer;font-weight:700;display:flex;flex-direction:column;align-items:flex-start;gap:2px}
.size small{font-weight:600;color:var(--violet)}
.size[aria-pressed=true]{border-color:var(--violet);background:#f5f0ff;box-shadow:0 0 0 2px var(--violet) inset}
.size:disabled{opacity:.45;text-decoration:line-through;cursor:not-allowed}
table.nut{width:100%;border-collapse:collapse;font-size:.92rem;border-radius:14px;overflow:hidden;background:#fffbeb}
table.nut td{padding:9px 12px;border-bottom:1px solid #fde68a}
table.nut td:last-child{text-align:end;font-weight:700}
.buy{display:flex;gap:12px;align-items:center;margin-top:20px}
.buy .qty{flex-shrink:0}
.buy .qty button{width:48px;height:52px}
.big{flex:1;border:0;border-radius:18px;min-height:56px;padding:0 18px;font-weight:800;font-size:1.05rem;cursor:pointer;color:#fff;background:linear-gradient(135deg,#7c3aed,#db2777)}
.big.green{background:linear-gradient(135deg,#16a34a,#059669)}
.big:disabled{opacity:.5;cursor:not-allowed}
.line{display:flex;gap:12px;align-items:center;padding:12px 0;border-bottom:1px solid var(--line)}
.line img,.line .ph{width:56px;height:56px;border-radius:14px;object-fit:cover;flex-shrink:0;display:grid;place-items:center;color:#fff;font-weight:800;background:var(--c,var(--violet))}
.line .info{flex:1;min-width:0}
.line .info b{display:block;font-size:.95rem}
.line .info span{color:var(--muted);font-size:.85rem}
.totals{margin:14px 0;display:grid;gap:6px}
.totals div{display:flex;justify-content:space-between}
.totals .grand{font:800 1.3rem "Baloo 2",sans-serif;color:var(--violet);border-top:2px dashed var(--line);padding-top:8px}
.hint{background:#fff7ed;color:#9a3412;border-radius:12px;padding:10px 12px;font-size:.88rem;font-weight:600;margin:6px 0}
form label{display:block;font-weight:700;font-size:.92rem;margin:14px 0 6px}
form input,form textarea{width:100%;border:2px solid var(--line);border-radius:14px;padding:13px 14px;font-size:1rem;background:#fff}
form input:focus,form textarea:focus{border-color:var(--violet);outline:none;box-shadow:0 0 0 4px #ede9fe}
.pay{display:grid;gap:10px;margin-top:8px}
.pay label{display:flex;align-items:center;gap:12px;border:2px solid var(--line);border-radius:14px;padding:14px;margin:0;cursor:pointer;font-weight:700}
.pay input{width:22px;height:22px;accent-color:var(--violet)}
.pay label:has(input:checked){border-color:var(--violet);background:#f5f0ff}
.err{color:#b91c1c;font-weight:700;margin:10px 0 0;min-height:1.2em}
.done{text-align:center;padding:30px 20px}
.done .big-emoji{font-size:4rem}
.done a{display:inline-flex;align-items:center;justify-content:center;margin-top:18px;text-decoration:none}
footer{text-align:center;color:var(--muted);font-size:.78rem;padding:28px 16px}
.logo{position:relative;overflow:hidden}
.logo img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.tagline{margin:4px 0 0;font-weight:600;font-size:.9rem;opacity:.95}
.about{background:#fff;border-radius:var(--radius);padding:18px 20px;margin:26px 0 0;box-shadow:0 4px 14px rgba(30,27,46,.07)}
.about h2{margin-top:0}
.about p{margin:0;color:var(--muted);line-height:1.6;white-space:pre-line}
.size[aria-pressed=true],.pay label:has(input:checked){background:var(--soft)}
.big{background:var(--hdr)}
</style>
<style>
/* Studio family (same palette and font as the seller dashboard) + Zepto-style product grid. */
:root{--violet:#6e2c7a;--hdr:#fcf6f0;--soft:#f4ecf5;--ink:#2a1b3d;--muted:#574f63;--line:#eee7e0;--bg:#f7f2ec;--card:#fff;
 --teal:#9cd0d4;--teal-d:#5fa9af;--orange:#ff7a47;--green:#6e2c7a;--green-d:#561f61;--radius:16px}
body{font-family:"Poppins","Noto Sans",system-ui,sans-serif;background:var(--bg);color:var(--ink)}
h1,h2,.price,.pad h3,.shead b,.totals .grand,.card .ph,.gallery .ph,.logo{font-family:"Poppins","Noto Sans",sans-serif}
header{background:var(--hdr);color:var(--ink);padding:18px 16px 46px;border-radius:0 0 28px 28px}
header:after{right:-60px;top:-70px;width:220px;height:220px;background:radial-gradient(circle,#9cd0d4 0%,rgba(156,208,212,0) 70%);opacity:.8}
header:before{content:"";position:absolute;left:-50px;bottom:-60px;width:180px;height:180px;border-radius:50%;background:radial-gradient(circle,#f3e1c8 0%,rgba(243,225,200,0) 70%)}
header.banner{color:#fff}
header.banner .langsel{background:rgba(255,255,255,.2);color:#fff;border-color:rgba(255,255,255,.5)}
header.banner .badges span{background:rgba(255,255,255,.22);color:#fff;border-color:transparent}
.logo{width:54px;height:54px;border-radius:50%;background:var(--teal);color:#fff;border:3px solid #fff;font-weight:700;font-size:1.4rem;box-shadow:0 6px 16px rgba(95,169,175,.35)}
h1{font-weight:700;font-size:1.3rem;letter-spacing:-.01em}
.tagline{font-weight:500;font-size:.85rem;opacity:.8}
.addr{opacity:.7}
.langsel{background:#fff;color:var(--ink);border:1px solid var(--line)}
.badges span{background:#fff;color:var(--ink);border:1px solid var(--line);font-weight:500;font-size:.76rem}
.searchbox{margin-top:-26px}
.searchbox input{border-radius:14px;border:1px solid var(--line);box-shadow:0 10px 26px rgba(70,45,20,.08);padding-top:14px;padding-bottom:14px;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='22' height='22' fill='none' stroke='%236e2c7a' stroke-width='2.5' stroke-linecap='round'%3E%3Ccircle cx='10' cy='10' r='7'/%3E%3Cpath d='m20 20-4.5-4.5'/%3E%3C/svg%3E")}
.chips{gap:8px;padding:14px 16px}
.chip{border:1px solid var(--line);box-shadow:none;font-weight:600;font-size:.85rem;min-height:40px;padding:8px 16px;background:#fff}
.chip[aria-pressed=true]{background:var(--ink);color:#fff;border-color:var(--ink);box-shadow:none}
main{max-width:1100px}
h2{font-weight:700;font-size:1.05rem;margin:20px 2px 12px}
h2:before{width:6px;height:18px;border-radius:4px}
.grid{gap:12px}
@media(min-width:640px){.grid{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(min-width:900px){.grid{grid-template-columns:repeat(4,minmax(0,1fr))}}
@media(min-width:1080px){.grid{grid-template-columns:repeat(5,minmax(0,1fr))}}
/* Product card: photo on a soft tile, ADD sitting on the photo's bottom corner, price first. */
.card{border:1px solid var(--line);box-shadow:none;border-radius:16px;overflow:visible;padding:8px}
.card .ph{border-radius:12px;background:var(--soft);overflow:visible;color:var(--violet);font-weight:700;font-size:2.2rem}
.card .ph img{border-radius:12px}
.ph-act{position:absolute;right:6px;bottom:-16px;z-index:2;font-size:.95rem;font-weight:600;color:var(--ink)}
.ph-act .qty span{font-size:.95rem;min-width:18px}
.top,.badges,.searchbox,.chips{max-width:1100px}
[dir=rtl] .ph-act{right:auto;left:6px}
.card .body{padding:22px 4px 4px;gap:2px}
.card .row{order:-1;margin-top:0;padding-top:0}
.price{font-weight:700;font-size:1.02rem;color:var(--ink)}
.card .name{font-weight:500;font-size:.88rem;line-height:1.3;color:var(--ink)}
.card .from,.unit{font-size:.74rem;color:var(--muted)}
.add{background:#fff;color:var(--violet);border:1.5px solid var(--violet);border-radius:10px;min-height:36px;min-width:64px;padding:0 16px;font-weight:700;font-size:.85rem;box-shadow:0 4px 10px rgba(110,44,122,.18);text-transform:uppercase;letter-spacing:.02em}
.qty{background:var(--violet);border:0;border-radius:10px;color:#fff;box-shadow:0 4px 10px rgba(110,44,122,.25)}
.qty button{color:#fff;width:34px;height:36px;font-size:1.1rem}
.qty span{color:#fff;font-weight:700}
.buy .qty{border-radius:14px}
.buy .qty button{width:46px;height:52px}
.out{background:rgba(42,27,61,.82);font-weight:600}
.incart{background:var(--violet)}
.bar{background:linear-gradient(180deg,rgba(247,242,236,0),var(--bg) 35%)}
.bar button{background:var(--violet);border-radius:16px;box-shadow:0 12px 26px rgba(110,44,122,.35);font-weight:600}
.bar .count{color:var(--violet)}
.big{background:var(--ink);border-radius:14px;font-weight:600}
.big.green{background:var(--violet)}
.size{border:1.5px solid var(--line);border-radius:12px;font-weight:600}
.size small{color:var(--violet)}
.size[aria-pressed=true]{border-color:var(--violet);background:var(--soft);box-shadow:0 0 0 1.5px var(--violet) inset}
.pay label{border:1.5px solid var(--line);border-radius:12px;font-weight:600}
.pay input{accent-color:var(--violet)}
.pay label:has(input:checked){border-color:var(--violet);background:var(--soft)}
form input,form textarea{border:1.5px solid var(--line);border-radius:12px}
form input:focus,form textarea:focus{border-color:var(--violet);box-shadow:0 0 0 4px var(--soft)}
.totals .grand{color:var(--ink);font-weight:700}
.close{background:var(--soft)}
table.nut{background:var(--hdr)}
table.nut td{border-bottom-color:var(--line)}
.about{border:1px solid var(--line);box-shadow:none}
.notice{border-radius:14px}
.hint{background:var(--hdr);color:var(--ink);border:1px solid #f3e1c8}
.hint.ok{background:#eaf6f7;border-color:#9cd0d4}
.size[data-out]{opacity:.45;text-decoration:line-through}
:focus-visible{outline-color:var(--teal-d)}
</style></head>
<body>
<header>
 <div class="top">
  <div class="brand"><div class="logo" aria-hidden="true" id="logo"></div><div><h1 id="shopName"></h1><p class="tagline" id="shopTagline"></p><p class="addr" id="shopAddr"></p></div></div>
  <label class="sr" for="lang">Language</label><select id="lang" class="langsel"></select>
 </div>
 <div class="badges" id="badges"></div>
</header>
<div class="searchbox"><label class="sr" for="q"></label><input id="q" type="search" autocomplete="off"></div>
<nav class="chips" id="chips" aria-label="Categories"></nav>
<main id="main"></main>
<main><section class="about" id="about" hidden><h2></h2><p></p></section></main>
<footer>🔒 Secure checkout · powered by RIRAONE</footer>
<div class="bar" id="bar" hidden><button id="cartBtn" type="button"><span><span class="count" id="cartCount">0</span><span id="cartLabel"></span></span><span id="cartTotal"></span></button></div>
<div class="sheet" id="sheet" role="dialog" aria-modal="true"><div class="shade" data-close></div><div class="panel" id="panel"></div></div>
<script id="data" type="application/json">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById("data").textContent);
const T = D.texts, R = D.rules;
// Studio palette: purple, teal, orange and their softer shades (same family as the dashboard).
const COLORS = ["#6e2c7a","#5fa9af","#ff7a47","#8d5a99","#e0663a","#7fbec4"];
const KEY = "rira-cart-" + D.shop.id;
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const money = (n) => "₹" + (Math.round(n * 100) / 100).toLocaleString("en-IN");
const byId = Object.fromEntries(D.products.map((p) => [p.id, p]));
const cats = [...new Set(D.products.map((p) => p.category).filter(Boolean))];
const catColor = (c) => COLORS[(cats.indexOf(c) + COLORS.length) % COLORS.length];
const prodColor = (p) => p.category ? catColor(p.category) : COLORS[p.id % COLORS.length];
let cart = [];
try { cart = JSON.parse(localStorage.getItem(KEY) || "[]"); } catch (e) {}
if (D.prefill.length) cart = D.prefill.filter((i) => byId[i.product_id]);
cart = cart.filter((i) => byId[i.product_id]);
let activeCat = "", query = "";

const save = () => { try { localStorage.setItem(KEY, JSON.stringify(cart)); } catch (e) {} };
const variantOf = (p, vid) => p.variants.find((v) => v.id === vid);
const unitPrice = (i) => { const p = byId[i.product_id]; const v = variantOf(p, i.variant_id); return v ? v.price : p.price; };
const stockOf = (p, vid) => { const v = variantOf(p, vid); return v ? v.stock : p.stock; };
const qtyIn = (pid, vid) => cart.filter((i) => i.product_id === pid && (vid === undefined || i.variant_id === vid)).reduce((a, i) => a + i.quantity, 0);
const subtotal = () => cart.reduce((a, i) => a + unitPrice(i) * i.quantity, 0);
const deliveryFee = (s) => (!R.delivery_fee || (R.free_above && s >= R.free_above)) ? 0 : R.delivery_fee;

const sent = {};
function track(kind) {
  if (sent[kind]) return;
  sent[kind] = true;
  try { navigator.sendBeacon(`/shop/${D.shop.id}/event`, kind); } catch (e) {}
}

// Count this visit: a random id kept by this browser makes one person one visitor. Links from the
// seller dashboard carry ?staff=1, which marks this browser as the shop's own from then on.
(function countVisit() {
  let visitor = null, staff = D.visit.staff;
  try {
    const url = new URL(location.href);
    if (url.searchParams.get("staff") === "1") {
      localStorage.setItem("rira-staff", "1");
      url.searchParams.delete("staff");
      history.replaceState(null, "", url);
    }
    staff = staff || localStorage.getItem("rira-staff") === "1";
    visitor = localStorage.getItem("rira-visitor");
    if (!visitor) {
      visitor = (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2) + Date.now());
      localStorage.setItem("rira-visitor", visitor);
    }
  } catch (e) {}
  fetch(`/shop/${D.shop.id}/visit`, {
    method: "POST", headers: { "Content-Type": "application/json" }, keepalive: true,
    body: JSON.stringify({ visitor, source: D.visit.source, staff }),
  }).catch(() => {});
})();

function setQty(pid, vid, q) {
  if (q > qtyIn(pid, vid)) track("add_to_cart");
  const p = byId[pid]; q = Math.max(0, Math.min(q, stockOf(p, vid), 99));
  const line = cart.find((i) => i.product_id === pid && i.variant_id === vid);
  if (line) line.quantity = q; else if (q > 0) cart.push({ product_id: pid, variant_id: vid, quantity: q });
  cart = cart.filter((i) => i.quantity > 0); save(); renderGrid(); renderBar();
}

function header() {
  document.title = D.shop.name;
  $("#shopName").textContent = D.shop.name; $("#shopAddr").textContent = D.shop.address;
  $("#logo").textContent = (D.shop.name.trim()[0] || "S").toUpperCase();
  if (D.shop.logo) $("#logo").innerHTML = `<img src="${esc(D.shop.logo)}" alt="">`;
  $("#shopTagline").textContent = D.shop.tagline;
  if (D.shop.banner) document.querySelector("header").classList.add("banner");
  if (D.shop.banner) document.querySelector("header").style.backgroundImage =
    `linear-gradient(180deg,rgba(17,12,34,.35),rgba(17,12,34,.7)),url("${D.shop.banner.replace(/"/g, "")}")`;
  if (D.shop.about) {
    const a = $("#about"); a.hidden = false;
    a.querySelector("h2").textContent = D.shop.name; a.querySelector("p").textContent = D.shop.about;
  }
  $("#lang").innerHTML = D.languages.map(([c, n]) => `<option value="${c}" ${c === D.lang ? "selected" : ""}>${esc(n)}</option>`).join("");
  $("#lang").onchange = (e) => { const u = new URL(location.href); u.searchParams.set("lang", e.target.value); u.searchParams.delete("add"); location.href = u; };
  const b = [];
  if (R.free_above && R.delivery_fee) b.push("🚚 " + T.free_above.replace("{amount}", R.free_above));
  else if (!R.delivery_fee) b.push("🚚 " + T.delivery + ": " + T.free);
  if (R.min_order) b.push("🧺 " + T.min_order.replace("{amount}", R.min_order));
  if (R.cod) b.push("💵 " + T.cod);
  $("#badges").innerHTML = b.map((x) => `<span>${esc(x)}</span>`).join("");
  $("#q").placeholder = T.search; $("label[for=q]").textContent = T.search;
  $("#q").oninput = (e) => { query = e.target.value.trim().toLowerCase(); renderGrid(); };
  $("#cartLabel").textContent = T.view_cart;
  $("#cartBtn").onclick = openCart;
}

function renderChips() {
  if (!cats.length) { $("#chips").hidden = true; return; }
  const all = [["", T.all, COLORS[0]], ...cats.map((c) => [c, c, catColor(c)])];
  $("#chips").innerHTML = all.map(([v, l, c]) => `<button class="chip" style="--c:${c}" aria-pressed="${v === activeCat}" data-cat="${esc(v)}">${esc(l)}</button>`).join("");
  $("#chips").querySelectorAll("[data-cat]").forEach((b) => b.onclick = () => { activeCat = b.dataset.cat; renderChips(); renderGrid(); });
}

function card(p) {
  const n = qtyIn(p.id), avail = p.stock > 0;
  const multi = p.variants.length > 1;
  const single = p.variants.length === 1 ? p.variants[0].id : null;
  let control;
  if (!avail) control = "";
  else if (!multi && n > 0) control = `<div class="qty" role="group" aria-label="${esc(p.name)}"><button data-dec="${p.id}" aria-label="−">−</button><span aria-live="polite">${n}</span><button data-inc="${p.id}" aria-label="+">+</button></div>`;
  else control = `<button class="add" data-add="${p.id}" aria-label="${esc(T.add + " " + p.name)}">${multi ? T.add + " ▾" : esc(T.add)}</button>`;
  return `<article class="card" data-open="${p.id}" tabindex="0" aria-label="${esc(p.name)}">
   <div class="ph" style="--c:${prodColor(p)}">${p.images.length ? `<img src="${esc(p.images[0])}" alt="" loading="lazy">` : esc(p.name.trim()[0] || "?")}
   ${avail ? "" : `<span class="out">${esc(T.sold_out)}</span>`}${n && multi ? `<span class="incart">${n}</span>` : ""}
   ${control ? `<div class="ph-act">${control}</div>` : ""}</div>
   <div class="body"><div class="row"><div><span class="price">${money(p.variants.length ? Math.min(...p.variants.map((v) => v.price)) : p.price)}</span>${!p.variants.length && p.unit ? ` <span class="unit">/ ${esc(p.unit)}</span>` : ""}</div></div>
   <div class="name">${esc(p.name)}</div>
   ${multi ? `<div class="from">${p.variants.map((v) => esc(v.label)).join(" · ")}</div>` : ""}</div></article>`;
}

function renderGrid() {
  const main = $("#main");
  if (!D.products.length) { main.innerHTML = `<p class="empty">${esc(T.no_products)}</p>`; return; }
  const list = D.products.filter((p) => (!activeCat || p.category === activeCat) &&
    (!query || (p.name + " " + (p.alt || "") + " " + p.description + " " + p.category).toLowerCase().includes(query)));
  const groups = {};
  list.forEach((p) => (groups[p.category] ||= []).push(p));
  let html = R.open ? "" : `<div class="notice">⏸️ ${esc(R.closed_message)}</div>`;
  for (const [c, ps] of Object.entries(groups))
    html += (c && !activeCat ? `<h2 style="--c:${catColor(c)}">${esc(c)}</h2>` : "") + `<div class="grid">${ps.map(card).join("")}</div>`;
  main.innerHTML = list.length ? html : `<p class="empty">🔍 ${esc(query)}</p>`;
  main.querySelectorAll("[data-open]").forEach((el) => {
    el.onclick = (e) => { if (!e.target.closest("button")) openProduct(+el.dataset.open); };
    el.onkeydown = (e) => { if (e.key === "Enter" && e.target === el) openProduct(+el.dataset.open); };
  });
  main.querySelectorAll("[data-add]").forEach((b) => b.onclick = () => {
    const p = byId[+b.dataset.add];
    if (p.variants.length > 1) return openProduct(p.id);
    const vid = p.variants.length ? p.variants[0].id : null;
    setQty(p.id, vid, qtyIn(p.id, vid) + 1);
  });
  main.querySelectorAll("[data-inc],[data-dec]").forEach((b) => b.onclick = () => {
    const p = byId[+(b.dataset.inc || b.dataset.dec)], vid = p.variants.length ? p.variants[0].id : null;
    setQty(p.id, vid, qtyIn(p.id, vid) + (b.dataset.inc ? 1 : -1));
  });
}

function renderBar() {
  const n = cart.reduce((a, i) => a + i.quantity, 0);
  $("#bar").hidden = n === 0 || !R.open;
  $("#cartCount").textContent = n; $("#cartTotal").textContent = money(subtotal()) + " →";
}

let lastFocus = null;
function openSheet(html, title = "") {
  lastFocus = document.activeElement;
  $("#panel").innerHTML = `<div class="shead"><b>${esc(title)}</b><button class="close" data-close aria-label="${esc(T.close)}">✕</button></div>` + html;
  $("#sheet").classList.add("open"); document.body.style.overflow = "hidden";
  $("#panel").querySelectorAll("[data-close]").forEach((b) => b.onclick = closeSheet);
  setTimeout(() => $("#panel .close").focus(), 30);
}
function closeSheet() { $("#sheet").classList.remove("open"); document.body.style.overflow = ""; lastFocus && lastFocus.focus(); }
$("#sheet .shade").onclick = closeSheet;
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && $("#sheet").classList.contains("open")) closeSheet(); });

function openProduct(pid) {
  const p = byId[pid];
  let vid = p.variants.length ? (p.variants.find((v) => v.stock > 0) || p.variants[0]).id : null;
  let q = 1;
  const two = p.options.length > 1;
  // With options (Size, Colour) the customer picks one value per option; the matching combination is the variant.
  const values = (k) => [...new Set(p.variants.map((v) => v[k]).filter(Boolean))];
  const pickValue = (k, value) => {
    const cur = variantOf(p, vid) || {}, other = k === "o1" ? "o2" : "o1";
    const same = (v) => v[k] === value;
    const match = p.variants.find((v) => same(v) && (!two || v[other] === cur[other]) && v.stock > 0)
      || p.variants.find((v) => same(v) && v.stock > 0) || p.variants.find(same);
    if (match) vid = match.id;
  };
  const inStock = (k, value) => {
    const cur = variantOf(p, vid) || {};
    return p.variants.some((v) => v[k] === value && v.stock > 0 && (k === "o1" || !two || v.o1 === cur.o1));
  };
  const draw = () => {
    const v = vid ? variantOf(p, vid) : null, price = v ? v.price : p.price, st = stockOf(p, vid);
    // The chosen combination's own photo comes first.
    const photos = v && v.image ? [v.image, ...p.images.filter((u) => u !== v.image)] : p.images;
    const imgs = photos.length ? photos.map((u, i) => `<img src="${esc(u)}" alt="${esc(p.name)} ${i + 1}">`).join("")
      : `<div class="ph" style="--c:${prodColor(p)}">${esc(p.name.trim()[0] || "?")}</div>`;
    const chooser = !p.variants.length ? "" : p.options.length
      ? ["o1", "o2"].slice(0, p.options.length).map((k, i) => `<div class="label">${esc(p.options[i])}${v && v[k] ? ": <b>" + esc(v[k]) + "</b>" : ""}</div>
         <div class="sizes" role="group">${values(k).map((val) => `<button class="size" data-o="${k}" data-val="${esc(val)}" aria-pressed="${!!v && v[k] === val}" ${inStock(k, val) ? "" : "data-out=1"}>${esc(val)}</button>`).join("")}</div>`).join("")
      : `<div class="label">${esc(T.choose_size)}</div><div class="sizes" role="group">${p.variants.map((x) =>
        `<button class="size" data-v="${x.id}" aria-pressed="${x.id === vid}" ${x.stock > 0 ? "" : "disabled"}>${esc(x.label)}<small>${money(x.price)}</small></button>`).join("")}</div>`;
    const food = !D.shop.type || D.shop.type === "food";
    openSheet(`<div class="gallery" id="gal">${imgs}</div>
     ${photos.length > 1 ? `<div class="dots" id="dots">${photos.map((_, i) => `<i class="${i ? "" : "on"}"></i>`).join("")}</div>` : ""}
     <div class="pad"><h3>${esc(p.name)}</h3>
      <div><span class="price" style="font-size:1.5rem">${money(price)}</span>${!p.variants.length && p.unit ? ` <span class="unit">/ ${esc(p.unit)}</span>` : ""}</div>
      ${p.description ? `<p class="desc">${esc(p.description)}</p>` : ""}
      ${chooser}
      ${p.nutrition.length ? `<div class="label">${food ? "🥗 " + esc(T.nutrition) : "📋 " + esc(T.details)}</div><table class="nut">${p.nutrition.map(([a, b]) => `<tr><td>${esc(a)}</td><td>${esc(b)}</td></tr>`).join("")}</table>` : ""}
      ${R.open ? `<div class="buy">${st > 0 ? `<div class="qty"><button data-q="-1" aria-label="−">−</button><span aria-live="polite">${q}</span><button data-q="1" aria-label="+">+</button></div>` : ""}
      <button class="big green" id="addBtn" ${st > 0 ? "" : "disabled"}>${st > 0 ? "🛒 " + esc(T.add) + " · " + money(price * q) : esc(T.sold_out)}</button></div>` : ""}
     </div>`, p.name);
    $("#panel").querySelectorAll("[data-v]").forEach((b) => b.onclick = () => { vid = +b.dataset.v; q = 1; draw(); });
    $("#panel").querySelectorAll("[data-o]").forEach((b) => b.onclick = () => { pickValue(b.dataset.o, b.dataset.val); q = 1; draw(); });
    $("#panel").querySelectorAll("[data-q]").forEach((b) => b.onclick = () => { q = Math.max(1, Math.min(q + +b.dataset.q, st, 99)); draw(); });
    const add = $("#addBtn");
    if (add) add.onclick = () => { setQty(p.id, vid, qtyIn(p.id, vid) + q); closeSheet(); };
    const gal = $("#gal"), dots = $("#dots");
    if (gal && dots) gal.onscroll = () => { const i = Math.round(gal.scrollLeft / gal.clientWidth); dots.querySelectorAll("i").forEach((d, j) => d.className = Math.abs(i) === j ? "on" : ""); };
  };
  draw();
}

// Delivery terms for the customer's address (shops with delivery zones): fee, COD, minimum, date.
let Q = null, quoteFor = "";
const draft = {};
const rulesNow = () => Q && Q.status === "ok"
  ? { rate: Q.rate, free_above: Q.free_above, min_order: Q.min_order, cod: R.cod && Q.cod, eta: Q.eta, note: Q.note }
  : { rate: R.delivery_fee, free_above: R.free_above, min_order: R.min_order, cod: R.cod && !R.zones, eta: null, note: null };
const feeFor = (s, r) => (!r.rate || (r.free_above && s >= r.free_above)) ? 0 : r.rate;

function drawTotals() {
  const box = $("#tot");
  if (!box) return;
  const r = rulesNow(), s = subtotal(), fee = feeFor(s, r), short = r.min_order && s < r.min_order;
  const blocked = R.zones && Q && Q.status !== "ok";
  box.innerHTML = `<div class="totals"><div><span>${esc(T.subtotal)}</span><b>${money(s)}</b></div>
   <div><span>${esc(T.delivery)}</span><b>${fee ? money(fee) : esc(T.free)}</b></div>
   <div class="grand"><span>${esc(T.total)}</span><span>${money(s + fee)}</span></div></div>
   ${short ? `<div class="hint">🧺 ${esc(T.min_order.replace("{amount}", r.min_order))}</div>` : ""}
   ${r.free_above && fee ? `<div class="hint">🚚 ${esc(T.free_above.replace("{amount}", r.free_above))}</div>` : ""}
   ${r.eta ? `<div class="hint ok">📦 ${esc(T.expected_delivery.replace("{when}", r.eta))}${r.note ? " · " + esc(r.note) : ""}</div>` : ""}
   ${blocked ? `<div class="hint">📍 ${esc(Q.status === "no_delivery" ? T.no_delivery_area + (Q.note ? " " + Q.note : "") : T.area_check)}</div>` : ""}`;
  const pm = $("#pmBox");
  if (pm) {
    const chosen = (pm.querySelector("input:checked") || {}).value;
    pm.innerHTML = `<label><input type="radio" name="pm" value="online" ${chosen !== "cod" || !r.cod ? "checked" : ""}> 💳 ${esc(T.pay_online)}</label>
    ${r.cod ? `<label><input type="radio" name="pm" value="cod" ${chosen === "cod" ? "checked" : ""}> 💵 ${esc(T.cod)}</label>` : ""}`;
  }
  const btn = $("#placeBtn");
  if (btn) { btn.disabled = Boolean(short) || (blocked && Q.status === "no_delivery"); btn.textContent = `✅ ${T.place_order} · ${money(s + fee)}`; }
}

async function getQuote() {
  if (!R.zones) return;
  const pin = ($("#f-pin") || {}).value || "", addr = ($("#f-addr") || {}).value || "";
  const key = pin.replace(/\D/g, "") + "|" + addr.trim().toLowerCase();
  if (key === quoteFor || (pin.replace(/\D/g, "").length < 6 && addr.trim().length < 8)) return;
  quoteFor = key;
  try {
    const res = await fetch(`/shop/${D.shop.id}/quote`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pincode: pin, address: addr, subtotal: subtotal() }) });
    if (res.ok && quoteFor === key) { Q = await res.json(); drawTotals(); }
  } catch (x) { /* offline - the checkout itself will check again */ }
}

function openCart() {
  if (!cart.length) return;
  track("checkout_open");
  const lines = cart.map((i) => { const p = byId[i.product_id], v = variantOf(p, i.variant_id);
    return `<div class="line">${p.images.length ? `<img src="${esc(p.images[0])}" alt="">` : `<div class="ph" style="--c:${prodColor(p)}">${esc(p.name[0])}</div>`}
     <div class="info"><b>${esc(p.name)}${v && p.variants.length > 1 ? " · " + esc(v.label) : ""}</b><span>${money(unitPrice(i))} × ${i.quantity} = ${money(unitPrice(i) * i.quantity)}</span></div>
     <div class="qty"><button data-l="${i.product_id}:${i.variant_id}" data-d="-1" aria-label="−">−</button><span>${i.quantity}</span><button data-l="${i.product_id}:${i.variant_id}" data-d="1" aria-label="+">+</button></div></div>`; }).join("");
  const c = D.customer, v = (k, fallback) => esc(draft[k] ?? fallback ?? "");
  openSheet(`<div class="pad">${lines}
   <div id="tot"></div>
   <form id="co" novalidate>
    <label for="f-name">${esc(T.name)}</label><input id="f-name" name="name" autocomplete="name" required value="${v("name", c.name)}">
    ${D.token ? "" : `<label for="f-phone">${esc(T.phone)}</label><input id="f-phone" name="phone" type="tel" inputmode="tel" autocomplete="tel" required placeholder="+91 98765 43210" value="${v("phone")}">`}
    <label for="f-addr">${esc(T.address)}</label><textarea id="f-addr" name="address" rows="3" autocomplete="street-address" required>${v("address", c.address)}</textarea>
    ${R.zones ? `<label for="f-pin">${esc(T.pincode)}</label><input id="f-pin" name="pincode" inputmode="numeric" autocomplete="postal-code" maxlength="10" required value="${v("pincode", c.pincode)}">` : ""}
    <label for="f-note">${esc(T.note)}</label><input id="f-note" name="note" value="${v("note")}">
    <div class="pay" role="radiogroup" id="pmBox"></div>
    <p class="err" id="err" role="alert"></p>
    <button class="big" id="placeBtn" type="submit"></button>
   </form></div>`, "🛒 " + T.cart);
  drawTotals();
  getQuote();
  const form = $("#co");
  form.oninput = (e) => { draft[e.target.name] = e.target.value; if (e.target.name === "pincode") getQuote(); };
  form.address.onblur = getQuote;
  $("#panel").querySelectorAll("[data-l]").forEach((b) => b.onclick = () => {
    const [pid, vid] = b.dataset.l.split(":"); const vv = vid === "null" ? null : +vid;
    setQty(+pid, vv, qtyIn(+pid, vv) + +b.dataset.d); cart.length ? openCart() : closeSheet();
  });
  form.onsubmit = submit;
}

async function submit(e) {
  e.preventDefault();
  const f = e.target.elements, btn = $("#placeBtn"), err = $("#err");
  err.textContent = "";
  const body = { items: cart, name: f.name.value.trim(), address: f.address.value.trim(), note: f.note.value.trim(),
    pincode: f.pincode ? f.pincode.value.trim() : null,
    phone: f.phone ? f.phone.value : null, c: D.token, payment_method: (f.pm && f.pm.value) || "online", lang: D.lang };
  if (!body.name) { err.textContent = T.name; f.name.focus(); return; }
  if (f.phone && f.phone.value.replace(/\D/g, "").length < 10) { err.textContent = T.phone; f.phone.focus(); return; }
  if (body.address.length < 8) { err.textContent = T.address; f.address.focus(); return; }
  if (f.pincode && body.pincode.replace(/\D/g, "").length < 3) { err.textContent = T.pincode_needed; f.pincode.focus(); return; }
  btn.disabled = true; btn.textContent = "⏳ …";
  try {
    const res = await fetch(`/shop/${D.shop.id}/checkout`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const out = await res.json();
    if (!res.ok) throw new Error(typeof out.detail === "string" ? out.detail : T.error);
    Object.keys(draft).forEach((k) => delete draft[k]);
    cart = []; save(); renderGrid(); renderBar();
    if (out.redirect) { location.href = out.redirect; return; }
    openSheet(`<div class="done"><div class="big-emoji">🎉</div><h3>${esc(T.order_placed)}</h3><p class="desc">#${out.order_id} · ${esc(out.message)}</p>
      ${D.whatsapp ? `<a class="big green" href="${esc(D.whatsapp)}">💬 ${esc(T.back_to_whatsapp)}</a>` : ""}</div>`);
  } catch (x) {
    err.textContent = x.message || T.error; btn.disabled = false; btn.textContent = "✅ " + T.place_order;
  }
}

header(); renderChips(); renderGrid(); renderBar();
if (D.prefill.length && cart.length && R.open) openCart();
</script>
</body></html>"""
