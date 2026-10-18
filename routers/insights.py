"""Numbers for the dashboard home page and the customers list. Computed in Python - fine at
small-seller volumes; move to SQL aggregates if a business ever has tens of thousands of orders."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, or_
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


def funnel(db: Session, business_id: int, days: int = 30) -> dict:
    """Shop funnel over the last `days`: page views -> added to cart -> opened checkout -> orders -> paid."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    counts = dict(
        db.query(models.ShopEvent.kind, func.count(models.ShopEvent.id))
        .filter(models.ShopEvent.business_id == business_id, models.ShopEvent.created_at >= since)
        .group_by(models.ShopEvent.kind)
        .all()
    )
    paid = db.query(models.Order).filter(
        models.Order.business_id == business_id, models.Order.created_at >= since, models.Order.is_paid.is_(True)
    ).count()
    return {"views": counts.get("view", 0), "carts": counts.get("add_to_cart", 0),
            "checkouts": counts.get("checkout_open", 0), "orders": counts.get("order", 0), "paid": paid}


def unread_chats(db: Session, business_id: int) -> int:
    from routers.conversations import is_unread

    chats = db.query(models.Conversation).filter(
        models.Conversation.business_id == business_id, models.Conversation.last_customer_message_at.isnot(None)).all()
    return sum(1 for c in chats if is_unread(c))


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
                entry = sold[item.display_name]
                entry["quantity"] += item.quantity
                entry["revenue"] += item.quantity * item.price_at_order
    top_products = sorted(
        ({"name": name, **values} for name, values in sold.items()), key=lambda p: p["quantity"], reverse=True
    )[:5]

    conversations = db.query(models.Conversation).filter(
        models.Conversation.business_id == business.id,
        or_(models.Conversation.ai_paused.is_(True), models.Conversation.needs_seller.is_(True)),
    ).all()
    # Open customer questions, plus paused chats where the customer spoke last.
    needs_you = sum(1 for c in conversations
                    if c.needs_seller or (c.messages and c.messages[-1].sender == "customer"))

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
        "unread": unread_chats(db, business.id),
        "open_issues": db.query(models.Incident).filter(
            models.Incident.business_id == business.id, models.Incident.status != "resolved").count(),
        "low_stock": low_stock,
        "total_orders": len(orders),
        "daily": daily,
        "top_products": top_products,
        "funnel": funnel(db, business.id),
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


def _change(now_value: float, before: float) -> int | None:
    """Percent change vs the previous period; None when there's nothing to compare with."""
    if not before:
        return None
    return round((now_value - before) / before * 100)


@router.get("/insights")
def insights(days: int = 30, db: Session = Depends(get_db), business: models.Business = Depends(current_business)):
    """The Home page's deeper numbers for the last `days` (7, 30 or 90), compared with the period before."""
    from languages import LANGUAGES

    days = days if days in (7, 30, 90) else 30
    now = datetime.now(timezone.utc)
    start, prev_start = now - timedelta(days=days), now - timedelta(days=2 * days)

    def when(o):
        return _ist(o.created_at)

    orders = (
        db.query(models.Order)
        .options(joinedload(models.Order.items).joinedload(models.OrderItem.product), joinedload(models.Order.customer))
        .filter(models.Order.business_id == business.id, models.Order.created_at >= prev_start)
        .all()
    )
    cur = [o for o in orders if o.created_at and when(o) >= start.astimezone(IST)]
    prev = [o for o in orders if o.created_at and when(o) < start.astimezone(IST)]
    live = [o for o in cur if o.status != "Cancelled"]
    paid = [o for o in live if o.is_paid]
    prev_paid = [o for o in prev if o.status != "Cancelled" and o.is_paid]
    revenue, prev_revenue = _paid_amount(paid), _paid_amount(prev_paid)

    # Customers: new = first ever order in this period.
    phones = {o.customer.phone for o in live if o.customer}
    earlier = {phone for (phone,) in db.query(models.Customer.phone).join(models.Order).filter(
        models.Order.business_id == business.id, models.Order.created_at < start, models.Order.status != "Cancelled"
    ).distinct().all()}
    returning = len(phones & earlier)

    # Busiest hours / weekdays (IST), by orders placed.
    hours = [0] * 24
    weekdays = [0] * 7
    for o in live:
        hours[when(o).hour] += 1
        weekdays[when(o).weekday()] += 1

    # Chat automation.
    message_counts = dict(
        db.query(models.Message.sender, func.count(models.Message.id)).join(models.Conversation)
        .filter(models.Conversation.business_id == business.id, models.Message.created_at >= start)
        .group_by(models.Message.sender).all()
    )
    chats = db.query(models.Conversation).filter(
        models.Conversation.business_id == business.id, models.Conversation.last_customer_message_at >= start).all()
    alerted = {cid for (cid,) in db.query(models.SellerAlert.conversation_id).filter(
        models.SellerAlert.business_id == business.id, models.SellerAlert.created_at >= start).distinct().all()}
    handled_by_bot = sum(1 for c in chats if c.id not in alerted)
    languages = defaultdict(int)
    for c in chats:
        languages[c.preferred_language or "en"] += 1

    # Products: best sellers and ones that didn't sell.
    sold = defaultdict(lambda: {"quantity": 0, "revenue": 0.0})
    sold_ids = set()
    for o in paid:
        for item in o.items:
            entry = sold[item.display_name]
            entry["quantity"] += item.quantity
            entry["revenue"] += item.quantity * item.price_at_order
            sold_ids.add(item.product_id)
    active_products = db.query(models.Product).filter(
        models.Product.business_id == business.id, models.Product.is_active.is_(True)).all()
    views = funnel(db, business.id, days)

    return {
        "days": days,
        "revenue": revenue, "revenue_change": _change(revenue, prev_revenue),
        "orders": len(live), "orders_change": _change(len(live), len([o for o in prev if o.status != "Cancelled"])),
        "avg_order": round(revenue / len(paid)) if paid else 0,
        "paid_rate": round(len(paid) / len(live) * 100) if live else None,
        "cancelled": sum(1 for o in cur if o.status == "Cancelled"),
        "cod_orders": sum(1 for o in live if o.payment_method == "cod"),
        "customers": len(phones), "new_customers": len(phones - earlier), "returning_customers": returning,
        "conversion": round(views["orders"] / views["views"] * 100, 1) if views["views"] else None,
        "funnel": views,
        "chats": len(chats), "handled_by_bot": handled_by_bot,
        "bot_rate": round(handled_by_bot / len(chats) * 100) if chats else None,
        "questions_to_you": len(alerted),
        "messages": {"customer": message_counts.get("customer", 0), "bot": message_counts.get("ai", 0),
                     "you": message_counts.get("seller", 0)},
        "languages": sorted(({"code": k, "name": LANGUAGES.get(k, (k, k))[1], "chats": v} for k, v in languages.items()),
                            key=lambda r: r["chats"], reverse=True),
        "hours": hours, "weekdays": weekdays,
        "top_products": sorted(({"name": n, "quantity": v["quantity"], "revenue": round(v["revenue"], 2)}
                                for n, v in sold.items()), key=lambda p: p["revenue"], reverse=True)[:8],
        "not_selling": [p.name for p in active_products if p.id not in sold_ids][:12],
        "products_total": len(active_products),
    }
