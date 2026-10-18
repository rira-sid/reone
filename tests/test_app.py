import asyncio
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


def business_id_for(pnid):
    with SessionLocal() as db:
        return db.query(models.Business).filter_by(wa_phone_number_id=pnid).first().id


def pick(code):
    return {"type": "interactive", "interactive": {"type": "list_reply", "list_reply": {"id": f"lang:{code}", "title": code}}}


def start_chat(client, pnid, phone, lang="en"):
    """A customer who has chatted before in `lang` (there's no language menu any more)."""
    with SessionLocal() as db:
        business = db.query(models.Business).filter(models.Business.wa_phone_number_id == pnid).first()
        c = db.query(models.Conversation).filter(models.Conversation.business_id == business.id,
                                                 models.Conversation.phone == phone).first()
        if not c:
            c = models.Conversation(business_id=business.id, phone=phone)
            db.add(c)
        c.preferred_language = lang
        c.last_processed_message_id = 0
        c.last_customer_message_at = datetime.now(timezone.utc)  # inside WhatsApp's 24h window
        db.commit()


def shop_checkout(client, business_id, phone, items, **extra):
    from chatbot import customer_token
    body = {"items": items, "name": "Ravi", "address": "12 Gandhi St, Chennai 600001",
            "c": customer_token(business_id, phone), **extra}
    return client.post(f"/shop/{business_id}/checkout", json=body)


def place_whatsapp_order(client, fake_ai, pnid, phone, product_id, qty=2, **extra):
    """Customer chats on WhatsApp, taps Shop now and checks out; returns the new order id."""
    business_id = business_id_for(pnid)
    with SessionLocal() as db:
        chatting = db.query(models.Conversation).filter_by(business_id=business_id, phone=phone).first()
    if not chatting or not chatting.preferred_language:
        start_chat(client, pnid, phone)
    r = shop_checkout(client, business_id, phone, [{"product_id": product_id, "quantity": qty}], **extra)
    assert r.status_code == 200, r.text
    return r.json()["order_id"]


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

def test_hi_gets_a_human_greeting_without_menu_or_link(client, fake_ai, fake_whatsapp):
    business_id, h = new_seller(client, name="Anu Bakes", wa_phone_number_id="PN_ANU", wa_token="t",
                                welcome_message="Fresh every morning!")
    fake_ai.queue = [fake_ai.turn(intent="greeting", reply="Hi! Welcome to Anu Bakes 😊 How can I help you today?",
                                  language_code="en")]
    whatsapp_message(client, "PN_ANU", "919000000101", text("hello"))
    assert fake_whatsapp.sent[-1][1:] == ("919000000101", "text", "Hi! Welcome to Anu Bakes 😊 How can I help you today?")
    assert not [p for p in fake_whatsapp.payloads if p.get("type") == "interactive"]  # no language menu, no link
    call = fake_ai.calls[-1]
    assert call["welcome_note"] == "Fresh every morning!" and "[NEW]" in call["transcript"]
    # A customer who asks to order gets the link, in the language they're writing in.
    fake_ai.queue = [fake_ai.turn(intent="shop", reply="இதோ எங்கள் கடை 😊", language_code="ta")]
    whatsapp_message(client, "PN_ANU", "919000000101", text("order pannanum"))
    url = fake_whatsapp.payloads[-1]["interactive"]["action"]["parameters"]["url"]
    assert "/shop/anu-bakes" in url and "?c=919000000101." in url and "lang=ta" in url
    # A price question is answered without a link.
    fake_ai.queue = [fake_ai.turn(intent="shop", reply="Cupcakes are ₹40 each 😊", share_link=False)]
    whatsapp_message(client, "PN_ANU", "919000000101", text("cupcake price?"))
    assert fake_whatsapp.sent[-1][2:] == ("text", "Cupcakes are ₹40 each 😊")


def test_wants_to_buy_gets_shop_link_with_cart(client, fake_ai, fake_whatsapp):
    _, h = new_seller(client, name="Anu Bakes", wa_phone_number_id="PN_BUY", wa_token="t")
    cupcake = add_product(client, h, "Cupcake")
    start_chat(client, "PN_BUY", "919000000102")
    fake_ai.queue = [fake_ai.turn(intent="shop", cart=[ai.CartItem(product_id=cupcake["id"], quantity=2),
                                                       ai.CartItem(product_id=99999, quantity=1)])]
    whatsapp_message(client, "PN_BUY", "919000000102", text("2 cupcakes venum"))
    call = fake_ai.calls[-1]
    assert call["shop"] == "Anu Bakes" and call["products"] == ["Cupcake"] and call["language"] == "English"
    url = fake_whatsapp.payloads[-1]["interactive"]["action"]["parameters"]["url"]
    assert f"add={cupcake['id']}%3A2" in url and "99999" not in url  # unknown products dropped


def test_legacy_number_goes_to_business_one(client, fake_whatsapp):
    whatsapp_message(client, "ENV_PNID", "919000000103", text("hi"))
    assert fake_whatsapp.sent[-1][0] == "ENV_PNID"


def test_unknown_number_ignored(client, fake_ai, fake_whatsapp):
    whatsapp_message(client, "NOBODY", "919000000104", text("hi"))
    assert fake_ai.calls == [] and fake_whatsapp.sent == []


def test_retried_delivery_answered_once(client, fake_ai, fake_whatsapp):
    new_seller(client, wa_phone_number_id="PN_RETRY", wa_token="t")
    whatsapp_message(client, "PN_RETRY", "919000000105", text("hi"), message_id="wamid.same")
    import main
    main._seen_message_id_set.clear()  # simulate a restart wiping the in-memory dedupe
    main._SEEN_MESSAGE_IDS.clear()
    whatsapp_message(client, "PN_RETRY", "919000000105", text("hi"), message_id="wamid.same")
    assert len(fake_whatsapp.sent) == 1


def test_photo_and_voice_note_understood_reply_is_text(client, fake_ai, fake_whatsapp):
    new_seller(client, wa_phone_number_id="PN_MEDIA", wa_token="t")
    start_chat(client, "PN_MEDIA", "919000000106", lang="ta")
    whatsapp_message(client, "PN_MEDIA", "919000000106", {"type": "image", "image": {"id": "M1", "caption": "this"}})
    assert fake_ai.calls[-1]["image"] == (b"\xff\xd8fake-jpeg", "image/jpeg") and fake_ai.calls[-1]["language"] == "Tamil"
    fake_ai.queue = [fake_ai.turn(intent="shop", heard_text="rendu chicken masala venum")]
    whatsapp_message(client, "PN_MEDIA", "919000000106", {"type": "audio", "audio": {"id": "AUDIO1", "voice": True}})
    assert fake_ai.calls[-1]["audio"] == (b"OggSfake-voice", "audio/ogg; codecs=opus")
    assert all(s[2] != "audio" for s in fake_whatsapp.sent)  # never answers with a voice note
    with SessionLocal() as db:
        texts = [m.text for m in db.query(models.Message).order_by(models.Message.id.desc()).limit(3)]
    assert "🎤 rendu chicken masala venum" in texts


def test_order_status_answered_automatically(client, fake_ai, fake_whatsapp):
    _, h = new_seller(client, wa_phone_number_id="PN_STATUS", wa_token="t")
    product = add_product(client, h)
    start_chat(client, "PN_STATUS", "919000000107")
    fake_ai.queue = [fake_ai.turn(intent="order_status", reply="I can't find an order yet - can I help you place one?")]
    whatsapp_message(client, "PN_STATUS", "919000000107", text("where is my order"))
    assert fake_whatsapp.sent[-1][2:] == ("text", "I can't find an order yet - can I help you place one?")

    order_id = place_whatsapp_order(client, fake_ai, "PN_STATUS", "919000000107", product["id"])
    with SessionLocal() as db:
        db.get(models.Order, order_id).payment_link_url = "https://rzp.io/pay-me"
        db.commit()
    fake_ai.queue = [fake_ai.turn(intent="order_status", reply=f"Order #{order_id} is waiting for payment",
                                  payment_order_id=order_id)]
    whatsapp_message(client, "PN_STATUS", "919000000107", text("send payment link"))
    assert fake_ai.calls[-1]["orders"] == [order_id]
    assert f"waiting for payment (₹" in fake_ai.calls[-1]["open_items"]
    assert fake_whatsapp.sent[-1][3] == f"Order #{order_id} is waiting for payment\n\n💳 https://rzp.io/pay-me"


def test_greeting_and_split_messages(client, fake_ai, fake_whatsapp):
    new_seller(client, wa_phone_number_id="PN_HELLO", wa_token="t")
    start_chat(client, "PN_HELLO", "919000000108")
    fake_ai.queue = [fake_ai.turn(intent="greeting", reply="You're welcome! 😊")]
    whatsapp_message(client, "PN_HELLO", "919000000108", text("thanks"))
    assert fake_whatsapp.sent[-1][3] == "You're welcome! 😊"
    # Several messages sent before the bot answers are read together and answered once.
    import chatbot

    with SessionLocal() as db:
        convo = db.query(models.Conversation).filter_by(phone="919000000108").first()
        for line in ("Hi", "I need masala", "chicken masala 250g"):
            db.add(models.Message(conversation_id=convo.id, sender="customer", text=line))
        db.commit()
        convo_id = convo.id
    sent = len(fake_whatsapp.sent)
    fake_ai.queue = [fake_ai.turn(intent="shop", reply="Chicken Masala 250g is ₹230 😊", share_link=False)]
    asyncio.run(chatbot.respond(convo_id))
    assert len(fake_whatsapp.sent) == sent + 1 and len(fake_ai.calls[-1]["transcript"].split("[NEW]")) == 4
    asyncio.run(chatbot.respond(convo_id))  # nothing new -> no second reply
    assert len(fake_whatsapp.sent) == sent + 1
    assert chatbot.looks_unfinished("I need") and not chatbot.looks_unfinished("chicken masala 250g")


SELLER = "919800000001"


def open_seller_window(business_id):
    """The seller has messaged the shop number in the last 24h, so alerts can be free text."""
    with SessionLocal() as db:
        db.add(models.Conversation(business_id=business_id, phone=SELLER, preferred_language="en",
                                   last_customer_message_at=datetime.now(timezone.utc)))
        db.commit()


def test_question_goes_to_seller_who_replies_from_whatsapp(client, fake_ai, fake_whatsapp):
    business_id, h = new_seller(client, wa_phone_number_id="PN_ASK", wa_token="t", alert_phone="98000 00001")
    start_chat(client, "PN_ASK", "919000000109")
    fake_ai.queue = [fake_ai.turn(intent="question", reply="Good question! Let me check that for you 😊")]
    whatsapp_message(client, "PN_ASK", "919000000109", text("is it spicy?"))
    to_seller = [s for s in fake_whatsapp.sent if s[1] == SELLER]
    assert to_seller[-1][2:] == ("template", "seller_alert")  # seller hasn't written in 24h
    assert fake_whatsapp.sent[-1][3] == "Good question! Let me check that for you 😊"  # AI's natural holding line
    convo = client.get("/conversations", headers=h).json()[0]
    assert convo["needs_seller"] and not convo["overdue"]

    open_seller_window(business_id)
    sent = len(fake_whatsapp.sent)
    whatsapp_message(client, "PN_ASK", "919000000109", text("and is it vegan?"))
    new = fake_whatsapp.sent[sent:]
    alert = [s for s in new if s[1] == SELLER][-1]
    assert alert[2] == "text" and "added" in alert[3] and "and is it vegan?" in alert[3]  # same thread
    assert not [s for s in new if s[1] == "919000000109"]  # no second holding message
    alert_id = f"wamid.out.{fake_whatsapp.sent.index(alert) + 1}"

    # Seller swipe-replies to the alert on their own WhatsApp.
    whatsapp_message(client, "PN_ASK", SELLER, {**text("Mild, and yes vegan!"), "context": {"id": alert_id}})
    assert ("PN_ASK", "919000000109", "text", "Mild, and yes vegan!") in fake_whatsapp.sent
    assert fake_whatsapp.sent[-1][2:] == ("reaction", "✅")
    convo = [c for c in client.get("/conversations", headers=h).json() if c["phone"] == "919000000109"][0]
    assert not convo["needs_seller"]
    msgs = client.get(f"/conversations/{convo['id']}/messages", headers=h).json()
    assert msgs[-1]["sender"] == "seller" and msgs[-1]["text"] == "Mild, and yes vegan!"

    # For 30 minutes after the owner's reply the chat stays with the owner - the bot stays quiet.
    sent = len(fake_whatsapp.sent)
    calls = len(fake_ai.calls)
    whatsapp_message(client, "PN_ASK", "919000000109", text("thank you"))
    assert len(fake_ai.calls) == calls and not [s for s in fake_whatsapp.sent[sent:] if s[1] == "919000000109"]
    assert [s for s in fake_whatsapp.sent[sent:] if s[1] == SELLER]


def test_seller_message_without_swipe_is_treated_as_customer(client, fake_ai, fake_whatsapp):
    new_seller(client, wa_phone_number_id="PN_SELFTEST", wa_token="t", alert_phone=SELLER)
    fake_ai.queue = [fake_ai.turn(intent="greeting", reply="Hi! How can I help you today?")]
    whatsapp_message(client, "PN_SELFTEST", SELLER, text("hi"))  # seller testing their own shop
    assert fake_whatsapp.sent[-1][1:] == (SELLER, "text", "Hi! How can I help you today?")


def test_seller_hands_chat_back_with_bot_command(client, fake_ai, fake_whatsapp):
    business_id, h = new_seller(client, wa_phone_number_id="PN_BACK", wa_token="t", alert_phone=SELLER)
    open_seller_window(business_id)
    start_chat(client, "PN_BACK", "919000000110")
    whatsapp_message(client, "PN_BACK", "919000000110", text("custom cake?"))
    alert_no = max(i for i, s in enumerate(fake_whatsapp.sent) if s[1] == SELLER) + 1
    whatsapp_message(client, "PN_BACK", SELLER, {**text("Bot"), "context": {"id": f"wamid.out.{alert_no}"}})
    assert "assistant is back on" in fake_whatsapp.sent[-1][3]
    assert not [m for m in fake_whatsapp.sent if m[1] == "919000000110" and m[3] == "Bot"]  # never relayed
    convo = [c for c in client.get("/conversations", headers=h).json() if c["phone"] == "919000000110"][0]
    assert not convo["needs_seller"] and not convo["ai_paused"]


def test_unanswered_question_escalates(client, fake_ai, fake_whatsapp):
    import asyncio
    from seller_alerts import escalate_waiting
    business_id, h = new_seller(client, wa_phone_number_id="PN_WAIT", wa_token="t", alert_phone=SELLER)
    open_seller_window(business_id)
    start_chat(client, "PN_WAIT", "919000000111")
    whatsapp_message(client, "PN_WAIT", "919000000111", text("do you deliver to Madurai?"))
    now = datetime.now(timezone.utc)

    def run(minutes):
        before = len(fake_whatsapp.sent)
        with SessionLocal() as db:
            asyncio.run(escalate_waiting(db, now + timedelta(minutes=minutes)))
        return [s for s in fake_whatsapp.sent[before:] if s[0] == "PN_WAIT"]

    assert run(2) == []
    realert = run(6)
    assert len(realert) == 1 and realert[0][1] == SELLER and "waiting" in realert[0][3]
    told = run(16)
    assert len(told) == 1 and told[0][1] == "919000000111" and "busy" in told[0][3]
    assert run(30) == []  # nothing more after that


def test_ai_failure_goes_to_seller(client, fake_ai, fake_whatsapp, monkeypatch):
    _, h = new_seller(client, wa_phone_number_id="PN_FAIL", wa_token="t")
    start_chat(client, "PN_FAIL", "919000000112")

    async def broken(*args, **kwargs):
        raise RuntimeError("API down")

    monkeypatch.setattr(ai, "run_turn", broken)
    whatsapp_message(client, "PN_FAIL", "919000000112", text("hi"))
    assert "seller" in fake_whatsapp.sent[-1][3]
    assert client.get("/conversations", headers=h).json()[0]["needs_seller"]


def test_paused_chat_gets_no_bot_reply(client, fake_ai, fake_whatsapp):
    _, h = new_seller(client, wa_phone_number_id="PN_PAUSE", wa_token="t")
    start_chat(client, "PN_PAUSE", "919000000113")
    convo_id = client.get("/conversations", headers=h).json()[0]["id"]
    client.patch(f"/conversations/{convo_id}/takeover", json={"ai_paused": True}, headers=h)
    sent_before = len(fake_whatsapp.sent)
    whatsapp_message(client, "PN_PAUSE", "919000000113", text("hello?"))
    assert len(fake_whatsapp.sent) == sent_before


def test_closed_shop_says_so_in_chat(client, fake_ai, fake_whatsapp):
    new_seller(client, wa_phone_number_id="PN_SHUT", wa_token="t", accepting_orders=False,
               closed_message="On holiday till Monday!")
    start_chat(client, "PN_SHUT", "919000000114")
    fake_ai.queue = [fake_ai.turn(intent="shop")]
    whatsapp_message(client, "PN_SHUT", "919000000114", text("i want to order"))
    assert fake_whatsapp.sent[-1][3] == "On holiday till Monday!"
    assert "NOT accepting orders" in fake_ai.calls[-1]["policies"]


# ---- shop page & checkout ------------------------------------------------------------------

def test_catalog_builder_sizes_photos_nutrition(client):
    _, h = new_seller(client)
    p = client.post("/products", json={
        "name": "Chicken Masala", "category": "Masala", "description": "Stone ground",
        "nutrition": "Energy: 320 kcal\nProtein: 12 g",
        "variants": [{"label": "100g", "price": 40, "stock": 20}, {"label": "250g", "price": 90, "stock": 12}],
    }, headers=h).json()
    assert [v["label"] for v in p["variants"]] == ["100g", "250g"]
    assert p["price"] == 40 and p["stock"] == 32  # "from" price and total stock
    assert client.post("/products", json={"name": "X", "variants": [{"label": "S", "price": 1}, {"label": "s", "price": 2}]},
                       headers=h).status_code == 400

    import base64
    jpeg = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff fake jpeg").decode()
    for _ in range(8):
        p = client.post(f"/products/{p['id']}/images", json={"data": jpeg}, headers=h).json()
    assert len(p["images"]) == 8
    assert client.post(f"/products/{p['id']}/images", json={"data": jpeg}, headers=h).status_code == 400  # max 8
    assert client.post(f"/products/{p['id']}/images", json={"data": "data:text/html;base64,PGI+"},
                       headers=h).status_code == 400
    last = p["images"][-1]["id"]
    p = client.post(f"/products/{p['id']}/images/{last}/cover", json={}, headers=h).json()
    assert p["images"][0]["id"] == last
    img = client.get(p["images"][0]["url"].split(".com", 1)[-1])
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg" and img.content.startswith(b"\xff\xd8")
    p = client.delete(f"/products/{p['id']}/images/{last}", headers=h).json()
    assert len(p["images"]) == 7

    # Edit sizes: rename one, drop one, add one.
    v100 = p["variants"][0]
    p = client.patch(f"/products/{p['id']}", json={"variants": [
        {"id": v100["id"], "label": "100 g", "price": 45, "stock": 5}, {"label": "500 g", "price": 160, "stock": 3}]},
        headers=h).json()
    assert [(v["label"], v["price"]) for v in p["variants"]] == [("100 g", 45), ("500 g", 160)]
    assert p["variants"][0]["id"] == v100["id"]


def test_shop_checkout_with_sizes_end_to_end(client, fake_ai, fake_whatsapp):
    business_id, h = new_seller(client, wa_phone_number_id="PN_CART", wa_token="t", alert_phone=SELLER,
                                delivery_fee=30)
    open_seller_window(business_id)
    p = client.post("/products", json={"name": "Chicken Masala", "variants": [
        {"label": "100g", "price": 40, "stock": 20}, {"label": "250g", "price": 90, "stock": 1}]}, headers=h).json()
    v100, v250 = p["variants"]
    start_chat(client, "PN_CART", "919000000115")

    page = client.get(f"/shop/{business_id}?c=" + __import__("chatbot").customer_token(business_id, "919000000115")
                      + f"&add={p['id']}-{v250['id']}:1")
    assert page.status_code == 200 and "Chicken Masala" in page.text and '"token": "919000000115.' in page.text
    assert f'"prefill": [{{"product_id": {p["id"]}, "variant_id": {v250["id"]}, "quantity": 1}}]' in page.text

    # Size required when there are several, stock is checked per size.
    r = shop_checkout(client, business_id, "919000000115", [{"product_id": p["id"], "quantity": 1}])
    assert r.status_code == 400 and "size" in r.json()["detail"]
    r = shop_checkout(client, business_id, "919000000115", [{"product_id": p["id"], "variant_id": v250["id"], "quantity": 2}])
    assert r.status_code == 409 and "Only 1" in r.json()["detail"]

    r = shop_checkout(client, business_id, "919000000115", [
        {"product_id": p["id"], "variant_id": v100["id"], "quantity": 2},
        {"product_id": p["id"], "variant_id": v250["id"], "quantity": 1}], note="less spicy")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["total"] == 2 * 40 + 90 + 30 and out["redirect"] is None  # no payment method connected yet
    order = [o for o in client.get("/orders", headers=h).json() if o["id"] == out["order_id"]][0]
    assert sorted(i["product_name"] for i in order["items"]) == ["Chicken Masala (100g)", "Chicken Masala (250g)"]
    assert order["customer"]["name"] == "Ravi" and order["customer_note"] == "less spicy"
    summary = [s for s in fake_whatsapp.sent if s[1] == "919000000115"][-1][3]
    assert f"Order #{out['order_id']}" in summary and "Chicken Masala (250g)" in summary and "Total ₹200" in summary
    seller_msgs = [s[3] for s in fake_whatsapp.sent if s[1] == SELLER]
    assert any("New order" in m for m in seller_msgs) and any("no online payment" in m for m in seller_msgs)


def test_shop_checkout_from_public_link_needs_phone(client):
    business_id, h = new_seller(client, cod_enabled=True)
    product = add_product(client, h, price=100, stock=5)
    body = {"items": [{"product_id": product["id"], "quantity": 1}], "name": "Walk-in", "address": "1 Main Rd, Salem 636001",
            "payment_method": "cod"}
    assert client.post(f"/shop/{business_id}/checkout", json=body).status_code == 400
    r = client.post(f"/shop/{business_id}/checkout", json={**body, "phone": "98765 43210"})
    assert r.status_code == 200 and "cash" in r.json()["message"]
    assert client.get("/customers", headers=h).json()[0]["phone"] == "919876543210"
    # A forged token is ignored (falls back to the typed number).
    r = client.post(f"/shop/{business_id}/checkout", json={**body, "c": "919999999999.forged"})
    assert r.status_code == 400


def test_unpaid_orders_cancelled_after_a_day(client, fake_ai, fake_whatsapp):
    import asyncio
    import reminders
    _, h = new_seller(client, wa_phone_number_id="PN_STALE", wa_token="t")
    product = add_product(client, h)
    order_id = place_whatsapp_order(client, fake_ai, "PN_STALE", "919000000116", product["id"])
    now = datetime.now(timezone.utc)
    asyncio.run(reminders.cancel_stale_unpaid(now + timedelta(hours=2)))
    assert latest_order(client, h)["status"] == "Pending"
    asyncio.run(reminders.cancel_stale_unpaid(now + timedelta(hours=25)))
    order = latest_order(client, h)
    assert order["id"] == order_id and order["status"] == "Cancelled"
    assert "cancelled" in fake_whatsapp.sent[-1][3] or fake_whatsapp.sent[-1][2] == "template"


# ---- inbox ---------------------------------------------------------------------------------

def test_seller_reply_and_failure(client, fake_whatsapp):
    _, h = new_seller(client, wa_phone_number_id="PN_REPLY", wa_token="t")
    start_chat(client, "PN_REPLY", "919000000011")
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

def order_via_chat(client, fake_ai, pnid, phone, product_id, qty=1, payment_method="online", order_note=None):
    business_id = business_id_for(pnid)
    with SessionLocal() as db:
        chatting = db.query(models.Conversation).filter_by(business_id=business_id, phone=phone).first()
    if not chatting:
        start_chat(client, pnid, phone)
    return shop_checkout(client, business_id, phone, [{"product_id": product_id, "quantity": qty}],
                         payment_method=payment_method, note=order_note)


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
    assert "in cash" in fake_whatsapp.sent[-1][3] and "Pay here" not in fake_whatsapp.sent[-1][3]
    assert client.get("/products", headers=h).json()[0]["stock"] == 7  # reserved at once for COD

    r = client.patch(f"/orders/{order['id']}/payment", json={"is_paid": True}, headers=h).json()
    assert r["is_paid"] and r["customer_notified"]
    assert client.get("/products", headers=h).json()[0]["stock"] == 7  # not deducted twice

    client.patch(f"/orders/{order['id']}/status", json={"status": "Cancelled"}, headers=h)
    assert client.get("/products", headers=h).json()[0]["stock"] == 10  # cancelling puts stock back


def test_cod_ignored_when_disabled(client, fake_ai):
    _, h = new_seller(client, wa_phone_number_id="PN_NOCOD", wa_token="t")
    product = add_product(client, h)
    r = order_via_chat(client, fake_ai, "PN_NOCOD", "919000000031", product["id"], payment_method="cod")
    assert r.status_code == 400 and client.get("/orders", headers=h).json() == []


def test_delivery_fee_and_free_delivery(client, fake_ai, fake_whatsapp):
    _, h = new_seller(client, wa_phone_number_id="PN_FEE", wa_token="t", delivery_fee=40, free_delivery_above=500)
    product = add_product(client, h, price=100, stock=50)
    order_via_chat(client, fake_ai, "PN_FEE", "919000000032", product["id"], qty=2)
    order = latest_order(client, h)
    assert order["delivery_fee"] == 40 and order["total_amount"] == 240
    assert "Delivery ₹40" in fake_whatsapp.sent[-1][3]
    order_via_chat(client, fake_ai, "PN_FEE", "919000000032", product["id"], qty=5)
    order = latest_order(client, h)
    assert order["delivery_fee"] == 0 and order["total_amount"] == 500
    whatsapp_message(client, "PN_FEE", "919000000032", text("delivery charge?"))
    assert "Delivery fee: ₹40 (free delivery for orders of ₹500 or more)" in fake_ai.calls[-1]["policies"]


def test_minimum_order_enforced(client, fake_ai, fake_whatsapp):
    _, h = new_seller(client, wa_phone_number_id="PN_MIN", wa_token="t", min_order_amount=300)
    product = add_product(client, h, price=100)
    r = order_via_chat(client, fake_ai, "PN_MIN", "919000000033", product["id"], qty=1)
    assert r.status_code == 400 and "Minimum order is ₹300" in r.json()["detail"]
    assert client.get("/orders", headers=h).json() == []


def test_closed_shop_takes_no_orders(client, fake_ai, fake_whatsapp):
    _, h = new_seller(client, wa_phone_number_id="PN_SHUT2", wa_token="t", accepting_orders=False,
                      closed_message="On holiday till Monday!")
    product = add_product(client, h)
    r = order_via_chat(client, fake_ai, "PN_SHUT2", "919000000034", product["id"])
    assert r.status_code == 400 and r.json()["detail"] == "On holiday till Monday!"
    assert client.get("/orders", headers=h).json() == []


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
                                   "description": "Stone ground </script><b>x</b>"}, headers=h)
    page = client.get(f"/shop/{business_id}")
    assert page.status_code == 200
    assert "<title>Spice &lt;Box&gt;</title>" in page.text and "Stone ground" in page.text
    assert "</script><b>" not in page.text  # data can't break out of the JSON script tag
    assert '"whatsapp": "https://wa.me/919840012345"' in page.text and '"cod": true' in page.text
    assert '"token": null' in page.text  # public link: no customer attached
    ta = client.get(f"/shop/{business_id}?lang=ta").text
    assert 'lang="ta"' in ta and "சேர்" in ta
    assert 'dir="rtl"' in client.get(f"/shop/{business_id}?lang=ar").text
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

    assert f"Order #{order_id}" in fake_whatsapp.sent[-2][3]
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


def test_more_options_opens_razorpay_link_not_the_pay_page(client, fake_ai, fake_whatsapp, monkeypatch):
    import routers.pay as pay
    import routers.payments as payments

    class FakeLinks:
        def create(self, data):
            return {"id": "plink_1", "short_url": "https://rzp.io/l/real-link"}

    class FakeClient(FakeRazorpay):
        payment_link = FakeLinks()

    fake = FakeClient()
    monkeypatch.setattr(pay, "_razorpay_client", lambda business: fake)
    monkeypatch.setattr(payments, "_client", lambda business: fake)
    _, h = new_seller(client, wa_phone_number_id="PN_MORE", wa_token="t", razorpay_key_id="rzp_test_1",
                      razorpay_key_secret="secret", razorpay_webhook_secret="hook-rzp")
    product = add_product(client, h, "Pepper", price=80, stock=5)
    order_id = place_whatsapp_order(client, fake_ai, "PN_MORE", "919000000077", product["id"], qty=1)
    with SessionLocal() as db:
        pay_page = db.get(models.Order, order_id).payment_link_url

    page = client.get(pay_page.split("onrender.com", 1)[-1] if "onrender.com" in pay_page else pay_page).text
    assert "href='https://rzp.io/l/real-link'" in page
    with SessionLocal() as db:
        assert db.get(models.Order, order_id).payment_link_url == pay_page  # customer's link unchanged


def test_order_paid_from_a_payment_link_is_acknowledged(client):
    business_id, _ = new_seller(client, razorpay_key_id="rzp_test_1", razorpay_key_secret="s",
                                razorpay_webhook_secret="hook-x")
    body = json.dumps({"event": "order.paid", "payload": {
        "order": {"entity": {"id": "order_made_by_payment_link"}},
        "payment": {"entity": {"id": "pay_x", "amount": 10000}},
    }}).encode()
    r = client.post(f"/payments/webhook/razorpay/{business_id}", content=body, headers=signed("hook-x", body))
    assert r.status_code == 200 and r.json()["status"] == "ignored"


def test_shop_answer_is_the_ais_reply_in_the_customers_language(client, fake_ai, fake_whatsapp):
    _, h = new_seller(client, wa_phone_number_id="PN_LANG", wa_token="t")
    mutton = add_product(client, h, "Mutton Masala", price=120)
    start_chat(client, "PN_LANG", "919000000201", lang="en")
    fake_ai.queue = [fake_ai.turn(intent="shop", language_code="ta", cart=[ai.CartItem(product_id=mutton["id"], quantity=2)],
                                  reply="ஆம், மட்டன் மசாலா உள்ளது - ₹120.")]
    whatsapp_message(client, "PN_LANG", "919000000201", text("மட்டன் மசாலா ரெண்டு பாக்கெட் இருக்கா?"))
    button = fake_whatsapp.payloads[-1]["interactive"]
    assert button["body"]["text"] == "ஆம், மட்டன் மசாலா உள்ளது - ₹120."
    assert button["action"]["parameters"]["display_text"] == "இப்போதே வாங்க"  # switched to Tamil
    assert "lang=ta" in button["action"]["parameters"]["url"]

    whatsapp_message(client, "PN_LANG", "919000000201", text("preservatives irukka?"))  # fake AI: question
    assert "விற்பனையாளருக்கு" in fake_whatsapp.sent[-1][3]  # "passed to the seller" in Tamil


def test_bare_bot_command_from_seller_hands_back_latest_chat(client, fake_ai, fake_whatsapp):
    business_id, h = new_seller(client, wa_phone_number_id="PN_BARE", wa_token="t", alert_phone=SELLER)
    open_seller_window(business_id)
    start_chat(client, "PN_BARE", "919000000202")
    whatsapp_message(client, "PN_BARE", "919000000202", text("custom order?"))
    whatsapp_message(client, "PN_BARE", SELLER, text("bot."))  # no swipe
    assert "assistant is back on" in fake_whatsapp.sent[-1][3]
    assert not [m for m in fake_whatsapp.sent if m[1] == "919000000202" and m[3] == "bot."]
    convo = [c for c in client.get("/conversations", headers=h).json() if c["phone"] == "919000000202"][0]
    assert not convo["needs_seller"]


# ---- founder console, branding, funnel -----------------------------------------------------

def founder_headers(client):
    """Create the founder account once (needs DASHBOARD_PASSWORD as setup key), then log in."""
    if client.get("/founder/status").json()["setup_needed"]:
        body = {"email": "founder@rira.one", "password": "short", "setup_key": "owner-pass"}
        assert client.post("/founder/setup", json=body).status_code == 400  # password too short
        assert client.post("/founder/setup", json={**body, "password": "a-long-founder-pass",
                                                   "setup_key": "wrong"}).status_code == 403
        assert client.post("/founder/setup", json={**body, "password": "a-long-founder-pass"}).status_code == 200
    r = client.post("/founder/login", json={"email": "Founder@rira.one", "password": "a-long-founder-pass"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_founder_login_is_separate_from_shop_logins(client):
    founder_h = founder_headers(client)
    assert client.get("/founder/status").json()["setup_needed"] is False
    assert client.post("/founder/setup", json={"email": "x@y.z", "password": "another-long-pass",
                                               "setup_key": "owner-pass"}).status_code == 403  # only once
    assert client.post("/founder/login", json={"email": "founder@rira.one", "password": "nope"}).status_code == 401
    # Shop logins - even business 1's owner - can't use the founder console...
    assert client.get("/admin/overview", headers=owner_headers(client)).status_code == 401
    _, seller_h = new_seller(client)
    assert client.get("/admin/overview", headers=seller_h).status_code == 401
    # ...and a founder token isn't a shop login.
    assert client.get("/orders", headers=founder_h).status_code == 401
    assert client.get("/founder/me", headers=founder_h).json()["email"] == "founder@rira.one"


def test_founder_console_onboards_and_tracks_businesses(client):
    founder_h = founder_headers(client)

    r = client.post("/admin/businesses", json={"name": "Anu Bakes", "email": "Anu@Bakes.in"}, headers=founder_h)
    assert r.status_code == 200, r.text
    created = r.json()
    assert created["login_email"] == "anu@bakes.in" and len(created["password"]) >= 8
    assert client.post("/admin/businesses", json={"name": "Dup", "email": "anu@bakes.in"}, headers=founder_h).status_code == 409
    bid = created["business"]["id"]
    login = client.post("/auth/login", json={"email": "anu@bakes.in", "password": created["password"]})
    assert login.status_code == 200

    # Shop funnel is tracked per business.
    product = add_product(client, {"Authorization": f"Bearer {login.json()['token']}"}, "Cupcake", price=50)
    client.get(f"/shop/{bid}")
    client.get(f"/shop/{bid}")
    client.post(f"/shop/{bid}/event", content="add_to_cart")
    client.post(f"/shop/{bid}/event", content="checkout_open")
    client.post(f"/shop/{bid}/event", content="nonsense")
    client.post(f"/shop/{bid}/checkout", json={"items": [{"product_id": product["id"], "quantity": 1}], "name": "Ravi",
                                               "address": "1 Main Road, Salem", "phone": "9876500000"})
    row = [b for b in client.get("/admin/overview", headers=founder_h).json()["businesses"] if b["id"] == bid][0]
    assert row["funnel_30d"] == {"views": 2, "carts": 1, "checkouts": 1, "orders": 1, "paid": 0}
    assert row["orders_30d"] == 1 and row["products"] == 1 and row["setup"]["products"]
    assert not row["setup"]["whatsapp"]

    # Founder opens the seller's dashboard for support.
    support = client.post(f"/admin/businesses/{bid}/open", headers=founder_h).json()
    assert client.get("/business", headers={"Authorization": f"Bearer {support['token']}"}).json()["id"] == bid

    # Pausing stops the shop and the login.
    assert client.patch(f"/admin/businesses/{bid}", json={"is_active": False}, headers=founder_h).json()["is_active"] is False
    assert client.get(f"/shop/{bid}").status_code == 503
    assert client.post("/auth/login", json={"email": "anu@bakes.in", "password": created["password"]}).status_code == 403
    assert client.patch("/admin/businesses/1", json={"is_active": False}, headers=founder_h).status_code == 400
    client.patch(f"/admin/businesses/{bid}", json={"is_active": True}, headers=founder_h)
    assert client.get(f"/shop/{bid}").status_code == 200


def test_shop_branding(client):
    import base64
    business_id, h = new_seller(client, name="Anu Bakes")
    assert client.patch("/business", json={"brand_color": "red"}, headers=h).status_code == 400
    out = client.patch("/business", json={"brand_color": "#E11D48", "tagline": "Fresh every morning",
                                          "about": "Home bakery since 2019"}, headers=h).json()
    assert out["brand_color"] == "#E11D48" and out["tagline"] == "Fresh every morning"
    png = "data:image/png;base64," + base64.b64encode(b"\x89PNG fake").decode()
    out = client.post("/business/images/logo", json={"data": png}, headers=h).json()
    assert out["logo_url"].endswith(f"/shop/media/{out['logo_url'].rsplit('/', 1)[1]}")
    logo = client.get("/shop/media/" + out["logo_url"].rsplit("/", 1)[1])
    assert logo.status_code == 200 and logo.headers["content-type"] == "image/png"
    page = client.get(f"/shop/{business_id}").text
    # Every shop uses the shared studio palette now; the brand colour setting no longer recolours it.
    assert "--violet:#6e2c7a" in page and "#E11D48" not in page and "Fresh every morning" in page and "Home bakery since 2019" in page
    assert client.delete("/business/images/logo", headers=h).json()["logo_url"] is None
    # A very light brand colour still gives readable (darker) text.
    client.patch("/business", json={"brand_color": "#FDE68A"}, headers=h)
    assert "--violet:#FDE68A" not in client.get(f"/shop/{business_id}").text


def test_voice_note_never_guessed(client, fake_ai, fake_whatsapp, monkeypatch):
    new_seller(client, wa_phone_number_id="PN_VOICE2", wa_token="t")
    start_chat(client, "PN_VOICE2", "919000000203", lang="ta")
    fake_ai.queue = [fake_ai.turn(intent="unclear")]
    whatsapp_message(client, "PN_VOICE2", "919000000203", {"type": "audio", "audio": {"id": "AUDIO9"}})
    assert "வாய்ஸ் நோட்" in fake_whatsapp.sent[-1][3]

    async def busy(*args, **kwargs):
        raise ai.VoiceNotHeard("busy")
    monkeypatch.setattr(ai, "run_turn", busy)
    whatsapp_message(client, "PN_VOICE2", "919000000203", {"type": "audio", "audio": {"id": "AUDIO10"}})
    assert "வாய்ஸ் நோட்" in fake_whatsapp.sent[-1][3]  # asked to resend, not a random answer


def _audio_attempts(monkeypatch, main, fallback):
    import asyncio
    from google.genai import errors
    tried = []

    class Models:
        async def generate_content(self, model, contents, config):
            tried.append(model)
            raise errors.APIError(503, {"error": {"message": "busy"}})

    fake = type("C", (), {"aio": type("A", (), {"models": Models()})()})()
    monkeypatch.setattr(ai, "_gemini_client", lambda: fake)
    monkeypatch.setattr(ai, "GEMINI_AUDIO_RETRY_DELAYS", (0, 0))
    monkeypatch.setattr(ai, "GEMINI_MODEL", main)
    monkeypatch.setattr(ai, "GEMINI_FALLBACK_MODEL", fallback)
    raised = False
    try:
        asyncio.run(ai._run_gemini("p", None, (b"ogg", "audio/ogg")))
    except ai.VoiceNotHeard:
        raised = True
    return raised, tried


def test_audio_never_uses_fallback_model(monkeypatch):
    # Paid setup: full model first, lite fallback - voice never goes to the lite model.
    raised, tried = _audio_attempts(monkeypatch, "gemini-flash-latest", "gemini-flash-lite-latest")
    assert raised and tried == ["gemini-flash-latest"] * 3
    # Free setup: lite first, then the full model gets the last try.
    raised, tried = _audio_attempts(monkeypatch, "gemini-flash-lite-latest", "gemini-flash-latest")
    assert raised and tried == ["gemini-flash-lite-latest"] * 3 + ["gemini-flash-latest"]
