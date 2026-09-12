"""A completed sale is one atomic fact: document, items, payments and stock."""
from __future__ import annotations

from decimal import Decimal

import pytest

from apps.core.context import tenant_context
from apps.inventory.models import InventoryMovement, MovementType
from apps.inventory.services import InventoryService
from apps.sales.models import Sale

pytestmark = pytest.mark.django_db


def test_a_completed_sale_writes_everything_together(tenant_a, make_stocked_variant, client_for, sell):
    variant = make_stocked_variant(tenant_a, quantity=10, price="119000.00")
    client = client_for(tenant_a.owner, tenant_a.org)

    response = sell(client, [{"variant": str(variant.pk), "quantity": 2}])

    assert response.status_code == 201, response.data
    body = response.data
    assert body["status"] == "COMPLETED"
    assert body["number"] == "V-000001"
    assert Decimal(body["total"]) == Decimal("238000.00")
    assert len(body["items"]) == 1
    assert len(body["payments"]) == 1

    with tenant_context(tenant_a.org.pk):
        assert InventoryService.available(location=tenant_a.location, variant=variant) == 8
        movement = InventoryMovement.objects.get(
            movement_type=MovementType.SALE, source_id=body["id"]
        )
        assert movement.quantity == -2

        # The cost is frozen on the line so margin reports never drift when the
        # average cost moves later.
        from apps.sales.models import SaleItem

        item = SaleItem.objects.get(sale_id=body["id"])
        assert item.unit_cost == variant.average_cost
        assert item.sku == variant.sku  # snapshot: the receipt stays readable


def test_totals_are_computed_by_the_server_never_by_the_client(
    tenant_a, make_stocked_variant, client_for, sell
):
    variant = make_stocked_variant(tenant_a, quantity=10, price="119000.00")
    client = client_for(tenant_a.owner, tenant_a.org)

    # The client sends a total it wishes were true; the server ignores it.
    response = sell(
        client,
        [{"variant": str(variant.pk), "quantity": 1}],
        payments=[{"method": "CASH", "amount": "119000.00"}],
        total="1.00",
        subtotal="1.00",
    )

    assert response.status_code == 201
    assert Decimal(response.data["total"]) == Decimal("119000.00")


def test_tax_is_extracted_from_the_inclusive_price(tenant_a, make_stocked_variant, client_for, sell):
    variant = make_stocked_variant(tenant_a, quantity=5, price="119000.00")
    client = client_for(tenant_a.owner, tenant_a.org)

    response = sell(client, [{"variant": str(variant.pk), "quantity": 1}])

    item = response.data["items"][0]
    assert Decimal(item["line_total"]) == Decimal("119000.00")
    assert Decimal(item["taxable_base"]) == Decimal("100000.00")
    assert Decimal(item["tax_amount"]) == Decimal("19000.00")
    assert Decimal(response.data["tax_total"]) == Decimal("19000.00")


def test_a_business_that_does_not_charge_tax_sells_without_it(
    tenant_a, make_stocked_variant, client_for, sell
):
    """Turning IVA off makes the whole price the taxable base — no tax line at all."""
    variant = make_stocked_variant(tenant_a, quantity=5, price="119000.00")
    tenant_a.org.charges_tax = False
    tenant_a.org.save(update_fields=["charges_tax"])
    client = client_for(tenant_a.owner, tenant_a.org)

    response = sell(client, [{"variant": str(variant.pk), "quantity": 1}])

    item = response.data["items"][0]
    assert Decimal(item["tax_amount"]) == Decimal("0.00")
    assert Decimal(item["taxable_base"]) == Decimal("119000.00")
    assert Decimal(response.data["tax_total"]) == Decimal("0.00")
    # The customer still pays the shelf price: the rate governs the split, not the total.
    assert Decimal(response.data["total"]) == Decimal("119000.00")


def test_the_business_rate_governs_the_split(tenant_a, make_stocked_variant, client_for, sell):
    """The rate comes from the organization, not from the product."""
    variant = make_stocked_variant(tenant_a, quantity=5, price="105000.00")
    tenant_a.org.tax_rate = Decimal("5")
    tenant_a.org.save(update_fields=["tax_rate"])
    client = client_for(tenant_a.owner, tenant_a.org)

    response = sell(
        client,
        [{"variant": str(variant.pk), "quantity": 1}],
        payments=[{"method": "CASH", "amount": "105000.00"}],
    )

    item = response.data["items"][0]
    assert Decimal(item["tax_rate"]) == Decimal("5.00")
    assert Decimal(item["taxable_base"]) == Decimal("100000.00")
    assert Decimal(item["tax_amount"]) == Decimal("5000.00")


def test_a_stale_client_total_is_rejected(tenant_a, make_stocked_variant, client_for, sell):
    """An offline till working from an old price list must not sell at that price."""
    variant = make_stocked_variant(tenant_a, quantity=5, price="119000.00")
    client = client_for(tenant_a.owner, tenant_a.org)

    response = sell(
        client,
        [{"variant": str(variant.pk), "quantity": 1}],
        payments=[{"method": "CASH", "amount": "99000.00"}],
        expected_total="99000.00",
    )

    assert response.status_code == 409
    assert response.data["code"] == "price_mismatch"
    with tenant_context(tenant_a.org.pk):
        assert Sale.objects.count() == 0
        assert InventoryService.available(location=tenant_a.location, variant=variant) == 5


def test_payments_must_cover_the_total(tenant_a, make_stocked_variant, client_for, sell):
    variant = make_stocked_variant(tenant_a, quantity=5, price="119000.00")
    client = client_for(tenant_a.owner, tenant_a.org)

    response = sell(
        client,
        [{"variant": str(variant.pk), "quantity": 1}],
        payments=[{"method": "CASH", "amount": "100000.00"}],
    )

    assert response.status_code == 400
    assert response.data["code"] == "payment_mismatch"
    with tenant_context(tenant_a.org.pk):
        assert InventoryService.available(location=tenant_a.location, variant=variant) == 5


def test_cash_overpayment_produces_change_but_card_overpayment_does_not(
    tenant_a, make_stocked_variant, client_for, sell
):
    variant = make_stocked_variant(tenant_a, quantity=5, price="119000.00")
    client = client_for(tenant_a.owner, tenant_a.org)

    with_change = sell(
        client,
        [{"variant": str(variant.pk), "quantity": 1}],
        payments=[{"method": "CASH", "amount": "150000.00"}],
    )
    assert with_change.status_code == 201
    assert Decimal(with_change.data["change_amount"]) == Decimal("31000.00")

    card_overpay = sell(
        client,
        [{"variant": str(variant.pk), "quantity": 1}],
        payments=[{"method": "CARD", "amount": "150000.00"}],
    )
    assert card_overpay.status_code == 400
    assert card_overpay.data["code"] == "payment_mismatch"


def test_multiple_payment_methods_add_up(tenant_a, make_stocked_variant, client_for, sell):
    variant = make_stocked_variant(tenant_a, quantity=5, price="119000.00")
    client = client_for(tenant_a.owner, tenant_a.org)

    response = sell(
        client,
        [{"variant": str(variant.pk), "quantity": 2}],
        payments=[
            {"method": "CASH", "amount": "100000.00"},
            {"method": "CARD", "amount": "138000.00", "reference": "APR-991"},
        ],
    )

    assert response.status_code == 201
    assert Decimal(response.data["paid_total"]) == Decimal("238000.00")
    assert len(response.data["payments"]) == 2


def test_list_exposes_payment_methods_without_amounts(
    tenant_a, make_stocked_variant, client_for, sell
):
    """The list is thin on purpose: methods for a badge, not full payment detail."""
    variant = make_stocked_variant(tenant_a, quantity=5, price="119000.00")
    client = client_for(tenant_a.owner, tenant_a.org)
    sell(
        client,
        [{"variant": str(variant.pk), "quantity": 2}],
        payments=[
            {"method": "CASH", "amount": "100000.00"},
            {"method": "CARD", "amount": "138000.00"},
        ],
    )

    row = client.get("/api/v1/sales/").data["results"][0]

    assert "payments" not in row
    assert sorted(row["payment_methods"]) == ["CARD", "CASH"]


def test_line_discounts_reduce_the_total_and_the_tax(
    tenant_a, make_stocked_variant, client_for, sell
):
    variant = make_stocked_variant(tenant_a, quantity=5, price="119000.00")
    client = client_for(tenant_a.owner, tenant_a.org)

    response = sell(
        client,
        [{"variant": str(variant.pk), "quantity": 1, "discount_amount": "19000.00"}],
        payments=[{"method": "CASH", "amount": "100000.00"}],
    )

    assert response.status_code == 201
    assert Decimal(response.data["total"]) == Decimal("100000.00")
    assert Decimal(response.data["discount_total"]) == Decimal("19000.00")
    # Tax follows the discounted price, not the list price.
    assert Decimal(response.data["tax_total"]) == Decimal("15966.39")


def test_a_sale_that_cannot_be_stocked_writes_nothing(
    tenant_a, make_stocked_variant, client_for, sell
):
    variant = make_stocked_variant(tenant_a, quantity=1, price="119000.00")
    client = client_for(tenant_a.owner, tenant_a.org)

    response = sell(client, [{"variant": str(variant.pk), "quantity": 5}])

    assert response.status_code == 409
    assert response.data["code"] == "insufficient_stock"
    with tenant_context(tenant_a.org.pk):
        assert Sale.objects.count() == 0
        assert InventoryMovement.objects.filter(movement_type=MovementType.SALE).count() == 0
        assert InventoryService.available(location=tenant_a.location, variant=variant) == 1


def test_an_idempotency_key_is_mandatory_for_sales(tenant_a, make_stocked_variant, client_for):
    variant = make_stocked_variant(tenant_a, quantity=5)
    client = client_for(tenant_a.owner, tenant_a.org)

    response = client.post(
        "/api/v1/sales/",
        {
            "lines": [{"variant": str(variant.pk), "quantity": 1}],
            "payments": [{"method": "CASH", "amount": "119000.00"}],
        },
        format="json",
    )

    assert response.status_code == 400
    assert response.data["code"] == "idempotency_key_required"


def test_a_retried_sale_is_one_sale(tenant_a, make_stocked_variant, client_for, sell):
    """The network dropped, the till retries. The customer is charged once."""
    variant = make_stocked_variant(tenant_a, quantity=10, price="119000.00")
    client = client_for(tenant_a.owner, tenant_a.org)
    key = "retry-after-timeout"

    first = sell(client, [{"variant": str(variant.pk), "quantity": 2}], key=key)
    second = sell(client, [{"variant": str(variant.pk), "quantity": 2}], key=key)

    assert first.status_code == second.status_code == 201
    assert first.data["id"] == second.data["id"]
    assert first.data["number"] == second.data["number"]

    with tenant_context(tenant_a.org.pk):
        assert Sale.objects.count() == 1
        assert InventoryService.available(location=tenant_a.location, variant=variant) == 8


def test_sale_numbers_are_consecutive_and_gap_free(tenant_a, make_stocked_variant, client_for, sell):
    variant = make_stocked_variant(tenant_a, quantity=10, price="119000.00")
    client = client_for(tenant_a.owner, tenant_a.org)

    numbers = [
        sell(client, [{"variant": str(variant.pk), "quantity": 1}]).data["number"] for _ in range(3)
    ]

    assert numbers == ["V-000001", "V-000002", "V-000003"]


def test_a_rejected_sale_does_not_consume_a_number(
    tenant_a, make_stocked_variant, client_for, sell
):
    variant = make_stocked_variant(tenant_a, quantity=1, price="119000.00")
    client = client_for(tenant_a.owner, tenant_a.org)

    assert sell(client, [{"variant": str(variant.pk), "quantity": 1}]).data["number"] == "V-000001"
    assert sell(client, [{"variant": str(variant.pk), "quantity": 5}]).status_code == 409
    variant2 = make_stocked_variant(tenant_a, quantity=5, sku="OTRO")
    assert sell(client, [{"variant": str(variant2.pk), "quantity": 1}]).data["number"] == "V-000002"


def test_cancelling_a_sale_returns_the_stock(tenant_a, make_stocked_variant, client_for, sell):
    variant = make_stocked_variant(tenant_a, quantity=10, price="119000.00")
    client = client_for(tenant_a.owner, tenant_a.org)
    sale = sell(client, [{"variant": str(variant.pk), "quantity": 3}]).data

    response = client.post(
        f"/api/v1/sales/{sale['id']}/cancel/", {"reason": "Cliente se arrepintió"}, format="json"
    )

    assert response.status_code == 200
    assert response.data["status"] == "CANCELLED"
    with tenant_context(tenant_a.org.pk):
        assert InventoryService.available(location=tenant_a.location, variant=variant) == 10
        assert InventoryService.ledger_balance(location=tenant_a.location, variant=variant) == 10


def test_a_sale_cannot_be_cancelled_twice(tenant_a, make_stocked_variant, client_for, sell):
    variant = make_stocked_variant(tenant_a, quantity=10, price="119000.00")
    client = client_for(tenant_a.owner, tenant_a.org)
    sale = sell(client, [{"variant": str(variant.pk), "quantity": 3}]).data

    client.post(f"/api/v1/sales/{sale['id']}/cancel/", {}, format="json")
    second = client.post(f"/api/v1/sales/{sale['id']}/cancel/", {}, format="json")

    assert second.status_code == 400
    assert second.data["code"] == "invalid_operation"
    with tenant_context(tenant_a.org.pk):
        # The stock came back once, not twice.
        assert InventoryService.available(location=tenant_a.location, variant=variant) == 10


def test_a_cashier_can_sell_but_not_cancel(
    tenant_a, make_employee, make_stocked_variant, client_for, sell
):
    cashier = make_employee(tenant_a, username="caja")
    variant = make_stocked_variant(tenant_a, quantity=10, price="119000.00")
    client = client_for(cashier)

    sale = sell(client, [{"variant": str(variant.pk), "quantity": 1}])
    assert sale.status_code == 201

    cancelled = client.post(f"/api/v1/sales/{sale.data['id']}/cancel/", {}, format="json")
    assert cancelled.status_code == 403


def test_sales_never_cross_tenants(tenant_a, tenant_b, make_stocked_variant, client_for, sell):
    variant_a = make_stocked_variant(tenant_a, quantity=5)
    client_a = client_for(tenant_a.owner, tenant_a.org)
    sale = sell(client_a, [{"variant": str(variant_a.pk), "quantity": 1}]).data

    client_b = client_for(tenant_b.owner, tenant_b.org)
    assert client_b.get(f"/api/v1/sales/{sale['id']}/").status_code == 404
    assert client_b.get("/api/v1/sales/").data["count"] == 0
    # And B cannot sell A's stock through a nested id either.
    blocked = sell(client_b, [{"variant": str(variant_a.pk), "quantity": 1}])
    assert blocked.status_code == 400
