"""Products for any shop: up to two options (Size x Colour), each combination with its own price, stock and photo."""
import json

import ai
import models
from database import SessionLocal
from test_app import new_seller, shop_checkout

TINY_JPEG = "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////wAALCAABAAEBAREA/8QAFAABAAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q=="


def kurta(client, h, **extra):
    body = {"name": "Cotton Kurta", "price": 0, "option_names": ["Size", "Colour"],
            "nutrition": "Fabric: Cotton\nWash: Hand wash",
            "variants": [{"option1": "M", "option2": "Red", "price": 499, "stock": 3},
                         {"option1": "L", "option2": "Red", "price": 549, "stock": 0},
                         {"option1": "M", "option2": "Blue", "price": 499, "stock": 2}], **extra}
    r = client.post("/products", json=body, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def test_combinations_get_labels_price_and_stock(client):
    _, h = new_seller(client, business_type="clothing")
    p = kurta(client, h)
    assert p["option_names"] == ["Size", "Colour"]
    assert [v["label"] for v in p["variants"]] == ["M / Red", "L / Red", "M / Blue"]
    assert p["price"] == 499 and p["stock"] == 5  # "from" price and total stock


def test_option_rules(client):
    _, h = new_seller(client)
    bad = client.post("/products", json={"name": "X", "option_names": ["Size", "Colour", "Sleeve"], "variants": []}, headers=h)
    assert bad.status_code == 400
    dup = client.post("/products", json={"name": "X", "option_names": ["Size", "size"]}, headers=h)
    assert dup.status_code == 400
    missing = client.post("/products", json={"name": "X", "option_names": ["Size", "Colour"],
                                             "variants": [{"option1": "M", "price": 10}]}, headers=h)
    assert missing.status_code == 400
    same = client.post("/products", json={"name": "X", "option_names": ["Size"],
                                          "variants": [{"option1": "M", "price": 10}, {"option1": "m", "price": 12}]}, headers=h)
    assert same.status_code == 400


def test_old_style_sizes_still_work(client):
    _, h = new_seller(client)
    r = client.post("/products", json={"name": "Masala", "variants": [{"label": "100g", "price": 40, "stock": 5}]},
                    headers=h)
    assert r.status_code == 200 and r.json()["option_names"] is None and r.json()["variants"][0]["label"] == "100g"


def test_combination_photo_and_editing(client):
    _, h = new_seller(client)
    p = kurta(client, h)
    with_photo = client.post(f"/products/{p['id']}/images", json={"data": TINY_JPEG}, headers=h)
    assert with_photo.status_code == 200, with_photo.text
    image_id = with_photo.json()["images"][0]["id"]
    variants = [{**v, "image_id": image_id if v["option2"] == "Blue" else None} for v in p["variants"]]
    r = client.patch(f"/products/{p['id']}", json={"variants": variants}, headers=h)
    blue = next(v for v in r.json()["variants"] if v["option2"] == "Blue")
    assert blue["image_id"] == image_id
    # a photo from another product is ignored
    other = kurta(client, h, name="Other")
    r = client.patch(f"/products/{other['id']}", json={"variants": [{**other["variants"][0], "image_id": image_id}]},
                     headers=h)
    assert r.json()["variants"][0]["image_id"] is None
    # deleting the photo clears it from the combination
    r = client.delete(f"/products/{p['id']}/images/{image_id}", headers=h)
    assert all(v["image_id"] is None for v in r.json()["variants"])


def test_shop_page_and_checkout_with_combination(client):
    bid, h = new_seller(client, business_type="clothing")
    p = kurta(client, h)
    html = client.get(f"/shop/{bid}").text
    data = json.loads(html.split('<script id="data" type="application/json">', 1)[1].split("</script>", 1)[0]
                      .replace("<\\/", "</"))
    item = next(x for x in data["products"] if x["id"] == p["id"])
    assert item["options"] == ["Size", "Colour"] and item["variants"][0]["o1"] == "M" and item["variants"][0]["o2"] == "Red"
    assert data["shop"]["type"] == "clothing"
    blue = next(v for v in p["variants"] if v["option2"] == "Blue")
    r = shop_checkout(client, bid, "919000000501", [{"product_id": p["id"], "variant_id": blue["id"], "quantity": 2}])
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        order = db.get(models.Order, r.json()["order_id"])
        assert order.items[0].variant_label == "M / Blue" and order.total_amount == 998


def test_bot_sees_options_and_details(client):
    _, h = new_seller(client)
    p = kurta(client, h)
    with SessionLocal() as db:
        text = ai._catalog_text([db.get(models.Product, p["id"])])
    assert "Size / Colour: variant_id" in text and "M / Blue" in text and "details: Fabric: Cotton; Wash: Hand wash" in text
