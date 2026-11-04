"""HTTP API. Handlers stay thin: parse -> call service -> serialize."""
from fastapi import APIRouter, Query, Response

from app import schemas as s
from app.api.deps import DB, Actor, exact
from app.services import analytics, catalog, inventory, purchasing, receiving, transfers

router = APIRouter(prefix="/api/v1")
Limit = Query(100, ge=1, le=500)
Offset = Query(0, ge=0)


# ------------------------------------------------------------- products
@router.post("/products", response_model=s.ProductOut, status_code=201, tags=["products"])
def create_product(body: s.ProductIn, db: DB):
    return catalog.create_product(db, body.model_dump())


@router.get("/products", response_model=list[s.ProductOut], tags=["products"])
def list_products(db: DB, category: str | None = None, q: str | None = None, active: bool | None = None,
                  limit: int = Limit, offset: int = Offset):
    return catalog.list_products(db, category=category, q=q, active=active, limit=limit, offset=offset)


@router.get("/products/{product_id}", response_model=s.ProductOut, tags=["products"])
def get_product(product_id: int, db: DB):
    return catalog.get_product(db, product_id)


@router.patch("/products/{product_id}", response_model=s.ProductOut, tags=["products"])
def patch_product(product_id: int, body: s.ProductPatch, db: DB):
    return catalog.update_product(db, product_id, body.model_dump(exclude_unset=True))


@router.delete("/products/{product_id}", status_code=204, tags=["products"])
def delete_product(product_id: int, db: DB):
    catalog.delete_product(db, product_id)
    return Response(status_code=204)


@router.post("/products/{product_id}/variants", response_model=s.VariantOut, status_code=201, tags=["products"])
def add_variant(product_id: int, body: s.VariantIn, db: DB):
    return catalog.add_variant(db, product_id, body.model_dump())


@router.patch("/variants/{variant_id}", response_model=s.VariantOut, tags=["products"])
def patch_variant(variant_id: int, body: s.VariantPatch, db: DB):
    return catalog.update_variant(db, variant_id, body.model_dump(exclude_unset=True))


# ----------------------------------------------------------- warehouses
@router.post("/warehouses", response_model=s.WarehouseOut, status_code=201, tags=["warehouses"])
def create_warehouse(body: s.WarehouseIn, db: DB):
    return catalog.create_warehouse(db, body.model_dump())


@router.get("/warehouses", response_model=list[s.WarehouseOut], tags=["warehouses"])
def list_warehouses(db: DB):
    from sqlalchemy import select
    from app.models import Warehouse
    return db.scalars(select(Warehouse).order_by(Warehouse.id)).all()


@router.get("/warehouses/{warehouse_id}", response_model=s.WarehouseOut, tags=["warehouses"])
def get_warehouse(warehouse_id: int, db: DB):
    return catalog.get_warehouse(db, warehouse_id)


@router.patch("/warehouses/{warehouse_id}", response_model=s.WarehouseOut, tags=["warehouses"])
def patch_warehouse(warehouse_id: int, body: s.WarehousePatch, db: DB):
    return catalog.update_warehouse(db, warehouse_id, body.model_dump(exclude_unset=True))


@router.post("/warehouses/{warehouse_id}/locations", response_model=s.LocationOut, status_code=201, tags=["warehouses"])
def create_location(warehouse_id: int, body: s.LocationIn, db: DB):
    return catalog.create_location(db, warehouse_id, body.model_dump())


@router.patch("/locations/{location_id}", response_model=s.LocationOut, tags=["warehouses"])
def patch_location(location_id: int, body: s.LocationPatch, db: DB):
    return catalog.update_location(db, location_id, body.model_dump(exclude_unset=True))


@router.get("/warehouses/{warehouse_id}/inventory", response_model=list[s.StockLevelOut], tags=["warehouses"])
def warehouse_inventory(warehouse_id: int, db: DB, limit: int = Limit, offset: int = Offset):
    catalog.get_warehouse(db, warehouse_id)
    return inventory.stock_levels(db, warehouse_id=warehouse_id, limit=limit, offset=offset)


# ------------------------------------------------------------ suppliers
@router.post("/suppliers", response_model=s.SupplierOut, status_code=201, tags=["suppliers"])
def create_supplier(body: s.SupplierIn, db: DB):
    return catalog.create_supplier(db, body.model_dump())


@router.get("/suppliers", response_model=list[s.SupplierOut], tags=["suppliers"])
def list_suppliers(db: DB):
    from sqlalchemy import select
    from app.models import Supplier
    return db.scalars(select(Supplier).order_by(Supplier.id)).all()


@router.get("/suppliers/{supplier_id}", response_model=s.SupplierOut, tags=["suppliers"])
def get_supplier(supplier_id: int, db: DB):
    return catalog.get_supplier(db, supplier_id)


@router.patch("/suppliers/{supplier_id}", response_model=s.SupplierOut, tags=["suppliers"])
def patch_supplier(supplier_id: int, body: s.SupplierPatch, db: DB):
    return catalog.update_supplier(db, supplier_id, body.model_dump(exclude_unset=True))


@router.put("/suppliers/{supplier_id}/products", response_model=s.SupplierProductOut, tags=["suppliers"])
def link_product(supplier_id: int, body: s.SupplierProductIn, db: DB, user: Actor):
    return catalog.link_supplier_product(db, supplier_id, body.model_dump(), actor=user)


@router.get("/suppliers/{supplier_id}/products", response_model=list[s.SupplierProductOut], tags=["suppliers"])
def supplier_products(supplier_id: int, db: DB):
    from sqlalchemy import select
    from app.models import SupplierProduct
    catalog.get_supplier(db, supplier_id)
    return db.scalars(select(SupplierProduct).where(SupplierProduct.supplier_id == supplier_id)).all()


@router.delete("/suppliers/{supplier_id}/products/{variant_id}", status_code=204, tags=["suppliers"])
def unlink_product(supplier_id: int, variant_id: int, db: DB):
    catalog.unlink_supplier_product(db, supplier_id, variant_id)
    return Response(status_code=204)


@router.get("/suppliers/{supplier_id}/purchase-history", tags=["suppliers"])
def purchase_history(supplier_id: int, db: DB):
    return exact(purchasing.supplier_purchase_history(db, supplier_id))


# ------------------------------------------------------------ inventory
@router.get("/inventory", response_model=list[s.StockLevelOut], tags=["inventory"])
def stock_levels(db: DB, warehouse_id: int | None = None, variant_id: int | None = None,
                 location_id: int | None = None, limit: int = Limit, offset: int = Offset):
    return inventory.stock_levels(db, warehouse_id=warehouse_id, variant_id=variant_id, location_id=location_id,
                                  limit=limit, offset=offset)


@router.get("/inventory/variants/{variant_id}/totals", tags=["inventory"])
def variant_totals(variant_id: int, db: DB):
    from app.services import stock
    stock.get_variant(db, variant_id)
    return inventory.variant_totals(db, variant_id)


@router.post("/stock/in", response_model=s.MovementOut, status_code=201, tags=["movements"])
def stock_in(body: s.StockInIn, db: DB, user: Actor):
    return inventory.stock_in(db, actor=user, **body.model_dump())


@router.post("/stock/out", response_model=list[s.MovementOut], status_code=201, tags=["movements"])
def stock_out(body: s.StockOutIn, db: DB, user: Actor):
    return inventory.stock_out(db, actor=user, **body.model_dump())


@router.post("/stock/reserve", response_model=list[s.MovementOut], status_code=201, tags=["inventory"])
def reserve(body: s.ReserveIn, db: DB, user: Actor):
    return inventory.reserve(db, actor=user, **body.model_dump())


@router.post("/stock/release", response_model=list[s.MovementOut], status_code=201, tags=["inventory"])
def release(body: s.ReserveIn, db: DB, user: Actor):
    return inventory.release(db, actor=user, **body.model_dump())


@router.post("/stock/adjustments", response_model=list[s.MovementOut], status_code=201, tags=["inventory"])
def adjust(body: s.AdjustmentIn, db: DB, user: Actor):
    return inventory.adjust(db, actor=user, **body.model_dump())


@router.get("/stock/movements", response_model=list[s.MovementOut], tags=["movements"])
def movements(db: DB, variant_id: int | None = None, warehouse_id: int | None = None,
              movement_type: str | None = None, reference_type: str | None = None,
              reference_id: int | None = None, limit: int = Limit, offset: int = Offset):
    return inventory.movements(db, variant_id=variant_id, warehouse_id=warehouse_id, movement_type=movement_type,
                               reference_type=reference_type, reference_id=reference_id, limit=limit, offset=offset)


# ------------------------------------------------------------ transfers
@router.post("/transfers", response_model=s.TransferOut, status_code=201, tags=["transfers"])
def create_transfer(body: s.TransferIn, db: DB, user: Actor):
    return transfers.create_transfer(db, actor=user, **body.model_dump())


@router.get("/transfers", response_model=list[s.TransferOut], tags=["transfers"])
def list_transfers(db: DB, status: str | None = None, limit: int = Limit, offset: int = Offset):
    return transfers.list_transfers(db, status=status, limit=limit, offset=offset)


@router.get("/transfers/{transfer_id}", response_model=s.TransferOut, tags=["transfers"])
def get_transfer(transfer_id: int, db: DB):
    return transfers.get_transfer(db, transfer_id)


@router.post("/transfers/{transfer_id}/dispatch", response_model=s.TransferOut, tags=["transfers"])
def dispatch_transfer(transfer_id: int, db: DB, user: Actor):
    return transfers.dispatch(db, transfer_id, user)


@router.post("/transfers/{transfer_id}/receive", response_model=s.TransferOut, tags=["transfers"])
def receive_transfer(transfer_id: int, db: DB, user: Actor):
    return transfers.receive(db, transfer_id, user)


@router.post("/transfers/{transfer_id}/cancel", response_model=s.TransferOut, tags=["transfers"])
def cancel_transfer(transfer_id: int, db: DB, user: Actor):
    return transfers.cancel(db, transfer_id, user)


# ------------------------------------------------------- purchase orders
@router.post("/purchase-orders", response_model=s.POOut, status_code=201, tags=["purchase-orders"])
def create_po(body: s.POIn, db: DB, user: Actor):
    return purchasing.create_po(db, actor=user, **body.model_dump())


@router.get("/purchase-orders", response_model=list[s.POOut], tags=["purchase-orders"])
def list_pos(db: DB, status: str | None = None, supplier_id: int | None = None,
             limit: int = Limit, offset: int = Offset):
    return purchasing.list_pos(db, status=status, supplier_id=supplier_id, limit=limit, offset=offset)


@router.get("/purchase-orders/{po_id}", response_model=s.POOut, tags=["purchase-orders"])
def get_po(po_id: int, db: DB):
    return purchasing.get_po(db, po_id)


@router.patch("/purchase-orders/{po_id}", response_model=s.POOut, tags=["purchase-orders"])
def patch_po(po_id: int, body: s.POPatch, db: DB, user: Actor):
    d = body.model_dump(exclude_unset=True)
    return purchasing.update_po(db, po_id, lines=d.get("lines"), expected_date=d.get("expected_date"),
                                notes=d.get("notes"), actor=user)


@router.post("/purchase-orders/{po_id}/submit", response_model=s.POOut, tags=["purchase-orders"])
def submit_po(po_id: int, db: DB, user: Actor):
    return purchasing.submit(db, po_id, user)


@router.post("/purchase-orders/{po_id}/approve", response_model=s.POOut, tags=["purchase-orders"])
def approve_po(po_id: int, db: DB, user: Actor, body: s.CommentIn = s.CommentIn()):
    return purchasing.approve(db, po_id, user, body.comment)


@router.post("/purchase-orders/{po_id}/reject", response_model=s.POOut, tags=["purchase-orders"])
def reject_po(po_id: int, body: s.RejectIn, db: DB, user: Actor):
    return purchasing.reject(db, po_id, user, body.reason)


@router.post("/purchase-orders/{po_id}/cancel", response_model=s.POOut, tags=["purchase-orders"])
def cancel_po(po_id: int, db: DB, user: Actor, body: s.CommentIn = s.CommentIn()):
    return purchasing.cancel(db, po_id, user, body.comment)


@router.post("/purchase-orders/{po_id}/close", response_model=s.POOut, tags=["purchase-orders"])
def close_po(po_id: int, db: DB, user: Actor, body: s.CommentIn = s.CommentIn()):
    return purchasing.close(db, po_id, user, body.comment)


@router.post("/purchase-orders/{po_id}/receipts", response_model=s.ReceiptOut, status_code=201,
             tags=["goods-receipts"])
def receive_goods(po_id: int, body: s.ReceiptIn, db: DB, user: Actor):
    return receiving.receive_goods(db, po_id, [l.model_dump() for l in body.lines], actor=user, notes=body.notes)


@router.get("/goods-receipts", response_model=list[s.ReceiptOut], tags=["goods-receipts"])
def list_receipts(db: DB, po_id: int | None = None, limit: int = Limit, offset: int = Offset):
    return receiving.list_receipts(db, po_id=po_id, limit=limit, offset=offset)


@router.get("/goods-receipts/{receipt_id}", response_model=s.ReceiptOut, tags=["goods-receipts"])
def get_receipt(receipt_id: int, db: DB):
    return receiving.get_receipt(db, receipt_id)


# ------------------------------------------------------------ analytics
@router.get("/analytics/dashboard", tags=["analytics"])
def a_dashboard(db: DB):
    return exact(analytics.dashboard(db))


@router.get("/analytics/low-stock", tags=["analytics"])
def a_low_stock(db: DB):
    return exact(analytics.low_stock(db))


@router.get("/analytics/valuation", tags=["analytics"])
def a_valuation(db: DB):
    return exact(analytics.valuation(db))


@router.get("/analytics/movers", tags=["analytics"])
def a_movers(db: DB, days: int = Query(90, ge=1, le=3650), fast_min_units: int = Query(50, ge=1),
             slow_max_units: int = Query(5, ge=0)):
    return exact(analytics.movers(db, days=days, fast_min_units=fast_min_units, slow_max_units=slow_max_units))


@router.get("/analytics/warehouse-utilization", tags=["analytics"])
def a_utilization(db: DB):
    return exact(analytics.warehouse_utilization(db))


@router.get("/analytics/reorder-recommendations", tags=["analytics"])
def a_reorder(db: DB):
    return exact(analytics.reorder_recommendations(db))
