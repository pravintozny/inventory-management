"""Stock core: the single choke-point through which every quantity change flows.

``apply_stock_change`` guarantees, inside the caller's transaction:
  1. no bucket (available/reserved/damaged/in_transit) can go negative;
  2. location + warehouse capacity counters are updated atomically with a conditional UPDATE;
  3. the stock row is changed with a version-checked UPDATE (optimistic) *and* read under
     ``SELECT ... FOR UPDATE`` on databases that support it (pessimistic), retried on conflict;
  4. an immutable ``StockMovement`` ledger row with before/after balances is written.
Because everything runs in the request's single transaction, a failure anywhere (e.g. one
line of a multi-line transfer) rolls back the whole operation.
"""
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import (
    CapacityExceeded, ConcurrencyConflict, InsufficientStock, NotFound, ValidationFailed,
)
from app.models import (
    Location, ProductVariant, Q4, StockLevel, StockMovement, Warehouse,
)

MAX_RETRIES = 8
BUCKETS = ("available", "reserved", "damaged", "in_transit")


def money(value) -> Decimal:
    return Decimal(str(value)).quantize(Q4, rounding=ROUND_HALF_UP)


# ------------------------------------------------------------ lookups
def get_variant(db: Session, variant_id: int, *, active: bool = False) -> ProductVariant:
    v = db.get(ProductVariant, variant_id)
    if v is None:
        raise NotFound(f"Variant {variant_id} not found")
    if active and not (v.is_active and v.product.is_active):
        raise ValidationFailed(f"Variant {v.sku} is inactive")
    return v


def get_warehouse(db: Session, warehouse_id: int, *, active: bool = True) -> Warehouse:
    w = db.get(Warehouse, warehouse_id)
    if w is None:
        raise NotFound(f"Warehouse {warehouse_id} not found")
    if active and not w.is_active:
        raise ValidationFailed(f"Warehouse {w.code} is inactive")
    return w


def resolve_location(db: Session, warehouse_id: int, location_id: int | None) -> Location:
    """Validate that ``location_id`` belongs to the warehouse; default to its default location."""
    if location_id is None:
        loc = db.scalars(
            select(Location).where(Location.warehouse_id == warehouse_id, Location.is_default.is_(True))
        ).first()
        if loc is None:
            raise NotFound(f"Warehouse {warehouse_id} has no default location")
        return loc
    loc = db.get(Location, location_id)
    if loc is None:
        raise NotFound(f"Location {location_id} not found")
    if loc.warehouse_id != warehouse_id:
        raise ValidationFailed(f"Location {location_id} does not belong to warehouse {warehouse_id}")
    if not loc.is_active:
        raise ValidationFailed(f"Location {loc.code} is inactive")
    return loc


# --------------------------------------------------------- stock rows
def _level_query(location_id: int, variant_id: int):
    return (
        select(StockLevel)
        .where(StockLevel.location_id == location_id, StockLevel.variant_id == variant_id)
        .execution_options(populate_existing=True)
        .with_for_update()  # no-op on SQLite
    )


def _get_or_create_level(db: Session, loc: Location, variant_id: int) -> StockLevel:
    level = db.scalars(_level_query(loc.id, variant_id)).first()
    if level is not None:
        return level
    try:
        with db.begin_nested():  # savepoint: a concurrent creator makes us fall through
            db.add(StockLevel(warehouse_id=loc.warehouse_id, location_id=loc.id, variant_id=variant_id))
            db.flush()
    except IntegrityError:
        pass
    return db.scalars(_level_query(loc.id, variant_id)).one()


def _bump_counter(db: Session, model, row_id: int, net: int, label: str) -> None:
    """used_units += net, refusing to exceed capacity or drop below zero (atomic in SQL)."""
    if net == 0:
        return
    stmt = update(model).where(model.id == row_id)
    if net > 0:
        stmt = stmt.where(or_(model.capacity_units.is_(None), model.used_units + net <= model.capacity_units))
    else:
        stmt = stmt.where(model.used_units + net >= 0)
    res = db.execute(stmt.values(used_units=model.used_units + net).execution_options(synchronize_session=False))
    if res.rowcount != 1:
        if net > 0:
            raise CapacityExceeded(f"{label} capacity exceeded", required=net)
        raise ConcurrencyConflict(f"{label} usage counter would become negative")


def apply_stock_change(
    db: Session,
    *,
    location: Location,
    variant_id: int,
    movement_type: str,
    d_available: int = 0,
    d_reserved: int = 0,
    d_damaged: int = 0,
    d_in_transit: int = 0,
    unit_cost: Decimal | None = None,
    reference_type: str | None = None,
    reference_id: int | None = None,
    reason: str = "",
    actor: str = "system",
) -> StockMovement:
    """Apply a signed delta to the four buckets of one (location, variant) row.

    ``unit_cost`` is the cost of units *entering* the valued set (available + reserved); it is
    blended into the moving weighted-average cost. Pure re-bucketing needs no cost.
    """
    deltas = {"available": d_available, "reserved": d_reserved, "damaged": d_damaged, "in_transit": d_in_transit}
    if not any(deltas.values()):
        raise ValidationFailed("Stock change must alter at least one bucket")
    valued_delta = d_available + d_reserved
    if valued_delta > 0 and unit_cost is None:
        raise ValidationFailed("unit_cost is required when units enter valued stock")
    net = sum(deltas.values())

    for attempt in range(MAX_RETRIES):
        level = _get_or_create_level(db, location, variant_id)
        new = {b: getattr(level, b) + d for b, d in deltas.items()}
        short = {b: {"have": getattr(level, b), "change": deltas[b]} for b, v in new.items() if v < 0}
        if short:
            raise InsufficientStock(
                "Insufficient stock for requested change",
                variant_id=variant_id, location_id=location.id, buckets=short,
            )
        valued_before = level.available + level.reserved
        new_avg = level.avg_cost
        if valued_delta > 0:
            total = valued_before + valued_delta
            new_avg = money((level.avg_cost * valued_before + money(unit_cost) * valued_delta) / total)
        cost_snapshot = level.avg_cost if valued_delta <= 0 else money(unit_cost)

        res = db.execute(
            update(StockLevel)
            .where(StockLevel.id == level.id, StockLevel.version == level.version)
            .values(**new, avg_cost=new_avg, version=level.version + 1)
            .execution_options(synchronize_session=False)
        )
        if res.rowcount == 1:
            break
        db.expire(level)  # lost an optimistic race: re-read and retry
    else:
        raise ConcurrencyConflict("Could not update stock after repeated conflicts; please retry")

    _bump_counter(db, Location, location.id, net, f"Location {location.code}")
    _bump_counter(db, Warehouse, location.warehouse_id, net, f"Warehouse {location.warehouse_id}")
    db.expire(level)
    db.expire(location, ["used_units"])
    db.expire(location.warehouse, ["used_units"])

    movement = StockMovement(
        movement_type=movement_type, variant_id=variant_id, warehouse_id=location.warehouse_id,
        location_id=location.id, d_available=d_available, d_reserved=d_reserved, d_damaged=d_damaged,
        d_in_transit=d_in_transit, available_after=new["available"], reserved_after=new["reserved"],
        damaged_after=new["damaged"], in_transit_after=new["in_transit"], unit_cost=cost_snapshot,
        reference_type=reference_type, reference_id=reference_id, reason=reason, actor=actor,
    )
    db.add(movement)
    db.flush()
    return movement


def current_avg_cost(db: Session, location_id: int, variant_id: int) -> Decimal:
    level = db.scalars(
        select(StockLevel).where(StockLevel.location_id == location_id, StockLevel.variant_id == variant_id)
    ).first()
    return level.avg_cost if level else Decimal("0")
