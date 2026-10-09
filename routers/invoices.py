"""Printable HTML invoices at an unguessable public link, so customers can open them from
WhatsApp without logging in. HTML (not PDF) so names and addresses in any script - Tamil,
Hindi, Arabic... - render correctly; the seller prints or saves as PDF from the browser."""
import hmac
import os
from datetime import datetime, timedelta, timezone
from html import escape

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session, joinedload

import models
from auth import sign_value
from database import get_db

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://reone-backend.onrender.com").rstrip("/")
IST = timezone(timedelta(hours=5, minutes=30))

router = APIRouter(prefix="/invoices", tags=["invoices"])


def invoice_path(order_id: int) -> str:
    return f"/invoices/{order_id}?t={sign_value(f'invoice:{order_id}')}"


def invoice_url(order_id: int) -> str:
    return PUBLIC_BASE_URL + invoice_path(order_id)


def _fmt_date(value: datetime | None) -> str:
    if not value:
        return ""
    if value.tzinfo is None:  # SQLite stores UTC without a zone
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(IST).strftime("%d %b %Y")


def _money(value: float) -> str:
    return f"₹{value:,.2f}"


@router.get("/{order_id}", response_class=HTMLResponse)
def view_invoice(order_id: int, t: str = "", db: Session = Depends(get_db)):
    if not hmac.compare_digest(t, sign_value(f"invoice:{order_id}")):
        raise HTTPException(status_code=404, detail="Invoice not found")
    order = (
        db.query(models.Order)
        .options(joinedload(models.Order.customer), joinedload(models.Order.business),
                 joinedload(models.Order.items).joinedload(models.OrderItem.product))
        .filter(models.Order.id == order_id)
        .first()
    )
    if not order:
        raise HTTPException(status_code=404, detail="Invoice not found")

    b, c = order.business, order.customer
    seller_lines = [escape(x) for x in (b.address, f"Phone: {b.phone}" if b.phone else None,
                                         f"GSTIN: {b.gstin}" if b.gstin else None) if x]
    rows = "".join(
        f"<tr><td>{escape(i.product.name)}</td><td class='num'>{i.quantity}</td>"
        f"<td class='num'>{_money(i.price_at_order)}</td><td class='num'>{_money(i.price_at_order * i.quantity)}</td></tr>"
        for i in order.items
    )
    title = "Invoice" if order.is_paid else "Order summary"
    status = (f"<span class='paid'>PAID</span> on {_fmt_date(order.paid_at or order.created_at)}"
              if order.is_paid else "<span class='unpaid'>Payment pending</span>")

    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} #{order.id} - {escape(b.name)}</title>
<style>
  body {{ font-family: "Noto Sans", "Segoe UI", system-ui, sans-serif; color: #15181e; background: #f6f7f9; margin: 0; padding: 16px; }}
  .sheet {{ max-width: 720px; margin: 0 auto; background: #fff; border: 1px solid #e4e7eb; border-radius: 10px; padding: 32px; }}
  .head {{ display: flex; justify-content: space-between; gap: 16px; flex-wrap: wrap; border-bottom: 2px solid #0f9d6e; padding-bottom: 16px; }}
  h1 {{ margin: 0 0 4px; font-size: 1.4rem; }} h2 {{ margin: 0; font-size: 1.1rem; color: #0f9d6e; text-align: right; }}
  .muted {{ color: #6b7280; font-size: 0.85rem; line-height: 1.5; }}
  .meta {{ display: flex; justify-content: space-between; gap: 16px; flex-wrap: wrap; margin: 20px 0; }}
  .label {{ font-size: 0.7rem; text-transform: uppercase; letter-spacing: .05em; color: #6b7280; font-weight: 600; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
  th {{ text-align: left; font-size: 0.72rem; text-transform: uppercase; color: #6b7280; border-bottom: 1px solid #e4e7eb; padding: 8px 6px; }}
  td {{ padding: 10px 6px; border-bottom: 1px solid #f0f1f3; }} .num {{ text-align: right; white-space: nowrap; }}
  .total td {{ font-weight: 700; border-bottom: none; font-size: 1rem; }}
  .paid {{ color: #0b7a56; font-weight: 700; }} .unpaid {{ color: #b91c1c; font-weight: 700; }}
  .actions {{ max-width: 720px; margin: 12px auto 0; text-align: right; }}
  button {{ background: #0f9d6e; color: #fff; border: 0; border-radius: 8px; padding: 9px 16px; font-weight: 600; cursor: pointer; }}
  @media print {{ body {{ background: #fff; padding: 0; }} .sheet {{ border: 0; }} .actions {{ display: none; }} }}
  @media (max-width: 520px) {{ .sheet {{ padding: 20px 16px; }} h2 {{ text-align: left; }} }}
</style></head>
<body>
<div class="sheet">
  <div class="head">
    <div><h1>{escape(b.name)}</h1><div class="muted">{"<br>".join(seller_lines)}</div></div>
    <div><h2>{title}</h2><div class="muted" style="text-align:right">#{order.id}<br>{_fmt_date(order.created_at)}</div></div>
  </div>
  <div class="meta">
    <div><div class="label">Bill to</div><div>{escape(c.name)}</div>
      <div class="muted">+{escape(c.phone)}<br>{escape(c.address or "").replace(chr(10), "<br>")}</div></div>
    <div style="text-align:right"><div class="label">Status</div><div>{status}</div></div>
  </div>
  <table>
    <thead><tr><th>Item</th><th class="num">Qty</th><th class="num">Price</th><th class="num">Amount</th></tr></thead>
    <tbody>{rows}<tr class="total"><td colspan="3">Total</td><td class="num">{_money(order.total_amount)}</td></tr></tbody>
  </table>
</div>
<div class="actions"><button onclick="window.print()">Print / Save as PDF</button></div>
</body></html>"""
    return HTMLResponse(html)
