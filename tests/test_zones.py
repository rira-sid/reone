"""Delivery zones: matching an address, zone terms at checkout, unknown areas go to the owner, bot context."""
from types import SimpleNamespace

import chatbot
import models
import zones
from database import SessionLocal
from test_app import new_seller, shop_checkout
from test_seller_round import add_product


def zone(name, match_type, match_values=None, **terms):
    return SimpleNamespace(name=name, match_type=match_type, match_values=match_values, delivers=True,
                           days_min=None, days_max=None, fee=None, free_above=None, min_order=None, cod=None,
                           note=None, **{k: v for k, v in terms.items()})


def test_matching_order():
    city = zone("Coimbatore city", "pincode", "641")
    exact = zone("Gandhipuram", "pincode", "641012")
    chennai = zone("Chennai", "city", "Chennai, Madras")
    tn = zone("Tamil Nadu", "state", "Tamil Nadu")
    goa = zone("Goa", "state", "Goa")
    rest = zone("Rest of India", "rest")
    all_zones = [city, exact, chennai, tn, goa, rest]
    assert zones.find_zone(all_zones, "641012", "") is exact  # longest PIN prefix wins
    assert zones.find_zone(all_zones, "641 001", "") is city
    assert zones.find_zone(all_zones, None, "5 Anna Salai, Madras") is chennai  # old name works
    assert zones.find_zone(all_zones, None, "Main road, Coimbatore 641 004") is city  # PIN inside the address
    assert zones.find_zone(all_zones, "620001", "Trichy") is tn  # state from the PIN code
    assert zones.find_zone(all_zones, "403001", "Panaji") is goa  # Goa is inside Maharashtra's 40x
    assert zones.find_zone(all_zones, "110001", "Delhi") is rest
    assert zones.find_zone([city, chennai], "110001", "Delhi") is None


def test_zone_api_validation(client):
    _, h = new_seller(client)
    assert client.post("/business/zones", json={"name": "X", "match_type": "pincode", "match_values": "abc"},
                       headers=h).status_code == 400
    assert client.post("/business/zones", json={"name": "", "match_type": "rest"}, headers=h).status_code == 400
    r = client.post("/business/zones", json={"name": "Everywhere", "match_type": "rest", "fee": 80}, headers=h)
    assert r.status_code == 200
    assert client.post("/business/zones", json={"name": "Again", "match_type": "rest"}, headers=h).status_code == 409
    r = client.post("/business/zones", json={"name": "Local", "match_type": "pincode", "match_values": "641,\n 642 001",
                                             "fee": 0, "days_min": 1, "days_max": 1}, headers=h)
    assert r.json()["match_values"] == "641, 642001"
    zid = r.json()["id"]
    assert [z["name"] for z in client.get("/business/zones", headers=h).json()] == ["Everywhere", "Local"]
    check = client.get("/business/zones/check", params={"pincode": "641001"}, headers=h).json()
    assert check["zone"] == "Local" and check["fee"] == 0 and check["days"] == [1, 1]
    r = client.put(f"/business/zones/{zid}", json={"name": "Local", "match_type": "pincode", "match_values": "641",
                                                    "delivers": False}, headers=h)
    assert r.status_code == 200 and r.json()["delivers"] is False
    assert client.delete(f"/business/zones/{zid}", headers=h).status_code == 200
    _, other = new_seller(client, name="Other")
    assert client.get("/business/zones", headers=other).json() == []


def test_checkout_uses_zone_terms(client):
    bid, h = new_seller(client, cod_enabled=True, delivery_fee=40, delivery_days_min=3, delivery_days_max=5)
    product = add_product(client, h, price=100, stock=20, variants=[])
    client.post("/business/zones", json={"name": "Chennai", "match_type": "city", "match_values": "Chennai",
                                         "fee": 0, "days_min": 1, "days_max": 2, "cod": False}, headers=h)
    client.post("/business/zones", json={"name": "Rest of India", "match_type": "rest", "fee": 90, "min_order": 300},
                headers=h)
    items = [{"product_id": product["id"], "quantity": 1}]
    # Chennai (address says so): free delivery, no COD
    q = client.post(f"/shop/{bid}/quote", json={"address": "12 Gandhi St, Chennai", "subtotal": 100}).json()
    assert q["zone"] == "Chennai" and q["fee"] == 0 and q["cod"] is False and q["eta"]
    assert shop_checkout(client, bid, "919000000201", items, payment_method="cod").status_code == 400
    r = shop_checkout(client, bid, "919000000201", items)
    assert r.status_code == 200, r.text
    order = next(o for o in client.get("/orders", headers=h).json() if o["id"] == r.json()["order_id"])
    assert order["delivery_fee"] == 0 and order["total_amount"] == 100
    # Elsewhere: ₹90 and a ₹300 minimum
    far = {"address": "4 MG Road, Pune", "pincode": "411001"}
    assert shop_checkout(client, bid, "919000000202", items, **far).status_code == 400  # below minimum
    r = shop_checkout(client, bid, "919000000202", [{"product_id": product["id"], "quantity": 3}],
                      payment_method="cod", **far)
    assert r.status_code == 200, r.text
    order = next(o for o in client.get("/orders", headers=h).json() if o["id"] == r.json()["order_id"])
    assert order["delivery_fee"] == 90 and order["total_amount"] == 390 and order["expected_from"]
    with SessionLocal() as db:
        saved = db.get(models.Order, order["id"])
        assert saved.pincode == "411001" and saved.delivery_zone_id is not None


def test_no_delivery_and_unknown_area(client, fake_whatsapp):
    bid, h = new_seller(client, alert_phone="919800000001", wa_phone_number_id="PN_ZONE", wa_token="t")
    product = add_product(client, h, price=100, stock=20, variants=[])
    client.post("/business/zones", json={"name": "Coimbatore", "match_type": "pincode", "match_values": "641"},
                headers=h)
    client.post("/business/zones", json={"name": "Islands", "match_type": "pincode", "match_values": "744",
                                         "delivers": False, "note": "Sorry, couriers don't reach there."}, headers=h)
    items = [{"product_id": product["id"], "quantity": 1}]
    r = shop_checkout(client, bid, "919000000203", items, address="1 Beach Rd, Port Blair", pincode="744101")
    assert r.status_code == 400 and "couriers" in r.json()["detail"]
    r = shop_checkout(client, bid, "919000000204", items, address="4 MG Road, Pune", pincode="411001")
    assert r.status_code == 409
    assert client.get("/orders", headers=h).json() == []
    chat = next(c for c in client.get("/conversations", headers=h).json() if c["phone"] == "919000000204")
    assert chat["needs_seller"] is True
    alert = [s for s in fake_whatsapp.sent if s[1] == "919800000001"]
    assert alert, "the owner gets an alert about the unknown area"


def test_bot_gets_zone_text(client):
    bid, h = new_seller(client, delivery_fee=50)
    client.post("/business/zones", json={"name": "Coimbatore", "match_type": "pincode", "match_values": "641",
                                         "fee": 0, "days_min": 1, "days_max": 1, "note": "Same day before 2 pm"},
                headers=h)
    with SessionLocal() as db:
        business = db.get(models.Business, bid)
        text = chatbot.shop_policies(business, db)
    assert "Coimbatore (PIN codes starting 641): free delivery" in text and "Same day before 2 pm" in text
    assert "not known - check with the shop" in text
