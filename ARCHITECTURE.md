# Architecture

## 1. The shape of the problem

Most of this system is ordinary CRUD. Three things are not, and they are where
the design effort went:

1. **Tenant isolation** — one leak ends the product.
2. **Ledger inventory under concurrency** — two registers, one shoe, one truth.
3. **Idempotency** — an offline-first POS makes duplicate delivery normal, not exceptional.

Everything else (customers, suppliers, reports) is mechanical and was kept
deliberately plain.

## 2. Tenancy

**Shared database, shared schema, `organization_id` on every row.**

```
User ──< Membership >── Organization ──< Location
             │
   username + role + status
```

- `Organization` **is** the tenant. It is not itself tenant-scoped.
- Every business row inherits `TenantScopedModel` (`apps/core/models.py`):
  UUID pk, timestamps, `organization` FK, and two managers.

### Where the tenant comes from

The organization is a claim inside the signed JWT, chosen after login and
re-validated against an **ACTIVE** membership on *every* request
(`apps/core/authentication.py`). It is never read from a header, a query
parameter or a request body. Signing the claim is not what makes it safe —
re-reading the membership is: a suspended employee, a deleted membership or a
deactivated business stops working on the next request rather than when the
token expires.

A token with no organization claim is an **identity token**: it says who you
are, lists your businesses and lets you pick one, and opens no business
endpoint at all. Choosing or switching means calling
`/auth/select-organization/`, which re-checks membership and mints a session
token. See §3.

The validated organization is bound to a `contextvar` for the request, and
cleared before and after by `TenantContextMiddleware` — workers are reused, and
a leftover tenant would be the worst bug this system could have.

### Three layers of isolation

| Layer | What it stops |
|---|---|
| `TenantManager` (default manager) | Any query that forgets to filter — including reverse relations, since it is `_default_manager` |
| `TenantPrimaryKeyRelatedField` | A nested id in a request body pointing at another tenant's row |
| Composite unique constraints | `(organization, sku)`, `(organization, barcode)`, `(organization, code)` — no global namespace to collide in |

The second layer matters more than it looks. List endpoints are usually
filtered correctly; the hole in real systems is `{"variant_id": "<someone
else's uuid>"}`. `TenantModelSerializer` sets `serializer_related_field`, so
every auto-generated relation is scoped without anyone remembering to do it.

### Why the manager defers its error

A queryset built with no tenant context is **not filtered and not rejected** —
it is marked *pending* and refuses to **execute**.

The reason is practical: django-filter, drf-spectacular and the admin all call
`Model._default_manager.all()` at import time, before any request exists.
Raising there makes the ecosystem unusable; silently returning `.none()` turns
a missing-context bug into mysterious missing data, which is worse. Marking the
queryset unusable keeps introspection (which only reads `.query`) working while
any real read or write without a tenant fails loudly.

`apps.core.context.unscoped()` is the deliberate escape hatch for
cross-tenant maintenance. It must never appear in a request path.

### Decision NOT taken: PostgreSQL Row-Level Security

RLS would add a second, database-enforced layer. It was skipped because it
complicates migrations, tests and connection pooling, and the current threat
model has no untrusted SQL. Nothing in the data model prevents adding it later
as defense in depth — the `organization_id` column it needs is already there.

## 3. Signing in

Identity and business are separate questions, and the tokens keep them separate.

| Step | Endpoint | Result |
|---|---|---|
| Sign in globally | `POST /auth/login/` with `email` + `password` | **Identity token** + the list of businesses. With exactly one, it returns that session directly — there is nothing to choose. |
| Sign in at a till | `POST /auth/login/` with `organization` (slug) + `username` + `password` | **Session token**. On a registered terminal the `X-Device-Token` header supplies the business and the slug is unnecessary. |
| Choose or switch | `POST /auth/select-organization/` | **Session token** for that business. Works from an identity token or from another business's session: switching never asks for the password again. |
| Open another business | `POST /auth/organizations/new/` | A second business under the same account. This is what the whole model is for. |

The password belongs to the person and is the same everywhere — a till login
is the same secret as a global login, just scoped to one membership by
`username` + `organization` instead of `email`. The lockout, though, is always
local: five bad guesses close one till, not that person's shifts in every
other shop.

Every employee has an email — the client only implements the global (email +
password) sign-in path, so `POST /employees/` requires one. If the email given
already has an account, that person is linked to this business instead of
creating a duplicate.

### Joining a business

Two ways in, both kept on purpose:

- **Direct** (`POST /employees/`) — the owner sets the username, email and
  password themselves. No invitation, no acceptance, works immediately. If the
  email given already has an account, that person is linked rather than
  duplicated.
- **By invitation** (`POST /invitations/`) — an emailed link carrying a token
  that is stored hashed, expires, and can be revoked or reissued. Accepting
  creates the membership; when the email already has an account it creates
  *only* the membership and never touches that password.

## 4. Authorization

Roles are what users see; **capabilities** are what the code checks.

```
OWNER    → every capability
MANAGER  → everything except users.manage / organization.manage / subscription.manage
CASHIER  → products.read, sales.create, cash.*, customers.*, inventory.read
```

The role is read from the **membership**, not from the person: the same account
may be an owner here and a cashier next door, and gets exactly the capabilities
of the business it is currently working in.

The role→capability map lives in code (`apps/core/capabilities.py`), not in a
table. Per-tenant custom roles later mean adding a `Role` model that resolves
to the same capability strings — **no view or permission class changes**. That
is the whole point of not checking roles directly.

Views declare `read_capability` / `write_capability`; `HasCapability` enforces
them server-side. The frontend hiding a button is never the control.

## 5. Inventory

**The ledger is the truth. The balance is a cache.**

```
InventoryMovement   append-only, never UPDATEd, never DELETEd
StockLevel          materialised (location, variant) → quantity
```

A mistake is corrected with a compensating movement, which is exactly what
makes history auditable. `manage.py recalculate_stock` rebuilds every balance
from the ledger; a non-zero `levels_corrected` means something wrote stock
outside `InventoryService`, which is a bug.

### Concurrency

`InventoryService.apply_movements()` is the only writer. It:

1. creates any missing `StockLevel` rows (`bulk_create(ignore_conflicts=True)`),
2. locks them with `SELECT … FOR UPDATE` **ordered by variant id**,
3. checks availability, writes the movements and the new balances in one transaction.

The ordering is not cosmetic. Register 1 selling *(A, B)* and register 2
selling *(B, A)* deadlock without it — a bug that stays invisible until a busy
Saturday. Locks are taken on `StockLevel`, never on `ProductVariant`, so
editing a price never blocks a sale.

### Out-of-stock policy (decision D4)

| Path | Behaviour |
|---|---|
| Online | Refuses to oversell → `InsufficientStock`, HTTP 409 |
| Replayed from offline | Accepted with `allow_negative=True`, stock may go negative, a `StockDiscrepancy` is opened |

The goods already left the shelf; refusing the record does not put them back.
This is the only behavioural difference between the two paths, and it lives in
one argument in one function.

## 6. Sales — the transactional core

A completed sale is **one atomic fact**. Inside a single `transaction.atomic()`:

```
stock movements  →  document number  →  Sale  →  SaleItems  →  Payments  →  cash movement
```

If any step fails, none of it happened. There is no state in which a receipt
exists but the goods never left, or the goods left but no receipt exists.

Stock is taken **first**, deliberately: the most likely failure is
`InsufficientStock`, and failing before a number is reserved keeps the
consecutive sequence tight.

The sale's id is minted in Python rather than by the database, so the stock
movements can point at the sale before its row exists — and so an offline
terminal can send the id it already printed on the customer's receipt.

### What the server never trusts

Totals, taxes and change are always recomputed from the catalogue. The client
may send `expected_total`; if it disagrees with the server, the sale is
rejected with `price_mismatch` (409). That is the offline case: a terminal
working from a stale price list must not sell at yesterday's price silently.

A price override per line *is* allowed — haggling is normal in retail — but it
is explicit, recorded on the line, and auditable.

### Frozen snapshots

Each `SaleItem` stores its own `description`, `sku`, `tax_rate` and `unit_cost`.
Renaming a product or receiving a cheaper batch must never rewrite the tax
breakdown or the margin of a sale that already happened.

### Document numbering

`DocumentSequence` holds one counter per `(organization, location,
document_type)`, locked with `SELECT … FOR UPDATE` until the creating
transaction commits.

This is a deliberate serialisation point: two registers in the same store
complete sales one after the other, for a few milliseconds each. Gap-free
numbering is worth that, especially with electronic invoicing ahead. The lock
is per store and per document type, so two locations — or a sale and a
purchase — never wait on each other. A rejected sale releases its number with
the rollback, so no gap appears.

## 7. Cash

```
CashRegister ──< CashSession ──< CashMovement
```

**Only cash touches the drawer.** Card and transfer payments are recorded on
the sale and reported per method at closing time, but they never move the
counted balance — otherwise every arqueo would show a difference that does not
exist. Change given leaves the drawer, so a 150.000 payment on a 119.000 sale
nets 119.000.

`expected_amount` is the sum of the session's movements — the same
ledger-versus-cache idea as inventory. Closing compares it to what the cashier
counted and stores the difference; a shortfall is recorded, never silently
absorbed.

A partial unique index enforces one open session per register.

`provision_organization` creates one default `CashRegister` alongside the
default location — a store can take cash the moment it signs up, with no setup
step. Cash control stays *effectively* optional: a sale that omits
`cash_register` still completes with no drawer movement, **unless the
organization currently has an open cash session somewhere**, in which case
omitting it is refused (`invalid_operation`, 400) instead of silently
completing outside every drawer. That is the one case where the omission is
almost certainly a client bug rather than a deliberate choice — the money was
taken while a shift was open, and it would otherwise vanish from that day's
arqueo. A store with a register nobody has opened today is unaffected.

## 8. Refunds

A refund is capped by what remains refundable on each line
(`quantity - refunded_quantity`), enforced three ways:

1. The service checks the cap.
2. The sale row is locked with `SELECT … FOR UPDATE`, so two cashiers refunding
   the same receipt at once cannot both pass the check.
3. A `CheckConstraint` (`refunded_quantity <= quantity`) makes the invariant
   impossible to violate even from a shell.

When the last refundable units go back, the refund amount is the **exact
remainder** of the line rather than a proportional share. Otherwise rounding
would leave a few pesos permanently unrefundable on a line that was fully
returned.

`restock=False` covers damaged goods: the customer is paid, the stock is not
credited. Cancelling a sale is only possible while no refund exists — once
there is a partial refund the sale has a history, and cancelling would erase it.

## 9. Idempotency

HTTP retries and offline `operation_id`s are the same problem, so they share
one mechanism (`apps/core/idempotency.py`).

```
same key + same payload      → the original response is replayed, work runs once
same key + different payload → 409, keys are not reusable
key still in flight          → 409, retry later
operation failed             → key released, a legitimate retry can succeed
```

The reservation row is committed in its **own** transaction before the business
transaction, so a concurrent duplicate collides on the unique index
`(organization, key)` instead of running twice.

`Idempotency-Key` is **mandatory** on `POST /sales/` and `POST /refunds/`:
money must not move twice because a till lost its connection mid-request.
It is optional elsewhere.

## 10. Offline synchronisation

The POS keeps selling when the connection drops. Every offline operation is
stamped with a UUID the terminal generates and replayed later.

```
Device ──< SyncOperation(operation_id unique per tenant)
```

### Push

`POST /sync/operations/` takes a batch and returns **one result per
operation**: `PROCESSED`, `FAILED`, or a duplicate no-op. The batch itself
needs no idempotency key — every operation carries its own, so resending the
whole batch is safe.

Each operation runs in **its own transaction**. One malformed operation must
not roll back the twenty valid ones around it, and a till that has been offline
all weekend cannot be blocked by a single bad row.

A rejected operation is recorded as `FAILED` with its error code and stays
recorded: resending it returns the stored failure instead of retrying forever.
Fixing it needs a human or a corrected payload, not a tighter retry loop.

### Why SyncOperation and not IdempotencyKey

Phase 1 anticipated resolving `operation_id` through the `IdempotencyKey`
table. It ended up as its own table, and the reason is the batch: an
`IdempotencyKey` replays one stored HTTP response, while a sync push returns a
result per operation. Sync also needs state that has no place in a generic
idempotency record — the device, the raw payload, the failure reason, and when
the terminal actually performed the operation.

The guarantee is identical (`UNIQUE (organization, operation_id)`); only the
table differs.

### Pull

`GET /sync/pull/?since=<cursor>` returns the catalogue, customers and stock
that changed since the cursor, plus a new cursor.

Soft deletion is what keeps this simple: nothing is ever removed, rows are
deactivated. There are no tombstones to reconcile — a terminal that receives
`is_active: false` stops offering the item.

### The offline exemption

Replayed sales pass `allow_negative_stock=True`, so stock may go negative and a
`StockDiscrepancy` is opened (decision D4). This is the **only** difference
between the online and offline paths, it is one argument in one function, and a
test asserts that the online path still refuses the identical sale.

## 11. Reporting

No report models and no denormalised report tables. Everything a store asks for
is already in the ledger and the sales tables, and a second copy of the truth is
a second thing that can be wrong. If a query outgrows the data volume, the
answer is an index or a materialised view, not a parallel schema.

Margins use the `unit_cost` frozen on each sale line, so a report about last
month does not move when this month's purchases change the average cost — a
test asserts exactly that. Units sold are net of refunds.

## 12. Services

Business logic lives in service functions, not in views, serializers or model
`save()` overrides: `SaleService`, `RefundService`, `InventoryService`,
`CashService`, `receive_purchase`, `provision_organization`. Trivial CRUD
(customers, suppliers, brands) stays in the viewset — a service class per
resource would be ceremony, not architecture.

Services never import views, and the dependency direction is one-way:

```
sync → sales → cash → inventory → catalog → organizations → core
reporting ─────────────┘ (reads only)
```

## 13. Audit

`AuditLog` is written **explicitly from services**, never from signals. A
signal knows that a row changed; it does not know that the change was
`sale.refunded` rather than `sale.updated`. Business intent cannot be
reconstructed after the fact, so it is recorded at the moment it is known.

Recorded events: `sale.created`, `sale.cancelled`, `sale.refunded`,
`purchase.created`, `purchase.received`, `purchase.cancelled`, `cash.opened`,
`cash.closed`, `cash.withdrawal`, `cash.deposit`, `inventory.adjusted`,
`inventory.initial_stock`, `organization.created`, `user.created`,
`user.permission_changed`, `user.suspended`, `device.registered`,
`device.deactivated`.

## 14. Decisions taken, with reasons

| # | Decision | Why | Cost of reversing later |
|---|---|---|---|
| D1 | Prices stored tax-inclusive; tax extracted when documenting | Colombian retail convention; the shelf price is the price | Low — one helper in `core/money.py` |
| D1b | Tax configured once per business (`Organization.charges_tax` + `tax_rate`), never per product | A small shop has one IVA standing, not one per item. A rate on every product was four defaults that could disagree with each other, and the till showed a breakdown the server did not compute | Medium — a per-product override would go on top of the business rate, and old sales already snapshot their own rate |
| D2 | `Location` modelled from day one | Ledger, cash and sales all carry it. Retrofitting means migrating all history | **Very high** — this is why it exists now |
| D3 | Moving weighted average cost | Standard in retail, survives out-of-order offline operations; FIFO layers do not | Medium |
| D4 | Block online, accept offline + discrepancy | Matches what physically happened in the store | Low |
| — | UUID primary keys | An offline terminal must mint the final id of a sale locally | High |
| — | Integer quantities | Clothing and footwear sell by the unit | Medium |
| — | JWT, not sessions | The POS must hold a valid credential while offline | Medium |
| D5 | Line-level discounts only | A whole-sale discount would need proration across lines to keep the tax split exact. Clients apply it as line discounts | Low |
| D6 | Cash-only drawer movements | Card totals belong to the sale; mixing them would falsify every arqueo | Low |
| D7 | Sales are immutable | Correction happens through refund or cancellation, both of which leave a trail | High |
| D8 | `SyncOperation` is its own dedup table | Batch semantics return a result per operation, and sync needs device/payload/failure state | Low |
| D9 | Reports are queries, not tables | A second copy of the truth is a second thing that can be wrong | Low |
| D10 | A lapsed subscription blocks writes, never reads | A store that stops paying must still get its own data out | Low |
| D11 | A default `CashRegister` is provisioned with the organization; a cash sale with no register is refused only while a session is open somewhere | Cash usable on day one; a client that forgets `cash_register` mid-shift loses money from the arqueo silently otherwise | Low |

## 15. Security posture

| Control | Where |
|---|---|
| Tenant isolation | Three layers, §2 |
| Server-side authorization | `HasCapability`, §4 |
| Cross-tenant id probing | 404, never 403 — existence is not confirmed |
| Rate limiting | `register` 5/h, `auth` 10/min, `sync` 60/min, `write` 120/min |
| Subscription gating | Writes blocked when cancelled or expired (D10) |
| Plan limits | `subscriptions.limits.enforce_limit` on users, locations, products |
| Credential lockout | Per membership, not per person: 5 bad passwords close one till and leave that person's other shops alone |
| Invitation tokens | Hashed at rest, expiring, revocable; the clear value exists only in the email |
| Secrets | Environment only. Production **refuses to boot** without `DJANGO_SECRET_KEY` — no default |
| Transport | SSL redirect, HSTS with preload, secure cookies, `SameSite=Lax`, `Referrer-Policy` |
| Headers | `nosniff`, `X-Frame-Options: DENY` |
| Upload bounds | 5 MB, so one request cannot exhaust a worker |
| Logs | No request bodies, no tokens, no credentials |
| CORS | Explicit origin allowlist, credentials off |

`manage.py check --deploy` passes clean against production settings.

## 16. Deliberately NOT built

- **PostgreSQL RLS** — see §2.
- **Celery** — nothing in phase 1 justifies a broker and a worker. Redis is
  used for cache and throttling. Sync and reports in phase 3 will bring it in.
- **A payment gateway** — `Subscription.provider` / `external_reference` are
  the seam. Wompi, Mercado Pago or Stripe plugs in without touching the core.
- **Generic quota framework** — plan limits are three explicit calls to
  `subscriptions.limits.enforce_limit`.
- **Plan prices and limits** — those are commercial decisions nobody has made.
  Plans are seeded at 0 / unlimited rather than inventing numbers.
- **Full EAV product attributes** — `size` and `color` are columns because they
  are the two axes fashion retail actually reports on; `attributes` (JSONB)
  absorbs the long tail with no migration per attribute.
- **A generic RBAC engine** — see §4.
- **Sale editing** — see D7. There is no `PATCH /sales/{id}/`.
- **Whole-sale discounts** — see D5.
- **Partial purchase receipt** — a purchase is received in full. Splitting a
  delivery is two purchases, which is also how the supplier invoices it.
- **Layaway, credit sales, store credit** — no requirement stated. Payments
  must cover the total today.
- **Celery** — still not justified. Sync batches are small and processed
  inline; reports are queries. Redis serves cache and throttling. A broker and
  a worker would be two more things to operate for no current benefit. The
  first real trigger will be scheduled reports or emailed receipts.
- **Offline cash sessions** — opening and closing a till is an online act. Only
  sales, cancellations and refunds replay from offline.
- **Per-device credentials** — a device is registered by an authenticated user
  and pushes under that user's token. Device-scoped tokens are the next step if
  terminals ever run unattended.
- **Conflict resolution beyond D4** — a replayed sale is accepted as fact. There
  is no merge algorithm because there is nothing to merge: sales are appends,
  not edits.
- **PDF or printed receipts** — `/sales/{id}/receipt/` returns the data; the
  rendering belongs to the client.

## 17. Constraints for contributors

1. **Never write `StockLevel.quantity` outside `InventoryService`.**
2. **Never introspect a tenant model at import time.** Declare FK filters
   explicitly in FilterSets; use `model = X` on viewsets, not
   `queryset = X.objects.all()`.
3. **Never use `all_objects` in a request path.** It exists for the admin,
   maintenance commands and the authentication lookup that establishes the
   context in the first place.
4. **Record audit entries from the service that knows the intent.**
5. **Never create a `Sale`, `Refund` or stock movement outside its service.**
   The atomicity guarantees live there, not in the models.
6. **A module-level `FilterSet` on a tenant model must declare its foreign-key
   filters explicitly** (`filters.UUIDFilter(field_name="x_id")`). A generated
   `ModelChoiceFilter` captures its queryset at import time, when no tenant
   context exists. `filterset_fields` is built per request and is safe.
7. **Reports never write.** `apps/reporting` has no models and no service that
   mutates anything.
8. **New sync operation types go in `_HANDLERS`** and must be replay-safe on
   their own, not because the caller promises to send them once.
