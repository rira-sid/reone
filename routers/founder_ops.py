"""Founder console, part 2: enrolments, sign-ins, visitors, plans & billing, announcements, the
founder's own activity trail and system health. Founder login only (founder_auth.py)."""
from collections import Counter
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

import billing
import models
from business import DEFAULT_BUSINESS_ID
from database import get_db
from founder_auth import current_founder
from tracking import audit, client_ip

router = APIRouter(prefix="/admin", tags=["founder"], dependencies=[Depends(current_founder)])


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _since(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=max(1, min(days, 365)))


def _business(db: Session, business_id: int) -> models.Business:
    business = db.get(models.Business, business_id)
    if not business:
        raise HTTPException(status_code=404, detail="Business not found")
    return business


def _names(db: Session) -> dict[int, str]:
    return {b.id: b.name for b in db.query(models.Business.id, models.Business.name).all()}


# ---- enrolments -----------------------------------------------------------------------------

class Decision(BaseModel):
    approve: bool
    note: str | None = None


@router.post("/businesses/{business_id}/approval")
async def decide(business_id: int, payload: Decision, request: Request, db: Session = Depends(get_db),
                 founder: models.FounderUser = Depends(current_founder)):
    business = _business(db, business_id)
    if business.id == DEFAULT_BUSINESS_ID:
        raise HTTPException(status_code=400, detail="That's your own shop")
    business.approval_status = "approved" if payload.approve else "rejected"
    if payload.approve and business.plan == "trial":
        billing.start_trial(business, business.signup_source or "self")  # trial starts when they're let in
    db.commit()
    audit(db, founder, "approve" if payload.approve else "reject", business.id, payload.note, client_ip(request))
    if payload.approve:
        await billing.seller_message(
            db, business, f"Welcome to RIRAONE! {business.name} is approved - your shop can now take orders. "
                          f"Your free trial runs for {billing.TRIAL_DAYS} days.",
            "account is approved - welcome to RIRAONE", billing.SELLER_DASHBOARD_URL)
    return {"id": business.id, "approval_status": business.approval_status}


# ---- sign-ins --------------------------------------------------------------------------------

@router.get("/logins")
def logins(days: int = 30, site: str | None = None, business_id: int | None = None, failed_only: bool = False,
           db: Session = Depends(get_db)):
    q = db.query(models.LoginEvent).filter(models.LoginEvent.created_at >= _since(days))
    if site:
        q = q.filter(models.LoginEvent.site == site)
    if business_id:
        q = q.filter(models.LoginEvent.business_id == business_id)
    if failed_only:
        q = q.filter(models.LoginEvent.success.is_(False))
    rows = q.order_by(models.LoginEvent.id.desc()).limit(500).all()
    names = _names(db)
    staff = {s.id: s.name for s in db.query(models.StaffUser.id, models.StaffUser.name).all()}
    all_rows = db.query(models.LoginEvent).filter(models.LoginEvent.created_at >= _since(days)).all()
    return {
        "summary": {
            "total": len(all_rows),
            "failed": sum(1 for r in all_rows if not r.success),
            "people": len({r.email for r in all_rows if r.success}),
            "countries": len({r.country for r in all_rows if r.country}),
        },
        "events": [{
            "id": r.id, "site": r.site, "business_id": r.business_id, "business_name": names.get(r.business_id),
            "who": staff.get(r.staff_id) if r.staff_id else ("Owner" if r.business_id else None),
            "email": r.email, "success": r.success, "ip": r.ip, "country": r.country,
            "country_code": r.country_code, "city": r.city, "device": r.device, "created_at": _utc(r.created_at),
        } for r in rows],
    }


# ---- visitors --------------------------------------------------------------------------------

def _top(counter: Counter, n: int = 12) -> list[dict]:
    return [{"label": label or "Unknown", "count": count} for label, count in counter.most_common(n)]


@router.get("/visitors")
def visitors(days: int = 30, page: str | None = None, business_id: int | None = None, db: Session = Depends(get_db)):
    q = db.query(models.Visit).filter(models.Visit.created_at >= _since(days))
    if page:
        q = q.filter(models.Visit.page == page)
    if business_id:
        q = q.filter(models.Visit.business_id == business_id)
    rows = q.all()
    names = _names(db)
    flags = {r.country: r.country_code for r in rows if r.country}
    by_day = Counter(_utc(r.created_at).date().isoformat() for r in rows if r.created_at)
    today = datetime.now(timezone.utc).date()
    span = max(1, min(days, 90))
    return {
        "total": len(rows),
        "countries": [{**c, "code": flags.get(c["label"])} for c in _top(Counter(r.country for r in rows), 30)],
        "cities": _top(Counter(", ".join(p for p in (r.city, r.country) if p) or None for r in rows), 15),
        "devices": _top(Counter((r.device or "Unknown").split(" · ")[0] for r in rows)),
        "systems": _top(Counter(" · ".join((r.device or "Unknown").split(" · ")[1:2]) or "Unknown" for r in rows)),
        "sources": _top(Counter(r.source for r in rows)),
        "pages": _top(Counter(
            {"shop": "Shop pages", "signup": "Seller sign-up page", "login": "Seller login page"}.get(r.page, r.page)
            for r in rows)),
        "shops": _top(Counter(names.get(r.business_id, f"#{r.business_id}") for r in rows if r.business_id)),
        "daily": [{"date": (today - timedelta(days=i)).isoformat(),
                   "count": by_day.get((today - timedelta(days=i)).isoformat(), 0)} for i in range(span - 1, -1, -1)],
    }


# ---- plans & billing -------------------------------------------------------------------------

@router.get("/billing")
def billing_overview(db: Session = Depends(get_db)):
    now = datetime.now(timezone.utc)
    rows, mrr, overdue, upcoming, by_plan = [], 0.0, [], [], Counter()
    for business in db.query(models.Business).order_by(models.Business.id).all():
        st = billing.status(db, business, now)
        by_plan[st["plan_name"]] += 1
        if st["plan"] in billing.PAID_PLANS and st["state"] in ("active", "due_soon"):
            mrr += billing.price(db, st["plan"]) * 30 / billing.PLANS[st["plan"]]["days"]
        row = {"id": business.id, "name": business.name, **st}
        rows.append(row)
        if st["state"] in ("overdue", "paused"):
            overdue.append(row)
        elif st["state"] == "due_soon" or (st["days_left"] is not None and 0 <= st["days_left"] <= 14 and not st["exempt"]):
            upcoming.append(row)
    invoices = db.query(models.PlanInvoice).order_by(models.PlanInvoice.id.desc()).limit(200).all()
    paid = [i for i in invoices if i.status == "paid"]
    return {
        "plans": billing.plan_list(db),
        "trial_days": billing.TRIAL_DAYS,
        "grace_days": billing.GRACE_DAYS,
        "mrr": round(mrr),
        "collected_30d": round(sum(i.amount for i in paid if _utc(i.paid_at) and _utc(i.paid_at) >= now - timedelta(days=30))),
        "collected_total": round(sum(i.amount for i in paid)),
        "pending_total": round(sum(i.amount for i in invoices if i.status == "pending")),
        "by_plan": _top(by_plan),
        "overdue": overdue,
        "upcoming": sorted(upcoming, key=lambda r: r["days_left"] or 0),
        "businesses": rows,
        "invoices": [billing.invoice_dict(i) for i in invoices],
    }


class PlanPrices(BaseModel):
    monthly: float
    quarterly: float
    annual: float


@router.put("/plans")
def set_prices(payload: PlanPrices, request: Request, db: Session = Depends(get_db),
               founder: models.FounderUser = Depends(current_founder)):
    for plan, amount in payload.model_dump().items():
        if amount < 1 or amount > 1_000_000:
            raise HTTPException(status_code=400, detail="Prices must be between ₹1 and ₹10,00,000")
        row = db.get(models.PlanPrice, plan) or models.PlanPrice(plan=plan, price=amount)
        row.price = round(amount, 2)
        db.merge(row)
    db.commit()
    audit(db, founder, "plan prices", None, ", ".join(f"{k} ₹{v:g}" for k, v in payload.model_dump().items()),
          client_ip(request))
    return billing.plan_list(db)


class PlanChange(BaseModel):
    plan: str | None = None
    paid_until: datetime | None = None
    extend_days: int | None = None
    billing_exempt: bool | None = None
    auto_pause: bool | None = None


@router.patch("/businesses/{business_id}/billing")
def change_billing(business_id: int, payload: PlanChange, request: Request, db: Session = Depends(get_db),
                   founder: models.FounderUser = Depends(current_founder)):
    business = _business(db, business_id)
    changes = []
    if payload.plan is not None:
        if payload.plan not in billing.PLANS or payload.plan == "founder":
            raise HTTPException(status_code=400, detail="Unknown plan")
        business.plan = payload.plan
        changes.append(f"plan {payload.plan}")
        if payload.plan == "free":
            business.billing_exempt = True
    if payload.paid_until is not None:
        business.paid_until = _utc(payload.paid_until)
        changes.append(f"paid until {business.paid_until:%d %b %Y}")
    if payload.extend_days:
        if not -366 <= payload.extend_days <= 366:
            raise HTTPException(status_code=400, detail="Extend by at most a year")
        start = max(_utc(business.paid_until) or datetime.now(timezone.utc), datetime.now(timezone.utc))
        business.paid_until = start + timedelta(days=payload.extend_days)
        changes.append(f"extended {payload.extend_days} days")
    if payload.billing_exempt is not None:
        business.billing_exempt = payload.billing_exempt
        changes.append("free account" if payload.billing_exempt else "paying account")
    if payload.auto_pause is not None:
        business.auto_pause = payload.auto_pause
        changes.append(f"auto-pause {'on' if payload.auto_pause else 'off'}")
    paid_until = _utc(business.paid_until)
    if (business.paused_reason == "billing" and not business.is_active
            and (business.billing_exempt or (paid_until and paid_until > datetime.now(timezone.utc)))):
        business.is_active, business.paused_reason = True, None
        changes.append("shop resumed")
    db.commit()
    audit(db, founder, "billing change", business.id, "; ".join(changes), client_ip(request))
    return {"id": business.id, **billing.status(db, business)}


class InvoiceIn(BaseModel):
    plan: str | None = None
    send: bool = True


@router.post("/businesses/{business_id}/invoice")
async def send_invoice(business_id: int, payload: InvoiceIn, request: Request, db: Session = Depends(get_db),
                       founder: models.FounderUser = Depends(current_founder)):
    business = _business(db, business_id)
    try:
        invoice = billing.create_invoice(db, business, payload.plan)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except billing.BillingUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    sent = False
    if payload.send:
        sent = await billing.seller_message(
            db, business, f"Your RIRAONE {billing.PLANS[invoice.plan]['name']} plan: ₹{invoice.amount:g}. Pay here:",
            f"{billing.PLANS[invoice.plan]['name']} plan payment of Rs {invoice.amount:g} is ready", invoice.link_url)
    audit(db, founder, "payment link", business.id, f"{invoice.plan} ₹{invoice.amount:g}" + (" (sent)" if sent else ""),
          client_ip(request))
    return {**billing.invoice_dict(invoice), "whatsapp_sent": sent}


class ManualPayment(BaseModel):
    plan: str
    note: str | None = None


@router.post("/businesses/{business_id}/payment")
async def record_payment(business_id: int, payload: ManualPayment, request: Request, db: Session = Depends(get_db),
                         founder: models.FounderUser = Depends(current_founder)):
    """Founder received the money another way (cash, bank transfer): record it and extend the plan."""
    business = _business(db, business_id)
    if payload.plan not in billing.PAID_PLANS:
        raise HTTPException(status_code=400, detail="Choose Monthly, Quarterly or Annual")
    for old in db.query(models.PlanInvoice).filter(models.PlanInvoice.business_id == business.id,
                                                   models.PlanInvoice.status == "pending").all():
        old.status = "cancelled"
    invoice = models.PlanInvoice(business_id=business.id, plan=payload.plan, amount=billing.price(db, payload.plan))
    db.add(invoice)
    db.commit()
    db.refresh(invoice)
    await billing.mark_paid(db, invoice, "manual", note=payload.note)
    audit(db, founder, "payment recorded", business.id, f"{payload.plan} ₹{invoice.amount:g} {payload.note or ''}".strip(),
          client_ip(request))
    return {"id": business.id, **billing.status(db, business)}


@router.post("/businesses/{business_id}/remind")
async def remind(business_id: int, request: Request, db: Session = Depends(get_db),
                 founder: models.FounderUser = Depends(current_founder)):
    business = _business(db, business_id)
    st = billing.status(db, business)
    link = billing.SELLER_DASHBOARD_URL
    try:
        link = billing.create_invoice(db, business).link_url or link
    except (billing.BillingUnavailable, ValueError):
        pass
    sent = await billing.seller_message(db, business, "A friendly reminder to renew your RIRAONE plan. Pay here:",
                                        "plan renewal is due" if st["state"] != "paused" else "shop paused - payment overdue",
                                        link)
    audit(db, founder, "billing reminder", business.id, "sent" if sent else "not delivered", client_ip(request))
    if not sent:
        raise HTTPException(status_code=502, detail="Couldn't deliver on WhatsApp - check their phone / alert number")
    return {"ok": True}


# ---- announcements ---------------------------------------------------------------------------

class AnnouncementIn(BaseModel):
    title: str
    body: str | None = None
    business_id: int | None = None
    level: str = "info"
    whatsapp: bool = False


@router.get("/announcements")
def list_announcements(db: Session = Depends(get_db)):
    names = _names(db)
    rows = db.query(models.Announcement).order_by(models.Announcement.id.desc()).limit(100).all()
    return [{"id": a.id, "title": a.title, "body": a.body, "level": a.level, "is_active": a.is_active,
             "business_id": a.business_id, "business_name": names.get(a.business_id) if a.business_id else "All sellers",
             "whatsapp_sent": a.whatsapp_sent, "created_at": _utc(a.created_at)} for a in rows]


@router.post("/announcements")
async def create_announcement(payload: AnnouncementIn, request: Request, db: Session = Depends(get_db),
                              founder: models.FounderUser = Depends(current_founder)):
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Write a title")
    if payload.business_id:
        _business(db, payload.business_id)
    a = models.Announcement(title=title[:200], body=(payload.body or "").strip() or None, business_id=payload.business_id,
                            level="warning" if payload.level == "warning" else "info")
    db.add(a)
    db.commit()
    if payload.whatsapp:
        targets = ([db.get(models.Business, payload.business_id)] if payload.business_id else
                   db.query(models.Business).filter(models.Business.id != DEFAULT_BUSINESS_ID,
                                                    models.Business.approval_status == "approved").all())
        for business in targets:
            text = title + (f"\n{a.body}" if a.body else "")
            if await billing.seller_message(db, business, text, " ".join(text.split())[:200], billing.SELLER_DASHBOARD_URL):
                a.whatsapp_sent += 1
        db.commit()
    audit(db, founder, "announcement", payload.business_id, title, client_ip(request))
    return {"id": a.id, "whatsapp_sent": a.whatsapp_sent}


class AnnouncementPatch(BaseModel):
    is_active: bool


@router.patch("/announcements/{announcement_id}")
def toggle_announcement(announcement_id: int, payload: AnnouncementPatch, db: Session = Depends(get_db)):
    a = db.get(models.Announcement, announcement_id)
    if not a:
        raise HTTPException(status_code=404, detail="Not found")
    a.is_active = payload.is_active
    db.commit()
    return {"id": a.id, "is_active": a.is_active}


# ---- founder activity & health ---------------------------------------------------------------

@router.get("/audit")
def audit_log(db: Session = Depends(get_db)):
    names = _names(db)
    rows = db.query(models.FounderAudit).order_by(models.FounderAudit.id.desc()).limit(300).all()
    return [{"id": r.id, "action": r.action, "business_id": r.business_id, "business_name": names.get(r.business_id),
             "detail": r.detail, "ip": r.ip, "created_at": _utc(r.created_at)} for r in rows]


@router.get("/health")
def health(db: Session = Depends(get_db)):
    from routers.admin import setup_steps

    now = datetime.now(timezone.utc)
    names = _names(db)
    warnings = []
    for b in db.query(models.Business).filter(models.Business.is_active.is_(True)).all():
        age = now - (_utc(b.created_at) or now)
        steps = setup_steps(db, b)
        if b.approval_status == "pending":
            warnings.append({"business_id": b.id, "name": b.name, "level": "info", "text": "Waiting for your approval"})
        if b.wa_health == "error":
            warnings.append({"business_id": b.id, "name": b.name, "level": "error",
                             "text": f"WhatsApp not working: {b.wa_health_detail}"})
        elif not steps["whatsapp"] and age > timedelta(days=3):
            warnings.append({"business_id": b.id, "name": b.name, "level": "warning",
                             "text": f"WhatsApp still not connected after {age.days} days"})
        if sum(steps.values()) < len(steps) and age > timedelta(days=7):
            missing = ", ".join(k for k, done in steps.items() if not done)
            warnings.append({"business_id": b.id, "name": b.name, "level": "warning",
                             "text": f"Setup unfinished after {age.days} days (missing: {missing})"})
    events = db.query(models.SystemEvent).filter(models.SystemEvent.created_at >= now - timedelta(days=7)) \
        .order_by(models.SystemEvent.id.desc()).limit(200).all()
    return {
        "warnings": warnings,
        "events": [{"id": e.id, "source": e.source, "business_id": e.business_id, "business_name": names.get(e.business_id),
                    "message": e.message, "created_at": _utc(e.created_at)} for e in events],
        "event_counts": _top(Counter(e.source for e in events)),
    }
