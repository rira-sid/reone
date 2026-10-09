from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session, joinedload

import models
import schemas
from notifications import within_service_window
from routers.orders import _serialize_order
from auth import current_business
from database import get_db
from business import whatsapp_creds
from whatsapp import WhatsAppSendError, send_message

router = APIRouter(prefix="/conversations", tags=["conversations"])


class ConversationOut(BaseModel):
    id: int
    phone: str
    customer_name: str | None
    language: str | None
    ai_paused: bool
    updated_at: datetime | None
    # False once 24h have passed since the customer's last message - WhatsApp then rejects
    # free-form replies until the customer writes again.
    reply_window_open: bool

    model_config = ConfigDict(from_attributes=True)


class MessageOut(BaseModel):
    id: int
    sender: str
    text: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TakeoverUpdate(BaseModel):
    ai_paused: bool


class SellerReply(BaseModel):
    text: str


def _get_conversation(db: Session, business: models.Business, conversation_id: int) -> models.Conversation:
    conversation = db.query(models.Conversation).filter(
        models.Conversation.id == conversation_id, models.Conversation.business_id == business.id
    ).first()
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


def _conversation_out(conversation: models.Conversation) -> dict:
    return {
        "id": conversation.id,
        "phone": conversation.phone,
        "customer_name": conversation.customer_name,
        "language": conversation.language,
        "ai_paused": conversation.ai_paused,
        "updated_at": conversation.updated_at,
        "reply_window_open": within_service_window(conversation),
    }


@router.get("", response_model=list[ConversationOut])
def list_conversations(db: Session = Depends(get_db), business: models.Business = Depends(current_business)):
    conversations = (
        db.query(models.Conversation)
        .filter(models.Conversation.business_id == business.id)
        .order_by(models.Conversation.updated_at.desc())
        .all()
    )
    return [_conversation_out(c) for c in conversations]


@router.get("/{conversation_id}/orders", response_model=list[schemas.OrderOut])
def list_customer_orders(conversation_id: int, db: Session = Depends(get_db), business: models.Business = Depends(current_business)):
    conversation = _get_conversation(db, business, conversation_id)
    orders = (
        db.query(models.Order)
        .join(models.Customer)
        .options(joinedload(models.Order.customer), joinedload(models.Order.items).joinedload(models.OrderItem.product))
        .filter(models.Order.business_id == business.id, models.Customer.phone == conversation.phone)
        .order_by(models.Order.id.desc())
        .all()
    )
    return [_serialize_order(o) for o in orders]


@router.get("/{conversation_id}/messages", response_model=list[MessageOut])
def list_messages(conversation_id: int, db: Session = Depends(get_db), business: models.Business = Depends(current_business)):
    return _get_conversation(db, business, conversation_id).messages


@router.patch("/{conversation_id}/takeover", response_model=ConversationOut)
def set_takeover(conversation_id: int, payload: TakeoverUpdate, db: Session = Depends(get_db), business: models.Business = Depends(current_business)):
    conversation = _get_conversation(db, business, conversation_id)
    conversation.ai_paused = payload.ai_paused
    db.commit()
    db.refresh(conversation)
    return _conversation_out(conversation)


@router.post("/{conversation_id}/reply", response_model=MessageOut)
async def seller_reply(conversation_id: int, payload: SellerReply, db: Session = Depends(get_db), business: models.Business = Depends(current_business)):
    conversation = _get_conversation(db, business, conversation_id)
    # Send first: only record the message once the customer has actually been sent it, so the
    # inbox never shows a reply that didn't go out.
    try:
        await send_message(whatsapp_creds(business), conversation.phone, payload.text)
    except WhatsAppSendError as e:
        raise HTTPException(status_code=502, detail=str(e))
    # A seller typing into the chat implies they're handling it - keep the AI out of the way.
    conversation.ai_paused = True
    message = models.Message(conversation_id=conversation.id, sender="seller", text=payload.text)
    db.add(message)
    db.commit()
    db.refresh(message)
    return message
