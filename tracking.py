"""Who signs in, who visits, and alerts to the founder.

Country/city come from the visitor's IP via a free lookup service (ipwho.is, with ip-api.com as a
fallback), cached in memory. Lookups run after the response is sent, so pages never wait on them.
Visits keep only the resolved place, never the IP; sign-ins keep the IP (security trail).
Set GEO_LOOKUP=0 to switch lookups off."""
import ipaddress
import os
import re
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import Request

import models
from database import SessionLocal

GEO_LOOKUP = os.getenv("GEO_LOOKUP", "1") != "0"
FOUNDER_ALERT_TEMPLATE = os.getenv("WA_TEMPLATE_FOUNDER_ALERT", "founder_alert")
FAILED_LOGIN_ALERT_AT = 5  # failed attempts on one email within 15 minutes
_geo_cache: OrderedDict[str, tuple[str | None, str | None, str | None]] = OrderedDict()


def client_ip(request: Request) -> str | None:
    """Render puts the real client first in X-Forwarded-For."""
    forwarded = request.headers.get("x-forwarded-for", "")
    ip = forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else None)
    return ip or None


def device_label(user_agent: str | None) -> str:
    ua = user_agent or ""
    if not ua:
        return "Unknown"
    kind = "Tablet" if re.search(r"iPad|Tablet", ua) else "Mobile" if re.search(r"Mobi|Android|iPhone", ua) else "Desktop"
    os_name = next((name for pattern, name in [
        (r"Android", "Android"), (r"iPhone|iPad|iOS", "iOS"), (r"Windows", "Windows"),
        (r"Mac OS X|Macintosh", "Mac"), (r"CrOS", "ChromeOS"), (r"Linux", "Linux"),
    ] if re.search(pattern, ua)), "Other")
    browser = next((name for pattern, name in [
        (r"WhatsApp", "WhatsApp"), (r"FBAN|FBAV|Instagram", "Facebook/Instagram app"), (r"Edg/", "Edge"),
        (r"OPR/|Opera", "Opera"), (r"SamsungBrowser", "Samsung Internet"), (r"Chrome/", "Chrome"),
        (r"Firefox/", "Firefox"), (r"Safari/", "Safari"),
    ] if re.search(pattern, ua)), "Other")
    return f"{kind} · {os_name} · {browser}"


def traffic_source(referrer: str | None, utm: str | None = None, user_agent: str | None = None) -> str:
    if utm:
        return utm[:40]
    ua = user_agent or ""
    if "WhatsApp" in ua:
        return "WhatsApp"
    if "Instagram" in ua:
        return "Instagram"
    if "FBAN" in ua or "FBAV" in ua:
        return "Facebook"
    host = re.sub(r"^https?://(www\.)?", "", referrer or "").split("/")[0].lower()
    if not host:
        return "Direct / WhatsApp"
    for needle, name in [("whatsapp", "WhatsApp"), ("wa.me", "WhatsApp"), ("instagram", "Instagram"),
                         ("facebook", "Facebook"), ("google", "Google"), ("youtube", "YouTube"), ("t.co", "Twitter/X")]:
        if needle in host:
            return name
    return host[:60]


def _is_public(ip: str | None) -> bool:
    try:
        return ipaddress.ip_address(ip).is_global
    except (TypeError, ValueError):
        return False


async def geo_lookup(ip: str | None) -> tuple[str | None, str | None, str | None]:
    """(country, country_code, city) - all None when unknown."""
    if not GEO_LOOKUP or not _is_public(ip):
        return None, None, None
    if ip in _geo_cache:
        _geo_cache.move_to_end(ip)
        return _geo_cache[ip]
    result = (None, None, None)
    try:
        async with httpx.AsyncClient(timeout=4) as client:
            r = await client.get(f"https://ipwho.is/{ip}", params={"fields": "success,country,country_code,city"})
            data = r.json()
            if data.get("success"):
                result = (data.get("country"), data.get("country_code"), data.get("city"))
            else:
                r = await client.get(f"http://ip-api.com/json/{ip}", params={"fields": "status,country,countryCode,city"})
                data = r.json()
                if data.get("status") == "success":
                    result = (data.get("country"), data.get("countryCode"), data.get("city"))
    except (httpx.HTTPError, ValueError) as e:
        print("GEO LOOKUP FAILED:", repr(e))
        return result  # don't cache failures
    _geo_cache[ip] = result
    if len(_geo_cache) > 5000:
        _geo_cache.popitem(last=False)
    return result


def request_info(request: Request) -> dict:
    """What to note about a request now, while it's available (the background task can't read it)."""
    return {"ip": client_ip(request), "device": device_label(request.headers.get("user-agent"))}


# ---- background recorders --------------------------------------------------------------------

async def record_visit(info: dict, page: str, business_id: int | None, source: str):
    country, code, city = await geo_lookup(info.get("ip"))
    with SessionLocal() as db:
        db.add(models.Visit(page=page, business_id=business_id, country=country, country_code=code, city=city,
                            device=info.get("device"), source=source))
        db.commit()


async def record_login(info: dict, site: str, email: str | None, success: bool,
                       business_id: int | None = None, staff_id: int | None = None):
    """Store the attempt, then alert the founder about anything suspicious."""
    country, code, city = await geo_lookup(info.get("ip"))
    with SessionLocal() as db:
        if success and business_id and code:
            seen_before = db.query(models.LoginEvent).filter(
                models.LoginEvent.site == site, models.LoginEvent.business_id == business_id,
                models.LoginEvent.success.is_(True), models.LoginEvent.country_code.isnot(None)).all()
            new_country = bool(seen_before) and code not in {e.country_code for e in seen_before}
        else:
            new_country = False
        db.add(models.LoginEvent(site=site, business_id=business_id, staff_id=staff_id, email=email, success=success,
                                 ip=info.get("ip"), country=country, country_code=code, city=city,
                                 device=info.get("device")))
        db.commit()
        place = ", ".join(p for p in (city, country) if p) or "unknown place"
        if new_country:
            await founder_alert(db, f"Login from a NEW country: {email} signed in from {place} ({info.get('device')})")
        if not success and email:
            since = datetime.now(timezone.utc) - timedelta(minutes=15)
            fails = db.query(models.LoginEvent).filter(
                models.LoginEvent.email == email, models.LoginEvent.success.is_(False),
                models.LoginEvent.created_at >= since).count()
            if fails == FAILED_LOGIN_ALERT_AT:
                await founder_alert(db, f"{fails} wrong passwords in 15 minutes for {email} on the {site} login, "
                                        f"last from {place}")


def log_event(source: str, message: str, business_id: int | None = None):
    """Note a problem for the founder console's health list. Never raises."""
    try:
        with SessionLocal() as db:
            db.add(models.SystemEvent(source=source, message=message[:2000], business_id=business_id))
            db.commit()
    except Exception as e:  # noqa: BLE001 - logging must never break the caller
        print("SYSTEM EVENT NOT SAVED:", repr(e), message)


def audit(db, founder: models.FounderUser | None, action: str, business_id: int | None = None,
          detail: str | None = None, ip: str | None = None):
    db.add(models.FounderAudit(founder_id=founder.id if founder else None, action=action,
                               business_id=business_id, detail=detail, ip=ip))
    db.commit()


# ---- founder alerts ----------------------------------------------------------------------------

def founder_phone(db) -> str | None:
    founder = db.query(models.FounderUser).order_by(models.FounderUser.id).first()
    phone = (founder.alert_phone if founder else None) or os.getenv("FOUNDER_ALERT_PHONE")
    digits = re.sub(r"\D", "", phone or "")
    return ("91" + digits if len(digits) == 10 else digits) or None


async def founder_alert(db, text: str) -> bool:
    """WhatsApp the founder from RIRAONE's own number (business 1). Free text inside the 24h window,
    otherwise the approved `founder_alert` template. Never raises."""
    from business import DEFAULT_BUSINESS_ID, whatsapp_creds
    from seller_alerts import _seller_window_open
    from whatsapp import WhatsAppSendError, send_message, send_template

    phone = founder_phone(db)
    rira = db.get(models.Business, DEFAULT_BUSINESS_ID)
    if not phone or not rira:
        return False
    try:
        if _seller_window_open(db, rira, phone):
            await send_message(whatsapp_creds(rira), phone, f"RIRAONE founder alert\n{text}")
        else:
            await send_template(whatsapp_creds(rira), phone, FOUNDER_ALERT_TEMPLATE, "en", [" ".join(text.split())[:900]])
        return True
    except WhatsAppSendError as e:
        print("FOUNDER ALERT NOT DELIVERED:", e)
        return False
