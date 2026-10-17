"""Seller round: catalogue translations, insights, white-label brand."""
import asyncio
import json

import catalog_i18n
import models
from database import SessionLocal
from test_app import new_seller


def add_product(client, h, **fields):
    body = {"name": "Chicken Masala", "category": "Masala", "description": "Stone-ground.", "price": 50, "stock": 9,
            "nutrition": "Energy: 320 kcal", "variants": [{"label": "100g", "price": 40, "stock": 5},
                                                          {"label": "250g", "price": 90, "stock": 5}], **fields}
    r = client.post("/products", json=body, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def shop_data(client, key, lang):
    html = client.get(f"/shop/{key}?lang={lang}").text
    marker = '<script id="data" type="application/json">'
    start = html.index(marker) + len(marker)
    return json.loads(html[start:html.index("</script>", start)].replace("<\\/", "</"))


def test_products_are_translated_for_the_shop(client, fake_translator):
    bid, h = new_seller(client, name="Translate Shop")
    p = add_product(client, h)
    assert "Chicken Masala" in fake_translator  # translated right after saving
    data = client.get(f"/products/{p['id']}/translations", headers=h).json()
    ta = next(x for x in data["languages"] if x["lang"] == "ta")
    assert ta["name"] == "[ta] Chicken Masala" and ta["sizes"] == ["[ta] 100g", "[ta] 250g"] and not ta["outdated"]
    assert len(data["languages"]) == len(catalog_i18n.TARGETS)

    shop = shop_data(client, bid, "ta")["products"][0]
    assert shop["name"] == "[ta] Chicken Masala" and shop["alt"] == "Chicken Masala"
    assert shop["category"] == "[ta] Masala" and shop["variants"][0]["label"] == "[ta] 100g"
    assert shop_data(client, bid, "en")["products"][0]["name"] == "Chicken Masala"

    # Seller's own wording is kept, even when the English changes...
    r = client.put(f"/products/{p['id']}/translations/ta", headers=h,
                   json={"name": "சிக்கன் மசாலா", "description": "", "category": "மசாலா", "sizes": ["100 கி", "250 கி"]})
    assert r.status_code == 200 and r.json()["edited"]
    client.patch(f"/products/{p['id']}", json={"description": "Fresh stone-ground."}, headers=h)
    assert shop_data(client, bid, "ta")["products"][0]["name"] == "சிக்கன் மசாலா"
    hi = shop_data(client, bid, "hi")["products"][0]
    assert hi["description"] == "[hi] Fresh stone-ground."  # other languages follow the new English
    # ...until they ask to translate again.
    assert client.post(f"/products/{p['id']}/translations/refresh", headers=h).json()["languages"] == len(catalog_i18n.TARGETS)
    assert shop_data(client, bid, "ta")["products"][0]["name"] == "[ta] Chicken Masala"
    assert client.put(f"/products/{p['id']}/translations/xx", headers=h, json={"name": "x"}).status_code == 404
    _, other = new_seller(client)
    assert client.get(f"/products/{p['id']}/translations", headers=other).status_code == 404


def test_background_catch_up_translates_missing_products(client, fake_translator):
    bid, h = new_seller(client, name="Catch Up Shop")
    p = add_product(client, h, name="Pepper Powder")
    with SessionLocal() as db:
        db.query(models.ProductTranslation).filter(models.ProductTranslation.product_id == p["id"]).delete()
        db.commit()
        assert not catalog_i18n.is_current(db, db.get(models.Product, p["id"]))
    for _ in range(30):  # other test shops' products may be in the queue too
        with SessionLocal() as db:
            asyncio.run(catalog_i18n.catch_up(db))
            if catalog_i18n.is_current(db, db.get(models.Product, p["id"])):
                break
    assert shop_data(client, bid, "ml")["products"][0]["name"] == "[ml] Pepper Powder"


def test_insights(client):
    bid, h = new_seller(client, name="Insight Shop")
    p = add_product(client, h)
    from test_app import shop_checkout

    r = shop_checkout(client, bid, "919000000777", [{"product_id": p["id"], "variant_id": p["variants"][0]["id"], "quantity": 2}])
    assert r.status_code == 200, r.text
    data = client.get("/insights?days=30", headers=h).json()
    assert data["orders"] == 1 and data["customers"] == 1 and data["new_customers"] == 1
    assert sum(data["hours"]) == 1 and len(data["weekdays"]) == 7
    assert data["paid_rate"] == 0 and data["not_selling"] == ["Chicken Masala"]
    assert client.get("/insights?days=7", headers=h).json()["days"] == 7
    assert client.get("/insights").status_code == 401


def test_brand_for_white_label_login(client):
    bid, h = new_seller(client, name="Kavi Silks", brand_color="#16A34A", tagline="Pure silk")
    me = client.get("/auth/me", headers=h).json()
    assert me["slug"].startswith("kavi-silks") and me["brand_color"] == "#16A34A"
    b = client.get(f"/brand/{me['slug']}").json()
    assert b["name"] == "Kavi Silks" and b["brand_color"] == "#16A34A" and b["tagline"] == "Pure silk"
    assert client.get("/brand/no-such-shop").status_code == 404


def test_opening_a_chat_marks_it_read(client):
    from test_app import text, whatsapp_message

    bid, h = new_seller(client, name="Unread Shop", wa_phone_number_id="PN_UNREAD", wa_token="t")
    whatsapp_message(client, "PN_UNREAD", "919000000888", text("hi"))
    whatsapp_message(client, "PN_UNREAD", "919000000889", text("hello"))
    assert client.get("/stats", headers=h).json()["unread"] == 2
    chats = client.get("/conversations", headers=h).json()
    assert all(c["unread"] for c in chats)
    client.get(f"/conversations/{chats[0]['id']}/messages", headers=h)  # seller opens one chat
    assert client.get("/stats", headers=h).json()["unread"] == 1
    assert [c["unread"] for c in client.get("/conversations", headers=h).json() if c["id"] == chats[0]["id"]] == [False]
    whatsapp_message(client, "PN_UNREAD", chats[0]["phone"], text("are you there?"))  # writes again
    assert client.get("/stats", headers=h).json()["unread"] == 2
