"""Founder console part 2: enrolments, sign-in history, visitors, plans & billing, announcements."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

import billing
import models
from database import SessionLocal
from test_app import _ids, founder_headers, new_seller, signed


@pytest.fixture(autouse=True)
def fake_links(monkeypatch):
    """Plan payment links without calling Razorpay."""
    made = []

    def link(db, invoice, business):
        made.append(invoice.id)
        return {"id": f"plink_{invoice.id}", "short_url": f"https://rzp.io/sub{invoice.id}"}

    monkeypatch.setattr(billing, "_razorpay_link", link)
    return made


def self_signup(client, name="Walk-in Shop"):
    n = next(_ids)
    r = client.post("/auth/signup", json={"business_name": name, "email": f"self{n}@test.in", "password": "password1",
                                          "phone": "9876500000"}, headers={"X-Forwarded-For": f"10.0.{n}.1"})
    assert r.status_code == 200, r.text
    return r.json()["business_id"], {"Authorization": f"Bearer {r.json()['token']}"}


def test_open_signup_waits_for_approval(client):
    founder = founder_headers(client)
    bid, h = self_signup(client)
    account = client.get("/account", headers=h).json()
    assert account["approval_status"] == "pending"
    assert account["billing"]["plan"] == "trial" and account["billing"]["days_left"] > 13
    # Dashboard works, but customers can't use the shop yet.
    assert client.get("/products", headers=h).status_code == 200
    assert client.get(f"/shop/{bid}").status_code == 503
    overview = client.get("/admin/overview", headers=founder).json()
    assert overview["totals"]["pending_approval"] >= 1
    row = next(b for b in overview["businesses"] if b["id"] == bid)
    assert row["signup_source"] == "self" and row["approval_status"] == "pending"

    assert client.post(f"/admin/businesses/{bid}/approval", json={"approve": True}).status_code == 401  # founder only
    assert client.post(f"/admin/businesses/{bid}/approval", json={"approve": True}, headers=founder).status_code == 200
    assert client.get(f"/shop/{bid}").status_code == 200
    # Rejected sign-ups can't log in at all.
    bid2, _ = self_signup(client)
    client.post(f"/admin/businesses/{bid2}/approval", json={"approve": False}, headers=founder)
    email = f"self{next(_ids) - 1}@test.in"
    assert client.post("/auth/login", json={"email": email, "password": "password1"}).status_code == 403
    actions = [a["action"] for a in client.get("/admin/audit", headers=founder).json()]
    assert "approve" in actions and "reject" in actions


def test_invite_code_still_approves_instantly(client):
    _, h = new_seller(client)
    assert client.get("/account", headers=h).json()["approval_status"] == "approved"
    n = next(_ids)
    bad = {"business_name": "X", "email": f"bad{n}@test.in", "password": "password1", "signup_code": "WRONG"}
    assert client.post("/auth/signup", json=bad).status_code == 403


def test_open_signups_are_rate_limited_per_network(client):
    codes = []
    for _ in range(6):
        n = next(_ids)
        codes.append(client.post("/auth/signup", json={"business_name": "Spam", "email": f"spam{n}@test.in",
                                                       "password": "password1"},
                                 headers={"X-Forwarded-For": "203.0.113.9"}).status_code)
    assert codes[:5] == [200] * 5 and codes[5] == 429


def test_logins_are_recorded(client):
    founder = founder_headers(client)
    bid, _ = new_seller(client)
    email = f"seller{next(_ids) - 1}@test.in"
    client.post("/auth/login", json={"email": email, "password": "nope"}, headers={"User-Agent": "Mozilla/5.0 (iPhone)"})
    client.post("/auth/login", json={"email": email, "password": "password1"},
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/120 Safari/537"})
    data = client.get("/admin/logins", headers=founder).json()
    mine = [e for e in data["events"] if e["email"] == email]
    assert [e["success"] for e in mine][:2] == [True, False]
    assert mine[0]["business_id"] == bid and mine[0]["device"] == "Desktop · Windows · Chrome"
    assert mine[1]["device"].startswith("Mobile · iOS")
    assert data["summary"]["failed"] >= 1
    founder_logins = client.get("/admin/logins?site=founder", headers=founder).json()["events"]
    assert founder_logins and all(e["site"] == "founder" for e in founder_logins)
    failed = client.get("/admin/logins?failed_only=true", headers=founder).json()["events"]
    assert failed and not any(e["success"] for e in failed)


def test_visits_are_counted(client):
    founder = founder_headers(client)
    bid, _ = new_seller(client)
    phone = {"User-Agent": "Mozilla/5.0 (Linux; Android 14) Chrome/120 Mobile WhatsApp", "X-Forwarded-For": "10.1.1.1"}
    # Opening the page alone counts nothing - the page's own beacon does (robots never send it).
    page = client.get(f"/shop/{bid}?src=Instagram", headers=phone).text
    assert '"source": "Instagram"' in page
    data = client.get(f"/admin/visitors?business_id={bid}", headers=founder).json()
    assert data["total"] == 0
    for visitor, source in [("a", "WhatsApp"), ("a", "WhatsApp"), ("b", "Instagram")]:
        assert client.post(f"/shop/{bid}/visit", json={"visitor": visitor, "source": source},
                           headers=phone).status_code == 204
    assert client.post("/track/visit", json={"page": "signup", "referrer": "https://www.google.com/", "visitor": "c"},
                       headers={**phone, "User-Agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/120"}).status_code == 204
    data = client.get(f"/admin/visitors?business_id={bid}", headers=founder).json()
    assert (data["total"], data["people"]) == (3, 2)  # visitor "a" came twice
    assert {s["label"]: s["count"] for s in data["sources"]} == {"WhatsApp": 1, "Instagram": 1}
    everything = client.get("/admin/visitors", headers=founder).json()
    assert any(p["label"] == "Seller sign-up page" for p in everything["pages"])
    assert any(s["label"] == "Google" for s in everything["sources"])
    assert len(everything["daily"]) == 30 and everything["daily"][-1]["people"] >= 3


def test_own_visits_and_robots_are_not_counted(client):
    founder = founder_headers(client)
    bid, h = new_seller(client, alert_phone="9000000077")
    phone = {"User-Agent": "Mozilla/5.0 (iPhone) Safari/604.1", "X-Forwarded-For": "10.2.2.2"}
    # A browser that signed in to a dashboard, or a shop link opened from the dashboard (?staff=1).
    client.post(f"/shop/{bid}/visit", json={"visitor": "me", "staff": True}, headers=phone)
    client.post("/track/visit", json={"page": "login", "visitor": "me", "staff": True}, headers=phone)
    # Link previews and crawlers.
    for agent in ("WhatsApp/2.24.1 A", "facebookexternalhit/1.1", "Googlebot/2.1"):
        client.post(f"/shop/{bid}/visit", json={"visitor": "bot"}, headers={**phone, "User-Agent": agent})
    # Same internet address a seller signed in from this week (TestClient signs in as "testclient").
    client.post(f"/shop/{bid}/visit", json={"visitor": "x"}, headers={"User-Agent": phone["User-Agent"]})
    assert client.get(f"/admin/visitors?business_id={bid}", headers=founder).json()["total"] == 0
    # The shop's own alert number opening its personal shop link is marked as the shop's own.
    from chatbot import sign_value
    token = "919000000077." + sign_value(f"cust:{bid}:919000000077")
    assert '"staff": true' in client.get(f"/shop/{bid}?c={token}", headers=phone).text
    assert '"staff": false' in client.get(f"/shop/{bid}", headers=phone).text


def test_plan_payment_through_razorpay_extends_and_unpauses(client, fake_whatsapp):
    founder = founder_headers(client)
    bid, h = new_seller(client, alert_phone="9000000001")
    plans = client.get("/account", headers=h).json()["billing"]["plans"]
    assert [p["id"] for p in plans] == ["monthly", "quarterly", "annual"]

    invoice = client.post("/account/pay", json={"plan": "quarterly"}, headers=h).json()
    assert invoice["link_url"].startswith("https://rzp.io/") and invoice["amount"] == billing.price(SessionLocal(), "quarterly")
    # Same plan again reuses the open link.
    assert client.post("/account/pay", json={"plan": "quarterly"}, headers=h).json()["id"] == invoice["id"]

    with SessionLocal() as db:  # pretend the trial ran out and the shop got paused
        b = db.get(models.Business, bid)
        b.paid_until = datetime.now(timezone.utc) - timedelta(days=10)
        b.is_active, b.paused_reason = False, "billing"
        db.commit()
    assert client.get("/account", headers=h).json()["billing"]["state"] == "paused"  # owner can still log in to pay

    body = json.dumps({"event": "payment_link.paid", "payload": {
        "payment_link": {"entity": {"reference_id": f"SUB-{invoice['id']}"}},
        "payment": {"entity": {"id": f"pay_sub_{invoice['id']}", "amount": round(invoice["amount"] * 100)}},
    }}).encode()
    r = client.post("/payments/webhook/razorpay", content=body, headers=signed("legacy-webhook-secret", body))
    assert r.json()["status"] == "ok"
    again = client.post("/payments/webhook/razorpay", content=body, headers=signed("legacy-webhook-secret", body))
    assert again.json()["status"] == "already processed"

    st = client.get("/account", headers=h).json()["billing"]
    assert st["plan"] == "quarterly" and st["state"] == "active" and 89 < st["days_left"] <= 90
    assert client.get(f"/shop/{bid}").status_code == 200
    data = client.get("/admin/billing", headers=founder).json()
    assert data["collected_total"] >= invoice["amount"] and data["mrr"] > 0


def test_reminders_then_auto_pause(client, fake_whatsapp):
    bid, h = new_seller(client, alert_phone="9000000002")
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        db.get(models.Business, bid).paid_until = now + timedelta(days=2)
        db.commit()

    def tick(at):
        with SessionLocal() as db:
            asyncio.run(billing.billing_tick(db, at))
        return [s for s in fake_whatsapp.sent if s[1] == "919000000002"]

    assert len(tick(now)) == 1  # "ends soon"
    assert len(tick(now)) == 1  # only once per stage
    assert len(tick(now + timedelta(days=2, hours=1))) == 2  # due
    assert len(tick(now + timedelta(days=6))) == 3  # overdue
    with SessionLocal() as db:
        assert db.get(models.Business, bid).is_active
    assert len(tick(now + timedelta(days=10))) == 4  # paused
    with SessionLocal() as db:
        b = db.get(models.Business, bid)
        assert not b.is_active and b.paused_reason == "billing"
    assert client.get(f"/shop/{bid}").status_code == 503
    assert client.get("/account", headers=h).status_code == 200


def test_founder_manual_payment_free_accounts_and_prices(client):
    founder = founder_headers(client)
    bid, h = new_seller(client)
    r = client.post(f"/admin/businesses/{bid}/payment", json={"plan": "annual", "note": "cash"}, headers=founder)
    assert r.status_code == 200 and r.json()["plan"] == "annual" and r.json()["days_left"] > 364
    r = client.patch(f"/admin/businesses/{bid}/billing", json={"plan": "free"}, headers=founder)
    assert r.json()["state"] == "exempt"
    with SessionLocal() as db:
        assert db.get(models.Business, bid).billing_exempt
    prices = client.put("/admin/plans", json={"monthly": 599, "quarterly": 1599, "annual": 5999}, headers=founder).json()
    assert [p["price"] for p in prices] == [599, 1599, 5999] and prices[2]["saving_pct"] > 0
    assert client.put("/admin/plans", json={"monthly": 0, "quarterly": 1, "annual": 1}, headers=founder).status_code == 400
    client.put("/admin/plans", json={"monthly": 499, "quarterly": 1349, "annual": 4999}, headers=founder)
    r = client.patch(f"/admin/businesses/{bid}/billing", json={"extend_days": 7}, headers=founder)
    assert r.status_code == 200


def test_announcements_reach_sellers(client):
    founder = founder_headers(client)
    bid, h = new_seller(client)
    other, h2 = new_seller(client)
    client.post("/admin/announcements", json={"title": "New feature: voice orders"}, headers=founder)
    client.post("/admin/announcements", json={"title": "Only for you", "business_id": bid}, headers=founder)
    mine = [a["title"] for a in client.get("/account", headers=h).json()["announcements"]]
    theirs = [a["title"] for a in client.get("/account", headers=h2).json()["announcements"]]
    assert "Only for you" in mine and "Only for you" not in theirs and "New feature: voice orders" in theirs
    a_id = client.get("/admin/announcements", headers=founder).json()[0]["id"]
    client.patch(f"/admin/announcements/{a_id}", json={"is_active": False}, headers=founder)
    assert "Only for you" not in [a["title"] for a in client.get("/account", headers=h).json()["announcements"]]


def test_health_lists_problems(client):
    founder = founder_headers(client)
    bid, _ = self_signup(client)
    from tracking import log_event

    log_event("ai", "Gemini 503", bid)
    data = client.get("/admin/health", headers=founder).json()
    assert any(w["business_id"] == bid and "approval" in w["text"] for w in data["warnings"])
    assert any(e["source"] == "ai" and e["business_id"] == bid for e in data["events"])
    assert client.get("/admin/health").status_code == 401


def test_shops_get_a_web_address_from_their_name(client):
    founder = founder_headers(client)
    created = client.post("/admin/businesses", json={"name": "Meena's Pickles & Snacks", "email": f"meena{next(_ids)}@x.in"},
                          headers=founder).json()
    bid = created["business"]["id"]
    assert created["business"]["shop_url"].endswith("/shop/meenas-pickles-and-snacks")
    assert client.get("/shop/meenas-pickles-and-snacks").status_code == 200
    assert client.get("/shop/MEENAS-PICKLES-AND-SNACKS").status_code == 200
    assert client.get(f"/shop/{bid}").status_code == 200  # old number links still work
    # Same name again gets its own address.
    again = client.post("/admin/businesses", json={"name": "Meena's Pickles & Snacks", "email": f"meena{next(_ids)}@x.in"},
                        headers=founder).json()
    assert again["business"]["shop_url"].endswith("/shop/meenas-pickles-and-snacks-2")
    # A name in another script falls back to shop-<id>.
    _, h = new_seller(client, name="மீனா ஊறுகாய்")
    assert client.get("/business", headers=h).json()["catalog_path"].startswith("/shop/shop-")
    # The seller can choose their own address.
    assert client.patch("/business", json={"slug": "Meena Foods"}, headers=h).json()["catalog_path"] == "/shop/meena-foods"
    assert client.patch("/business", json={"slug": "meenas-pickles-and-snacks"}, headers=h).status_code == 409
    assert client.patch("/business", json={"slug": "ab"}, headers=h).status_code == 400
    assert client.patch("/business", json={"slug": "12345"}, headers=h).status_code == 400
    assert client.get("/shop/meena-foods").status_code == 200


def test_founder_chooses_the_web_address(client):
    founder = founder_headers(client)
    r = client.post("/admin/businesses", json={"name": "Siva Stores", "email": f"siva{next(_ids)}@x.in",
                                               "slug": "Siva Masala"}, headers=founder)
    assert r.status_code == 200 and r.json()["business"]["slug"] == "siva-masala"
    bid = r.json()["business"]["id"]
    dup = client.post("/admin/businesses", json={"name": "Other", "email": f"o{next(_ids)}@x.in", "slug": "siva-masala"},
                      headers=founder)
    assert dup.status_code == 400 and "taken" in dup.json()["detail"]
    r = client.patch(f"/admin/businesses/{bid}/address", json={"slug": "sivamasala"}, headers=founder)
    assert r.json()["slug"] == "sivamasala" and r.json()["shop_url"].endswith("/shop/sivamasala")
    assert client.patch(f"/admin/businesses/{bid}/address", json={"slug": "x"}, headers=founder).status_code == 400
    assert client.patch(f"/admin/businesses/{bid}/address", json={"slug": "abc"}).status_code == 401
