"""Expected delivery dates: seller's 'usually delivered in X-Y days' -> window on the order -> customer messages."""
from datetime import date, datetime, timedelta, timezone

import delivery
from test_app import new_seller
from test_seller_round import add_product

IST = timezone(timedelta(hours=5, minutes=30))


def cod_order(client, h, phone):
    product = add_product(client, h, price=150, stock=5, variants=[])
    r = client.post("/orders", json={"customer": {"name": "Ravi", "phone": phone, "address": "1 Road"},
                                      "items": [{"product_id": product["id"], "quantity": 1}],
                                      "payment_method": "cod"}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def test_window_text():
    assert delivery.window_text(date(2026, 10, 21), date(2026, 10, 23)) == "21-23 Oct"
    assert delivery.window_text(date(2026, 10, 30), date(2026, 11, 2)) == "30 Oct - 2 Nov"
    assert delivery.window_text(date(2026, 10, 5), date(2026, 10, 5)) == "Mon, 5 Oct"
    assert delivery.window_text(None, None) == ""


def test_setting_is_validated_and_saved(client):
    _, h = new_seller(client)
    assert client.patch("/business", json={"delivery_days_min": 99}, headers=h).status_code == 400
    r = client.patch("/business", json={"delivery_days_min": 2, "delivery_days_max": 4}, headers=h)
    assert r.status_code == 200
    b = client.get("/business", headers=h).json()
    assert b["delivery_days_min"] == 2 and b["delivery_days_max"] == 4


def test_cod_order_gets_expected_window(client):
    _, h = new_seller(client, cod_enabled=True, delivery_days_min=2, delivery_days_max=4)
    order = cod_order(client, h, "919000000101")
    today = datetime.now(IST).date()
    assert order["expected_from"] == str(today + timedelta(days=2))
    assert order["expected_to"] == str(today + timedelta(days=4))


def test_no_setting_no_window(client):
    _, h = new_seller(client, cod_enabled=True)
    order = cod_order(client, h, "919000000102")
    assert order["expected_from"] is None and order["expected_to"] is None


def test_one_number_means_exact_day(client):
    _, h = new_seller(client, cod_enabled=True, delivery_days_max=3)
    order = cod_order(client, h, "919000000103")
    assert order["expected_from"] == order["expected_to"] == str(datetime.now(IST).date() + timedelta(days=3))


def test_online_order_window_set_when_paid(client):
    _, h = new_seller(client, delivery_days_min=1, delivery_days_max=2)
    product = add_product(client, h, price=150, stock=5, variants=[])
    r = client.post("/orders", json={"customer": {"name": "Meena", "phone": "919000000104", "address": "2 Road"},
                                      "items": [{"product_id": product["id"], "quantity": 1}]}, headers=h)
    order = r.json()
    assert order["expected_from"] is None  # nothing promised before payment
    paid = client.patch(f"/orders/{order['id']}/payment", json={"is_paid": True, "notify_customer": False}, headers=h).json()
    assert paid["expected_from"] == str(datetime.now(IST).date() + timedelta(days=1))
