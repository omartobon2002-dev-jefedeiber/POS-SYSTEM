"""End-to-end smoke check of phase 1 against a running dev server.

Not a substitute for the test suite - it is the quick "is the whole thing
wired together" pass:

    python manage.py runserver 127.0.0.1:8011
    python scripts/smoke_phase1.py

It writes real data (two throwaway organizations with random names) into
whatever database the server is pointed at. Development only.
"""
import json
import os
import urllib.error
import urllib.request
import uuid

BASE = os.environ.get("SMOKE_BASE_URL", "http://127.0.0.1:8011/api/v1")
FAILS = []


def call(method, path, token=None, body=None, headers=None, expect=None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req) as resp:
            payload = json.loads(resp.read() or b"null")
            status = resp.status
    except urllib.error.HTTPError as e:
        payload = json.loads(e.read() or b"null")
        status = e.code
    if expect is not None and status != expect:
        FAILS.append(f"{method} {path} -> {status} (expected {expect}): {payload}")
    return status, payload


def check(label, condition, detail=""):
    print(("  PASS  " if condition else "  FAIL  ") + label + ("" if condition else f"  <- {detail}"))
    if not condition:
        FAILS.append(label)


suffix = uuid.uuid4().hex[:8]

print("\n[1] Signup provisions a complete tenant")
_, a = call("POST", "/auth/register/", body={
    "username": f"ana{suffix}", "password": "UnaClaveMuySegura9",
    "first_name": "Ana", "organization_name": f"Moda Urbana {suffix}", "tax_id": "900123456-7",
}, expect=201)
token_a = a["access"]
slug_a = a["organization"]["slug"]
check("owner role granted", a["role"] == "OWNER", a.get("role"))
check("slug returned so the owner can sign back in", bool(slug_a), a["organization"])
check("capabilities returned", "sales.refund" in a["capabilities"])
_, sub = call("GET", "/subscription/", token_a, expect=200)
check("trial subscription created", sub["status"] == "TRIAL", sub)
_, locs = call("GET", "/locations/", token_a, expect=200)
check("default location created", locs["count"] == 1 and locs["results"][0]["is_default"], locs)
loc_a = locs["results"][0]["id"]

print("\n[2] Catalog: product with variants")
_, prod = call("POST", "/products/", token_a, body={
    "name": "Nike Air Max",
    "variants": [
        {"sku": f"NAM-38-BLK-{suffix}", "barcode": f"770{suffix}01", "size": "38", "color": "Black", "price": "459900.00"},
        {"sku": f"NAM-39-BLK-{suffix}", "barcode": f"770{suffix}02", "size": "39", "color": "Black", "price": "459900.00"},
    ],
}, expect=201)
check("two variants created", len(prod["variants"]) == 2, prod)
v38 = prod["variants"][0]["id"]
v39 = prod["variants"][1]["id"]

_, look = call("GET", f"/variants/lookup/?barcode=770{suffix}01", token_a, expect=200)
check("barcode lookup works", look["sku"].endswith(f"38-BLK-{suffix}"), look)

print("\n[3] Inventory ledger")
call("POST", "/inventory/initial-stock/", token_a, body={
    "location": loc_a, "reason": "Carga inicial",
    "lines": [{"variant": v38, "quantity": 10}, {"variant": v39, "quantity": 4}],
}, expect=201)
_, stock = call("GET", f"/inventory/stock/?variant={v38}", token_a, expect=200)
check("stock = 10 after initial load", stock["results"][0]["quantity"] == 10, stock)

key = str(uuid.uuid4())
s1, _ = call("POST", "/inventory/adjustments/", token_a,
             body={"location": loc_a, "reason": "Merma", "lines": [{"variant": v38, "quantity": -3}]},
             headers={"Idempotency-Key": key}, expect=201)
s2, r2 = call("POST", "/inventory/adjustments/", token_a,
              body={"location": loc_a, "reason": "Merma", "lines": [{"variant": v38, "quantity": -3}]},
              headers={"Idempotency-Key": key})
check("idempotent retry replays (201)", s2 == 201, (s2, r2))
_, stock = call("GET", f"/inventory/stock/?variant={v38}", token_a)
check("stock = 7 after ONE adjustment applied twice", stock["results"][0]["quantity"] == 7, stock)

st, body = call("POST", "/inventory/adjustments/", token_a,
                body={"location": loc_a, "reason": "Merma", "lines": [{"variant": v38, "quantity": -1}]},
                headers={"Idempotency-Key": key})
check("same key + different payload -> 409", st == 409 and body.get("code") == "idempotency_conflict", (st, body))

st, body = call("POST", "/inventory/adjustments/", token_a,
                body={"location": loc_a, "lines": [{"variant": v38, "quantity": -99}]})
check("overselling refused -> 409 insufficient_stock", st == 409 and body.get("code") == "insufficient_stock", (st, body))

_, mv = call("GET", f"/inventory/movements/?variant={v38}", token_a)
total = sum(m["quantity"] for m in mv["results"])
check("ledger sums to materialised stock", total == 7, total)

print("\n[4] Tenant isolation")
_, b = call("POST", "/auth/register/", body={
    "username": f"ana{suffix}", "password": "OtraClaveSegura9",
    "organization_name": f"Otra Tienda {suffix}",
}, expect=201)
token_b = b["access"]
_, plist = call("GET", "/products/", token_b, expect=200)
check("tenant B sees no products of A", plist["count"] == 0, plist)
st, _ = call("GET", f"/variants/{v38}/", token_b)
check("tenant B cannot read A's variant by id -> 404", st == 404, st)
st, body = call("POST", "/inventory/adjustments/", token_b,
                body={"lines": [{"variant": v38, "quantity": -1}]})
check("tenant B cannot move A's stock -> 400 invalid variant", st == 400, (st, body))
_, stock = call("GET", f"/inventory/stock/?variant={v38}", token_a)
check("A's stock untouched by B", stock["results"][0]["quantity"] == 7, stock)

print("\n[5] Authorization is server-side")
_, hired = call("POST", "/employees/", token_a, body={
    "username": "jperez", "first_name": "Juan", "last_name": "Perez", "role": "CASHIER",
    "password": "ClaveDeCajero9", "email": f"jperez{suffix}@example.com",
}, expect=201)
check("employee created with the given username", hired["username"] == "jperez", hired)
_, cashier = call("POST", "/auth/login/", body={
    "organization": slug_a, "username": "jperez", "password": "ClaveDeCajero9",
}, expect=200)
tc = cashier["access"]
check("cashier scoped to same org", cashier["organization"]["id"] == a["organization"]["id"])
st, _ = call("GET", "/products/", tc)
check("cashier can read products", st == 200, st)
st, body = call("POST", "/products/", tc, body={"name": "X", "variants": []})
check("cashier cannot create products -> 403", st == 403, (st, body))
st, body = call("POST", "/inventory/adjustments/", tc, body={"lines": [{"variant": v38, "quantity": -1}]})
check("cashier cannot adjust inventory -> 403", st == 403, (st, body))
st, _ = call("GET", "/employees/", tc)
check("cashier can list employees (read scope)", st == 200, st)
st, body = call("POST", "/employees/", tc, body={"username": "otro", "first_name": "Otro", "role": "CASHIER"})
check("cashier cannot create employees -> 403", st == 403, (st, body))

print("\n[6] Identity is (organization, username)")
_, hired_b = call("POST", "/employees/", token_b, body={
    "username": "jperez", "first_name": "Juan", "role": "CASHIER",
    "password": "OtraClaveDeCajero9", "email": f"jperez.b{suffix}@example.com",
}, expect=201)
check("same username reused in another business", hired_b["username"] == "jperez", hired_b)
st, _ = call("POST", "/auth/login/", body={
    "organization": b["organization"]["slug"], "username": "jperez", "password": "ClaveDeCajero9",
})
check("A's password does not open B's account -> 401", st == 401, st)
# A till resolves its own business, so the cashier types no slug at all.
_, dev = call("POST", "/sync/devices/", token_a, body={
    "identifier": f"TILL-{suffix}", "name": "Caja 1", "platform": "android",
}, expect=201)
check("terminal token returned once, in the clear", bool(dev.get("token")), dev)
_, till = call("POST", "/auth/login/", body={"username": "jperez", "password": "ClaveDeCajero9"},
               headers={"X-Device-Token": dev["token"]}, expect=200)
check("till login needs no slug", till["organization"]["id"] == a["organization"]["id"])

print("\n" + ("ALL CHECKS PASSED" if not FAILS else f"{len(FAILS)} FAILURE(S):\n- " + "\n- ".join(map(str, FAILS))))

raise SystemExit(1 if FAILS else 0)
