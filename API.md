# API

Base path `/api/v1/`. JSON only. Interactive docs at `/api/v1/docs/` (Swagger)
or `/api/v1/reference/` (Scalar, self-hosted — no CDN dependency), schema at
`/api/v1/schema/`.

## Authentication

`Authorization: Bearer <access>`. One person, one account, **many businesses**.
The business is a signed claim inside the token — never a tenant id in a URL,
header or body — and it is re-checked against an active membership on every
request.

Two kinds of token:

- **Identity token** — who you are. Lists your businesses and lets you pick
  one. Opens no business endpoint (403).
- **Session token** — who you are *and* which business. Everything else needs
  this one.

| Method | Path | Notes |
|---|---|---|
| POST | `/auth/register/` | Creates the account + organization + default location + default cash register + trial. Returns a session. 5/hour |
| POST | `/auth/login/` | Two forms, below. 10/min |
| POST | `/auth/refresh/` | The business claim survives the refresh |
| GET | `/auth/organizations/` | Your businesses. Works with either token |
| POST | `/auth/select-organization/` | `{"organization": "<slug or id>"}` → session token. Choosing and switching are the same call |
| POST | `/auth/organizations/new/` | Another business under the same account. Returns its session |
| GET | `/auth/me/` | The current session, re-read from the database |
| PATCH | `/auth/me/` | Your own name, phone, email |

**The two ways to sign in:**

```
1. Global (SSO)      { "email": "ana@tienda.co", "password": "..." }
                     → identity token + your businesses
                       (with exactly one business, the session directly:
                        there is nothing to choose)

2. Business + user   { "organization": "boutique-iber",
                       "username": "jperez", "password": "..." }
                     → session token. A registered till sends the same body
                       with X-Device-Token instead of "organization": the
                       till already knows its business.
```

A terminal gets its token from `POST /sync/devices/`, which returns it in the
clear exactly once. Registering one needs an authenticated session, so form 2
is always the bootstrap path.

**Identity is per business.** `jperez` at *Boutique Iber* and `jperez` at *Otra
Tienda* are two different memberships, possibly of two unrelated people. The
username lives on the membership; the email and the password live on the
person and are the same everywhere.

**Every employee has an email.** `POST /employees/` requires one, because it is
the credential the client signs in with (form 1). If the email already has an
account elsewhere, that person is linked to this business instead of creating
a duplicate.

An unknown slug, an unknown username, an unknown email and a wrong secret all
return the same 401 `invalid_credentials`: the endpoint must not reveal which
shops exist or who works there. Five bad attempts lock **that membership** for
15 minutes — one till, not that person's shifts elsewhere.

A session response is everything a client needs to work:

```json
{
  "access": "...", "refresh": "...",
  "scope": "session",
  "user": {"id": "...", "email": "ana@tienda.co", "full_name": "Ana"},
  "organization": {"id": "...", "slug": "moda-urbana", "name": "Moda Urbana", "currency": "COP"},
  "membership": "...",
  "role": "OWNER",
  "capabilities": ["cash.close", "inventory.adjust", "products.write", "..."],
  "default_location": "..."
}
```

An identity response instead carries `"scope": "identity"` and `organizations`,
one row per business with its `membership`, `organization` and `role`.

Drive the UI from `capabilities`; they are the role's, in *this* business. The
server checks them again regardless.

## Employees

Two ways onto a team, and both are kept: the counter needs one, a distributed
team needs the other.

`/employees/{id}/` is a **membership** id, not a person id — a business
administers its side of the relationship and nothing else.

| Method | Path | Capability | Notes |
|---|---|---|---|
| GET | `/employees/` | `organization.read` | Everyone in this business |
| POST | `/employees/` | `users.manage` | `username`, `first_name`, `role`, `password`, `email`. An `email` that already has an account links that person instead of duplicating them |
| PATCH | `/employees/{id}/` | `users.manage` | Username, role, status, default location |
| DELETE | `/employees/{id}/` | `users.manage` | Sets `status: SUSPENDED`; never deletes the row |
| POST | `/employees/{id}/unlock/` | `users.manage` | Clears a lockout from failed sign-ins |

An employee signs in with `username` + `password` (or globally with `email` +
`password`).

Personal fields (`first_name`, `last_name`, `phone`, `email`) belong to the
person, not the business. They can be edited through this endpoint only while
that person works nowhere else; otherwise it is 403 `shared_identity` and they
change their own via `PATCH /auth/me/`.

A business must always keep one active `OWNER`: removing or demoting the last
one returns 400.

Membership statuses are `ACTIVE`, `INVITED`, `SUSPENDED` and `LOCKED` (set by
failed sign-ins, cleared by `unlock`).

## Invitations

For staff who have their own email — including someone who already has an
account in another business.

| Method | Path | Capability | Notes |
|---|---|---|---|
| GET | `/invitations/` | `organization.read` | This business's invitations |
| POST | `/invitations/` | `users.manage` | `email`, `role`. Emails a link; re-inviting the same address revokes the previous link |
| DELETE | `/invitations/{id}/` | `users.manage` | Revokes it |
| POST | `/invitations/{id}/resend/` | `users.manage` | New token, new email; the old link dies |
| GET | `/auth/invitations/{token}/` | — | Public preview: business, role, and `account_exists` so the client knows whether to ask for a password |
| POST | `/auth/invitations/accept/` | — | `token`, `username`, plus `password` only if there is no account yet. Returns a session |

The token is stored **hashed** and expires in 7 days; the clear value exists
only in the email. Accepting an invitation for an address that already has an
account creates *only* the membership and never touches that password.

A pending invitation occupies a seat against the plan limit — otherwise
inviting ten people would walk straight past it.

## Errors

One envelope for everything:

```json
{"detail": "Only 7 unit(s) available for Nike Air Max / 38 / Black.",
 "code": "insufficient_stock",
 "context": {"variant": "...", "available": 7, "requested": 99}}
```

| Code | HTTP | Meaning |
|---|---|---|
| `insufficient_stock` | 409 | Would oversell. Online operations refuse |
| `price_mismatch` | 409 | `expected_total` disagrees with the server total |
| `payment_mismatch` | 400 | Payments do not cover the total, or a non-cash overpayment |
| `idempotency_key_required` | 400 | Missing header on a sale or refund |
| `idempotency_conflict` | 409 | Key reused with a different payload |
| `operation_in_progress` | 409 | Identical request still running; retry |
| `invalid_operation` | 400 | Business rule violated |
| `plan_limit_exceeded` | 402 | Subscription plan limit reached |
| `invalid_credentials` | 401 | Login: wrong slug, username, email or secret — deliberately indistinguishable |
| `invalid_invitation` | 400/404 | Invitation token expired, revoked or already used |
| `already_member` | 409 | That person already works in this business |
| `shared_identity` | 403 | Personal data of someone who also works elsewhere |
| `username_taken` | 409 | That username is already used in this business |
| `subscription_inactive` | 402 | Subscription cancelled or expired — writes blocked, reads still work |

Another tenant's row is **404, never 403** — confirming existence would leak it.

## Idempotency

Send `Idempotency-Key: <uuid>` on critical writes. Same key + same payload
replays the stored response (`Idempotent-Replay: true`); same key + different
payload is 409. A failed operation releases its key so a legitimate retry can
succeed.

**Mandatory** on `POST /sales/` and `POST /refunds/` — money must not move
twice because a till lost its connection. Optional elsewhere.

## Rate limits

| Scope | Limit | Applies to |
|---|---|---|
| `register` | 5/hour | `POST /auth/register/` |
| `auth` | 10/min | login, refresh, select organization, invitation preview and accept |
| `write` | 120/min | `/sales/`, `/refunds/` |
| `sync` | 60/min | `/sync/operations/`, `/sync/pull/` |

## Endpoints

### Health
`GET /api/v1/health/` — unauthenticated. Reports database and cache
reachability; `200` healthy, `503` degraded. It leaks nothing about the
business.

### Organization
| Method | Path | Capability |
|---|---|---|
| GET | `/organization/` | `organization.read` |
| PATCH | `/organization/settings/` | `organization.manage` |
| CRUD | `/locations/` | read `organization.read` · write `organization.manage` |

`PATCH /organization/settings/` is where the whole business configuration is
edited — including tax. `charges_tax` (bool) says whether the business sells
with IVA and `tax_rate` (0–100) is the rate; every sale is computed from those
two and from nothing else. `tax_regime` is the DIAN label printed on receipts.
The same payload carries the fiscal contact (`phone`, `address`, `city`), the
DIAN resolution fields and the receipt settings (`receipt_footer`,
`receipt_paper_width`), so no client needs to keep a local copy of any of it.
The full object also rides along on every session response, so a terminal has
the current configuration the moment it logs in.

There is no `/organizations/` collection under this prefix: `/organization/` is
always the one the session token names. The businesses *you* belong to are at
`/auth/organizations/`, which lists memberships, not organizations — there is
no id to guess because you only ever see your own.

Staff live under `/employees/` and `/invitations/` — see
[Employees](#employees) above.

### Catalog
| Method | Path | Capability |
|---|---|---|
| CRUD | `/categories/` `/brands/` `/products/` `/variants/` | read `products.read` · write `products.write` |
| POST/DELETE | `/products/{id}/photo/` | `products.write` — the product's one photo |
| GET | `/variants/lookup/?barcode=` | `products.read` — POS scan; falls back to exact SKU |

`POST /products/` accepts nested variants:

```json
{"name": "Nike Air Max",
 "variants": [
   {"sku": "NAM-38-BLK", "barcode": "7701234567", "size": "38", "color": "Black", "price": "459900.00"},
   {"sku": "NAM-39-BLK", "barcode": "7701234568", "size": "39", "color": "Black", "price": "459900.00"}
 ]}
```

`PATCH` upserts: variants with an `id` are updated, new ones created, none are
deleted. `DELETE` deactivates the product and its variants.

Filters: `?category=` `?brand=` `?is_active=` `?size=` `?color=` `?search=`.

**Photo.** One per product, not a gallery — a small shop photographs a garment
once. It never travels in the plain JSON body; upload or replace it with
`multipart/form-data`:

```
POST /api/v1/products/{id}/photo/
Content-Type: multipart/form-data

image: <file>
```

JPEG/PNG/WEBP only, 4MB max (a clean 400 either way — never Django's generic
"request too large"). Re-uploading deletes the previous file from disk before
storing the new one. `DELETE /products/{id}/photo/` removes it. `image` on the
product comes back as `null` until one is uploaded, and as a full URL once it
exists — read straight off `GET`/`POST`/`PATCH /products/`, no extra call
needed. Files are served straight off local disk for now (`MEDIA_URL`), which
will move behind object storage before this scales past one node.

### Inventory
| Method | Path | Capability |
|---|---|---|
| GET | `/inventory/stock/` | `inventory.read` |
| PATCH | `/inventory/stock/{id}/` | `inventory.adjust` — **only `reorder_point`** |
| GET | `/inventory/movements/` | `inventory.read` — the ledger, read-only by design |
| POST | `/inventory/initial-stock/` | `inventory.adjust` — positive quantities only |
| POST | `/inventory/adjustments/` | `inventory.adjust` — signed quantities |
| GET/PATCH | `/inventory/discrepancies/` | `inventory.read` / `inventory.adjust` |

Stock quantity is never writable. It changes only through movements — a PATCH
on quantity would reintroduce the untraceable `stock -= n` this design removes.

```http
POST /api/v1/inventory/adjustments/
Idempotency-Key: 8f14e45f-ceea-467a-9c1e-1b2c3d4e5f60

{"reason": "Merma por daño",
 "lines": [{"variant": "<uuid>", "quantity": -3, "note": "Caja mojada"}]}
```

`location` is optional; omitted, it resolves to the organization's default.
Returns the created movements (201).

Filters: `?location=` `?variant=` `?product=` `?movement_type=`
`?occurred_after=` `?occurred_before=` `?in_stock=` `?below_reorder_point=`.

**Deleting a product leaves nothing to sell but everything to audit.** The
product is deactivated, its stock is adjusted down to zero, and its balance row
stays in the database because `recalculate_stock` rebuilds balances from the
ledger and needs to find it. That row is hidden from `/inventory/stock/` — a
deleted product should not keep appearing in the stock list at zero — and comes
back with `?include_inactive=true` or `?is_active=false`.

The movements themselves are never hidden and never deleted: `/inventory/movements/`
still shows the original entry and the adjustment that zeroed it. That is the
history, and it is what makes "where did these units come from?" answerable
after the product is gone.

### Customers
| Method | Path | Capability |
|---|---|---|
| CRUD | `/customers/` | read `customers.read` · write `customers.write` |
| GET | `/customers/{id}/history/` | `customers.read` — their sales, newest first |

List responses annotate `total_purchases` and `total_spent`, derived from
sales rather than stored.

### Purchasing
| Method | Path | Capability |
|---|---|---|
| CRUD | `/suppliers/` | read `suppliers.read` · write `suppliers.write` |
| GET/POST | `/purchases/` | read `purchases.read` · write `purchases.create` |
| POST | `/purchases/{id}/receive/` | `purchases.create` |
| POST | `/purchases/{id}/cancel/` | `purchases.create` — drafts only |

```json
POST /api/v1/purchases/
{"supplier": "<uuid>", "supplier_invoice": "FV-9001", "receive": true,
 "items": [{"variant": "<uuid>", "quantity": 12, "unit_cost": "60000.00"}]}
```
`supplier` is optional — clients that don't track suppliers may omit it.

`receive: true` creates and receives in one call — the normal flow for a small
store. Receiving writes `PURCHASE` movements and moves the average cost.
Purchases are never edited: a received purchase is corrected with an inventory
adjustment.

### Sales
| Method | Path | Capability |
|---|---|---|
| GET | `/sales/` `/sales/{id}/` | `sales.read` |
| POST | `/sales/` | `sales.create` — **`Idempotency-Key` required** |
| POST | `/sales/{id}/cancel/` | `sales.cancel` |
| GET | `/sales/{id}/receipt/` | `sales.read` — sale plus tax broken out per rate |

```http
POST /api/v1/sales/
Idempotency-Key: 8f14e45f-ceea-467a-9c1e-1b2c3d4e5f60

{"customer": "<uuid|null>",
 "cash_register": "<uuid|null>",
 "expected_total": "238000.00",
 "lines": [{"variant": "<uuid>", "quantity": 2, "discount_amount": "0"}],
 "payments": [{"method": "CASH", "amount": "250000.00"}]}
```

- Every total is recomputed server-side. Whatever the client sends as
  `total`/`subtotal` is ignored.
- `expected_total` is optional; if it disagrees with the server, the sale is
  rejected (409 `price_mismatch`) instead of selling at a stale price.
- `unit_price` may be sent per line to override the shelf price.
- Discounts are **per line**. A whole-sale discount is applied by the client as
  line discounts, so the tax split stays exact.
- Payments must cover the total. Overpayment is only allowed against cash and
  produces `change_amount`.
- `cash_register` is required on a cash payment **while the organization has
  an open cash session anywhere** — omitting it there is refused (409
  `invalid_operation`) instead of completing outside every drawer. With no
  open session, it's optional and the sale simply gets no drawer movement.
  A default register is created automatically with the organization
  (`GET /cash/registers/` to find it) — see `ARCHITECTURE.md` D11.
- `id` may be sent by an offline terminal to keep the id it already printed.

There is no `PATCH /sales/{id}/`. A completed sale is corrected with a refund
or a cancellation, both of which leave a trail.

### Refunds
| Method | Path | Capability |
|---|---|---|
| GET | `/refunds/` `/refunds/{id}/` | `sales.read` |
| POST | `/refunds/` | `sales.refund` — **`Idempotency-Key` required** |

```http
POST /api/v1/refunds/
Idempotency-Key: <uuid>

{"sale": "<uuid>", "method": "CASH", "restock": true,
 "cash_register": "<uuid|null>", "reason": "Talla equivocada",
 "lines": [{"sale_item": "<uuid>", "quantity": 1}]}
```

Never more than `refundable_quantity` on each line, across successive refunds.
`restock: false` pays the customer without crediting stock. The sale becomes
`PARTIALLY_REFUNDED`, or `REFUNDED` once nothing remains refundable.
`cash_register` follows the same rule as on a sale: required for a `CASH`
refund while any session is open in the organization.

### Cash
| Method | Path | Capability |
|---|---|---|
| CRUD | `/cash/registers/` | read `cash.read` · write `organization.manage` |
| GET | `/cash/sessions/` `/cash/sessions/{id}/` | `cash.read` |
| POST | `/cash/sessions/` | `cash.open` — opens a shift |
| POST | `/cash/sessions/{id}/close/` | `cash.close` — arqueo |
| GET | `/cash/sessions/{id}/summary/` | `cash.read` |
| GET/POST | `/cash/sessions/{id}/movements/` | `cash.movement` |
| GET | `/cash/movements/` | `cash.read` |

```json
POST /api/v1/cash/sessions/        {"register": "<uuid>", "opening_amount": "100000.00"}
POST /api/v1/cash/sessions/{id}/close/  {"counted_amount": "95000.00", "notes": "Faltante"}
POST /api/v1/cash/sessions/{id}/movements/  {"movement_type": "WITHDRAWAL", "amount": "30000.00"}
```

Amounts on movements are always positive; `movement_type` gives the direction.
Closing returns `expected_amount`, `counted_amount` and `difference`
(positive = surplus). `summary/` adds totals by movement type and by payment
method, so card and transfer are visible without polluting the drawer.

### Expenses
Operating spend only — rent, payroll, utilities, the delivery paid out of the
drawer. Merchandise never comes through here: it enters as a `Purchase` and is
counted as cost of goods sold when it sells, so nothing is counted twice.

| Method | Path | Capability |
|---|---|---|
| CRUD | `/expense-categories/` | read `expenses.read` · write `expenses.write` |
| GET | `/expenses/` `/expenses/{id}/` | `expenses.read` |
| POST | `/expenses/` | `expenses.write` |
| PATCH | `/expenses/{id}/` | `expenses.write` — category, description, reference, note, date |
| DELETE | `/expenses/{id}/` | `expenses.write` — only while its shift is open |

```json
POST /api/v1/expenses/
{"category": "<uuid>", "description": "Domicilio de la tarde",
 "amount": "20000.00", "payment_method": "CASH"}
```

**A cash expense leaves the drawer.** When `payment_method` is `CASH` and a
register is open at that location, the expense writes a `WITHDRAWAL` into the
cash ledger and comes back with `cash_session` set and `paid_from_drawer: true`
— the arqueo then balances on its own. With no open register it is still
recorded (paid from a pocket or the safe) and `paid_from_drawer` is `false`.
With **two** registers open at one location the request is rejected: pass
`cash_session` to say which drawer the money came out of.

Because the money already moved, `amount` and `payment_method` are immutable.
A mistyped expense is deleted while its shift is still open; once the arqueo is
closed, deletion returns 400 and the correction is a cash adjustment.

A business is provisioned with nine editable default categories, so an expense
can be recorded before anything is configured. Businesses created before this
module existed start with none and define their own.

### Synchronization
| Method | Path | Capability |
|---|---|---|
| CRUD | `/sync/devices/` | read `organization.read` · write `organization.manage` |
| GET | `/sync/operations/` | `inventory.read` |
| POST | `/sync/operations/` | `sync.push` |
| GET | `/sync/pull/` | `products.read` |

Registering a device is idempotent by `identifier`: `201` the first time, `200`
on re-registration, and the same device id either way.

```http
POST /api/v1/sync/operations/

{"device": "<uuid>",
 "operations": [
   {"operation_id": "<uuid generated by the terminal>",
    "operation_type": "SALE_CREATE",
    "occurred_at": "2026-08-20T15:00:00Z",
    "payload": {"lines": [...], "payments": [...]}}
 ]}
```

```json
{"summary": {"accepted": 1, "duplicated": 0, "failed": 0},
 "results": [{"operation_id": "...", "status": "PROCESSED", "duplicate": false,
              "result": {"sale_id": "...", "number": "V-000012", "total": "238000.00"},
              "error_code": null, "detail": null}]}
```

- **No `Idempotency-Key` needed.** Every operation carries its own
  `operation_id`, so resending the whole batch is safe. `duplicate: true` means
  it was already applied and nothing happened.
- Each operation is processed in its own transaction: one bad operation does not
  sink the batch.
- A rejected operation is stored as `FAILED` with its `error_code`. Resending it
  returns the stored failure — fix the payload, do not retry harder.
- Supported types: `SALE_CREATE`, `SALE_CANCEL`, `REFUND_CREATE`. Opening and
  closing a cash session are online-only.
- At most 200 operations per push.
- Replayed sales are accepted even without stock (decision D4): stock may go
  negative and a discrepancy is opened. The online path still refuses.

```http
GET /api/v1/sync/pull/?since=2026-08-20T14:00:00Z&location=<uuid>
```

Returns `cursor`, `since`, and the categories, brands, products, variants,
customers and stock levels changed since the cursor. Nothing is ever deleted —
deactivated rows arrive with `is_active: false`, so there are no tombstones to
reconcile.

### Reports
| Method | Path | Capability |
|---|---|---|
| GET | `/reports/` | `reports.read` — index of available reports |
| GET | `/reports/dashboard/` | `reports.read` — the whole landing page in one payload |
| GET | `/reports/sales-summary/` | `reports.read` |
| GET | `/reports/top-products/` | `reports.read` |
| GET | `/reports/margin/` | `reports.read` — gross, before expenses |
| GET | `/reports/expenses/` | `reports.read` — operating spend by category |
| GET | `/reports/profit/` | `reports.read` — revenue → cost → expenses → **net** |
| GET | `/reports/inventory-valuation/` | `reports.read` |
| GET | `/reports/cash-sessions/` | `reports.read` |
| GET | `/reports/refunds/` | `reports.read` |

All accept `?from=` `?to=` (ISO-8601, default the last 30 days) and
`?location=`. `top-products` also takes `?limit=` (default 10, max 100), and
`dashboard` takes `?top_limit=` (default 5, max 50).

`dashboard` composes `sales`, `profit`, `refunds`, a condensed `inventory`
block and `top_products` under one period, so the landing page is one request
instead of five that each resolve "now" a few milliseconds apart. Each block
keeps the exact shape of its own endpoint — drilling into the detail means no
second payload to learn.

`margin` is **gross** profit: revenue minus the cost of the goods. `profit`
continues the line down to `net_profit` by subtracting operating expenses, and
is the only report that answers "how much did I actually make". Label them
accordingly in the UI: showing gross profit as "ganancia" hides exactly the
costs that weigh most.

Margins use the cost frozen on each sale line, so a report about a past period
does not move when later purchases change the average cost. Units are net of
refunds.

### Subscription
| Method | Path | Capability |
|---|---|---|
| GET | `/plans/` | authenticated |
| GET | `/subscription/` | `subscription.read` |

A cancelled or expired subscription returns `402 subscription_inactive` on
writes. Reads keep working: a store that stops paying must still be able to get
its own data out. `TRIAL` and `PAST_DUE` write normally.

## Conventions

- Pagination: `?page=` `?page_size=` (default 50, max 200); responses carry
  `count` / `next` / `previous` / `results`.
- All ids are UUIDs, generated client-side where offline creation needs it.
- Money is a decimal string with 2 places. Prices are **tax-inclusive**.
- Quantities are integers.
- Timestamps are ISO-8601 UTC.

## Not implemented, deliberately

`PATCH /sales/{id}/` (sales are immutable), whole-sale discounts, layaway and
credit sales, partial purchase receipt, device-scoped tokens, PDF receipts.
See ARCHITECTURE.md §16.
