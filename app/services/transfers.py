"""Inter-warehouse transfers: PENDING -> IN_TRANSIT -> RECEIVED (or CANCELLED).

Dispatch moves stock out of the source (available -> gone) and into the destination's
``in_transit`` bucket in one transaction, so units are never lost nor double counted. Receipt
moves ``in_transit -> available`` at the destination, blending the carried unit cost into its
weighted-average cost. Cancelling an in-transit transfer returns the units to the source.
"""
import uuid

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.errors import InvalidState, NotFound, ValidationFailed
from app.models import StockTransfer, StockTransferLine
from app.services import stock


def create_transfer(db: Session, *, from_warehouse_id: int, to_warehouse_id: int, lines: list[dict],
                    notes: str = "", actor: str = "system") -> StockTransfer:
    if from_warehouse_id == to_warehouse_id:
        raise ValidationFailed("Source and destination warehouses must differ")
    stock.get_warehouse(db, from_warehouse_id)
    stock.get_warehouse(db, to_warehouse_id)
    if not lines:
        raise ValidationFailed("A transfer needs at least one line")
    seen = set()
    t = StockTransfer(transfer_number=f"TMP-{uuid.uuid4().hex}", from_warehouse_id=from_warehouse_id,
                      to_warehouse_id=to_warehouse_id, notes=notes, created_by=actor)
    for ln in lines:
        if ln["quantity"] < 1:
            raise ValidationFailed("Line quantity must be >= 1")
        stock.get_variant(db, ln["variant_id"], active=True)
        src = stock.resolve_location(db, from_warehouse_id, ln.get("from_location_id"))
        dst = stock.resolve_location(db, to_warehouse_id, ln.get("to_location_id"))
        key = (ln["variant_id"], src.id, dst.id)
        if key in seen:
            raise ValidationFailed("Duplicate transfer line", variant_id=ln["variant_id"])
        seen.add(key)
        t.lines.append(StockTransferLine(variant_id=ln["variant_id"], from_location_id=src.id,
                                         to_location_id=dst.id, quantity=ln["quantity"]))
    db.add(t)
    db.flush()
    t.transfer_number = f"TR-{t.id:06d}"
    db.flush()
    return t


def get_transfer(db: Session, transfer_id: int) -> StockTransfer:
    t = db.get(StockTransfer, transfer_id)
    if t is None:
        raise NotFound(f"Transfer {transfer_id} not found")
    return t


def _move_status(db: Session, t: StockTransfer, frm: str, to: str) -> None:
    res = db.execute(update(StockTransfer).where(StockTransfer.id == t.id, StockTransfer.status == frm)
                     .values(status=to).execution_options(synchronize_session=False))
    if res.rowcount != 1:
        raise InvalidState(f"Transfer is no longer {frm}; reload and retry")
    db.expire(t, ["status"])


def _locked(db: Session, transfer_id: int) -> StockTransfer:
    t = db.scalars(select(StockTransfer).where(StockTransfer.id == transfer_id)
                   .execution_options(populate_existing=True).with_for_update()).first()
    if t is None:
        raise NotFound(f"Transfer {transfer_id} not found")
    return t


def _ordered(t: StockTransfer):
    return sorted(t.lines, key=lambda l: (l.variant_id, l.from_location_id, l.to_location_id))


def dispatch(db: Session, transfer_id: int, actor: str = "system") -> StockTransfer:
    t = _locked(db, transfer_id)
    if t.status != "PENDING":
        raise InvalidState(f"Only PENDING transfers can be dispatched (status is {t.status})")
    _move_status(db, t, "PENDING", "IN_TRANSIT")
    for ln in _ordered(t):
        src = stock.resolve_location(db, t.from_warehouse_id, ln.from_location_id)
        dst = stock.resolve_location(db, t.to_warehouse_id, ln.to_location_id)
        out = stock.apply_stock_change(
            db, location=src, variant_id=ln.variant_id, movement_type="TRANSFER_OUT", d_available=-ln.quantity,
            reference_type="TRANSFER", reference_id=t.id, reason=f"Dispatch {t.transfer_number}", actor=actor)
        ln.unit_cost = out.unit_cost  # source's average cost travels with the goods
        stock.apply_stock_change(
            db, location=dst, variant_id=ln.variant_id, movement_type="TRANSFER_IN_TRANSIT",
            d_in_transit=ln.quantity, reference_type="TRANSFER", reference_id=t.id,
            reason=f"Inbound {t.transfer_number}", actor=actor)
    db.flush()
    return t


def receive(db: Session, transfer_id: int, actor: str = "system") -> StockTransfer:
    t = _locked(db, transfer_id)
    if t.status != "IN_TRANSIT":
        raise InvalidState(f"Only IN_TRANSIT transfers can be received (status is {t.status})")
    _move_status(db, t, "IN_TRANSIT", "RECEIVED")
    for ln in _ordered(t):
        dst = stock.resolve_location(db, t.to_warehouse_id, ln.to_location_id)
        stock.apply_stock_change(
            db, location=dst, variant_id=ln.variant_id, movement_type="TRANSFER_IN", d_in_transit=-ln.quantity,
            d_available=ln.quantity, unit_cost=ln.unit_cost, reference_type="TRANSFER", reference_id=t.id,
            reason=f"Received {t.transfer_number}", actor=actor)
    db.flush()
    return t


def cancel(db: Session, transfer_id: int, actor: str = "system") -> StockTransfer:
    t = _locked(db, transfer_id)
    if t.status == "PENDING":
        _move_status(db, t, "PENDING", "CANCELLED")
        return t
    if t.status != "IN_TRANSIT":
        raise InvalidState(f"Cannot cancel a transfer in status {t.status}")
    _move_status(db, t, "IN_TRANSIT", "CANCELLED")
    for ln in _ordered(t):
        src = stock.resolve_location(db, t.from_warehouse_id, ln.from_location_id)
        dst = stock.resolve_location(db, t.to_warehouse_id, ln.to_location_id)
        stock.apply_stock_change(
            db, location=dst, variant_id=ln.variant_id, movement_type="TRANSFER_CANCEL", d_in_transit=-ln.quantity,
            reference_type="TRANSFER", reference_id=t.id, reason=f"Cancelled {t.transfer_number}", actor=actor)
        stock.apply_stock_change(
            db, location=src, variant_id=ln.variant_id, movement_type="TRANSFER_RETURN", d_available=ln.quantity,
            unit_cost=ln.unit_cost, reference_type="TRANSFER", reference_id=t.id,
            reason=f"Returned to source {t.transfer_number}", actor=actor)
    db.flush()
    return t


def list_transfers(db: Session, *, status=None, limit=100, offset=0):
    q = select(StockTransfer).order_by(StockTransfer.id.desc()).limit(limit).offset(offset)
    if status:
        q = q.where(StockTransfer.status == status)
    return db.scalars(q).all()
