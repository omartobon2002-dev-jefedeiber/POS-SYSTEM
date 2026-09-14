"""Cash control: only cash reaches the drawer, and the arqueo must add up."""
from __future__ import annotations

from decimal import Decimal

import pytest

from apps.cash.models import CashMovementType, CashRegister, CashSession
from apps.cash.services import CashService
from apps.core.context import tenant_context
from apps.core.exceptions import InvalidOperation

pytestmark = pytest.mark.django_db


def test_provisioning_creates_a_default_register(tenant_a):
    """A store must be able to take cash on day one, no setup step required."""
    with tenant_context(tenant_a.org.pk):
        register = CashRegister.objects.get()

    assert register.location_id == tenant_a.location.pk
    assert register.code == "PRINCIPAL"


def test_a_register_can_only_have_one_open_session(tenant_a, open_register):
    register, _ = open_register(tenant_a)

    with tenant_context(tenant_a.org.pk), pytest.raises(InvalidOperation):
        CashService.open_session(
            organization=tenant_a.org, register=register, user=tenant_a.owner, opening_amount=0
        )


def test_a_cash_sale_reaches_the_drawer(tenant_a, make_stocked_variant, client_for, sell, open_register):
    register, session = open_register(tenant_a, opening_amount="100000.00")
    variant = make_stocked_variant(tenant_a, quantity=10, price="119000.00")
    client = client_for(tenant_a.owner, tenant_a.org)

    sell(
        client,
        [{"variant": str(variant.pk), "quantity": 1}],
        cash_register=str(register.pk),
    )

    with tenant_context(tenant_a.org.pk):
        assert CashService.expected_amount(session) == Decimal("219000.00")


def test_change_given_leaves_the_drawer(tenant_a, make_stocked_variant, client_for, sell, open_register):
    """The customer hands over 150.000 and takes 31.000 back: 119.000 stays."""
    register, session = open_register(tenant_a, opening_amount="0.00")
    variant = make_stocked_variant(tenant_a, quantity=10, price="119000.00")
    client = client_for(tenant_a.owner, tenant_a.org)

    sell(
        client,
        [{"variant": str(variant.pk), "quantity": 1}],
        payments=[{"method": "CASH", "amount": "150000.00"}],
        cash_register=str(register.pk),
    )

    with tenant_context(tenant_a.org.pk):
        assert CashService.expected_amount(session) == Decimal("119000.00")


def test_card_payments_do_not_move_the_drawer(
    tenant_a, make_stocked_variant, client_for, sell, open_register
):
    """Otherwise every arqueo would show a difference that does not exist."""
    register, session = open_register(tenant_a, opening_amount="100000.00")
    variant = make_stocked_variant(tenant_a, quantity=10, price="119000.00")
    client = client_for(tenant_a.owner, tenant_a.org)

    sell(
        client,
        [{"variant": str(variant.pk), "quantity": 1}],
        payments=[{"method": "CARD", "amount": "119000.00", "reference": "APR-1"}],
        cash_register=str(register.pk),
    )

    with tenant_context(tenant_a.org.pk):
        assert CashService.expected_amount(session) == Decimal("100000.00")
        summary = CashService.session_summary(session)
    assert summary["payments_by_method"]["CARD"] == Decimal("119000.00")
    assert summary["sales_total"] == Decimal("119000.00")


def test_withdrawals_and_deposits_move_the_expected_balance(tenant_a, open_register, client_for):
    register, session = open_register(tenant_a, opening_amount="100000.00")
    client = client_for(tenant_a.owner, tenant_a.org)
    url = f"/api/v1/cash/sessions/{session.pk}/movements/"

    client.post(url, {"movement_type": "WITHDRAWAL", "amount": "30000.00"}, format="json")
    client.post(url, {"movement_type": "DEPOSIT", "amount": "5000.00"}, format="json")

    with tenant_context(tenant_a.org.pk):
        assert CashService.expected_amount(session) == Decimal("75000.00")
        summary = CashService.session_summary(session)
    assert summary["total_withdrawals"] == Decimal("30000.00")
    assert summary["total_deposits"] == Decimal("5000.00")
    assert summary["total_sales_cash"] == Decimal("0.00")


def test_closing_computes_the_difference(tenant_a, open_register, client_for):
    register, session = open_register(tenant_a, opening_amount="100000.00")
    client = client_for(tenant_a.owner, tenant_a.org)

    response = client.post(
        f"/api/v1/cash/sessions/{session.pk}/close/",
        {"counted_amount": "95000.00", "notes": "Faltante"},
        format="json",
    )

    assert response.status_code == 200
    assert response.data["status"] == "CLOSED"
    assert Decimal(response.data["expected_amount"]) == Decimal("100000.00")
    assert Decimal(response.data["counted_amount"]) == Decimal("95000.00")
    assert Decimal(response.data["difference"]) == Decimal("-5000.00")


def test_a_closed_session_accepts_nothing_more(tenant_a, open_register, client_for):
    register, session = open_register(tenant_a)
    client = client_for(tenant_a.owner, tenant_a.org)
    client.post(
        f"/api/v1/cash/sessions/{session.pk}/close/", {"counted_amount": "100000.00"}, format="json"
    )

    again = client.post(
        f"/api/v1/cash/sessions/{session.pk}/close/", {"counted_amount": "100000.00"}, format="json"
    )
    movement = client.post(
        f"/api/v1/cash/sessions/{session.pk}/movements/",
        {"movement_type": "DEPOSIT", "amount": "1000.00"},
        format="json",
    )

    assert again.status_code == 400
    assert movement.status_code == 400


def test_taking_cash_without_an_open_session_is_refused(
    tenant_a, make_stocked_variant, client_for, sell
):
    from apps.cash.models import CashRegister

    with tenant_context(tenant_a.org.pk):
        register = CashRegister.objects.create(
            organization=tenant_a.org, location=tenant_a.location, name="Caja 2", code="CAJA2"
        )
    variant = make_stocked_variant(tenant_a, quantity=5, price="119000.00")
    client = client_for(tenant_a.owner, tenant_a.org)

    response = sell(
        client, [{"variant": str(variant.pk), "quantity": 1}], cash_register=str(register.pk)
    )

    assert response.status_code == 400
    assert response.data["code"] == "invalid_operation"


def test_a_cash_refund_takes_money_out_of_the_drawer(
    tenant_a, make_stocked_variant, client_for, sell, open_register
):
    import uuid

    register, session = open_register(tenant_a, opening_amount="100000.00")
    variant = make_stocked_variant(tenant_a, quantity=10, price="119000.00")
    client = client_for(tenant_a.owner, tenant_a.org)
    sale = sell(
        client, [{"variant": str(variant.pk), "quantity": 2}], cash_register=str(register.pk)
    ).data

    client.post(
        "/api/v1/refunds/",
        {
            "sale": sale["id"],
            "cash_register": str(register.pk),
            "lines": [{"sale_item": sale["items"][0]["id"], "quantity": 1}],
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )

    with tenant_context(tenant_a.org.pk):
        # 100.000 float + 238.000 sale - 119.000 refund
        assert CashService.expected_amount(session) == Decimal("219000.00")
        summary = CashService.session_summary(session)
    assert summary["movements_by_type"][CashMovementType.REFUND] == Decimal("-119000.00")


def test_cancelling_a_cash_sale_reverses_the_drawer(
    tenant_a, make_stocked_variant, client_for, sell, open_register
):
    register, session = open_register(tenant_a, opening_amount="100000.00")
    variant = make_stocked_variant(tenant_a, quantity=10, price="119000.00")
    client = client_for(tenant_a.owner, tenant_a.org)
    sale = sell(
        client, [{"variant": str(variant.pk), "quantity": 1}], cash_register=str(register.pk)
    ).data

    client.post(f"/api/v1/sales/{sale['id']}/cancel/", {"reason": "Error de cajero"}, format="json")

    with tenant_context(tenant_a.org.pk):
        assert CashService.expected_amount(session) == Decimal("100000.00")


def test_a_cash_sale_without_a_register_is_refused_while_a_session_is_open(
    tenant_a, make_stocked_variant, client_for, sell, open_register
):
    """The exact failure mode this guards: a cash sale that would otherwise
    complete successfully but never reach any drawer, silently."""
    open_register(tenant_a, opening_amount="100000.00")
    variant = make_stocked_variant(tenant_a, quantity=10, price="119000.00")
    client = client_for(tenant_a.owner, tenant_a.org)

    response = sell(client, [{"variant": str(variant.pk), "quantity": 1}])

    assert response.status_code == 400
    assert response.data["code"] == "invalid_operation"
    with tenant_context(tenant_a.org.pk):
        from apps.sales.models import Sale

        assert Sale.objects.count() == 0


def test_a_cash_sale_without_a_register_still_works_with_no_session_open(
    tenant_a, make_stocked_variant, client_for, sell
):
    """A store that has a register (created automatically) but hasn't opened a
    shift today isn't forced into cash control it isn't using yet."""
    variant = make_stocked_variant(tenant_a, quantity=10, price="119000.00")
    client = client_for(tenant_a.owner, tenant_a.org)

    response = sell(client, [{"variant": str(variant.pk), "quantity": 1}])

    assert response.status_code == 201
    assert response.data["cash_session"] is None


def test_cash_sessions_never_cross_tenants(tenant_a, tenant_b, open_register, client_for):
    _, session = open_register(tenant_a)
    client_b = client_for(tenant_b.owner, tenant_b.org)

    assert client_b.get(f"/api/v1/cash/sessions/{session.pk}/").status_code == 404
    assert client_b.get("/api/v1/cash/sessions/").data["count"] == 0
    with tenant_context(tenant_b.org.pk):
        assert CashSession.objects.count() == 0


def test_a_register_cannot_be_reopened_the_same_day(tenant_a, open_register, client_for):
    """After a normal close, the till stays closed until the next local day."""
    register, session = open_register(tenant_a, opening_amount="100000.00")
    client = client_for(tenant_a.owner, tenant_a.org)

    closed = client.post(
        f"/api/v1/cash/sessions/{session.pk}/close/",
        {"counted_amount": "100000.00"},
        format="json",
    )
    assert closed.status_code == 200
    assert closed.data["close_reason"] == "NORMAL"

    again = client.post(
        "/api/v1/cash/sessions/",
        {"register": str(register.pk), "opening_amount": "50000.00"},
        format="json",
    )
    assert again.status_code == 409
    assert again.data["code"] == "register_already_used_today"


def test_a_register_can_open_again_the_next_day(tenant_a, open_register):
    from datetime import timedelta

    from django.utils import timezone

    register, session = open_register(tenant_a, opening_amount="100000.00")

    with tenant_context(tenant_a.org.pk):
        CashService.close_session(
            session=session, counted_amount="100000.00", user=tenant_a.owner
        )
        session.refresh_from_db()
        yesterday = timezone.now() - timedelta(days=1)
        CashSession.objects.filter(pk=session.pk).update(
            opened_at=yesterday, closed_at=yesterday
        )

        next_day = CashService.open_session(
            organization=tenant_a.org,
            register=register,
            user=tenant_a.owner,
            opening_amount="80000.00",
        )
        assert next_day.status == CashSession.Status.OPEN
        assert next_day.opening_amount == Decimal("80000.00")


def test_transferring_a_session_hands_the_drawer_to_another_cashier(
    tenant_a, make_employee, open_register, client_for, make_stocked_variant, sell
):
    register, session = open_register(tenant_a, opening_amount="100000.00")
    cashier = make_employee(tenant_a, username="caja2")
    client = client_for(tenant_a.owner, tenant_a.org)
    variant = make_stocked_variant(tenant_a, quantity=5, price="119000.00")
    sell(
        client,
        [{"variant": str(variant.pk), "quantity": 1}],
        cash_register=str(register.pk),
    )

    response = client.post(
        f"/api/v1/cash/sessions/{session.pk}/transfer/",
        {
            "counted_amount": "219000.00",
            "to_user": str(cashier.user_id),
            "notes": "Cambio de turno",
        },
        format="json",
    )

    assert response.status_code == 200, response.data
    closed = response.data["closed_session"]
    opened = response.data["opened_session"]
    assert closed["status"] == "CLOSED"
    assert closed["close_reason"] == "TRANSFER"
    assert str(closed["superseded_by"]) == opened["id"]
    assert opened["status"] == "OPEN"
    assert str(opened["opened_by"]) == str(cashier.user_id)
    assert Decimal(opened["opening_amount"]) == Decimal("219000.00")
    assert str(opened["previous_session"]) == closed["id"]

    # Sales continue on the successor session.
    sell(
        client,
        [{"variant": str(variant.pk), "quantity": 1}],
        cash_register=str(register.pk),
    )
    with tenant_context(tenant_a.org.pk):
        successor = CashSession.objects.get(pk=opened["id"])
        assert CashService.expected_amount(successor) == Decimal("338000.00")


def test_transfer_rejects_a_closed_session(tenant_a, make_employee, open_register, client_for):
    _, session = open_register(tenant_a, opening_amount="50000.00")
    cashier = make_employee(tenant_a, username="otro")
    client = client_for(tenant_a.owner, tenant_a.org)

    client.post(
        f"/api/v1/cash/sessions/{session.pk}/close/",
        {"counted_amount": "50000.00"},
        format="json",
    )

    response = client.post(
        f"/api/v1/cash/sessions/{session.pk}/transfer/",
        {"counted_amount": "50000.00", "to_user": str(cashier.user_id)},
        format="json",
    )
    assert response.status_code == 400
    assert response.data["code"] == "invalid_operation"
