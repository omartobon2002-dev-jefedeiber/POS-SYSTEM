"""The drawer is opened and closed every day.

A shift left open overnight takes no more money until it is counted, and a
register closed by mistake can only be reopened the same day by a manager.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.models import Membership
from apps.cash.models import CashSession
from apps.cash.services import CashService
from apps.core.context import tenant_context

pytestmark = pytest.mark.django_db


def _age(session, days=1):
    """Pretend the shift was opened `days` ago."""
    with tenant_context(session.organization_id):
        CashSession.objects.filter(pk=session.pk).update(
            opened_at=timezone.now() - timedelta(days=days)
        )


def test_a_shift_from_yesterday_refuses_sales(
    tenant_a, make_stocked_variant, client_for, sell, open_register
):
    register, session = open_register(tenant_a)
    _age(session)
    variant = make_stocked_variant(tenant_a, quantity=5, price="50000.00")

    response = sell(
        client_for(tenant_a.owner, tenant_a.org),
        [{"variant": str(variant.pk), "quantity": 1}],
        cash_register=str(register.pk),
    )

    assert response.status_code == 409
    assert response.data["code"] == "cash_session_stale"


def test_a_shift_from_yesterday_refuses_manual_cash_movements(tenant_a, client_for, open_register):
    _, session = open_register(tenant_a)
    _age(session)

    response = client_for(tenant_a.owner, tenant_a.org).post(
        f"/api/v1/cash/sessions/{session.pk}/movements/",
        {"movement_type": "WITHDRAWAL", "amount": "1000.00", "note": "x"},
        format="json",
    )

    assert response.status_code == 409
    assert response.data["code"] == "cash_session_stale"


def test_a_stale_shift_can_still_be_closed_and_is_flagged(tenant_a, client_for, open_register):
    _, session = open_register(tenant_a)
    _age(session)
    client = client_for(tenant_a.owner, tenant_a.org)

    detail = client.get(f"/api/v1/cash/sessions/{session.pk}/")
    assert detail.data["is_stale"] is True

    closed = client.post(
        f"/api/v1/cash/sessions/{session.pk}/close/", {"counted_amount": "100000.00"}, format="json"
    )
    assert closed.status_code == 200
    assert closed.data["is_stale"] is False


def test_an_offline_sale_is_accepted_on_a_stale_shift(
    tenant_a, make_stocked_variant, open_register
):
    """Replayed from a terminal that was offline: it already happened in the store."""
    from apps.customers.models import Customer
    from apps.sales.models import Sale
    from apps.sales.services import SaleService

    register, session = open_register(tenant_a)
    _age(session)
    variant = make_stocked_variant(tenant_a, quantity=5, price="50000.00")

    with tenant_context(tenant_a.org.pk):
        customer = Customer.objects.create(organization=tenant_a.org, name="Offline")
        sale = SaleService.create_sale(
            organization=tenant_a.org,
            location=tenant_a.location,
            lines=[{"variant": variant, "quantity": 1}],
            payments=[{"method": "CASH", "amount": "50000.00"}],
            customer=customer,
            cash_register=register,
            source=Sale.Source.SYNC,
        )
    assert sale.cash_session_id == session.pk


def test_only_a_manager_can_reopen_a_register_closed_today(
    tenant_a, make_employee, client_for, open_register
):
    register, session = open_register(tenant_a)
    with tenant_context(tenant_a.org.pk):
        CashService.close_session(session=session, counted_amount="100000.00", user=tenant_a.owner)

    cashier = make_employee(tenant_a, role=Membership.Role.CASHIER)
    body = {"register": str(register.pk), "opening_amount": "0", "reopen": True}

    denied = client_for(cashier.user, tenant_a.org).post("/api/v1/cash/sessions/", body, format="json")
    assert denied.status_code == 403

    plain = client_for(tenant_a.owner, tenant_a.org).post(
        "/api/v1/cash/sessions/", {**body, "reopen": False}, format="json"
    )
    assert plain.status_code == 409
    assert plain.data["code"] == "register_already_used_today"

    reopened = client_for(tenant_a.owner, tenant_a.org).post("/api/v1/cash/sessions/", body, format="json")
    assert reopened.status_code == 201
