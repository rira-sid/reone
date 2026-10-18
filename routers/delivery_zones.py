"""Settings -> Delivery zones: where the shop delivers and on what terms (fee, COD, minimum, delivery time).
The bot and the checkout use these; anything a zone leaves blank comes from the shop's general rules."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

import models
import zones
from auth import current_business, owner_business
from database import get_db

router = APIRouter(prefix="/business/zones", tags=["delivery zones"])


class ZoneIn(BaseModel):
    name: str
    match_type: str = "pincode"
    match_values: str | None = None
    delivers: bool = True
    days_min: int | None = None
    days_max: int | None = None
    fee: float | None = None
    free_above: float | None = None
    min_order: float | None = None
    cod: bool | None = None
    note: str | None = None


def _check(db: Session, business: models.Business, payload: ZoneIn, zone_id: int | None = None) -> dict:
    data = payload.model_dump()
    data["name"] = data["name"].strip()
    if not data["name"]:
        raise HTTPException(status_code=400, detail="Give the zone a name, e.g. Coimbatore city")
    if data["match_type"] not in zones.MATCH_TYPES:
        raise HTTPException(status_code=400, detail="Zone type must be PIN code, city, state or everywhere else")
    if data["match_type"] == "rest":
        data["match_values"] = None
        other = db.query(models.DeliveryZone).filter(
            models.DeliveryZone.business_id == business.id, models.DeliveryZone.match_type == "rest",
            models.DeliveryZone.id != (zone_id or 0)).first()
        if other:
            raise HTTPException(status_code=409, detail=f"'{other.name}' already covers everywhere else")
    else:
        listed = [v.strip() for v in (data["match_values"] or "").replace("\n", ",").split(",") if v.strip()]
        if data["match_type"] == "pincode":
            if not listed or any(not v.replace(" ", "").isdigit() for v in listed):
                raise HTTPException(status_code=400, detail="Enter PIN codes or their first digits, e.g. 641, 642001")
            listed = [v.replace(" ", "") for v in listed]
        elif not listed:
            raise HTTPException(status_code=400, detail="Enter at least one name")
        data["match_values"] = ", ".join(listed)
    for field in ("days_min", "days_max"):
        if data[field] is not None and not 0 <= data[field] <= 60:
            raise HTTPException(status_code=400, detail="Delivery time must be between 0 and 60 days")
    for field in ("fee", "free_above", "min_order"):
        if data[field] is not None and data[field] < 0:
            raise HTTPException(status_code=400, detail="Amounts can't be negative")
    data["note"] = (data["note"] or "").strip() or None
    return data


@router.get("")
def list_zones(db: Session = Depends(get_db), business: models.Business = Depends(current_business)):
    return [zones.zone_dict(z) for z in zones.business_zones(db, business)]


@router.post("")
def add_zone(payload: ZoneIn, db: Session = Depends(get_db), business: models.Business = Depends(owner_business)):
    data = _check(db, business, payload)
    count = db.query(models.DeliveryZone).filter(models.DeliveryZone.business_id == business.id).count()
    zone = models.DeliveryZone(business_id=business.id, position=count, **data)
    db.add(zone)
    db.commit()
    db.refresh(zone)
    return zones.zone_dict(zone)


def _get(db: Session, business: models.Business, zone_id: int) -> models.DeliveryZone:
    zone = db.query(models.DeliveryZone).filter(models.DeliveryZone.id == zone_id,
                                                models.DeliveryZone.business_id == business.id).first()
    if not zone:
        raise HTTPException(status_code=404, detail="Zone not found")
    return zone


@router.put("/{zone_id}")
def update_zone(zone_id: int, payload: ZoneIn, db: Session = Depends(get_db),
                business: models.Business = Depends(owner_business)):
    zone = _get(db, business, zone_id)
    for key, value in _check(db, business, payload, zone_id).items():
        setattr(zone, key, value)
    db.commit()
    return zones.zone_dict(zone)


@router.delete("/{zone_id}")
def delete_zone(zone_id: int, db: Session = Depends(get_db), business: models.Business = Depends(owner_business)):
    db.delete(_get(db, business, zone_id))
    db.commit()
    return {"ok": True}


@router.get("/check")
def check_address(pincode: str | None = None, address: str | None = None, db: Session = Depends(get_db),
                  business: models.Business = Depends(current_business)):
    """'Try an address' in Settings: which zone and terms a customer there would get."""
    terms = zones.terms_for(db, business, pincode, address)
    return {"status": terms.status, "zone": terms.zone.name if terms.zone else None, "fee": terms.fee,
            "free_above": terms.free_above, "min_order": terms.min_order, "cod": terms.cod,
            "days": list(terms.days) if terms.days else None, "note": terms.note}
