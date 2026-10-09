from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import models
import schemas
from auth import current_business
from database import get_db

router = APIRouter(prefix="/products", tags=["products"])


@router.get("", response_model=list[schemas.ProductOut])
def list_products(db: Session = Depends(get_db), business: models.Business = Depends(current_business)):
    return db.query(models.Product).filter(
        models.Product.business_id == business.id, models.Product.is_active.is_(True)
    ).all()


@router.post("", response_model=schemas.ProductOut)
def create_product(payload: schemas.ProductCreate, db: Session = Depends(get_db), business: models.Business = Depends(current_business)):
    product = models.Product(business_id=business.id, **payload.model_dump())
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


@router.patch("/{product_id}", response_model=schemas.ProductOut)
def update_product(product_id: int, payload: schemas.ProductUpdate, db: Session = Depends(get_db), business: models.Business = Depends(current_business)):
    product = db.query(models.Product).filter(
        models.Product.id == product_id, models.Product.business_id == business.id
    ).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(product, field, value)
    db.commit()
    db.refresh(product)
    return product


@router.delete("/{product_id}")
def delete_product(product_id: int, db: Session = Depends(get_db), business: models.Business = Depends(current_business)):
    product = db.query(models.Product).filter(
        models.Product.id == product_id, models.Product.business_id == business.id
    ).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    if db.query(models.OrderItem).filter(models.OrderItem.product_id == product.id).first():
        product.is_active = False  # keep it for past orders/invoices, hide it everywhere else
    else:
        db.delete(product)
    db.commit()
    return {"status": "deleted"}
