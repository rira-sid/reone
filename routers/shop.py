"""Public catalog page a seller can share (Instagram bio, WhatsApp Status...). Each product has an
"Order on WhatsApp" button that opens a chat with the shop, pre-filled with the item."""
import re
from html import escape
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

import models
from database import get_db

router = APIRouter(prefix="/shop", tags=["shop"])


def _wa_link(phone: str | None, text: str) -> str | None:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 10:  # Indian mobile without country code
        digits = "91" + digits
    if len(digits) < 11:
        return None
    return f"https://wa.me/{digits}?text={quote(text)}"


@router.get("/{business_id}", response_class=HTMLResponse)
def catalog(business_id: int, db: Session = Depends(get_db)):
    business = db.get(models.Business, business_id)
    if not business:
        raise HTTPException(status_code=404, detail="Shop not found")
    products = (
        db.query(models.Product)
        .filter(models.Product.business_id == business.id, models.Product.is_active.is_(True))
        .all()
    )
    groups: dict[str, list[models.Product]] = {}
    for p in sorted(products, key=lambda p: ((p.category or "~").lower(), p.name.lower())):
        groups.setdefault(p.category or "Other", []).append(p)

    sections = []
    for category, items in groups.items():
        cards = []
        for p in items:
            link = _wa_link(business.phone, f"Hi! I'd like to order {p.name}.")
            available = p.stock > 0
            button = (f'<a class="order" href="{escape(link)}">Order on WhatsApp</a>' if link and available
                      else '<span class="soldout">Out of stock</span>' if not available else "")
            description = f'<p class="desc">{escape(p.description)}</p>' if p.description else ""
            cards.append(
                f'<li class="{"" if available else "out"}"><div><h3>{escape(p.name)}</h3>{description}'
                f'<p class="price">₹{p.price:g} <span>/ {escape(p.unit or "pcs")}</span></p></div>{button}</li>'
            )
        heading = f"<h2>{escape(category)}</h2>" if len(groups) > 1 or category != "Other" else ""
        sections.append(f'<section>{heading}<ul>{"".join(cards)}</ul></section>')

    chat_link = _wa_link(business.phone, "Hi! I'd like to place an order.")
    chat_button = f'<a class="chat" href="{escape(chat_link)}">Chat to order</a>' if chat_link else ""
    notices = []
    if not business.accepting_orders:
        notices.append(escape(business.closed_message or "We're not taking orders right now - back soon!"))
    if business.delivery_fee:
        fee = f"Delivery ₹{business.delivery_fee:g}"
        if business.free_delivery_above:
            fee += f" · free above ₹{business.free_delivery_above:g}"
        notices.append(fee)
    if business.min_order_amount:
        notices.append(f"Minimum order ₹{business.min_order_amount:g}")
    if business.cod_enabled:
        notices.append("Cash on delivery available")
    notice_html = "".join(f"<span>{n}</span>" for n in notices)
    body = "".join(sections) if sections else '<p class="empty">No products listed yet.</p>'

    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(business.name)}</title>
<meta property="og:title" content="{escape(business.name)}">
<meta property="og:description" content="Browse our products and order on WhatsApp.">
<style>
  :root {{ --accent: #0f9d6e; --text: #15181e; --muted: #6b7280; --border: #e4e7eb; --bg: #f6f7f9; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; font-family: "Noto Sans", "Segoe UI", system-ui, sans-serif; color: var(--text); background: var(--bg); }}
  header {{ background: #11161d; color: #fff; padding: 28px 16px 22px; text-align: center; }}
  header h1 {{ margin: 0 0 6px; font-size: 1.5rem; }}
  header p {{ margin: 0; color: #c6ccd6; font-size: .9rem; }}
  .notices {{ display: flex; flex-wrap: wrap; gap: 6px; justify-content: center; margin-top: 14px; }}
  .notices span {{ background: rgba(255,255,255,.1); border-radius: 999px; padding: 4px 10px; font-size: .78rem; }}
  .chat {{ display: inline-block; margin-top: 16px; background: var(--accent); color: #fff; text-decoration: none;
           padding: 10px 20px; border-radius: 999px; font-weight: 700; }}
  main {{ max-width: 720px; margin: 0 auto; padding: 16px; }}
  h2 {{ font-size: 1rem; margin: 20px 4px 10px; }}
  ul {{ list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 10px; }}
  li {{ background: #fff; border: 1px solid var(--border); border-radius: 12px; padding: 14px 16px;
        display: flex; justify-content: space-between; align-items: center; gap: 12px; }}
  li.out {{ opacity: .6; }}
  h3 {{ margin: 0 0 2px; font-size: .98rem; }}
  .desc {{ margin: 0 0 4px; color: var(--muted); font-size: .82rem; }}
  .price {{ margin: 0; font-weight: 700; }} .price span {{ color: var(--muted); font-weight: 400; font-size: .8rem; }}
  .order {{ flex-shrink: 0; background: var(--accent); color: #fff; text-decoration: none; padding: 8px 12px;
            border-radius: 8px; font-size: .8rem; font-weight: 700; }}
  .soldout {{ flex-shrink: 0; color: #b91c1c; font-size: .8rem; font-weight: 600; }}
  .empty {{ text-align: center; color: var(--muted); }}
  footer {{ text-align: center; color: var(--muted); font-size: .75rem; padding: 24px 16px 32px; }}
</style></head>
<body>
<header>
  <h1>{escape(business.name)}</h1>
  <p>{escape(business.address or "")}</p>
  <div class="notices">{notice_html}</div>
  {chat_button}
</header>
<main>{body}</main>
<footer>Orders and payments on WhatsApp · powered by RIRAONE</footer>
</body></html>"""
    return HTMLResponse(html)
