"""Numbers for the dashboard home page and the customers list. Computed in Python - fine at
small-seller volumes; move to SQL aggregates if a business ever has tens of thousands of orders."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session, joinedload

import models
from auth import current_business
from database import get_db

router = APIRouter(tags=["insights"])
IST = timezone(timedelta(hours=5, minutes=30))
LOW_STOCK = 5


def _ist(value: datetime) -> datetime:
    if value.tzinfo is None:  # SQLite stores UTC without a zone
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(IST)


def _paid_amount(orders):
    return round(sum(o.total_amount for o in orders if o.is_paid), 2)


@router.get("/stats")
def stats(db: Session = Depends(get_db), business: models.Business = Depends(current_business)):
    today = datetime.now(IST).date()
    orders = (
        db.query(models.Order)
        .options(joinedload(models.Order.items).joinedload(models.OrderItem.product))
        .filter(models.Order.business_id == business.id, models.Order.status != "Cancelled")
        .all()
    )
    dated = [(o, _ist(o.created_at).date()) for o in orders if o.created_at]
    todays = [o for o, d in dated if d == today]
    this_week = [o for o, d in dated if (today - d).days < 7]
    unpaid = [o for o in orders if not o.is_paid]

    daily = []
    for back in range(13, -1, -1):
        day = today - timedelta(days=back)
        day_orders = [o for o, d in dated if d == day]
        daily.append({"date": day.isoformat(), "orders": len(day_orders), "revenue": _paid_amount(day_orders)})

    sold = defaultdict(lambda: {"quantity": 0, "revenue": 0.0})
    for o, d in dated:
        if (today - d).days < 30 and o.is_paid:
            for item in o.items:
                entry = sold[item.product.name]
                entry["quantity"] += item.quantity
                entry["revenue"] += item.quantity * item.price_at_order
    top_products = sorted(
        ({"name": name, **values} for name, values in sold.items()), key=lambda p: p["quantity"], reverse=True
    )[:5]

    conversations = db.query(models.Conversation).filter(
        models.Conversation.business_id == business.id, models.Conversation.ai_paused.is_(True)
    ).all()
    # A paused chat only needs the seller if the customer spoke last.
    needs_you = sum(1 for c in conversations if c.messages and c.messages[-1].sender == "customer")

    low_stock = db.query(models.Product).filter(
        models.Product.business_id == business.id,
        models.Product.is_active.is_(True),
        models.Product.stock <= LOW_STOCK,
    ).count()

    return {
        "today": {"orders": len(todays), "revenue": _paid_amount(todays)},
        "week": {"orders": len(this_week), "revenue": _paid_amount(this_week)},
        "unpaid": {"orders": len(unpaid), "amount": round(sum(o.total_amount for o in unpaid), 2)},
        "to_ship": sum(1 for o in orders if o.is_paid and o.status in ("Confirmed", "Packed")),
        "needs_you": needs_you,
        "low_stock": low_stock,
        "total_orders": len(orders),
        "daily": daily,
        "top_products": top_products,
    }


@router.get("/customers")
def customers(db: Session = Depends(get_db), business: models.Business = Depends(current_business)):
    """One row per phone number (manual orders can create duplicate customer records)."""
    rows = {}
    records = (
        db.query(models.Customer)
        .options(joinedload(models.Customer.orders))
        .filter(models.Customer.business_id == business.id)
        .all()
    )
    for customer in records:
        row = rows.setdefault(customer.phone, {
            "phone": customer.phone, "name": customer.name, "address": customer.address,
            "orders": 0, "total_spent": 0.0, "last_order_at": None,
        })
        for order in customer.orders:
            if order.status == "Cancelled":
                continue
            row["orders"] += 1
            if order.is_paid:
                row["total_spent"] += order.total_amount
            if order.created_at and (row["last_order_at"] is None or order.created_at > row["last_order_at"]):
                row["last_order_at"] = order.created_at
                row["name"] = customer.name or row["name"]
                row["address"] = customer.address or row["address"]
    result = [r for r in rows.values() if r["orders"]]
    for r in result:
        r["total_spent"] = round(r["total_spent"], 2)
    # Compare as timestamps: Postgres returns aware datetimes, SQLite naive ones.
    return sorted(result, key=lambda r: _ist(r["last_order_at"]).timestamp() if r["last_order_at"] else 0, reverse=True)
