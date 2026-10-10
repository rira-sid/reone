import hashlib
import hmac
import itertools
import json
from datetime import datetime, timedelta, timezone

import ai
import models
import voice
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


def test_voice_note_heard_and_answered_by_voice(client, fake_ai, fake_whatsapp, fake_voice):
    new_seller(client, wa_phone_number_id="PN_VOICE", wa_token="t")
    fake_ai.queue = [fake_ai.turn(reply="Sure! How many?", heard_text="rendu chicken masala venum",
                                  spoken_reply="Sure, how many packets?")]
    whatsapp_message(client, "PN_VOICE", "919000000021", {"type": "audio", "audio": {"id": "AUDIO1", "voice": True}})
    assert fake_ai.calls[-1]["audio"] == (b"OggSfake-voice", "audio/ogg; codecs=opus")
    assert fake_whatsapp.sent[-2][2:] == ("text", "Sure! How many?")
    assert fake_whatsapp.sent[-1][2:] == ("audio", "MEDIA_REPLY")
    assert fake_voice == ["Sure, how many packets?"]
    with SessionLocal() as db:
        texts = [m.text for m in db.query(models.Message).order_by(models.Message.id.desc()).limit(3)]
    assert "🎤 rendu chicken masala venum" in texts and "🔊 Sure, how many packets?" in texts


def test_text_message_gets_no_voice_reply(client, fake_ai, fake_whatsapp, fake_voice):
    new_seller(client, wa_phone_number_id="PN_TEXTONLY", wa_token="t")
    fake_ai.queue = [fake_ai.turn(reply="Hi!", spoken_reply="Hi there!")]
    whatsapp_message(client, "PN_TEXTONLY", "919000000022", text("hello"))
    assert fake_whatsapp.sent[-1][2] == "text" and fake_voice == []


def test_order_confirmed_by_voice(client, fake_ai, fake_whatsapp, fake_voice):
    _, h = new_seller(client, wa_phone_number_id="PN_VOICEORDER", wa_token="t")
    product = add_product(client, h, "Pepper", price=90, stock=5)
    fake_ai.queue = [fake_ai.turn(cart=[ai.CartItem(product_id=product["id"], quantity=1)], customer_name="Ravi",
                                  delivery_address="12 Gandhi St, Chennai 600001", ready_to_place_order=True,
                                  reply="Thank you!", spoken_reply="Thank you Ravi, your order of 90 rupees is confirmed.")]
    whatsapp_message(client, "PN_VOICEORDER", "919000000023", text("yes confirm"))
    assert "Order #" in fake_whatsapp.sent[-2][3]
    assert fake_whatsapp.sent[-1][2] == "audio"
    assert fake_voice == ["Thank you Ravi, your order of 90 rupees is confirmed."]


def test_voice_failure_still_sends_text(client, fake_ai, fake_whatsapp, fake_voice, monkeypatch):
    async def broken(text):
        raise RuntimeError("tts down")
    monkeypatch.setattr(voice, "synthesize", broken)
    new_seller(client, wa_phone_number_id="PN_VOICEFAIL", wa_token="t")
    fake_ai.queue = [fake_ai.turn(reply="Got it", spoken_reply="Got it")]
    whatsapp_message(client, "PN_VOICEFAIL", "919000000024", {"type": "audio", "audio": {"id": "AUDIO2"}})
    assert fake_whatsapp.sent[-1][2:] == ("text", "Got it")


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


# ---- shop rules: COD, delivery fee, minimum order, closed shop -----------------------------

def order_via_chat(client, fake_ai, pnid, phone, product_id, qty=1, **turn):
    fake_ai.queue = [fake_ai.turn(cart=[ai.CartItem(product_id=product_id, quantity=qty)], customer_name="Ravi",
                                  delivery_address="12 Gandhi St", ready_to_place_order=True, reply="Thanks!", **turn)]
    whatsapp_message(client, pnid, phone, text("confirm"))


def latest_order(client, headers):
    return client.get("/orders", headers=headers).json()[0]


def test_cash_on_delivery(client, fake_ai, fake_whatsapp):
    _, h = new_seller(client, wa_phone_number_id="PN_COD", wa_token="t", cod_enabled=True)
    product = add_product(client, h, price=100, stock=10)
    order_via_chat(client, fake_ai, "PN_COD", "919000000030", product["id"], qty=3, payment_method="cod",
                   order_note="less spicy")
    order = latest_order(client, h)
    assert order["payment_method"] == "cod" and order["status"] == "Confirmed" and not order["is_paid"]
    assert order["customer_note"] == "less spicy"
    assert "cash on delivery" in fake_whatsapp.sent[-1][3] and "Pay here" not in fake_whatsapp.sent[-1][3]
    assert client.get("/products", headers=h).json()[0]["stock"] == 7  # reserved at once for COD

    r = client.patch(f"/orders/{order['id']}/payment", json={"is_paid": True}, headers=h).json()
    assert r["is_paid"] and r["customer_notified"]
    assert client.get("/products", headers=h).json()[0]["stock"] == 7  # not deducted twice

    client.patch(f"/orders/{order['id']}/status", json={"status": "Cancelled"}, headers=h)
    assert client.get("/products", headers=h).json()[0]["stock"] == 10  # cancelling puts stock back


def test_cod_ignored_when_disabled(client, fake_ai):
    _, h = new_seller(client, wa_phone_number_id="PN_NOCOD", wa_token="t")
    product = add_product(client, h)
    order_via_chat(client, fake_ai, "PN_NOCOD", "919000000031", product["id"], payment_method="cod")
    assert latest_order(client, h)["payment_method"] == "online"


def test_delivery_fee_and_free_delivery(client, fake_ai, fake_whatsapp):
    _, h = new_seller(client, wa_phone_number_id="PN_FEE", wa_token="t", delivery_fee=40, free_delivery_above=500)
    product = add_product(client, h, price=100, stock=50)
    order_via_chat(client, fake_ai, "PN_FEE", "919000000032", product["id"], qty=2)
    order = latest_order(client, h)
    assert order["delivery_fee"] == 40 and order["total_amount"] == 240
    assert "incl. ₹40 delivery" in fake_whatsapp.sent[-1][3]
    order_via_chat(client, fake_ai, "PN_FEE", "919000000032", product["id"], qty=5)
    order = latest_order(client, h)
    assert order["delivery_fee"] == 0 and order["total_amount"] == 500
    assert "Delivery fee: ₹40 (free delivery for orders of ₹500 or more)" in fake_ai.calls[-1]["policies"]


def test_minimum_order_keeps_cart(client, fake_ai, fake_whatsapp):
    _, h = new_seller(client, wa_phone_number_id="PN_MIN", wa_token="t", min_order_amount=300)
    product = add_product(client, h, price=100)
    order_via_chat(client, fake_ai, "PN_MIN", "919000000033", product["id"], qty=1)
    assert client.get("/orders", headers=h).json() == []
    assert "Minimum order is ₹300" in fake_whatsapp.sent[-1][3]
    with SessionLocal() as db:
        convo = db.query(models.Conversation).filter_by(phone="919000000033").first()
        assert json.loads(convo.cart_json) and convo.customer_name == "Ravi"  # nothing lost


def test_closed_shop_takes_no_orders(client, fake_ai, fake_whatsapp):
    _, h = new_seller(client, wa_phone_number_id="PN_SHUT", wa_token="t", accepting_orders=False,
                      closed_message="On holiday till Monday!")
    product = add_product(client, h)
    order_via_chat(client, fake_ai, "PN_SHUT", "919000000034", product["id"])
    assert client.get("/orders", headers=h).json() == []
    assert fake_whatsapp.sent[-1][3] == "On holiday till Monday!"
    assert "NOT accepting orders" in fake_ai.calls[-1]["policies"]


def test_manual_order_from_dashboard(client):
    _, h = new_seller(client, cod_enabled=True)
    product = add_product(client, h, price=150, stock=5)
    r = client.post("/orders", json={"customer": {"name": "Walk-in", "phone": "919000000035", "address": ""},
                                      "items": [{"product_id": product["id"], "quantity": 2}],
                                      "payment_method": "cod", "customer_note": "pickup"}, headers=h)
    assert r.status_code == 200 and r.json()["total_amount"] == 300 and r.json()["customer_note"] == "pickup"
    assert client.post("/orders", json={"customer": {"name": "X", "phone": "1"},
                                         "items": [{"product_id": product["id"], "quantity": 0}]},
                       headers=h).status_code == 400


def test_csv_export(client, fake_ai):
    _, h = new_seller(client, wa_phone_number_id="PN_CSV", wa_token="t")
    product = add_product(client, h, name="மசாலா", price=80)
    order_via_chat(client, fake_ai, "PN_CSV", "919000000036", product["id"], qty=2)
    r = client.get("/orders/export.csv", headers=h)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert r.text.startswith("﻿Order,") and "2 x மசாலா @ 80" in r.text
    assert client.get("/orders/export.csv").status_code == 401


def test_public_catalog(client):
    business_id, h = new_seller(client, name="Spice <Box>", phone="98400 12345", delivery_fee=30, cod_enabled=True)
    client.post("/products", json={"name": "Sambar Powder", "price": 120, "stock": 4, "category": "Powders",
                                   "description": "Stone ground"}, headers=h)
    client.post("/products", json={"name": "Rasam Powder", "price": 90, "stock": 0, "category": "Powders"}, headers=h)
    page = client.get(f"/shop/{business_id}")
    assert page.status_code == 200
    assert "Spice &lt;Box&gt;" in page.text and "Stone ground" in page.text and "Powders" in page.text
    assert "https://wa.me/919840012345?text=" in page.text and "Out of stock" in page.text
    assert "Delivery ₹30" in page.text and "Cash on delivery available" in page.text
    assert client.get("/shop/999999").status_code == 404


def test_shop_settings_round_trip(client):
    _, h = new_seller(client)
    out = client.patch("/business", json={"delivery_fee": 25, "free_delivery_above": 0, "min_order_amount": 200,
                                          "cod_enabled": True, "accepting_orders": False,
                                          "closed_message": "  Back soon  "}, headers=h).json()
    assert out["delivery_fee"] == 25 and out["free_delivery_above"] is None and out["min_order_amount"] == 200
    assert out["cod_enabled"] and not out["accepting_orders"] and out["closed_message"] == "Back soon"
    assert out["catalog_path"].startswith("/shop/")


# ---- payment methods: UPI page, WhatsApp Pay ----------------------------------------------

def test_payment_settings_validation(client):
    _, h = new_seller(client)
    assert client.patch("/business", json={"upi_id": "not a upi"}, headers=h).status_code == 400
    assert client.patch("/business", json={"payment_method": "upi"}, headers=h).status_code == 400  # no UPI ID yet
    assert client.patch("/business", json={"payment_method": "bitcoin"}, headers=h).status_code == 400
    r = client.patch("/business", json={"upi_id": "shop@okaxis", "payment_method": "upi"}, headers=h)
    assert r.status_code == 200 and r.json()["active_payment_method"] == "upi"


def test_upi_checkout_sends_pay_button_and_page(client, fake_ai, fake_whatsapp):
    _, h = new_seller(client, wa_phone_number_id="PN_UPI", wa_token="t", upi_id="masala@okaxis",
                      upi_name="Siddharth Masala", payment_method="upi")
    product = add_product(client, h, "Pepper", price=90, stock=5)
    order_id = place_whatsapp_order(client, fake_ai, "PN_UPI", "919000000031", product["id"], qty=2)

    assert "Tap the Pay button" in fake_whatsapp.sent[-2][3]
    assert fake_whatsapp.sent[-1][2:] == ("interactive", "cta_url")
    button = fake_whatsapp.payloads[-1]["interactive"]
    assert button["action"]["parameters"]["display_text"] == "Pay ₹180"
    assert button["header"]["image"]["link"].endswith("/static/pay/upi-banner.png")

    pay_path = button["action"]["parameters"]["url"].split(".com", 1)[1]
    page = client.get(pay_path)
    assert page.status_code == 200
    assert "tez://upi/pay?pa=masala%40okaxis&amp;pn=Siddharth%20Masala&amp;am=180.00" in page.text
    assert "phonepe://pay?" in page.text and "paytmmp://pay?" in page.text and "<svg" in page.text
    assert client.get(f"/pay/{order_id}?t=guess").status_code == 404

    # Seller confirms by hand; the page then says paid.
    assert client.patch(f"/orders/{order_id}/payment", json={"is_paid": True}, headers=h).status_code == 200
    assert "Paid" in client.get(pay_path).text


def test_banner_is_served(client):
    r = client.get("/static/pay/upi-banner.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"


def _wapay_status(reference_id, status="captured"):
    return {"type": "payment", "status": status, "payment": {"reference_id": reference_id}}


def test_whatsapp_pay_order_card_and_verified_confirmation(client, fake_ai, fake_whatsapp, monkeypatch):
    import routers.pay as pay
    business_id, h = new_seller(client, wa_phone_number_id="PN_WAPAY", wa_token="t", wa_payment_config="rira-rzp",
                                payment_method="whatsapp_pay")
    product = add_product(client, h, "Pepper", price=90, stock=5)
    order_id = place_whatsapp_order(client, fake_ai, "PN_WAPAY", "919000000032", product["id"], qty=2)

    card = fake_whatsapp.payloads[-1]["interactive"]
    assert card["type"] == "order_details"
    params = card["action"]["parameters"]
    reference = f"RIRA{business_id}-{order_id}"
    assert params["reference_id"] == reference and params["total_amount"] == {"value": 18000, "offset": 100}
    assert params["payment_settings"][0]["payment_gateway"]["configuration_name"] == "rira-rzp"
    assert params["order"]["items"][0] == {"retailer_id": str(product["id"]), "name": "Pepper",
                                           "amount": {"value": 9000, "offset": 100}, "quantity": 2}

    # A forged "captured" webhook is ignored when Meta's lookup says it isn't paid.
    async def lookup_pending(creds, config, ref):
        return {"reference_id": ref, "status": "pending"}
    monkeypatch.setattr(pay, "lookup_payment", lookup_pending)
    client.post("/webhook", json={"entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": "PN_WAPAY"}, "statuses": [_wapay_status(reference)]}}]}]})
    assert not [o for o in client.get("/orders", headers=h).json() if o["id"] == order_id][0]["is_paid"]

    async def lookup_captured(creds, config, ref):
        return {"reference_id": ref, "status": "captured", "amount": {"value": 18000, "offset": 100},
                "transactions": [{"status": "success", "pg_transaction_id": "pay_wa_1"}]}
    monkeypatch.setattr(pay, "lookup_payment", lookup_captured)
    client.post("/webhook", json={"entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": "PN_WAPAY"}, "statuses": [_wapay_status(reference)]}}]}]})
    order = [o for o in client.get("/orders", headers=h).json() if o["id"] == order_id][0]
    assert order["is_paid"] and order["status"] == "Confirmed"
    assert "Payment received" in fake_whatsapp.sent[-1][3]


def test_whatsapp_pay_amount_mismatch_not_marked_paid(client, fake_ai, fake_whatsapp, monkeypatch):
    import routers.pay as pay
    business_id, h = new_seller(client, wa_phone_number_id="PN_WAPAY2", wa_token="t", wa_payment_config="cfg",
                                payment_method="whatsapp_pay")
    product = add_product(client, h, "Pepper", price=90, stock=5)
    order_id = place_whatsapp_order(client, fake_ai, "PN_WAPAY2", "919000000033", product["id"], qty=1)

    async def lookup_short(creds, config, ref):
        return {"status": "captured", "amount": {"value": 100, "offset": 100}, "transactions": []}
    monkeypatch.setattr(pay, "lookup_payment", lookup_short)
    client.post("/webhook", json={"entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": "PN_WAPAY2"}, "statuses": [_wapay_status(f"RIRA{business_id}-{order_id}")]}}]}]})
    assert not [o for o in client.get("/orders", headers=h).json() if o["id"] == order_id][0]["is_paid"]


def test_failed_pay_button_falls_back_to_link(client, fake_ai, fake_whatsapp, monkeypatch):
    import routers.pay as pay
    _, h = new_seller(client, wa_phone_number_id="PN_UPI2", wa_token="t", upi_id="x@ybl", payment_method="upi")
    product = add_product(client, h, "Pepper", price=90, stock=5)

    async def broken(creds, to, interactive):
        raise pay.WhatsAppSendError("interactive not allowed")
    monkeypatch.setattr(pay, "send_interactive", broken)
    order_id = place_whatsapp_order(client, fake_ai, "PN_UPI2", "919000000034", product["id"], qty=1)
    assert fake_whatsapp.sent[-1][3].startswith("Pay here: https://") and f"/pay/{order_id}?t=" in fake_whatsapp.sent[-1][3]


# ---- Razorpay UPI app buttons (recommended, auto-confirmed) --------------------------------

class FakeRazorpay:
    """Stands in for razorpay.Client: creates orders, checks signatures against one known value."""

    def __init__(self):
        self.created = []
        self.order = self
        self.utility = self

    def create(self, data):
        self.created.append(data)
        return {"id": f"order_RZP{len(self.created)}"}

    def verify_payment_signature(self, params):
        import razorpay
        if params["razorpay_signature"] != "good-signature":
            raise razorpay.errors.SignatureVerificationError("bad signature")


def razorpay_seller(client, monkeypatch, pnid, **extra):
    import routers.pay as pay
    import routers.payments as payments
    fake = FakeRazorpay()
    monkeypatch.setattr(pay, "_razorpay_client", lambda business: fake)
    monkeypatch.setattr(payments, "ensure_payment_link", lambda db, business, order: "https://rzp.io/l/more")
    business_id, h = new_seller(client, wa_phone_number_id=pnid, wa_token="t", razorpay_key_id="rzp_test_1",
                                razorpay_key_secret="secret", razorpay_webhook_secret="hook-rzp", **extra)
    return business_id, h, fake


def test_manual_upi_never_picked_automatically(client):
    _, h = new_seller(client, upi_id="shop@okaxis")
    assert client.get("/business", headers=h).json()["active_payment_method"] is None


def test_razorpay_upi_buttons_amount_locked_and_signature_verified(client, fake_ai, fake_whatsapp, monkeypatch):
    _, h, rzp = razorpay_seller(client, monkeypatch, "PN_RZPUPI")
    product = add_product(client, h, "Pepper", price=229.5, stock=5)
    order_id = place_whatsapp_order(client, fake_ai, "PN_RZPUPI", "919000000041", product["id"], qty=2)

    assert client.get("/business", headers=h).json()["active_payment_method"] == "razorpay_link"
    button = fake_whatsapp.payloads[-1]["interactive"]
    assert button["type"] == "cta_url" and button["action"]["parameters"]["display_text"] == "Pay ₹459"
    assert button["footer"]["text"] == "Secure payment by Razorpay"

    pay_path = button["action"]["parameters"]["url"].split(".com", 1)[1]
    page = client.get(pay_path).text
    assert rzp.created == [{"amount": 45900, "currency": "INR", "receipt": f"order-{order_id}",
                            "notes": {"order_id": str(order_id), "business_id": str(rzp_bid(client, h))}}]
    for app in ("gpay", "phonepe", "paytm", "bhim"):
        assert f"data-app='{app}'" in page
    assert '"amount": 45900' in page and '"order_id": "order_RZP1"' in page and "https://rzp.io/l/more" in page
    client.get(pay_path)
    assert len(rzp.created) == 1  # reopening the page reuses the same Razorpay order

    confirm = pay_path.replace("?t=", "/confirm?t=")
    good = {"razorpay_payment_id": "pay_UPI1", "razorpay_order_id": "order_RZP1", "razorpay_signature": "good-signature"}
    assert client.post(confirm, json={**good, "razorpay_signature": "forged"}).status_code == 400
    assert client.post(confirm, json={**good, "razorpay_order_id": "order_OTHER"}).status_code == 400
    assert not [o for o in client.get("/orders", headers=h).json() if o["id"] == order_id][0]["is_paid"]

    assert client.post(confirm, json=good).json() == {"paid": True}
    order = [o for o in client.get("/orders", headers=h).json() if o["id"] == order_id][0]
    assert order["is_paid"] and "Payment received" in fake_whatsapp.sent[-1][3]
    assert "Paid" in client.get(pay_path).text


def rzp_bid(client, h):
    return client.get("/business", headers=h).json()["id"]


def test_razorpay_order_paid_webhook_confirms(client, fake_ai, fake_whatsapp, monkeypatch):
    business_id, h, rzp = razorpay_seller(client, monkeypatch, "PN_RZPHOOK")
    product = add_product(client, h, "Pepper", price=90, stock=5)
    order_id = place_whatsapp_order(client, fake_ai, "PN_RZPHOOK", "919000000042", product["id"], qty=1)
    pay_path = fake_whatsapp.payloads[-1]["interactive"]["action"]["parameters"]["url"].split(".com", 1)[1]
    client.get(pay_path)  # customer opens the page -> Razorpay order created

    body = json.dumps({"event": "order.paid", "payload": {
        "order": {"entity": {"id": "order_RZP1"}},
        "payment": {"entity": {"id": "pay_HOOK1", "amount": 9000}},
    }}).encode()
    url = f"/payments/webhook/razorpay/{business_id}"
    assert client.post(url, content=body, headers=signed("hook-rzp", body)).json() == {"status": "ok"}
    assert [o for o in client.get("/orders", headers=h).json() if o["id"] == order_id][0]["is_paid"]
    assert client.post(url, content=body, headers=signed("hook-rzp", body)).json() == {"status": "already processed"}
