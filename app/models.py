"""SQLAlchemy data model.

Conventions
* Quantities are integers. Money is ``Decimal`` (NUMERIC(18,4) on PostgreSQL, exact text on SQLite).
* Every stockable thing is a ``ProductVariant``; a product without explicit variants gets a
  single default variant carrying the product SKU, so inventory only ever references variants.
* Stock is held per (location, variant) in ``StockLevel`` with four buckets:
  available / reserved / damaged / in_transit. DB CHECK constraints forbid negative buckets.
* ``StockMovement`` is the append-only audit ledger of every bucket change.
"""
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    JSON, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, Numeric, String, Text,
    UniqueConstraint, event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

Q4 = Decimal("0.0001")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Money(TypeDecorator):
    """Exact decimal: NUMERIC on real databases, canonical string on SQLite (no float drift)."""

    impl = Numeric(18, 4)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "sqlite":
            return dialect.type_descriptor(String(32))
        return dialect.type_descriptor(Numeric(18, 4))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        d = Decimal(str(value)).quantize(Q4)
        return str(d) if dialect.name == "sqlite" else d

    def process_result_value(self, value, dialect):
        return None if value is None else Decimal(str(value))


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


# ---------------------------------------------------------------- products
class Product(TimestampMixin, Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(100), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    uom: Mapped[str] = mapped_column(String(20), default="each")
    purchase_price: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    selling_price: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    reorder_point: Mapped[int] = mapped_column(Integer, default=0)
    reorder_quantity: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    variants: Mapped[list["ProductVariant"]] = relationship(
        back_populates="product", cascade="all, delete-orphan", order_by="ProductVariant.id"
    )
    __table_args__ = (CheckConstraint("reorder_point >= 0 AND reorder_quantity >= 0", name="ck_products_reorder_nonneg"),)


class ProductVariant(TimestampMixin, Base):
    __tablename__ = "product_variants"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    sku: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    purchase_price: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    selling_price: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    reorder_point: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reorder_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    product: Mapped[Product] = relationship(back_populates="variants")

    @property
    def effective_purchase_price(self) -> Decimal:
        return self.purchase_price if self.purchase_price is not None else self.product.purchase_price

    @property
    def effective_selling_price(self) -> Decimal:
        return self.selling_price if self.selling_price is not None else self.product.selling_price

    @property
    def effective_reorder_point(self) -> int:
        return self.reorder_point if self.reorder_point is not None else self.product.reorder_point

    @property
    def effective_reorder_quantity(self) -> int:
        return self.reorder_quantity if self.reorder_quantity is not None else self.product.reorder_quantity


# -------------------------------------------------------------- warehouses
class Warehouse(TimestampMixin, Base):
    __tablename__ = "warehouses"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    address: Mapped[str] = mapped_column(String(300), default="")
    city: Mapped[str] = mapped_column(String(100), default="")
    country: Mapped[str] = mapped_column(String(100), default="")
    capacity_units: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None = unlimited
    used_units: Mapped[int] = mapped_column(Integer, default=0)  # all buckets, maintained atomically
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    locations: Mapped[list["Location"]] = relationship(back_populates="warehouse", order_by="Location.id")
    __table_args__ = (
        CheckConstraint("used_units >= 0", name="ck_warehouses_used_nonneg"),
        CheckConstraint("capacity_units IS NULL OR capacity_units >= 0", name="ck_warehouses_capacity_nonneg"),
        CheckConstraint("capacity_units IS NULL OR used_units <= capacity_units", name="ck_warehouses_within_capacity"),
    )


class Location(TimestampMixin, Base):
    """Storage location (zone/aisle/bin) inside a warehouse. Each warehouse has a default one."""

    __tablename__ = "locations"
    id: Mapped[int] = mapped_column(primary_key=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), index=True)
    code: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(200), default="")
    capacity_units: Mapped[int | None] = mapped_column(Integer, nullable=True)
    used_units: Mapped[int] = mapped_column(Integer, default=0)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    warehouse: Mapped[Warehouse] = relationship(back_populates="locations")
    __table_args__ = (
        UniqueConstraint("warehouse_id", "code", name="uq_locations_wh_code"),
        CheckConstraint("used_units >= 0", name="ck_locations_used_nonneg"),
        CheckConstraint("capacity_units IS NULL OR capacity_units >= 0", name="ck_locations_capacity_nonneg"),
        CheckConstraint("capacity_units IS NULL OR used_units <= capacity_units", name="ck_locations_within_capacity"),
    )


# --------------------------------------------------------------- inventory
class StockLevel(Base):
    __tablename__ = "stock_levels"
    id: Mapped[int] = mapped_column(primary_key=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), index=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"))
    variant_id: Mapped[int] = mapped_column(ForeignKey("product_variants.id"), index=True)
    available: Mapped[int] = mapped_column(Integer, default=0)
    reserved: Mapped[int] = mapped_column(Integer, default=0)
    damaged: Mapped[int] = mapped_column(Integer, default=0)
    in_transit: Mapped[int] = mapped_column(Integer, default=0)  # inbound, not yet usable
    avg_cost: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))  # moving weighted average
    version: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (
        UniqueConstraint("location_id", "variant_id", name="uq_stock_location_variant"),
        CheckConstraint("available >= 0", name="ck_stock_available_nonneg"),
        CheckConstraint("reserved >= 0", name="ck_stock_reserved_nonneg"),
        CheckConstraint("damaged >= 0", name="ck_stock_damaged_nonneg"),
        CheckConstraint("in_transit >= 0", name="ck_stock_in_transit_nonneg"),
    )

    @property
    def on_hand(self) -> int:
        return self.available + self.reserved + self.damaged


class StockMovement(Base):
    """Append-only ledger. Rows are never updated or deleted (enforced below)."""

    __tablename__ = "stock_movements"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    movement_type: Mapped[str] = mapped_column(String(30), index=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("product_variants.id"), index=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), index=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"))
    d_available: Mapped[int] = mapped_column(Integer, default=0)
    d_reserved: Mapped[int] = mapped_column(Integer, default=0)
    d_damaged: Mapped[int] = mapped_column(Integer, default=0)
    d_in_transit: Mapped[int] = mapped_column(Integer, default=0)
    available_after: Mapped[int] = mapped_column(Integer)
    reserved_after: Mapped[int] = mapped_column(Integer)
    damaged_after: Mapped[int] = mapped_column(Integer)
    in_transit_after: Mapped[int] = mapped_column(Integer)
    unit_cost: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    reference_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    reference_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason: Mapped[str] = mapped_column(String(500), default="")
    actor: Mapped[str] = mapped_column(String(100), default="system")
    __table_args__ = (Index("ix_movements_reference", "reference_type", "reference_id"),)


@event.listens_for(StockMovement, "before_update")
@event.listens_for(StockMovement, "before_delete")
def _movements_are_immutable(*_):
    raise RuntimeError("stock_movements is an append-only audit ledger")


# --------------------------------------------------------------- suppliers
class Supplier(TimestampMixin, Base):
    __tablename__ = "suppliers"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    contact_name: Mapped[str] = mapped_column(String(200), default="")
    email: Mapped[str] = mapped_column(String(200), default="")
    phone: Mapped[str] = mapped_column(String(50), default="")
    address: Mapped[str] = mapped_column(String(300), default="")
    payment_terms: Mapped[str] = mapped_column(String(100), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class SupplierProduct(TimestampMixin, Base):
    __tablename__ = "supplier_products"
    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), index=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("product_variants.id"), index=True)
    supplier_sku: Mapped[str] = mapped_column(String(64), default="")
    unit_price: Mapped[Decimal] = mapped_column(Money)
    lead_time_days: Mapped[int] = mapped_column(Integer, default=0)
    min_order_qty: Mapped[int] = mapped_column(Integer, default=1)
    is_preferred: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (
        UniqueConstraint("supplier_id", "variant_id", name="uq_supplier_variant"),
        CheckConstraint("min_order_qty >= 1", name="ck_supplier_products_moq"),
        CheckConstraint("lead_time_days >= 0", name="ck_supplier_products_lead"),
    )


class SupplierPriceHistory(Base):
    __tablename__ = "supplier_price_history"
    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_product_id: Mapped[int] = mapped_column(ForeignKey("supplier_products.id"), index=True)
    unit_price: Mapped[Decimal] = mapped_column(Money)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    changed_by: Mapped[str] = mapped_column(String(100), default="system")


# ---------------------------------------------------------- purchase orders
class PurchaseOrder(TimestampMixin, Base):
    __tablename__ = "purchase_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    po_number: Mapped[str] = mapped_column(String(32), unique=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), index=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), index=True)
    status: Mapped[str] = mapped_column(String(30), default="DRAFT", index=True)
    expected_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    subtotal: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    discount_total: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    tax_total: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    total: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    created_by: Mapped[str] = mapped_column(String(100), default="system")
    approved_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    supplier: Mapped[Supplier] = relationship()
    lines: Mapped[list["PurchaseOrderLine"]] = relationship(
        back_populates="po", cascade="all, delete-orphan", order_by="PurchaseOrderLine.id"
    )
    events: Mapped[list["PurchaseOrderEvent"]] = relationship(order_by="PurchaseOrderEvent.id")


class PurchaseOrderLine(Base):
    __tablename__ = "purchase_order_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    po_id: Mapped[int] = mapped_column(ForeignKey("purchase_orders.id", ondelete="CASCADE"), index=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("product_variants.id"), index=True)
    quantity: Mapped[int] = mapped_column(Integer)
    unit_cost: Mapped[Decimal] = mapped_column(Money)
    discount_pct: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    tax_pct: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    line_subtotal: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    line_discount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    line_tax: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    line_total: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    landed_unit_cost: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))  # (subtotal-discount)/qty
    received_quantity: Mapped[int] = mapped_column(Integer, default=0)  # includes damaged units
    damaged_quantity: Mapped[int] = mapped_column(Integer, default=0)
    po: Mapped[PurchaseOrder] = relationship(back_populates="lines")
    variant: Mapped[ProductVariant] = relationship()
    __table_args__ = (
        UniqueConstraint("po_id", "variant_id", name="uq_po_line_variant"),
        CheckConstraint("quantity >= 1", name="ck_po_lines_qty_pos"),
        CheckConstraint("received_quantity >= 0 AND received_quantity <= quantity", name="ck_po_lines_received_range"),
        CheckConstraint("damaged_quantity >= 0 AND damaged_quantity <= received_quantity", name="ck_po_lines_damaged_range"),
    )


class PurchaseOrderEvent(Base):
    """Approval-workflow audit trail."""

    __tablename__ = "purchase_order_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    po_id: Mapped[int] = mapped_column(ForeignKey("purchase_orders.id", ondelete="CASCADE"), index=True)
    from_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    to_status: Mapped[str] = mapped_column(String(30))
    actor: Mapped[str] = mapped_column(String(100))
    comment: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ----------------------------------------------------------- goods receipts
class GoodsReceipt(Base):
    __tablename__ = "goods_receipts"
    id: Mapped[int] = mapped_column(primary_key=True)
    receipt_number: Mapped[str] = mapped_column(String(32), unique=True)
    po_id: Mapped[int] = mapped_column(ForeignKey("purchase_orders.id"), index=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"))
    received_by: Mapped[str] = mapped_column(String(100), default="system")
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    notes: Mapped[str] = mapped_column(Text, default="")
    lines: Mapped[list["GoodsReceiptLine"]] = relationship(
        back_populates="receipt", cascade="all, delete-orphan", order_by="GoodsReceiptLine.id"
    )


class GoodsReceiptLine(Base):
    __tablename__ = "goods_receipt_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    receipt_id: Mapped[int] = mapped_column(ForeignKey("goods_receipts.id", ondelete="CASCADE"), index=True)
    po_line_id: Mapped[int] = mapped_column(ForeignKey("purchase_order_lines.id"), index=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("product_variants.id"))
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"))
    quantity_received: Mapped[int] = mapped_column(Integer)  # total delivered = good + damaged
    quantity_damaged: Mapped[int] = mapped_column(Integer, default=0)
    unit_cost: Mapped[Decimal] = mapped_column(Money)
    receipt: Mapped[GoodsReceipt] = relationship(back_populates="lines")
    __table_args__ = (
        CheckConstraint("quantity_received >= 1", name="ck_gr_lines_received_pos"),
        CheckConstraint("quantity_damaged >= 0 AND quantity_damaged <= quantity_received", name="ck_gr_lines_damaged_range"),
    )


# ---------------------------------------------------------------- transfers
class StockTransfer(TimestampMixin, Base):
    __tablename__ = "stock_transfers"
    id: Mapped[int] = mapped_column(primary_key=True)
    transfer_number: Mapped[str] = mapped_column(String(32), unique=True)
    from_warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), index=True)
    to_warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(100), default="system")
    lines: Mapped[list["StockTransferLine"]] = relationship(
        back_populates="transfer", cascade="all, delete-orphan", order_by="StockTransferLine.id"
    )
    __table_args__ = (CheckConstraint("from_warehouse_id <> to_warehouse_id", name="ck_transfers_distinct_wh"),)


class StockTransferLine(Base):
    __tablename__ = "stock_transfer_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    transfer_id: Mapped[int] = mapped_column(ForeignKey("stock_transfers.id", ondelete="CASCADE"), index=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("product_variants.id"))
    from_location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"))
    to_location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_cost: Mapped[Decimal | None] = mapped_column(Money, nullable=True)  # captured at dispatch
    transfer: Mapped[StockTransfer] = relationship(back_populates="lines")
    __table_args__ = (CheckConstraint("quantity >= 1", name="ck_transfer_lines_qty_pos"),)
