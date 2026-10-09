import hashlib
import hmac
import itertools
import json
from datetime import datetime, timedelta, timezone

import ai
import models
from database import SessionLocal

_ids = itertools.count(1)


# ---- helpers -------------------------------------------------------------------------------

def new_seller(client, name="Test Shop", **settings):
    """Sign up a fresh business; returns (business_id, auth headers)."""
    n = next(_ids)
    r = client.post("/auth/signup", json={"business_name": name, "email": f"seller{n}@test.in",
                                          "password": "password1", "signup_code": "PILOT1"})
    assert r.status_code == 200, r.text
    headers = {"Authorization": f"Bearer {r.json()['token']}"}
    if settings:
        assert client.patch("/business", json=settings, headers=headers).status_code == 200
    return r.json()["business_id"], headers


def owner_headers(client):
    token = client.post("/auth/login", json={"password": "owner-pass"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def add_product(client, headers, name="Masala", price=100, stock=10):
    return client.post("/products", json={"name": name, "price": price, "stock": stock}, headers=headers).json()


def whatsapp_message(client, phone_number_id, sender, message, message_id=None):
    message_id = message_id or f"wamid.{next(_ids)}"
    return client.post("/webhook", json={"entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": phone_number_id},
        "contacts": [{"profile": {"name": "Customer"}}],
        "messages": [{"id": message_id, "from": sender, **message}],
    }}]}]})


def text(body):
    return {"type": "text", "text": {"body": body}}


def razorpay_event(order_id, amount_rupees):
    return json.dumps({"event": "payment_link.paid", "payload": {
        "payment_link": {"entity": {"reference_id": str(order_id)}},
        "payment": {"entity": {"id": f"pay_{next(_ids)}", "amount": round(amount_rupees * 100)}},
    }}).encode()


def signed(secret, body):
    return {"X-Razorpay-Signature": hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(),
            "Content-Type": "application/json"}


def place_whatsapp_order(client, fake_ai, pnid, phone, product_id, qty=2):
    """Drive a WhatsApp chat through to a confirmed order; returns the new order id."""
    fake_ai.queue = [fake_ai.turn(cart=[ai.CartItem(product_id=product_id, quantity=qty)], customer_name="Ravi",
                                  delivery_address="12 Gandhi St, Chennai 600001", ready_to_place_order=True,
                                  reply="Thank you!")]
    whatsapp_message(client, pnid, phone, text("yes confirm"))
    with SessionLocal() as db:
        return db.query(models.Order).order_by(models.Order.id.desc()).first().id


# ---- auth ----------------------------------------------------------------------------------

def test_signup_rules(client):
    body = {"business_name": "X", "email": "rules@test.in", "password": "password1", "signup_code": "PILOT1"}
    assert client.post("/auth/signup", json={**body, "signup_code": "nope"}).status_code == 403
    assert client.post("/auth/signup", json={**body, "password": "short"}).status_code == 400
    assert client.post("/auth/signup", json=body).status_code == 200
    assert client.post("/auth/signup", json={**body, "email": "RULES@test.in"}).status_code == 409


def test_login(client):
    new_seller(client)
    n = next(_ids) - 1
    assert client.post("/auth/login", json={"email": f"SELLER{n}@test.in", "password": "password1"}).status_code == 200
    assert client.post("/auth/login", json={"email": f"seller{n}@test.in", "password": "wrong"}).status_code == 401
    assert client.post("/auth/login", json={"password": "owner-pass"}).status_code == 200
    assert client.get("/orders").status_code == 401
    assert client.get("/orders", headers={"Authorization": "Bearer forged"}).status_code == 401


def test_meta_webhook_verification(client):
    ok = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "42"})
    assert ok.status_code == 200 and ok.text == "42"
    assert client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "x"}).status_code == 403


# ---- isolation & settings ------------------------------------------------------------------

def test_businesses_are_isolated(client):
    _, a = new_seller(client)
    _, b = new_seller(client)
    product = add_product(client, a, "Only A")
    assert [p["name"] for p in client.get("/products", headers=b).json()] == []
    assert client.patch(f"/products/{product['id']}", json={"price": 1}, headers=b).status_code == 404
    assert client.delete(f"/products/{product['id']}", headers=b).status_code == 404


def test_secrets_are_write_only_and_encrypted(client):
    business_id, h = new_seller(client)
    out = client.patch("/business", json={"wa_phone_number_id": f"PN{business_id}", "wa_token": "tok-secret",
                                          "razorpay_key_id": "rzp_test", "razorpay_key_secret": "key-secret",
                                          "razorpay_webhook_secret": "hook-secret"}, headers=h).json()
    assert out["whatsapp_connected"] and out["razorpay_connected"] and out["razorpay_webhook_ready"]
    assert not any(s in json.dumps(out) for s in ("tok-secret", "key-secret", "hook-secret"))
    with SessionLocal() as db:
        stored = db.get(models.Business, business_id)
        assert "tok-secret" not in stored.wa_token_enc
    # Blank secret = leave unchanged
    out = client.patch("/business", json={"wa_token": ""}, headers=h).json()
    assert out["whatsapp_connected"]


def test_whatsapp_number_cannot_be_claimed_twice(client):
    _, a = new_seller(client, wa_phone_number_id="PN_SHARED", wa_token="t")
    _, b = new_seller(client)
    assert client.patch("/business", json={"wa_phone_number_id": "PN_SHARED"}, headers=b).status_code == 409


# ---- WhatsApp chat flow --------------------------------------------------------------------

def test_message_routed_to_right_business(client, fake_ai, fake_whatsapp):
    _, h = new_seller(client, name="Anu Bakes", wa_phone_number_id="PN_ANU", wa_token="t")
    add_product(client, h, "Cupcake")
    whatsapp_message(client, "PN_ANU", "919000000001", text("2 cupcakes"))
    assert fake_ai.calls[-1]["shop"] == "Anu Bakes" and fake_ai.calls[-1]["products"] == ["Cupcake"]
    assert fake_whatsapp.sent[-1][:2] == ("PN_ANU", "919000000001")


def test_legacy_number_goes_to_business_one(client, fake_whatsapp):
    whatsapp_message(client, "ENV_PNID", "919000000002", text("hi"))
    assert fake_whatsapp.sent[-1][0] == "ENV_PNID"


def test_unknown_number_ignored(client, fake_ai, fake_whatsapp):
    whatsapp_message(client, "NOBODY", "919000000003", text("hi"))
    assert fake_ai.calls == [] and fake_whatsapp.sent == []


def test_retried_delivery_answered_once(client, fake_ai, fake_whatsapp):
    new_seller(client, wa_phone_number_id="PN_RETRY", wa_token="t")
    whatsapp_message(client, "PN_RETRY", "919000000004", text("hi"), message_id="wamid.same")
    import main
    main._seen_message_id_set.clear()  # simulate a restart wiping the in-memory dedupe
    main._SEEN_MESSAGE_IDS.clear()
    whatsapp_message(client, "PN_RETRY", "919000000004", text("hi"), message_id="wamid.same")
    assert len(fake_ai.calls) == 1 and len(fake_whatsapp.sent) == 1


def test_photo_passed_to_ai(client, fake_ai):
    new_seller(client, wa_phone_number_id="PN_PHOTO", wa_token="t")
    whatsapp_message(client, "PN_PHOTO", "919000000005", {"type": "image", "image": {"id": "M1", "caption": "this"}})
    assert fake_ai.calls[-1]["image"] == (b"\xff\xd8fake-jpeg", "image/jpeg")


def test_checkout_creates_order(client, fake_ai, fake_whatsapp):
    _, h = new_seller(client, wa_phone_number_id="PN_SHOP", wa_token="t")
    product = add_product(client, h, "Pepper", price=90, stock=5)
    order_id = place_whatsapp_order(client, fake_ai, "PN_SHOP", "919000000006", product["id"], qty=2)
    order = [o for o in client.get("/orders", headers=h).json() if o["id"] == order_id][0]
    assert order["total_amount"] == 180 and order["customer"]["name"] == "Ravi"
    assert f"Order #{order_id}" in fake_whatsapp.sent[-1][3]
    # Razorpay isn't connected, so the customer is told payment details will follow.
    assert "payment details" in fake_whatsapp.sent[-1][3]


def test_out_of_stock_hands_off_to_seller(client, fake_ai, fake_whatsapp):
    _, h = new_seller(client, wa_phone_number_id="PN_STOCK", wa_token="t")
    product = add_product(client, h, stock=1)
    place_whatsapp_order(client, fake_ai, "PN_STOCK", "919000000007", product["id"], qty=5)
    convo = client.get("/conversations", headers=h).json()[0]
    assert convo["ai_paused"] and "seller will reply" in fake_whatsapp.sent[-1][3]


def test_ai_failure_hands_off(client, fake_ai, fake_whatsapp, monkeypatch):
    _, h = new_seller(client, wa_phone_number_id="PN_FAIL", wa_token="t")

    async def broken(*args, **kwargs):
        raise RuntimeError("API down")

    monkeypatch.setattr(ai, "run_turn", broken)
    whatsapp_message(client, "PN_FAIL", "919000000008", text("hi"))
    assert "seller will reply" in fake_whatsapp.sent[-1][3]
    assert client.get("/conversations", headers=h).json()[0]["ai_paused"]


def test_paused_chat_gets_no_ai_reply(client, fake_ai, fake_whatsapp):
    _, h = new_seller(client, wa_phone_number_id="PN_PAUSE", wa_token="t")
    whatsapp_message(client, "PN_PAUSE", "919000000009", text("hi"))
    convo_id = client.get("/conversations", headers=h).json()[0]["id"]
    client.patch(f"/conversations/{convo_id}/takeover", json={"ai_paused": True}, headers=h)
    sent_before = len(fake_whatsapp.sent)
    whatsapp_message(client, "PN_PAUSE", "919000000009", text("hello?"))
    assert len(fake_whatsapp.sent) == sent_before


def test_ai_sees_customers_past_orders(client, fake_ai):
    _, h = new_seller(client, wa_phone_number_id="PN_HIST", wa_token="t")
    product = add_product(client, h)
    order_id = place_whatsapp_order(client, fake_ai, "PN_HIST", "919000000010", product["id"])
    whatsapp_message(client, "PN_HIST", "919000000010", text("where is my order?"))
    assert fake_ai.calls[-1]["orders"] == [order_id]


# ---- inbox ---------------------------------------------------------------------------------

def test_seller_reply_and_failure(client, fake_whatsapp):
    _, h = new_seller(client, wa_phone_number_id="PN_REPLY", wa_token="t")
    whatsapp_message(client, "PN_REPLY", "919000000011", text("hi"))
    convo = client.get("/conversations", headers=h).json()[0]
    assert convo["reply_window_open"]
    assert client.post(f"/conversations/{convo['id']}/reply", json={"text": "Hello!"}, headers=h).status_code == 200
    assert fake_whatsapp.sent[-1][3] == "Hello!"
    fake_whatsapp.fail = True
    assert client.post(f"/conversations/{convo['id']}/reply", json={"text": "lost"}, headers=h).status_code == 502
    texts = [m["text"] for m in client.get(f"/conversations/{convo['id']}/messages", headers=h).json()]
    assert "Hello!" in texts and "lost" not in texts


def test_customer_orders_in_inbox(client, fake_ai):
    _, h = new_seller(client, wa_phone_number_id="PN_INBOX", wa_token="t")
    product = add_product(client, h)
    order_id = place_whatsapp_order(client, fake_ai, "PN_INBOX", "919000000012", product["id"])
    convo_id = client.get("/conversations", headers=h).json()[0]["id"]
    assert [o["id"] for o in client.get(f"/conversations/{convo_id}/orders", headers=h).json()] == [order_id]


# ---- payments, invoices, updates -----------------------------------------------------------

def test_payment_webhook(client, fake_ai, fake_whatsapp):
    business_id, h = new_seller(client, wa_phone_number_id="PN_PAY", wa_token="t", razorpay_webhook_secret="hook2")
    product = add_product(client, h, price=50, stock=10)
    order_id = place_whatsapp_order(client, fake_ai, "PN_PAY", "919000000013", product["id"], qty=2)
    url = f"/payments/webhook/razorpay/{business_id}"
    body = razorpay_event(order_id, 100)

    assert client.post(url, content=body, headers=signed("wrong", body)).status_code == 400
    wrong_amount = razorpay_event(order_id, 1)
    assert client.post(url, content=wrong_amount, headers=signed("hook2", wrong_amount)).status_code == 400

    assert client.post(url, content=body, headers=signed("hook2", body)).json() == {"status": "ok"}
    assert "Invoice: https://" in fake_whatsapp.sent[-1][3]
    assert client.post(url, content=body, headers=signed("hook2", body)).json() == {"status": "already processed"}

    order = [o for o in client.get("/orders", headers=h).json() if o["id"] == order_id][0]
    assert order["is_paid"] and order["status"] == "Confirmed"
    assert client.get("/products", headers=h).json()[0]["stock"] == 8  # deducted exactly once


def test_legacy_payment_webhook_url(client):
    body = razorpay_event(999999, 10)
    r = client.post("/payments/webhook/razorpay", content=body, headers=signed("legacy-webhook-secret", body))
    assert r.status_code == 404  # signature accepted (business 1's env secret), order doesn't exist


def test_invoice(client, fake_ai):
    _, h = new_seller(client, name="Meena Masala", wa_phone_number_id="PN_INV", wa_token="t",
                      gstin="33ABCDE1234F1Z5", address="4 Market Rd")
    product = add_product(client, h, name="<script>alert(1)</script>")
    order_id = place_whatsapp_order(client, fake_ai, "PN_INV", "919000000014", product["id"])
    order = [o for o in client.get("/orders", headers=h).json() if o["id"] == order_id][0]
    page = client.get(order["invoice_path"])
    assert page.status_code == 200 and "Meena Masala" in page.text and "33ABCDE1234F1Z5" in page.text
    assert "<script>alert(1)</script>" not in page.text  # escaped
    assert client.get(f"/invoices/{order_id}?t=guess").status_code == 404


def test_status_updates_and_24h_window(client, fake_ai, fake_whatsapp):
    _, h = new_seller(client, wa_phone_number_id="PN_SHIP", wa_token="t")
    product = add_product(client, h)
    order_id = place_whatsapp_order(client, fake_ai, "PN_SHIP", "919000000015", product["id"])

    r = client.patch(f"/orders/{order_id}/status", json={"status": "Shipped", "tracking_info": "DTDC 1"}, headers=h).json()
    assert r["customer_notified"] and fake_whatsapp.sent[-1][2] == "text" and "DTDC 1" in fake_whatsapp.sent[-1][3]

    with SessionLocal() as db:
        convo = db.query(models.Conversation).filter_by(phone="919000000015").first()
        convo.last_customer_message_at = datetime.now(timezone.utc) - timedelta(hours=30)
        db.commit()
    r = client.patch(f"/orders/{order_id}/status", json={"status": "Delivered"}, headers=h).json()
    assert r["customer_notified"] and fake_whatsapp.sent[-1][2] == "template"
    assert not client.get("/conversations", headers=h).json()[0]["reply_window_open"]

    sent = len(fake_whatsapp.sent)
    client.patch(f"/orders/{order_id}/status", json={"status": "Delivered"}, headers=h)
    client.patch(f"/orders/{order_id}/status", json={"status": "Cancelled", "notify_customer": False}, headers=h)
    assert len(fake_whatsapp.sent) == sent


# ---- products ------------------------------------------------------------------------------

def test_deleting_ordered_product_keeps_history(client, fake_ai):
    _, h = new_seller(client, wa_phone_number_id="PN_DEL", wa_token="t")
    sold = add_product(client, h, "Sold item")
    unsold = add_product(client, h, "Never sold")
    order_id = place_whatsapp_order(client, fake_ai, "PN_DEL", "919000000016", sold["id"])

    assert client.delete(f"/products/{sold['id']}", headers=h).status_code == 200
    assert client.delete(f"/products/{unsold['id']}", headers=h).status_code == 200
    assert client.get("/products", headers=h).json() == []
    order = [o for o in client.get("/orders", headers=h).json() if o["id"] == order_id][0]
    assert order["items"][0]["product_name"] == "Sold item"  # past order still readable

    whatsapp_message(client, "PN_DEL", "919000000016", text("menu?"))
    assert fake_ai.calls[-1]["products"] == []  # hidden from the AI's catalog


def test_owner_business_still_works_with_password(client):
    h = owner_headers(client)
    assert client.get("/business", headers=h).json()["id"] == 1


# ---- payment reminders ---------------------------------------------------------------------

def test_payment_reminder_sent_once(client, fake_ai, fake_whatsapp):
    import asyncio
    import reminders
    _, h = new_seller(client, wa_phone_number_id="PN_REMIND", wa_token="t")
    product = add_product(client, h)
    order_id = place_whatsapp_order(client, fake_ai, "PN_REMIND", "919000000020", product["id"])
    with SessionLocal() as db:
        db.get(models.Order, order_id).payment_link_url = "https://rzp.io/test"
        db.commit()

    now = datetime.now(timezone.utc)
    assert asyncio.run(reminders.send_due_reminders(now)) == 0  # too early
    later = now + timedelta(hours=3)
    sent_before = len(fake_whatsapp.sent)
    asyncio.run(reminders.send_due_reminders(later))
    mine = [m for m in fake_whatsapp.sent[sent_before:] if m[1] == "919000000020"]
    assert len(mine) == 1 and "https://rzp.io/test" in mine[0][3]
    asyncio.run(reminders.send_due_reminders(later + timedelta(hours=1)))
    assert len([m for m in fake_whatsapp.sent if m[1] == "919000000020" and "reminder" in m[3].lower()]) == 1


# ---- stats & customers ---------------------------------------------------------------------

def test_stats_and_customers(client, fake_ai):
    business_id, h = new_seller(client, wa_phone_number_id="PN_STATS", wa_token="t", razorpay_webhook_secret="s")
    product = add_product(client, h, name="Ghee", price=200, stock=3)
    paid_id = place_whatsapp_order(client, fake_ai, "PN_STATS", "919000000021", product["id"], qty=1)
    place_whatsapp_order(client, fake_ai, "PN_STATS", "919000000021", product["id"], qty=1)
    body = razorpay_event(paid_id, 200)
    client.post(f"/payments/webhook/razorpay/{business_id}", content=body, headers=signed("s", body))

    stats = client.get("/stats", headers=h).json()
    assert stats["today"] == {"orders": 2, "revenue": 200}
    assert stats["unpaid"] == {"orders": 1, "amount": 200}
    assert stats["to_ship"] == 1 and stats["low_stock"] == 1
    assert stats["top_products"][0] == {"name": "Ghee", "quantity": 1, "revenue": 200}
    assert len(stats["daily"]) == 14 and stats["daily"][-1]["revenue"] == 200

    customers = client.get("/customers", headers=h).json()
    assert len(customers) == 1
    assert customers[0]["orders"] == 2 and customers[0]["total_spent"] == 200 and customers[0]["name"] == "Ravi"


# ---- staff ---------------------------------------------------------------------------------

def test_staff_permissions(client):
    _, owner = new_seller(client)
    r = client.post("/business/staff", json={"name": "Kumar", "email": "kumar@shop.in", "password": "helper123"}, headers=owner)
    assert r.status_code == 200
    staff_id = r.json()["id"]
    assert client.post("/business/staff", json={"name": "Dup", "email": "KUMAR@shop.in", "password": "helper123"},
                       headers=owner).status_code == 409

    login = client.post("/auth/login", json={"email": "kumar@shop.in", "password": "helper123"})
    assert login.status_code == 200
    staff = {"Authorization": f"Bearer {login.json()['token']}"}
    assert client.get("/auth/me", headers=staff).json()["role"] == "staff"
    assert client.get("/orders", headers=staff).status_code == 200
    assert client.get("/business", headers=staff).status_code == 200
    assert client.patch("/business", json={"name": "Hacked"}, headers=staff).status_code == 403
    assert client.get("/business/staff", headers=staff).status_code == 403

    client.delete(f"/business/staff/{staff_id}", headers=owner)
    assert client.get("/orders", headers=staff).status_code == 401  # removed staff lose access at once
