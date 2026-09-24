# Frontend Implementation Brief — Retail POS SaaS

This is the implementation brief for the frontend of the Retail POS SaaS
product. The UI/UX is already designed elsewhere — **this document says
nothing about screens, components, layout, or frontend stack.** It describes
the backend contract the frontend must implement against: what to call, in
what order, what can go wrong, and what the frontend is responsible for
deciding versus what the server always decides.

Treat this as the spec for wiring, not for pixels.

## Required reading before starting

- `API.md` — every endpoint, request/response shape, error codes, rate limits.
- `DOMAIN.md` — the entities and their invariants (what a sale, a refund, a
  cash session actually mean).
- `ARCHITECTURE.md` — why things are the way they are; read it before working
  around something that looks like friction, because it's usually deliberate.
- `http://<api-host>/api/v1/reference/` — live, interactive OpenAPI reference
  (self-hosted, works over LAN too) generated from the actual running schema.
  Treat it as more current than this document for field-level detail.
- The backend test suite (`backend/tests/`) is the executable spec for
  business rules. When this brief and a test disagree, the test is right.

## Non-goals of this document

Not covered here, on purpose: framework choice, component library, styling,
state management library, routing library, form library, build tooling. None
of that is this brief's concern.

---

## 1. Session and authentication

**One person, one account, possibly several businesses.** This is the shape the
UI has to carry: sign in as a person, then work inside a business. Build the
picker.

Two kinds of token come back from the API, and they are not interchangeable:

- **Identity token** — who you are. Good for listing your businesses, picking
  one, and creating a new one. Every business endpoint returns **403** with it.
- **Session token** — who you are *and* which business. Everything else needs
  this. Hold exactly one at a time and drop the identity token once you have it.

**The login form has one field more than you think it does — or two fewer:**

- **By email (the default on a browser):** email + password. If the person has
  one business, the response *is* a session and you go straight in. If they
  have several, you get an identity token plus `organizations` — show the
  picker, then `POST /auth/select-organization/ {"organization": "<slug>"}`.
- **By business and username:** business slug + username + password. An
  alternative path the API supports, but this client only implements the
  email form above — that's exactly why `POST /employees/` requires an email
  for every employee it creates.
- **On a registered terminal:** send the terminal's token as the
  `X-Device-Token` header and the slug field disappears — the till already
  knows its business, so the cashier types a username and a password, nothing
  else. The token comes back in the clear exactly once from
  `POST /sync/devices/`; store it and never show it again.

### What to build for the picker

- `GET /auth/organizations/` lists the businesses of whoever is signed in, most
  recently used first. Works with either token, so the **switcher** in the app
  chrome is the same call — switching business never asks for the password
  again, it just calls `select-organization` and replaces the session token.
- `POST /auth/organizations/new/` creates another business under the same
  account and returns its session. Put it at the bottom of the picker.
- With exactly one business, show no picker and no switcher. Most users are
  here; the UI should not make them pay for the ones who aren't.

### The session response

- `POST /auth/register/`, `POST /auth/login/`, `POST /auth/select-organization/`
  and `POST /auth/invitations/accept/` all return the same session shape:
  `access`, `refresh`, `scope: "session"`, `user`, `organization`,
  `membership`, `role`, `capabilities`, `default_location`. Store all of it —
  this single response renders the whole session with no follow-up call.
- An identity response is `access`, `refresh`, `scope: "identity"`, `user` and
  `organizations` (each row: `membership`, `organization`, `role`). **Branch on
  `scope`**, not on whether a field happens to be present.
- `GET /auth/me/` re-derives whichever shape matches the current token. Call it
  on boot to validate a stored token before trusting it.
- `PATCH /auth/me/` is where a person edits their own name, phone and email.
  Note that `role` and `capabilities` are *not* theirs to change: they belong
  to the membership.
- Access tokens last 60 minutes, refresh tokens 30 days, and refresh does
  **not** rotate (the same refresh token stays valid until it expires). The
  business claim survives the refresh, so a refreshed token keeps working in
  the same business. Build a refresh-on-401 strategy — don't force a re-login
  every hour.
- A suspended membership, a deleted membership or a deactivated business stops
  working immediately (mid-lifetime of the access token), independent of
  expiry: any authenticated call can start returning 401. Handle it the same
  way as an expired token — force reauthentication, don't treat it as a one-off
  error.
- Login failures always return the same 401 `invalid_credentials`, whether the
  slug, the username, the email or the secret was wrong. Show one message; do
  not try to tell the user which field was at fault. Five bad attempts lock
  that membership for 15 minutes, and only the owner can clear it.

### Two ways onto a team, and you need both

`Settings → Employees` lists **memberships**. The id in `/employees/{id}/` is a
membership id, not a person id.

- **Created directly** — a name, a username, an email, a role and a password,
  all set by the owner. No invitation, no pending state. The email is
  required: it's what the employee signs in with, since this client only
  offers the email + password form. `POST /employees/{id}/unlock/` clears a
  lockout from failed sign-ins.
- **Invited by email** — `POST /invitations/` sends an expiring link. Show
  pending invitations alongside the team with a way to revoke or resend. The
  invited person lands on a route of yours carrying the token: call
  `GET /auth/invitations/{token}/` to render who invited them and to what, and
  use `account_exists` to decide whether to ask for a new password or just a
  username. `POST /auth/invitations/accept/` returns a session — they are
  already signed in, so do not bounce them back to the login screen.

Editing an employee's personal fields can return **403 `shared_identity`**:
that person also works somewhere else, so only they can change their own name
or email. Show that as an explanation, not as an error the owner can retry.

### Authorization is a UI hint, not a guarantee

`capabilities` is a flat list of strings (`sales.create`, `inventory.adjust`,
`costs.read`, `cash.close`, …). Use it to decide what to show or enable. **Never treat its
absence as the only enforcement** — the server checks again on every write
and returns 403 regardless of what the frontend rendered. Design every write
flow to handle a 403 gracefully (it can legitimately happen: another admin
just changed this user's role in another tab).

Cashiers have `products.write` and `inventory.adjust` but **not** `costs.read`:
hide cost columns / wizard cost fields and never send `unit_cost`. Owners with
`costs.read` see a pending-cost banner (`GET /variants/pending-cost/`) and can
complete costs with `POST /variants/{id}/set-cost/`.

A request touching another tenant's resource returns **404, not 403** — the
API deliberately does not distinguish "doesn't exist" from "exists but not
yours." Do not build error handling that assumes a 403 confirms something
exists.

---

## 2. The error contract

Every non-2xx response (barring plain validation 400s from field errors) uses
one envelope:

```json
{"detail": "human-readable message", "code": "machine_code", "context": {}}
```

Build error handling around `code`, not around parsing `detail` text (that
string is for display, not for branching logic). Codes the frontend must
handle specifically, not just show generically:

| Code | HTTP | Frontend must |
|---|---|---|
| `insufficient_stock` | 409 | Show current availability, block the sale/adjustment from completing |
| `price_mismatch` | 409 | The catalogue changed since the cart was built — refresh prices, ask the user to confirm before resubmitting |
| `payment_mismatch` | 400 | Payments don't cover the total, or a non-cash overpayment — surface which |
| `idempotency_key_required` | 400 | Only happens if the frontend forgot to send the header — treat as a bug, not a user-facing error |
| `idempotency_conflict` | 409 | The same key was reused with a different payload — a client bug (a stale key was reused); regenerate the key |
| `operation_in_progress` | 409 | An identical request is still being processed — safe to retry shortly with the *same* key |
| `subscription_inactive` | 403 | The business is locked out entirely (reads and writes) until a platform operator records a payment — sign the user out and explain why |
| `plan_limit_exceeded` | 402 | Block the create, point at plan upgrade — `context` carries the resource and the limit |
| `invalid_credentials` | 401 | Only from login. Wrong slug, wrong username or wrong secret — deliberately indistinguishable |
| `username_taken` | 409 | Only from creating an employee: that username is already used *in this business* |

For everything else, `detail` is display-safe and `context` may carry
structured extras (e.g. `insufficient_stock` includes `available` and
`requested`).

---

## 3. Idempotency (mandatory on two endpoints, safe practice elsewhere)

`POST /sales/` and `POST /refunds/` **require** an `Idempotency-Key` header
(a client-generated UUID) — the request is rejected without one. This exists
because a network retry after a timeout must not charge or refund twice.

Rules the frontend must follow:

- Generate a fresh UUID **per logical operation**, not per HTTP request. A
  retry of the *same* sale attempt reuses the *same* key; a genuinely new
  sale gets a new key.
- On a network error or timeout, retry with the same key — the server either
  replays the original result or tells you it's still processing
  (`operation_in_progress`). Never regenerate the key just because the first
  attempt failed to get a response.
- Do regenerate the key if the user actually changes the request (edits the
  cart, changes the payment amount) before resubmitting — same key with a
  different payload is a hard error (`idempotency_conflict`), not a merge.
- Every other write endpoint accepts the same header optionally. Using it for
  purchases and cash movements too is good practice, not required.

---

## 4. What the server always computes — never trust a client-side total

Sale and refund totals, tax breakdown, and change are **always recomputed
server-side**, regardless of what the frontend sends. The frontend may
compute and display a running total for the cart (users need to see a total
before submitting), but must:

- Optionally send `expected_total` with the sale — if it disagrees with the
  server's own calculation, the sale is rejected (409 `price_mismatch`)
  instead of silently completing at a different price. Use this to catch a
  stale local price cache before money changes hands.
- Never construct a receipt, a margin figure, or a tax breakdown from
  client-side arithmetic for anything persisted or printed — always render
  the server's response fields.
- Treat `unit_price` as overridable per line (the till may discount at the
  register), but `tax_rate`, `taxable_base`, `tax_amount`, and `unit_cost` on
  a `SaleItem` are the server's numbers, frozen at the moment of sale — never
  recompute them client-side after the fact (e.g., in a receipt reprint).

Prices are **tax-inclusive** (Colombian retail convention) — what's shown on
the shelf is what's charged; the tax breakdown is informational, not
additive.

Tax comes from the business, never from the product: read `charges_tax` and
`tax_rate` off the organization on the session. When `charges_tax` is false,
show no tax line at all rather than an IVA of zero. A cart preview may compute
the split locally for display, but the persisted numbers are always the
server's — and on a reprint, use the `tax_breakdown` the sale carries, not the
business's current rate, or an old sale will be shown with today's IVA.

---

## 5. Domain flows to implement

Each of these corresponds to one or more endpoints in `API.md`. Ordering and
preconditions matter — get them wrong and the API will reject the call with
one of the codes in §2, not silently do the wrong thing.

### 5.1 Onboarding
Register creates the account, the organization, a default location, a default
cash register, and a trial subscription in one call — nothing further to
provision before the user can start working, and it returns a session, so go
straight into the app rather than back to the login screen. Opening a *second*
business later (`POST /auth/organizations/new/`) does exactly the same thing
and returns that business's session. `GET /cash/registers/` after
signup returns that register (`code: "PRINCIPAL"`) if the UI needs its id
before the user has created any of their own.

### 5.2 Catalog
Products carry nested variants on create (`POST /products/`) and support
upsert on update (`PATCH` — variants with an `id` update in place, variants
without one are created, none are ever deleted; deactivate via `is_active`
instead). Barcode/SKU scanning during a sale should hit
`GET /variants/lookup/?barcode=` before falling back to a manual search.

### 5.3 Inventory
Stock quantity is **read-only** everywhere except through the two write
endpoints (`/inventory/initial-stock/`, `/inventory/adjustments/`) — there is
no "edit stock count" field anywhere, by design. The movement ledger
(`/inventory/movements/`) is append-only and read-only from the frontend's
perspective; it exists for audit views, not for direct manipulation.
`StockDiscrepancy` records need a review/resolve flow somewhere in the app —
they only appear when an offline sale oversold (see §5.7).

### 5.4 Purchasing
A purchase is `DRAFT` until received; receiving (`POST
/purchases/{id}/receive/`, or `receive: true` on create) is what actually
moves stock and updates cost. A received purchase cannot be edited or
cancelled — the frontend must not offer those actions once `status ===
"RECEIVED"`.

### 5.5 Point of sale
1. Build the cart from variant lookups; compute a running total client-side
   for display only.
2. **Every cash sale must send `cash_register`.** This is not optional in
   practice: if the organization has any open cash session and a cash payment
   arrives with `cash_register` omitted, the sale is refused (400
   `invalid_operation`) rather than completed outside every drawer — money
   that should have shown up in the day's arqueo would otherwise vanish
   silently. Resolve which register the current terminal/till is using
   *before* checkout (a setting, a login-time pick, or the org's default
   register from `GET /cash/registers/`), not as an afterthought at submit
   time. A register with no open session is a distinct, expected error
   (§5.6) — surface it as "open the register first," not as a generic
   failure.
3. Submit with a fresh `Idempotency-Key` and, if available, `expected_total`.
4. On success, the response has everything needed for a receipt; on
   `price_mismatch`, refresh prices and let the user re-confirm before
   resubmitting with a *new* key.
5. A completed sale is corrected only via `POST /sales/{id}/cancel/` (full
   void, only before any refund exists) or a refund (§5.6) — there is no
   sale-edit flow to build.

### 5.6 Cash
A default register (`code: "PRINCIPAL"`) already exists from signup (§5.1); a
store may add more. A register needs an open session before it can take cash.
The frontend needs: opening a session (float amount), taking
withdrawals/deposits during the shift, and a closing/arqueo screen that shows
`expected_amount` (system) next to a `counted_amount` field (cashier input)
and displays the resulting `difference` — this is a reconciliation view, not a
free-form form. Card and transfer payments never appear as drawer movements;
show them from `/cash/sessions/{id}/summary/`'s `payments_by_method` instead,
or the arqueo screen will look wrong to the cashier by design (this is correct
behavior — see `ARCHITECTURE.md` §6, not a bug to route around).

The reverse of §5.5's rule: the frontend is responsible for knowing, before
opening a checkout screen, which register (if any) the current session is
open on — don't discover it from a submit-time error. `GET
/cash/sessions/?status=OPEN` is how to find the register(s) currently in use.

### 5.7 Refunds
Refund lines are picked per `SaleItem`, capped by `refundable_quantity`
(`quantity - refunded_quantity`, already computed server-side per item) —
the frontend should disable/cap the quantity input at that number rather than
relying solely on the server rejection, but must still handle the rejection
(over-refund across two concurrent attempts is possible and correctly
blocked server-side). `restock: false` exists for damaged goods — expose it
as an explicit choice, not a default.

### 5.8 Customers
Deliberately thin — no CRM flow to build beyond contact fields and a purchase
history view (`/customers/{id}/history/`, itself just a list of that
customer's sales).

### 5.9 Reports
Six read-only aggregates, each accepting `from`/`to`/`location` query params
with sane server-side defaults (last 30 days) — no report-builder UI is
implied, these are fixed shapes. Margin and top-products figures are
historical (computed from cost frozen at sale time) — don't recompute them
from current catalogue prices/costs anywhere in the frontend.

### 5.10 Offline sync (only if the frontend targets an offline-capable POS terminal)
This is a distinct, harder mode — implement it only if the actual product
requirement includes a terminal that must keep selling without connectivity.
If so:

- Register the terminal once (`POST /sync/devices/`, idempotent by
  `identifier` — safe to call on every app boot).
- While offline, queue sale/cancel/refund operations locally, each with a
  client-generated `operation_id` (UUID) and, for a sale, a client-generated
  sale `id` (also a UUID) so the receipt the terminal prints locally matches
  what the server eventually records.
- On reconnect, push the queue as a batch to `POST /sync/operations/`. The
  response has one result per operation (`PROCESSED`, `FAILED`, or
  `duplicate: true`) — process the array, don't assume batch-level
  success/failure. A `FAILED` operation with an `error_code` needs to surface
  to a human; do not silently retry it forever (the server already recorded
  it as failed and will keep returning the same stored failure).
- Offline sales are accepted even without server-visible stock — pulling
  fresh stock levels via `GET /sync/pull/?since=<cursor>` after a sync is how
  the terminal catches up, and negative stock it might reveal is expected,
  not an error state to block on.
- Use the returned `cursor` for the next pull's `since` — don't track time
  locally for this.

---

## 6. Data conventions

- **Money**: decimal strings with 2 places from the API (`"119000.00"`), even
  though the configured currency (COP) has 0 decimal places in practice —
  round for display, but send back what the API gave you, not a
  client-rounded value, on anything that flows back into a request.
- **Quantities**: always integers.
- **IDs**: UUIDs everywhere, as strings.
- **Timestamps**: ISO-8601 UTC — convert to local time for display, but treat
  `occurred_at` (when it happened in the store) as distinct from `created_at`
  (when the server recorded it) where both appear; they can differ, notably
  for anything synced from offline.
- **Pagination**: `?page=`/`?page_size=` (default 50, max 200), response has
  `count`/`next`/`previous`/`results`. Every list endpoint uses this shape.
- **Filtering**: each list endpoint documents its own query params in
  `API.md` / the live reference — there's no universal filter syntax, check
  per endpoint.

---

## 7. Explicitly not available — don't build a UI expecting these

- Editing a completed sale (`PATCH /sales/{id}/` doesn't exist).
- A whole-sale discount endpoint — only per-line discounts exist; a "discount
  the whole cart" UI must compute and send it as line-level amounts.
- Partial purchase receipt — a purchase is received in full or not at all.
- Any payment gateway integration — subscriptions exist but billing isn't
  wired to a provider yet; don't build a checkout/payment-method flow against
  this backend today.
- Device-scoped credentials — an offline terminal currently pushes under the
  token of whichever user registered it, not its own credential.
- Layaway, credit sales, or store credit — payments must cover the sale total
  in the same request; there's no partial-payment-over-time model.
