"""Pydantic request/response models."""
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

# Exact money on the wire: always a 4-decimal string (never a float).
Dec4 = Annotated[Decimal, PlainSerializer(lambda d: format(d.quantize(Decimal("0.0001")), "f"), return_type=str,
                                          when_used="json")]
Qty = Annotated[int, Field(ge=1, le=10_000_000)]
Price = Annotated[Decimal, Field(ge=0, max_digits=14, decimal_places=4)]
Pct = Annotated[Decimal, Field(ge=0, le=100, max_digits=7, decimal_places=4)]
Code = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._\-/]*$")]


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---- products
class VariantIn(Strict):
    sku: Code
    name: str = Field(min_length=1, max_length=200)
    attributes: dict[str, Any] = {}
    purchase_price: Price | None = None
    selling_price: Price | None = None
    reorder_point: int | None = Field(None, ge=0)
    reorder_quantity: int | None = Field(None, ge=0)


class VariantPatch(Strict):
    name: str | None = Field(None, min_length=1, max_length=200)
    attributes: dict[str, Any] | None = None
    purchase_price: Price | None = None
    selling_price: Price | None = None
    reorder_point: int | None = Field(None, ge=0)
    reorder_quantity: int | None = Field(None, ge=0)
    is_active: bool | None = None


class VariantOut(ORM):
    id: int
    product_id: int
    sku: str
    name: str
    attributes: dict[str, Any]
    purchase_price: Dec4 | None
    selling_price: Dec4 | None
    reorder_point: int | None
    reorder_quantity: int | None
    is_default: bool
    is_active: bool


class ProductIn(Strict):
    sku: Code
    name: str = Field(min_length=1, max_length=200)
    category: str = Field(min_length=1, max_length=100)
    description: str = ""
    uom: str = Field("each", min_length=1, max_length=20)
    purchase_price: Price = Decimal("0")
    selling_price: Price = Decimal("0")
    reorder_point: int = Field(0, ge=0)
    reorder_quantity: int = Field(0, ge=0)
    variants: list[VariantIn] = []


class ProductPatch(Strict):
    name: str | None = Field(None, min_length=1, max_length=200)
    category: str | None = Field(None, min_length=1, max_length=100)
    description: str | None = None
    uom: str | None = Field(None, min_length=1, max_length=20)
    purchase_price: Price | None = None
    selling_price: Price | None = None
    reorder_point: int | None = Field(None, ge=0)
    reorder_quantity: int | None = Field(None, ge=0)
    is_active: bool | None = None


class ProductOut(ORM):
    id: int
    sku: str
    name: str
    category: str
    description: str
    uom: str
    purchase_price: Dec4
    selling_price: Dec4
    reorder_point: int
    reorder_quantity: int
    is_active: bool
    variants: list[VariantOut]


# ---- warehouses
class WarehouseIn(Strict):
    code: Code
    name: str = Field(min_length=1, max_length=200)
    address: str = ""
    city: str = ""
    country: str = ""
    capacity_units: int | None = Field(None, ge=0)


class WarehousePatch(Strict):
    name: str | None = Field(None, min_length=1, max_length=200)
    address: str | None = None
    city: str | None = None
    country: str | None = None
    capacity_units: int | None = Field(None, ge=0)
    is_active: bool | None = None


class LocationIn(Strict):
    code: Code
    name: str = ""
    capacity_units: int | None = Field(None, ge=0)


class LocationPatch(Strict):
    name: str | None = None
    capacity_units: int | None = Field(None, ge=0)
    is_active: bool | None = None


class LocationOut(ORM):
    id: int
    warehouse_id: int
    code: str
    name: str
    capacity_units: int | None
    used_units: int
    is_default: bool
    is_active: bool


class WarehouseOut(ORM):
    id: int
    code: str
    name: str
    address: str
    city: str
    country: str
    capacity_units: int | None
    used_units: int
    is_active: bool
    locations: list[LocationOut]


# ---- suppliers
class SupplierIn(Strict):
    code: Code
    name: str = Field(min_length=1, max_length=200)
    contact_name: str = ""
    email: str = Field("", max_length=200, pattern=r"^$|^[^@\s]+@[^@\s]+\.[^@\s]+$")
    phone: str = ""
    address: str = ""
    payment_terms: str = ""


class SupplierPatch(Strict):
    name: str | None = Field(None, min_length=1, max_length=200)
    contact_name: str | None = None
    email: str | None = Field(None, max_length=200, pattern=r"^$|^[^@\s]+@[^@\s]+\.[^@\s]+$")
    phone: str | None = None
    address: str | None = None
    payment_terms: str | None = None
    is_active: bool | None = None


class SupplierOut(ORM):
    id: int
    code: str
    name: str
    contact_name: str
    email: str
    phone: str
    address: str
    payment_terms: str
    is_active: bool


class SupplierProductIn(Strict):
    variant_id: int
    supplier_sku: str = ""
    unit_price: Price
    lead_time_days: int = Field(0, ge=0)
    min_order_qty: int = Field(1, ge=1)
    is_preferred: bool = False


class SupplierProductOut(ORM):
    id: int
    supplier_id: int
    variant_id: int
    supplier_sku: str
    unit_price: Dec4
    lead_time_days: int
    min_order_qty: int
    is_preferred: bool


# ---- inventory
class StockLevelOut(ORM):
    id: int
    warehouse_id: int
    location_id: int
    variant_id: int
    available: int
    reserved: int
    damaged: int
    in_transit: int
    avg_cost: Dec4
    version: int


class StockInIn(Strict):
    warehouse_id: int
    variant_id: int
    quantity: Qty
    location_id: int | None = None
    unit_cost: Price | None = None
    reason: str = ""


class StockOutIn(Strict):
    warehouse_id: int
    variant_id: int
    quantity: Qty
    location_id: int | None = None
    from_reserved: bool = False
    reason: str = ""


class ReserveIn(Strict):
    warehouse_id: int
    variant_id: int
    quantity: Qty
    location_id: int | None = None
    reason: str = ""


class AdjustmentIn(Strict):
    warehouse_id: int
    variant_id: int
    adjustment_type: str
    quantity: int = Field(ge=0, le=10_000_000)
    reason: str = Field(min_length=1, max_length=500)
    location_id: int | None = None
    unit_cost: Price | None = None


class MovementOut(ORM):
    id: int
    created_at: datetime
    movement_type: str
    variant_id: int
    warehouse_id: int
    location_id: int
    d_available: int
    d_reserved: int
    d_damaged: int
    d_in_transit: int
    available_after: int
    reserved_after: int
    damaged_after: int
    in_transit_after: int
    unit_cost: Dec4 | None
    reference_type: str | None
    reference_id: int | None
    reason: str
    actor: str


# ---- transfers
class TransferLineIn(Strict):
    variant_id: int
    quantity: Qty
    from_location_id: int | None = None
    to_location_id: int | None = None


class TransferIn(Strict):
    from_warehouse_id: int
    to_warehouse_id: int
    lines: list[TransferLineIn] = Field(min_length=1)
    notes: str = ""


class TransferLineOut(ORM):
    id: int
    variant_id: int
    from_location_id: int
    to_location_id: int
    quantity: int
    unit_cost: Dec4 | None


class TransferOut(ORM):
    id: int
    transfer_number: str
    from_warehouse_id: int
    to_warehouse_id: int
    status: str
    notes: str
    created_by: str
    lines: list[TransferLineOut]


# ---- purchase orders
class POLineIn(Strict):
    variant_id: int
    quantity: Qty
    unit_cost: Price | None = None  # defaults to the supplier's listed price
    discount_pct: Pct = Decimal("0")
    tax_pct: Pct = Decimal("0")


class POIn(Strict):
    supplier_id: int
    warehouse_id: int
    lines: list[POLineIn] = Field(min_length=1)
    expected_date: datetime | None = None
    notes: str = ""


class POPatch(Strict):
    lines: list[POLineIn] | None = Field(None, min_length=1)
    expected_date: datetime | None = None
    notes: str | None = None


class POLineOut(ORM):
    id: int
    variant_id: int
    quantity: int
    unit_cost: Dec4
    discount_pct: Dec4
    tax_pct: Dec4
    line_subtotal: Dec4
    line_discount: Dec4
    line_tax: Dec4
    line_total: Dec4
    landed_unit_cost: Dec4
    received_quantity: int
    damaged_quantity: int


class POEventOut(ORM):
    from_status: str | None
    to_status: str
    actor: str
    comment: str
    created_at: datetime


class POOut(ORM):
    id: int
    po_number: str
    supplier_id: int
    warehouse_id: int
    status: str
    expected_date: datetime | None
    notes: str
    subtotal: Dec4
    discount_total: Dec4
    tax_total: Dec4
    total: Dec4
    created_by: str
    approved_by: str | None
    approved_at: datetime | None
    rejection_reason: str | None
    lines: list[POLineOut]
    events: list[POEventOut]


class CommentIn(Strict):
    comment: str = ""


class RejectIn(Strict):
    reason: str = Field(min_length=1, max_length=500)


# ---- goods receipts
class ReceiptLineIn(Strict):
    po_line_id: int
    quantity_received: Qty
    quantity_damaged: int = Field(0, ge=0)
    location_id: int | None = None


class ReceiptIn(Strict):
    lines: list[ReceiptLineIn] = Field(min_length=1)
    notes: str = ""


class ReceiptLineOut(ORM):
    id: int
    po_line_id: int
    variant_id: int
    location_id: int
    quantity_received: int
    quantity_damaged: int
    unit_cost: Dec4


class ReceiptOut(ORM):
    id: int
    receipt_number: str
    po_id: int
    warehouse_id: int
    received_by: str
    received_at: datetime
    notes: str
    lines: list[ReceiptLineOut]
