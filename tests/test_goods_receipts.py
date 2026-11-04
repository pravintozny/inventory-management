from tests.conftest import levels


def approved_po(api, world, qty=10, **line):
    po = api.post("/purchase-orders", {"supplier_id": world["supplier"]["id"], "warehouse_id": world["w1_id"],
                                       "lines": [{"variant_id": world["variant_id"], "quantity": qty, **line}]},
                  user="buyer")
    api.post(f"/purchase-orders/{po['id']}/submit", expect=200, user="buyer")
    return api.post(f"/purchase-orders/{po['id']}/approve", expect=200, user="boss")


def receive(api, po, qty, damaged=0, expect=201, **kw):
    return api.req("POST", f"/purchase-orders/{po['id']}/receipts", expect, user="dock",
                   json={"lines": [{"po_line_id": po["lines"][0]["id"], "quantity_received": qty,
                                    "quantity_damaged": damaged, **kw}]})


def test_partial_then_complete_receipt(api, world):
    po = approved_po(api, world, 10)
    r1 = receive(api, po, 4).json()
    assert r1["receipt_number"] == f"GR-{r1['id']:06d}"
    assert api.get(f"/purchase-orders/{po['id']}")["status"] == "PARTIALLY_RECEIVED"
    assert levels(api, world["w1_id"], world["variant_id"])["available"] == 4
    receive(api, po, 6)
    final = api.get(f"/purchase-orders/{po['id']}")
    assert final["status"] == "RECEIVED" and final["lines"][0]["received_quantity"] == 10
    assert levels(api, world["w1_id"], world["variant_id"])["available"] == 10


def test_over_receipt_rejected_without_side_effects(api, world):
    po = approved_po(api, world, 10)
    receive(api, po, 8)
    r = receive(api, po, 3, expect=409)
    assert r.json()["error"]["code"] == "over_receipt" and r.json()["error"]["details"]["outstanding"] == 2
    assert levels(api, world["w1_id"], world["variant_id"])["available"] == 8
    assert api.get(f"/purchase-orders/{po['id']}")["lines"][0]["received_quantity"] == 8
    assert len(api.get(f"/goods-receipts?po_id={po['id']}")) == 1


def test_damaged_goods_tracked_separately(api, world):
    po = approved_po(api, world, 10)
    receive(api, po, 10, damaged=3)
    assert levels(api, world["w1_id"], world["variant_id"]) == {"available": 7, "reserved": 0, "damaged": 3, "in_transit": 0}
    line = api.get(f"/purchase-orders/{po['id']}")["lines"][0]
    assert (line["received_quantity"], line["damaged_quantity"]) == (10, 3)
    assert api.get(f"/purchase-orders/{po['id']}")["status"] == "RECEIVED"


def test_all_damaged_does_not_touch_valuation(api, world):
    po = approved_po(api, world, 5)
    receive(api, po, 5, damaged=5)
    v = api.get("/analytics/valuation")
    assert v["total_value"] == "0" or float(v["total_value"]) == 0


def test_receipt_validation(api, world):
    po = approved_po(api, world, 10)
    receive(api, po, 0, expect=422)
    receive(api, po, 2, damaged=3, expect=422)
    receive(api, po, 2, damaged=-1, expect=422)
    api.req("POST", f"/purchase-orders/{po['id']}/receipts", 422, json={"lines": [
        {"po_line_id": 9999, "quantity_received": 1}]})
    api.req("POST", f"/purchase-orders/{po['id']}/receipts", 422, json={"lines": [
        {"po_line_id": po["lines"][0]["id"], "quantity_received": 1},
        {"po_line_id": po["lines"][0]["id"], "quantity_received": 1}]})
    api.req("POST", f"/purchase-orders/{po['id']}/receipts", 422, json={"lines": []})
    # line from another PO
    other = approved_po(api, world, 5)
    api.req("POST", f"/purchase-orders/{po['id']}/receipts", 422, json={"lines": [
        {"po_line_id": other["lines"][0]["id"], "quantity_received": 1}]})


def test_cannot_receive_into_foreign_location(api, world):
    po = approved_po(api, world, 5)
    foreign = world["w2"]["locations"][0]["id"]
    receive(api, po, 1, location_id=foreign, expect=422)


def test_cost_blends_into_weighted_average(api, world):
    api.post("/stock/in", {"warehouse_id": world["w1_id"], "variant_id": world["variant_id"], "quantity": 10,
                           "unit_cost": "1.00"})
    po = approved_po(api, world, 10, unit_cost="3.00", discount_pct="10")  # landed 2.70
    receive(api, po, 10)
    lv = api.get(f"/inventory?warehouse_id={world['w1_id']}")[0]
    assert lv["avg_cost"] == "1.8500"


def test_receipt_capacity_failure_rolls_back_po_counts(api, world):
    tiny = api.post("/warehouses", {"code": "TNY", "name": "T", "capacity_units": 2})
    po = api.post("/purchase-orders", {"supplier_id": world["supplier"]["id"], "warehouse_id": tiny["id"],
                                       "lines": [{"variant_id": world["variant_id"], "quantity": 5}]}, user="a")
    api.post(f"/purchase-orders/{po['id']}/submit", expect=200, user="a")
    api.post(f"/purchase-orders/{po['id']}/approve", expect=200, user="b")
    receive(api, po, 5, expect=409)
    assert api.get(f"/purchase-orders/{po['id']}")["lines"][0]["received_quantity"] == 0


def test_close_partially_received_and_no_cancel_after_receipt(api, world):
    po = approved_po(api, world, 10)
    receive(api, po, 4)
    api.req("POST", f"/purchase-orders/{po['id']}/cancel", 409)
    assert api.post(f"/purchase-orders/{po['id']}/close", expect=200)["status"] == "CLOSED"
    receive(api, po, 1, expect=409)


def test_receipt_movements_reference_receipt(api, world):
    po = approved_po(api, world, 10)
    r = receive(api, po, 10, damaged=2).json()
    m = api.get(f"/stock/movements?reference_type=GOODS_RECEIPT&reference_id={r['id']}")
    assert len(m) == 1 and (m[0]["d_available"], m[0]["d_damaged"], m[0]["movement_type"]) == (8, 2, "RECEIPT")
