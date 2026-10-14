from sqlalchemy import Column, Integer, String, Float, ForeignKey, DateTime, Text, Boolean, LargeBinary
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from database import Base


class Business(Base):
    __tablename__ = "businesses"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Login
    owner_email = Column(String, nullable=True, index=True)
    password_hash = Column(String, nullable=True)

    # Shown on invoices
    phone = Column(String, nullable=True)
    address = Column(Text, nullable=True)
    gstin = Column(String, nullable=True)

    # Seller's own accounts. Secrets are encrypted (see secrets_box.py).
    wa_phone_number_id = Column(String, nullable=True, index=True)
    wa_token_enc = Column(Text, nullable=True)
    razorpay_key_id = Column(String, nullable=True)
    razorpay_key_secret_enc = Column(Text, nullable=True)
    razorpay_webhook_secret_enc = Column(Text, nullable=True)

    # How online payments are collected (see routers/pay.py): "upi" = seller's own UPI ID, seller
    # confirms by hand; "razorpay_link" = Razorpay link, auto-confirmed; "whatsapp_pay" = WhatsApp's
    # native "Review and pay" through Razorpay, auto-confirmed. Unset = best available.
    payment_method = Column(String, nullable=True)
    upi_id = Column(String, nullable=True)
    upi_name = Column(String, nullable=True)
    wa_payment_config = Column(String, nullable=True)  # payment configuration name in WhatsApp Manager

    # Ordering rules
    accepting_orders = Column(Boolean, default=True, nullable=False)
    closed_message = Column(Text, nullable=True)
    cod_enabled = Column(Boolean, default=False, nullable=False)
    delivery_fee = Column(Float, default=0.0, nullable=False)
    free_delivery_above = Column(Float, nullable=True)
    min_order_amount = Column(Float, nullable=True)

    # Customer chat: a welcome line the seller writes (shown after the language picker), and the
    # seller's own WhatsApp number that gets an instant alert whenever a customer asks a question.
    welcome_message = Column(Text, nullable=True)
    alert_phone = Column(String, nullable=True)

    products = relationship("Product", back_populates="business")
    orders = relationship("Order", back_populates="business")


class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False)
    name = Column(String, nullable=False)
    price = Column(Float, nullable=False)
    stock = Column(Integer, default=0)
    unit = Column(String, default="pcs")
    category = Column(String, nullable=True)
    description = Column(Text, nullable=True)
    # Free text, one fact per line: "Energy: 120 kcal". Shown as a table on the shop page.
    nutrition = Column(Text, nullable=True)
    # Products that appear on past orders are hidden instead of deleted, so old orders and
    # invoices still show what was bought.
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    business = relationship("Business", back_populates="products")
    variants = relationship("ProductVariant", back_populates="product", order_by="ProductVariant.position",
                            cascade="all, delete-orphan")
    images = relationship("ProductImage", back_populates="product", order_by="ProductImage.position",
                          cascade="all, delete-orphan")

    @property
    def active_variants(self) -> list["ProductVariant"]:
        return [v for v in self.variants if v.is_active]


class ProductVariant(Base):
    """A size / pack of a product (100g, 250g, S, M, XL...), each with its own price and stock.
    A product with no variants is sold at its own price and stock."""
    __tablename__ = "product_variants"

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    label = Column(String, nullable=False)
    price = Column(Float, nullable=False)
    stock = Column(Integer, default=0, nullable=False)
    position = Column(Integer, default=0, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)  # kept (hidden) when on past orders

    product = relationship("Product", back_populates="variants")


class ProductImage(Base):
    """Product photo, stored in the database (Render's disk is wiped on every deploy). The
    dashboard shrinks photos before upload, so each is ~100-200 KB."""
    __tablename__ = "product_images"

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    data = Column(LargeBinary, nullable=False)
    mime_type = Column(String, nullable=False, default="image/jpeg")
    position = Column(Integer, default=0, nullable=False)

    product = relationship("Product", back_populates="images")


class Customer(Base):
    __tablename__ = "customers"

    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False)
    name = Column(String, nullable=False)
    phone = Column(String, nullable=False)
    address = Column(Text, default="")

    orders = relationship("Order", back_populates="customer")


ORDER_STATUSES = ["Pending", "Confirmed", "Packed", "Shipped", "Delivered", "Cancelled"]


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False)
    status = Column(String, default="Pending")
    total_amount = Column(Float, default=0.0)
    is_paid = Column(Boolean, default=False, nullable=False)
    razorpay_payment_link_id = Column(String, nullable=True, index=True)
    razorpay_order_id = Column(String, nullable=True, index=True)  # for UPI-app payments on our pay page
    payment_link_url = Column(String, nullable=True)  # where the customer is sent to pay (often our pay page)
    razorpay_link_url = Column(String, nullable=True)  # Razorpay's hosted payment link (cards, netbanking, QR)
    razorpay_payment_id = Column(String, nullable=True, unique=True)
    paid_at = Column(DateTime(timezone=True), nullable=True)
    tracking_info = Column(Text, nullable=True)
    delivery_fee = Column(Float, default=0.0, nullable=False)
    payment_method = Column(String, default="online", nullable=False)  # "online" | "cod"
    customer_note = Column(Text, nullable=True)
    # True while this order's items are taken out of product stock (paid, or a confirmed COD order).
    stock_deducted = Column(Boolean, default=False, nullable=False)
    payment_reminder_sent_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    business = relationship("Business", back_populates="orders")
    customer = relationship("Customer", back_populates="orders")
    items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")


class OrderItem(Base):
    __tablename__ = "order_items"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    quantity = Column(Integer, nullable=False)
    price_at_order = Column(Float, nullable=False)
    variant_id = Column(Integer, ForeignKey("product_variants.id"), nullable=True)
    variant_label = Column(String, nullable=True)  # copied, so invoices survive variant edits

    order = relationship("Order", back_populates="items")
    product = relationship("Product")

    @property
    def display_name(self) -> str:
        return f"{self.product.name} ({self.variant_label})" if self.variant_label else self.product.name


class Conversation(Base):
    """One WhatsApp chat with a customer. Holds the in-progress cart and what the AI has
    collected so far, plus the seller's take-over switch for the live inbox."""
    __tablename__ = "conversations"

    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False)
    phone = Column(String, nullable=False, index=True)
    customer_name = Column(String, nullable=True)
    delivery_address = Column(Text, nullable=True)
    language = Column(String, nullable=True)
    # Language code the customer picked from the welcome menu (en, ta, hi...). None = not asked yet.
    preferred_language = Column(String, nullable=True)
    # A customer question is waiting for the seller (shown as "Needs you"; alerts escalate until answered).
    needs_seller = Column(Boolean, default=False, nullable=False)
    needs_seller_since = Column(DateTime(timezone=True), nullable=True)
    seller_alert_stage = Column(Integer, default=0, nullable=False)  # 0 none, 1 alerted, 2 re-alerted, 3 customer told to wait
    seller_ack_at = Column(DateTime(timezone=True), nullable=True)  # last "seller will reply" sent to the customer
    last_seller_reply_at = Column(DateTime(timezone=True), nullable=True)
    cart_json = Column(Text, default="[]")
    ai_paused = Column(Boolean, default=False, nullable=False)
    # WhatsApp only allows free-form messages within 24h of the customer's last message.
    last_customer_message_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    messages = relationship("Message", back_populates="conversation", order_by="Message.id")


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=False, index=True)
    sender = Column(String, nullable=False)  # "customer" | "ai" | "seller" | "system"
    # Meta's id for incoming messages - used to ignore Meta's retried deliveries, even after a restart.
    wa_message_id = Column(String, nullable=True, index=True)
    text = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    conversation = relationship("Conversation", back_populates="messages")


class SellerAlert(Base):
    """An alert sent to the seller's own WhatsApp. When the seller swipe-replies to it, the reply is
    passed on to the customer of `conversation_id`."""
    __tablename__ = "seller_alerts"

    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False, index=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=False)
    wa_message_id = Column(String, nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    conversation = relationship("Conversation")


class StaffUser(Base):
    """Extra logins for a business (shop helpers). They can use the inbox, orders and products,
    but not settings, payment keys or staff management - those stay with the owner."""
    __tablename__ = "staff_users"

    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    email = Column(String, nullable=False, index=True)
    password_hash = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
