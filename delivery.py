"""Expected delivery dates. The seller says how long delivery usually takes ("2-4 days"), for the whole
shop or per delivery zone; when an order is confirmed (paid, or cash on delivery placed) we fix the
window on the order and tell the customer "Expected delivery: 21-23 Oct"."""
from datetime import date, datetime, timedelta, timezone

import models
import zones

IST = timezone(timedelta(hours=5, minutes=30))


def delivery_days(business: models.Business, zone: models.DeliveryZone | None = None) -> tuple[int, int] | None:
    """(min, max) days, from the zone if it says, else the shop's general setting; None if nobody said."""
    terms = zones.zone_terms(business, zone) if zone else zones.general_terms(business)
    return terms.days


def set_expected(db, business: models.Business, order: models.Order, today: date | None = None) -> bool:
    """Fix the order's delivery window (once). Returns True if the order has one."""
    if order.expected_from:
        return True
    zone = db.get(models.DeliveryZone, order.delivery_zone_id) if order.delivery_zone_id else None
    days = delivery_days(business, zone)
    if not days:
        return False
    today = today or datetime.now(IST).date()
    order.expected_from = today + timedelta(days=days[0])
    order.expected_to = today + timedelta(days=days[1])
    return True


def window_text(start: date | None, end: date | None) -> str:
    """'Tue, 21 Oct' or '21-23 Oct' or '30 Oct - 2 Nov'."""
    if not start:
        return ""
    if not end or end == start:
        return start.strftime("%a, %d %b").replace(" 0", " ")
    if start.month == end.month:
        return f"{start.day}-{end.day} {end.strftime('%b')}"
    return f"{start.day} {start.strftime('%b')} - {end.day} {end.strftime('%b')}"


def expected_line(order: models.Order) -> str:
    """'🚚 Expected delivery: 21-23 Oct' or '' when unknown."""
    text = window_text(order.expected_from, order.expected_to)
    return f"🚚 Expected delivery: {text}" if text else ""
