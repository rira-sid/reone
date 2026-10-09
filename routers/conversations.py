from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

import models
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

    class Config:
        from_attributes = True


class MessageOut(BaseModel):
    id: int
    sender: str
    text: str
    created_at: datetime

    class Config:
        from_attributes = True


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


@router.get("", response_model=list[ConversationOut])
def list_conversations(db: Session = Depends(get_db), business: models.Business = Depends(current_business)):
    return (
        db.query(models.Conversation)
        .filter(models.Conversation.business_id == business.id)
        .order_by(models.Conversation.updated_at.desc())
        .all()
    )


@router.get("/{conversation_id}/messages", response_model=list[MessageOut])
def list_messages(conversation_id: int, db: Session = Depends(get_db), business: models.Business = Depends(current_business)):
    return _get_conversation(db, business, conversation_id).messages


@router.patch("/{conversation_id}/takeover", response_model=ConversationOut)
def set_takeover(conversation_id: int, payload: TakeoverUpdate, db: Session = Depends(get_db), business: models.Business = Depends(current_business)):
    conversation = _get_conversation(db, business, conversation_id)
    conversation.ai_paused = payload.ai_paused
    db.commit()
    db.refresh(conversation)
    return conversation


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
