# Resource Inventory Management

## Scope and data policy

The backend supports inventory management for configurable resource types, including food packets, water, medical kits, rescue vehicles, and personnel. These are examples of resources that can be configured; migrations do not seed operational inventory or represent test/demo quantities as real stock.

## Data model

- `resource_types` stores a unique code and case-insensitively unique name, category, unit, description, active status, and timestamps. Types are data, not separate quantity columns.
- `resource_depots` stores an active location with a PostGIS `POINT` in EPSG:4326 when coordinates are available, plus address, district, state, postal code, and contact fields.
- `inventory_balances` stores the current on-hand and reserved values per depot/resource type. `available_quantity` is computed as on-hand minus reserved. Database constraints reject negative on-hand or reserved quantities and reserved values greater than on-hand.
- `inventory_transactions` is the audit ledger. It records positive-quantity receipts, consumption, transfers, or migration opening balances with source/reference, actor, notes, and timestamps. Foreign keys prevent deleting depots or resource types while referenced by balances or ledger entries.

Transfers are a single ledger entry with source and destination depots and atomically debit and credit both balances. The service locks balances in depot-ID order, preventing concurrent operations from spending the same available stock or deadlocking on opposite-direction transfers. Balance rows and ledger entries must be committed or rolled back together in the caller's SQLAlchemy transaction.

## Application use

Use `app.services.resource_inventory.record_stock_movement` for receipts, consumption, and transfers. It validates positive finite quantities, verifies transfer endpoints, checks available stock, locks the affected balance rows, updates the balance snapshot, and appends the immutable transaction record. The caller controls the surrounding database transaction.

Use `calculate_inventory_balance` to independently reconstruct on-hand quantities from the ledger for an audit/reconciliation check. Inventory transaction rows cannot be updated or deleted through the SQLAlchemy ORM.

Example resource types to configure (not inserted automatically):

| Name | Category | Suggested unit |
| --- | --- | --- |
| Food packet | food | packet |
| Drinking water | water | litre |
| Medical kit | medical | kit |
| Rescue vehicle | transport | vehicle |
| Response personnel | personnel | person |

## Migration and legacy quantities

Alembic revision `4ad29f081c7e` creates the transaction ledger and balance snapshot, extends depot/type metadata, and removes the old `inventory` table. Existing inventory rows, if present, are copied to `inventory_balances`; positive legacy quantities are also recorded as `opening_balance` ledger entries whose source and reference identify the migration and old row. This is a provenance-preserving migration entry, not fabricated operational data.

Apply migrations from `backend` with `alembic upgrade head`. Run the focused tests with `pytest tests/test_resource_inventory.py`.

## Limitations

- No default resource types or stock are seeded. Configure resource types and depots from verified operational data.
- Inventory balances are maintained by the application service; direct database edits can make the balance snapshot disagree with the ledger. Reconcile with `calculate_inventory_balance`.
- Reservations are represented in the balance snapshot but reservation workflows are outside this step.
- The service is currently a backend persistence/service layer; no inventory API, UI, demand prediction, allocation optimization, or routing is included.
