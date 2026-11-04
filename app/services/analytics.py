"""Read-only inventory analytics."""
import math
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Location, Product, ProductVariant, PurchaseOrder, PurchaseOrderLine, StockLevel, StockMovement, SupplierProduct,
    Warehouse, utcnow,
)

ZERO = Decimal("0")


def _totals_by_variant(db: Session) -> dict[int, dict]:
    rows = db.execute(
        select(StockLevel.variant_id, func.sum(StockLevel.available), func.sum(StockLevel.reserved),
               func.sum(StockLevel.damaged), func.sum(StockLevel.in_transit))
        .join(Warehouse, Warehouse.id == StockLevel.warehouse_id).where(Warehouse.is_active.is_(True))
        .group_by(StockLevel.variant_id)).all()
    return {r[0]: {"available": int(r[1]), "reserved": int(r[2]), "damaged": int(r[3]), "in_transit": int(r[4])}
            for r in rows}


def _active_variants(db: Session):
    return db.scalars(select(ProductVariant).join(Product).where(
        ProductVariant.is_active.is_(True), Product.is_active.is_(True)).order_by(ProductVariant.id)).all()


def _open_po_qty(db: Session) -> dict[int, int]:
    rows = db.execute(
        select(PurchaseOrderLine.variant_id, func.sum(PurchaseOrderLine.quantity - PurchaseOrderLine.received_quantity))
        .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.po_id)
        .where(PurchaseOrder.status.in_(("PENDING_APPROVAL", "APPROVED", "PARTIALLY_RECEIVED")))
        .group_by(PurchaseOrderLine.variant_id)).all()
    return {r[0]: int(r[1]) for r in rows}


def low_stock(db: Session) -> list[dict]:
    """Variants whose sellable (available) stock across active warehouses is at/below the reorder point."""
    totals = _totals_by_variant(db)
    out = []
    for v in _active_variants(db):
        rp = v.effective_reorder_point
        avail = totals.get(v.id, {}).get("available", 0)
        if rp > 0 and avail <= rp:
            out.append({"variant_id": v.id, "sku": v.sku, "name": v.name, "category": v.product.category,
                        "available": avail, "reorder_point": rp, "shortfall": rp - avail})
    return sorted(out, key=lambda r: (r["available"] - r["reorder_point"], r["sku"]))


def valuation(db: Session) -> dict:
    """Moving-weighted-average valuation of owned, sellable-or-reserved stock (available + reserved).

    Damaged and in-transit units are reported separately and excluded from the valuation total.
    """
    rows = db.execute(
        select(StockLevel, Warehouse.code, ProductVariant.sku, Product.category)
        .join(Warehouse, Warehouse.id == StockLevel.warehouse_id)
        .join(ProductVariant, ProductVariant.id == StockLevel.variant_id)
        .join(Product, Product.id == ProductVariant.product_id)).all()
    by_wh, by_cat, by_var = {}, {}, {}
    grand = ZERO
    for lvl, wh, sku, cat in rows:
        qty = lvl.available + lvl.reserved
        val = lvl.avg_cost * qty
        grand += val
        for bucket, key in ((by_wh, wh), (by_cat, cat)):
            b = bucket.setdefault(key, {"quantity": 0, "value": ZERO, "damaged_units": 0, "in_transit_units": 0})
            b["quantity"] += qty
            b["value"] += val
            b["damaged_units"] += lvl.damaged
            b["in_transit_units"] += lvl.in_transit
        v = by_var.setdefault(sku, {"sku": sku, "quantity": 0, "value": ZERO})
        v["quantity"] += qty
        v["value"] += val
    for v in by_var.values():
        v["avg_cost"] = (v["value"] / v["quantity"]).quantize(Decimal("0.0001")) if v["quantity"] else ZERO
    return {
        "method": "moving_weighted_average", "total_value": grand,
        "by_warehouse": [{"warehouse": k, **v} for k, v in sorted(by_wh.items())],
        "by_category": [{"category": k, **v} for k, v in sorted(by_cat.items())],
        "by_variant": sorted(by_var.values(), key=lambda r: r["sku"]),
    }


def movers(db: Session, *, days: int = 90, fast_min_units: int = 50, slow_max_units: int = 5) -> dict:
    """Classify variants by units issued (STOCK_OUT) in the trailing window.

    FAST: issued >= fast_min_units. SLOW: holds stock but issued <= slow_max_units (0 => dead stock).
    """
    since = utcnow() - timedelta(days=days)
    issued = dict(db.execute(
        select(StockMovement.variant_id, func.sum(-(StockMovement.d_available + StockMovement.d_reserved)))
        .where(StockMovement.movement_type == "STOCK_OUT", StockMovement.created_at >= since)
        .group_by(StockMovement.variant_id)).all())
    totals = _totals_by_variant(db)
    fast, slow = [], []
    for v in _active_variants(db):
        units = int(issued.get(v.id, 0))
        held = totals.get(v.id, {}).get("available", 0) + totals.get(v.id, {}).get("reserved", 0)
        row = {"variant_id": v.id, "sku": v.sku, "name": v.name, "units_issued": units,
               "units_per_day": round(units / days, 3), "on_hand": held,
               "days_of_cover": None if units == 0 else round(held / (units / days), 1)}
        if units >= fast_min_units:
            fast.append(row)
        elif held > 0 and units <= slow_max_units:
            slow.append(row)
    return {"window_days": days, "fast_min_units": fast_min_units, "slow_max_units": slow_max_units,
            "fast_movers": sorted(fast, key=lambda r: -r["units_issued"]),
            "slow_movers": sorted(slow, key=lambda r: (r["units_issued"], -r["on_hand"]))}


def warehouse_utilization(db: Session) -> list[dict]:
    out = []
    for w in db.scalars(select(Warehouse).order_by(Warehouse.id)):
        locs = db.scalars(select(Location).where(Location.warehouse_id == w.id).order_by(Location.id)).all()
        pct = lambda used, cap: None if not cap else round(100 * used / cap, 2)
        out.append({
            "warehouse_id": w.id, "code": w.code, "name": w.name, "is_active": w.is_active,
            "capacity_units": w.capacity_units, "used_units": w.used_units,
            "free_units": None if w.capacity_units is None else w.capacity_units - w.used_units,
            "utilization_pct": pct(w.used_units, w.capacity_units),
            "locations": [{"location_id": l.id, "code": l.code, "capacity_units": l.capacity_units,
                           "used_units": l.used_units, "utilization_pct": pct(l.used_units, l.capacity_units)}
                          for l in locs],
        })
    return out


def reorder_recommendations(db: Session) -> list[dict]:
    """Suggest purchase quantities.

    inventory position = available + in_transit + open PO quantity still to arrive.
    If position <= reorder_point, order up to (reorder_point + reorder_quantity), rounded up to the
    chosen supplier's minimum order quantity. Preferred supplier wins, otherwise the cheapest.
    """
    totals, on_order = _totals_by_variant(db), _open_po_qty(db)
    out = []
    for v in _active_variants(db):
        rp, rq = v.effective_reorder_point, v.effective_reorder_quantity
        if rp <= 0:
            continue
        t = totals.get(v.id, {})
        position = t.get("available", 0) + t.get("in_transit", 0) + on_order.get(v.id, 0)
        if position > rp:
            continue
        sps = db.scalars(select(SupplierProduct).where(SupplierProduct.variant_id == v.id)).all()
        sp = min(sps, key=lambda s: (not s.is_preferred, s.unit_price, s.id)) if sps else None
        qty = max(rp + max(rq, 1) - position, 1)
        if sp and sp.min_order_qty > 1:
            qty = int(math.ceil(qty / sp.min_order_qty) * sp.min_order_qty)
        out.append({
            "variant_id": v.id, "sku": v.sku, "name": v.name, "available": t.get("available", 0),
            "in_transit": t.get("in_transit", 0), "on_order": on_order.get(v.id, 0), "reorder_point": rp,
            "recommended_quantity": qty,
            "supplier_id": sp.supplier_id if sp else None, "unit_price": sp.unit_price if sp else None,
            "estimated_cost": (sp.unit_price * qty) if sp else None,
            "lead_time_days": sp.lead_time_days if sp else None,
        })
    return sorted(out, key=lambda r: r["sku"])


def dashboard(db: Session) -> dict:
    val = valuation(db)
    return {"total_stock_value": val["total_value"], "low_stock_count": len(low_stock(db)),
            "reorder_recommendation_count": len(reorder_recommendations(db)),
            "warehouse_count": db.scalar(select(func.count()).select_from(Warehouse)),
            "variant_count": db.scalar(select(func.count()).select_from(ProductVariant))}
