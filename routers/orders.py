from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload

import models
import schemas
from auth import current_business
from database import get_db
from models import ORDER_STATUSES
from notifications import notify_order_update
from routers.invoices import invoice_path

router = APIRouter(prefix="/orders", tags=["orders"])


def _serialize_order(order: models.Order) -> dict:
    return {
        "id": order.id,
        "status": order.status,
        "total_amount": order.total_amount,
        "is_paid": order.is_paid,
        "payment_link_url": order.payment_link_url,
        "paid_at": order.paid_at,
        "tracking_info": order.tracking_info,
        "invoice_path": invoice_path(order.id),
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
def list_orders(db: Session = Depends(get_db), business: models.Business = Depends(current_business)):
    orders = (
        db.query(models.Order)
        .options(joinedload(models.Order.customer), joinedload(models.Order.items).joinedload(models.OrderItem.product))
        .filter(models.Order.business_id == business.id)
        .order_by(models.Order.created_at.desc())
        .all()
    )
    return [_serialize_order(o) for o in orders]


def place_order(db: Session, business_id: int, customer: models.Customer, items: list[schemas.OrderItemCreate]) -> models.Order:
    """Create an order (and its line items) for an already-added customer. Caller commits."""
    total = 0.0
    order = models.Order(business_id=business_id, customer_id=customer.id, status="Pending")
    db.add(order)
    db.flush()

    for item in items:
        product = db.query(models.Product).filter(
            models.Product.id == item.product_id, models.Product.business_id == business_id
        ).first()
        if not product or not product.is_active:
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
    return order


@router.post("", response_model=schemas.OrderOut)
def create_order(payload: schemas.OrderCreate, db: Session = Depends(get_db), business: models.Business = Depends(current_business)):
    customer = models.Customer(business_id=business.id, **payload.customer.model_dump())
    db.add(customer)
    db.flush()

    order = place_order(db, business.id, customer, payload.items)
    db.commit()
    db.refresh(order)
    return _serialize_order(order)


# What the customer is told when the seller moves their order along.
STATUS_UPDATES = {
    "Packed": "It has been packed and will be shipped soon.",
    "Shipped": "It has been shipped.",
    "Delivered": "It has been delivered. Thank you for shopping with us!",
    "Cancelled": "It has been cancelled. Please reply here if you have any questions.",
}


@router.patch("/{order_id}/status", response_model=schemas.OrderOut)
async def update_order_status(order_id: int, payload: schemas.OrderStatusUpdate, db: Session = Depends(get_db), business: models.Business = Depends(current_business)):
    if payload.status not in ORDER_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of {ORDER_STATUSES}")

    order = (
        db.query(models.Order)
        .options(joinedload(models.Order.customer), joinedload(models.Order.items).joinedload(models.OrderItem.product))
        .filter(models.Order.id == order_id, models.Order.business_id == business.id)
        .first()
    )
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    status_changed = order.status != payload.status
    order.status = payload.status
    if payload.tracking_info is not None:
        order.tracking_info = payload.tracking_info.strip() or None
    db.commit()
    db.refresh(order)

    notified = None
    if status_changed and payload.notify_customer and payload.status in STATUS_UPDATES:
        update = STATUS_UPDATES[payload.status]
        if payload.status == "Shipped" and order.tracking_info:
            update = f"{update} Tracking: {order.tracking_info}"
        notified = await notify_order_update(db, business, order, update)

    return {**_serialize_order(order), "customer_notified": notified}
