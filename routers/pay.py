"""How customers pay for online orders. Each seller picks one method in Settings:

- "razorpay_link": Recommended. WhatsApp gets a UPI-apps banner with a "Pay ₹X" button opening our
                   pay page. Each logo (Google Pay / PhonePe / Paytm / BHIM) starts a Razorpay UPI
                   Intent payment for a Razorpay order, so the app opens with the amount filled in and
                   locked - the customer just enters their PIN. Confirmed automatically: Razorpay's
                   signature on the page, and the order.paid webhook as a backup. "More options"
                   falls back to a Razorpay payment link (cards, netbanking, QR).
- "whatsapp_pay":  WhatsApp's native "Review and pay" order card, paid inside WhatsApp through the
                   seller's Razorpay account linked in WhatsApp Manager. Confirmed automatically from
                   Meta's payment webhook, double-checked with Meta's payment lookup.
- "upi":           Manual, opt-in only. Same pay page, but the logos open the apps with the seller's
                   own UPI ID. Nobody tells us the money arrived, so the seller taps "Mark paid".
"""
import hmac
import json
from html import escape
from urllib.parse import quote, urlencode

import qrcode
import qrcode.image.svg
import razorpay
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload

import models
from auth import sign_value
from business import business_for_phone_number_id, razorpay_creds, whatsapp_creds
from database import SessionLocal, get_db
from routers.invoices import PUBLIC_BASE_URL
from routers.orders import mark_order_paid
from whatsapp import WhatsAppSendError, lookup_payment, send_interactive, send_message

PAYMENT_METHODS = ("upi", "razorpay_link", "whatsapp_pay")
BANNER_URL = f"{PUBLIC_BASE_URL}/static/pay/upi-banner.png"

router = APIRouter(prefix="/pay", tags=["pay"])


# ---- which method applies ------------------------------------------------------------------

def method_ready(business: models.Business, method: str) -> bool:
    rp = razorpay_creds(business)
    if method == "upi":
        return bool(business.upi_id)
    if method == "razorpay_link":
        return bool(rp.key_id and rp.key_secret)
    if method == "whatsapp_pay":
        return bool(business.wa_payment_config)
    return False


def effective_payment_method(business: models.Business) -> str | None:
    """The seller's chosen method if it's set up, else Razorpay if connected, else None. Manual
    UPI is never picked automatically - only when the seller chooses it."""
    if business.payment_method and method_ready(business, business.payment_method):
        return business.payment_method
    if method_ready(business, "razorpay_link"):
        return "razorpay_link"
    return None


# ---- UPI pay page --------------------------------------------------------------------------

def pay_path(order_id: int) -> str:
    return f"/pay/{order_id}?t={sign_value(f'pay:{order_id}')}"


def pay_url(order_id: int) -> str:
    return PUBLIC_BASE_URL + pay_path(order_id)


def upi_query(business: models.Business, order: models.Order) -> str:
    return urlencode({
        "pa": business.upi_id,
        "pn": business.upi_name or business.name,
        "am": f"{order.total_amount:.2f}",
        "cu": "INR",
        "tn": f"Order {order.id}",
    }, quote_via=quote)


# (label, logo file, deep link prefix). BHIM has no public app-specific link, so it uses the
# standard upi:// link, which opens BHIM or lets the phone ask which UPI app to use.
UPI_APPS = (
    ("Google Pay", "gpay.png", "tez://upi/pay?"),
    ("PhonePe", "phonepe.png", "phonepe://pay?"),
    ("Paytm", "paytm.png", "paytmmp://pay?"),
    ("BHIM / any UPI app", "bhim.png", "upi://pay?"),
)


def _qr_svg(data: str) -> str:
    svg = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2).to_string()
    return svg.decode() if isinstance(svg, bytes) else svg


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{escape(title)}</title>
<style>
 body{{font-family:system-ui,-apple-system,sans-serif;background:#f3f4f6;margin:0;color:#111827}}
 .card{{max-width:440px;margin:0 auto;background:#fff;min-height:100vh;padding:24px 20px;box-sizing:border-box}}
 h1{{font-size:20px;margin:0 0 4px}} .muted{{color:#6b7280;font-size:14px}}
 .amount{{font-size:34px;font-weight:700;margin:16px 0 4px}}
 .apps{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:20px 0}}
 .app{{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:6px;height:96px;
       border:2px solid #e5e7eb;border-radius:14px;text-decoration:none;color:#374151;font-size:13px;background:#fff}}
 .app:active{{border-color:#2563eb;background:#eff6ff}} .app img{{max-width:78%;max-height:44px}}
 .qr{{text-align:center;margin-top:12px}} .qr svg{{width:200px;height:200px}}
 .upi{{background:#f9fafb;border-radius:10px;padding:10px 12px;font-family:monospace;display:flex;
       justify-content:space-between;align-items:center;margin-top:12px}}
 button{{border:0;background:#2563eb;color:#fff;border-radius:8px;padding:6px 12px;font-size:13px}}
 .note{{font-size:13px;color:#4b5563;margin-top:20px;line-height:1.5}} .paid{{font-size:22px;color:#059669}}
 button.app{{font:inherit;cursor:pointer}} .status{{background:#eff6ff;color:#1e40af;border-radius:10px;padding:12px;font-size:14px}}
 .more{{display:block;text-align:center;margin-top:14px;font-size:14px;color:#2563eb}}
</style></head><body><div class="card">{body}</div></body></html>""")


@router.get("/{order_id}", response_class=HTMLResponse)
def pay_page(order_id: int, t: str = "", db: Session = Depends(get_db)):
    if not hmac.compare_digest(t, sign_value(f"pay:{order_id}")):
        raise HTTPException(status_code=404, detail="Payment page not found")
    order = (
        db.query(models.Order)
        .options(joinedload(models.Order.business), joinedload(models.Order.items).joinedload(models.OrderItem.product))
        .filter(models.Order.id == order_id)
        .first()
    )
    if not order:
        raise HTTPException(status_code=404, detail="Payment page not found")
    b = order.business
    shop = escape(b.name)
    head = f"<h1>{shop}</h1><div class='muted'>Order #{order.id}</div>"

    if order.is_paid:
        return _page(f"Paid - {b.name}", f"{head}<p class='paid'>✅ Paid - thank you!</p>")
    if order.status == "Cancelled":
        return _page(b.name, f"{head}<p>This order was cancelled.</p>")
    items = ", ".join(f"{i.quantity} × {escape(i.product.name)}" for i in order.items)
    if effective_payment_method(b) in ("razorpay_link", "whatsapp_pay") and method_ready(b, "razorpay_link"):
        try:
            return _razorpay_page(db, b, order, head, items)
        except Exception as e:  # Razorpay down or keys wrong - fall through to UPI/manual below
            print(f"RAZORPAY ORDER FAILED (order {order.id}):", repr(e))
    if not b.upi_id:
        return _page(b.name, f"{head}<p>The seller will send you payment details on WhatsApp.</p>")

    query = upi_query(b, order)
    buttons = "".join(
        f"<a class='app' href='{escape(prefix + query)}'><img src='/static/pay/{logo}' alt=''>{escape(label)}</a>"
        for label, logo, prefix in UPI_APPS
    )
    upi_id = escape(b.upi_id)
    body = f"""{head}
<div class="amount">₹{order.total_amount:,.2f}</div><div class="muted">{items}</div>
<div class="apps">{buttons}</div>
<div class="qr">{_qr_svg('upi://pay?' + query)}<div class="muted">On a computer? Scan with any UPI app</div></div>
<div class="upi"><span>{upi_id}</span><button onclick="navigator.clipboard.writeText('{upi_id}');this.textContent='Copied'">Copy</button></div>
<p class="note">Money goes directly to {escape(b.upi_name or b.name)}. After you pay, the seller confirms it and
you'll get a message on WhatsApp. If anything goes wrong, just reply in the chat.</p>"""
    return _page(f"Pay {b.name}", body)


# Razorpay's UPI Intent app codes for the same four logos (works on Android and iOS).
RAZORPAY_APPS = (("Google Pay", "gpay.png", "gpay"), ("PhonePe", "phonepe.png", "phonepe"),
                 ("Paytm", "paytm.png", "paytm"), ("BHIM", "bhim.png", "bhim"))


def _razorpay_client(business: models.Business) -> razorpay.Client:
    creds = razorpay_creds(business)
    return razorpay.Client(auth=(creds.key_id, creds.key_secret))


def ensure_razorpay_order(db: Session, business: models.Business, order: models.Order) -> str:
    """Razorpay order for the exact total - it fixes the amount the UPI app shows. Created once."""
    if not order.razorpay_order_id:
        rp_order = _razorpay_client(business).order.create({
            "amount": round(order.total_amount * 100),
            "currency": "INR",
            "receipt": f"order-{order.id}",
            "notes": {"order_id": str(order.id), "business_id": str(business.id)},
        })
        order.razorpay_order_id = rp_order["id"]
        db.commit()
    return order.razorpay_order_id


def _razorpay_page(db: Session, b: models.Business, order: models.Order, head: str, items: str) -> HTMLResponse:
    from routers.payments import ensure_payment_link

    rp_order_id = ensure_razorpay_order(db, b, order)
    try:
        more_options = ensure_payment_link(db, b, order)  # cards, netbanking, QR on a laptop
    except Exception as e:
        print(f"PAYMENT LINK FAILED (order {order.id}):", repr(e))
        more_options = None
    buttons = "".join(
        f"<button class='app' data-app='{code}' data-label='{escape(label)}'><img src='/static/pay/{logo}' alt=''>"
        f"{escape(label)}</button>"
        for label, logo, code in RAZORPAY_APPS
    )
    payment = {
        "amount": round(order.total_amount * 100),
        "currency": "INR",
        "method": "upi",
        "contact": f"+{order.customer.phone}" if order.customer.phone.isdigit() else order.customer.phone,
        "email": "void@razorpay.com",  # Razorpay requires one; customers order by phone only
        "order_id": rp_order_id,
    }
    config = {"key": razorpay_creds(b).key_id, "payment": payment,
              "confirmUrl": f"/pay/{order.id}/confirm?t={sign_value(f'pay:{order.id}')}"}
    more = (f"<a class='more' href='{escape(more_options)}'>More options: card, netbanking, QR code</a>"
            if more_options else "")
    body = f"""{head}
<div class="amount">₹{order.total_amount:,.2f}</div><div class="muted">{items}</div>
<p class="muted">Tap your UPI app - the amount is already filled in, just enter your PIN.</p>
<div class="apps">{buttons}</div>
<div id="status" class="status" hidden></div>
{more}
<p class="note">🔒 Secure payment by Razorpay. Your order is confirmed automatically as soon as you pay.</p>
<script src="https://checkout.razorpay.com/v1/razorpay.js"></script>
<script>
const cfg = {json.dumps(config)};
const statusBox = document.getElementById("status");
function show(text) {{ statusBox.hidden = false; statusBox.textContent = text; }}
const rzp = new Razorpay({{ key: cfg.key }});
rzp.on("payment.success", async (r) => {{
  show("Payment done - confirming your order…");
  try {{
    const res = await fetch(cfg.confirmUrl, {{ method: "POST", headers: {{ "Content-Type": "application/json" }}, body: JSON.stringify(r) }});
    if (res.ok) {{ location.reload(); return; }}
  }} catch (e) {{}}
  show("Payment received. Your order will be confirmed on WhatsApp in a moment.");
}});
rzp.on("payment.error", (e) => {{
  const d = (e && (e.description || (e.error && e.error.description))) || "Payment didn't go through.";
  show(d + " Please try again or choose another app.");
}});
document.querySelectorAll("[data-app]").forEach((btn) => btn.addEventListener("click", () => {{
  show("Opening " + btn.dataset.label + "…");
  rzp.createPayment(cfg.payment, {{ app: btn.dataset.app }});
}}));
</script>"""
    return _page(f"Pay {b.name}", body)


class RazorpayResult(BaseModel):
    razorpay_payment_id: str
    razorpay_order_id: str
    razorpay_signature: str


@router.post("/{order_id}/confirm")
async def confirm_razorpay_payment(order_id: int, payload: RazorpayResult, t: str = "", db: Session = Depends(get_db)):
    """The pay page reports a successful Razorpay payment. Trust it only if Razorpay's signature
    (made with the seller's key secret) checks out for this order's Razorpay order."""
    from routers.payments import notify_payment_received

    if not hmac.compare_digest(t, sign_value(f"pay:{order_id}")):
        raise HTTPException(status_code=404, detail="Not found")
    order = (
        db.query(models.Order).options(joinedload(models.Order.business))
        .filter(models.Order.id == order_id).with_for_update().first()
    )
    if not order or not order.razorpay_order_id or payload.razorpay_order_id != order.razorpay_order_id:
        raise HTTPException(status_code=400, detail="Payment doesn't match this order")
    try:
        _razorpay_client(order.business).utility.verify_payment_signature(payload.model_dump())
    except razorpay.errors.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid payment signature")
    if not order.is_paid:  # the webhook may have got here first
        mark_order_paid(db, order, payload.razorpay_payment_id)
        db.commit()
        await notify_payment_received(order.business, order)
    return {"paid": True}


# ---- sending the payment request in WhatsApp ----------------------------------------------

def checkout_instruction(business: models.Business, order: models.Order) -> str | None:
    """Line appended to the order summary, for methods that send their own pay message. Also
    records the pay page as the order's link, so reminders and the AI can resend it."""
    method = effective_payment_method(business)
    if method in ("upi", "razorpay_link"):
        order.payment_link_url = pay_url(order.id)
        return "Tap the Pay button below to pay with Google Pay, PhonePe, Paytm or any UPI app."
    if method == "whatsapp_pay":
        return "Tap 'Review and pay' below to pay securely inside WhatsApp."
    return None


def wa_reference_id(order: models.Order) -> str:
    return f"RIRA{order.business_id}-{order.id}"


def _money(rupees: float) -> dict:
    return {"value": round(rupees * 100), "offset": 100}


def order_details_message(business: models.Business, order: models.Order) -> dict:
    subtotal = sum(i.price_at_order * i.quantity for i in order.items)
    return {
        "type": "order_details",
        "header": {"type": "image", "image": {"link": BANNER_URL}},
        "body": {"text": f"Order #{order.id} from {business.name}. Tap 'Review and pay' to complete your payment."},
        "footer": {"text": "Secure payment by Razorpay"},
        "action": {
            "name": "review_and_pay",
            "parameters": {
                "reference_id": wa_reference_id(order),
                "type": "physical-goods",
                "payment_settings": [{
                    "type": "payment_gateway",
                    "payment_gateway": {
                        "type": "razorpay",
                        "configuration_name": business.wa_payment_config,
                        "razorpay": {"receipt": f"order-{order.id}", "notes": {"order_id": str(order.id)}},
                    },
                }],
                "currency": "INR",
                "total_amount": _money(order.total_amount),
                "order": {
                    "status": "pending",
                    "items": [{
                        "retailer_id": str(i.product_id),
                        "name": i.product.name,
                        "amount": _money(i.price_at_order),
                        "quantity": i.quantity,
                    } for i in order.items],
                    "subtotal": _money(subtotal),
                    "tax": _money(0),
                    "shipping": _money(order.delivery_fee or 0),
                },
            },
        },
    }


def upi_button_message(business: models.Business, order: models.Order) -> dict:
    manual = effective_payment_method(business) == "upi"
    return {
        "type": "cta_url",
        "header": {"type": "image", "image": {"link": BANNER_URL}},
        "body": {"text": f"Order #{order.id} · Total ₹{order.total_amount:g}\n"
                         f"Pay {business.upi_name or business.name} with Google Pay, PhonePe, Paytm, BHIM or any UPI app."},
        "footer": {"text": "Money goes directly to the seller" if manual else "Secure payment by Razorpay"},
        "action": {"name": "cta_url",
                   "parameters": {"display_text": f"Pay ₹{order.total_amount:g}"[:20], "url": pay_url(order.id)}},
    }


async def send_payment_request(business: models.Business, order: models.Order, to: str):
    """Send the UPI button or WhatsApp Pay card after the order summary. Falls back to a plain
    link so the customer can always pay."""
    method = effective_payment_method(business)
    creds = whatsapp_creds(business)
    if method in ("upi", "razorpay_link"):
        message, fallback = upi_button_message(business, order), f"Pay here: {pay_url(order.id)}"
    elif method == "whatsapp_pay":
        message = order_details_message(business, order)
        fallback = (f"Pay here: {pay_url(order.id)}" if business.upi_id
                    else "The seller will send you the payment details shortly.")
    else:
        return
    try:
        await send_interactive(creds, to, message)
    except WhatsAppSendError as e:
        print(f"PAYMENT MESSAGE FAILED (order {order.id}), sending fallback:", e)
        try:
            await send_message(creds, to, fallback)
        except WhatsAppSendError as e2:
            print("PAYMENT FALLBACK NOT DELIVERED:", e2)


# ---- WhatsApp Pay confirmations ------------------------------------------------------------

def _rupees(money: dict | None) -> float | None:
    if not money:
        return None
    return money.get("value", 0) / (money.get("offset") or 100)


async def handle_payment_status(phone_number_id: str, status: dict):
    """Meta's webhook says a WhatsApp Pay payment changed. Confirm with Meta's lookup API before
    marking anything paid - the webhook alone could be forged."""
    from routers.payments import notify_payment_received

    reference_id = (status.get("payment") or {}).get("reference_id", "")
    if status.get("status") != "captured" or not reference_id.startswith("RIRA"):
        return
    try:
        business_part, order_part = reference_id[4:].split("-", 1)
        business_id, order_id = int(business_part), int(order_part)
    except ValueError:
        return

    with SessionLocal() as db:
        business = business_for_phone_number_id(db, phone_number_id)
        if not business or business.id != business_id or not business.wa_payment_config:
            return
        try:
            payment = await lookup_payment(whatsapp_creds(business), business.wa_payment_config, reference_id)
        except WhatsAppSendError as e:
            print(f"WHATSAPP PAY LOOKUP FAILED ({reference_id}):", e)
            return
        if not payment or payment.get("status") != "captured":
            print(f"WHATSAPP PAY NOT CAPTURED per lookup ({reference_id}):", payment)
            return
        success = next((t for t in payment.get("transactions") or [] if t.get("status") == "success"), None)

        order = (
            db.query(models.Order)
            .filter(models.Order.id == order_id, models.Order.business_id == business.id)
            .with_for_update()
            .first()
        )
        if not order or order.is_paid:
            return
        paid = _rupees(payment.get("amount"))
        if paid is None or abs(paid - order.total_amount) > 0.01:
            print(f"WHATSAPP PAY AMOUNT MISMATCH order {order.id}: paid {paid}, expected {order.total_amount}")
            return
        mark_order_paid(db, order, (success or {}).get("pg_transaction_id") or f"wapay-{reference_id}")
        db.commit()
        await notify_payment_received(business, order)
