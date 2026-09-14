"""Subscription access is a hard gate: no login, no reads, no writes."""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.core.context import tenant_context
from apps.subscriptions.models import Subscription

pytestmark = pytest.mark.django_db


def set_status(tenant, status):
    with tenant_context(tenant.org.pk):
        Subscription.objects.filter(organization=tenant.org).update(status=status)


def test_a_cancelled_subscription_blocks_reads_and_writes(tenant_a, make_variant, client_for):
    make_variant(tenant_a)
    client = client_for(tenant_a.owner, tenant_a.org)
    set_status(tenant_a, Subscription.Status.CANCELLED)

    assert client.get("/api/v1/products/").status_code == 403
    write = client.post("/api/v1/products/", {"name": "Nuevo"}, format="json")
    assert write.status_code == 403
    assert write.data["code"] == "subscription_inactive"


def test_a_past_due_subscription_is_blocked(tenant_a, client_for):
    client = client_for(tenant_a.owner, tenant_a.org)
    set_status(tenant_a, Subscription.Status.PAST_DUE)
    assert client.post("/api/v1/brands/", {"name": "Marca B"}, format="json").status_code == 403
    assert client.get("/api/v1/brands/").status_code == 403


def test_an_expired_trial_blocks_everything(tenant_a, client_for):
    client = client_for(tenant_a.owner, tenant_a.org)
    with tenant_context(tenant_a.org.pk):
        Subscription.objects.filter(organization=tenant_a.org).update(
            trial_ends_at=timezone.now() - timedelta(days=1)
        )

    response = client.post("/api/v1/brands/", {"name": "Marca C"}, format="json")
    assert response.status_code == 403
    assert client.get("/api/v1/brands/").status_code == 403


def test_active_subscription_keeps_working(tenant_a, client_for):
    client = client_for(tenant_a.owner, tenant_a.org)
    with tenant_context(tenant_a.org.pk):
        Subscription.objects.filter(organization=tenant_a.org).update(
            status=Subscription.Status.ACTIVE,
            current_period_end=timezone.now() + timedelta(days=30),
            trial_ends_at=None,
        )
    assert client.post("/api/v1/brands/", {"name": "Marca A"}, format="json").status_code == 201
