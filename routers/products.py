import base64
import binascii

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import models
import schemas
from auth import current_business
from database import get_db
from routers.invoices import PUBLIC_BASE_URL

router = APIRouter(prefix="/products", tags=["products"])

MAX_IMAGES = 4
MAX_IMAGE_BYTES = 1_500_000  # the dashboard shrinks photos to ~150 KB; this only stops abuse
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}


def image_url(image: models.ProductImage) -> str:
    return f"{PUBLIC_BASE_URL}/shop/image/{image.id}"


def product_out(product: models.Product) -> dict:
    return {
        "id": product.id,
        "name": product.name,
        "price": product.price,
        "stock": product.stock,
        "unit": product.unit,
        "category": product.category,
        "description": product.description,
        "nutrition": product.nutrition,
        "variants": [{"id": v.id, "label": v.label, "price": v.price, "stock": v.stock} for v in product.active_variants],
        "images": [{"id": i.id, "url": image_url(i)} for i in product.images],
    }


def _get_product(db: Session, business: models.Business, product_id: int) -> models.Product:
    product = db.query(models.Product).filter(
        models.Product.id == product_id, models.Product.business_id == business.id
    ).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return product


def _apply_variants(db: Session, product: models.Product, variants: list[schemas.VariantIn]):
    """Replace the product's sizes. Sizes that appear on past orders are hidden, not deleted."""
    clean = [v for v in variants if v.label.strip()]
    if len({v.label.strip().lower() for v in clean}) != len(clean):
        raise HTTPException(status_code=400, detail="Each size needs a different name")
    existing = {v.id: v for v in product.variants}
    keep = set()
    for position, v in enumerate(clean):
        if v.price < 0 or v.stock < 0:
            raise HTTPException(status_code=400, detail="Price and stock can't be negative")
        row = existing.get(v.id) if v.id else None
        if row is None:
            row = models.ProductVariant(product=product)
            db.add(row)
        row.label, row.price, row.stock, row.position, row.is_active = v.label.strip(), v.price, v.stock, position, True
        if row.id:
            keep.add(row.id)
    for vid, row in existing.items():
        if vid in keep:
            continue
        if db.query(models.OrderItem).filter(models.OrderItem.variant_id == vid).first():
            row.is_active = False
        else:
            product.variants.remove(row)
    db.flush()
    active = product.active_variants
    if active:  # the product's own price/stock become "from" price and total stock
        product.price = min(v.price for v in active)
        product.stock = sum(v.stock for v in active)


def decode_image(data_url: str) -> tuple[bytes, str]:
    """Data URL (data:image/jpeg;base64,...) -> (bytes, mime). Raises HTTPException(400)."""
    try:
        header, encoded = data_url.split(",", 1)
        mime = header.split(":", 1)[1].split(";", 1)[0]
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, IndexError, binascii.Error):
        raise HTTPException(status_code=400, detail="Not a valid image")
    if mime not in IMAGE_TYPES:
        raise HTTPException(status_code=400, detail="Use a JPEG, PNG or WebP photo")
    if len(raw) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=400, detail="Photo is too large")
    return raw, mime


@router.get("", response_model=list[schemas.ProductOut])
def list_products(db: Session = Depends(get_db), business: models.Business = Depends(current_business)):
    products = db.query(models.Product).filter(
        models.Product.business_id == business.id, models.Product.is_active.is_(True)
    ).order_by(models.Product.id).all()
    return [product_out(p) for p in products]


@router.post("", response_model=schemas.ProductOut)
def create_product(payload: schemas.ProductCreate, db: Session = Depends(get_db), business: models.Business = Depends(current_business)):
    data = payload.model_dump(exclude={"variants"})
    product = models.Product(business_id=business.id, **data)
    db.add(product)
    db.flush()
    if payload.variants:
        _apply_variants(db, product, payload.variants)
    db.commit()
    db.refresh(product)
    return product_out(product)


@router.patch("/{product_id}", response_model=schemas.ProductOut)
def update_product(product_id: int, payload: schemas.ProductUpdate, db: Session = Depends(get_db), business: models.Business = Depends(current_business)):
    product = _get_product(db, business, product_id)
    fields = payload.model_dump(exclude_unset=True)
    variants = fields.pop("variants", None)
    for field, value in fields.items():
        setattr(product, field, value)
    if variants is not None:
        _apply_variants(db, product, payload.variants)
    db.commit()
    db.refresh(product)
    return product_out(product)


@router.delete("/{product_id}")
def delete_product(product_id: int, db: Session = Depends(get_db), business: models.Business = Depends(current_business)):
    product = _get_product(db, business, product_id)
    if db.query(models.OrderItem).filter(models.OrderItem.product_id == product.id).first():
        product.is_active = False  # keep it for past orders/invoices, hide it everywhere else
    else:
        db.delete(product)
    db.commit()
    return {"status": "deleted"}


@router.post("/{product_id}/images", response_model=schemas.ProductOut)
def add_image(product_id: int, payload: schemas.ImageUpload, db: Session = Depends(get_db),
              business: models.Business = Depends(current_business)):
    """`data` is a data URL (data:image/jpeg;base64,...) - JSON keeps this free of upload libraries."""
    product = _get_product(db, business, product_id)
    if len(product.images) >= MAX_IMAGES:
        raise HTTPException(status_code=400, detail=f"Up to {MAX_IMAGES} photos per product")
    raw, mime = decode_image(payload.data)
    position = max([i.position for i in product.images], default=-1) + 1
    db.add(models.ProductImage(product_id=product.id, data=raw, mime_type=mime, position=position))
    db.commit()
    db.refresh(product)
    return product_out(product)


@router.delete("/{product_id}/images/{image_id}", response_model=schemas.ProductOut)
def delete_image(product_id: int, image_id: int, db: Session = Depends(get_db),
                 business: models.Business = Depends(current_business)):
    product = _get_product(db, business, product_id)
    image = next((i for i in product.images if i.id == image_id), None)
    if not image:
        raise HTTPException(status_code=404, detail="Photo not found")
    product.images.remove(image)
    db.commit()
    db.refresh(product)
    return product_out(product)


@router.post("/{product_id}/images/{image_id}/cover", response_model=schemas.ProductOut)
def make_cover(product_id: int, image_id: int, db: Session = Depends(get_db),
               business: models.Business = Depends(current_business)):
    """Move a photo to the front - the first photo is the one customers see in the list."""
    product = _get_product(db, business, product_id)
    ordered = sorted(product.images, key=lambda i: (i.id != image_id, i.position))
    if not ordered or ordered[0].id != image_id:
        raise HTTPException(status_code=404, detail="Photo not found")
    for position, image in enumerate(ordered):
        image.position = position
    db.commit()
    db.refresh(product)
    return product_out(product)
