from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload

import models
import schemas
from database import get_db
from business import DEFAULT_BUSINESS_ID
from models import ORDER_STATUSES

router = APIRouter(prefix="/orders", tags=["orders"])


def _serialize_order(order: models.Order) -> dict:
    return {
        "id": order.id,
        "status": order.status,
        "total_amount": order.total_amount,
        "is_paid": order.is_paid,
        "payment_link_url": order.payment_link_url,
        "created_at": order.created_at,
        "customer": order.customer,
        "items": [
            {
                "id": item.id,
                "product_id": item.product_id,
                "product_name": item.product.name,
                "quantity": item.quantity,
                "price_at_order": item.price_at_order,
            }
            for item in order.items
        ],
    }


@router.get("", response_model=list[schemas.OrderOut])
def list_orders(db: Session = Depends(get_db)):
    orders = (
        db.query(models.Order)
        .options(joinedload(models.Order.customer), joinedload(models.Order.items).joinedload(models.OrderItem.product))
        .filter(models.Order.business_id == DEFAULT_BUSINESS_ID)
        .order_by(models.Order.created_at.desc())
        .all()
    )
    return [_serialize_order(o) for o in orders]


@router.post("", response_model=schemas.OrderOut)
def create_order(payload: schemas.OrderCreate, db: Session = Depends(get_db)):
    customer = models.Customer(business_id=DEFAULT_BUSINESS_ID, **payload.customer.model_dump())
    db.add(customer)
    db.flush()

    total = 0.0
    order = models.Order(business_id=DEFAULT_BUSINESS_ID, customer_id=customer.id, status="Pending")
    db.add(order)
    db.flush()

    for item in payload.items:
        product = db.query(models.Product).filter(
            models.Product.id == item.product_id, models.Product.business_id == DEFAULT_BUSINESS_ID
        ).first()
        if not product:
            raise HTTPException(status_code=404, detail=f"Product {item.product_id} not found")
        line_total = product.price * item.quantity
        total += line_total
        db.add(models.OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=item.quantity,
            price_at_order=product.price,
        ))

    order.total_amount = total
    db.commit()
    db.refresh(order)
    return _serialize_order(order)


@router.patch("/{order_id}/status", response_model=schemas.OrderOut)
def update_order_status(order_id: int, payload: schemas.OrderStatusUpdate, db: Session = Depends(get_db)):
    if payload.status not in ORDER_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of {ORDER_STATUSES}")

    order = (
        db.query(models.Order)
        .options(joinedload(models.Order.customer), joinedload(models.Order.items).joinedload(models.OrderItem.product))
        .filter(models.Order.id == order_id, models.Order.business_id == DEFAULT_BUSINESS_ID)
        .first()
    )
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    order.status = payload.status
    db.commit()
    db.refresh(order)
    return _serialize_order(order)
