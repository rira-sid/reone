from datetime import datetime
from pydantic import BaseModel


class ProductBase(BaseModel):
    name: str
    price: float
    stock: int = 0
    unit: str = "pcs"


class ProductCreate(ProductBase):
    pass


class ProductUpdate(BaseModel):
    name: str | None = None
    price: float | None = None
    stock: int | None = None
    unit: str | None = None


class ProductOut(ProductBase):
    id: int

    class Config:
        from_attributes = True


class CustomerBase(BaseModel):
    name: str
    phone: str
    address: str = ""


class CustomerOut(CustomerBase):
    id: int

    class Config:
        from_attributes = True


class OrderItemCreate(BaseModel):
    product_id: int
    quantity: int


class OrderItemOut(BaseModel):
    id: int
    product_id: int
    product_name: str
    quantity: int
    price_at_order: float

    class Config:
        from_attributes = True


class OrderCreate(BaseModel):
    customer: CustomerBase
    items: list[OrderItemCreate]


class OrderStatusUpdate(BaseModel):
    status: str
    tracking_info: str | None = None
    notify_customer: bool = True


class OrderOut(BaseModel):
    id: int
    status: str
    total_amount: float
    is_paid: bool
    payment_link_url: str | None = None
    paid_at: datetime | None = None
    tracking_info: str | None = None
    invoice_path: str
    customer_notified: bool | None = None
    created_at: datetime
    customer: CustomerOut
    items: list[OrderItemOut]

    class Config:
        from_attributes = True


class PaymentLinkOut(BaseModel):
    order_id: int
    payment_link_url: str
