"""Two registers, one shoe.

These tests spawn real threads against a real PostgreSQL transaction each, so
they are marked `slow` and excluded from the fast loop with
`pytest -m "not slow"`. They are the only way to prove the locking strategy
works: a single-threaded test cannot fail the way a busy Saturday does.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import connection

from apps.catalog.models import ProductVariant
from apps.core.context import tenant_context
from apps.core.exceptions import InsufficientStock
from apps.customers.models import Customer
from apps.inventory.services import InventoryService
from apps.sales.models import Sale
from apps.sales.services import RefundService, SaleService

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.slow]


def _customer(tenant):
    return Customer.objects.create(organization=tenant.org, name="Cliente Concurrente")


def run_in_parallel(worker, count: int):
    """Run `count` copies of `worker`, released simultaneously by a barrier."""
    barrier = threading.Barrier(count)

    def wrapped(index):
        try:
            barrier.wait(timeout=10)
            return worker(index)
        finally:
            # Each thread owns its connection and must hand it back.
            connection.close()

    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(wrapped, range(count)))


def test_two_registers_cannot_sell_the_same_last_unit(tenant_a, make_stocked_variant):
    """Exactly one sale succeeds and stock lands on zero, never on minus one."""
    variant = make_stocked_variant(tenant_a, quantity=1, price="119000.00")
    org_id = tenant_a.org.pk
    with tenant_context(org_id):
        customer = _customer(tenant_a)

    def sell_one(_):
        try:
            with tenant_context(org_id):
                SaleService.create_sale(
                    organization=tenant_a.org,
                    location=tenant_a.location,
                    lines=[{"variant": ProductVariant.objects.get(pk=variant.pk), "quantity": 1}],
                    payments=[{"method": "CASH", "amount": "119000.00"}],
                    user=tenant_a.owner,
                    customer=Customer.objects.get(pk=customer.pk),
                )
            return "sold"
        except InsufficientStock:
            return "rejected"

    results = run_in_parallel(sell_one, 2)

    assert sorted(results) == ["rejected", "sold"]
    with tenant_context(org_id):
        assert InventoryService.available(location=tenant_a.location, variant=variant) == 0
        assert InventoryService.ledger_balance(location=tenant_a.location, variant=variant) == 0
        assert Sale.objects.count() == 1


def test_concurrent_sales_never_lose_a_unit(tenant_a, make_stocked_variant):
    """Eight simultaneous sales of one unit each against a stock of eight."""
    variant = make_stocked_variant(tenant_a, quantity=8, price="119000.00")
    org_id = tenant_a.org.pk
    with tenant_context(org_id):
        customer = _customer(tenant_a)

    def sell_one(_):
        try:
            with tenant_context(org_id):
                SaleService.create_sale(
                    organization=tenant_a.org,
                    location=tenant_a.location,
                    lines=[{"variant": ProductVariant.objects.get(pk=variant.pk), "quantity": 1}],
                    payments=[{"method": "CASH", "amount": "119000.00"}],
                    user=tenant_a.owner,
                    customer=Customer.objects.get(pk=customer.pk),
                )
            return "sold"
        except InsufficientStock:
            return "rejected"

    results = run_in_parallel(sell_one, 8)

    assert results.count("sold") == 8
    with tenant_context(org_id):
        assert InventoryService.available(location=tenant_a.location, variant=variant) == 0
        assert InventoryService.ledger_balance(location=tenant_a.location, variant=variant) == 0
        numbers = list(Sale.objects.values_list("number", flat=True))
    # Gap-free and unique: the sequence lock did its job.
    assert sorted(numbers) == [f"V-{i:06d}" for i in range(1, 9)]


def test_opposite_line_order_does_not_deadlock(tenant_a, make_stocked_variant):
    """Register 1 sells (A, B) while register 2 sells (B, A).

    Without a deterministic lock order this is a textbook deadlock. The test
    passing at all is the assertion.
    """
    variant_a = make_stocked_variant(tenant_a, quantity=20, price="100000.00", sku="AAA")
    variant_b = make_stocked_variant(tenant_a, quantity=20, price="100000.00", sku="BBB")
    org_id = tenant_a.org.pk
    with tenant_context(org_id):
        customer = _customer(tenant_a)

    def sell_pair(index):
        order = [variant_a, variant_b] if index % 2 == 0 else [variant_b, variant_a]
        with tenant_context(org_id):
            SaleService.create_sale(
                organization=tenant_a.org,
                location=tenant_a.location,
                lines=[
                    {"variant": ProductVariant.objects.get(pk=v.pk), "quantity": 1} for v in order
                ],
                payments=[{"method": "CASH", "amount": "200000.00"}],
                user=tenant_a.owner,
                customer=Customer.objects.get(pk=customer.pk),
            )
        return "sold"

    results = run_in_parallel(sell_pair, 8)

    assert results == ["sold"] * 8
    with tenant_context(org_id):
        assert InventoryService.available(location=tenant_a.location, variant=variant_a) == 12
        assert InventoryService.available(location=tenant_a.location, variant=variant_b) == 12


def test_concurrent_refunds_cannot_exceed_what_was_sold(tenant_a, make_stocked_variant):
    """Two cashiers refunding the same receipt at once must not double-refund."""
    variant = make_stocked_variant(tenant_a, quantity=10, price="119000.00")
    org_id = tenant_a.org.pk

    with tenant_context(org_id):
        customer = _customer(tenant_a)
        sale = SaleService.create_sale(
            organization=tenant_a.org,
            location=tenant_a.location,
            lines=[{"variant": ProductVariant.objects.get(pk=variant.pk), "quantity": 2}],
            payments=[{"method": "CASH", "amount": "238000.00"}],
            user=tenant_a.owner,
            customer=customer,
        )
        item_id = sale.items.first().pk

    def refund_both(_):
        from apps.sales.models import SaleItem

        try:
            with tenant_context(org_id):
                RefundService.create_refund(
                    sale=Sale.objects.get(pk=sale.pk),
                    lines=[{"sale_item": SaleItem.objects.get(pk=item_id), "quantity": 2}],
                    user=tenant_a.owner,
                )
            return "refunded"
        except Exception as exc:  # InvalidOperation once the cap is reached
            return type(exc).__name__

    results = run_in_parallel(refund_both, 2)

    assert results.count("refunded") == 1
    with tenant_context(org_id):
        from apps.sales.models import SaleItem

        assert SaleItem.objects.get(pk=item_id).refunded_quantity == 2
        assert InventoryService.available(location=tenant_a.location, variant=variant) == 10
        assert Sale.objects.get(pk=sale.pk).status == Sale.Status.REFUNDED
