"""Shop web addresses made from the business name: "Anu Home Bakes" -> /shop/anu-home-bakes.

Names in other scripts (Tamil, Hindi...) are transliterated where Python can't, so they fall back to
"shop-<id>"; the seller can pick any address in Settings. Old /shop/<id> links keep working."""
import re
import unicodedata

import models

RESERVED = {"image", "media", "admin", "api", "static", "pay", "event", "checkout"}
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,48}[a-z0-9])?$")


def slugify(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    ascii_text = ascii_text.lower().replace("&", " and ").replace("'", "")
    return re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")[:50].strip("-")


def valid(slug: str) -> bool:
    return bool(SLUG_RE.match(slug)) and slug not in RESERVED and not slug.isdigit()


def taken(db, slug: str, exclude_id: int | None = None) -> bool:
    q = db.query(models.Business).filter(models.Business.slug == slug)
    if exclude_id:
        q = q.filter(models.Business.id != exclude_id)
    return q.first() is not None


def ensure_slug(db, business: models.Business) -> str:
    """Give the business an address if it has none (needs business.id). Commits."""
    if business.slug:
        return business.slug
    base = slugify(business.name)
    if not valid(base):
        base = f"shop-{business.id}"
    slug, n = base, 2
    while taken(db, slug, business.id):
        slug, n = f"{base}-{n}", n + 1
    business.slug = slug
    db.commit()
    return slug


def check_wanted(db, wanted: str, exclude_id: int | None = None) -> str:
    """Normalise an address someone typed; raises ValueError with a message for people."""
    slug = slugify(wanted)
    if len(slug) < 3 or not valid(slug):
        raise ValueError("Use 3-50 letters, numbers or dashes for the web address")
    if taken(db, slug, exclude_id):
        raise ValueError(f"The address '{slug}' is already taken - try another")
    return slug


def backfill(db):
    for business in db.query(models.Business).filter(models.Business.slug.is_(None)).all():
        ensure_slug(db, business)


def find(db, key: str) -> models.Business | None:
    """A shop by its address, or by its number (old links)."""
    if key.isdigit():
        return db.get(models.Business, int(key))
    return db.query(models.Business).filter(models.Business.slug == key.lower()).first()


def shop_path(business: models.Business) -> str:
    return f"/shop/{business.slug or business.id}"
