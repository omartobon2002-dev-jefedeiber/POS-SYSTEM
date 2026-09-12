# Domain model

All three phases.

```
User ──< Membership >── Organization ──< Location
                             │              │
                             │              ├──< StockLevel >── ProductVariant
                             │              ├──< InventoryMovement >──┘
                             │              ├──< CashRegister ──< CashSession ──< CashMovement
                             │              ├──< Purchase ──< PurchaseItem >── ProductVariant
                             │              └──< Sale ──< SaleItem >── ProductVariant
                             │                    │  └──< Payment
                             │                    └──< Refund ──< RefundItem >── SaleItem
                             ├──< Category / Brand ──< Product ──< ProductVariant
                             ├──< Customer >───────────────────────┘ (optional on Sale)
                             ├──< Supplier
                             ├──< Device ──< SyncOperation
                             ├──< Subscription >── Plan
                             ├──< Invitation
                             ├──< DocumentSequence
                             ├──< AuditLog
                             └──< IdempotencyKey
```

## Identity and tenancy

### User
The person. Platform-level, **not** tenant-scoped: the same account can work in
two businesses. The global identity is `email`, unique across the platform.
The field stays nullable at the database level (the API supported email-less
accounts at one point), but `POST /employees/` now requires an email for every
person it creates, since email + password is the only sign-in form the client
implements. The password lives here, because it is the same in every business.

### Organization
The tenant. Holds business configuration directly — currency, timezone, tax,
fiscal contact, DIAN resolution and receipt settings — instead of a separate
settings table: it is one row per tenant and splitting it would only add a join.
Nothing of this is kept client-side; the server is the only source of truth.

**Tax is configured here and nowhere else.** `charges_tax` answers "does this
business sell with IVA?", and `tax_rate` is the rate it sells at. Products carry
no rate of their own. `Organization.effective_tax_rate` is the single accessor
every sale goes through: it returns `tax_rate`, or zero when `charges_tax` is
off. Switching the tax off leaves `tax_rate` untouched, so turning it back on
restores the configured rate.

`tax_regime` (Responsable de IVA / No responsable / Régimen Simple) is the DIAN
standing printed on the receipt. It is a label, never an input to the maths.

Colombian defaults: `country=CO`, `currency=COP`, `currency_decimals=0`,
`charges_tax=True`, `tax_rate=19`, `tax_regime=RESPONSABLE_IVA`.

### Membership
Binds a user to an organization, and carries that person's identity *inside* it:
`username` (unique per organization, so two shops may each have their own
`jperez`), a `role` (OWNER / MANAGER / CASHIER), a `status`
(ACTIVE / INVITED / SUSPENDED / LOCKED) and `default_location`, so a cashier's
terminal knows where it is without being told.

The lockout counters (`failed_attempts`, `locked_until`) live here too: sign-in
belongs to one business, so five bad guesses at one till must not close the
register in another.

Membership is never deleted — it is SUSPENDED, and an organization cannot lose
its last active owner.

### Invitation
An open invitation to join a business: `email`, `role`, `expires_at` and a
**hashed** token. The clear token exists only in the email that was sent. When
the invited email already has an account, accepting only adds a membership and
never touches that person's password.

### Location
A store or warehouse. Exactly one `is_default` per organization, enforced by a
partial unique constraint. Single-store tenants never see it; every inventory
movement, cash session and sale carries it anyway.

## Catalog

### Product
The commercial concept — "Nike Air Max". **Never sold, never stocked.** Holds
name, description, category, brand, `track_inventory` and `image`.

A product carries no tax rate: IVA is a property of the business, not of the
shoe (see Organization). A shop selling some goods taxed and others exempt is
not modelled — that would be an exception on top of the business rate, not a
mandatory field on every product.

`image` is one photo, not a gallery per colour or per variant — a small shop
photographs a garment once, and that is what staff and customers need to
recognise it. It is written only through `POST /products/{id}/photo/`
(multipart), never through the plain create/update body, and stored on local
disk for now (`apps/catalog/models.py:product_photo_path`), one file per
product named by a random id so a re-upload can never collide with the one it
replaces.

### ProductVariant
The sellable, stockable unit.

| Field | Note |
|---|---|
| `sku` | unique per organization |
| `barcode` | unique per organization when non-empty (partial index) |
| `size`, `color` | explicit columns — the two axes fashion retail reports on |
| `attributes` | JSONB for the long tail: material, season, fit |
| `price` | **tax-inclusive** shelf price (D1) |
| `average_cost` | moving weighted average, recomputed on purchase receipt (D3) |
| `last_purchase_cost` | informational |
| `weight_grams`, `is_active` | |

```
Product:  Nike Air Max
Variants: 38/Black · 39/Black · 40/Black · 41/Black · 40/White
```

Variants are never deleted — they are referenced by the ledger and by sales.
Deactivate instead.

## Inventory

### InventoryMovement — the source of truth
Append-only. One immutable fact: this many units entered or left this location.

| Field | Note |
|---|---|
| `quantity` | **signed**: positive enters, negative leaves. Never zero (check constraint) |
| `movement_type` | INITIAL_STOCK · PURCHASE · SALE · RETURN · ADJUSTMENT · TRANSFER |
| `unit_cost` | frozen at the moment of the movement, so a past sale's margin never changes |
| `source_type` / `source_id` | plain columns, not a GenericForeignKey: no content-type join on the hot path |
| `created_by`, `occurred_at`, `note` | `occurred_at` is when it happened **in the store**, which may predate sync |

### StockLevel — the cache
`(location, variant) → quantity`, unique. Written only by `InventoryService`.
Rebuildable at any time with `manage.py recalculate_stock`.

### StockDiscrepancy
Opened when a movement drives stock negative — which only happens on the
offline replay path (D4). Carries before/requested/after and waits for a human.

### The invariant

```
SUM(InventoryMovement.quantity)  ==  StockLevel.quantity
        for every (location, variant)
```

Worked example:

```
INITIAL_STOCK  +10   → stock 10
ADJUSTMENT      -3   → stock  7
PURCHASE        +5   → stock 12
SALE            -2   → stock 10
```

## SaaS

### Plan
Platform-level, shared by all tenants. BASIC / PRO / BUSINESS.
`max_users`, `max_locations`, `max_products` — **null means unlimited**, which
is how they are seeded: pricing and limits are commercial decisions that have
not been made.

### Subscription
One per organization. `status` TRIAL → ACTIVE → PAST_DUE / CANCELLED / EXPIRED,
`billing_cycle`, trial and period dates. 14-day trial on signup.

`PAST_DUE` deliberately keeps working: cutting a store off mid-sale over
billing is a product decision, not a technical one.

`provider` and `external_reference` are empty seams for a future gateway.

## Cross-cutting

### AuditLog
`action` (`sale.refunded`, `inventory.adjusted`, `user.permission_changed`, …),
`actor`, `object_type`/`object_id`, `metadata` (JSONB), `ip_address`.
Written from services, answers "who did this and when".

### IdempotencyKey
`(organization, key)` unique. Stores the request hash and a snapshot of the
response, so a retry replays instead of re-executing. Shared by HTTP retries
and, in phase 3, by offline `operation_id`s.

## Customers

### Customer
Name, phone, email, optional document (`CC` / `CE` / `NIT` / `PASSPORT`),
address, birth date, notes. `document_number` is unique per organization when
present.

Purchase history is **derived** from sales (`total_purchases`, `total_spent`
are annotations), never stored — a denormalised counter would drift the first
time a sale is cancelled.

## Purchasing

### Supplier
Name (unique per organization), tax id, contact, phone, email, address.

### Purchase
`DRAFT → RECEIVED` (or `DRAFT → CANCELLED`). Carries location, supplier,
`supplier_invoice`, `purchased_at`, `received_at`, `total_cost`, and both the
user who created it and the user who received it.

A number is assigned **on receipt**, not on creation: a draft that never
arrives must not consume a consecutive.

Receiving is what makes stock real. In one transaction it writes `PURCHASE`
movements, updates each variant's moving average cost, and stamps the number.
A received purchase cannot be cancelled — it is corrected with an inventory
adjustment, so the ledger keeps its history.

### PurchaseItem
Variant, quantity, `unit_cost`, `total_cost`. Unique per `(purchase, variant)`.

## Cash

### CashRegister
A physical till, belonging to a location. Unique `code` per organization. One
is created automatically (`PRINCIPAL`) alongside the organization's default
location, so a store can take cash on day one with no setup step.

### CashSession
One shift: `OPEN → CLOSED`. Opening float, opener and time; at close, the
`expected_amount` the movements imply, the `counted_amount` the cashier
counted, and the `difference` between them (positive = surplus).

A partial unique index enforces **one open session per register**.

### CashMovement
Signed money in or out of the drawer, append-only:
`OPENING · SALE · REFUND · WITHDRAWAL · DEPOSIT · ADJUSTMENT`.

Only cash creates movements. The closing summary reports card and transfer
totals separately, from the sale's payments.

```
expected_amount = SUM(CashMovement.amount)   for the session
difference      = counted_amount - expected_amount
```

## Expenses

### ExpenseCategory
How one business groups its spending: `name`, `is_active`, unique per
organization. Nine defaults are created with the organization (Arriendo,
Nómina, Servicios públicos…) so nothing has to be configured before the first
expense. Deactivated rather than deleted, because expenses point here with
PROTECT and old reports must stay readable.

### Expense
One payment out that is **not** merchandise: `category`, `location`, optional
`supplier`, `description`, `amount` (always positive), `payment_method`,
`occurred_at`, `reference` and `note`.

Merchandise never appears here. It enters as a `Purchase` and is counted as
cost of goods sold when it is actually sold, so counting it as an expense too
would subtract it twice from the same profit.

A cash expense is not just a record — the money left the drawer. It writes a
`WITHDRAWAL` into the cash ledger and stores the `cash_session` it came from,
so the arqueo accounts for it without anyone remembering to register a
withdrawal by hand. With no open register it is still recorded, with
`cash_session` null: the owner paid it from elsewhere, and refusing it would
only push the figure out of the system.

`amount` and `payment_method` are immutable once written, for the same reason
the ledger is append-only: they would rewrite a drawer movement an arqueo may
already have counted. Deletion is allowed only while that shift is open.

```
gross_profit = revenue - cost_of_goods       (margin report)
net_profit   = gross_profit - expenses       (profit report)
```

Only the second one answers "how much did I make". It could not exist before
this model did.

## Sales

### Sale
`DRAFT · COMPLETED · CANCELLED · REFUNDED · PARTIALLY_REFUNDED`

| Field | Note |
|---|---|
| `number` | consecutive per location, assigned at completion |
| `location`, `seller`, `customer` | customer is optional |
| `cash_session` | set when the sale was taken on a register with an open shift |
| `subtotal`, `discount_total`, `tax_total`, `total` | all recomputed server-side |
| `paid_total`, `change_amount` | change is only possible against cash |
| `refunded_total` | running total of refunds against this sale |
| `source`, `device_id` | `POS` or `SYNC`, plus the terminal that created it offline |
| `occurred_at` | when it happened in the store, which may predate sync |

`SETTLED_STATUSES` (`COMPLETED`, `PARTIALLY_REFUNDED`, `REFUNDED`) are the
statuses that count as revenue. Cancelled sales never do.

### SaleItem
Snapshots the product at the moment of sale — `description`, `sku`, `tax_rate`,
`unit_cost` — so a receipt stays readable and a margin stays fixed after the
catalogue changes. `tax_rate` here is the business's rate frozen at that
instant: changing it (or turning IVA off) never rewrites what was already sold.

`refunded_quantity` is materialised and guarded by a check constraint
(`refunded_quantity <= quantity`).

### Payment
Method (`CASH` / `CARD` / `TRANSFER` / `OTHER`), amount, reference (voucher or
approval code). A sale may have several.

### Refund / RefundItem
A refund belongs to exactly one sale. Each line points at a `SaleItem` and may
not exceed its remaining `refundable_quantity`. `restock=False` pays the
customer without crediting stock (damaged goods).

### The sale invariants

```
total        = SUM(SaleItem.line_total)
line_total   = unit_price * quantity - discount_amount
taxable_base + tax_amount = line_total          (tax-inclusive, D1)
paid_total  >= total
change_amount = paid_total - total              (cash only)
SUM(RefundItem.quantity) <= SaleItem.quantity   per line
```

Worked example — one shoe at 119.000 with 19% IVA, paid with 150.000 in cash:

```
line_total    119.000
taxable_base  100.000
tax_amount     19.000
paid_total    150.000
change         31.000
drawer        +119.000
stock              -1
```

## Cross-cutting

### DocumentSequence
`(organization, location, document_type) → last_number`, with a prefix
(`V-` sales, `C-` purchases, `D-` refunds). Locked until the creating
transaction commits, so numbering is gap-free.

## Synchronization

### Device
A terminal allowed to push offline operations: `identifier` (unique per
organization), name, location, optional cash register, platform, app version,
`last_seen_at` / `last_sync_at`.

Registering the same identifier twice **updates** the device rather than
duplicating it, so a till that reinstalls the app keeps its history.
Deactivating it cuts a lost terminal off without erasing what it sent.

### SyncOperation
One operation captured offline and replayed.

| Field | Note |
|---|---|
| `operation_id` | UUID minted by the terminal, **unique per organization** — this is what makes the protocol idempotent |
| `operation_type` | `SALE_CREATE` · `SALE_CANCEL` · `REFUND_CREATE` |
| `status` | `PROCESSED` or `FAILED` — a failure is recorded, not retried forever |
| `payload` | exactly what the terminal sent |
| `result` | what the server produced (sale id, number, total) |
| `error_code` / `error_detail` | why it was rejected |
| `occurred_at` vs `received_at` | when the store did it, versus when the server heard |

Opening and closing a cash session are online-only acts; only sales,
cancellations and refunds replay.

## Reporting

No models. `apps/reporting` is a set of read-only queries:

| Report | Answers |
|---|---|
| `sales-summary` | Gross, refunded, net, tax, discounts, average ticket, by payment method, by day |
| `top-products` | Best sellers by units **net of returns** |
| `margin` | Revenue against the cost frozen on each line when it was sold |
| `inventory-valuation` | Stock at average cost and at retail, plus any negative stock |
| `cash-sessions` | Arqueo history: shortfalls and surpluses |
| `refunds` | Count and value, split between restocked and written off |
