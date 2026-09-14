# Retail POS SaaS — Backend

Multi-tenant backend for fashion retail (clothing, footwear, accessories).
One codebase, one database, many businesses. Built for the founding customer,
designed so the second and the hundredth need no rewrite.

- **Stack:** Python 3.12 · Django 5.2 · DRF · PostgreSQL 16 · Redis
- **Docs:** [ARCHITECTURE.md](ARCHITECTURE.md) · [DOMAIN.md](DOMAIN.md) · [API.md](API.md)

## Status

| Phase | Scope | State |
|---|---|---|
| 1 | Tenancy, auth, roles, catalog, ledger inventory, subscriptions, audit, idempotency | **done** |
| 2 | Purchasing, Sales/POS, payments, customers, cash, refunds | **done** |
| 3 | Offline sync, reporting, hardening | **done** — 191 tests green |

## Running it (Docker — first time)

Requirements: [Docker Engine](https://docs.docker.com/engine/install/) with
the Compose plugin (`docker compose version` should print something). Nothing
else — Python, Postgres and Redis all run inside containers.

```bash
git clone https://github.com/IBER-DEV/POS-SYSTEM.git
cd POS-SYSTEM

# Optional: only needed if you want to change the host-side ports (see
# .env.example) — the defaults below work as-is with no .env file at all.
cp .env.example .env

docker compose up -d --build
```

This builds the `api` image and starts three services: `db` (Postgres 16),
`redis` and `api` (Django). The `api` container's
[entrypoint](backend/entrypoint.sh) runs migrations, seeds the subscription
plan catalogue and collects static files automatically **on every start** —
there is no manual `migrate` step to run.

Check it came up clean:

```bash
docker compose ps                 # all three should be Up (db/redis "healthy")
docker compose logs -f api        # watch it apply migrations, then "Starting development server..."
curl http://localhost:8000/api/v1/health/
```

- API: <http://localhost:8000/api/v1/>
- Health: <http://localhost:8000/api/v1/health/>
- Swagger UI: <http://localhost:8000/api/v1/docs/>
- API Reference (Scalar, self-hosted — see below): <http://localhost:8000/api/v1/reference/>
- OpenAPI schema: <http://localhost:8000/api/v1/schema/>

To stop everything: `docker compose down` (add `-v` only if you also want to
wipe the Postgres volume — see "Resetting the database" below).

### Everyday commands (Docker)

```bash
docker compose up -d              # start again (no --build needed unless requirements.txt changed)
docker compose logs -f api        # tail the Django dev server
docker compose exec api python manage.py migrate        # normally automatic, but safe to re-run
docker compose exec api python manage.py createsuperuser --email admin@example.com
# The api container exports DJANGO_SETTINGS_MODULE=development, which
# overrides pytest.ini's `test` settings (rate limiting stays on and can
# flake auth-heavy tests under load) — force it explicitly:
docker compose exec -e DJANGO_SETTINGS_MODULE=config.settings.test api pytest
docker compose exec -e DJANGO_SETTINGS_MODULE=config.settings.test api pytest -m "not slow"
docker compose restart api        # apply a code/dependency change without a full rebuild
```

### Resetting the database

To wipe all data and start over (keeps migrations, re-seeds plans):

```bash
docker compose exec api python manage.py flush --no-input
docker compose exec api python manage.py seed_plans
```

To also throw away the Postgres volume itself (fully from scratch):

```bash
docker compose down -v
docker compose up -d --build
```

### Running the backend without Docker

Only needed if you specifically want a local Python environment instead of
containers:

```bash
docker compose up -d db redis     # still need Postgres + Redis somewhere
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
cp .env.example .env              # already points at the compose ports
.venv/bin/python manage.py migrate
.venv/bin/python manage.py runserver
```

### API Reference (Scalar)

`/api/v1/reference/` renders the same OpenAPI schema through
[Scalar](https://github.com/scalar/scalar). The JS bundle is **vendored**
at [apps/core/static/core/vendor/scalar-api-reference.standalone.js](backend/apps/core/static/core/vendor/scalar-api-reference.standalone.js)
rather than loaded from a CDN `<script>` tag — a CDN outage must not take the
API docs down. The only external dependency left in the bundle is a handful of
webfonts from `fonts.scalar.com`; if that host is unreachable the page still
renders and works, just with the browser's fallback font.

There is also `scalar.config.json` at the repo root, for the separate
[Scalar Docs](https://docs.scalar.com) product (`npx @scalar/cli project
preview`) — a hosted, multi-page docs site (this README, ARCHITECTURE.md,
DOMAIN.md, API.md, plus the same OpenAPI schema) with its own build/publish
flow. `/api/v1/reference/` needs nothing but this Django app running; the
`scalar.config.json` flow needs Node and, for anything beyond `/`, either a
real browser (client-side routing) or `npx @scalar/cli project publish`.

#### Why the MCP button (and "Ask AI") only showed up on localhost

Scalar's developer-tools bar - the "Connect via MCP" button, "Ask AI" - is
gated by `showDeveloperTools`, which **defaults to a literal string check on
`window.location.hostname === "localhost"`**, baked into the bundle. Not
HTTPS, not a secure-context check - just that one hostname. Reached by a LAN
IP, a custom domain, or even `127.0.0.1`, the check fails and the whole bar
disappears with no error.

The fix is in [api_reference.html](backend/apps/core/templates/core/api_reference.html):
`showDeveloperTools: 'always'` in the `Scalar.createApiReference()` call. With
that, the bar shows up over plain HTTP too - `http://10.1.130.244:8010/api/v1/reference/`
works today with no proxy involved. (The `scalar.config.json` preview, being a
separate bundle we don't control, still only shows it on literal `localhost`.)

#### Optional: local HTTPS for the LAN address

Not needed for the MCP button anymore, but still useful if you ever want the
API reference to look and behave (redirects, mixed-content rules, service
workers) like it would in production. An optional `caddy` service (see
[Caddyfile](Caddyfile)) puts local HTTPS in front of the API:

```bash
docker compose --profile https up -d
```

- <https://localhost:8443/api/v1/reference/>
- <https://10.1.130.244:8443/api/v1/reference/> (replace with your machine's
  LAN IP — `hostname -I` or `ip a`; if it's not `10.1.130.244` or
  `10.1.134.14`, add it to the site address list in `Caddyfile` and restart
  the service)

It's a self-signed certificate from Caddy's local CA, so the browser shows a
"not private" warning the first time — clicking through is enough to make the
page a secure context (that's all `https:` requires; trusting the CA is only
about removing the warning, not about the security guarantee). To remove the
warning too, trust Caddy's root CA once:

```bash
docker compose --profile https cp caddy:/data/caddy/pki/authorities/local/root.crt ./caddy-root-ca.crt
# Linux (Chrome/Chromium via NSS):
certutil -d sql:$HOME/.pki/nssdb -A -t "C,," -n "Caddy Local Authority" -i ./caddy-root-ca.crt
# Firefox: Settings → Privacy & Security → Certificates → View Certificates → Import
```

The `api` service works the same with or without this profile — HTTPS is
opt-in, only needed for the MCP button over a LAN address.

### Creating the first business

Self-serve signup (`POST /auth/register/`) is **disabled**. Platform operators
provision tenants from InventorySas → **Acceso operadores**, or via:

```bash
docker compose exec api python manage.py createsuperuser --email ops@example.com
# Then open the frontend, “Acceso operadores”, and create the business there.
```

API surface: `/api/v1/platform/` (staff JWT, `scope: platform`).

Previously (legacy, disabled):

```bash
curl -X POST http://localhost:8000/api/v1/auth/register/ \
  -H 'Content-Type: application/json' \
  -d '{"email":"ana@modaurbana.co","password":"UnaClaveMuySegura9",
       "organization_name":"Moda Urbana","tax_id":"900123456-7"}'
```

The response carries `access` / `refresh` tokens plus the organization,
membership, role and capability list. Signup provisions the account, the
organization, a default location, a default cash register and a trial
subscription in a single transaction.

**One account, many businesses.** The person is identified globally by email;
who they are *inside* a business — username, role — is the membership. So
two shops may each have their own `jperez`, and the same owner can open a
second business (`POST /auth/organizations/new/`) without a second account.
Signing in with just the email returns the list of businesses; picking one
(`POST /auth/select-organization/`) mints the token that actually opens it, and
that claim is re-checked against an active membership on every request.

Staff join one of two ways. Directly, `POST /employees/` — a name, a username,
an email, a role and a password set on the spot by the owner. Remotely,
`POST /invitations/` emails an expiring link; if that address already has an
account, accepting only adds the membership.

## Commands worth knowing

Prefix each with `docker compose exec api` when running against the Docker
setup (e.g. `docker compose exec api python manage.py recalculate_stock`); run
bare from `backend/` with the venv active otherwise.

```bash
manage.py recalculate_stock          # rebuild stock from the ledger; also a drift detector
manage.py recalculate_stock --organization <slug|uuid>
manage.py spectacular --file schema.yml
ruff check .
pytest                               # business-rule suite (~12s)
pytest -m "not slow"                 # skip the thread-based concurrency tests
manage.py seed_plans                 # restore the plan catalogue
python scripts/smoke_phase1.py       # phase-1 wiring check against a running server
```

## Repository layout

```
backend/
├── config/            settings (base/development/production/test), urls, wsgi
└── apps/
    ├── core/          tenancy, idempotency, audit, capabilities, base classes
    ├── accounts/      User (owned by one tenant), auth + employee endpoints
    ├── organizations/ Organization (the tenant), Location
    ├── subscriptions/ Plan, Subscription, plan limits
    ├── catalog/       Category, Brand, Product, ProductVariant
    ├── inventory/     InventoryMovement (ledger), StockLevel, StockDiscrepancy
    ├── customers/     Customer
    ├── purchasing/    Supplier, Purchase, PurchaseItem
    ├── cash/          CashRegister, CashSession, CashMovement
    ├── expenses/      ExpenseCategory, Expense — operating spend, not merchandise
    ├── sales/         Sale, SaleItem, Payment, Refund, RefundItem
    ├── synchronization/  Device, SyncOperation — offline replay
    └── reporting/     read-only aggregates, no models of its own
```

`apps/core` is where tenant isolation lives. Read
[ARCHITECTURE.md](ARCHITECTURE.md) before touching it.

## Tests

The suite covers business rules, not CRUD plumbing — the invariants that cost
money if they break:

| File | Guards |
|---|---|
| `tests/test_tenancy.py` | Tenant A never reaches tenant B, including through a nested id in a request body |
| `tests/test_inventory_ledger.py` | Stock equals the ledger sum; overselling refused online, accepted offline with a discrepancy; moving average cost |
| `tests/test_idempotency.py` | A repeated request is one operation; keys are per-tenant; a failed operation releases its key |
| `tests/test_auth.py` | Sign-in resolves the business from a slug or a terminal token, never a picker; the same username in two shops is two people; credential lockout |
| `tests/test_authorization.py` | Capabilities enforced server-side; a deactivated employee loses access immediately; the last owner cannot be removed |
| `tests/test_money.py` | Tax is extracted from the price, never added (D1) |
| `tests/test_sales.py` | A sale writes document, items, payments and stock together or not at all; totals are server-side; retries charge once |
| `tests/test_refunds.py` | Never refund more than was sold, across successive refunds; rounding never strands money |
| `tests/test_cash.py` | Only cash reaches the drawer; the arqueo adds up; a closed shift accepts nothing |
| `tests/test_purchasing.py` | Receiving writes the ledger and moves the average cost; it cannot happen twice |
| `tests/test_catalog.py` | A SKU/barcode is unique per business, not per product, with a clean 400 either way; a product's one photo uploads, replaces, validates format/size and deletes cleanly |
| `tests/test_expenses.py` | A cash expense leaves the drawer so the arqueo still balances; net profit subtracts expenses from gross; the dashboard agrees with the separate reports |
| `tests/test_concurrency.py` | **`slow`** — two registers on one unit, eight parallel sales, reversed lock order, simultaneous refunds |
| `tests/test_sync.py` | Replaying an offline operation changes nothing; offline sales are accepted without stock and flagged; one bad operation does not sink the batch |
| `tests/test_reporting.py` | Margins use the cost frozen at sale time; refunds are netted out |
| `tests/test_subscription_gating.py` | A lapsed subscription blocks writes, never reads |

```bash
pytest                 # 191 tests, ~19s
pytest -m "not slow"   # 187 tests, ~11s — the loop to run while coding
```
