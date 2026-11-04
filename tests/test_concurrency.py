"""Concurrent stock updates against a real (file-backed) database with separate connections."""
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import func, select

from app.errors import DomainError
from app.models import StockLevel, StockMovement, Warehouse
from app.services import catalog, inventory, purchasing, receiving, transfers


def run_parallel(Session, fn, n):
    """Run fn(session) in n threads, each with its own session/transaction. Returns (ok, errors)."""
    def worker(i):
        s = Session()
        try:
            fn(s, i)
            s.commit()
            return None
        except DomainError as e:
            s.rollback()
            return e
        finally:
            s.close()

    with ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(worker, range(n)))
    return [r for r in results if r is None], [r for r in results if r is not None]


def setup(Session, stock=10, capacity=None):
    s = Session()
    w = catalog.create_warehouse(s, {"code": "W", "name": "W", "capacity_units": capacity})
    w2 = catalog.create_warehouse(s, {"code": "W2", "name": "W2"})
    p = catalog.create_product(s, {"sku": "P", "name": "P", "category": "c"})
    s.commit()
    if stock:
        inventory.stock_in(s, warehouse_id=w.id, variant_id=p.variants[0].id, quantity=stock)
        s.commit()
    ids = (w.id, w2.id, p.variants[0].id)
    s.close()
    return ids


def test_concurrent_issues_never_oversell(Session):
    wid, _, vid = setup(Session, stock=10)
    ok, errs = run_parallel(Session, lambda s, i: inventory.stock_out(s, warehouse_id=wid, variant_id=vid, quantity=1), 25)
    assert len(ok) == 10 and len(errs) == 15
    assert {e.code for e in errs} == {"insufficient_stock"}
    s = Session()
    lv = s.scalars(select(StockLevel)).one()
    assert lv.available == 0 and s.get(Warehouse, wid).used_units == 0
    assert s.scalar(select(func.count()).select_from(StockMovement).where(StockMovement.movement_type == "STOCK_OUT")) == 10


def test_concurrent_mixed_updates_keep_ledger_consistent(Session):
    wid, _, vid = setup(Session, stock=50)

    def op(s, i):
        if i % 3 == 0:
            inventory.stock_in(s, warehouse_id=wid, variant_id=vid, quantity=2)
        elif i % 3 == 1:
            inventory.reserve(s, warehouse_id=wid, variant_id=vid, quantity=1)
        else:
            inventory.stock_out(s, warehouse_id=wid, variant_id=vid, quantity=1)

    ok, errs = run_parallel(Session, op, 30)
    assert not errs
    s = Session()
    lv = s.scalars(select(StockLevel)).one()
    ledger = {b: s.scalar(select(func.sum(getattr(StockMovement, f"d_{b}")))) or 0
              for b in ("available", "reserved", "damaged", "in_transit")}
    assert (lv.available, lv.reserved) == (ledger["available"], ledger["reserved"]) == (50 + 20 - 10 - 10, 10)
    assert s.get(Warehouse, wid).used_units == lv.available + lv.reserved  # counters match rows
    assert lv.version == s.scalar(select(func.count()).select_from(StockMovement))  # every change versioned


def test_concurrent_first_writes_create_single_stock_row(Session):
    wid, _, vid = setup(Session, stock=0)
    ok, errs = run_parallel(Session, lambda s, i: inventory.stock_in(s, warehouse_id=wid, variant_id=vid, quantity=1), 12)
    assert not errs
    s = Session()
    rows = s.scalars(select(StockLevel)).all()
    assert len(rows) == 1 and rows[0].available == 12


def test_concurrent_stock_in_respects_capacity(Session):
    wid, _, vid = setup(Session, stock=0, capacity=7)
    ok, errs = run_parallel(Session, lambda s, i: inventory.stock_in(s, warehouse_id=wid, variant_id=vid, quantity=1), 15)
    assert len(ok) == 7 and {e.code for e in errs} == {"capacity_exceeded"}
    s = Session()
    assert s.get(Warehouse, wid).used_units == 7


def test_concurrent_dispatch_of_same_transfer_happens_once(Session):
    wid, w2, vid = setup(Session, stock=10)
    s = Session()
    t = transfers.create_transfer(s, from_warehouse_id=wid, to_warehouse_id=w2,
                                  lines=[{"variant_id": vid, "quantity": 6}])
    s.commit()
    tid = t.id
    s.close()
    ok, errs = run_parallel(Session, lambda s, i: transfers.dispatch(s, tid), 6)
    assert len(ok) == 1 and {e.code for e in errs} == {"invalid_state"}
    s = Session()
    by_wh = {l.warehouse_id: (l.available, l.in_transit) for l in s.scalars(select(StockLevel))}
    assert by_wh == {wid: (4, 0), w2: (0, 6)}


def test_concurrent_transfers_cannot_overdraw_source(Session):
    wid, w2, vid = setup(Session, stock=10)
    s = Session()
    ids = [transfers.create_transfer(s, from_warehouse_id=wid, to_warehouse_id=w2,
                                     lines=[{"variant_id": vid, "quantity": 4}]).id for _ in range(5)]
    s.commit()
    s.close()
    ok, errs = run_parallel(Session, lambda s, i: transfers.dispatch(s, ids[i]), 5)
    assert len(ok) == 2 and {e.code for e in errs} == {"insufficient_stock"}
    s = Session()
    total = sum(l.available + l.in_transit for l in s.scalars(select(StockLevel)))
    assert total == 10  # units are conserved


def test_concurrent_partial_receipts_never_exceed_ordered_quantity(Session):
    wid, _, vid = setup(Session, stock=0)
    s = Session()
    sup = catalog.create_supplier(s, {"code": "S", "name": "S"})
    catalog.link_supplier_product(s, sup.id, {"variant_id": vid, "unit_price": "1.00"})
    po = purchasing.create_po(s, supplier_id=sup.id, warehouse_id=wid, actor="a",
                              lines=[{"variant_id": vid, "quantity": 10}])
    purchasing.submit(s, po.id, "a")
    purchasing.approve(s, po.id, "b")
    s.commit()
    line_id, po_id = po.lines[0].id, po.id
    s.close()
    ok, errs = run_parallel(Session, lambda s, i: receiving.receive_goods(
        s, po_id, [{"po_line_id": line_id, "quantity_received": 3}], actor="dock"), 8)
    assert len(ok) == 3 and {e.code for e in errs} == {"over_receipt"}  # 3*3=9, a 4th would exceed 10
    s = Session()
    assert s.scalars(select(StockLevel)).one().available == 9
    assert purchasing.get_po(s, po_id).status == "PARTIALLY_RECEIVED"
    assert purchasing.get_po(s, po_id).lines[0].received_quantity == 9


def test_concurrent_receipts_on_different_lines_finish_po_exactly_once(Session):
    wid, _, vid = setup(Session, stock=0)
    s = Session()
    p2 = catalog.create_product(s, {"sku": "P2", "name": "P2", "category": "c"})
    v2 = p2.variants[0].id
    sup = catalog.create_supplier(s, {"code": "S", "name": "S"})
    for v in (vid, v2):
        catalog.link_supplier_product(s, sup.id, {"variant_id": v, "unit_price": "1.00"})
    po = purchasing.create_po(s, supplier_id=sup.id, warehouse_id=wid, actor="a",
                              lines=[{"variant_id": vid, "quantity": 5}, {"variant_id": v2, "quantity": 5}])
    purchasing.submit(s, po.id, "a")
    purchasing.approve(s, po.id, "b")
    s.commit()
    line_ids, po_id = [l.id for l in po.lines], po.id
    s.close()
    ok, errs = run_parallel(Session, lambda s, i: receiving.receive_goods(
        s, po_id, [{"po_line_id": line_ids[i], "quantity_received": 5}]), 2)
    assert len(ok) == 2 and not errs
    s = Session()
    assert purchasing.get_po(s, po_id).status == "RECEIVED"
