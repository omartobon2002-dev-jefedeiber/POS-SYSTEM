"""Web Push helpers for sale alerts."""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from apps.accounts.models import Membership
from apps.core.context import tenant_context
from apps.notifications.models import WebPushSubscription
from apps.notifications.services import (
    _format_money,
    can_receive_sale_push,
    notify_sale,
    recipient_user_ids,
)

pytestmark = pytest.mark.django_db


def test_only_owner_receives_sale_push(tenant_a, make_employee):
    cashier = make_employee(tenant_a, role=Membership.Role.CASHIER)
    assert can_receive_sale_push(tenant_a.membership) is True
    assert can_receive_sale_push(cashier) is False
    assert recipient_user_ids(organization=tenant_a.org) == [tenant_a.owner.pk]


def test_format_money_cop():
    assert _format_money(Decimal("380000"), "COP") == "$380.000"


def test_notify_sale_skips_without_vapid(tenant_a, settings):
    settings.VAPID_PUBLIC_KEY = ""
    settings.VAPID_PRIVATE_KEY = ""
    sale = SimpleNamespace(
        pk="00000000-0000-0000-0000-000000000001",
        total=Decimal("1000"),
        seller=tenant_a.owner,
        organization_id=tenant_a.org.pk,
    )
    with patch("pywebpush.webpush") as mocked:
        notify_sale(organization=tenant_a.org, sale=sale)
        mocked.assert_not_called()


def test_notify_sale_sends_to_owner_subscription(tenant_a, settings):
    settings.VAPID_PUBLIC_KEY = "BPtest-public"
    settings.VAPID_PRIVATE_KEY = "test-private"
    settings.VAPID_SUBJECT = "mailto:test@example.com"

    with tenant_context(tenant_a.org.pk):
        WebPushSubscription.objects.create(
            organization=tenant_a.org,
            user=tenant_a.owner,
            endpoint="https://push.example/endpoint",
            p256dh="p256dh-key",
            auth="auth-key",
        )
    sale = SimpleNamespace(
        pk="00000000-0000-0000-0000-000000000002",
        total=Decimal("380000"),
        seller=tenant_a.owner,
        organization_id=tenant_a.org.pk,
    )

    with patch("pywebpush.webpush") as mocked:
        notify_sale(organization=tenant_a.org, sale=sale)
        assert mocked.call_count == 1
        kwargs = mocked.call_args.kwargs
        assert kwargs["subscription_info"]["endpoint"] == "https://push.example/endpoint"
        assert "380.000" in kwargs["data"]
