from tests.conftest import levels


def test_product_gets_default_variant_with_product_sku(api):
    p = api.post("/products", {"sku": "A-1", "name": "A", "category": "c", "purchase_price": "1.5"})
    assert len(p["variants"]) == 1 and p["variants"][0]["sku"] == "A-1" and p["variants"][0]["is_default"]


def test_product_with_variants_and_attributes(api):
    p = api.post("/products", {"sku": "TEE", "name": "T-shirt", "category": "apparel", "uom": "each", "variants": [
        {"sku": "TEE-S-RED", "name": "S Red", "attributes": {"size": "S", "color": "red"}},
        {"sku": "TEE-M-RED", "name": "M Red", "attributes": {"size": "M", "color": "red"}, "selling_price": "12"}]})
    assert [v["attributes"]["size"] for v in p["variants"]] == ["S", "M"]
    v = api.post(f"/products/{p['id']}/variants", {"sku": "TEE-L", "name": "L", "attributes": {"size": "L"}})
    assert v["product_id"] == p["id"]


def test_duplicate_sku_rejected(api):
    body = {"sku": "DUP", "name": "x", "category": "c"}
    api.post("/products", body)
    r = api.req("POST", "/products", 409, json=body)
    assert r.json()["error"]["code"] == "conflict"


def test_validation_errors(api):
    api.req("POST", "/products", 422, json={"sku": "bad sku!", "name": "x", "category": "c"})
    api.req("POST", "/products", 422, json={"sku": "N", "name": "x", "category": "c", "purchase_price": "-1"})
    api.req("POST", "/products", 422, json={"sku": "N", "name": "x", "category": "c", "bogus": 1})
    api.req("POST", "/products", 422, json={"sku": "V", "name": "x", "category": "c", "variants": [
        {"sku": "V1", "name": "a"}, {"sku": "V1", "name": "b"}]})


def test_product_search_and_filters(api):
    api.post("/products", {"sku": "S1", "name": "Steel bolt", "category": "hardware"})
    api.post("/products", {"sku": "S2", "name": "Wooden plank", "category": "timber"})
    assert [p["sku"] for p in api.get("/products?q=bolt")] == ["S1"]
    assert [p["sku"] for p in api.get("/products?category=timber")] == ["S2"]


def test_cannot_delete_product_with_stock_history(api, world):
    api.post("/stock/in", {"warehouse_id": world["w1_id"], "variant_id": world["variant_id"], "quantity": 1})
    api.req("DELETE", f"/products/{world['product']['id']}", 409)
    other = api.post("/products", {"sku": "TMP", "name": "t", "category": "c"})
    api.req("DELETE", f"/products/{other['id']}", 204)


def test_warehouse_has_default_location_and_capacity_rules(api):
    w = api.post("/warehouses", {"code": "X", "name": "X", "capacity_units": 100})
    assert [l["is_default"] for l in w["locations"]] == [True]
    api.post(f"/warehouses/{w['id']}/locations", {"code": "A1", "capacity_units": 60})
    api.req("POST", f"/warehouses/{w['id']}/locations", 409, json={"code": "A2", "capacity_units": 60})
    api.req("POST", f"/warehouses/{w['id']}/locations", 409, json={"code": "A1"})  # duplicate code


def test_cannot_shrink_capacity_below_usage(api, world):
    api.post("/stock/in", {"warehouse_id": world["w1_id"], "variant_id": world["variant_id"], "quantity": 50})
    api.req("PATCH", f"/warehouses/{world['w1_id']}", 409, json={"capacity_units": 10})
    api.req("PATCH", f"/warehouses/{world['w1_id']}", 200, json={"capacity_units": 50})


def test_location_must_belong_to_warehouse(api, world):
    loc = world["w2"]["locations"][0]["id"]
    api.req("POST", "/stock/in", 422, json={"warehouse_id": world["w1_id"], "variant_id": world["variant_id"],
                                            "quantity": 1, "location_id": loc})


def test_supplier_validation_and_price_history(api, world, db):
    api.req("POST", "/suppliers", 422, json={"code": "BAD", "name": "n", "email": "not-an-email"})
    sid = world["supplier"]["id"]
    api.req("PUT", f"/suppliers/{sid}/products", 200,
            json={"variant_id": world["variant_id"], "unit_price": "2.75", "min_order_qty": 5})
    from sqlalchemy import select
    from app.models import SupplierPriceHistory
    prices = [str(r.unit_price) for r in db.scalars(select(SupplierPriceHistory).order_by(SupplierPriceHistory.id))]
    assert prices == ["2.5000", "2.7500"]


def test_single_preferred_supplier_per_variant(api, world):
    s2 = api.post("/suppliers", {"code": "B", "name": "B"})
    api.req("PUT", f"/suppliers/{s2['id']}/products", 200,
            json={"variant_id": world["variant_id"], "unit_price": "2.0", "is_preferred": True})
    first = api.get(f"/suppliers/{world['supplier']['id']}/products")
    assert first[0]["is_preferred"] is False


def test_supplier_link_requires_existing_variant(api, world):
    api.req("PUT", f"/suppliers/{world['supplier']['id']}/products", 404,
            json={"variant_id": 9999, "unit_price": "1"})
