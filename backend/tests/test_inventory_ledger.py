"""The inventory invariant: stock is whatever the ledger says it is."""
from __future__ import annotations

from decimal import Decimal

import pytest

from apps.core.context import tenant_context
from apps.core.exceptions import InsufficientStock
from apps.inventory.models import MovementType, StockDiscrepancy, StockLevel
from apps.inventory.services import (
    InventoryService,
    MovementLine,
    record_adjustment,
    record_initial_stock,
)

pytestmark = pytest.mark.django_db


def apply(tenant, variant, quantity, movement_type=MovementType.ADJUSTMENT, **kwargs):
    return InventoryService.apply_movements(
        organization=tenant.org,
        location=tenant.location,
        lines=[MovementLine(variant_id=str(variant.pk), quantity=quantity)],
        movement_type=movement_type,
        **kwargs,
    )


def test_purchase_then_sale_leaves_the_right_balance(tenant_a, make_variant):
    variant = make_variant(tenant_a)

    with tenant_context(tenant_a.org.pk):
        apply(tenant_a, variant, 10, MovementType.PURCHASE)
        apply(tenant_a, variant, -3, MovementType.SALE)

        assert InventoryService.available(location=tenant_a.location, variant=variant) == 7
        # The materialised balance and the ledger must agree, always.
        assert InventoryService.ledger_balance(location=tenant_a.location, variant=variant) == 7


def test_online_operations_refuse_to_oversell(tenant_a, make_variant):
    variant = make_variant(tenant_a)

    with tenant_context(tenant_a.org.pk):
        apply(tenant_a, variant, 2, MovementType.PURCHASE)

        with pytest.raises(InsufficientStock):
            apply(tenant_a, variant, -5, MovementType.SALE)

        # The rejected operation left nothing behind.
        assert InventoryService.available(location=tenant_a.location, variant=variant) == 2
        assert InventoryService.ledger_balance(location=tenant_a.location, variant=variant) == 2


def test_offline_replay_accepts_negative_stock_and_opens_a_discrepancy(tenant_a, make_variant):
    """Decision D4: the goods already left the shelf, so the fact is recorded."""
    variant = make_variant(tenant_a)

    with tenant_context(tenant_a.org.pk):
        apply(tenant_a, variant, 2, MovementType.PURCHASE)
        apply(
            tenant_a,
            variant,
            -5,
            MovementType.SALE,
            allow_negative=True,
            source_type="sync",
            source_id="op-123",
        )

        assert InventoryService.available(location=tenant_a.location, variant=variant) == -3
        discrepancy = StockDiscrepancy.objects.get(variant=variant)
        assert (discrepancy.quantity_before, discrepancy.quantity_after) == (2, -3)
        assert discrepancy.is_resolved is False


def test_recalculate_rebuilds_stock_from_the_ledger(tenant_a, make_variant):
    variant = make_variant(tenant_a)

    with tenant_context(tenant_a.org.pk):
        apply(tenant_a, variant, 10, MovementType.PURCHASE)
        apply(tenant_a, variant, -4, MovementType.SALE)

        # Simulate drift: something wrote the cache directly, which is a bug.
        StockLevel.objects.filter(variant=variant).update(quantity=999)

        result = InventoryService.recalculate(organization=tenant_a.org)

        assert result["levels_corrected"] == 1
        assert InventoryService.available(location=tenant_a.location, variant=variant) == 6


def test_moving_average_cost_follows_the_purchases(tenant_a, make_variant):
    """Decision D3: 10 @ 100 then 10 @ 200 averages to 150."""
    variant = make_variant(tenant_a)

    with tenant_context(tenant_a.org.pk):
        apply(tenant_a, variant, 10, MovementType.PURCHASE)
        InventoryService.update_average_cost(
            variant=variant, incoming_quantity=10, unit_cost=Decimal("100.00")
        )
        apply(tenant_a, variant, 10, MovementType.PURCHASE)
        InventoryService.update_average_cost(
            variant=variant, incoming_quantity=10, unit_cost=Decimal("200.00")
        )

    variant.refresh_from_db()
    assert variant.average_cost == Decimal("150.00")
    assert variant.last_purchase_cost == Decimal("200.00")


def test_initial_stock_seeds_the_cost_of_a_product_with_no_purchases_yet(tenant_a, make_variant):
    """The opening balance is the only cost there is until the first receipt."""
    variant = make_variant(tenant_a)

    with tenant_context(tenant_a.org.pk):
        record_initial_stock(
            organization=tenant_a.org,
            location=tenant_a.location,
            lines=[
                MovementLine(variant_id=str(variant.pk), quantity=10, unit_cost=Decimal("40000.00"))
            ],
        )

    variant.refresh_from_db()
    assert variant.average_cost == Decimal("40000.00")
    assert variant.last_purchase_cost == Decimal("40000.00")


def test_an_adjustment_never_restates_the_cost(tenant_a, make_variant):
    """Losing three units does not change what the remaining ones cost."""
    variant = make_variant(tenant_a)

    with tenant_context(tenant_a.org.pk):
        record_initial_stock(
            organization=tenant_a.org,
            location=tenant_a.location,
            lines=[
                MovementLine(variant_id=str(variant.pk), quantity=10, unit_cost=Decimal("40000.00"))
            ],
        )
        record_adjustment(
            organization=tenant_a.org,
            location=tenant_a.location,
            lines=[
                MovementLine(variant_id=str(variant.pk), quantity=-3, unit_cost=Decimal("999.00"))
            ],
            reason="Merma",
        )

    variant.refresh_from_db()
    assert variant.average_cost == Decimal("40000.00")


def test_the_catalogue_reports_the_cost_it_was_given(tenant_a, make_variant, client_for):
    """Margins are read off the product list, so the cost has to reach it."""
    variant = make_variant(tenant_a)
    with tenant_context(tenant_a.org.pk):
        record_initial_stock(
            organization=tenant_a.org,
            location=tenant_a.location,
            lines=[
                MovementLine(variant_id=str(variant.pk), quantity=5, unit_cost=Decimal("12500.00"))
            ],
        )

    response = client_for(tenant_a.owner, tenant_a.org).get("/api/v1/products/")

    assert response.status_code == 200
    listed = response.data["results"][0]["variants"][0]
    assert Decimal(listed["average_cost"]) == Decimal("12500.00")


def test_stock_quantity_is_not_writable_through_the_api(tenant_a, make_variant, client_for):
    variant = make_variant(tenant_a)
    with tenant_context(tenant_a.org.pk):
        apply(tenant_a, variant, 10, MovementType.PURCHASE)
        level = StockLevel.objects.get(variant=variant)

    response = client_for(tenant_a.owner, tenant_a.org).patch(
        f"/api/v1/inventory/stock/{level.pk}/",
        {"quantity": 500, "reorder_point": 3},
        format="json",
    )

    assert response.status_code == 200
    level.refresh_from_db()
    assert level.quantity == 10  # ignored: stock only moves through movements
    assert level.reorder_point == 3


def test_deleting_a_product_takes_its_row_out_of_the_stock_list(
    tenant_a, make_stocked_variant, client_for
):
    """Borrar un producto lo baja a cero, y su fila deja de estorbar en el listado."""
    variant = make_stocked_variant(tenant_a, quantity=10)
    client = client_for(tenant_a.owner, tenant_a.org)
    assert client.get("/api/v1/inventory/stock/").data["count"] == 1

    assert client.delete(f"/api/v1/products/{variant.product_id}/").status_code == 204

    assert client.get("/api/v1/inventory/stock/").data["count"] == 0
    with tenant_context(tenant_a.org.pk):
        # La fila sigue en la base: `recalculate_stock` reconstruye saldos desde
        # el ledger y necesita encontrarla.
        level = StockLevel.objects.get(variant=variant)
        assert level.quantity == 0


def test_the_stock_of_a_deleted_product_can_still_be_asked_for(
    tenant_a, make_stocked_variant, client_for
):
    """Esconderlo por defecto no es borrarlo: un reporte todavía puede pedirlo."""
    variant = make_stocked_variant(tenant_a, quantity=10)
    client = client_for(tenant_a.owner, tenant_a.org)
    client.delete(f"/api/v1/products/{variant.product_id}/")

    included = client.get("/api/v1/inventory/stock/?include_inactive=true").data
    only_deleted = client.get("/api/v1/inventory/stock/?is_active=false").data

    assert included["count"] == 1
    assert only_deleted["count"] == 1
    assert only_deleted["results"][0]["quantity"] == 0


def test_the_ledger_keeps_the_history_of_a_deleted_product(
    tenant_a, make_stocked_variant, client_for
):
    """El movimiento es el historial y no se toca: cuenta de dónde salió la mercancía."""
    variant = make_stocked_variant(tenant_a, quantity=10)
    client = client_for(tenant_a.owner, tenant_a.org)

    client.delete(f"/api/v1/products/{variant.product_id}/")

    movements = client.get(f"/api/v1/inventory/movements/?variant={variant.pk}").data
    kinds = [row["movement_type"] for row in movements["results"]]
    assert movements["count"] == 2
    # La entrada original y el ajuste que la deja en cero, ambos legibles.
    assert set(kinds) == {MovementType.PURCHASE, MovementType.ADJUSTMENT}
