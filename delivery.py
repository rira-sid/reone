"""Expected delivery dates. The seller says how long delivery usually takes ("2-4 days"); when an
order is confirmed (paid, or cash on delivery placed) we fix the window on the order and tell the
customer "Expected delivery: 21-23 Oct"."""
from datetime import date, datetime, timedelta, timezone

import models

IST = timezone(timedelta(hours=5, minutes=30))


def delivery_days(business: models.Business, order: models.Order | None = None) -> tuple[int, int] | None:
    """(min, max) days for this order, or None when the seller hasn't said."""
    low, high = business.delivery_days_min, business.delivery_days_max
    if low is None and high is None:
        return None
    low = high if low is None else low
    high = low if high is None else high
    return min(low, high), max(low, high)


def set_expected(business: models.Business, order: models.Order, today: date | None = None) -> bool:
    """Fix the order's delivery window (once). Returns True if the order has one."""
    if order.expected_from:
        return True
    days = delivery_days(business, order)
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
    day = lambda d: str(d.day)  # noqa: E731
    if start.month == end.month:
        return f"{day(start)}-{day(end)} {end.strftime('%b')}"
    return f"{day(start)} {start.strftime('%b')} - {day(end)} {end.strftime('%b')}"


def expected_line(order: models.Order) -> str:
    """'🚚 Expected delivery: 21-23 Oct' or '' when unknown."""
    text = window_text(order.expected_from, order.expected_to)
    return f"🚚 Expected delivery: {text}" if text else ""
