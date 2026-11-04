"""Inventory operations built on ``apply_stock_change``: stock in/out, reserve/release, adjustments."""
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.errors import InsufficientStock, ValidationFailed
from app.models import Location, StockLevel, StockMovement
from app.services import stock


def _positive(qty: int) -> None:
    if not isinstance(qty, int) or qty < 1:
        raise ValidationFailed("quantity must be a positive integer")


def stock_in(db: Session, *, warehouse_id: int, variant_id: int, quantity: int, unit_cost: Decimal | None = None,
             location_id: int | None = None, reason: str = "", actor: str = "system",
             reference_type: str | None = None, reference_id: int | None = None, movement_type: str = "STOCK_IN"):
    _positive(quantity)
    stock.get_warehouse(db, warehouse_id)
    variant = stock.get_variant(db, variant_id, active=True)
    loc = stock.resolve_location(db, warehouse_id, location_id)
    if unit_cost is None:
        existing = stock.current_avg_cost(db, loc.id, variant_id)
        unit_cost = existing if existing > 0 else variant.effective_purchase_price
    if Decimal(str(unit_cost)) < 0:
        raise ValidationFailed("unit_cost must be >= 0")
    return stock.apply_stock_change(
        db, location=loc, variant_id=variant_id, movement_type=movement_type, d_available=quantity,
        unit_cost=Decimal(str(unit_cost)), reason=reason, actor=actor,
        reference_type=reference_type, reference_id=reference_id,
    )


def _allocate(db: Session, warehouse_id: int, variant_id: int, quantity: int, bucket: str,
              location_id: int | None) -> list[tuple[Location, int]]:
    """Choose locations to satisfy ``quantity`` from ``bucket`` (explicit location, or greedy by id)."""
    if location_id is not None:
        return [(stock.resolve_location(db, warehouse_id, location_id), quantity)]
    rows = db.scalars(
        select(StockLevel)
        .where(StockLevel.warehouse_id == warehouse_id, StockLevel.variant_id == variant_id,
               getattr(StockLevel, bucket) > 0)
        .order_by(StockLevel.location_id).with_for_update()
    ).all()
    plan, remaining = [], quantity
    for lvl in rows:
        take = min(remaining, getattr(lvl, bucket))
        plan.append((db.get(Location, lvl.location_id), take))
        remaining -= take
        if remaining == 0:
            break
    if remaining:
        raise InsufficientStock(
            f"Only {quantity - remaining} {bucket} units available in warehouse", variant_id=variant_id,
            requested=quantity, available=quantity - remaining,
        )
    return plan


def stock_out(db: Session, *, warehouse_id: int, variant_id: int, quantity: int, location_id: int | None = None,
              from_reserved: bool = False, reason: str = "", actor: str = "system",
              reference_type: str | None = None, reference_id: int | None = None) -> list[StockMovement]:
    """Issue goods (sale/consumption). ``from_reserved`` consumes a prior reservation."""
    _positive(quantity)
    stock.get_warehouse(db, warehouse_id, active=False)
    stock.get_variant(db, variant_id)
    bucket = "reserved" if from_reserved else "available"
    out = []
    for loc, qty in _allocate(db, warehouse_id, variant_id, quantity, bucket, location_id):
        out.append(stock.apply_stock_change(
            db, location=loc, variant_id=variant_id, movement_type="STOCK_OUT",
            **{f"d_{bucket}": -qty}, reason=reason, actor=actor,
            reference_type=reference_type, reference_id=reference_id,
        ))
    return out


def reserve(db: Session, *, warehouse_id: int, variant_id: int, quantity: int, location_id: int | None = None,
            reason: str = "", actor: str = "system", reference_type: str | None = None,
            reference_id: int | None = None) -> list[StockMovement]:
    _positive(quantity)
    stock.get_warehouse(db, warehouse_id, active=False)
    stock.get_variant(db, variant_id)
    out = []
    for loc, qty in _allocate(db, warehouse_id, variant_id, quantity, "available", location_id):
        out.append(stock.apply_stock_change(
            db, location=loc, variant_id=variant_id, movement_type="RESERVE", d_available=-qty, d_reserved=qty,
            reason=reason, actor=actor, reference_type=reference_type, reference_id=reference_id,
        ))
    return out


def release(db: Session, *, warehouse_id: int, variant_id: int, quantity: int, location_id: int | None = None,
            reason: str = "", actor: str = "system", reference_type: str | None = None,
            reference_id: int | None = None) -> list[StockMovement]:
    _positive(quantity)
    stock.get_warehouse(db, warehouse_id, active=False)
    stock.get_variant(db, variant_id)
    out = []
    for loc, qty in _allocate(db, warehouse_id, variant_id, quantity, "reserved", location_id):
        out.append(stock.apply_stock_change(
            db, location=loc, variant_id=variant_id, movement_type="RELEASE", d_available=qty, d_reserved=-qty,
            reason=reason, actor=actor, reference_type=reference_type, reference_id=reference_id,
        ))
    return out


ADJUSTMENT_TYPES = {"INCREASE", "DECREASE", "DAMAGE", "WRITE_OFF_DAMAGED", "RECOUNT"}


def adjust(db: Session, *, warehouse_id: int, variant_id: int, adjustment_type: str, quantity: int,
           reason: str, location_id: int | None = None, unit_cost: Decimal | None = None,
           actor: str = "system") -> list[StockMovement]:
    """Manual stock adjustment; a non-empty reason is mandatory (audit).

    INCREASE/DECREASE change available; DAMAGE moves available -> damaged; WRITE_OFF_DAMAGED removes
    damaged units; RECOUNT sets available to the counted ``quantity`` (>= 0) at a location.
    """
    if adjustment_type not in ADJUSTMENT_TYPES:
        raise ValidationFailed(f"adjustment_type must be one of {sorted(ADJUSTMENT_TYPES)}")
    if not reason or not reason.strip():
        raise ValidationFailed("reason is required for stock adjustments")
    stock.get_warehouse(db, warehouse_id, active=False)
    stock.get_variant(db, variant_id)
    mt = "ADJUSTMENT"
    if adjustment_type == "INCREASE":
        _positive(quantity)
        return [stock_in(db, warehouse_id=warehouse_id, variant_id=variant_id, quantity=quantity,
                         unit_cost=unit_cost, location_id=location_id, reason=reason, actor=actor,
                         movement_type=mt)]
    if adjustment_type == "RECOUNT":
        if quantity < 0:
            raise ValidationFailed("counted quantity must be >= 0")
        loc = stock.resolve_location(db, warehouse_id, location_id)
        stock._get_or_create_level(db, loc, variant_id)
        current = db.scalars(select(StockLevel.available).where(
            StockLevel.location_id == loc.id, StockLevel.variant_id == variant_id)).one()
        diff = quantity - current
        if diff == 0:
            return []
        cost = None
        if diff > 0:
            existing = stock.current_avg_cost(db, loc.id, variant_id)
            cost = Decimal(str(unit_cost)) if unit_cost is not None else existing
        return [stock.apply_stock_change(db, location=loc, variant_id=variant_id, movement_type=mt,
                                         d_available=diff, unit_cost=cost, reason=f"RECOUNT: {reason}", actor=actor)]
    _positive(quantity)
    kw = {"DECREASE": {"d_available": -quantity},
          "DAMAGE": {"d_available": -quantity, "d_damaged": quantity},
          "WRITE_OFF_DAMAGED": {"d_damaged": -quantity}}[adjustment_type]
    out = []
    # Spread over locations only when none is specified.
    bucket = "damaged" if adjustment_type == "WRITE_OFF_DAMAGED" else "available"
    for loc, qty in _allocate(db, warehouse_id, variant_id, quantity, bucket, location_id):
        scaled = {k: (v // quantity) * qty for k, v in kw.items()}
        out.append(stock.apply_stock_change(db, location=loc, variant_id=variant_id, movement_type=mt,
                                            reason=f"{adjustment_type}: {reason}", actor=actor, **scaled))
    return out


# ------------------------------------------------------------------ queries
def stock_levels(db: Session, *, warehouse_id=None, variant_id=None, location_id=None, limit=100, offset=0):
    q = select(StockLevel).order_by(StockLevel.id).limit(limit).offset(offset)
    if warehouse_id is not None:
        q = q.where(StockLevel.warehouse_id == warehouse_id)
    if variant_id is not None:
        q = q.where(StockLevel.variant_id == variant_id)
    if location_id is not None:
        q = q.where(StockLevel.location_id == location_id)
    return db.scalars(q).all()


def variant_totals(db: Session, variant_id: int) -> dict:
    row = db.execute(
        select(func.coalesce(func.sum(StockLevel.available), 0), func.coalesce(func.sum(StockLevel.reserved), 0),
               func.coalesce(func.sum(StockLevel.damaged), 0), func.coalesce(func.sum(StockLevel.in_transit), 0))
        .where(StockLevel.variant_id == variant_id)
    ).one()
    return dict(zip(stock.BUCKETS, map(int, row)))


def movements(db: Session, *, variant_id=None, warehouse_id=None, movement_type=None, reference_type=None,
              reference_id=None, limit=100, offset=0):
    q = select(StockMovement).order_by(StockMovement.id.desc()).limit(limit).offset(offset)
    for col, val in ((StockMovement.variant_id, variant_id), (StockMovement.warehouse_id, warehouse_id),
                     (StockMovement.movement_type, movement_type), (StockMovement.reference_type, reference_type),
                     (StockMovement.reference_id, reference_id)):
        if val is not None:
            q = q.where(col == val)
    return db.scalars(q).all()
