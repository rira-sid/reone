"""Shop knowledge page: ready-made questions to answer once, the seller's own questions, and answers
saved from chat replies. Staff can add answers too - they're the ones answering customers."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

import knowledge
import models
from auth import current_business
from database import get_db

router = APIRouter(prefix="/knowledge", tags=["shop knowledge"])

MAX_LEN = 1500


@router.get("")
def overview(db: Session = Depends(get_db), business: models.Business = Depends(current_business)):
    saved = knowledge.answers(db, business)
    by_key = {a.key: a for a in saved if a.key}
    ready = [{"key": r["key"], "question": r["question"],
              "answer": by_key[r["key"]].answer if r["key"] in by_key else "",
              "id": by_key[r["key"]].id if r["key"] in by_key else None}
             for r in knowledge.ready_for(business)]
    return {"business_type": business.business_type, "ready": ready,
            "custom": [knowledge.answer_dict(a) for a in saved if not a.key]}


class ReadyAnswer(BaseModel):
    answer: str


@router.put("/ready/{key}")
def answer_ready(key: str, payload: ReadyAnswer, db: Session = Depends(get_db),
                 business: models.Business = Depends(current_business)):
    """Answer a ready-made question; a blank answer removes it (the owner gets asked instead)."""
    ready = knowledge.READY_BY_KEY.get(key)
    if not ready:
        raise HTTPException(status_code=404, detail="Unknown question")
    text = payload.answer.strip()[:MAX_LEN]
    row = db.query(models.ShopAnswer).filter(models.ShopAnswer.business_id == business.id,
                                             models.ShopAnswer.key == key).first()
    if not text:
        if row:
            db.delete(row)
            db.commit()
        return {"key": key, "question": ready["question"], "answer": "", "id": None}
    if not row:
        row = models.ShopAnswer(business_id=business.id, key=key, question=ready["question"], answer=text)
        db.add(row)
    row.answer = text
    db.commit()
    return {"key": key, "question": ready["question"], "answer": row.answer, "id": row.id}


class AnswerIn(BaseModel):
    question: str
    answer: str
    source: str = "seller"
    product_id: int | None = None


def _clean(payload: AnswerIn) -> tuple[str, str]:
    question, answer = payload.question.strip()[:500], payload.answer.strip()[:MAX_LEN]
    if not question or not answer:
        raise HTTPException(status_code=400, detail="Write both the question and your answer")
    return question, answer


@router.post("")
def add_answer(payload: AnswerIn, db: Session = Depends(get_db), business: models.Business = Depends(current_business)):
    question, answer = _clean(payload)
    row = models.ShopAnswer(business_id=business.id, question=question, answer=answer,
                            source="chat" if payload.source == "chat" else "seller", product_id=payload.product_id)
    db.add(row)
    db.commit()
    db.refresh(row)
    return knowledge.answer_dict(row)


def _get(db: Session, business: models.Business, answer_id: int) -> models.ShopAnswer:
    row = db.query(models.ShopAnswer).filter(models.ShopAnswer.id == answer_id,
                                             models.ShopAnswer.business_id == business.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Answer not found")
    return row


@router.put("/{answer_id}")
def update_answer(answer_id: int, payload: AnswerIn, db: Session = Depends(get_db),
                  business: models.Business = Depends(current_business)):
    row = _get(db, business, answer_id)
    row.question, row.answer = _clean(payload)
    db.commit()
    return knowledge.answer_dict(row)


@router.delete("/{answer_id}")
def delete_answer(answer_id: int, db: Session = Depends(get_db), business: models.Business = Depends(current_business)):
    db.delete(_get(db, business, answer_id))
    db.commit()
    return {"ok": True}
