from decimal import Decimal

import pytest

from app.services.purchasing import compute_line


def po_body(world, qty=10, **line):
    return {"supplier_id": world["supplier"]["id"], "warehouse_id": world["w1_id"],
            "lines": [{"variant_id": world["variant_id"], "quantity": qty, **line}]}


def test_line_math_discount_then_tax():
    r = compute_line(10, Decimal("2.50"), Decimal("10"), Decimal("20"))
    assert r["line_subtotal"] == Decimal("25.00") and r["line_discount"] == Decimal("2.50")
    assert r["line_tax"] == Decimal("4.50") and r["line_total"] == Decimal("27.00")
    assert r["landed_unit_cost"] == Decimal("2.2500")


def test_line_math_rounding_half_up():
    r = compute_line(3, Decimal("0.335"), Decimal("0"), Decimal("10"))
    assert r["line_subtotal"] == Decimal("1.01") and r["line_tax"] == Decimal("0.10")


def test_create_po_totals_default_price_and_numbering(api, world):
    p2 = api.post("/products", {"sku": "W-2", "name": "Gadget", "category": "parts"})
    v2 = p2["variants"][0]["id"]
    api.req("PUT", f"/suppliers/{world['supplier']['id']}/products", 200, json={"variant_id": v2, "unit_price": "10"})
    body = po_body(world, 10, discount_pct="10", tax_pct="20")  # unit cost defaults to supplier price 2.50
    body["lines"].append({"variant_id": v2, "quantity": 2, "unit_cost": "11.00"})
    po = api.post("/purchase-orders", body)
    assert po["po_number"] == f"PO-{po['id']:06d}" and po["status"] == "DRAFT"
    assert (po["subtotal"], po["discount_total"], po["tax_total"], po["total"]) == ("47.0000", "2.5000", "4.5000", "49.0000")
    assert po["events"][0]["to_status"] == "DRAFT"


def test_relationship_validation(api, world):
    other = api.post("/products", {"sku": "NOPE", "name": "Unsupplied", "category": "x"})
    r = api.req("POST", "/purchase-orders", 422, json={**po_body(world), "lines": [
        {"variant_id": other["variants"][0]["id"], "quantity": 1}]})
    assert "does not supply" in r.json()["error"]["message"]
    api.req("POST", "/purchase-orders", 404, json={**po_body(world), "supplier_id": 999})
    api.req("POST", "/purchase-orders", 404, json={**po_body(world), "warehouse_id": 999})
    api.req("POST", "/purchase-orders", 404, json={**po_body(world), "lines": [{"variant_id": 999, "quantity": 1}]})


def test_line_validation(api, world):
    api.req("POST", "/purchase-orders", 422, json=po_body(world, 3))                       # below MOQ 5
    api.req("POST", "/purchase-orders", 422, json=po_body(world, 0))
    api.req("POST", "/purchase-orders", 422, json=po_body(world, 5, discount_pct="101"))
    api.req("POST", "/purchase-orders", 422, json=po_body(world, 5, tax_pct="-1"))
    api.req("POST", "/purchase-orders", 422, json=po_body(world, 5, unit_cost="-1"))
    api.req("POST", "/purchase-orders", 422, json={**po_body(world), "lines": []})
    dup = po_body(world, 5)
    dup["lines"] *= 2
    api.req("POST", "/purchase-orders", 422, json=dup)


def test_inactive_supplier_rejected(api, world):
    api.req("PATCH", f"/suppliers/{world['supplier']['id']}", 200, json={"is_active": False})
    api.req("POST", "/purchase-orders", 422, json=po_body(world))


def test_approval_workflow_happy_path(api, world):
    po = api.post("/purchase-orders", po_body(world), user="alice")
    api.post(f"/purchase-orders/{po['id']}/submit", expect=200, user="alice")
    done = api.post(f"/purchase-orders/{po['id']}/approve", {"comment": "ok"}, expect=200, user="bob")
    assert done["status"] == "APPROVED" and done["approved_by"] == "bob" and done["approved_at"]
    assert [(e["from_status"], e["to_status"], e["actor"]) for e in done["events"]] == [
        (None, "DRAFT", "alice"), ("DRAFT", "PENDING_APPROVAL", "alice"), ("PENDING_APPROVAL", "APPROVED", "bob")]


def test_creator_cannot_approve_own_po(api, world):
    po = api.post("/purchase-orders", po_body(world), user="alice")
    api.post(f"/purchase-orders/{po['id']}/submit", expect=200, user="alice")
    api.req("POST", f"/purchase-orders/{po['id']}/approve", 409, user="alice")


def test_self_approval_can_be_enabled(api, world, monkeypatch):
    monkeypatch.setenv("ALLOW_SELF_APPROVAL", "true")
    po = api.post("/purchase-orders", po_body(world), user="alice")
    api.post(f"/purchase-orders/{po['id']}/submit", expect=200, user="alice")
    api.req("POST", f"/purchase-orders/{po['id']}/approve", 200, user="alice")


def test_invalid_transitions(api, world):
    po = api.post("/purchase-orders", po_body(world), user="a")
    api.req("POST", f"/purchase-orders/{po['id']}/approve", 409, user="b")   # not submitted
    api.req("POST", f"/purchase-orders/{po['id']}/receipts", 409, user="b",
            json={"lines": [{"po_line_id": po["lines"][0]["id"], "quantity_received": 1}]})
    api.post(f"/purchase-orders/{po['id']}/submit", expect=200, user="a")
    api.req("POST", f"/purchase-orders/{po['id']}/submit", 409, user="a")
    api.req("PATCH", f"/purchase-orders/{po['id']}", 409, json={"notes": "late edit"})


def test_reject_requires_reason_and_is_terminal(api, world):
    po = api.post("/purchase-orders", po_body(world), user="a")
    api.post(f"/purchase-orders/{po['id']}/submit", expect=200, user="a")
    api.req("POST", f"/purchase-orders/{po['id']}/reject", 422, user="b", json={"reason": ""})
    r = api.post(f"/purchase-orders/{po['id']}/reject", {"reason": "too pricey"}, expect=200, user="b")
    assert r["status"] == "REJECTED" and r["rejection_reason"] == "too pricey"
    api.req("POST", f"/purchase-orders/{po['id']}/approve", 409, user="c")


def test_edit_draft_recomputes_totals(api, world):
    po = api.post("/purchase-orders", po_body(world, 10))
    new = api.req("PATCH", f"/purchase-orders/{po['id']}", 200,
                  json={"lines": [{"variant_id": world["variant_id"], "quantity": 20}]}).json()
    assert new["total"] == "50.0000" and len(new["lines"]) == 1


def test_cancel_rules(api, world):
    po = api.post("/purchase-orders", po_body(world), user="a")
    assert api.post(f"/purchase-orders/{po['id']}/cancel", expect=200)["status"] == "CANCELLED"
    api.req("POST", f"/purchase-orders/{po['id']}/submit", 409, user="a")


def test_purchase_history_and_supplier_spend(api, world):
    sid = world["supplier"]["id"]
    po = api.post("/purchase-orders", po_body(world, 10, discount_pct="10"), user="a")
    api.post(f"/purchase-orders/{po['id']}/submit", expect=200, user="a")
    api.post(f"/purchase-orders/{po['id']}/approve", expect=200, user="b")
    api.post(f"/purchase-orders/{po['id']}/receipts", {"lines": [{"po_line_id": po["lines"][0]["id"],
                                                                 "quantity_received": 4}]}, user="b")
    api.post("/purchase-orders", po_body(world, 5), user="a")  # draft: excluded from spend
    h = api.get(f"/suppliers/{sid}/purchase-history")
    assert h["order_count"] == 2 and h["committed_order_count"] == 1 and h["total_spend"] == "22.5000"
    assert h["products"][0]["quantity_received"] == 4 and h["products"][0]["avg_unit_cost"] == "2.2500"
