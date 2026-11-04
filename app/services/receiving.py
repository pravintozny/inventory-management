"""Goods receipts: partial/complete receiving against approved purchase orders."""
import uuid
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.errors import InvalidState, NotFound, OverReceipt, ValidationFailed
from app.models import GoodsReceipt, GoodsReceiptLine, PurchaseOrder, PurchaseOrderLine
from app.services import purchasing, stock


def receive_goods(db: Session, po_id: int, lines: list[dict], *, actor: str = "system", notes: str = "") -> GoodsReceipt:
    """Receive goods against a PO.

    ``quantity_received`` is everything physically delivered on the line; ``quantity_damaged`` is the
    subset that is unusable. Good units become *available*, damaged units go to the *damaged* bucket.
    Both count against the ordered quantity (no over-receipt). The PO row is locked first so receipts
    on one PO are serialized and the resulting PO status is always computed from a consistent view.
    """
    po = purchasing.get_po(db, po_id, lock=True)
    if po.status not in purchasing.OPEN_STATUSES:
        raise InvalidState(f"Cannot receive goods for a purchase order in status {po.status}")
    if not lines:
        raise ValidationFailed("A receipt needs at least one line")
    stock.get_warehouse(db, po.warehouse_id, active=False)

    by_id = {l.id: l for l in po.lines}
    seen = set()
    for ln in lines:
        if ln["po_line_id"] not in by_id:
            raise ValidationFailed(f"PO line {ln['po_line_id']} does not belong to {po.po_number}")
        if ln["po_line_id"] in seen:
            raise ValidationFailed(f"PO line {ln['po_line_id']} listed twice")
        seen.add(ln["po_line_id"])
        rec, dmg = ln["quantity_received"], ln.get("quantity_damaged", 0)
        if rec < 1:
            raise ValidationFailed("quantity_received must be >= 1")
        if dmg < 0 or dmg > rec:
            raise ValidationFailed("quantity_damaged must be between 0 and quantity_received")

    receipt = GoodsReceipt(receipt_number=f"TMP-{uuid.uuid4().hex}", po_id=po.id, warehouse_id=po.warehouse_id,
                           received_by=actor, notes=notes)
    db.add(receipt)
    db.flush()
    receipt.receipt_number = f"GR-{receipt.id:06d}"

    for ln in sorted(lines, key=lambda x: (by_id[x["po_line_id"]].variant_id, x["po_line_id"])):
        pol = by_id[ln["po_line_id"]]
        rec, dmg = ln["quantity_received"], ln.get("quantity_damaged", 0)
        loc = stock.resolve_location(db, po.warehouse_id, ln.get("location_id"))
        # Atomic, conditional increment: can never exceed the ordered quantity even under concurrency.
        res = db.execute(
            update(PurchaseOrderLine)
            .where(PurchaseOrderLine.id == pol.id, PurchaseOrderLine.received_quantity + rec <= PurchaseOrderLine.quantity)
            .values(received_quantity=PurchaseOrderLine.received_quantity + rec,
                    damaged_quantity=PurchaseOrderLine.damaged_quantity + dmg)
            .execution_options(synchronize_session=False)
        )
        if res.rowcount != 1:
            db.refresh(pol)
            raise OverReceipt(
                f"Receiving {rec} exceeds the outstanding quantity for {pol.variant.sku}",
                po_line_id=pol.id, ordered=pol.quantity, already_received=pol.received_quantity,
                outstanding=pol.quantity - pol.received_quantity,
            )
        good = rec - dmg
        stock.apply_stock_change(
            db, location=loc, variant_id=pol.variant_id, movement_type="RECEIPT", d_available=good, d_damaged=dmg,
            unit_cost=pol.landed_unit_cost if good > 0 else None, reference_type="GOODS_RECEIPT",
            reference_id=receipt.id, reason=f"Receipt against {po.po_number}", actor=actor,
        )
        receipt.lines.append(GoodsReceiptLine(po_line_id=pol.id, variant_id=pol.variant_id, location_id=loc.id,
                                              quantity_received=rec, quantity_damaged=dmg,
                                              unit_cost=pol.landed_unit_cost))
    db.flush()

    db.expire(po, ["lines"])
    complete = all(l.received_quantity >= l.quantity for l in db.scalars(
        select(PurchaseOrderLine).where(PurchaseOrderLine.po_id == po.id).execution_options(populate_existing=True)))
    purchasing.transition(db, po.id, "RECEIVED" if complete else "PARTIALLY_RECEIVED", actor=actor,
                          comment=f"Goods receipt {receipt.receipt_number}")
    return receipt


def get_receipt(db: Session, receipt_id: int) -> GoodsReceipt:
    r = db.get(GoodsReceipt, receipt_id)
    if r is None:
        raise NotFound(f"Goods receipt {receipt_id} not found")
    return r


def list_receipts(db: Session, *, po_id=None, limit=100, offset=0):
    q = select(GoodsReceipt).order_by(GoodsReceipt.id.desc()).limit(limit).offset(offset)
    if po_id:
        q = q.where(GoodsReceipt.po_id == po_id)
    return db.scalars(q).all()
