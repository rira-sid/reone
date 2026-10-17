"""The seller's RIRAONE account: approval state, plan and payment, and founder announcements.
Plus the public visit beacon for RIRAONE's own login / sign-up pages."""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

import billing
import models
from auth import current_business, owner_business
from database import get_db

router = APIRouter(tags=["account"])


def announcements_for(db: Session, business_id: int) -> list[dict]:
    rows = db.query(models.Announcement).filter(
        models.Announcement.is_active.is_(True),
        or_(models.Announcement.business_id.is_(None), models.Announcement.business_id == business_id),
    ).order_by(models.Announcement.id.desc()).limit(5).all()
    return [{"id": a.id, "title": a.title, "body": a.body, "level": a.level, "created_at": a.created_at} for a in rows]


@router.get("/account")
def account(db: Session = Depends(get_db), business: models.Business = Depends(current_business)):
    return {
        "approval_status": business.approval_status,
        "is_active": business.is_active,
        "paused_reason": business.paused_reason,
        "billing": billing.status(db, business),
        "announcements": announcements_for(db, business.id),
    }


class PayIn(BaseModel):
    plan: str


@router.post("/account/pay")
def pay(payload: PayIn, db: Session = Depends(get_db), business: models.Business = Depends(owner_business)):
    """A payment link for the chosen plan (Monthly / Quarterly / Annual)."""
    if business.billing_exempt:
        raise HTTPException(status_code=400, detail="Your account doesn't need a plan")
    try:
        invoice = billing.create_invoice(db, business, payload.plan)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except billing.BillingUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    return billing.invoice_dict(invoice)


@router.get("/account/invoices")
def invoices(db: Session = Depends(get_db), business: models.Business = Depends(owner_business)):
    rows = db.query(models.PlanInvoice).filter(
        models.PlanInvoice.business_id == business.id, models.PlanInvoice.status != "cancelled"
    ).order_by(models.PlanInvoice.id.desc()).limit(24).all()
    return [billing.invoice_dict(i) for i in rows]


class VisitIn(BaseModel):
    page: str
    referrer: str | None = None
    src: str | None = None


@router.post("/track/visit")
def track_visit(payload: VisitIn, request: Request, background: BackgroundTasks):
    """Visits to RIRAONE's own login / sign-up pages (the seller dashboard site)."""
    from tracking import record_visit, request_info, traffic_source

    if payload.page in ("login", "signup"):
        background.add_task(record_visit, request_info(request), payload.page, None,
                            traffic_source(payload.referrer, payload.src, request.headers.get("user-agent")))
    return Response(status_code=204)
