import csv
import io
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
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
        "delivery_fee": order.delivery_fee or 0.0,
        "payment_method": order.payment_method or "online",
        "customer_note": order.customer_note,
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
        .order_by(models.Order.created_at.desc(), models.Order.id.desc())
        .all()
    )
    return [_serialize_order(o) for o in orders]


def delivery_fee_for(business: models.Business, subtotal: float) -> float:
    if not business.delivery_fee:
        return 0.0
    if business.free_delivery_above and subtotal >= business.free_delivery_above:
        return 0.0
    return business.delivery_fee


def reserve_stock(db: Session, order: models.Order):
    """Take the order's items out of stock (once)."""
    if order.stock_deducted:
        return
    for item in order.items:
        product = db.get(models.Product, item.product_id)
        if product:
            product.stock = max(0, product.stock - item.quantity)
    order.stock_deducted = True


def release_stock(db: Session, order: models.Order):
    """Put a cancelled order's items back in stock."""
    if not order.stock_deducted:
        return
    for item in order.items:
        product = db.get(models.Product, item.product_id)
        if product:
            product.stock += item.quantity
    order.stock_deducted = False


def mark_order_paid(db: Session, order: models.Order, payment_id: str | None = None):
    order.is_paid = True
    order.paid_at = datetime.now(timezone.utc)
    if order.status == "Pending":
        order.status = "Confirmed"
    if payment_id:
        order.razorpay_payment_id = payment_id
    reserve_stock(db, order)


def place_order(
    db: Session,
    business: models.Business,
    customer: models.Customer,
    items: list[schemas.OrderItemCreate],
    payment_method: str = "online",
    customer_note: str | None = None,
) -> models.Order:
    """Create an order (and its line items) for an already-added customer. Caller commits.
    Raises HTTPException(400) if the order breaks the shop's rules."""
    if payment_method not in ("online", "cod"):
        raise HTTPException(status_code=400, detail="Payment method must be online or cod")
    if payment_method == "cod" and not business.cod_enabled:
        raise HTTPException(status_code=400, detail="Cash on delivery is not available")

    order = models.Order(
        business_id=business.id, customer_id=customer.id, status="Pending",
        payment_method=payment_method, customer_note=(customer_note or "").strip() or None,
    )
    db.add(order)
    db.flush()

    subtotal = 0.0
    for item in items:
        product = db.query(models.Product).filter(
            models.Product.id == item.product_id, models.Product.business_id == business.id
        ).first()
        if not product or not product.is_active:
            raise HTTPException(status_code=404, detail=f"Product {item.product_id} not found")
        if item.quantity <= 0:
            raise HTTPException(status_code=400, detail="Quantity must be at least 1")
        subtotal += product.price * item.quantity
        order.items.append(models.OrderItem(
            product_id=product.id,
            quantity=item.quantity,
            price_at_order=product.price,
        ))

    if business.min_order_amount and subtotal < business.min_order_amount:
        raise HTTPException(status_code=400, detail=f"Minimum order is ₹{business.min_order_amount:g}")

    order.delivery_fee = delivery_fee_for(business, subtotal)
    order.total_amount = subtotal + order.delivery_fee
    if payment_method == "cod":
        order.status = "Confirmed"  # nothing to wait for - reserve the stock now
        db.flush()
        reserve_stock(db, order)
    return order


@router.post("", response_model=schemas.OrderOut)
def create_order(payload: schemas.OrderCreate, db: Session = Depends(get_db), business: models.Business = Depends(current_business)):
    customer = models.Customer(business_id=business.id, **payload.customer.model_dump())
    db.add(customer)
    db.flush()

    order = place_order(db, business, customer, payload.items, payload.payment_method, payload.customer_note)
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
    previous = order.status
    order.status = payload.status
    if payload.status == "Cancelled":
        release_stock(db, order)
    elif previous == "Cancelled" and (order.is_paid or order.payment_method == "cod"):
        reserve_stock(db, order)
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


@router.patch("/{order_id}/payment", response_model=schemas.OrderOut)
async def update_payment(order_id: int, payload: schemas.OrderPaymentUpdate, db: Session = Depends(get_db),
                         business: models.Business = Depends(current_business)):
    """Seller marks an order paid by hand - cash on delivery, or UPI paid outside Razorpay."""
    order = (
        db.query(models.Order)
        .options(joinedload(models.Order.customer), joinedload(models.Order.items).joinedload(models.OrderItem.product))
        .filter(models.Order.id == order_id, models.Order.business_id == business.id)
        .first()
    )
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    notified = None
    if payload.is_paid and not order.is_paid:
        mark_order_paid(db, order)
        db.commit()
        db.refresh(order)
        if payload.notify_customer:
            notified = await notify_order_update(db, business, order, "Your payment has been received. Thank you!")
    elif not payload.is_paid and order.is_paid:
        order.is_paid = False
        order.paid_at = None
        db.commit()
        db.refresh(order)
    return {**_serialize_order(order), "customer_notified": notified}


@router.get("/export.csv")
def export_orders(db: Session = Depends(get_db), business: models.Business = Depends(current_business)):
    """All orders as a spreadsheet, for accounts / GST filing."""
    orders = (
        db.query(models.Order)
        .options(joinedload(models.Order.customer), joinedload(models.Order.items).joinedload(models.OrderItem.product))
        .filter(models.Order.business_id == business.id)
        .order_by(models.Order.id)
        .all()
    )
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Order", "Date (UTC)", "Customer", "Phone", "Address", "Items", "Subtotal", "Delivery fee",
                     "Total", "Payment method", "Paid", "Paid at (UTC)", "Status", "Tracking", "Customer note"])
    for o in orders:
        writer.writerow([
            o.id, o.created_at.strftime("%Y-%m-%d %H:%M") if o.created_at else "",
            o.customer.name, o.customer.phone, o.customer.address or "",
            "; ".join(f"{i.quantity} x {i.product.name} @ {i.price_at_order:g}" for i in o.items),
            f"{o.total_amount - (o.delivery_fee or 0):.2f}", f"{o.delivery_fee or 0:.2f}", f"{o.total_amount:.2f}",
            "Cash on delivery" if o.payment_method == "cod" else "Online", "Yes" if o.is_paid else "No",
            o.paid_at.strftime("%Y-%m-%d %H:%M") if o.paid_at else "", o.status, o.tracking_info or "",
            o.customer_note or "",
        ])
    # BOM so Excel opens Tamil/Hindi names correctly.
    data = "\ufeff" + buffer.getvalue()
    return StreamingResponse(
        iter([data]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="orders-{business.id}.csv"'},
    )
