"""Offline replay: accept the same operation any number of times, apply it once."""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from apps.core.context import tenant_context
from apps.inventory.models import StockDiscrepancy
from apps.inventory.services import InventoryService
from apps.sales.models import Sale
from apps.synchronization.models import Device, SyncOperation

pytestmark = pytest.mark.django_db


def sale_op(variant, quantity=1, amount="119000.00", operation_id=None, customer=None):
    from apps.customers.models import Customer

    if customer is None:
        with tenant_context(variant.organization_id):
            customer = Customer.objects.create(
                organization=variant.organization, name="Cliente Sync"
            )
    return {
        "operation_id": operation_id or str(uuid.uuid4()),
        "operation_type": "SALE_CREATE",
        "occurred_at": "2026-08-20T15:00:00Z",
        "payload": {
            "customer": str(customer.pk if hasattr(customer, "pk") else customer),
            "lines": [{"variant": str(variant.pk), "quantity": quantity}],
            "payments": [{"method": "CASH", "amount": amount}],
        },
    }


def test_registering_the_same_terminal_twice_keeps_one_device(tenant_a, client_for):
    client = client_for(tenant_a.owner, tenant_a.org)
    body = {"identifier": "TILL-01", "name": "Caja móvil", "platform": "android"}

    first = client.post("/api/v1/sync/devices/", body, format="json")
    second = client.post("/api/v1/sync/devices/", {**body, "name": "Caja 1"}, format="json")

    assert first.status_code == 201
    assert second.status_code == 200  # updated, not duplicated
    assert first.data["id"] == second.data["id"]
    with tenant_context(tenant_a.org.pk):
        assert Device.objects.count() == 1
        assert Device.objects.get().name == "Caja 1"


def test_a_synced_sale_is_a_real_sale(tenant_a, make_stocked_variant, client_for, device, push):
    variant = make_stocked_variant(tenant_a, quantity=10, price="119000.00")
    terminal = device(tenant_a)
    client = client_for(tenant_a.owner, tenant_a.org)

    response = push(client, terminal, [sale_op(variant, quantity=2, amount="238000.00")])

    assert response.status_code == 200, response.data
    assert response.data["summary"] == {"accepted": 1, "duplicated": 0, "failed": 0}
    result = response.data["results"][0]
    assert result["status"] == "PROCESSED"
    assert result["duplicate"] is False

    with tenant_context(tenant_a.org.pk):
        sale = Sale.objects.get()
        assert sale.source == Sale.Source.SYNC
        assert sale.device_id == "TILL-01"
        assert sale.number == "V-000001"
        assert sale.total == Decimal("238000.00")
        # The terminal's clock, not the server's.
        assert sale.occurred_at.isoformat().startswith("2026-08-20T15:00")
        assert InventoryService.available(location=tenant_a.location, variant=variant) == 8


def test_replaying_an_operation_changes_nothing(
    tenant_a, make_stocked_variant, client_for, device, push
):
    variant = make_stocked_variant(tenant_a, quantity=10, price="119000.00")
    terminal = device(tenant_a)
    client = client_for(tenant_a.owner, tenant_a.org)
    operation = sale_op(variant, quantity=2, amount="238000.00")

    first = push(client, terminal, [operation])
    second = push(client, terminal, [operation])

    assert first.data["results"][0]["duplicate"] is False
    assert second.data["results"][0]["duplicate"] is True
    assert second.data["summary"]["duplicated"] == 1
    assert first.data["results"][0]["result"] == second.data["results"][0]["result"]

    with tenant_context(tenant_a.org.pk):
        assert Sale.objects.count() == 1
        assert InventoryService.available(location=tenant_a.location, variant=variant) == 8


def test_resending_a_whole_batch_is_safe(tenant_a, make_stocked_variant, client_for, device, push):
    """The till never got the response and sends everything again."""
    variant = make_stocked_variant(tenant_a, quantity=10, price="119000.00")
    terminal = device(tenant_a)
    client = client_for(tenant_a.owner, tenant_a.org)
    batch = [sale_op(variant), sale_op(variant), sale_op(variant)]

    push(client, terminal, batch)
    again = push(client, terminal, batch)

    assert again.data["summary"] == {"accepted": 0, "duplicated": 3, "failed": 0}
    with tenant_context(tenant_a.org.pk):
        assert Sale.objects.count() == 3
        assert InventoryService.available(location=tenant_a.location, variant=variant) == 7


def test_an_offline_sale_is_accepted_even_without_stock(
    tenant_a, make_stocked_variant, client_for, device, push
):
    """Decision D4: the goods already left the shelf. Record it, flag it."""
    variant = make_stocked_variant(tenant_a, quantity=1, price="119000.00")
    terminal = device(tenant_a)
    client = client_for(tenant_a.owner, tenant_a.org)

    response = push(client, terminal, [sale_op(variant, quantity=3, amount="357000.00")])

    assert response.data["summary"]["accepted"] == 1
    with tenant_context(tenant_a.org.pk):
        assert InventoryService.available(location=tenant_a.location, variant=variant) == -2
        discrepancy = StockDiscrepancy.objects.get(variant=variant)
        assert discrepancy.quantity_after == -2
        assert discrepancy.is_resolved is False
        assert Sale.objects.count() == 1


def test_an_online_sale_of_the_same_shape_is_still_refused(
    tenant_a, make_stocked_variant, client_for, sell
):
    """The offline exemption must not leak into the online path."""
    variant = make_stocked_variant(tenant_a, quantity=1, price="119000.00")
    client = client_for(tenant_a.owner, tenant_a.org)

    response = sell(client, [{"variant": str(variant.pk), "quantity": 3}])

    assert response.status_code == 409
    assert response.data["code"] == "insufficient_stock"


def test_a_rejected_operation_is_recorded_and_not_retried(
    tenant_a, make_stocked_variant, client_for, device, push
):
    variant = make_stocked_variant(tenant_a, quantity=5, price="119000.00")
    terminal = device(tenant_a)
    client = client_for(tenant_a.owner, tenant_a.org)
    broken = sale_op(variant, quantity=1, amount="1.00")  # payment does not cover the total

    first = push(client, terminal, [broken])
    second = push(client, terminal, [broken])

    assert first.data["summary"]["failed"] == 1
    assert first.data["results"][0]["error_code"] == "payment_mismatch"
    # Resending it returns the recorded failure instead of trying again.
    assert second.data["results"][0]["duplicate"] is True
    assert second.data["results"][0]["status"] == "FAILED"

    with tenant_context(tenant_a.org.pk):
        assert Sale.objects.count() == 0
        assert SyncOperation.objects.filter(status=SyncOperation.Status.FAILED).count() == 1


def test_one_bad_operation_does_not_sink_the_batch(
    tenant_a, make_stocked_variant, client_for, device, push
):
    """Each operation gets its own transaction."""
    variant = make_stocked_variant(tenant_a, quantity=10, price="119000.00")
    terminal = device(tenant_a)
    client = client_for(tenant_a.owner, tenant_a.org)

    response = push(
        client,
        terminal,
        [
            sale_op(variant),
            {
                "operation_id": str(uuid.uuid4()),
                "operation_type": "SALE_CREATE",
                "payload": {"lines": [{"variant": str(uuid.uuid4()), "quantity": 1}], "payments": []},
            },
            sale_op(variant),
        ],
    )

    assert response.data["summary"] == {"accepted": 2, "duplicated": 0, "failed": 1}
    with tenant_context(tenant_a.org.pk):
        assert Sale.objects.count() == 2
        assert InventoryService.available(location=tenant_a.location, variant=variant) == 8


def test_a_synced_refund_returns_the_goods(
    tenant_a, make_stocked_variant, client_for, device, push, sell
):
    variant = make_stocked_variant(tenant_a, quantity=10, price="119000.00")
    terminal = device(tenant_a)
    client = client_for(tenant_a.owner, tenant_a.org)
    sale = sell(client, [{"variant": str(variant.pk), "quantity": 3}]).data

    response = push(
        client,
        terminal,
        [
            {
                "operation_id": str(uuid.uuid4()),
                "operation_type": "REFUND_CREATE",
                "payload": {
                    "sale": sale["id"],
                    "lines": [{"sale_item": sale["items"][0]["id"], "quantity": 1}],
                },
            }
        ],
    )

    assert response.data["summary"]["accepted"] == 1
    with tenant_context(tenant_a.org.pk):
        assert InventoryService.available(location=tenant_a.location, variant=variant) == 8
        assert Sale.objects.get(pk=sale["id"]).status == Sale.Status.PARTIALLY_REFUNDED


def test_a_deactivated_device_cannot_push(
    tenant_a, make_stocked_variant, client_for, device, push
):
    variant = make_stocked_variant(tenant_a, quantity=5)
    terminal = device(tenant_a)
    client = client_for(tenant_a.owner, tenant_a.org)
    client.delete(f"/api/v1/sync/devices/{terminal.pk}/")

    response = push(client, terminal, [sale_op(variant)])

    assert response.status_code == 400
    with tenant_context(tenant_a.org.pk):
        assert Sale.objects.count() == 0


def test_a_device_belongs_to_exactly_one_tenant(
    tenant_a, tenant_b, make_stocked_variant, client_for, device, push
):
    variant = make_stocked_variant(tenant_a, quantity=5)
    terminal = device(tenant_a)
    client_b = client_for(tenant_b.owner, tenant_b.org)

    response = push(client_b, terminal, [sale_op(variant)])

    assert response.status_code == 400
    with tenant_context(tenant_a.org.pk):
        assert Sale.objects.count() == 0


def test_pull_returns_only_what_changed(tenant_a, make_stocked_variant, client_for):
    make_stocked_variant(tenant_a, quantity=5, sku="ANTES")
    client = client_for(tenant_a.owner, tenant_a.org)

    full = client.get("/api/v1/sync/pull/")
    assert full.status_code == 200
    assert len(full.data["variants"]) == 1
    cursor = full.data["cursor"]

    make_stocked_variant(tenant_a, quantity=5, sku="DESPUES")
    delta = client.get(f"/api/v1/sync/pull/?since={cursor}")

    assert [v["sku"] for v in delta.data["variants"]] == ["DESPUES"]
    assert delta.data["since"] is not None


def test_pull_never_leaks_another_tenant(tenant_a, tenant_b, make_stocked_variant, client_for):
    make_stocked_variant(tenant_a, quantity=5, sku="DE-A")
    make_stocked_variant(tenant_b, quantity=5, sku="DE-B")

    data = client_for(tenant_b.owner, tenant_b.org).get("/api/v1/sync/pull/").data

    assert [v["sku"] for v in data["variants"]] == ["DE-B"]
    assert len(data["stock"]) == 1
