"""Platform operator API."""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Membership
from apps.accounts.tokens import issue_platform_tokens
from apps.core.context import tenant_context
from apps.subscriptions.management.commands.seed_plans import seed_plans
from apps.subscriptions.models import Subscription

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture
def platform_operator(db):
    return User.objects.create_superuser(
        email="ops@modapos.co",
        password="ClaveDePrueba123",
    )


@pytest.fixture
def platform_client(platform_operator):
    client = APIClient()
    tokens = issue_platform_tokens(platform_operator)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
    return client


def test_platform_login(platform_operator, client):
    response = client.post(
        "/api/v1/platform/auth/login/",
        {"email": "ops@modapos.co", "password": "ClaveDePrueba123"},
        format="json",
    )
    assert response.status_code == 200
    assert response.json()["scope"] == "platform"
    assert response.json()["access"]


def test_platform_provisions_a_business(platform_client, db):
    seed_plans()
    response = platform_client.post(
        "/api/v1/platform/organizations/",
        {
            "organization_name": "Tienda Admin",
            "owner_email": "owner@tienda.co",
            "owner_password": "ClaveDePrueba123",
            "months": 1,
        },
        format="json",
    )
    assert response.status_code == 201, response.data
    body = response.json()
    assert body["name"] == "Tienda Admin"
    assert body["owner"]["email"] == "owner@tienda.co"
    assert body["subscription"]["status"] == "ACTIVE"
    assert body["subscription"]["grants_access"] is True


def test_record_payment_reactivates_access(platform_client, tenant_a, client_for):
    seed_plans()
    with tenant_context(tenant_a.org.pk):
        Subscription.objects.filter(organization=tenant_a.org).update(
            status=Subscription.Status.EXPIRED,
            current_period_end=timezone.now() - timedelta(days=1),
        )

    tenant_client = client_for(tenant_a.owner, tenant_a.org)
    assert tenant_client.get("/api/v1/products/").status_code == 403

    now = timezone.now()
    response = platform_client.post(
        f"/api/v1/platform/organizations/{tenant_a.org.id}/payments/",
        {
            "amount": "99000.00",
            "period_start": now.isoformat(),
            "period_end": (now + timedelta(days=30)).isoformat(),
            "method": "TRANSFER",
            "reference": "TRX-1",
        },
        format="json",
    )
    assert response.status_code == 201

    tenant_client = client_for(tenant_a.owner, tenant_a.org)
    assert tenant_client.get("/api/v1/products/").status_code == 200


def test_suspend_organization_blocks_login(platform_client, tenant_a, client):
    response = platform_client.patch(
        f"/api/v1/platform/organizations/{tenant_a.org.id}/",
        {"is_active": False},
        format="json",
    )
    assert response.status_code == 200
    assert response.json()["is_active"] is False

    login = client.post(
        "/api/v1/auth/login/",
        {
            "email": tenant_a.owner.email,
            "password": "ClaveDePrueba123",
        },
        format="json",
    )
    assert login.status_code == 403
    assert login.json()["code"] == "organization_suspended"


def test_export_csv(platform_client, tenant_a):
    response = platform_client.get("/api/v1/platform/organizations/export/")
    assert response.status_code == 200
    assert "text/csv" in response["Content-Type"]
    assert tenant_a.org.slug.encode() in response.content or tenant_a.org.name in response.content.decode()


def test_member_password_reset(platform_client, tenant_a, make_employee):
    emp = make_employee(tenant_a, username="caja1", password="ViejaClave123")
    response = platform_client.post(
        f"/api/v1/platform/organizations/{tenant_a.org.id}/members/{emp.id}/set-password/",
        {"password": "NuevaClave4567"},
        format="json",
    )
    assert response.status_code == 200
    emp.user.refresh_from_db()
    assert emp.user.check_password("NuevaClave4567")
