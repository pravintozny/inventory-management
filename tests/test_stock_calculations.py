from decimal import Decimal

import pytest

from app.errors import CapacityExceeded, InsufficientStock, ValidationFailed
from app.models import Location, StockLevel, StockMovement, Warehouse
from app.services import catalog, inventory


@pytest.fixture()
def env(db):
    w = catalog.create_warehouse(db, {"code": "W", "name": "W", "capacity_units": 100})
    p = catalog.create_product(db, {"sku": "P", "name": "P", "category": "c", "purchase_price": Decimal("3")})
    db.commit()
    return w, p.variants[0]


def level(db, w, v):
    return db.query(StockLevel).filter_by(warehouse_id=w.id, variant_id=v.id).one()


def test_buckets_after_mixed_operations(db, env):
    w, v = env
    inventory.stock_in(db, warehouse_id=w.id, variant_id=v.id, quantity=50, unit_cost=Decimal("4"))
    inventory.reserve(db, warehouse_id=w.id, variant_id=v.id, quantity=10)
    inventory.adjust(db, warehouse_id=w.id, variant_id=v.id, adjustment_type="DAMAGE", quantity=5, reason="dropped")
    inventory.stock_out(db, warehouse_id=w.id, variant_id=v.id, quantity=15)
    lv = level(db, w, v)
    assert (lv.available, lv.reserved, lv.damaged, lv.in_transit) == (20, 10, 5, 0)
    assert db.get(Warehouse, w.id).used_units == 35  # 20+10+5


def test_negative_stock_prevented(db, env):
    w, v = env
    inventory.stock_in(db, warehouse_id=w.id, variant_id=v.id, quantity=5)
    db.commit()
    with pytest.raises(InsufficientStock) as e:
        inventory.stock_out(db, warehouse_id=w.id, variant_id=v.id, quantity=6)
    assert e.value.details["available"] == 5
    db.rollback()
    with pytest.raises(InsufficientStock):
        inventory.release(db, warehouse_id=w.id, variant_id=v.id, quantity=1)  # nothing reserved
    db.rollback()
    with pytest.raises(InsufficientStock):
        inventory.reserve(db, warehouse_id=w.id, variant_id=v.id, quantity=6)
    db.rollback()
    with pytest.raises(InsufficientStock):
        inventory.adjust(db, warehouse_id=w.id, variant_id=v.id, adjustment_type="DECREASE", quantity=9, reason="x")
    assert level(db, w, v).available == 5


def test_reserved_stock_cannot_be_issued_as_available(db, env):
    w, v = env
    inventory.stock_in(db, warehouse_id=w.id, variant_id=v.id, quantity=10)
    inventory.reserve(db, warehouse_id=w.id, variant_id=v.id, quantity=8)
    db.commit()
    with pytest.raises(InsufficientStock):
        inventory.stock_out(db, warehouse_id=w.id, variant_id=v.id, quantity=3)
    db.rollback()
    inventory.stock_out(db, warehouse_id=w.id, variant_id=v.id, quantity=8, from_reserved=True)
    lv = level(db, w, v)
    assert (lv.available, lv.reserved) == (2, 0)


def test_adjustments_require_reason_and_valid_type(db, env):
    w, v = env
    with pytest.raises(ValidationFailed):
        inventory.adjust(db, warehouse_id=w.id, variant_id=v.id, adjustment_type="INCREASE", quantity=1, reason=" ")
    with pytest.raises(ValidationFailed):
        inventory.adjust(db, warehouse_id=w.id, variant_id=v.id, adjustment_type="NOPE", quantity=1, reason="r")
    with pytest.raises(ValidationFailed):
        inventory.stock_in(db, warehouse_id=w.id, variant_id=v.id, quantity=0)


def test_recount_sets_available_and_logs_difference(db, env):
    w, v = env
    inventory.stock_in(db, warehouse_id=w.id, variant_id=v.id, quantity=10)
    inventory.adjust(db, warehouse_id=w.id, variant_id=v.id, adjustment_type="RECOUNT", quantity=7, reason="cycle count")
    assert level(db, w, v).available == 7
    assert inventory.adjust(db, warehouse_id=w.id, variant_id=v.id, adjustment_type="RECOUNT", quantity=7, reason="again") == []
    inventory.adjust(db, warehouse_id=w.id, variant_id=v.id, adjustment_type="RECOUNT", quantity=0, reason="empty")
    assert level(db, w, v).available == 0


def test_write_off_damaged(db, env):
    w, v = env
    inventory.stock_in(db, warehouse_id=w.id, variant_id=v.id, quantity=10)
    inventory.adjust(db, warehouse_id=w.id, variant_id=v.id, adjustment_type="DAMAGE", quantity=4, reason="flood")
    inventory.adjust(db, warehouse_id=w.id, variant_id=v.id, adjustment_type="WRITE_OFF_DAMAGED", quantity=3, reason="scrap")
    lv = level(db, w, v)
    assert (lv.available, lv.damaged) == (6, 1)
    assert db.get(Warehouse, w.id).used_units == 7


def test_multi_location_issue_allocates_across_locations(db, env):
    w, v = env
    l2 = catalog.create_location(db, w.id, {"code": "B1"})
    default = db.query(Location).filter_by(warehouse_id=w.id, is_default=True).one()
    inventory.stock_in(db, warehouse_id=w.id, variant_id=v.id, quantity=4, location_id=default.id)
    inventory.stock_in(db, warehouse_id=w.id, variant_id=v.id, quantity=6, location_id=l2.id)
    moves = inventory.stock_out(db, warehouse_id=w.id, variant_id=v.id, quantity=7)
    assert [(m.location_id, m.d_available) for m in moves] == [(default.id, -4), (l2.id, -3)]
    with pytest.raises(InsufficientStock):
        inventory.stock_out(db, warehouse_id=w.id, variant_id=v.id, quantity=4)


def test_capacity_enforced_for_warehouse_and_location(db, env):
    w, v = env
    inventory.stock_in(db, warehouse_id=w.id, variant_id=v.id, quantity=100)
    with pytest.raises(CapacityExceeded):
        inventory.stock_in(db, warehouse_id=w.id, variant_id=v.id, quantity=1)
    db.rollback()
    loc = catalog.create_location(db, w.id, {"code": "TINY", "capacity_units": 0})
    with pytest.raises(CapacityExceeded):
        inventory.stock_in(db, warehouse_id=w.id, variant_id=v.id, quantity=1, location_id=loc.id)


def test_failed_capacity_check_leaves_no_partial_state(db, env):
    w, v = env
    inventory.stock_in(db, warehouse_id=w.id, variant_id=v.id, quantity=99)
    db.commit()
    with pytest.raises(CapacityExceeded):
        inventory.stock_in(db, warehouse_id=w.id, variant_id=v.id, quantity=2)
    db.rollback()
    assert level(db, w, v).available == 99
    assert db.query(StockMovement).count() == 1


def test_weighted_average_cost(db, env):
    w, v = env
    inventory.stock_in(db, warehouse_id=w.id, variant_id=v.id, quantity=10, unit_cost=Decimal("10"))
    inventory.stock_in(db, warehouse_id=w.id, variant_id=v.id, quantity=30, unit_cost=Decimal("20"))
    assert level(db, w, v).avg_cost == Decimal("17.5000")
    inventory.stock_out(db, warehouse_id=w.id, variant_id=v.id, quantity=20)
    assert level(db, w, v).avg_cost == Decimal("17.5000")  # outflow does not change the average
    inventory.stock_in(db, warehouse_id=w.id, variant_id=v.id, quantity=20, unit_cost=Decimal("30"))
    assert level(db, w, v).avg_cost == Decimal("23.7500")


def test_stock_in_defaults_cost_to_purchase_price(db, env):
    w, v = env
    m = inventory.stock_in(db, warehouse_id=w.id, variant_id=v.id, quantity=1)
    assert m.unit_cost == Decimal("3.0000")


def test_audit_ledger_records_before_after_and_is_immutable(db, env):
    w, v = env
    inventory.stock_in(db, warehouse_id=w.id, variant_id=v.id, quantity=10, actor="alice", reason="opening")
    inventory.stock_out(db, warehouse_id=w.id, variant_id=v.id, quantity=4, actor="bob")
    db.commit()
    rows = db.query(StockMovement).order_by(StockMovement.id).all()
    assert [(r.movement_type, r.d_available, r.available_after, r.actor) for r in rows] == [
        ("STOCK_IN", 10, 10, "alice"), ("STOCK_OUT", -4, 6, "bob")]
    rows[0].reason = "tampered"
    with pytest.raises(RuntimeError):
        db.flush()
    db.rollback()
    db.delete(db.query(StockMovement).first())
    with pytest.raises(RuntimeError):
        db.flush()


def test_db_check_constraints_are_a_backstop(db, env):
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError
    w, v = env
    inventory.stock_in(db, warehouse_id=w.id, variant_id=v.id, quantity=1)
    db.commit()
    with pytest.raises(IntegrityError):
        db.execute(text("UPDATE stock_levels SET available = -1"))
    db.rollback()
