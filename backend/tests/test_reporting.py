"""Reports are derived from the operational tables, never from a second copy."""
from __future__ import annotations

from decimal import Decimal

import pytest

pytestmark = pytest.mark.django_db


@pytest.fixture
def traded(tenant_a, make_stocked_variant, client_for, sell):
    """Two sales of a 119.000 item bought at 50.000, then one unit refunded."""
    variant = make_stocked_variant(tenant_a, quantity=20, price="119000.00", cost="50000.00")
    client = client_for(tenant_a.owner, tenant_a.org)
    first = sell(client, [{"variant": str(variant.pk), "quantity": 3}]).data
    sell(client, [{"variant": str(variant.pk), "quantity": 2}])
    import uuid

    client.post(
        "/api/v1/refunds/",
        {"sale": first["id"], "lines": [{"sale_item": first["items"][0]["id"], "quantity": 1}]},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    return client, variant


def test_sales_summary_nets_out_refunds(traded):
    client, _ = traded

    data = client.get("/api/v1/reports/sales-summary/").data

    assert data["sales_count"] == 2
    assert Decimal(data["gross_total"]) == Decimal("595000.00")  # 357.000 + 238.000
    assert Decimal(data["refunded_total"]) == Decimal("119000.00")
    assert Decimal(data["net_total"]) == Decimal("476000.00")
    assert Decimal(data["average_ticket"]) == Decimal("297500.00")
    assert Decimal(data["payments_by_method"]["CASH"]) == Decimal("595000.00")
    assert len(data["by_day"]) == 1


def test_top_products_subtracts_returned_units(traded):
    client, variant = traded

    rows = client.get("/api/v1/reports/top-products/").data

    assert len(rows) == 1
    assert rows[0]["variant"] == str(variant.pk)
    assert rows[0]["units"] == 4  # 5 sold, 1 returned


def test_margin_uses_the_cost_frozen_at_sale_time(traded):
    """The whole point of freezing unit_cost: last month's margin cannot move."""
    client, variant = traded

    before = client.get("/api/v1/reports/margin/").data
    assert before["units_sold"] == 4
    assert Decimal(before["revenue"]) == Decimal("476000.00")
    assert Decimal(before["cost"]) == Decimal("200000.00")  # 4 x 50.000
    assert Decimal(before["gross_profit"]) == Decimal("276000.00")

    # A later, much more expensive purchase moves the average cost.
    variant.average_cost = Decimal("90000.00")
    variant.save(update_fields=["average_cost"])

    after = client.get("/api/v1/reports/margin/").data
    money = ("units_sold", "revenue", "cost", "gross_profit", "margin_percent")
    assert {k: after[k] for k in money} == {k: before[k] for k in money}


def test_inventory_valuation_prices_stock_at_average_cost(tenant_a, make_stocked_variant, client_for):
    make_stocked_variant(tenant_a, quantity=10, price="119000.00", cost="50000.00")
    client = client_for(tenant_a.owner, tenant_a.org)

    data = client.get("/api/v1/reports/inventory-valuation/").data

    assert data["units_on_hand"] == 10
    assert Decimal(data["cost_value"]) == Decimal("500000.00")
    assert Decimal(data["retail_value"]) == Decimal("1190000.00")
    assert Decimal(data["potential_margin"]) == Decimal("690000.00")
    assert data["negative_stock"] == []


def test_inventory_valuation_surfaces_negative_stock(
    tenant_a, make_stocked_variant, client_for, device, push
):
    """Negative stock only comes from replayed offline sales, and must be visible."""
    import uuid

    variant = make_stocked_variant(tenant_a, quantity=1, price="119000.00")
    terminal = device(tenant_a)
    client = client_for(tenant_a.owner, tenant_a.org)
    from apps.core.context import tenant_context
    from apps.customers.models import Customer

    with tenant_context(tenant_a.org.pk):
        customer = Customer.objects.create(organization=tenant_a.org, name="Cliente Report")
    push(
        client,
        terminal,
        [
            {
                "operation_id": str(uuid.uuid4()),
                "operation_type": "SALE_CREATE",
                "payload": {
                    "customer": str(customer.pk),
                    "lines": [{"variant": str(variant.pk), "quantity": 4}],
                    "payments": [{"method": "CASH", "amount": "476000.00"}],
                },
            }
        ],
    )

    data = client.get("/api/v1/reports/inventory-valuation/").data

    assert len(data["negative_stock"]) == 1
    assert data["negative_stock"][0]["quantity"] == -3


def test_cash_session_report_shows_the_differences(tenant_a, open_register, client_for):
    _, session = open_register(tenant_a, opening_amount="100000.00")
    client = client_for(tenant_a.owner, tenant_a.org)
    client.post(
        f"/api/v1/cash/sessions/{session.pk}/close/", {"counted_amount": "94000.00"}, format="json"
    )

    data = client.get("/api/v1/reports/cash-sessions/").data

    assert data["sessions_closed"] == 1
    assert Decimal(data["total_shortfall"]) == Decimal("-6000.00")
    assert Decimal(data["total_surplus"]) == Decimal("0.00")
    assert Decimal(data["sessions"][0]["difference"]) == Decimal("-6000.00")


def test_refunds_summary_separates_restocked_from_written_off(traded):
    client, _ = traded

    data = client.get("/api/v1/reports/refunds/").data

    assert data["refunds_count"] == 1
    assert data["restocked_count"] == 1
    assert data["written_off_count"] == 0


def test_a_cashier_cannot_read_reports(tenant_a, make_employee, client_for):
    cashier = make_employee(tenant_a, username="cajarep")

    response = client_for(cashier).get("/api/v1/reports/sales-summary/")

    assert response.status_code == 403


def test_reports_never_mix_tenants(tenant_a, tenant_b, traded, client_for):
    data = client_for(tenant_b.owner, tenant_b.org).get("/api/v1/reports/sales-summary/").data

    assert data["sales_count"] == 0
    assert Decimal(data["gross_total"]) == Decimal("0.00")
