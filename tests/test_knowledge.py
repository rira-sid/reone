"""Shop knowledge: ready-made questions by shop type, own questions, saved chat answers, and the bot using them."""
from test_app import new_seller, text, whatsapp_message


def test_ready_questions_follow_the_shop_type(client):
    _, h = new_seller(client)
    keys = {r["key"] for r in client.get("/knowledge", headers=h).json()["ready"]}
    assert {"returns", "veg", "fabric", "warranty"} <= keys  # no type yet: everything
    assert client.patch("/business", json={"business_type": "spaceships"}, headers=h).status_code == 400
    client.patch("/business", json={"business_type": "clothing"}, headers=h)
    data = client.get("/knowledge", headers=h).json()
    keys = {r["key"] for r in data["ready"]}
    assert data["business_type"] == "clothing"
    assert {"returns", "fabric", "size_fit"} <= keys and "veg" not in keys and "warranty" not in keys


def test_answer_edit_and_clear(client):
    _, h = new_seller(client)
    assert client.put("/knowledge/ready/nope", json={"answer": "x"}, headers=h).status_code == 404
    r = client.put("/knowledge/ready/returns", json={"answer": "  Exchange within 7 days, unused.  "}, headers=h)
    assert r.json()["answer"] == "Exchange within 7 days, unused." and r.json()["id"]
    r = client.put("/knowledge/ready/returns", json={"answer": "Exchange within 10 days."}, headers=h)
    ready = {x["key"]: x for x in client.get("/knowledge", headers=h).json()["ready"]}
    assert ready["returns"]["answer"] == "Exchange within 10 days."
    client.put("/knowledge/ready/returns", json={"answer": ""}, headers=h)
    ready = {x["key"]: x for x in client.get("/knowledge", headers=h).json()["ready"]}
    assert ready["returns"]["answer"] == "" and ready["returns"]["id"] is None

    assert client.post("/knowledge", json={"question": "Open on Sunday?", "answer": ""}, headers=h).status_code == 400
    row = client.post("/knowledge", json={"question": "Do you ship abroad?", "answer": "Yes, to UAE and Singapore.",
                                          "source": "chat"}, headers=h).json()
    assert row["source"] == "chat"
    r = client.put(f"/knowledge/{row['id']}", json={"question": "Do you ship abroad?", "answer": "UAE only."}, headers=h)
    assert r.json()["answer"] == "UAE only."
    _, other = new_seller(client, name="Other")
    assert client.delete(f"/knowledge/{row['id']}", headers=other).status_code == 404
    assert client.delete(f"/knowledge/{row['id']}", headers=h).status_code == 200
    assert client.get("/knowledge", headers=h).json()["custom"] == []


def test_bot_answers_from_knowledge(client, fake_ai, fake_whatsapp):
    _, h = new_seller(client, wa_phone_number_id="PN_KNOW", wa_token="t", alert_phone="919800000009")
    client.put("/knowledge/ready/shelf_life", json={"answer": "6 months unopened, 2 months after opening."}, headers=h)
    client.post("/knowledge", json={"question": "Do you ship abroad?", "answer": "Only to UAE."}, headers=h)
    fake_ai.queue = [fake_ai.turn(intent="info", reply="It stays fresh for 6 months unopened 😊")]
    whatsapp_message(client, "PN_KNOW", "919000000301", text("how long will it last?"))
    call = fake_ai.calls[-1]
    assert "6 months unopened" in call["knowledge"] and "Only to UAE." in call["knowledge"]
    assert fake_whatsapp.sent[-1][1:] == ("919000000301", "text", "It stays fresh for 6 months unopened 😊")
    chat = next(c for c in client.get("/conversations", headers=h).json() if c["phone"] == "919000000301")
    assert chat["needs_seller"] is False  # answered by the bot, the owner isn't bothered

    # "info" with nothing to say goes to the owner instead of silence
    fake_ai.queue = [fake_ai.turn(intent="info", reply="")]
    whatsapp_message(client, "PN_KNOW", "919000000302", text("do you have a shop in Madurai?"))
    chat = next(c for c in client.get("/conversations", headers=h).json() if c["phone"] == "919000000302")
    assert chat["needs_seller"] is True
