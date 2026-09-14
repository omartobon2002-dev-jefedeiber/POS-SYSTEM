"""Cashiers operate catalogue and stock; costs stay with the owner."""
from __future__ import annotations

from decimal import Decimal

import pytest

from apps.accounts.models import Membership
from apps.catalog.models import ProductVariant
from apps.inventory.models import InventoryMovement, StockLevel

pytestmark = pytest.mark.django_db


@pytest.fixture
def cashier(tenant_a, make_employee):
    return make_employee(tenant_a, role=Membership.Role.CASHIER, username="cajero-cost")


def test_cashier_initial_stock_ignores_forged_unit_cost(tenant_a, cashier, make_variant, client_for):
    from apps.core.context import tenant_context

    variant = make_variant(tenant_a)
    client = client_for(cashier)

    response = client.post(
        "/api/v1/inventory/initial-stock/",
        {
            "lines": [
                {"variant": str(variant.pk), "quantity": 4, "unit_cost": "50000.00"},
            ]
        },
        format="json",
    )

    assert response.status_code == 201
    assert "unit_cost" not in response.data[0]
    with tenant_context(tenant_a.org.pk):
        variant.refresh_from_db()
        assert variant.average_cost == Decimal("0")
        movement = InventoryMovement.objects.get(variant=variant)
        assert movement.unit_cost is None
        assert StockLevel.objects.get(variant=variant).quantity == 4


def test_owner_sees_costs_and_can_complete_pending(tenant_a, make_variant, client_for):
    variant = make_variant(tenant_a)
    client = client_for(tenant_a.owner)

    seeded = client.post(
        "/api/v1/inventory/initial-stock/",
        {"lines": [{"variant": str(variant.pk), "quantity": 3}]},
        format="json",
    )
    assert seeded.status_code == 201

    pending = client.get("/api/v1/variants/pending-cost/")
    assert pending.status_code == 200
    assert pending.data["count"] >= 1
    assert any(str(row["id"]) == str(variant.pk) for row in pending.data["results"])
    assert "average_cost" in pending.data["results"][0]

    set_cost = client.post(
        f"/api/v1/variants/{variant.pk}/set-cost/",
        {"unit_cost": "12500.00"},
        format="json",
    )
    assert set_cost.status_code == 200
    assert Decimal(set_cost.data["average_cost"]) == Decimal("12500.00")

    variant.refresh_from_db()
    assert variant.average_cost == Decimal("12500.00")
    assert variant.last_purchase_cost == Decimal("12500.00")

    pending_after = client.get("/api/v1/variants/pending-cost/")
    assert all(str(row["id"]) != str(variant.pk) for row in pending_after.data["results"])


def test_cashier_cannot_list_pending_cost_or_set_cost(tenant_a, cashier, make_variant, client_for):
    variant = make_variant(tenant_a)
    client = client_for(cashier)

    assert client.get("/api/v1/variants/pending-cost/").status_code == 403
    assert (
        client.post(
            f"/api/v1/variants/{variant.pk}/set-cost/",
            {"unit_cost": "10.00"},
            format="json",
        ).status_code
        == 403
    )


def test_products_list_hides_costs_from_cashier(tenant_a, cashier, make_stocked_variant, client_for):
    make_stocked_variant(tenant_a, quantity=2, cost="8000.00")
    client = client_for(cashier)

    response = client.get("/api/v1/products/")
    assert response.status_code == 200
    results = response.data["results"] if "results" in response.data else response.data
    variant = results[0]["variants"][0]
    assert "average_cost" not in variant
    assert "last_purchase_cost" not in variant


def test_margin_report_includes_cost_coverage(tenant_a, make_stocked_variant, client_for, sell):
    from apps.core.context import tenant_context

    variant = make_stocked_variant(tenant_a, quantity=5, cost="0")
    # Force zero cost so coverage is incomplete even after a sale.
    with tenant_context(tenant_a.org.pk):
        ProductVariant.objects.filter(pk=variant.pk).update(average_cost=0, last_purchase_cost=0)

    client = client_for(tenant_a.owner)
    sold = sell(client, [{"variant": str(variant.pk), "quantity": 1}])
    assert sold.status_code == 201

    margin = client.get("/api/v1/reports/margin/")
    assert margin.status_code == 200
    coverage = margin.data["cost_coverage"]
    assert coverage["incomplete"] is True
    assert coverage["units_total"] >= 1
    assert coverage["units_with_cost"] == 0
