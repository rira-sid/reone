"""One-time setup wizard: what's done so far, the last step's live test (the seller says "Hi" to their
own shop and pays a ₹1 test order on their phone), and marking the wizard finished."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import knowledge
import models
from auth import current_business, owner_business
from business import whatsapp_creds
from database import get_db
from notifications import within_service_window
from routers.pay import effective_payment_method, send_payment_request
from seller_alerts import _digits

router = APIRouter(prefix="/business/setup", tags=["setup"])

TEST_AMOUNT = 1.0


def _own_chat(db: Session, business: models.Business) -> models.Conversation | None:
    phone = _digits(business.alert_phone)
    if not phone:
        return None
    return db.query(models.Conversation).filter(
        models.Conversation.business_id == business.id, models.Conversation.phone == phone).first()


def _test_order(db: Session, business: models.Business) -> models.Order | None:
    return db.query(models.Order).filter(models.Order.business_id == business.id, models.Order.is_test.is_(True)) \
        .order_by(models.Order.id.desc()).first()


def _bot_replied(db: Session, conversation: models.Conversation | None) -> bool:
    if not conversation:
        return False
    return db.query(models.Message.id).filter(models.Message.conversation_id == conversation.id,
                                              models.Message.sender == "ai").first() is not None


@router.get("")
def status(db: Session = Depends(get_db), business: models.Business = Depends(current_business)):
    ready = {r["key"] for r in knowledge.ready_for(business)}
    answered = {a.key for a in knowledge.answers(db, business) if a.key and (a.answer or "").strip()}
    chat = _own_chat(db, business)
    order = _test_order(db, business)
    zones = db.query(models.DeliveryZone.id).filter(models.DeliveryZone.business_id == business.id).count()
    return {
        "completed": business.setup_completed_at is not None,
        "zones": zones,
        "delivery_days": business.delivery_days_max is not None,
        "knowledge_total": len(ready),
        "knowledge_answered": len(ready & answered),
        # The test: the seller's own number has chatted with the shop and the bot answered.
        "said_hi": _bot_replied(db, chat),
        "chat_open": within_service_window(chat),
        "test_order": {"id": order.id, "paid": order.is_paid, "status": order.status} if order else None,
    }


@router.post("/test-order")
async def send_test_order(db: Session = Depends(get_db), business: models.Business = Depends(owner_business)):
    """A ₹1 order for the seller's own number, with the same payment message a customer gets. Paying it
    checks the whole way round: WhatsApp → pay page → Razorpay → payment confirmed → invoice."""
    if not whatsapp_creds(business):
        raise HTTPException(status_code=409, detail="Connect your WhatsApp number first.")
    if not business.alert_phone:
        raise HTTPException(status_code=409, detail="Add your alert number first - the test order goes to that phone.")
    if not effective_payment_method(business):
        raise HTTPException(status_code=409, detail="Set up payments first.")
    chat = _own_chat(db, business)
    if not within_service_window(chat):
        raise HTTPException(status_code=409, detail="First send 'Hi' to your shop number from your alert phone, "
                                                    "then try again.")
    phone = _digits(business.alert_phone)
    customer = models.Customer(business_id=business.id, name=chat.customer_name or business.name, phone=phone,
                               address="Test order")
    db.add(customer)
    db.flush()
    order = models.Order(business_id=business.id, customer_id=customer.id, status="Pending", is_test=True,
                         total_amount=TEST_AMOUNT, payment_method="online",
                         customer_note="Setup test - ₹1 order to check payments")
    db.add(order)
    db.commit()
    db.refresh(order)
    await send_payment_request(business, order, phone)
    return {"id": order.id, "paid": False, "status": order.status}


@router.post("/finish")
def finish(db: Session = Depends(get_db), business: models.Business = Depends(owner_business)):
    business.setup_completed_at = business.setup_completed_at or datetime.now(timezone.utc)
    db.commit()
    return {"completed": True}


@router.post("/reopen")
def reopen(db: Session = Depends(get_db), business: models.Business = Depends(owner_business)):
    business.setup_completed_at = None
    db.commit()
    return {"completed": False}
