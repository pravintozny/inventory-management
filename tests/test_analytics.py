from tests.conftest import levels


def add_variant_product(api, sku, rp=0, rq=0, cost="1.00", category="parts"):
    p = api.post("/products", {"sku": sku, "name": sku, "category": category, "purchase_price": cost,
                               "reorder_point": rp, "reorder_quantity": rq})
    return p["variants"][0]["id"]


def test_valuation_weighted_average_across_warehouses_and_categories(api, world):
    v = world["variant_id"]
    api.post("/stock/in", {"warehouse_id": world["w1_id"], "variant_id": v, "quantity": 10, "unit_cost": "2.00"})
    api.post("/stock/in", {"warehouse_id": world["w1_id"], "variant_id": v, "quantity": 10, "unit_cost": "4.00"})
    v2 = add_variant_product(api, "B-1", category="tools")
    api.post("/stock/in", {"warehouse_id": world["w2_id"], "variant_id": v2, "quantity": 5, "unit_cost": "10.00"})
    api.post("/stock/reserve", {"warehouse_id": world["w1_id"], "variant_id": v, "quantity": 5})  # still valued
    api.post("/stock/adjustments", {"warehouse_id": world["w1_id"], "variant_id": v, "adjustment_type": "DAMAGE",
                                    "quantity": 3, "reason": "x"})  # excluded from value
    val = api.get("/analytics/valuation")
    assert val["total_value"] == "101.0000"  # 17 units * 3.00 + 5 * 10.00
    by_wh = {r["warehouse"]: r for r in val["by_warehouse"]}
    assert by_wh["WH1"]["value"] == "51.0000" and by_wh["WH1"]["damaged_units"] == 3
    assert {r["category"]: r["value"] for r in val["by_category"]} == {"parts": "51.0000", "tools": "50.0000"}


def test_valuation_excludes_in_transit_until_received(api, world):
    v = world["variant_id"]
    api.post("/stock/in", {"warehouse_id": world["w1_id"], "variant_id": v, "quantity": 10, "unit_cost": "2.00"})
    t = api.post("/transfers", {"from_warehouse_id": world["w1_id"], "to_warehouse_id": world["w2_id"],
                                "lines": [{"variant_id": v, "quantity": 10}]})
    api.post(f"/transfers/{t['id']}/dispatch", expect=200)
    mid = api.get("/analytics/valuation")
    assert mid["total_value"] == "0.0000"
    assert [r["in_transit_units"] for r in mid["by_warehouse"] if r["warehouse"] == "WH2"] == [10]
    api.post(f"/transfers/{t['id']}/receive", expect=200)
    assert api.get("/analytics/valuation")["total_value"] == "20.0000"


def test_low_stock_includes_never_stocked_and_uses_available(api, world):
    v = world["variant_id"]  # reorder point 10
    assert [r["sku"] for r in api.get("/analytics/low-stock")] == ["W-1"]  # zero stock rows at all
    api.post("/stock/in", {"warehouse_id": world["w1_id"], "variant_id": v, "quantity": 15})
    assert api.get("/analytics/low-stock") == []
    api.post("/stock/reserve", {"warehouse_id": world["w1_id"], "variant_id": v, "quantity": 6})
    row = api.get("/analytics/low-stock")[0]
    assert (row["available"], row["reorder_point"], row["shortfall"]) == (9, 10, 1)


def test_variant_with_no_reorder_point_is_never_low(api, world):
    add_variant_product(api, "NORP", rp=0)
    assert "NORP" not in [r["sku"] for r in api.get("/analytics/low-stock")]


def test_fast_and_slow_movers(api, world):
    w = world["w1_id"]
    fast, slow, dead = world["variant_id"], add_variant_product(api, "SLOW"), add_variant_product(api, "DEAD")
    for v in (fast, slow, dead):
        api.post("/stock/in", {"warehouse_id": w, "variant_id": v, "quantity": 100})
    api.post("/stock/out", {"warehouse_id": w, "variant_id": fast, "quantity": 60})
    api.post("/stock/out", {"warehouse_id": w, "variant_id": slow, "quantity": 2})
    m = api.get("/analytics/movers?days=30&fast_min_units=50&slow_max_units=5")
    assert [r["sku"] for r in m["fast_movers"]] == ["W-1"]
    assert m["fast_movers"][0]["units_per_day"] == 2.0 and m["fast_movers"][0]["days_of_cover"] == 20.0
    assert [r["sku"] for r in m["slow_movers"]] == ["DEAD", "SLOW"]
    api.req("GET", "/analytics/movers?days=0", 422)


def test_warehouse_utilization(api, world):
    api.post("/stock/in", {"warehouse_id": world["w1_id"], "variant_id": world["variant_id"], "quantity": 250})
    api.post("/warehouses", {"code": "INF", "name": "Unbounded"})
    rows = {r["code"]: r for r in api.get("/analytics/warehouse-utilization")}
    assert rows["WH1"]["utilization_pct"] == 25.0 and rows["WH1"]["free_units"] == 750
    assert rows["WH2"]["utilization_pct"] == 0.0
    assert rows["INF"]["utilization_pct"] is None and rows["INF"]["free_units"] is None


def test_reorder_recommendation_accounts_for_open_pos_and_moq(api, world):
    v = world["variant_id"]  # rp=10, rq=40, supplier MOQ 5 @ 2.50
    api.post("/stock/in", {"warehouse_id": world["w1_id"], "variant_id": v, "quantity": 4})
    rec = api.get("/analytics/reorder-recommendations")[0]
    # order up to rp+rq = 50; position 4 -> 46 -> rounded up to MOQ 5 -> 50
    assert rec["recommended_quantity"] == 50
    assert rec["estimated_cost"] == "125.0000" and rec["supplier_id"] == world["supplier"]["id"]
    # An approved PO for 50 covers the gap -> no recommendation.
    po = api.post("/purchase-orders", {"supplier_id": world["supplier"]["id"], "warehouse_id": world["w1_id"],
                                       "lines": [{"variant_id": v, "quantity": 50}]}, user="a")
    api.post(f"/purchase-orders/{po['id']}/submit", expect=200, user="a")
    api.post(f"/purchase-orders/{po['id']}/approve", expect=200, user="b")
    assert api.get("/analytics/reorder-recommendations") == []


def test_reorder_counts_in_transit_stock(api, world):
    v = world["variant_id"]
    api.post("/stock/in", {"warehouse_id": world["w1_id"], "variant_id": v, "quantity": 30})
    t = api.post("/transfers", {"from_warehouse_id": world["w1_id"], "to_warehouse_id": world["w2_id"],
                                "lines": [{"variant_id": v, "quantity": 25}]})
    api.post(f"/transfers/{t['id']}/dispatch", expect=200)
    # available total = 5 (<= 10) but 25 are on the way: position 30 > 10 -> no reorder
    assert api.get("/analytics/reorder-recommendations") == []
    assert [r["sku"] for r in api.get("/analytics/low-stock")] == ["W-1"]


def test_dashboard_and_ui_served(api, client, world):
    d = api.get("/analytics/dashboard")
    assert d["warehouse_count"] == 2 and d["variant_count"] == 1
    assert "Inventory" in client.get("/").text
