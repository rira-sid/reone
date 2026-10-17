from sqlalchemy import Column, Integer, String, Float, ForeignKey, DateTime, Text, Boolean, LargeBinary
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from database import Base


class Business(Base):
    __tablename__ = "businesses"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    # Shop web address: /shop/<slug> (slugs.py). Unique; made from the name, changeable in Settings.
    slug = Column(String, nullable=True, index=True)
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

    # Shop page branding
    brand_color = Column(String, nullable=True)  # "#RRGGBB"; None = RIRAONE's default gradient
    tagline = Column(String, nullable=True)
    about = Column(Text, nullable=True)
    logo_image_id = Column(Integer, nullable=True)
    banner_image_id = Column(Integer, nullable=True)

    # The founder can pause a business: its shop and WhatsApp bot stop, its login is refused.
    # paused_reason "billing" = paused automatically for an unpaid plan; the owner can still log in to pay.
    is_active = Column(Boolean, default=True, nullable=False)
    paused_reason = Column(String, nullable=True)

    # Enrolment: self sign-ups wait for the founder's approval ("pending"); their dashboard works,
    # but the shop page and WhatsApp bot stay off until approved. "rejected" can't log in.
    approval_status = Column(String, default="approved", nullable=False)
    signup_source = Column(String, nullable=True)  # "founder" | "invite" | "self"

    # RIRAONE plan (see billing.py). paid_until = end of the trial or of the last paid month.
    plan = Column(String, nullable=True)
    trial_ends_at = Column(DateTime(timezone=True), nullable=True)
    paid_until = Column(DateTime(timezone=True), nullable=True)
    billing_exempt = Column(Boolean, default=False, nullable=False)  # founder's own shop, free pilots
    auto_pause = Column(Boolean, default=True, nullable=False)  # pause the shop when the grace period ends

    # Result of the periodic WhatsApp connection check (billing.check_whatsapp_health).
    wa_health = Column(String, nullable=True)  # "ok" | "error" | None (not connected / not checked)
    wa_health_detail = Column(Text, nullable=True)
    wa_health_checked_at = Column(DateTime(timezone=True), nullable=True)

    @property
    def is_live(self) -> bool:
        """Customers can use the shop page and WhatsApp bot."""
        return self.is_active and self.approval_status == "approved"

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
    # When the seller (or staff) last opened this chat in the Inbox - newer customer messages are "unread".
    seller_read_at = Column(DateTime(timezone=True), nullable=True)
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


class ShopImage(Base):
    """A business's logo or shop banner (stored in the database, like product photos)."""
    __tablename__ = "shop_images"

    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False, index=True)
    data = Column(LargeBinary, nullable=False)
    mime_type = Column(String, nullable=False, default="image/jpeg")


SHOP_EVENTS = ("view", "add_to_cart", "checkout_open", "order")


class ShopEvent(Base):
    """One step of the shop funnel: page view -> added to cart -> opened checkout -> placed order."""
    __tablename__ = "shop_events"

    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False, index=True)
    kind = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)


class FounderUser(Base):
    """RIRAONE's founder login for the founder console (separate from every shop login)."""
    __tablename__ = "founder_users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, nullable=False, unique=True)
    password_hash = Column(String, nullable=False)
    alert_phone = Column(String, nullable=True)  # founder's WhatsApp for sign-up / security / billing alerts
    created_at = Column(DateTime(timezone=True), server_default=func.now())


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


class LoginEvent(Base):
    """Every sign-in attempt on the seller dashboard and the founder console."""
    __tablename__ = "login_events"

    id = Column(Integer, primary_key=True, index=True)
    site = Column(String, nullable=False)  # "seller" | "founder"
    business_id = Column(Integer, nullable=True, index=True)
    staff_id = Column(Integer, nullable=True)
    email = Column(String, nullable=True)
    success = Column(Boolean, nullable=False)
    ip = Column(String, nullable=True)
    country = Column(String, nullable=True)
    country_code = Column(String, nullable=True)
    city = Column(String, nullable=True)
    device = Column(String, nullable=True)  # "Mobile · Android · Chrome"
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)


class Visit(Base):
    """A page visit: a shop page, or RIRAONE's own sign-up / login page. No IP is kept - only
    the country/city it resolved to."""
    __tablename__ = "visits"

    id = Column(Integer, primary_key=True, index=True)
    page = Column(String, nullable=False, index=True)  # "shop" | "signup" | "login"
    business_id = Column(Integer, nullable=True, index=True)
    country = Column(String, nullable=True)
    country_code = Column(String, nullable=True)
    city = Column(String, nullable=True)
    device = Column(String, nullable=True)
    source = Column(String, nullable=True)  # "WhatsApp" | "Instagram" | "Direct" | site name
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)


class PlanInvoice(Base):
    """A RIRAONE subscription charge to a seller (one month of a plan)."""
    __tablename__ = "plan_invoices"

    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False, index=True)
    plan = Column(String, nullable=False)
    amount = Column(Float, nullable=False)
    status = Column(String, default="pending", nullable=False)  # "pending" | "paid" | "cancelled"
    method = Column(String, nullable=True)  # "razorpay" | "manual"
    razorpay_link_id = Column(String, nullable=True)
    link_url = Column(String, nullable=True)
    razorpay_payment_id = Column(String, nullable=True)
    note = Column(Text, nullable=True)
    paid_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    business = relationship("Business")


class BillingNotice(Base):
    """A billing reminder already sent, so each one goes out once per billing period."""
    __tablename__ = "billing_notices"

    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False, index=True)
    period = Column(String, nullable=False)  # paid_until date the notice is about
    stage = Column(String, nullable=False)  # "soon" | "due" | "overdue" | "paused"
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class FounderAudit(Base):
    """What the founder did, and when - a trail for pauses, plan changes, support logins..."""
    __tablename__ = "founder_audit"

    id = Column(Integer, primary_key=True, index=True)
    founder_id = Column(Integer, nullable=True)
    action = Column(String, nullable=False)
    business_id = Column(Integer, nullable=True, index=True)
    detail = Column(Text, nullable=True)
    ip = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)


class Announcement(Base):
    """A message from the founder to all sellers (business_id None) or one seller, shown as a
    banner in their dashboard and optionally sent on WhatsApp."""
    __tablename__ = "announcements"

    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, nullable=True, index=True)
    title = Column(String, nullable=False)
    body = Column(Text, nullable=True)
    level = Column(String, default="info", nullable=False)  # "info" | "warning"
    is_active = Column(Boolean, default=True, nullable=False)
    whatsapp_sent = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class SystemEvent(Base):
    """Problems worth the founder's attention: AI failures, WhatsApp send errors, billing errors."""
    __tablename__ = "system_events"

    id = Column(Integer, primary_key=True, index=True)
    source = Column(String, nullable=False)  # "ai" | "whatsapp" | "billing" | "payments"
    business_id = Column(Integer, nullable=True, index=True)
    message = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)


class PlanPrice(Base):
    """Founder-set price for a plan (monthly / quarterly / annual). Missing = billing.PLANS default."""
    __tablename__ = "plan_prices"

    plan = Column(String, primary_key=True)
    price = Column(Float, nullable=False)


class ProductTranslation(Base):
    """A product's texts in one customer language (catalog_i18n.py)."""
    __tablename__ = "product_translations"

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    lang = Column(String, nullable=False)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    category = Column(String, nullable=True)
    nutrition = Column(Text, nullable=True)
    sizes_json = Column(Text, nullable=True)  # labels in the same order as the product's sizes
    source_hash = Column(String, nullable=True)  # English text it was made from
    edited = Column(Boolean, default=False, nullable=False)  # seller corrected it by hand
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
