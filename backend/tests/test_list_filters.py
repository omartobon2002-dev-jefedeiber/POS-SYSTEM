"""Server-side filters the frontend relies on instead of loading everything."""
from __future__ import annotations

import pytest

from apps.core.context import tenant_context
from apps.inventory.models import StockLevel

pytestmark = pytest.mark.django_db


def test_stock_can_be_asked_for_several_products_at_once(tenant_a, make_stocked_variant, client_for):
    a = make_stocked_variant(tenant_a, quantity=3)
    b = make_stocked_variant(tenant_a, quantity=4)
    make_stocked_variant(tenant_a, quantity=5)  # not asked for

    response = client_for(tenant_a.owner, tenant_a.org).get(
        "/api/v1/inventory/stock/", {"products": f"{a.product_id},{b.product_id}"}
    )

    assert response.status_code == 200
    assert {str(row["variant"]) for row in response.data["results"]} == {str(a.pk), str(b.pk)}


def test_products_filter_by_stock_status_across_the_catalogue(
    tenant_a, make_stocked_variant, client_for
):
    out = make_stocked_variant(tenant_a, quantity=1)
    low = make_stocked_variant(tenant_a, quantity=2)
    plenty = make_stocked_variant(tenant_a, quantity=50)
    with tenant_context(tenant_a.org.pk):
        StockLevel.objects.filter(variant=out).update(quantity=0)
        StockLevel.objects.filter(variant__in=[low, plenty]).update(reorder_point=5)
    client = client_for(tenant_a.owner, tenant_a.org)

    out_ids = {p["id"] for p in client.get("/api/v1/products/", {"stock_status": "out"}).data["results"]}
    low_ids = {p["id"] for p in client.get("/api/v1/products/", {"stock_status": "low"}).data["results"]}

    assert out_ids == {str(out.product_id)}
    assert low_ids == {str(low.product_id)}


def test_sales_filter_by_payment_method(tenant_a, make_stocked_variant, client_for, sell):
    variant = make_stocked_variant(tenant_a, quantity=10, price="10000.00")
    client = client_for(tenant_a.owner, tenant_a.org)
    cash = sell(client, [{"variant": str(variant.pk), "quantity": 1}])
    card = sell(
        client,
        [{"variant": str(variant.pk), "quantity": 1}],
        payments=[{"method": "CARD", "amount": "10000.00"}],
    )

    ids = {s["id"] for s in client.get("/api/v1/sales/", {"payment_method": "CARD"}).data["results"]}

    assert ids == {card.data["id"]}
    assert cash.data["id"] not in ids
