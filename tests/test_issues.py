"""Issues page: listing, 'I'm on it', resolving in the customer's favour, counts on /stats and the founder site."""
from datetime import datetime, timedelta, timezone

import models
from database import SessionLocal
from test_app import founder_headers, new_seller


def make_issue(business_id, phone="919811100001", age=timedelta(0), paid_order=False):
    """An open issue on a chat (and optionally a paid order), created `age` ago."""
    db = SessionLocal()
    try:
        conversation = models.Conversation(business_id=business_id, phone=phone, customer_name="Ravi")
        db.add(conversation)
        db.flush()
        order = None
        if paid_order:
            customer = models.Customer(business_id=business_id, name="Ravi", phone=phone, address="12 Main St")
            db.add(customer)
            db.flush()
            order = models.Order(business_id=business_id, customer_id=customer.id, status="Delivered",
                                 payment_method="cod", is_paid=True, total_amount=250.0)
            db.add(order)
            db.flush()
        incident = models.Incident(business_id=business_id, conversation_id=conversation.id,
                                   order_id=order.id if order else None, category="damaged",
                                   summary="The packet came torn",
                                   created_at=datetime.now(timezone.utc) - age)
        db.add(incident)
        db.commit()
        return incident.id, conversation.id
    finally:
        db.close()


def test_list_and_start_work(client):
    bid, h = new_seller(client)
    iid, cid = make_issue(bid)
    rows = client.get("/issues", headers=h).json()
    assert [r["id"] for r in rows] == [iid]
    row = rows[0]
    assert row["number"] == f"INC-{iid}" and row["status"] == "open" and row["conversation_id"] == cid
    assert row["overdue"] is False and row["order_paid"] is False
    assert client.get("/stats", headers=h).json()["open_issues"] == 1

    r = client.patch(f"/issues/{iid}", json={"status": "in_progress"}, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "in_progress"
    assert client.get("/stats", headers=h).json()["open_issues"] == 1  # still not resolved

    chat = next(c for c in client.get("/conversations", headers=h).json() if c["id"] == cid)
    assert chat["open_issue"] == f"INC-{iid}"


def test_other_shops_cant_see_or_touch_it(client):
    bid, _ = new_seller(client)
    _, other = new_seller(client, name="Other Shop")
    iid, _ = make_issue(bid)
    assert client.get("/issues", headers=other).json() == []
    assert client.patch(f"/issues/{iid}", json={"status": "resolved"}, headers=other).status_code == 404


def test_refund_needs_a_paid_order(client):
    bid, h = new_seller(client)
    iid, _ = make_issue(bid)
    r = client.post(f"/issues/{iid}/resolve", json={"action": "refund"}, headers=h)
    assert r.status_code == 400


def test_cod_refund_and_discount(client):
    bid, h = new_seller(client)
    iid, _ = make_issue(bid, paid_order=True)
    row = client.get("/issues", headers=h).json()[0]
    assert row["order_paid"] is True and row["order_paid_online"] is False and row["order_total"] == 250.0
    r = client.post(f"/issues/{iid}/resolve", json={"action": "partial_refund", "amount": 100}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "resolved" and r.json()["refund_amount"] == 100
    assert client.get("/stats", headers=h).json()["open_issues"] == 0

    iid2, _ = make_issue(bid, phone="919811100002")
    assert client.post(f"/issues/{iid2}/resolve", json={"action": "discount"}, headers=h).status_code == 400
    r = client.post(f"/issues/{iid2}/resolve", json={"action": "discount", "amount": 50}, headers=h)
    assert r.status_code == 200 and r.json()["discount_amount"] == 50


def test_replacement_creates_free_order(client):
    bid, h = new_seller(client)
    iid, _ = make_issue(bid, paid_order=True)
    r = client.post(f"/issues/{iid}/resolve", json={"action": "replacement"}, headers=h)
    assert r.status_code == 200, r.text
    new_id = r.json()["replacement_order_id"]
    order = next(o for o in client.get("/orders", headers=h).json() if o["id"] == new_id)
    assert order["total_amount"] == 0 and order["is_paid"] is True


def test_founder_sees_issues_waiting_over_an_hour(client):
    bid, _ = new_seller(client, name="Slow Shop")
    fresh, _ = make_issue(bid, phone="919811100003")
    old, _ = make_issue(bid, phone="919811100004", age=timedelta(hours=2))
    data = client.get("/admin/overview", headers=founder_headers(client)).json()
    overdue_ids = [i["id"] for i in data["overdue_issues"]]
    assert old in overdue_ids and fresh not in overdue_ids
    row = next(i for i in data["overdue_issues"] if i["id"] == old)
    assert row["business_name"] == "Slow Shop" and row["overdue"] is True
    shop = next(b for b in data["businesses"] if b["id"] == bid)
    assert shop["issues_open"] == 2 and shop["issues_overdue"] == 1
    assert data["totals"]["issues_overdue"] >= 1
