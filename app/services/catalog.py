"""Products/variants, warehouses/locations and suppliers/supplier-products."""
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import Conflict, NotFound, ValidationFailed
from app.models import (
    Location, Product, ProductVariant, StockLevel, Supplier, SupplierPriceHistory, SupplierProduct, Warehouse,
)
from app.services import stock


def _flush(db: Session, what: str) -> None:
    try:
        db.flush()
    except IntegrityError as e:  # unique / FK / check violations -> clean 409
        db.rollback()
        raise Conflict(f"{what} violates a uniqueness or integrity constraint", detail=str(e.orig)) from e


# ---------------------------------------------------------------- products
def create_product(db: Session, data: dict) -> Product:
    variants = data.pop("variants", None) or []
    product = Product(**data)
    skus = [v["sku"] for v in variants]
    if len(set(skus)) != len(skus):
        raise ValidationFailed("Duplicate variant SKUs in request")
    if not variants:
        product.variants.append(ProductVariant(sku=product.sku, name=product.name, attributes={}, is_default=True))
    for v in variants:
        product.variants.append(ProductVariant(**v))
    db.add(product)
    _flush(db, "Product")
    return product


def get_product(db: Session, product_id: int) -> Product:
    p = db.get(Product, product_id)
    if p is None:
        raise NotFound(f"Product {product_id} not found")
    return p


def list_products(db: Session, *, category=None, q=None, active=None, limit=100, offset=0):
    stmt = select(Product).order_by(Product.id).limit(limit).offset(offset)
    if category:
        stmt = stmt.where(Product.category == category)
    if active is not None:
        stmt = stmt.where(Product.is_active.is_(active))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(Product.name.ilike(like) | Product.sku.ilike(like))
    return db.scalars(stmt).all()


def update_product(db: Session, product_id: int, data: dict) -> Product:
    p = get_product(db, product_id)
    for k, v in data.items():
        setattr(p, k, v)
    _flush(db, "Product")
    return p


def add_variant(db: Session, product_id: int, data: dict) -> ProductVariant:
    p = get_product(db, product_id)
    # Once a product gains real variants the implicit default variant stays valid but is
    # kept (it may already hold stock); callers can deactivate it.
    v = ProductVariant(product_id=p.id, **data)
    db.add(v)
    _flush(db, "Variant")
    return v


def update_variant(db: Session, variant_id: int, data: dict) -> ProductVariant:
    v = stock.get_variant(db, variant_id)
    for k, val in data.items():
        setattr(v, k, val)
    _flush(db, "Variant")
    return v


def delete_product(db: Session, product_id: int) -> None:
    p = get_product(db, product_id)
    ids = [v.id for v in p.variants]
    has_stock = db.scalar(select(func.count()).select_from(StockLevel).where(StockLevel.variant_id.in_(ids)))
    if has_stock:
        raise Conflict("Product has inventory history; deactivate it instead of deleting")
    db.delete(p)
    _flush(db, "Product")


# -------------------------------------------------------------- warehouses
def create_warehouse(db: Session, data: dict) -> Warehouse:
    w = Warehouse(**data)
    w.locations.append(Location(code="DEFAULT", name="Default location", is_default=True))
    db.add(w)
    _flush(db, "Warehouse")
    return w


def get_warehouse(db: Session, warehouse_id: int) -> Warehouse:
    return stock.get_warehouse(db, warehouse_id, active=False)


def update_warehouse(db: Session, warehouse_id: int, data: dict) -> Warehouse:
    w = get_warehouse(db, warehouse_id)
    cap = data.get("capacity_units", w.capacity_units)
    if "capacity_units" in data and cap is not None and cap < w.used_units:
        raise Conflict(f"Capacity {cap} is below current usage {w.used_units}")
    for k, v in data.items():
        setattr(w, k, v)
    _flush(db, "Warehouse")
    return w


def create_location(db: Session, warehouse_id: int, data: dict) -> Location:
    w = get_warehouse(db, warehouse_id)
    cap = data.get("capacity_units")
    if cap is not None and w.capacity_units is not None:
        allocated = db.scalar(select(func.coalesce(func.sum(Location.capacity_units), 0)).where(
            Location.warehouse_id == w.id)) or 0
        if allocated + cap > w.capacity_units:
            raise Conflict("Sum of location capacities would exceed warehouse capacity")
    loc = Location(warehouse_id=w.id, **data)
    db.add(loc)
    _flush(db, "Location")
    return loc


def update_location(db: Session, location_id: int, data: dict) -> Location:
    loc = db.get(Location, location_id)
    if loc is None:
        raise NotFound(f"Location {location_id} not found")
    if "capacity_units" in data and data["capacity_units"] is not None and data["capacity_units"] < loc.used_units:
        raise Conflict(f"Capacity {data['capacity_units']} is below current usage {loc.used_units}")
    if data.get("is_active") is False and (loc.is_default or loc.used_units > 0):
        raise Conflict("Cannot deactivate the default location or a location that still holds stock")
    for k, v in data.items():
        setattr(loc, k, v)
    _flush(db, "Location")
    return loc


def utilization(w: Warehouse) -> dict:
    pct = None if not w.capacity_units else round(100 * w.used_units / w.capacity_units, 2)
    return {"capacity_units": w.capacity_units, "used_units": w.used_units, "utilization_pct": pct}


# --------------------------------------------------------------- suppliers
def create_supplier(db: Session, data: dict) -> Supplier:
    s = Supplier(**data)
    db.add(s)
    _flush(db, "Supplier")
    return s


def get_supplier(db: Session, supplier_id: int) -> Supplier:
    s = db.get(Supplier, supplier_id)
    if s is None:
        raise NotFound(f"Supplier {supplier_id} not found")
    return s


def update_supplier(db: Session, supplier_id: int, data: dict) -> Supplier:
    s = get_supplier(db, supplier_id)
    for k, v in data.items():
        setattr(s, k, v)
    _flush(db, "Supplier")
    return s


def link_supplier_product(db: Session, supplier_id: int, data: dict, actor: str = "system") -> SupplierProduct:
    """Create or update the supplier<->variant relationship; price changes are versioned."""
    get_supplier(db, supplier_id)
    stock.get_variant(db, data["variant_id"])
    sp = db.scalars(select(SupplierProduct).where(
        SupplierProduct.supplier_id == supplier_id, SupplierProduct.variant_id == data["variant_id"])).first()
    if sp is None:
        sp = SupplierProduct(supplier_id=supplier_id, **data)
        db.add(sp)
        db.flush()
        db.add(SupplierPriceHistory(supplier_product_id=sp.id, unit_price=sp.unit_price, changed_by=actor))
    else:
        old = sp.unit_price
        for k, v in data.items():
            setattr(sp, k, v)
        if Decimal(str(data["unit_price"])) != old:
            db.add(SupplierPriceHistory(supplier_product_id=sp.id, unit_price=data["unit_price"], changed_by=actor))
    if sp.is_preferred:  # at most one preferred supplier per variant
        for other in db.scalars(select(SupplierProduct).where(
                SupplierProduct.variant_id == sp.variant_id, SupplierProduct.id != sp.id,
                SupplierProduct.is_preferred.is_(True))):
            other.is_preferred = False
    _flush(db, "Supplier product")
    return sp


def unlink_supplier_product(db: Session, supplier_id: int, variant_id: int) -> None:
    sp = db.scalars(select(SupplierProduct).where(
        SupplierProduct.supplier_id == supplier_id, SupplierProduct.variant_id == variant_id)).first()
    if sp is None:
        raise NotFound("Supplier does not supply this variant")
    db.query(SupplierPriceHistory).filter_by(supplier_product_id=sp.id).delete()
    db.delete(sp)
    _flush(db, "Supplier product")
