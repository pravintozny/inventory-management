# Inventory & Warehouse Management System

A FastAPI + SQLAlchemy 2 + Alembic service covering products, warehouses, inventory, suppliers,
purchase orders, goods receipts, stock movements and analytics, with a small dashboard at `/`.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the design, data model, and consistency/concurrency approach.

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
alembic upgrade head                 # creates ./inventory.db (SQLite) by default
uvicorn app.main:app --reload        # API docs: http://localhost:8000/docs  dashboard: http://localhost:8000/
pytest                               # full test suite
```

Configuration (environment variables):

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./inventory.db` | SQLAlchemy URL. Use PostgreSQL (`postgresql+psycopg://...`, install the driver) in production. |
| `ALLOW_SELF_APPROVAL` | `false` | Allow a PO's creator to approve it (segregation of duties is enforced by default). |

Callers identify themselves with an `X-User` header (used for audit fields and approval rules).
Authentication/authorization is intentionally out of scope: run behind a gateway that sets it.

## API overview (`/api/v1`)

| Area | Endpoints |
|---|---|
| Products | `POST/GET /products`, `GET/PATCH/DELETE /products/{id}`, `POST /products/{id}/variants`, `PATCH /variants/{id}` |
| Warehouses | `POST/GET /warehouses`, `GET/PATCH /warehouses/{id}`, `POST /warehouses/{id}/locations`, `PATCH /locations/{id}`, `GET /warehouses/{id}/inventory` |
| Suppliers | `POST/GET /suppliers`, `GET/PATCH /suppliers/{id}`, `PUT/GET /suppliers/{id}/products`, `DELETE /suppliers/{id}/products/{variant_id}`, `GET /suppliers/{id}/purchase-history` |
| Inventory | `GET /inventory`, `POST /stock/in`, `/stock/out`, `/stock/reserve`, `/stock/release`, `/stock/adjustments` |
| Movements | `GET /stock/movements` (filter by variant, warehouse, type, reference) |
| Transfers | `POST/GET /transfers`, `POST /transfers/{id}/dispatch\|receive\|cancel` |
| Purchase orders | `POST/GET /purchase-orders`, `PATCH` (draft), `POST .../submit\|approve\|reject\|cancel\|close` |
| Goods receipts | `POST /purchase-orders/{id}/receipts`, `GET /goods-receipts` |
| Analytics | `/analytics/dashboard\|low-stock\|valuation\|movers\|warehouse-utilization\|reorder-recommendations` |

Money is serialized as 4-decimal strings (never floats). Errors share one shape:
`{"error": {"code": "insufficient_stock", "message": "...", "details": {...}}}`.
