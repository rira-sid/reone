"""Delivery zones: which terms (fee, COD, minimum order, delivery time) apply to a customer's address.

Matching order: PIN code prefix (longest wins) > city named in the address > state (named in the
address, or worked out from the PIN code) > the "everywhere else" zone. A shop with no zones uses its
general order rules everywhere. A shop with zones but none that fits -> "unknown": the owner is asked
instead of charging the customer a guess."""
import re
from dataclasses import dataclass

import models

# First digits of an Indian PIN code -> state, for state zones when the address doesn't name it.
# Longer prefixes win (Goa sits inside Maharashtra's 40x, Puducherry inside Tamil Nadu's 60x).
PIN_STATE = {
    "11": "Delhi", "12": "Haryana", "13": "Haryana", "14": "Punjab", "15": "Punjab", "160": "Chandigarh",
    "16": "Punjab", "17": "Himachal Pradesh", "18": "Jammu and Kashmir", "19": "Jammu and Kashmir",
    "20": "Uttar Pradesh", "21": "Uttar Pradesh", "22": "Uttar Pradesh", "23": "Uttar Pradesh",
    "27": "Uttar Pradesh", "28": "Uttar Pradesh", "30": "Rajasthan", "31": "Rajasthan", "32": "Rajasthan",
    "33": "Rajasthan", "34": "Rajasthan", "36": "Gujarat", "37": "Gujarat", "38": "Gujarat", "39": "Gujarat",
    "403": "Goa", "40": "Maharashtra", "41": "Maharashtra", "42": "Maharashtra", "43": "Maharashtra",
    "44": "Maharashtra", "45": "Madhya Pradesh", "46": "Madhya Pradesh", "47": "Madhya Pradesh",
    "48": "Madhya Pradesh", "49": "Chhattisgarh", "50": "Telangana", "51": "Andhra Pradesh",
    "52": "Andhra Pradesh", "53": "Andhra Pradesh", "56": "Karnataka", "57": "Karnataka", "58": "Karnataka",
    "59": "Karnataka", "605": "Puducherry", "60": "Tamil Nadu", "61": "Tamil Nadu", "62": "Tamil Nadu",
    "63": "Tamil Nadu", "64": "Tamil Nadu", "67": "Kerala", "68": "Kerala", "69": "Kerala",
    "737": "Sikkim", "744": "Andaman and Nicobar", "70": "West Bengal", "71": "West Bengal",
    "72": "West Bengal", "73": "West Bengal", "74": "West Bengal", "75": "Odisha", "76": "Odisha",
    "77": "Odisha", "78": "Assam", "80": "Bihar", "84": "Bihar", "85": "Bihar", "82": "Jharkhand",
    "83": "Jharkhand",
}
STATE_ALIASES = {"tn": "tamil nadu", "tamilnadu": "tamil nadu", "kerela": "kerala", "pondicherry": "puducherry",
                 "orissa": "odisha", "bombay": "mumbai", "madras": "chennai", "bangalore": "bengaluru"}
MATCH_TYPES = ("pincode", "city", "state", "rest")


@dataclass
class Terms:
    """What applies to one order: from the zone, else the shop's general rules."""
    status: str  # "ok" | "no_delivery" | "unknown"
    zone: models.DeliveryZone | None
    fee: float
    free_above: float | None
    min_order: float | None
    cod: bool
    days: tuple[int, int] | None
    note: str | None = None

    def fee_for(self, subtotal: float) -> float:
        if not self.fee or (self.free_above and subtotal >= self.free_above):
            return 0.0
        return self.fee


def values(zone: models.DeliveryZone) -> list[str]:
    return [v.strip() for v in re.split(r"[,\n;]+", zone.match_values or "") if v.strip()]


def clean_pincode(text: str | None) -> str | None:
    digits = re.sub(r"\D", "", text or "")
    return digits if 3 <= len(digits) <= 10 else None


def pincode_in(address: str | None) -> str | None:
    """A 6-digit Indian PIN code written inside the address ("641001" or "641 001"), if any."""
    m = re.search(r"(?<!\d)(\d{3})\s?(\d{3})(?!\d)", address or "")
    return m[1] + m[2] if m else None


def state_for_pincode(pincode: str | None) -> str | None:
    if not pincode:
        return None
    for length in (3, 2):
        if pincode[:length] in PIN_STATE:
            return PIN_STATE[pincode[:length]]
    return None


def _norm(text: str) -> str:
    """Lower case, no punctuation, common old/short names replaced, padded with spaces for whole-word search."""
    words = re.sub(r"[^\w\s]", " ", (text or "").lower()).split()
    return " " + " ".join(STATE_ALIASES.get(w, w) for w in words) + " "


def _mentions(text_norm: str, names: list[str]) -> bool:
    return any(_norm(n).strip() and _norm(n) in text_norm for n in names)


def business_zones(db, business: models.Business) -> list[models.DeliveryZone]:
    return db.query(models.DeliveryZone).filter(models.DeliveryZone.business_id == business.id).order_by(
        models.DeliveryZone.position, models.DeliveryZone.id).all()


def find_zone(zones: list[models.DeliveryZone], pincode: str | None, address: str | None):
    """The zone for this address, or None if no zone fits."""
    pincode = clean_pincode(pincode) or pincode_in(address)
    best, best_len = None, 0
    for zone in zones:
        if zone.match_type == "pincode" and pincode:
            for prefix in values(zone):
                prefix = re.sub(r"\D", "", prefix)
                if prefix and pincode.startswith(prefix) and len(prefix) > best_len:
                    best, best_len = zone, len(prefix)
    if best:
        return best
    address_norm = _norm(address or "")
    for zone in zones:
        if zone.match_type == "city" and _mentions(address_norm, values(zone)):
            return zone
    state = _norm(state_for_pincode(pincode) or "")
    for zone in zones:
        if zone.match_type == "state" and (_mentions(address_norm, values(zone)) or _mentions(state, values(zone))):
            return zone
    return next((z for z in zones if z.match_type == "rest"), None)


def _days(low, high) -> tuple[int, int] | None:
    if low is None and high is None:
        return None
    low = high if low is None else low
    high = low if high is None else high
    return min(low, high), max(low, high)


def general_terms(business: models.Business) -> Terms:
    return Terms("ok", None, business.delivery_fee or 0.0, business.free_delivery_above,
                 business.min_order_amount, business.cod_enabled,
                 _days(business.delivery_days_min, business.delivery_days_max))


def zone_terms(business: models.Business, zone: models.DeliveryZone) -> Terms:
    """A zone's terms; anything left blank comes from the shop's general rules."""
    if not zone.delivers:
        return Terms("no_delivery", zone, 0.0, None, None, False, None, zone.note)
    general = general_terms(business)
    return Terms(
        "ok", zone,
        zone.fee if zone.fee is not None else general.fee,
        zone.free_above if zone.free_above is not None else general.free_above,
        zone.min_order if zone.min_order is not None else general.min_order,
        business.cod_enabled and (zone.cod if zone.cod is not None else True),
        _days(zone.days_min, zone.days_max) or general.days,
        zone.note,
    )


def terms_for(db, business: models.Business, pincode: str | None, address: str | None) -> Terms:
    zones = business_zones(db, business)
    if not zones:
        return general_terms(business)
    zone = find_zone(zones, pincode, address)
    if zone is None:
        general = general_terms(business)
        return Terms("unknown", None, general.fee, general.free_above, general.min_order, False, None)
    return zone_terms(business, zone)


def zone_dict(zone: models.DeliveryZone) -> dict:
    return {k: getattr(zone, k) for k in ("id", "name", "match_type", "match_values", "delivers", "days_min",
                                          "days_max", "fee", "free_above", "min_order", "cod", "note", "position")}


def _days_text(days: tuple[int, int]) -> str:
    return f"usually {days[0]}-{days[1]} days" if days[0] != days[1] else f"usually {days[0]} days"


def describe(db, business: models.Business) -> str:
    """The zones in plain words, for the bot."""
    zones = business_zones(db, business)
    if not zones:
        return ""
    lines = ["Delivery zones - use the customer's PIN code, city or state (ask for the PIN code if you don't know "
             "where they are). If no zone fits, say you'll check with the shop and set notify_owner:"]
    for z in zones:
        listed = ", ".join(values(z))
        where = {"pincode": f"PIN codes starting {listed}", "city": f"cities: {listed}",
                 "state": f"states: {listed}", "rest": "everywhere else"}.get(z.match_type, "")
        terms = zone_terms(business, z)
        if terms.status == "no_delivery":
            lines.append(f"- {z.name} ({where}): NO delivery." + (f" {z.note}" if z.note else ""))
            continue
        parts = [f"delivery ₹{terms.fee:g}" if terms.fee else "free delivery"]
        if terms.fee and terms.free_above:
            parts.append(f"free above ₹{terms.free_above:g}")
        if terms.min_order:
            parts.append(f"minimum order ₹{terms.min_order:g}")
        parts.append("cash on delivery available" if terms.cod else "no cash on delivery")
        if terms.days:
            parts.append(_days_text(terms.days))
        lines.append(f"- {z.name} ({where}): " + ", ".join(parts) + (f". {z.note}" if z.note else ""))
    if not any(z.match_type == "rest" for z in zones):
        lines.append("- Anywhere not listed: not known - check with the shop.")
    return "\n".join(lines)
