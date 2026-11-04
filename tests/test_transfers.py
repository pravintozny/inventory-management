from tests.conftest import levels


def seed(api, world, qty=20, cost="4.00"):
    api.post("/stock/in", {"warehouse_id": world["w1_id"], "variant_id": world["variant_id"], "quantity": qty,
                           "unit_cost": cost})


def make_transfer(api, world, qty):
    return api.post("/transfers", {"from_warehouse_id": world["w1_id"], "to_warehouse_id": world["w2_id"],
                                   "lines": [{"variant_id": world["variant_id"], "quantity": qty}]})


def test_full_transfer_lifecycle_conserves_units(api, world):
    seed(api, world)
    t = make_transfer(api, world, 8)
    assert t["status"] == "PENDING" and levels(api, world["w1_id"], world["variant_id"])["available"] == 20
    api.post(f"/transfers/{t['id']}/dispatch", expect=200)
    assert levels(api, world["w1_id"], world["variant_id"])["available"] == 12
    assert levels(api, world["w2_id"], world["variant_id"]) == {"available": 0, "reserved": 0, "damaged": 0, "in_transit": 8}
    api.post(f"/transfers/{t['id']}/receive", expect=200)
    assert levels(api, world["w2_id"], world["variant_id"]) == {"available": 8, "reserved": 0, "damaged": 0, "in_transit": 0}
    wh = {w["id"]: w["used_units"] for w in api.get("/warehouses")}
    assert wh == {world["w1_id"]: 12, world["w2_id"]: 8}


def test_transfer_carries_cost_and_blends_at_destination(api, world):
    seed(api, world, 20, "4.00")
    api.post("/stock/in", {"warehouse_id": world["w2_id"], "variant_id": world["variant_id"], "quantity": 10,
                           "unit_cost": "10.00"})
    t = make_transfer(api, world, 10)
    api.post(f"/transfers/{t['id']}/dispatch", expect=200)
    api.post(f"/transfers/{t['id']}/receive", expect=200)
    dest = api.get(f"/inventory?warehouse_id={world['w2_id']}")[0]
    assert dest["avg_cost"] == "7.0000"  # (10*10 + 10*4) / 20


def test_cannot_dispatch_more_than_available_and_nothing_changes(api, world):
    seed(api, world, 5)
    t = make_transfer(api, world, 6)
    r = api.req("POST", f"/transfers/{t['id']}/dispatch", 409)
    assert r.json()["error"]["code"] == "insufficient_stock"
    assert api.get(f"/transfers/{t['id']}")["status"] == "PENDING"
    assert levels(api, world["w1_id"], world["variant_id"])["available"] == 5
    assert levels(api, world["w2_id"], world["variant_id"])["in_transit"] == 0


def test_multi_line_transfer_is_atomic(api, world):
    seed(api, world, 10)
    p2 = api.post("/products", {"sku": "W-2", "name": "Other", "category": "parts"})
    v2 = p2["variants"][0]["id"]
    api.post("/stock/in", {"warehouse_id": world["w1_id"], "variant_id": v2, "quantity": 1})
    t = api.post("/transfers", {"from_warehouse_id": world["w1_id"], "to_warehouse_id": world["w2_id"],
                                "lines": [{"variant_id": world["variant_id"], "quantity": 5},
                                          {"variant_id": v2, "quantity": 2}]})
    api.req("POST", f"/transfers/{t['id']}/dispatch", 409)
    assert levels(api, world["w1_id"], world["variant_id"])["available"] == 10  # first line rolled back
    assert api.get("/stock/movements?reference_type=TRANSFER") == []


def test_state_machine_guards(api, world):
    seed(api, world)
    t = make_transfer(api, world, 1)
    api.req("POST", f"/transfers/{t['id']}/receive", 409)       # not dispatched yet
    api.post(f"/transfers/{t['id']}/dispatch", expect=200)
    api.req("POST", f"/transfers/{t['id']}/dispatch", 409)      # double dispatch
    api.post(f"/transfers/{t['id']}/receive", expect=200)
    api.req("POST", f"/transfers/{t['id']}/cancel", 409)        # already received


def test_cancel_in_transit_returns_stock_to_source(api, world):
    seed(api, world, 10, "3.00")
    t = make_transfer(api, world, 4)
    api.post(f"/transfers/{t['id']}/dispatch", expect=200)
    api.post(f"/transfers/{t['id']}/cancel", expect=200)
    assert levels(api, world["w1_id"], world["variant_id"])["available"] == 10
    assert levels(api, world["w2_id"], world["variant_id"])["in_transit"] == 0
    assert {w["id"]: w["used_units"] for w in api.get("/warehouses")} == {world["w1_id"]: 10, world["w2_id"]: 0}


def test_cancel_pending_moves_nothing(api, world):
    seed(api, world)
    t = make_transfer(api, world, 4)
    assert api.post(f"/transfers/{t['id']}/cancel", expect=200)["status"] == "CANCELLED"
    assert levels(api, world["w1_id"], world["variant_id"])["available"] == 20


def test_transfer_validation(api, world):
    v = world["variant_id"]
    api.req("POST", "/transfers", 422, json={"from_warehouse_id": world["w1_id"], "to_warehouse_id": world["w1_id"],
                                             "lines": [{"variant_id": v, "quantity": 1}]})
    api.req("POST", "/transfers", 404, json={"from_warehouse_id": 99, "to_warehouse_id": world["w1_id"],
                                             "lines": [{"variant_id": v, "quantity": 1}]})
    api.req("POST", "/transfers", 422, json={"from_warehouse_id": world["w1_id"], "to_warehouse_id": world["w2_id"],
                                             "lines": [{"variant_id": v, "quantity": 0}]})
    api.req("POST", "/transfers", 422, json={"from_warehouse_id": world["w1_id"], "to_warehouse_id": world["w2_id"],
                                             "lines": []})


def test_transfer_respects_destination_capacity(api, world):
    small = api.post("/warehouses", {"code": "S", "name": "Small", "capacity_units": 3})
    seed(api, world, 10)
    t = api.post("/transfers", {"from_warehouse_id": world["w1_id"], "to_warehouse_id": small["id"],
                                "lines": [{"variant_id": world["variant_id"], "quantity": 5}]})
    r = api.req("POST", f"/transfers/{t['id']}/dispatch", 409)
    assert r.json()["error"]["code"] == "capacity_exceeded"
    assert levels(api, world["w1_id"], world["variant_id"])["available"] == 10


def test_transfer_audit_trail(api, world):
    seed(api, world)
    t = make_transfer(api, world, 3)
    api.post(f"/transfers/{t['id']}/dispatch", expect=200)
    api.post(f"/transfers/{t['id']}/receive", expect=200)
    types = [m["movement_type"] for m in reversed(api.get(f"/stock/movements?reference_type=TRANSFER&reference_id={t['id']}"))]
    assert types == ["TRANSFER_OUT", "TRANSFER_IN_TRANSIT", "TRANSFER_IN"]
