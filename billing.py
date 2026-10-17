"""RIRAONE plans: what each seller pays the founder - Monthly, Quarterly or Annual.

Every new business starts on a free trial (TRIAL_DAYS). `paid_until` is when the trial or the last
paid period ends. The founder sets the prices in the console (PlanPrice rows; defaults below). Payment is a Razorpay payment link created with RIRAONE's own Razorpay account
(business 1's keys); its webhook (`payment_link.paid`, reference "SUB-<invoice id>") extends the
plan by its period automatically. The founder can also mark a payment received by hand.

Reminders go to the seller's WhatsApp from RIRAONE's number: 3 days before, on the day, 3 days
after. After GRACE_DAYS unpaid the shop pauses (if auto_pause), and resumes the moment they pay."""
import os
from datetime import datetime, timedelta, timezone

import httpx

import models
from business import DEFAULT_BUSINESS_ID, razorpay_creds, whatsapp_creds
from tracking import founder_alert, log_event

PLANS = {
    "trial": {"name": "Free trial", "price": 0, "days": 0},
    "monthly": {"name": "Monthly", "price": 499, "days": 30},
    "quarterly": {"name": "Quarterly", "price": 1349, "days": 90},
    "annual": {"name": "Annual", "price": 4999, "days": 365},
    "free": {"name": "Free (pilot)", "price": 0, "days": 0},
    "founder": {"name": "RIRAONE's own shop", "price": 0, "days": 0},
}
PAID_PLANS = ("monthly", "quarterly", "annual")
TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "14"))
GRACE_DAYS = int(os.getenv("BILLING_GRACE_DAYS", "7"))
REMIND_BEFORE_DAYS = 3
OVERDUE_REMIND_DAYS = 3
BILLING_TEMPLATE = os.getenv("WA_TEMPLATE_BILLING", "billing_reminder")
WA_HEALTH_EVERY = timedelta(hours=6)
SELLER_DASHBOARD_URL = os.getenv("SELLER_DASHBOARD_URL", "https://reone-dashboard.vercel.app")


class BillingUnavailable(Exception):
    pass


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def start_trial(business: models.Business, source: str, now: datetime | None = None):
    now = now or datetime.now(timezone.utc)
    business.signup_source = source
    business.plan = "trial"
    business.trial_ends_at = business.paid_until = now + timedelta(days=TRIAL_DAYS)


def backfill(db):
    """Businesses from before billing existed: RIRAONE's own shop is free, the rest get a trial from today."""
    for business in db.query(models.Business).filter(models.Business.plan.is_(None)).all():
        if business.id == DEFAULT_BUSINESS_ID:
            business.plan, business.billing_exempt = "founder", True
        else:
            start_trial(business, business.signup_source or "invite")
    db.commit()


def price(db, plan: str) -> float:
    row = db.get(models.PlanPrice, plan)
    return row.price if row else PLANS.get(plan, {"price": 0})["price"]


def plan_list(db) -> list[dict]:
    monthly = price(db, "monthly")
    rows = []
    for key in PAID_PLANS:
        amount, days = price(db, key), PLANS[key]["days"]
        full = monthly * days / 30
        rows.append({"id": key, "name": PLANS[key]["name"], "price": amount, "days": days,
                     "per_month": round(amount * 30 / days), "saving_pct": round((1 - amount / full) * 100) if full else 0})
    return rows


def status(db, business: models.Business, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    plan = business.plan or "trial"
    paid_until = _utc(business.paid_until)
    days_left = None if paid_until is None else (paid_until - now).total_seconds() / 86400
    if business.billing_exempt or plan in ("free", "founder"):
        state = "exempt"
    elif not business.is_active and business.paused_reason == "billing":
        state = "paused"
    elif days_left is None or days_left < 0:
        state = "overdue"
    elif days_left <= REMIND_BEFORE_DAYS:
        state = "due_soon"
    else:
        state = "trial" if plan == "trial" else "active"
    pending = db.query(models.PlanInvoice).filter(
        models.PlanInvoice.business_id == business.id, models.PlanInvoice.status == "pending"
    ).order_by(models.PlanInvoice.id.desc()).first()
    return {
        "plan": plan,
        "plan_name": PLANS.get(plan, {"name": plan})["name"],
        "price": price(db, plan),
        "next_plan": plan if plan in PAID_PLANS else "monthly",
        "paid_until": paid_until,
        "trial_ends_at": _utc(business.trial_ends_at),
        "days_left": None if days_left is None else round(days_left, 1),
        "state": state,
        "exempt": state == "exempt",
        "auto_pause": business.auto_pause,
        "grace_days": GRACE_DAYS,
        "pending_invoice": invoice_dict(pending) if pending else None,
        "plans": plan_list(db),
    }


def invoice_dict(invoice: models.PlanInvoice) -> dict:
    return {
        "id": invoice.id, "business_id": invoice.business_id,
        "business_name": invoice.business.name if invoice.business else None,
        "plan": invoice.plan, "plan_name": PLANS.get(invoice.plan, {"name": invoice.plan})["name"],
        "amount": invoice.amount, "status": invoice.status, "method": invoice.method,
        "link_url": invoice.link_url, "note": invoice.note,
        "paid_at": _utc(invoice.paid_at), "created_at": _utc(invoice.created_at),
    }


def _razorpay_link(db, invoice: models.PlanInvoice, business: models.Business) -> dict:
    """Payment link on RIRAONE's own Razorpay account. Separate function so tests can replace it."""
    import razorpay

    creds = razorpay_creds(db.get(models.Business, DEFAULT_BUSINESS_ID))
    if not creds.key_id or not creds.key_secret:
        raise BillingUnavailable("RIRAONE's Razorpay account isn't connected (business 1 → Settings → Payments)")
    client = razorpay.Client(auth=(creds.key_id, creds.key_secret))
    customer = {"name": business.name}
    if business.alert_phone or business.phone:
        customer["contact"] = business.alert_phone or business.phone
    if business.owner_email:
        customer["email"] = business.owner_email
    return client.payment_link.create({
        "amount": round(invoice.amount * 100), "currency": "INR", "accept_partial": False,
        "description": f"RIRAONE {PLANS[invoice.plan]['name']} plan ({PLANS[invoice.plan]['days']} days)",
        "customer": customer, "notify": {"sms": False, "email": bool(business.owner_email)},
        "reference_id": f"SUB-{invoice.id}",
    })


def create_invoice(db, business: models.Business, plan: str | None = None) -> models.PlanInvoice:
    """The open charge for this business's next month, with a payment link. Reuses a pending one."""
    plan = plan or status(db, business)["next_plan"]
    if plan not in PAID_PLANS:
        raise ValueError("Choose Monthly, Quarterly or Annual")
    pending = db.query(models.PlanInvoice).filter(
        models.PlanInvoice.business_id == business.id, models.PlanInvoice.status == "pending").all()
    for old in pending:
        if old.plan == plan and old.link_url and old.amount == price(db, plan):
            return old
        old.status = "cancelled"  # switched plan - the old link is no longer the one to pay
    invoice = models.PlanInvoice(business_id=business.id, plan=plan, amount=price(db, plan))
    db.add(invoice)
    db.commit()
    try:
        link = _razorpay_link(db, invoice, business)
    except BillingUnavailable:
        db.delete(invoice)
        db.commit()
        raise
    except Exception as e:  # Razorpay down / rejected
        db.delete(invoice)
        db.commit()
        log_event("billing", f"Could not create plan payment link: {e!r}", business.id)
        raise BillingUnavailable("Could not create the payment link - please try again") from e
    invoice.razorpay_link_id, invoice.link_url, invoice.method = link["id"], link["short_url"], "razorpay"
    db.commit()
    return invoice


async def mark_paid(db, invoice: models.PlanInvoice, method: str, payment_id: str | None = None,
                    note: str | None = None, now: datetime | None = None):
    now = now or datetime.now(timezone.utc)
    if invoice.status == "paid":
        return
    business = invoice.business
    invoice.status, invoice.method, invoice.paid_at = "paid", method, now
    invoice.razorpay_payment_id = payment_id
    if note:
        invoice.note = note
    start = max(_utc(business.paid_until) or now, now)
    business.paid_until = start + timedelta(days=PLANS[invoice.plan]["days"])
    business.plan = invoice.plan
    resumed = False
    if not business.is_active and business.paused_reason == "billing":
        business.is_active, business.paused_reason, resumed = True, None, True
    db.commit()
    until = _utc(business.paid_until).strftime("%d %b %Y")
    await seller_message(db, business, f"Payment received - thank you! Your {PLANS[invoice.plan]['name']} plan is "
                                       f"active until {until}." + (" Your shop is live again." if resumed else ""),
                         "payment received, thank you", SELLER_DASHBOARD_URL)
    await founder_alert(db, f"{business.name} paid ₹{invoice.amount:g} ({PLANS[invoice.plan]['name']}, {method}). "
                            f"Active until {until}.")


async def handle_link_paid(db, event: dict) -> dict:
    """payment_link.paid on RIRAONE's Razorpay account for a plan charge."""
    link = event["payload"]["payment_link"]["entity"]
    payment = event["payload"]["payment"]["entity"]
    try:
        invoice_id = int(str(link.get("reference_id", "")).removeprefix("SUB-"))
    except ValueError:
        return {"status": "ignored"}
    invoice = db.query(models.PlanInvoice).filter(models.PlanInvoice.id == invoice_id).with_for_update().first()
    if not invoice:
        return {"status": "ignored"}
    if invoice.status == "paid":
        return {"status": "already processed"}
    if abs(payment["amount"] / 100 - invoice.amount) > 0.01:
        log_event("billing", f"Plan invoice {invoice.id}: paid {payment['amount'] / 100} but expected {invoice.amount}",
                  invoice.business_id)
        return {"status": "amount mismatch"}
    await mark_paid(db, invoice, "razorpay", payment.get("id"))
    return {"status": "ok"}


# ---- messages to sellers --------------------------------------------------------------------

def _seller_phone(business: models.Business) -> str | None:
    from seller_alerts import _digits

    return _digits(business.alert_phone or business.phone) or None


async def seller_message(db, business: models.Business, text: str, template_status: str, link: str) -> bool:
    """WhatsApp the seller from RIRAONE's number. Inside the 24h window: free text; otherwise the
    `billing_reminder` template (name, status line, link). Never raises."""
    from seller_alerts import _seller_window_open
    from whatsapp import WhatsAppSendError, send_message, send_template

    phone = _seller_phone(business)
    rira = db.get(models.Business, DEFAULT_BUSINESS_ID)
    if not phone or not rira or business.id == DEFAULT_BUSINESS_ID:
        return False
    try:
        if _seller_window_open(db, rira, phone):
            await send_message(whatsapp_creds(rira), phone, f"RIRAONE · {business.name}\n{text}\n{link}")
        else:
            await send_template(whatsapp_creds(rira), phone, BILLING_TEMPLATE, "en",
                                [business.name[:60], template_status[:200], link])
        return True
    except WhatsAppSendError as e:
        log_event("billing", f"Billing message to {business.name} not delivered: {e}", business.id)
        return False


def _notice_sent(db, business: models.Business, period: str, stage: str) -> bool:
    if db.query(models.BillingNotice).filter(
            models.BillingNotice.business_id == business.id, models.BillingNotice.period == period,
            models.BillingNotice.stage == stage).first():
        return True
    db.add(models.BillingNotice(business_id=business.id, period=period, stage=stage))
    db.commit()
    return False


async def billing_tick(db, now: datetime | None = None) -> int:
    """Send the reminders that are due and pause shops past the grace period. Returns actions taken."""
    now = now or datetime.now(timezone.utc)
    actions = 0
    businesses = db.query(models.Business).filter(
        models.Business.billing_exempt.is_(False), models.Business.paid_until.isnot(None),
        models.Business.approval_status == "approved").all()
    for business in businesses:
        if business.plan in ("free", "founder") or business.id == DEFAULT_BUSINESS_ID:
            continue
        if not business.is_active and business.paused_reason != "billing":
            continue  # paused by the founder - leave it alone
        paid_until = _utc(business.paid_until)
        period = paid_until.date().isoformat()
        days_left = (paid_until - now).total_seconds() / 86400
        if days_left > REMIND_BEFORE_DAYS:
            continue
        if days_left > 0:
            stage = "soon"
        elif days_left > -OVERDUE_REMIND_DAYS:
            stage = "due"
        elif days_left > -GRACE_DAYS:
            stage = "overdue"
        else:
            stage = "paused"
        if stage == "paused" and (not business.auto_pause or not business.is_active):
            continue
        if _notice_sent(db, business, period, stage):
            continue
        link = SELLER_DASHBOARD_URL
        try:
            link = create_invoice(db, business).link_url or link
        except (BillingUnavailable, ValueError) as e:
            print(f"BILLING: no payment link for business {business.id}: {e}")
        what = "free trial" if business.plan == "trial" else f"{PLANS.get(business.plan, {}).get('name', '')} plan"
        date = paid_until.strftime("%d %b")
        if stage == "soon":
            text = f"Your RIRAONE {what} ends on {date}. Pay now to keep your shop running without a break:"
            line = f"{what} ends on {date}"
        elif stage == "due":
            text = f"Your RIRAONE {what} ended on {date}. Please pay to continue:"
            line = f"{what} payment is due"
        elif stage == "overdue":
            pause_on = (paid_until + timedelta(days=GRACE_DAYS)).strftime("%d %b")
            text = (f"Reminder: your RIRAONE payment is overdue." +
                    (f" Your shop will pause on {pause_on} if unpaid." if business.auto_pause else "") + " Pay here:")
            line = "payment is overdue" + (f", shop pauses on {pause_on}" if business.auto_pause else "")
        else:
            business.is_active, business.paused_reason = False, "billing"
            db.commit()
            text = "Your RIRAONE shop is paused because the plan wasn't renewed. Pay to switch it back on instantly:"
            line = "shop paused - payment overdue"
            await founder_alert(db, f"{business.name} was auto-paused: unpaid since {date}.")
        await seller_message(db, business, text, line, link)
        actions += 1
    return actions


# ---- WhatsApp connection health ----------------------------------------------------------------

async def check_whatsapp_health(db, now: datetime | None = None) -> int:
    """Ask Meta whether each seller's WhatsApp number + token still work (an expired or revoked token
    silently stops their bot). Each business is checked at most every 6 hours."""
    from whatsapp import GRAPH_BASE

    now = now or datetime.now(timezone.utc)
    checked = 0
    for business in db.query(models.Business).filter(models.Business.is_active.is_(True)).all():
        last = _utc(business.wa_health_checked_at)
        if last and now - last < WA_HEALTH_EVERY:
            continue
        creds = whatsapp_creds(business)
        business.wa_health_checked_at = now
        if not creds:
            business.wa_health, business.wa_health_detail = None, "Not connected"
            db.commit()
            continue
        previous = business.wa_health
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(f"{GRAPH_BASE}/{creds.phone_number_id}",
                                     params={"fields": "display_phone_number,verified_name,quality_rating"},
                                     headers={"Authorization": f"Bearer {creds.token}"})
            if r.status_code < 400:
                data = r.json()
                business.wa_health = "ok"
                business.wa_health_detail = (f"{data.get('verified_name', '')} {data.get('display_phone_number', '')}"
                                             f" · quality {data.get('quality_rating', '?')}").strip()
            else:
                try:
                    detail = r.json()["error"]["message"]
                except (ValueError, KeyError, TypeError):
                    detail = r.text[:300]
                business.wa_health, business.wa_health_detail = "error", detail
        except httpx.HTTPError as e:
            print("WA HEALTH CHECK FAILED:", repr(e))  # network blip - not the seller's fault
            continue
        db.commit()
        checked += 1
        if business.wa_health == "error" and previous != "error":
            log_event("whatsapp", f"WhatsApp connection broken: {business.wa_health_detail}", business.id)
            await founder_alert(db, f"{business.name}: WhatsApp stopped working - {business.wa_health_detail}")
    return checked
