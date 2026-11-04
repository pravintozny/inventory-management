# Architecture

## Stack
Python 3.10+, FastAPI (HTTP), SQLAlchemy 2.0 (ORM/Core), Alembic (migrations), pytest.
SQLite for development/tests; PostgreSQL is the production target (same code, stronger locking).
The repository was empty when this was written, so the stack was chosen here.

## Layering
```
app/api/routes.py     thin HTTP handlers (parse -> service -> serialize); one DB transaction per request
app/schemas.py        Pydantic request/response models (strict: unknown fields rejected)
app/services/         business rules; no HTTP knowledge
   stock.py           THE choke point for every quantity change (see Consistency)
   inventory.py       stock in/out, reserve/release, adjustments, queries
   transfers.py       inter-warehouse transfer state machine
   purchasing.py      PO creation/totals/approval workflow/supplier history
   receiving.py       goods receipts (partial/complete/damaged)
   catalog.py         products, variants, warehouses, locations, suppliers
   analytics.py       read-only reports
app/models.py         data model, constraints, immutable audit ledger
alembic/              migrations (0001 = full initial schema)
```
`get_db` commits at the end of a successful request and rolls back on any exception, so every
operation (including a multi-line transfer or receipt) is atomic.

## Data model (summary)
* **Product / ProductVariant** – a product always has >= 1 variant. If none are supplied, a default
  variant with the product SKU is created. Variants carry `attributes` (JSON, e.g. size/colour),
  optional price/reorder overrides that fall back to the product. **All inventory references variants.**
* **Warehouse / Location** – each warehouse has a default location plus any number of named locations.
  Optional `capacity_units` on both; `used_units` counters are maintained transactionally.
* **StockLevel** – one row per (location, variant) with buckets `available`, `reserved`, `damaged`,
  `in_transit`, a moving weighted `avg_cost`, and a `version`. `CHECK` constraints forbid negatives.
* **StockMovement** – append-only ledger: bucket deltas, balances after, unit cost, actor, reason,
  and a polymorphic reference (`GOODS_RECEIPT`, `TRANSFER`, ...). ORM `before_update/before_delete`
  hooks refuse mutation.
* **Supplier / SupplierProduct / SupplierPriceHistory** – supplier<->variant relationship with price,
  lead time, MOQ, preferred flag (one preferred supplier per variant); every price change is versioned.
* **PurchaseOrder / Line / Event** – lines hold qty, unit cost, discount %, tax %, computed amounts and
  received/damaged counters; events are the approval audit trail.
* **GoodsReceipt / Line** – immutable record of each delivery.
* **StockTransfer / Line** – PENDING -> IN_TRANSIT -> RECEIVED, or CANCELLED.

### Bucket semantics
| Bucket | Meaning | Valued? | Counts toward capacity? |
|---|---|---|---|
| available | sellable on-hand | yes | yes |
| reserved | on-hand, promised to an order | yes | yes |
| damaged | on-hand, unusable | no | yes |
| in_transit | inbound to this location (transfer dispatched, not yet received) | no | yes (space is held) |

## Business rules
**Purchase order totals** (per line, each rounded half-up to cents):
`subtotal = qty x unit_cost`, `discount = subtotal x discount%`, `tax = (subtotal - discount) x tax%`,
`line_total = subtotal - discount + tax`. PO totals are sums of lines. Inventory is costed at the
*landed unit cost* `(subtotal - discount) / qty` (tax is treated as recoverable and excluded).

**PO workflow:** `DRAFT -> PENDING_APPROVAL -> APPROVED -> PARTIALLY_RECEIVED -> RECEIVED`, plus
`REJECTED`, `CANCELLED` (only before any receipt) and `CLOSED` (short-closing a partially received PO).
Only drafts are editable. The approver must differ from the creator (`ALLOW_SELF_APPROVAL` overrides).
Lines must reference variants the supplier actually supplies and respect the supplier's MOQ; duplicate
variants on one PO are rejected; inactive suppliers/warehouses/variants are rejected.

**Goods receipts:** `quantity_received` is everything physically delivered and counts toward the ordered
quantity; `quantity_damaged` (<= received) goes to the `damaged` bucket, the rest to `available`.
Over-receipt is refused (409 `over_receipt` with the outstanding quantity). PO status becomes
`PARTIALLY_RECEIVED` or `RECEIVED` automatically.

**Transfers:** dispatch moves source `available` out and destination `in_transit` in, in one transaction,
carrying the source's average cost; receive moves `in_transit -> available` and blends that cost into the
destination's average; cancelling an in-transit transfer returns units to the source.

**Valuation:** moving weighted average per (location, variant): inflows blend
`(avg x on_hand + cost x qty) / (on_hand + qty)`; outflows leave the average unchanged. Value counts
available + reserved; damaged and in-transit units are reported separately.

**Analytics:** *low stock* = available (all active warehouses) <= reorder point. *Movers* use units issued
(`STOCK_OUT`) over a window: fast >= `fast_min_units`; slow = holds stock and issued <= `slow_max_units`.
*Utilization* = used/capacity per warehouse and location. *Reorder* = position (available + in transit +
open PO quantity) <= reorder point -> order up to `reorder_point + reorder_quantity`, rounded up to the
preferred (else cheapest) supplier's MOQ.

## Consistency & concurrency
Layers, from strongest to last-resort:
1. **Single choke point.** `stock.apply_stock_change` is the only code that writes quantities. It
   validates all four buckets, updates the row, updates capacity counters and writes the ledger row.
2. **Row locking + optimistic versioning.** The stock row is read with `SELECT ... FOR UPDATE`
   (PostgreSQL) and written with `UPDATE ... WHERE id = ? AND version = ?`; a lost race re-reads and
   retries (bounded; then 409 `concurrency_conflict`). Multi-line operations process lines in a
   deterministic `(variant, location)` order to avoid deadlocks.
3. **Atomic conditional counters.** Warehouse/location usage is changed with
   `UPDATE ... SET used = used + n WHERE capacity IS NULL OR used + n <= capacity` so concurrent inbound
   operations cannot jointly exceed capacity. Receipt quantities use the same pattern
   (`received + q <= ordered`).
4. **Guarded state transitions.** PO/transfer status changes use `UPDATE ... WHERE status = <expected>`;
   a PO row is locked while receiving so concurrent receipts serialise and the final status is correct.
5. **First-write race.** Creating a missing stock row happens in a SAVEPOINT; the unique
   `(location, variant)` constraint makes losers fall back to reading the winner's row.
6. **Database constraints as a backstop.** `CHECK (bucket >= 0)`, `used <= capacity`,
   `received <= ordered`, FKs and unique keys. Constraint violations surface as 409, never 500.
7. **Transactions.** One transaction per request; any failure rolls everything back (no partial transfers
   or receipts).

SQLite runs with `BEGIN IMMEDIATE` (write lock taken up-front), `foreign_keys=ON` and a busy timeout so
the same code is safe under threads in dev/tests.

## Audit trail
Every bucket change writes a `StockMovement` (type, deltas, balances after, cost, actor via `X-User`,
reason, reference). PO workflow steps write `PurchaseOrderEvent`s. Adjustments require a reason.

## Known limitations / future work
* Quantities are integers (no fractional UoM); UoM is descriptive, with no conversion factors.
* Transfers are received in full (no partial transfer receipts).
* No authentication/roles: identity is a trusted `X-User` header.
* Single currency; no FX. Tax is per-line percentage and excluded from inventory cost.
* Reservations are anonymous quantities (not tied to sales orders / lots / serial numbers).
* Only the moving-average costing method is implemented (no FIFO/LIFO layers).
* Concurrency tests run on SQLite (writers serialise). PostgreSQL row-lock behaviour follows the same
  code paths but is not exercised by the automated suite in this change.
* Ledger immutability is enforced in the ORM and by convention; add DB triggers if direct SQL writers exist.
