"""Purchase orders: creation with validated relationships, totals, approval workflow, history."""
import uuid
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import config
from app.errors import InvalidState, NotFound, ValidationFailed
from app.models import (
    PurchaseOrder, PurchaseOrderEvent, PurchaseOrderLine, SupplierProduct, utcnow,
)
from app.services import stock

CENT = Decimal("0.01")

# status -> allowed next statuses
TRANSITIONS = {
    "DRAFT": {"PENDING_APPROVAL", "CANCELLED"},
    "PENDING_APPROVAL": {"APPROVED", "REJECTED", "CANCELLED"},
    "APPROVED": {"PARTIALLY_RECEIVED", "RECEIVED", "CANCELLED"},
    "PARTIALLY_RECEIVED": {"PARTIALLY_RECEIVED", "RECEIVED", "CLOSED"},
    "RECEIVED": set(), "REJECTED": set(), "CANCELLED": set(), "CLOSED": set(),
}
OPEN_STATUSES = ("APPROVED", "PARTIALLY_RECEIVED")


def _c(x) -> Decimal:
    return Decimal(str(x)).quantize(CENT, rounding=ROUND_HALF_UP)


def compute_line(qty: int, unit_cost: Decimal, discount_pct: Decimal, tax_pct: Decimal) -> dict:
    """subtotal = qty*cost; discount = subtotal*d%; tax = (subtotal-discount)*t%; each rounded to cents."""
    unit_cost, d, t = Decimal(str(unit_cost)), Decimal(str(discount_pct)), Decimal(str(tax_pct))
    subtotal = _c(unit_cost * qty)
    discount = _c(subtotal * d / 100)
    net = subtotal - discount
    tax = _c(net * t / 100)
    return {
        "line_subtotal": subtotal, "line_discount": discount, "line_tax": tax, "line_total": net + tax,
        "landed_unit_cost": (net / qty).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
    }


def _build_lines(db: Session, supplier_id: int, lines: list[dict]) -> list[PurchaseOrderLine]:
    if not lines:
        raise ValidationFailed("A purchase order needs at least one line")
    seen, out = set(), []
    for ln in lines:
        vid = ln["variant_id"]
        if vid in seen:
            raise ValidationFailed(f"Variant {vid} appears on more than one line")
        seen.add(vid)
        stock.get_variant(db, vid, active=True)
        sp = db.scalars(select(SupplierProduct).where(
            SupplierProduct.supplier_id == supplier_id, SupplierProduct.variant_id == vid)).first()
        if sp is None:
            raise ValidationFailed(f"Supplier does not supply variant {vid}", variant_id=vid)
        qty = ln["quantity"]
        if qty < sp.min_order_qty:
            raise ValidationFailed(f"Quantity {qty} is below supplier minimum order {sp.min_order_qty}", variant_id=vid)
        cost = ln.get("unit_cost")
        cost = sp.unit_price if cost is None else Decimal(str(cost))
        d, t = Decimal(str(ln.get("discount_pct", 0))), Decimal(str(ln.get("tax_pct", 0)))
        if not (0 <= d <= 100 and 0 <= t <= 100 and cost >= 0):
            raise ValidationFailed("discount_pct/tax_pct must be within 0..100 and unit_cost >= 0", variant_id=vid)
        out.append(PurchaseOrderLine(variant_id=vid, quantity=qty, unit_cost=cost, discount_pct=d, tax_pct=t,
                                     **compute_line(qty, cost, d, t)))
    return out


def _apply_totals(po: PurchaseOrder) -> None:
    po.subtotal = sum((l.line_subtotal for l in po.lines), Decimal("0"))
    po.discount_total = sum((l.line_discount for l in po.lines), Decimal("0"))
    po.tax_total = sum((l.line_tax for l in po.lines), Decimal("0"))
    po.total = po.subtotal - po.discount_total + po.tax_total


def _event(db: Session, po: PurchaseOrder, frm: str | None, to: str, actor: str, comment: str = "") -> None:
    db.add(PurchaseOrderEvent(po_id=po.id, from_status=frm, to_status=to, actor=actor, comment=comment))


def get_po(db: Session, po_id: int, *, lock: bool = False) -> PurchaseOrder:
    q = select(PurchaseOrder).where(PurchaseOrder.id == po_id).execution_options(populate_existing=True)
    if lock:
        q = q.with_for_update()
    po = db.scalars(q).first()
    if po is None:
        raise NotFound(f"Purchase order {po_id} not found")
    return po


def create_po(db: Session, *, supplier_id: int, warehouse_id: int, lines: list[dict], expected_date=None,
              notes: str = "", actor: str = "system") -> PurchaseOrder:
    from app.services.catalog import get_supplier
    supplier = get_supplier(db, supplier_id)
    if not supplier.is_active:
        raise ValidationFailed("Supplier is inactive")
    stock.get_warehouse(db, warehouse_id)
    po = PurchaseOrder(po_number=f"TMP-{uuid.uuid4().hex}", supplier_id=supplier_id, warehouse_id=warehouse_id,
                       expected_date=expected_date, notes=notes, created_by=actor, status="DRAFT")
    po.lines = _build_lines(db, supplier_id, lines)
    _apply_totals(po)
    db.add(po)
    db.flush()
    po.po_number = f"PO-{po.id:06d}"
    _event(db, po, None, "DRAFT", actor)
    db.flush()
    return po


def update_po(db: Session, po_id: int, *, lines: list[dict] | None, expected_date, notes, actor: str) -> PurchaseOrder:
    po = get_po(db, po_id, lock=True)
    if po.status != "DRAFT":
        raise InvalidState(f"Only DRAFT purchase orders can be edited (status is {po.status})")
    if lines is not None:
        new_lines = _build_lines(db, po.supplier_id, lines)
        po.lines.clear()
        db.flush()  # delete old lines before inserting replacements (unique po_id+variant_id)
        po.lines.extend(new_lines)
        _apply_totals(po)
    if expected_date is not None:
        po.expected_date = expected_date
    if notes is not None:
        po.notes = notes
    db.flush()
    return po


def transition(db: Session, po_id: int, to: str, *, actor: str, comment: str = "", **extra) -> PurchaseOrder:
    """Guarded state change: UPDATE ... WHERE status = <expected> so concurrent actors cannot both win."""
    po = get_po(db, po_id)
    frm = po.status
    if to not in TRANSITIONS[frm]:
        raise InvalidState(f"Cannot move purchase order from {frm} to {to}")
    res = db.execute(update(PurchaseOrder).where(PurchaseOrder.id == po_id, PurchaseOrder.status == frm)
                     .values(status=to, **extra).execution_options(synchronize_session=False))
    if res.rowcount != 1:
        raise InvalidState("Purchase order changed concurrently; reload and retry")
    _event(db, po, frm, to, actor, comment)
    db.flush()
    return get_po(db, po_id)


def submit(db: Session, po_id: int, actor: str) -> PurchaseOrder:
    po = get_po(db, po_id)
    if not po.lines:
        raise ValidationFailed("Cannot submit an empty purchase order")
    return transition(db, po_id, "PENDING_APPROVAL", actor=actor)


def approve(db: Session, po_id: int, actor: str, comment: str = "") -> PurchaseOrder:
    po = get_po(db, po_id)
    if po.status == "PENDING_APPROVAL" and po.created_by == actor and not config.allow_self_approval():
        raise InvalidState("Segregation of duties: a purchase order cannot be approved by its creator")
    return transition(db, po_id, "APPROVED", actor=actor, comment=comment, approved_by=actor, approved_at=utcnow())


def reject(db: Session, po_id: int, actor: str, reason: str) -> PurchaseOrder:
    if not reason or not reason.strip():
        raise ValidationFailed("A rejection reason is required")
    return transition(db, po_id, "REJECTED", actor=actor, comment=reason, rejection_reason=reason)


def cancel(db: Session, po_id: int, actor: str, comment: str = "") -> PurchaseOrder:
    po = get_po(db, po_id, lock=True)
    if any(l.received_quantity for l in po.lines):
        raise InvalidState("Goods were already received against this order; close it instead of cancelling")
    return transition(db, po_id, "CANCELLED", actor=actor, comment=comment)


def close(db: Session, po_id: int, actor: str, comment: str = "") -> PurchaseOrder:
    return transition(db, po_id, "CLOSED", actor=actor, comment=comment)


def list_pos(db: Session, *, status=None, supplier_id=None, limit=100, offset=0):
    q = select(PurchaseOrder).order_by(PurchaseOrder.id.desc()).limit(limit).offset(offset)
    if status:
        q = q.where(PurchaseOrder.status == status)
    if supplier_id:
        q = q.where(PurchaseOrder.supplier_id == supplier_id)
    return db.scalars(q).all()


def supplier_purchase_history(db: Session, supplier_id: int) -> dict:
    from app.services.catalog import get_supplier
    get_supplier(db, supplier_id)
    pos = db.scalars(select(PurchaseOrder).where(PurchaseOrder.supplier_id == supplier_id)
                     .order_by(PurchaseOrder.id.desc())).all()
    committed = [p for p in pos if p.status in ("APPROVED", "PARTIALLY_RECEIVED", "RECEIVED", "CLOSED")]
    per_variant: dict[int, dict] = {}
    for p in committed:
        for l in p.lines:
            d = per_variant.setdefault(l.variant_id, {"variant_id": l.variant_id, "sku": l.variant.sku,
                                                      "quantity_ordered": 0, "quantity_received": 0,
                                                      "_cost": Decimal("0")})
            d["quantity_ordered"] += l.quantity
            d["quantity_received"] += l.received_quantity
            d["_cost"] += l.line_subtotal - l.line_discount
    products = []
    for d in per_variant.values():
        cost = d.pop("_cost")
        d["total_spend"] = cost
        d["avg_unit_cost"] = (cost / d["quantity_ordered"]).quantize(Decimal("0.0001"))
        products.append(d)
    return {
        "supplier_id": supplier_id,
        "order_count": len(pos),
        "committed_order_count": len(committed),
        "total_spend": sum((p.total for p in committed), Decimal("0")),
        "orders": [{"id": p.id, "po_number": p.po_number, "status": p.status, "total": p.total,
                    "created_at": p.created_at} for p in pos],
        "products": products,
    }
