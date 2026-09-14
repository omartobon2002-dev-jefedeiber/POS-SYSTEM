"""Authorization vocabulary.

Roles are the user-facing concept; capabilities are what the code checks.
Keeping the mapping in code (not in a table) is enough for the current product
and costs nothing to replace later with per-tenant custom roles: a `Role` model
would resolve to the same capability strings and no view would change.

Tenant answers "which business is this?"; capabilities answer "what may this
user do inside it?". They are never conflated.
"""
from __future__ import annotations

# Organization / users
ORGANIZATION_READ = "organization.read"
ORGANIZATION_MANAGE = "organization.manage"
USERS_MANAGE = "users.manage"

# Catalog
PRODUCTS_READ = "products.read"
PRODUCTS_WRITE = "products.write"

# Costs (margin / valuation). Separate from products.write so a cashier can
# maintain the catalogue without seeing or setting unit costs.
COSTS_READ = "costs.read"

# Inventory
INVENTORY_READ = "inventory.read"
INVENTORY_ADJUST = "inventory.adjust"

# Purchasing
SUPPLIERS_READ = "suppliers.read"
SUPPLIERS_WRITE = "suppliers.write"
PURCHASES_READ = "purchases.read"
PURCHASES_CREATE = "purchases.create"

# Sales
SALES_READ = "sales.read"
SALES_CREATE = "sales.create"
SALES_CANCEL = "sales.cancel"
SALES_REFUND = "sales.refund"

# Customers
CUSTOMERS_READ = "customers.read"
CUSTOMERS_WRITE = "customers.write"

# Expenses
EXPENSES_READ = "expenses.read"
EXPENSES_WRITE = "expenses.write"

# Cash
CASH_READ = "cash.read"
CASH_OPEN = "cash.open"
CASH_CLOSE = "cash.close"
CASH_MOVEMENT = "cash.movement"

# Reporting / subscription / sync
REPORTS_READ = "reports.read"
SUBSCRIPTION_READ = "subscription.read"
SUBSCRIPTION_MANAGE = "subscription.manage"
SYNC_PUSH = "sync.push"

ALL_CAPABILITIES: frozenset[str] = frozenset(
    value
    for name, value in list(globals().items())
    if name.isupper() and isinstance(value, str) and "." in value
)

_MANAGER_CAPABILITIES = frozenset(
    {
        ORGANIZATION_READ,
        PRODUCTS_READ,
        PRODUCTS_WRITE,
        COSTS_READ,
        INVENTORY_READ,
        INVENTORY_ADJUST,
        SUPPLIERS_READ,
        SUPPLIERS_WRITE,
        PURCHASES_READ,
        PURCHASES_CREATE,
        SALES_READ,
        SALES_CREATE,
        SALES_CANCEL,
        SALES_REFUND,
        CUSTOMERS_READ,
        CUSTOMERS_WRITE,
        EXPENSES_READ,
        EXPENSES_WRITE,
        CASH_READ,
        CASH_OPEN,
        CASH_CLOSE,
        CASH_MOVEMENT,
        REPORTS_READ,
        SUBSCRIPTION_READ,
        SYNC_PUSH,
    }
)

_CASHIER_CAPABILITIES = frozenset(
    {
        ORGANIZATION_READ,
        PRODUCTS_READ,
        PRODUCTS_WRITE,
        INVENTORY_READ,
        INVENTORY_ADJUST,
        SALES_READ,
        SALES_CREATE,
        CUSTOMERS_READ,
        CUSTOMERS_WRITE,
        CASH_READ,
        CASH_OPEN,
        CASH_CLOSE,
        CASH_MOVEMENT,
        SYNC_PUSH,
    }
)

ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    "OWNER": ALL_CAPABILITIES,
    "MANAGER": _MANAGER_CAPABILITIES,
    "CASHIER": _CASHIER_CAPABILITIES,
}


def capabilities_for_role(role: str) -> frozenset[str]:
    return ROLE_CAPABILITIES.get(role, frozenset())
